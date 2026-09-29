"""Exercise the actual publication script against a disposable local Git remote."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from space_data import build as B, contract as C, fetch as F
from tests.test_pipeline import FIX, at, small_contract


class GitPublication(unittest.TestCase):
    def test_orphan_noop_retention_and_failed_remote(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, remote = root / "repo", root / "remote.git"
            repo.mkdir()
            source = Path(__file__).resolve().parents[1]
            for name in ("space_data", "scripts"):
                shutil.copytree(source / name, repo / name, ignore=shutil.ignore_patterns("__pycache__"))

            def git(*args):
                return subprocess.check_output(["git", *args], cwd=repo, stderr=subprocess.PIPE, text=True).strip()

            git("init", "--bare", str(remote))
            git("init", "-b", "main")
            git("remote", "add", "origin", str(remote))
            # Not part of the output; must never leak into the pages tree.
            (repo / "private-marker").write_text("not public")
            with small_contract():
                data = B.collect(F.saved(FIX))
            # Expand saved rows to production thresholds without any network.
            for group in C.GROUPS:
                rows = data[group["group"]]["elements"]
                data[group["group"]]["elements"] = [
                    [i + 1, *rows[i % len(rows)][1:]] for i in range(max(len(rows), group["min_rows"]))
                ]

            def publish(hour, change=False):
                public = repo / "public"
                if public.exists():
                    shutil.rmtree(public)
                if change:
                    data["visual"]["elements"][0][1] = f"Changed at {hour}"
                B.write(str(public), data, at(f"2026-09-30T{hour}:00:00Z"))
                return subprocess.run(["bash", "scripts/publish.sh", "public"], cwd=repo,
                                      capture_output=True, text=True)

            first = publish("01")
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            sha = git("--git-dir=" + str(remote), "rev-parse", "pages")
            self.assertEqual(git("--git-dir=" + str(remote), "rev-list", "--count", "pages"), "1")
            paths = git("--git-dir=" + str(remote), "ls-tree", "-r", "--name-only", "pages").splitlines()
            self.assertNotIn("private-marker", paths)
            self.assertIn("v1/index.json", paths)
            same = publish("05")
            self.assertEqual(same.returncode, 0, same.stdout + same.stderr)
            self.assertEqual(git("--git-dir=" + str(remote), "rev-parse", "pages"), sha)
            for hour, previous in (("09", "20260930T0100Z"), ("13", "20260930T0900Z")):
                result = publish(hour, change=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                index = json.loads(git("--git-dir=" + str(remote), "show", "pages:v1/index.json"))
                self.assertEqual(index["previous"], previous)
                entries = git("--git-dir=" + str(remote), "ls-tree", "--name-only", "pages:v1").splitlines()
                self.assertEqual(sorted(entries), sorted([index["dataset"], previous, "index.json"]))
                self.assertEqual(git("--git-dir=" + str(remote), "rev-list", "--count", "pages"), "1")
            # An unreachable remote must not be mistaken for a missing branch.
            git("remote", "set-url", "origin", str(root / "missing.git"))
            failed = publish("17", change=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertNotIn("pages commit", failed.stdout)
