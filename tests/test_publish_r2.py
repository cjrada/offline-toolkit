#!/usr/bin/env python3
"""Tests tools/publish_r2.py against an in-process fake of the R2 S3 API. No network, no credentials.

Covers SigV4 (AWS's published example), layout, refusals, create-only versions, multipart retries and abort,
verification, manifests, retention (3/2/1), and that nothing is written or deleted on a dry run or after a
failed verification. Usage: python3 tests/test_publish_r2.py
"""
import base64, hashlib, io, json, os, re, sys, tempfile, threading, unittest, urllib.parse, zipfile
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import publish_r2 as pr  # noqa: E402

ACCOUNT = "0123456789abcdef0123456789abcdef"
ENV = {"R2_ACCESS_KEY_ID": "AKIDTESTKEY", "R2_SECRET_ACCESS_KEY": "s3cr3t-value-never-printed",
       "R2_ACCOUNT_ID": ACCOUNT, "R2_BUCKET": "kits-bucket", "R2_PUBLIC_URL": "https://pub-abc.r2.dev"}


class FakeS3:
    """Enough of the S3 API for the publisher. Checks the signature's payload hash and Content-MD5, honours
    If-None-Match: *, and records every write and delete."""

    def __init__(self, bucket="kits-bucket"):
        self.bucket, self.objects, self.uploads = bucket, {}, {}
        self.lock = threading.Lock()
        self.writes, self.deletes, self.aborts, self.fail = [], [], [], []
        self.headers_seen = []
        self.corrupt_meta = False
        self.manifest_tamper = None  # key -> bytes served instead of what was written

    def _err(self, st, code):
        return st, {}, f"<Error><Code>{code}</Code><Message>m</Message><AWSAccessKeyId>AKIDTESTKEY</AWSAccessKeyId></Error>".encode()

    def __call__(self, method, url, body=None, headers=None, timeout=120):
        u = urllib.parse.urlparse(url)
        assert u.scheme == "https" and u.hostname == f"{ACCOUNT}.r2.cloudflarestorage.com", url
        assert headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIDTESTKEY/")
        assert "/auto/s3/aws4_request" in headers["Authorization"]
        self.headers_seen.append(headers)
        qs = dict(urllib.parse.parse_qsl(u.query, keep_blank_values=True))
        path = urllib.parse.unquote(u.path)
        assert path.startswith(f"/{self.bucket}"), path
        key = path[len(self.bucket) + 2:]
        data = body or b""
        assert headers["x-amz-content-sha256"] == hashlib.sha256(data).hexdigest(), "payload hash not signed"
        if "content-md5" in headers:
            assert headers["content-md5"] == base64.b64encode(hashlib.md5(data).digest()).decode()
        with self.lock:
            for i, (m, pat, resp) in enumerate(self.fail):
                if m == method and re.search(pat, url):
                    self.fail.pop(i)
                    if resp == "drop":
                        raise OSError("connection reset")
                    return self._err(resp, "InternalError" if resp >= 500 else "AccessDenied")
        if method == "GET" and not key:
            return self._list(qs)
        if method == "GET":
            if key not in self.objects:
                return self._err(404, "NoSuchKey")
            if self.manifest_tamper and key in self.manifest_tamper:
                return 200, {}, self.manifest_tamper[key]
            return 200, {}, self.objects[key]["data"]
        if method == "HEAD":
            if key not in self.objects:
                return 404, {}, b""
            o = self.objects[key]
            meta = dict(o["meta"])
            if self.corrupt_meta:
                meta["x-amz-meta-sha256"] = "0" * 64
            return 200, {"Content-Length": str(len(o["data"])), "ETag": '"abc-3"', **meta}, b""
        if method in ("PUT", "POST") and body is not None:
            assert headers.get("content-type") not in (None, "application/x-www-form-urlencoded"), (method, url)
        if method == "PUT" and "partNumber" in qs:
            up = self.uploads[qs["uploadId"]]
            with self.lock:
                up["parts"][int(qs["partNumber"])] = data
            return 200, {"ETag": f'"{hashlib.md5(data).hexdigest()}"'}, b""
        if method == "PUT":
            with self.lock:
                if headers.get("if-none-match") == "*" and key in self.objects:
                    return self._err(412, "PreconditionFailed")
                self.objects[key] = {"data": data, "meta": {k: v for k, v in headers.items() if k.startswith("x-amz-meta-")},
                                     "ctype": headers.get("content-type"), "cc": headers.get("cache-control")}
                self.writes.append(key)
            return 200, {}, b""
        if method == "POST" and "uploads" in qs:
            uid = f"up{len(self.uploads)}"
            self.uploads[uid] = {"key": key, "parts": {}, "meta": {k: v for k, v in headers.items() if k.startswith("x-amz-meta-")}}
            return 200, {}, f"<InitiateMultipartUploadResult><UploadId>{uid}</UploadId></InitiateMultipartUploadResult>".encode()
        if method == "POST" and "uploadId" in qs:
            up = self.uploads.pop(qs["uploadId"])
            nums = [int(n) for n in re.findall(rb"<PartNumber>(\d+)</PartNumber>", data)]
            assert nums == sorted(up["parts"]), (nums, sorted(up["parts"]))
            blob = b"".join(up["parts"][n] for n in nums)
            with self.lock:
                self.objects[key] = {"data": blob, "meta": up["meta"], "ctype": "application/zip", "cc": None}
                self.writes.append(key)
            return 200, {}, b"<CompleteMultipartUploadResult/>"
        if method == "DELETE" and "uploadId" in qs:
            self.aborts.append(qs["uploadId"])
            self.uploads.pop(qs["uploadId"], None)
            return 204, {}, b""
        if method == "DELETE":
            with self.lock:
                self.objects.pop(key, None)
                self.deletes.append(key)
            return 204, {}, b""
        raise AssertionError(f"unexpected {method} {url}")

    def _list(self, qs):
        assert qs.get("list-type") == "2"
        prefix, delim = qs.get("prefix", ""), qs.get("delimiter")
        keys = sorted(k for k in self.objects if k.startswith(prefix))
        contents, prefixes = [], set()
        for k in keys:
            rest = k[len(prefix):]
            if delim and delim in rest:
                prefixes.add(prefix + rest.split(delim)[0] + delim)
            else:
                contents.append(k)
        # paginate in pages of 2 to exercise continuation tokens
        start = int(qs.get("continuation-token", "0"))
        page = contents[start:start + 2]
        more = start + 2 < len(contents)
        xml = '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        xml += "".join(f"<Contents><Key>{k}</Key><Size>{len(self.objects[k]['data'])}</Size>"
                       f"<LastModified>2026-11-26T00:00:00.000Z</LastModified></Contents>" for k in page)
        if start == 0:
            xml += "".join(f"<CommonPrefixes><Prefix>{p}</Prefix></CommonPrefixes>" for p in sorted(prefixes))
        xml += f"<IsTruncated>{'true' if more else 'false'}</IsTruncated>"
        if more:
            xml += f"<NextContinuationToken>{start + 2}</NextContinuationToken>"
        return 200, {}, (xml + "</ListBucketResult>").encode()


