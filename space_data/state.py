"""Run state that survives across workflow runs: cadence and failure hold.

Every entry point (cron, dispatch, rerun) goes through gate() before any
CelesTrak request, and the attempt is persisted before the first request.
So a cancelled or crashed run still counts toward the minimum interval. An
upstream failure (non-200, redirect, bad data) sets a hold that blocks every
later attempt until a human clears it explicitly. If the state cannot be read
or written, nothing is requested (fail closed).

The state lives on the orphan branch `state` as state.json. Cloudflare Pages
serves only `pages`.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone

SCHEMA = 1
# CelesTrak updates GP data every 2 hours. Cron runs every 4 hours; 3 hours
# leaves room for GitHub's late cron starts without allowing extra downloads.
MIN_INTERVAL = timedelta(hours=3)
FILE = "state.json"


class StateError(RuntimeError):
    """State unreadable, malformed or not saved; do not query CelesTrak."""


def iso(t):
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def initial():
    return {"schema": SCHEMA, "hold": None, "lastAttempt": None, "lastSuccess": None, "satcat": None, "log": []}


def check(state):
    """The state, or StateError if anything about it is off."""
    if not isinstance(state, dict) or state.get("schema") != SCHEMA:
        raise StateError("state schema")
    try:
        for key in ("lastAttempt", "lastSuccess"):
            if state.get(key) is not None:
                parse_time(state[key])
        hold = state.get("hold")
        if hold is not None and not (isinstance(hold, dict) and isinstance(hold.get("reason"), str)):
            raise StateError("hold")
        if hold is not None:
            parse_time(hold["since"])
        sat = state.get("satcat")
        if sat is not None and not (isinstance(sat, dict) and isinstance(sat.get("marker"), dict)):
            raise StateError("satcat")
        if not isinstance(state.get("log"), list):
            raise StateError("log")
    except (KeyError, TypeError, ValueError) as e:
        raise StateError(f"state field: {e}") from None
    return state


def note(state, now, text):
    state["log"] = (state["log"] + [f"{iso(now)} {text}"])[-20:]


def gate(state, now):
    """("go" | "wait" | "hold", message)."""
    hold = state["hold"]
    if hold:
        return "hold", (f"Held since {hold['since']}: {hold['reason']}. Investigate, then run the workflow "
                        "manually with clear_hold set to a short note.")
    if state["lastAttempt"]:
        last = parse_time(state["lastAttempt"])
        if now < last:
            return "wait", f"last attempt {state['lastAttempt']} is in the future; not querying"
        if now - last < MIN_INTERVAL:
            return "wait", f"last attempt {state['lastAttempt']}; next allowed after {iso(last + MIN_INTERVAL)}"
    return "go", ""


def begin(state, now):
    state["lastAttempt"] = iso(now)
    note(state, now, "attempt")
    return state


def fail(state, now, reason):
    state["hold"] = {"since": iso(now), "reason": reason[:500]}
    note(state, now, f"hold: {reason[:200]}")
    return state


def succeed(state, now, satcat_marker, outcome):
    state["lastSuccess"] = iso(now)
    if satcat_marker is not None:
        state["satcat"] = {"marker": satcat_marker, "checked": iso(now)}
    note(state, now, outcome)
    return state


def clear(state, now, text):
    if state["hold"]:
        note(state, now, f"hold cleared: {text[:200]}")
        state["hold"] = None
    return state


class DirStore:
    """State in a local file (tests, local runs)."""

    def __init__(self, path):
        self.path = path

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                return check(json.load(fh))
        except FileNotFoundError:
            return initial()
        except (OSError, ValueError) as e:
            raise StateError(f"{self.path}: {e}") from None

    def save(self, state):
        try:
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(check(state), fh, indent=1)
        except OSError as e:
            raise StateError(f"{self.path}: {e}") from None


class GitStore:
    """State as state.json on an orphan branch, one commit, written with a lease."""

    def __init__(self, remote="origin", branch="state", cwd=None):
        self.remote, self.branch, self.cwd = remote, branch, cwd
        self.expected = None

    def git(self, *args, stdin=None):
        r = subprocess.run(["git", *args], cwd=self.cwd, input=stdin, capture_output=True, text=True)
        return r.returncode, r.stdout.strip(), r.stderr.strip()

    def load(self):
        code, out, err = self.git("ls-remote", "--exit-code", "--heads", self.remote, f"refs/heads/{self.branch}")
        if code == 2:  # no such branch: first run
            self.expected = ""
            return initial()
        if code != 0 or not out:
            raise StateError(f"cannot list {self.branch}: {err or code}")
        sha = out.split()[0]
        code, _, err = self.git("fetch", "-q", "--depth=1", self.remote, self.branch)
        if code == 0:
            code, fetched, err = self.git("rev-parse", "FETCH_HEAD")
            if code == 0 and fetched != sha:
                code, err = 1, f"{self.branch} moved during load"
        if code != 0:
            raise StateError(f"cannot fetch {self.branch}: {err}")
        code, body, err = self.git("show", f"{sha}:{FILE}")
        if code != 0:
            raise StateError(f"{self.branch} has no {FILE}: {err}")
        try:
            state = check(json.loads(body))
        except ValueError as e:
            raise StateError(f"{FILE}: {e}") from None
        self.expected = sha
        return state

    def save(self, state):
        if self.expected is None:
            raise StateError("save before load")
        body = json.dumps(check(state), indent=1) + "\n"
        code, blob, err = self.git("hash-object", "-w", "--stdin", stdin=body)
        if code == 0:
            code, tree, err = self.git("mktree", stdin=f"100644 blob {blob}\t{FILE}\n")
        if code == 0:
            code, commit, err = self.git("-c", "user.name=space-data build", "-c",
                                         "user.email=actions@users.noreply.github.com",
                                         "commit-tree", tree, "-m", "Run state")
        if code == 0:
            code, _, err = self.git("push", "-q", f"--force-with-lease=refs/heads/{self.branch}:{self.expected}",
                                    self.remote, f"{commit}:refs/heads/{self.branch}")
        if code != 0:
            raise StateError(f"cannot save state: {err}")
        self.expected = commit
