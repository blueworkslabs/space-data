"""The scheduled job: gate on persisted state, build, publish, record.

    python -m space_data.run [--clear-hold NOTE]

Order matters. First the state and the published dataset are loaded (no
CelesTrak traffic). If that fails, stop. Then gate: a hold or the minimum
interval ends the run here. Then the attempt is saved. Only after that are
requests made. An upstream failure saves a hold. Exit codes: 0 published,
unchanged or waiting; 1 held, upstream failure or state/publication error.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

from . import build as B
from . import fetch as F
from . import state as S


class PublishError(RuntimeError):
    pass


def git_published(cwd=None):
    """Extract what `pages` serves into a temporary directory; None when the
    branch does not exist yet. Raises PublishError when Git cannot tell."""
    r = subprocess.run(["git", "ls-remote", "--exit-code", "--heads", "origin", "refs/heads/pages"],
                       cwd=cwd, capture_output=True, text=True)
    if r.returncode == 2:
        return None
    if r.returncode != 0:
        raise PublishError(f"cannot list pages: {r.stderr.strip()}")
    target = tempfile.mkdtemp(prefix="published-")
    r = subprocess.run(f"git fetch -q --depth=1 origin pages && git archive FETCH_HEAD | tar -x -C '{target}'",
                       shell=True, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        shutil.rmtree(target, ignore_errors=True)
        raise PublishError(f"cannot read pages: {r.stderr.strip()}")
    return target


def script_publisher(public, cwd=None):
    r = subprocess.run(["bash", "scripts/publish.sh", public], cwd=cwd, capture_output=True, text=True)
    print(r.stdout, end="")
    if r.returncode != 0:
        raise PublishError(r.stderr.strip() or "publish.sh failed")
    return "unchanged" if "Data unchanged" in r.stdout else "published"


def run(store, fetch, published_loader, publisher, now, out="public", work="work", clear_note=None, log=print):
    """Returns (exit code, message)."""
    try:
        state = store.load()
    except S.StateError as e:
        return 1, f"state unreadable, not querying CelesTrak: {e}"
    if clear_note:
        state = S.clear(state, now, clear_note)
        try:
            store.save(state)
        except S.StateError as e:
            return 1, f"could not clear the hold: {e}"
    verdict, why = S.gate(state, now)
    if verdict == "hold":
        return 1, why
    if verdict == "wait":
        return 0, why
    try:
        published = published_loader()
    except PublishError as e:
        return 1, f"published data unreadable, not querying CelesTrak: {e}"
    try:
        store.save(S.begin(state, now))
    except S.StateError as e:
        return 1, f"could not record the attempt, not querying CelesTrak: {e}"
    try:
        marker = B.satcat_marker(fetch)
        known = (state.get("satcat") or {}).get("marker")
        reuse = B.published_satcat(published) if published and marker == known else None
        log("SATCAT unchanged since last check; reusing published catalogue rows" if reuse
            else "SATCAT updated or not yet published; downloading catalogue lists")
        data = B.collect(fetch, reuse)
    except (F.Stop, ValueError) as e:
        reason = f"upstream: {e}"
        try:
            store.save(S.fail(state, now, reason))
        except S.StateError as se:
            return 1, f"{reason}; failure detail not saved ({se}); pre-request hold remains"
        return 1, f"{reason}; hold set"
    finally:
        if published:
            shutil.rmtree(published, ignore_errors=True)
    index = B.write(out, data, now)
    os.makedirs(work, exist_ok=True)
    with open(os.path.join(work, "report.md"), "w", encoding="utf-8") as fh:
        fh.write(B.report(index) + f"\nSATCAT: {'reused' if reuse else 'downloaded'} (marker {marker})\n")
    try:
        outcome = publisher(out)
    except PublishError as e:
        return 1, f"publication failed (pre-request hold remains; investigate before retrying): {e}"
    try:
        store.save(S.succeed(state, now, marker, f"{outcome} {index['dataset']}"))
    except S.StateError as e:
        return 1, f"{outcome} {index['dataset']}, but state not saved: {e}"
    return 0, f"{outcome} {index['dataset']}"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--clear-hold", default="", help="clear a failure hold, with a short note")
    p.add_argument("--out", default="public")
    p.add_argument("--work", default="work")
    a = p.parse_args(argv)
    if os.path.exists(a.out) and os.listdir(a.out):
        sys.exit(f"{a.out} is not empty")
    code, message = run(S.GitStore(), F.saving(F.live(), os.path.join(a.work, "raw")), git_published,
                        script_publisher, datetime.now(timezone.utc), a.out, a.work, a.clear_hold.strip() or None)
    print(message)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(f"**{message}**\n\n")
            report = os.path.join(a.work, "report.md")
            if os.path.exists(report):
                with open(report, encoding="utf-8") as r:
                    fh.write(r.read())
    sys.exit(code)


if __name__ == "__main__":
    main()