def kit(tmp, tier="small", version="v1", info=None):
    base = {"name": f"offline-toolkit-{tier}", "tier": tier, "version": version, "git_dirty": False, "git_commit": "a" * 40,
            "redistributable": True, "items": [{"id": "m", "dest": "models/m.gguf", "pinned": True},
                                               {"id": "map", "dest": "maps/au.pmtiles", "pinned": False}]}
    base.update(info or {})
    fd, path = tempfile.mkstemp(suffix=".zip", dir=tmp)
    os.close(fd)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"{base['name']}/KIT-INFO.json", json.dumps(base))
        zf.writestr(f"{base['name']}/blob.bin", os.urandom(300 * 1024))  # stored: several 64 KiB test parts
    return path


class Base(unittest.TestCase):
    def setUp(self):
        # Tests run the same locally and on a GitHub runner; GitHub mode is tested on its own below.
        self._gha = os.environ.pop("GITHUB_ACTIONS", None)
        self.addCleanup(lambda: self._gha is not None and os.environ.__setitem__("GITHUB_ACTIONS", self._gha))
        self.tmp = tempfile.mkdtemp()
        self.fake = FakeS3()
        pr.time.sleep = lambda s: None
        self.part = pr.PART
        pr.PART = 64 * 1024  # small parts so the test kits are multipart

    def tearDown(self):
        pr.PART = self.part

    def client(self, env, bucket):
        r2 = pr.client_from_env(env, bucket)
        r2.transport = self.fake
        return r2

    def run_pub(self, args, env=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = pr.main(args, env=dict(ENV if env is None else env), make_client=self.client)
        self.out = out.getvalue() + err.getvalue()
        # Only ::add-mask:: (GitHub hides the value it registers) may carry a secret.
        logged = "\n".join(l for l in self.out.splitlines() if not l.startswith("::add-mask::"))
        self.assertNotIn(ENV["R2_SECRET_ACCESS_KEY"], logged, "secret printed")
        self.assertNotIn("Signature=", self.out)
        return rc

    def publish(self, tier, version, **kw):
        rc = self.run_pub(["--zip", kit(self.tmp, tier, version), *kw.pop("args", [])], **kw)
        return rc

    def versions(self, tier):
        return [e["version"] for e in json.loads(self.fake.objects[f"{tier}/index.json"]["data"])["versions"]]

    def prefixes(self, tier):
        return sorted({k.split("/")[1] for k in self.fake.objects if k.startswith(tier + "/") and k.count("/") == 2})


class Signing(unittest.TestCase):
    def test_aws_example(self):
        # "Example: GET Object" from the AWS SigV4 for S3 documentation.
        h = {"host": "examplebucket.s3.amazonaws.com", "range": "bytes=0-9", "x-amz-date": "20130524T000000Z",
             "x-amz-content-sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"}
        got = pr.sigv4("GET", "/test.txt", "", h, "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "us-east-1")
        self.assertTrue(got.endswith("Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"), got)

    def test_error_body_is_sanitised(self):
        msg = pr.s3_error(403, b"<Error><Code>SignatureDoesNotMatch</Code><Message>no</Message>"
                               b"<AWSAccessKeyId>AKIDTESTKEY</AWSAccessKeyId><StringToSign>x</StringToSign></Error>")
        self.assertEqual(msg, "HTTP 403 SignatureDoesNotMatch: no")


class Config(unittest.TestCase):
    def test_missing_vars_message(self):
        p = pr.config_problems({**ENV, "R2_BUCKET": "", "R2_PUBLIC_URL": ""}, need_public_url=True)
        text = "\n".join(p)
        for needle in ("R2_BUCKET is not set", "R2_PUBLIC_URL is not set", "Settings -> Secrets and variables -> Actions -> Variables",
                       "R2_BUCKET = the R2 bucket name", "no trailing slash"):
            self.assertIn(needle, text)

    def test_wellformed(self):
        self.assertEqual(pr.config_problems(ENV, need_public_url=True), [])
        self.assertEqual(pr.public_base("https://pub-abc.r2.dev/"), "https://pub-abc.r2.dev")
        self.assertEqual(pr.public_base("https://dl.example.org/kits"), "https://dl.example.org/kits")
        for bad in ("http://pub-abc.r2.dev", "https://x.r2.dev/?a=1", "pub-abc.r2.dev", "https://a b"):
            self.assertEqual(pr.public_base(bad), "", bad)
        self.assertTrue(pr.config_problems({**ENV, "R2_BUCKET": "Bad_Bucket"}))
        self.assertTrue(pr.config_problems({**ENV, "R2_ACCOUNT_ID": "not-an-id"}))
        p = pr.config_problems({**ENV, "R2_SECRET_ACCESS_KEY": " "})
        self.assertTrue(any("R2_SECRET_ACCESS_KEY" in x for x in p), p)

    def test_part_size(self):
        mib = 1 << 20
        self.assertEqual(pr.part_size_for(173 * 10**9, 64 * mib), 64 * mib)
        big = 2 * 10**12
        ps = pr.part_size_for(big, 64 * mib)
        self.assertTrue(ps % mib == 0 and -(-big // ps) <= pr.MAX_PARTS < 10000, ps)

    def test_keep(self):
        self.assertEqual(pr.parse_keep(""), {"small": 3, "medium": 2, "large": 1})
        self.assertEqual(pr.parse_keep("small=5, large=2"), {"small": 5, "medium": 2, "large": 2})
        for bad in ("large=0", "huge=1", "small=x"):
            with self.assertRaises(pr.Fail):
                pr.parse_keep(bad)


class Publish(Base):
    def test_github_masks_and_never_logs_secrets(self):
        os.environ["GITHUB_ACTIONS"] = "true"
        try:
            self.assertEqual(self.publish("small", "v1"), 0, self.out)
            self.fake.fail = [("PUT", r"partNumber=1&", 403)]
            self.assertEqual(self.publish("small", "v2"), 1)  # error path: S3 error body echoes the key id
        finally:
            del os.environ["GITHUB_ACTIONS"]
        lines = self.out.splitlines()
        self.assertIn(f"::add-mask::{ENV['R2_SECRET_ACCESS_KEY']}", lines)
        for secret in (ENV["R2_SECRET_ACCESS_KEY"], ENV["R2_ACCESS_KEY_ID"]):
            self.assertFalse([l for l in lines if secret in l and not l.startswith("::add-mask::")], secret)
        self.assertTrue(any(l.startswith("::error::") for l in lines))

    def test_layout_and_manifests(self):
        self.assertEqual(self.publish("small", "v1"), 0, self.out)
        objs = self.fake.objects
        self.assertEqual(sorted(objs), ["index.json", "latest.json", "small/index.json", "small/latest.json",
                                        "small/v1/KIT-INFO.json", "small/v1/offline-toolkit-small-v1.zip",
                                        "small/v1/offline-toolkit-small-v1.zip.sha256"])
        z = objs["small/v1/offline-toolkit-small-v1.zip"]["data"]
        sha = hashlib.sha256(z).hexdigest()
        self.assertEqual(objs["small/v1/offline-toolkit-small-v1.zip.sha256"]["data"], f"{sha}  offline-toolkit-small-v1.zip\n".encode())
        latest = json.loads(objs["small/latest.json"]["data"])
        self.assertEqual((latest["version"], latest["sha256"], latest["size"], latest["zip"]),
                         ("v1", sha, len(z), "small/v1/offline-toolkit-small-v1.zip"))
        top = json.loads(objs["latest.json"]["data"])
        self.assertEqual(top["tiers"]["small"]["version"], "v1")
        self.assertIsNone(top["tiers"]["large"])
        self.assertEqual([e["version"] for e in json.loads(objs["index.json"]["data"])["tiers"]["small"]], ["v1"])
        self.assertEqual(objs["small/latest.json"]["cc"], "no-cache, max-age=0")
        self.assertEqual(objs["small/v1/offline-toolkit-small-v1.zip"]["meta"]["x-amz-meta-sha256"], sha)
        self.assertGreater(len(z), 4 * pr.PART)  # it really was a multipart upload
        self.assertIn("https://pub-abc.r2.dev/small/v1/offline-toolkit-small-v1.zip", self.out)
        # version objects are create-only
        version_puts = [h for h in self.fake.headers_seen if h.get("content-type") in ("text/plain; charset=utf-8",)]
        self.assertTrue(version_puts and all(h.get("if-none-match") == "*" for h in version_puts))

    def test_refusals(self):
        for info, msg in (({"redistributable": False}, "NOT redistributable"), ({"git_dirty": True}, "dirty"),
                          ({"items": [{"id": "x", "dest": "a.zim", "pinned": False}]}, "unpinned"),
                          ({"version": "../x"}, "unsafe version"), ({"version": ".hidden"}, "unsafe version"),
                          ({"tier": "huge", "name": "offline-toolkit-huge"}, "unexpected kit name")):
            with self.subTest(msg=msg):
                path = kit(self.tmp, "small", "v9", info)
                self.assertEqual(self.run_pub(["--zip", path]), 1)
                self.assertIn(msg, self.out)
        self.assertEqual(self.fake.writes, [])

    def test_existing_version_is_refused(self):
        self.assertEqual(self.publish("small", "v1"), 0)
        writes = list(self.fake.writes)
        self.assertEqual(self.publish("small", "v1"), 1)
        self.assertIn("already exists", self.out)
        self.assertEqual(self.fake.writes, writes)

    def test_create_only_sidecar(self):
        # A sidecar that appeared after the prefix check (e.g. a concurrent run) is not overwritten.
        orig = self.fake.__class__.__call__

        def racing(fake, method, url, body=None, headers=None, timeout=120):
            if method == "PUT" and url.endswith(".zip.sha256"):
                fake.objects.setdefault("small/v1/offline-toolkit-small-v1.zip.sha256", {"data": b"other", "meta": {}, "ctype": "", "cc": None})
            return orig(fake, method, url, body, headers, timeout)
        self.fake.__class__ = type("Racing", (FakeS3,), {"__call__": racing})
        self.assertEqual(self.publish("small", "v1"), 1)
        self.assertIn("already exists", self.out)
        self.assertNotIn("small/latest.json", self.fake.objects)

    def test_part_retries(self):
        self.fake.fail = [("PUT", r"partNumber=2&", 503), ("PUT", r"partNumber=3&", "drop"), ("PUT", r"partNumber=2&", 500)]
        self.assertEqual(self.publish("small", "v1"), 0, self.out)
        self.assertEqual(self.fake.fail, [])
        self.assertEqual(self.fake.aborts, [])

    def test_failed_part_aborts_and_publishes_nothing(self):
        self.fake.fail = [("PUT", r"partNumber=2&", 503)] * 20
        self.assertEqual(self.publish("small", "v1"), 1)
        self.assertEqual(len(self.fake.aborts), 1)
        self.assertEqual(self.fake.objects, {})
        self.assertIn("aborted the multipart upload", self.out)

    def test_dry_run_writes_nothing(self):
        for v in ("v1", "v2", "v3"):
            self.assertEqual(self.publish("small", v), 0)
        before = {k: o["data"] for k, o in self.fake.objects.items()}
        writes = len(self.fake.writes)
        self.assertEqual(self.publish("small", "v4", args=["--dry-run"]), 0, self.out)
        self.assertIn("would delete small/v1/", self.out)
        self.assertEqual(len(self.fake.writes), writes)
        self.assertEqual(self.fake.deletes, [])
        self.assertEqual({k: o["data"] for k, o in self.fake.objects.items()}, before)

    def test_offline_dry_run_needs_no_credentials(self):
        env = {"PATH": "/bin"}
        self.assertEqual(self.publish("small", "v1", args=["--dry-run", "--offline"], env=env), 0, self.out)
        self.assertEqual(self.fake.headers_seen, [])
        self.assertEqual(self.publish("small", "v1", args=["--offline"], env=env), 1)

    def test_preflight(self):
        self.assertEqual(self.run_pub(["--tier", "large", "--version", "v1", "--dry-run"]), 0, self.out)
        self.assertEqual(self.fake.writes, [])
        self.assertEqual(self.publish("large", "v1"), 0)
        self.assertEqual(self.run_pub(["--tier", "large", "--version", "v1", "--dry-run"]), 1)
        self.assertIn("already exists", self.out)
        self.assertEqual(self.run_pub(["--tier", "large", "--version", "v2"]), 1)  # no writes without a zip
        self.assertEqual(self.run_pub(["--tier", "large", "--version", "v2", "--dry-run"],
                                      env={**ENV, "R2_SECRET_ACCESS_KEY": ""}), 1)
        self.assertIn("R2_SECRET_ACCESS_KEY", self.out)
        self.assertEqual(self.run_pub(["--tier", "large", "--version", "v2", "--dry-run"], env={**ENV, "R2_BUCKET": ""}), 1)
        self.assertIn("R2_BUCKET = the R2 bucket name", self.out)

    def test_check_config(self):
        self.assertEqual(self.run_pub(["--check-config"]), 0, self.out)
        self.assertEqual(self.run_pub(["--check-config"], env={**ENV, "R2_PUBLIC_URL": ""}), 1)
        self.assertIn("R2_PUBLIC_URL = the public base URL", self.out)
        self.assertEqual(self.run_pub(["--check-config", "--no-secrets"], env={"R2_BUCKET": "b-1", "R2_PUBLIC_URL": "https://x.r2.dev"}), 0)
        self.assertEqual(self.fake.headers_seen, [])


class Retention(Base):
    def test_keep_3_2_1(self):
        for tier, keep in (("small", 3), ("medium", 2), ("large", 1)):
            for i in range(1, 6):
                self.assertEqual(self.publish(tier, f"v{i}"), 0, self.out)
                want = [f"v{j}" for j in range(i, max(0, i - keep), -1)]
                self.assertEqual(self.versions(tier), want, tier)
                self.assertEqual(self.prefixes(tier), sorted(want), tier)
        top = json.loads(self.fake.objects["index.json"]["data"])["tiers"]
        self.assertEqual({t: [e["version"] for e in v] for t, v in top.items()},
                         {"small": ["v5", "v4", "v3"], "medium": ["v5", "v4"], "large": ["v5"]})

    def test_large_only_new_remains_and_delete_comes_last(self):
        self.assertEqual(self.publish("large", "old"), 0)
        self.fake.writes.clear()
        order = []
        orig = FakeS3.__call__

        def spy(fake, method, url, body=None, headers=None, timeout=120):
            if method in ("PUT", "DELETE") and "partNumber" not in url and "uploadId" not in url:
                order.append((method, urllib.parse.urlparse(url).path.split("/", 2)[2]))
            if method == "GET" and url.endswith("/large/index.json"):
                order.append(("GET", "large/index.json"))
            return orig(fake, method, url, body, headers, timeout)
        self.fake.__class__ = type("Spy", (FakeS3,), {"__call__": spy})
        self.assertEqual(self.publish("large", "new"), 0, self.out)
        self.assertEqual(self.prefixes("large"), ["new"])
        self.assertEqual(self.versions("large"), ["new"])
        first_delete = next(i for i, (m, _) in enumerate(order) if m == "DELETE")
        before = order[:first_delete]
        self.assertTrue(all(m == "DELETE" for m, _ in order[first_delete:]), order)
        for k in ("large/index.json", "large/latest.json", "index.json", "latest.json"):
            self.assertIn(("PUT", k), before)
        # the manifest was read back after it was written, before anything was deleted
        last_put = max(i for i, x in enumerate(before) if x == ("PUT", "large/index.json"))
        self.assertIn(("GET", "large/index.json"), before[last_put + 1:])
        self.assertTrue(all(k.startswith("large/old/") for _, k in order[first_delete:]))

    def test_no_delete_when_upload_verification_fails(self):
        self.assertEqual(self.publish("large", "old"), 0)
        latest = self.fake.objects["large/latest.json"]["data"]
        self.fake.corrupt_meta = True
        self.assertEqual(self.publish("large", "new"), 1)
        self.assertIn("sha256 metadata after upload does not match", self.out)
        self.assertEqual(self.fake.deletes, [])
        self.assertEqual(self.fake.objects["large/latest.json"]["data"], latest)
        self.assertIn("old", self.prefixes("large"))

    def test_no_delete_when_manifest_reread_fails(self):
        self.assertEqual(self.publish("large", "old"), 0)
        self.fake.manifest_tamper = {"latest.json": b'{"tiers": {}}'}
        self.assertEqual(self.publish("large", "new"), 1)
        self.assertIn("does not point at large new", self.out)
        self.assertEqual(self.fake.deletes, [])
        self.assertIn("old", self.prefixes("large"))

    def test_unindexed_and_foreign_prefixes_are_left_alone(self):
        self.assertEqual(self.publish("large", "a"), 0)
        self.fake.objects["large/stray/offline-toolkit-large-stray.zip"] = {"data": b"x", "meta": {}, "ctype": "", "cc": None}
        self.fake.objects["large/a/notes.txt"] = {"data": b"mine", "meta": {}, "ctype": "", "cc": None}
        self.assertEqual(self.publish("large", "b"), 0, self.out)
        self.assertIn("large/stray/ is in the bucket but not in large/index.json", self.out)
        self.assertIn("not deleting large/a/", self.out)
        self.assertEqual(self.prefixes("large"), ["a", "b", "stray"])
        self.assertEqual(self.versions("large"), ["b"])

    def test_keep_override(self):
        for v in ("v1", "v2", "v3"):
            self.assertEqual(self.publish("large", v, args=["--keep", "large=2"]), 0)
        self.assertEqual(self.versions("large"), ["v3", "v2"])
        self.assertEqual(self.publish("large", "v4", env={**ENV, "KIT_KEEP": "large=1"}), 0)
        self.assertEqual(self.versions("large"), ["v4"])

    def test_index_feeds_the_downloads_page(self):
        import build_site
        for v in ("v1", "v2"):
            self.assertEqual(self.publish("medium", v), 0)
        page = build_site.render(json.loads(self.fake.objects["index.json"]["data"]), ENV["R2_PUBLIC_URL"])
        self.assertIn('href="https://pub-abc.r2.dev/medium/v2/offline-toolkit-medium-v2.zip"', page)
        self.assertIn('href="https://pub-abc.r2.dev/medium/v1/offline-toolkit-medium-v1.zip.sha256"', page)
        self.assertEqual(page.count("Not published yet."), 2)

    def test_malformed_index_is_not_overwritten(self):
        self.fake.objects["small/index.json"] = {"data": b"not json", "meta": {}, "ctype": "", "cc": None}
        self.assertEqual(self.publish("small", "v1"), 1)
        self.assertIn("malformed", self.out)
        self.assertEqual(self.fake.writes, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
