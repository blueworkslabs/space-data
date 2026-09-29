import contextlib
import http.server
import json
import os
import shutil
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from unittest import mock

from space_data import build as B
from space_data import contract as C
from space_data import fetch as F
from space_data import publish as P
from space_data import rows as R
from space_data.validate import validate

HERE = os.path.dirname(__file__)
FIX = os.path.join(HERE, "fixtures")
SMALL = [dict(g, min_rows=1) for g in C.GROUPS]


def at(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


@contextlib.contextmanager
def small_contract(max_bytes=4000):
    with mock.patch.object(C, "GROUPS", SMALL), mock.patch.object(C, "MAX_FILE_BYTES", max_bytes):
        yield


def build_into(public, built="2026-09-29T21:00:00Z", fetch=None):
    data = B.collect(fetch or F.saved(FIX))
    return B.write(public, data, at(built))


class Rows(unittest.TestCase):
    def test_rows_match_space_watch_parsers(self):
        # expected-module-rows.json: Space Watch 0.4.1's own parseElements /
        # parseSatcat on these fixtures (node, space-orbit.js, space-catalog.js).
        with open(os.path.join(FIX, "expected-module-rows.json"), encoding="utf-8") as fh:
            expected = json.load(fh)
        for name, want in expected.items():
            with open(os.path.join(FIX, name), encoding="utf-8") as fh:
                records = json.load(fh)
            make = R.satcat_row if ".satcat." in name else R.element_row
            self.assertEqual(R.rows(records, make), want, name)

    def test_edge_cases(self):
        self.assertEqual(R.epoch_ms("2026-09-29T01:02:03.123987"), R.epoch_ms("2026-09-29T01:02:03.123Z"))
        self.assertEqual(R.epoch_ms("1970-01-01T00:00:01"), 1000)
        for bad in ("2026-09-29", "2026-09-29T01:02:03+01:00", "2026-13-01T00:00:00", 5, None):
            self.assertIsNone(R.epoch_ms(bad), bad)
        self.assertIsNone(R.num(True, 0, 1))
        self.assertIsNone(R.num(float("nan"), 0, 1))
        self.assertEqual(R.text(" AéB\n "), "AB")
        self.assertIsNone(R.catalog_id(1_000_000_000))
        self.assertEqual(R.catalog_id(123456), 123456, "six-digit catalogue numbers are valid")
        with self.assertRaises(ValueError):
            R.rows({"not": "a list"}, R.element_row)


class Build(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_build_shards_and_validates(self):
        public = os.path.join(self.tmp, "public")
        with small_contract():
            index = build_into(public)
            self.assertEqual(validate(public), [])
        self.assertEqual(index["dataset"], "20260929T2100Z")
        self.assertEqual(list(index["groups"]), [g["group"] for g in C.GROUPS])
        self.assertNotIn("satcat", index["groups"]["starlink"])
        files = index["groups"]["starlink"]["elements"]
        self.assertGreater(len(files), 1, "sharded under the small limit")
        joined = []
        for f in files:
            self.assertLessEqual(f["bytes"], 4000)
            with open(os.path.join(public, "v1", index["path"], f["file"]), encoding="utf-8") as fh:
                joined += json.load(fh)["rows"]
        with open(os.path.join(FIX, "starlink.gp.json"), encoding="utf-8") as fh:
            self.assertEqual(joined, R.rows(json.load(fh), R.element_row))
        with open(os.path.join(public, "_headers"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), C.HEADERS)

    def test_real_limit_is_below_host_cap(self):
        self.assertLess(C.MAX_FILE_BYTES, C.HOST_LIMIT_BYTES)

    def test_too_few_rows_stops_before_writing(self):
        with self.assertRaisesRegex(ValueError, "visual: 26 valid element sets, expected at least 100"):
            B.collect(F.saved(FIX))

    def test_validate_catches_tampering(self):
        public = os.path.join(self.tmp, "public")
        with small_contract():
            index = build_into(public)
            path = os.path.join(public, "v1", index["path"], "geo/elements-1.json")
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
            doc["rows"][0][5] = 1.5  # eccentricity out of range
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(doc, fh)
            errs = validate(public)
            self.assertTrue(any("size or hash" in e for e in errs), errs)
            self.assertTrue(any("invalid row" in e for e in errs), errs)
            os.mkdir(os.path.join(public, "v1", "stray"))
            self.assertTrue(any("unexpected entries" in e for e in validate(public)))

    def test_validate_rejects_stale_source(self):
        public = os.path.join(self.tmp, "public")
        with small_contract():
            build_into(public, built="2026-10-09T00:00:00Z")
            errs = validate(public)
        self.assertTrue(any("newest element set" in e for e in errs), errs)


class Publish(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def served(self, public):
        """What `pages` serves after publishing public/."""
        pages = os.path.join(self.tmp, "pages")
        shutil.rmtree(pages, ignore_errors=True)
        shutil.copytree(public, pages)
        return pages

    def test_noop_previous_and_rotation(self):
        empty = os.path.join(self.tmp, "empty")
        os.mkdir(empty)
        with small_contract():
            first = os.path.join(self.tmp, "a")
            build_into(first, "2026-09-29T21:00:00Z")
            self.assertEqual(P.plan(first, empty), "publish 20260929T2100Z")
            pages = self.served(first)
            same = os.path.join(self.tmp, "b")
            build_into(same, "2026-09-30T01:00:00Z")
            self.assertEqual(P.plan(same, pages), "noop", "identical data publishes nothing")

            def fewer(url):
                data = F.saved(FIX)(url)
                return data[1:] if "GROUP=geo" in url and "/NORAD/" in url else data

            second = os.path.join(self.tmp, "c")
            build_into(second, "2026-09-30T01:00:00Z", fewer)
            self.assertEqual(P.plan(second, pages), "publish 20260930T0100Z")
            with open(os.path.join(second, "v1", "index.json"), encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["previous"], "20260929T2100Z")
            self.assertEqual(sorted(os.listdir(os.path.join(second, "v1"))), ["20260929T2100Z", "20260930T0100Z", "index.json"])
            pages = self.served(second)
            third = os.path.join(self.tmp, "d")
            build_into(third, "2026-09-30T05:00:00Z")
            self.assertEqual(P.plan(third, pages), "publish 20260930T0500Z")
            self.assertEqual(sorted(os.listdir(os.path.join(third, "v1"))), ["20260930T0100Z", "20260930T0500Z", "index.json"])
            older = os.path.join(self.tmp, "e")
            build_into(older, "2026-09-29T20:00:00Z", fewer)
            with self.assertRaisesRegex(ValueError, "not older"):
                P.plan(older, self.served(third))


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/json"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"[1]")
        elif self.path.startswith("/html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<p>limit</p>")
        elif self.path.startswith("/moved"):
            self.send_response(301)
            self.send_header("Location", "/json")
            self.end_headers()
        else:
            self.send_response(403)
            self.end_headers()

    def log_message(self, *a):
        pass


class Fetch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_only_200_json_is_accepted(self):
        fetch = F.live(pause_s=0)
        self.assertEqual(fetch(self.base + "/json"), [1])
        for path, why in (("/html", "not JSON"), ("/moved", "redirect"), ("/limit", "HTTP 403")):
            with self.assertRaisesRegex(F.Stop, why):
                fetch(self.base + path)

    def test_raw_names(self):
        self.assertEqual(F.raw_name(C.GP_URL.format(group="last-30-days")), "last-30-days.gp.json")
        self.assertEqual(F.raw_name(C.SATCAT_URL.format(group="geo")), "geo.satcat.json")


if __name__ == "__main__":
    unittest.main()
