#!/usr/bin/env python3
"""Tests tools/publish_gcs.py's resumable upload against an in-process fake of the GCS JSON API,
including dropped connections, 503s, partial chunks and the create-only precondition. No network."""
import base64, hashlib, json, os, sys, tempfile, unittest, urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import publish_gcs as pg  # noqa: E402


class FakeGCS:
    def __init__(self, fail_plan=()):
        self.objects, self.sessions, self.fail_plan, self.calls = {}, {}, list(fail_plan), 0

    def __call__(self, method, url, body=None, headers=None, timeout=120):
        self.calls += 1
        u = urllib.parse.urlparse(url)
        if method == "POST" and u.path.startswith("/upload/storage/v1/b/"):
            q = dict(urllib.parse.parse_qsl(u.query))
            if q.get("ifGenerationMatch") == "0" and q["name"] in self.objects:
                return 412, {}, b"precondition"
            sid = f"https://storage.googleapis.com/session/{len(self.sessions)}"
            self.sessions[sid] = {"meta": json.loads(body), "data": bytearray(), "size": int(headers["X-Upload-Content-Length"])}
            return 200, {"Location": sid}, b""
        if method == "PUT" and url in self.sessions:
            if self.fail_plan:
                f = self.fail_plan.pop(0)
                if f == "drop":
                    raise OSError("connection reset")
                if f == "503":
                    return 503, {}, b"busy"
                if f == "half" and body:  # store only half the chunk, report 308
                    s = self.sessions[url]
                    s["data"] += body[: len(body) // 2]
                    return 308, {"Range": f"bytes=0-{len(s['data']) - 1}"}, b""
            s = self.sessions[url]
            rng = headers["Content-Range"]
            if body:
                start = int(rng.split(" ")[1].split("-")[0])
                assert start == len(s["data"]), (start, len(s["data"]))
                s["data"] += body
            if len(s["data"]) < s["size"]:
                return 308, ({"Range": f"bytes=0-{len(s['data']) - 1}"} if s["data"] else {}), b""
            md5 = base64.b64encode(hashlib.md5(bytes(s["data"])).digest()).decode()
            if md5 != s["meta"]["md5Hash"]:
                return 400, {}, b"md5 mismatch"
            self.objects[s["meta"]["name"]] = bytes(s["data"])
            return 200, {}, json.dumps({"name": s["meta"]["name"], "md5Hash": md5}).encode()
        raise AssertionError(f"unexpected {method} {url}")


class Upload(unittest.TestCase):
    def setUp(self):
        pg.CHUNK = 256 * 1024
        pg.time.sleep = lambda s: None
        self.tok = pg.Token(lambda: "t")
        fd, self.path = tempfile.mkstemp()
        self.data = os.urandom(pg.CHUNK * 3 + 12345)
        with os.fdopen(fd, "wb") as fh:
            fh.write(self.data)

    def tearDown(self):
        os.remove(self.path)

    def go(self, fake, **kw):
        pg.http = fake
        return pg.upload(self.tok, "bkt", "kits/x.zip", path=self.path, **kw)

    def test_clean_upload(self):
        fake = FakeGCS()
        self.go(fake)
        self.assertEqual(fake.objects["kits/x.zip"], self.data)

    def test_survives_drops_503_and_partial_chunks(self):
        fake = FakeGCS(["drop", "half", "503", "drop", "half"])
        self.go(fake)
        self.assertEqual(fake.objects["kits/x.zip"], self.data)

    def test_never_overwrites(self):
        fake = FakeGCS()
        self.go(fake)
        with self.assertRaises(SystemExit):
            self.go(fake)

    def test_small_data_upload(self):
        fake = FakeGCS()
        pg.http = fake
        pg.upload(self.tok, "bkt", "kits/latest.json", data=b'{"a":1}', create_only=False)
        pg.upload(self.tok, "bkt", "kits/latest.json", data=b'{"a":2}', create_only=False)
        self.assertEqual(fake.objects["kits/latest.json"], b'{"a":2}')


if __name__ == "__main__":
    unittest.main(verbosity=2)
