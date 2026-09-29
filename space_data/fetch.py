"""CelesTrak downloads under its usage policy (celestrak.org/usage-policy.php).

Every response other than HTTP 200 with JSON stops the whole build: no retry,
no redirect following, nothing published. The failing workflow run is the
report to a human that the policy asks for.
"""
from __future__ import annotations

import json
import http.client
import os
import time
import urllib.error
import urllib.request

from . import contract as C


class Stop(RuntimeError):
    """CelesTrak did not answer 200 JSON; stop querying."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Stop(f"HTTP {code} redirect from {req.full_url} to {newurl}; not following")


_opener = urllib.request.build_opener(_NoRedirect)


def live(pause_s=2.0, timeout_s=90):
    """A fetch(url) -> parsed JSON that talks to CelesTrak, one request at a time."""
    state = {"last": 0.0}

    def fetch(url):
        wait = state["last"] + pause_s - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(url, headers={"User-Agent": C.USER_AGENT, "Accept": "application/json"})
        try:
            with _opener.open(req, timeout=timeout_s) as resp:
                status, ctype, body = resp.status, resp.headers.get("Content-Type", ""), resp.read()
        except urllib.error.HTTPError as e:
            raise Stop(f"HTTP {e.code} from {url}") from None
        except urllib.error.URLError as e:
            raise Stop(f"{url}: {e.reason}") from None
        except (OSError, http.client.HTTPException) as e:
            raise Stop(f"{url}: {type(e).__name__}: {e}") from None
        finally:
            state["last"] = time.monotonic()
        if status != 200:
            raise Stop(f"HTTP {status} from {url}")
        if "json" not in ctype.lower():
            raise Stop(f"{url} answered {ctype or 'no content type'}, not JSON")
        try:
            return json.loads(body)
        except ValueError:
            raise Stop(f"{url} answered invalid JSON") from None

    return fetch


def raw_name(url):
    """File name for a saved response: <group>.gp.json, <group>.satcat.json
    or satcat-dir.json."""
    if url == C.SATCAT_DIR_URL:
        return "satcat-dir.json"
    group = url.split("GROUP=", 1)[1].split("&", 1)[0]
    return f"{group}.{'satcat' if '/satcat/' in url else 'gp'}.json"


def saved(directory):
    """A fetch(url) that reads responses saved by --save-raw (tests, reruns)."""

    def fetch(url):
        path = os.path.join(directory, raw_name(url))
        if not os.path.exists(path):
            raise Stop(f"no saved response {path}")
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    return fetch


def saving(fetch, directory):
    os.makedirs(directory, exist_ok=True)

    def wrapped(url):
        data = fetch(url)
        with open(os.path.join(directory, raw_name(url)), "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        return data

    return wrapped
