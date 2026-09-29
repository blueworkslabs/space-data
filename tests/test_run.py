"""Cross-run cadence, failure hold and SATCAT reuse, without live requests."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from space_data import build as B, contract as C, fetch as F, run as Run, state as S
from tests.test_pipeline import FIX, at, small_contract

T0 = at("2026-09-30T00:37:00Z")


class Upstream:
    """Saved fixtures plus a SATCAT directory listing; records every request
    and whether the attempt had been saved before it."""

    def __init__(self, store, marker=("2026-09-29 19:09:43 UTC", 6751394), fail_on=None, interrupt_on=None):
        self.store, self.marker, self.fail_on, self.interrupt_on = store, marker, fail_on, interrupt_on
        self.urls, self.saved = [], F.saved(FIX)

    def __call__(self, url):
        state = self.store.load()
        assert state["lastAttempt"], "a request before the attempt was saved"
        self.urls.append(url)
        if self.fail_on and self.fail_on in url:
            raise F.Stop(f"HTTP 403 from {url}")
        if self.interrupt_on and self.interrupt_on in url:
            raise KeyboardInterrupt
        if url == C.SATCAT_DIR_URL:
            return [{"FILE_NAME": "satcat.csv", "FILE_MTIME": self.marker[0], "FILE_SIZE": self.marker[1]}]
        return self.saved(url)

    def satcat_lists(self):
        return [u for u in self.urls if "/satcat/records.php" in u]


class Harness:
    def __init__(self, tmp):
        self.tmp = Path(tmp)
        self.store = S.DirStore(str(self.tmp / "state.json"))
        self.pages = self.tmp / "pages"
        self.published_calls = 0
        self.n = 0

    def loader(self):
        self.published_calls += 1
        if not self.pages.exists():
            return None
        copy = tempfile.mkdtemp(dir=self.tmp)
        shutil.rmtree(copy)
        shutil.copytree(self.pages, copy)
        return copy

    def publisher(self, public):
        if self.pages.exists():
            shutil.rmtree(self.pages)
        shutil.copytree(public, self.pages)
        return "published"

    def run(self, now, upstream=None, clear=None, loader=None):
        self.n += 1
        up = upstream or Upstream(self.store)
        out, work = self.tmp / f"public{self.n}", self.tmp / f"work{self.n}"
        code, msg = Run.run(self.store, up, loader or self.loader, self.publisher, now, str(out), str(work),
                            clear, log=lambda *_: None)
        return code, msg, up


class Cadence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.h = Harness(self.tmp)
        ctx = small_contract(max_bytes=1_000_000)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)

    def test_first_run_publishes_and_records(self):
        code, msg, up = self.h.run(T0)
        self.assertEqual((code, msg), (0, "published 20260930T0037Z"))
        self.assertEqual(up.urls[0], C.SATCAT_DIR_URL)
        self.assertEqual(len(up.satcat_lists()), 4)
        state = self.h.store.load()
        self.assertEqual(state["lastAttempt"], "2026-09-30T00:37:00Z")
        self.assertEqual(state["lastSuccess"], "2026-09-30T00:37:00Z")
        self.assertEqual(state["satcat"]["marker"], {"mtime": "2026-09-29 19:09:43 UTC", "size": 6751394})
        self.assertIsNone(state["hold"])

    def test_immediate_dispatch_or_rerun_waits_without_requests(self):
        self.h.run(T0)
        for later in (timedelta(0), timedelta(minutes=5), timedelta(hours=2, minutes=59)):
            code, msg, up = self.h.run(T0 + later)
            self.assertEqual(code, 0)
            self.assertIn("next allowed after 2026-09-30T03:37:00Z", msg)
            self.assertEqual(up.urls, [])

    def test_satcat_unchanged_is_reused_updated_is_downloaded(self):
        self.h.run(T0)
        code, msg, up = self.h.run(T0 + timedelta(hours=4))
        self.assertEqual(code, 0, msg)
        self.assertEqual(up.satcat_lists(), [], "unchanged SATCAT: only the directory is checked")
        self.assertEqual(sum(u == C.SATCAT_DIR_URL for u in up.urls), 1)
        index = json.loads((self.h.pages / "v1" / "index.json").read_text())
        self.assertEqual(index["dataset"], "20260930T0437Z")
        with open(os.path.join(FIX, "visual.satcat.json"), encoding="utf-8") as fh:
            from space_data.rows import rows, satcat_row
            want = rows(json.load(fh), satcat_row)
        self.assertEqual(B.published_satcat(str(self.h.pages))["visual"], want)
        changed = Upstream(self.h.store, marker=("2026-09-30 07:00:00 UTC", 6751500))
        code, msg, up = self.h.run(T0 + timedelta(hours=8), changed)
        self.assertEqual(code, 0, msg)
        self.assertEqual(len(up.satcat_lists()), 4)
        self.assertEqual(self.h.store.load()["satcat"]["marker"]["mtime"], "2026-09-30 07:00:00 UTC")

    def test_unpublished_or_damaged_catalogue_is_downloaded(self):
        self.h.run(T0)
        index = json.loads((self.h.pages / "v1" / "index.json").read_text())
        victim = self.h.pages / "v1" / index["path"] / "geo" / "satcat-1.json"
        victim.write_text(victim.read_text().replace('"PAY"', '"XXX"', 1))
        code, msg, up = self.h.run(T0 + timedelta(hours=4))
        self.assertEqual(code, 0, msg)
        self.assertEqual(len(up.satcat_lists()), 4, "a damaged published catalogue is not reused")

    def test_upstream_failure_holds_until_cleared(self):
        self.h.run(T0)
        failing = Upstream(self.h.store, fail_on="GROUP=gnss")
        code, msg, up = self.h.run(T0 + timedelta(hours=4), failing)
        self.assertEqual(code, 1)
        self.assertIn("hold set", msg)
        before = (self.h.pages / "v1" / "index.json").read_text()
        self.assertIn("20260930T0037Z", before, "nothing published on failure")
        for later in (timedelta(hours=8), timedelta(days=3)):
            code, msg, up = self.h.run(T0 + later)
            self.assertEqual(code, 1)
            self.assertIn("Held since 2026-09-30T04:37:00Z: upstream: HTTP 403", msg)
            self.assertEqual(up.urls, [], "a hold blocks every later attempt")
        code, msg, up = self.h.run(T0 + timedelta(days=3, hours=1), clear="checked: CelesTrak maintenance")
        self.assertEqual(code, 0, msg)
        self.assertTrue(msg.startswith("published"), msg)
        log = self.h.store.load()["log"]
        self.assertTrue(any("hold cleared: checked: CelesTrak maintenance" in line for line in log))

    def test_clearing_does_not_bypass_the_interval(self):
        self.h.run(T0, Upstream(self.h.store, fail_on="GROUP=visual"))
        code, msg, up = self.h.run(T0 + timedelta(hours=1), clear="retry")
        self.assertEqual(code, 0)
        self.assertIn("next allowed", msg)
        self.assertEqual(up.urls, [])

    def test_bad_data_holds_too(self):
        empty = Upstream(self.h.store)
        empty.saved = lambda url: [] if "GROUP=geo" in url and "/satcat/" in url else F.saved(FIX)(url)
        code, msg, _ = self.h.run(T0, empty)
        self.assertEqual(code, 1)
        self.assertIn("geo: no valid catalogue records", msg)
        self.assertIsNotNone(self.h.store.load()["hold"])

    def test_cancelled_run_still_counts(self):
        with self.assertRaises(KeyboardInterrupt):
            self.h.run(T0, Upstream(self.h.store, interrupt_on="GROUP=last-30-days"))
        state = self.h.store.load()
        self.assertEqual(state["lastAttempt"], "2026-09-30T00:37:00Z")
        self.assertIsNotNone(state["hold"], "an incomplete attempt needs investigation")
        code, msg, up = self.h.run(T0 + timedelta(minutes=1))
        self.assertEqual((code, up.urls), (1, []))
        code, msg, up = self.h.run(T0 + timedelta(hours=4))
        self.assertEqual((code, up.urls), (1, []))

    def test_failed_hold_write_cannot_allow_later_requests(self):
        class FailsAfterBegin(S.DirStore):
            saves = 0
            def save(self, state):
                self.saves += 1
                if self.saves > 1:
                    raise S.StateError("connection lost after attempt recorded")
                super().save(state)
        self.h.store = FailsAfterBegin(self.h.store.path)
        code, msg, _ = self.h.run(T0, Upstream(self.h.store, fail_on="GROUP=visual"))
        self.assertEqual(code, 1)
        self.h.store = S.DirStore(self.h.store.path)
        code, msg, up = self.h.run(T0 + timedelta(hours=4))
        self.assertEqual((code, up.urls), (1, []), msg)

    def test_fail_closed_without_state(self):
        Path(self.h.store.path).write_text("{not json")
        code, msg, up = self.h.run(T0)
        self.assertEqual((code, up.urls), (1, []))
        self.assertIn("state unreadable", msg)
        Path(self.h.store.path).write_text(json.dumps({"schema": 1, "hold": "yes", "lastAttempt": None,
                                                       "lastSuccess": None, "satcat": None, "log": []}))
        code, msg, up = self.h.run(T0)
        self.assertEqual((code, up.urls), (1, []))

    def test_unsaved_attempt_or_unreadable_pages_means_no_requests(self):
        class ReadOnly(S.DirStore):
            def save(self, state):
                raise S.StateError("push rejected")
        self.h.store = ReadOnly(str(Path(self.tmp) / "ro.json"))
        code, msg, up = self.h.run(T0, Upstream(self.h.store))
        self.assertEqual((code, up.urls), (1, []))
        self.assertIn("could not record the attempt", msg)
        self.h.store = S.DirStore(str(Path(self.tmp) / "ok.json"))

        def broken():
            raise Run.PublishError("fetch pages: auth")
        code, msg, up = self.h.run(T0, loader=broken)
        self.assertEqual((code, up.urls), (1, []))
        self.assertIsNone(self.h.store.load()["lastAttempt"], "no attempt recorded when nothing was requested")


class GitState(unittest.TestCase):
    def test_orphan_branch_roundtrip_lease_and_unreachable_remote(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, remote = root / "repo", root / "remote.git"
            repo.mkdir()
            git = lambda *a, cwd=repo: subprocess.check_output(["git", *a], cwd=cwd, text=True).strip()
            git("init", "--bare", "-b", "main", str(remote))
            git("init", "-b", "main")
            git("remote", "add", "origin", str(remote))
            store = S.GitStore(cwd=str(repo))
            self.assertEqual(store.load(), S.initial(), "absent branch: first run")
            store.save(S.begin(S.initial(), T0))
            again = S.GitStore(cwd=str(repo))
            self.assertEqual(again.load()["lastAttempt"], "2026-09-30T00:37:00Z")
            self.assertEqual(git("--git-dir=" + str(remote), "rev-list", "--count", "state"), "1")
            self.assertEqual(git("--git-dir=" + str(remote), "ls-tree", "--name-only", "state"), "state.json")
            # Another writer moved the branch: the stale lease is refused.
            again.save(S.fail(again.load(), T0, "HTTP 403"))
            with self.assertRaises(S.StateError):
                store.save(S.initial())
            self.assertIsNotNone(S.GitStore(cwd=str(repo)).load()["hold"])
            git("remote", "set-url", "origin", str(root / "missing.git"))
            with self.assertRaises(S.StateError):
                S.GitStore(cwd=str(repo)).load()

    def test_published_loader(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, remote = root / "repo", root / "remote.git"
            repo.mkdir()
            git = lambda *a: subprocess.check_output(["git", *a], cwd=repo, text=True).strip()
            git("init", "--bare", "-b", "main", str(remote))
            git("init", "-b", "main")
            git("remote", "add", "origin", str(remote))
            self.assertIsNone(Run.git_published(cwd=str(repo)), "no pages branch yet")
            (repo / "v1").mkdir()
            (repo / "v1" / "index.json").write_text("{}")
            git("add", "v1")
            git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "pages")
            git("push", "-q", "origin", "HEAD:refs/heads/pages")
            got = Run.git_published(cwd=str(repo))
            self.addCleanup(shutil.rmtree, got, True)
            self.assertEqual((Path(got) / "v1" / "index.json").read_text(), "{}")
            git("remote", "set-url", "origin", str(root / "missing.git"))
            with self.assertRaises(Run.PublishError):
                Run.git_published(cwd=str(repo))


if __name__ == "__main__":
    unittest.main()
