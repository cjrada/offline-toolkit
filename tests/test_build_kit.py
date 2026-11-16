#!/usr/bin/env python3
"""Offline tests for tools/build_kit.py and the safety checks in tools/publish_gcs.py.
Uses file:// URLs (only allowed when KIT_ALLOW_FILE_URLS=1). Run: python3 tests/test_build_kit.py"""
import hashlib, io, json, os, subprocess, sys, tarfile, tempfile, unittest, zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(ROOT, "tools", "build_kit.py")
PUBLISH = os.path.join(ROOT, "tools", "publish_gcs.py")


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def run(*args, env=None):
    e = {**os.environ, "KIT_ALLOW_FILE_URLS": "1", "SOURCE_DATE_EPOCH": "1763251200", **(env or {})}
    return subprocess.run([sys.executable, *args], capture_output=True, text=True, env=e)


class BuildKit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.t = self.tmp.name
        src = os.path.join(t, "src")
        os.makedirs(src)
        with open(os.path.join(src, "model.gguf"), "wb") as fh:
            fh.write(b"GGUF" + b"\0" * 1000)
        with tarfile.open(os.path.join(src, "tools.tar.gz"), "w:gz") as tf:
            for name, data in (("pkg-1/kiwix-serve", b"#!bin"), ("pkg-1/README", b"x"), ("pkg-1/sub/fonts/a.pbf", b"f")):
                ti = tarfile.TarInfo(name); ti.size = len(data); tf.addfile(ti, io.BytesIO(data))
        with zipfile.ZipFile(os.path.join(src, "win.zip"), "w") as zf:
            zf.writestr("kiwix/kiwix-serve.exe", b"MZ")
            zf.writestr("kiwix/icu.dll", b"MZ")
            zf.writestr("kiwix/readme.txt", b"no")
        self.src = src

    def tearDown(self):
        self.tmp.cleanup()

    def manifest(self, items, name="m.json"):
        p = os.path.join(self.t, name)
        json.dump({"tools": {"go-pmtiles": {"module": "x", "version": "v0"}}, "items": items}, open(p, "w"), indent=1)
        return p

    def url(self, f):
        return "file://" + os.path.join(self.src, f)

    def items(self, pin=True):
        S = lambda f: sha(os.path.join(self.src, f)) if pin else None
        return [
            {"id": "model", "tier": "small", "kind": "file", "url": self.url("model.gguf"), "sha256": S("model.gguf"), "dest": "models/m.gguf"},
            {"id": "lin", "tier": "small", "kind": "tar", "url": self.url("tools.tar.gz"), "sha256": S("tools.tar.gz"),
             "include": ["*/kiwix-serve"], "flatten": True, "dest": "bin/linux/", "mode": "755"},
            {"id": "fonts", "tier": "small", "kind": "tar", "url": self.url("tools.tar.gz"), "sha256": S("tools.tar.gz"),
             "include": ["*/sub/fonts/*"], "strip": 2, "dest": "www/maps/"},
            {"id": "win", "tier": "small", "kind": "zip", "url": self.url("win.zip"), "sha256": S("win.zip"),
             "include": ["*.exe", "*.dll"], "flatten": True, "dest": "bin/windows/"},
            {"id": "big", "tier": "medium", "kind": "file", "url": self.url("model.gguf"), "sha256": S("model.gguf"), "dest": "models/big.gguf"},
            {"id": "nonfree", "tier": "small", "optional": True, "redistributable": False, "kind": "file",
             "url": self.url("model.gguf"), "sha256": S("model.gguf"), "dest": "library/nonfree.zim"},
        ]

    def build(self, m, out, *extra):
        return run(BUILD, "--manifest", m, "--out", out, "--cache", os.path.join(self.t, "cache"), "--no-index", *extra)

    def test_build_layout_and_reproducible_zip(self):
        m = self.manifest(self.items())
        out = os.path.join(self.t, "kit")
        z1, z2 = os.path.join(self.t, "a.zip"), os.path.join(self.t, "b.zip")
        r = self.build(m, out, "--zip", z1, "--frozen")
        self.assertEqual(r.returncode, 0, r.stderr)
        for p in ("models/m.gguf", "bin/linux/kiwix-serve", "www/maps/fonts/a.pbf", "bin/windows/kiwix-serve.exe",
                  "bin/windows/icu.dll", "start.sh", "start.bat", "tools/pick-model.sh", "KIT-INFO.json", "SHA256SUMS"):
            self.assertTrue(os.path.isfile(os.path.join(out, p)), p)
        self.assertFalse(os.path.exists(os.path.join(out, "models/big.gguf")), "medium item in small tier")
        self.assertFalse(os.path.exists(os.path.join(out, "bin/windows/readme.txt")))
        self.assertTrue(os.stat(os.path.join(out, "bin/linux/kiwix-serve")).st_mode & 0o100)
        info = json.load(open(os.path.join(out, "KIT-INFO.json")))
        self.assertTrue(info["redistributable"])
        sums = open(os.path.join(out, "SHA256SUMS")).read()
        self.assertIn(f"{sha(os.path.join(out, 'models/m.gguf'))}  models/m.gguf", sums)
        with zipfile.ZipFile(z1) as zf:
            zi = zf.getinfo("offline-toolkit-small/start.sh")
            self.assertEqual((zi.external_attr >> 16) & 0o777, 0o755)
        # same inputs -> byte-identical zip (KIT-INFO built_at differs, so rebuild with identical info)
        r = run(BUILD, "--manifest", m, "--out", out, "--cache", os.path.join(self.t, "cache"), "--no-index", "--zip", z2)
        self.assertEqual(r.returncode, 0, r.stderr)
        with zipfile.ZipFile(z1) as a, zipfile.ZipFile(z2) as b:
            self.assertEqual(a.namelist(), b.namelist())
            self.assertEqual([i.date_time for i in a.infolist()], [i.date_time for i in b.infolist()])

    def test_tier_and_replaces(self):
        items = self.items()
        items[4]["replaces"] = ["model"]
        out = os.path.join(self.t, "kit")
        r = self.build(self.manifest(items), out, "--tier", "medium")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(os.path.join(out, "models/big.gguf")))
        self.assertFalse(os.path.exists(os.path.join(out, "models/m.gguf")))

    def test_hash_mismatch_fails(self):
        items = self.items()
        items[0]["sha256"] = "0" * 64
        r = self.build(self.manifest(items), os.path.join(self.t, "kit"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("sha256 mismatch", r.stderr)

    def test_frozen_refuses_unpinned_and_lock_pins(self):
        m = self.manifest(self.items(pin=False))
        r = self.build(m, os.path.join(self.t, "kit"), "--frozen")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no sha256", r.stderr)
        # --lock rewrites the manifest with real hashes, after which --frozen passes
        r = run(BUILD, "--manifest", m, "--cache", os.path.join(self.t, "cache"), "--lock", "--tier", "large")
        self.assertEqual(r.returncode, 0, r.stderr)
        got = {i["id"]: i["sha256"] for i in json.load(open(m))["items"]}
        self.assertEqual(got["model"], sha(os.path.join(self.src, "model.gguf")))
        self.assertIsNone(got["nonfree"], "optional item pinned without --with")
        r = self.build(m, os.path.join(self.t, "kit"), "--frozen")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_unsafe_paths_rejected(self):
        bad = os.path.join(self.src, "evil.tar")
        with tarfile.open(bad, "w") as tf:
            ti = tarfile.TarInfo("../../escape"); ti.size = 1; tf.addfile(ti, io.BytesIO(b"x"))
        items = [{"id": "evil", "tier": "small", "kind": "tar", "url": self.url("evil.tar"), "sha256": sha(bad), "dest": "x/"}]
        r = self.build(self.manifest(items), os.path.join(self.t, "kit"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("unsafe archive member", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.t, "escape")))
        items = [{"id": "d", "tier": "small", "kind": "file", "url": self.url("model.gguf"), "sha256": None, "dest": "../x"}]
        r = self.build(self.manifest(items), os.path.join(self.t, "kit"))
        self.assertIn("unsafe path", r.stderr)

    def test_http_url_rejected(self):
        items = [{"id": "h", "tier": "small", "kind": "file", "url": "http://example.com/x", "sha256": None, "dest": "x"}]
        r = run(BUILD, "--manifest", self.manifest(items), "--check", env={"KIT_ALLOW_FILE_URLS": "0"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("only https://", r.stderr)

    def test_publish_refuses_nonredistributable(self):
        out, z = os.path.join(self.t, "kit"), os.path.join(self.t, "k.zip")
        r = self.build(self.manifest(self.items()), out, "--with", "nonfree", "--zip", z)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(json.load(open(os.path.join(out, "KIT-INFO.json")))["redistributable"])
        r = run(PUBLISH, "--zip", z, "--bucket", "b", "--dry-run")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("NOT redistributable", r.stderr)

    def test_publish_dry_run_ok(self):
        out, z = os.path.join(self.t, "kit"), os.path.join(self.t, "k.zip")
        self.assertEqual(self.build(self.manifest(self.items()), out, "--zip", z).returncode, 0)
        r = run(PUBLISH, "--zip", z, "--bucket", "b", "--dry-run")
        info = json.load(open(os.path.join(out, "KIT-INFO.json")))
        if info["git_dirty"]:
            self.assertIn("dirty git tree", r.stderr)  # running from a working copy with changes
        else:
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("dry run", r.stderr)


class RealManifest(unittest.TestCase):
    def test_manifest_valid(self):
        r = run(BUILD, "--check", "--tier", "large", "--with", "post-disaster")
        self.assertEqual(r.returncode, 0, r.stderr)
        m = json.loads(open(os.path.join(ROOT, "tools", "kit-manifest.json")).read())
        nonfree = [i["id"] for i in m["items"] if i.get("redistributable") is False]
        self.assertTrue(all(i.get("optional") for i in m["items"] if i["id"] in nonfree), "non-redistributable items must be optional")


if __name__ == "__main__":
    unittest.main(verbosity=2)
