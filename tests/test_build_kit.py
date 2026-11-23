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

    def test_fetch_only_then_build_parallel(self):
        m = self.manifest(self.items())
        r = run(BUILD, "--manifest", m, "--cache", os.path.join(self.t, "cache"), "--fetch-only", "--jobs", "4", "--frozen")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("cache filled", r.stderr)
        r = self.build(m, os.path.join(self.t, "kit"), "--jobs", "4", "--frozen")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("downloads and assembly took", r.stderr)

    def test_parallel_failure_stops_build(self):
        items = self.items()
        items[3]["sha256"] = "0" * 64
        r = self.build(self.manifest(items), os.path.join(self.t, "kit"), "--jobs", "4")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("sha256 mismatch", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

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

    def test_zim_extract(self):
        try:
            from libzim.writer import Creator, Item, StringProvider, Hint
        except ImportError:
            if os.environ.get("CI"):
                raise  # never silently skip in CI
            self.skipTest("libzim not installed (pip install -r tools/requirements-build.txt)")

        class PdfItem(Item):
            def __init__(self, path, data):
                super().__init__(); self.p, self.d = path, data
            def get_path(self): return self.p
            def get_title(self): return self.p
            def get_mimetype(self): return "application/pdf"
            def get_contentprovider(self): return StringProvider(self.d)
            def get_hints(self): return {Hint.FRONT_ARTICLE: False}

        pdf = b"%PDF-1.4 fake manual " * 50
        zim = os.path.join(self.src, "w.zim")
        with Creator(zim).config_indexing(False, "eng") as c:
            c.add_metadata("Title", "t")
            c.add_item(PdfItem("files/Water (2).pdf", pdf.decode()))
        zitem = {"id": "zim-water", "tier": "small", "kind": "file", "url": self.url("w.zim"), "sha256": sha(zim), "dest": "library/w.zim"}
        good = hashlib.sha256(pdf).hexdigest()
        ex = {"id": "pdf", "tier": "small", "kind": "zim-extract", "source": "zim-water", "entry": "files/Water (2).pdf",
              "sha256": good, "dest": "www/docs/pdf/water.pdf"}
        out = os.path.join(self.t, "kit")
        r = self.build(self.manifest([zitem, ex]), out, "--frozen")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(open(os.path.join(out, "www/docs/pdf/water.pdf"), "rb").read(), pdf)
        ex2 = dict(ex, sha256="1" * 64, dest="www/docs/pdf/x.pdf")
        r = self.build(self.manifest([zitem, ex2], "m2.json"), os.path.join(self.t, "kit2"))
        self.assertIn("sha256 mismatch", r.stderr)
        ex3 = dict(ex, entry="files/missing.pdf", sha256="2" * 64)
        r = self.build(self.manifest([zitem, ex3], "m3.json"), os.path.join(self.t, "kit3"))
        self.assertIn("not found", r.stderr)

    def test_git_commit_pin(self):
        repo = os.path.join(self.t, "repo")
        g = lambda *a: subprocess.run(["git", "-C", repo, *a], check=True, capture_output=True, text=True).stdout.strip()
        os.makedirs(os.path.join(repo, "fonts", "Noto"))
        open(os.path.join(repo, "fonts", "Noto", "0-255.pbf"), "w").write("glyphs")
        open(os.path.join(repo, "README.md"), "w").write("not copied")
        g("init", "-q"); g("add", "-A")
        g("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "x")
        commit = g("rev-parse", "HEAD")
        it = {"id": "assets", "tier": "small", "kind": "git", "url": "file://" + repo, "commit": commit,
              "include": ["fonts/*"], "dest": "www/maps/"}
        out = os.path.join(self.t, "kit")
        r = self.build(self.manifest([it]), out, "--frozen")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.isfile(os.path.join(out, "www/maps/fonts/Noto/0-255.pbf")))
        self.assertFalse(os.path.exists(os.path.join(out, "www/maps/README.md")))
        bad = dict(it, commit="0" * 40)
        r = self.build(self.manifest([bad], "m2.json"), os.path.join(self.t, "kit2"))
        self.assertNotEqual(r.returncode, 0)

    def test_version_validated(self):
        r = self.build(self.manifest(self.items()), os.path.join(self.t, "kit"), "--version", "../evil")
        self.assertIn("--version must match", r.stderr)


class RealManifest(unittest.TestCase):
    def test_manifest_valid(self):
        r = run(BUILD, "--check", "--tier", "large", "--with", "post-disaster", "--frozen")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(" 0 unpinned", r.stderr)
        m = json.loads(open(os.path.join(ROOT, "tools", "kit-manifest.json")).read())
        nonfree = [i["id"] for i in m["items"] if i.get("redistributable") is False]
        self.assertTrue(all(i.get("optional") for i in m["items"] if i["id"] in nonfree), "non-redistributable items must be optional")


class Downloads(unittest.TestCase):
    """aria2c + Kiwix metalink mirrors, the single-stream fallback, hash stamps and the GCS cache plan
    (in-process; aria2c and the network are stubbed)."""

    def setUp(self):
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import build_kit
        self.bk = build_kit
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = os.path.join(self.tmp.name, "cache")
        os.makedirs(self.cache)
        self.good = b"ZIM" + b"x" * 5000
        self.it = {"id": "zim", "tier": "medium", "kind": "file", "dest": "library/a.zim",
                   "url": "https://download.kiwix.org/zim/other/a_2026-01.zim",
                   "sha256": hashlib.sha256(self.good).hexdigest(), "size": len(self.good)}
        self.saved = (build_kit.Dl.aria2c, build_kit.Dl.mirrors, build_kit.Dl.jobs, build_kit.STAMPS,
                      build_kit.download, build_kit.OPENER, build_kit.ARIA2_MIN)
        build_kit.STAMPS = build_kit.Stamps(self.cache)
        build_kit.ARIA2_MIN = 0
        self.downloads = []

        def fake_download(url, dest, retries=5, tag=""):
            self.downloads.append(url)
            open(dest, "wb").write(self.good)
        build_kit.download = fake_download

    def tearDown(self):
        bk = self.bk
        (bk.Dl.aria2c, bk.Dl.mirrors, bk.Dl.jobs, bk.STAMPS, bk.download, bk.OPENER, bk.ARIA2_MIN) = self.saved
        self.tmp.cleanup()

    def stub_aria2c(self, payload, rc=0):
        """An aria2c that records its argv and writes payload to --dir/--out."""
        d = os.path.join(self.tmp.name, "bin")
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "payload"), "wb").write(payload)
        exe = os.path.join(d, "aria2c")
        with open(exe, "w") as fh:
            fh.write(f"""#!{sys.executable}
import sys
a = sys.argv[1:]
open({os.path.join(d, "argv")!r}, "w").write("\\n".join(a))
out = a[a.index("--dir") + 1] + "/" + a[a.index("--out") + 1]
open(out, "wb").write(open({os.path.join(d, "payload")!r}, "rb").read())
sys.exit({rc})
""")
        os.chmod(exe, 0o755)
        self.bk.Dl.aria2c = exe
        return os.path.join(d, "argv")

    def meta4(self, sha=None, size=None, extra=""):
        sha = sha or self.it["sha256"]
        size = self.it["size"] if size is None else size
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<metalink xmlns="urn:ietf:params:xml:ns:metalink">
 <file name="a_2026-01.zim"><size>{size}</size>
  <hash type="sha-256">{sha}</hash>
  <url location="de" priority="2">https://mirror-b.example/kiwix/zim/other/a_2026-01.zim</url>
  <url location="se" priority="1">https://mirror-a.example/zim/other/a_2026-01.zim</url>
  <url location="fr" priority="3">http://plain-http.example/zim/other/a_2026-01.zim</url>
  <url priority="4">https://wrong-name.example/zim/other/b.zim</url>{extra}
 </file></metalink>""".encode()

    def serve_meta4(self, data):
        seen = []

        class R(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False

        class Opener:
            def open(self, req, timeout=None):
                seen.append((req.get_method(), req.full_url))
                if req.get_method() == "HEAD":  # redirect lookup: pretend download.kiwix.org sends us to a mirror
                    r = R(b"")
                    r.geturl = lambda: redirect
                    return r
                if isinstance(data, Exception):
                    raise data
                return R(data)
        redirect = getattr(self, "redirect", "https://redirected.example/zim/other/a_2026-01.zim")
        self.bk.OPENER = Opener()
        return seen

    def test_parse_meta4_https_only_by_priority(self):
        urls = self.bk.parse_meta4(self.meta4(), self.it, "a_2026-01.zim")
        self.assertEqual(urls, ["https://mirror-a.example/zim/other/a_2026-01.zim",
                                "https://mirror-b.example/kiwix/zim/other/a_2026-01.zim"])

    def test_parse_meta4_rejects_changed_file_and_entities(self):
        for data in (self.meta4(sha="0" * 64), self.meta4(size=1),
                     b'<?xml version="1.0"?><!DOCTYPE m [<!ENTITY a "x">]><metalink/>'):
            with self.assertRaises(ValueError):
                self.bk.parse_meta4(data, self.it, "a_2026-01.zim")

    def test_aria2c_with_kiwix_mirrors(self):
        argv = self.stub_aria2c(self.good)
        seen = self.serve_meta4(self.meta4())
        path = self.bk.fetch(dict(self.it), self.cache, None)
        self.assertEqual(open(path, "rb").read(), self.good)
        self.assertEqual(seen, [("GET", self.it["url"] + ".meta4")])
        a = open(argv).read().split("\n")
        # only the direct mirrors: the download.kiwix.org URL redirects, and aria2c can't refuse http redirects
        self.assertEqual(a[a.index("--") + 1:], ["https://mirror-a.example/zim/other/a_2026-01.zim",
                                                  "https://mirror-b.example/kiwix/zim/other/a_2026-01.zim"])
        self.assertIn("--follow-metalink=false", a)
        self.assertEqual(self.downloads, [], "fell back although aria2c worked")

    def test_metalink_failure_uses_resolved_redirect_target(self):
        argv = self.stub_aria2c(self.good)
        seen = self.serve_meta4(OSError("boom"))
        self.bk.fetch(dict(self.it), self.cache, None)
        a = open(argv).read().split("\n")
        self.assertEqual(a[a.index("--") + 1:], ["https://redirected.example/zim/other/a_2026-01.zim"])
        self.assertIn(("HEAD", self.it["url"]), seen)

    def test_http_redirect_target_skips_aria2c(self):
        argv = self.stub_aria2c(self.good)
        self.redirect = "http://plain.example/a_2026-01.zim"
        self.serve_meta4(OSError("boom"))
        it = dict(self.it)
        self.bk.fetch(it, self.cache, None)
        self.assertFalse(os.path.exists(argv), "aria2c must not get a URL that redirects to http")
        self.assertEqual(self.downloads, [self.it["url"]])
        self.assertEqual(it["_dl"]["how"], "https, 1 stream")

    def test_aria2c_failure_or_bad_bytes_falls_back_to_single_stream(self):
        self.serve_meta4(self.meta4())
        for payload, rc in ((self.good, 3), (b"evil" * 100, 0)):
            with self.subTest(rc=rc):
                for f in os.listdir(self.cache):
                    if f != ".verified":
                        os.remove(os.path.join(self.cache, f))
                self.downloads.clear()
                self.stub_aria2c(payload, rc)
                it = dict(self.it)
                path = self.bk.fetch(it, self.cache, None)
                self.assertEqual(open(path, "rb").read(), self.good)
                self.assertEqual(self.downloads, [self.it["url"]], "fallback must use the manifest URL")
                self.assertEqual(it["_dl"]["how"], "https, 1 stream")

    def test_bad_cached_copy_is_downloaded_again(self):
        self.bk.Dl.aria2c = None
        path = os.path.join(self.cache, self.bk.cache_name(self.it))
        open(path, "wb").write(b"poisoned cache object")
        open(os.path.join(self.cache, ".gcs-pulled"), "w").write(os.path.basename(path) + "\n")
        self.bk.fetch(dict(self.it), self.cache, None)
        self.assertEqual(open(path, "rb").read(), self.good)
        self.assertEqual(self.downloads, [self.it["url"]])
        self.assertTrue(os.path.exists(path + ".bad"))

    def test_good_cached_copy_is_used(self):
        self.bk.Dl.aria2c = None
        path = os.path.join(self.cache, self.bk.cache_name(self.it))
        open(path, "wb").write(self.good)
        open(os.path.join(self.cache, ".gcs-pulled"), "w").write(os.path.basename(path) + "\n")
        it = dict(self.it)
        self.bk.fetch(it, self.cache, None)
        self.assertEqual(self.downloads, [])
        self.assertEqual(it["_dl"]["how"], "GCS cache")

    def test_stamps_avoid_rehashing_and_expire_on_change(self):
        p = os.path.join(self.cache, "f.bin")
        open(p, "wb").write(self.good)
        calls = []
        real = self.bk.sha256_file
        self.bk.sha256_file = lambda x: calls.append(x) or real(x)
        try:
            h = self.bk.STAMPS.sha(p)
            self.assertEqual(self.bk.Stamps(self.cache).sha(p), h, "a later build step must reuse .verified")
            link = os.path.join(self.tmp.name, "kit-link")
            os.link(p, link)
            self.assertEqual(self.bk.STAMPS.known(link), h, "hard links in the kit reuse the hash")
            self.assertEqual(len(calls), 1)
            open(p, "ab").write(b"more")
            self.assertNotEqual(self.bk.Stamps(self.cache).sha(p), h)
            self.assertEqual(len(calls), 2)
        finally:
            self.bk.sha256_file = real
        line = open(os.path.join(self.cache, ".verified")).readline().split()
        self.assertEqual(line[:2], [h, "f.bin"])

    def test_cache_plan(self):
        out = os.path.join(self.tmp.name, "plan.txt")
        m = {i["id"]: i for i in json.load(open(os.path.join(ROOT, "tools", "kit-manifest.json")))["items"]}
        self.bk.OPENER = FakeProtomaps({m["map-australia"]["url"]: b"PMTiles\x03"})  # the map's build is probed
        self.assertEqual(self.bk.main(["--tier", "large", "--frozen", "--cache-plan", out]), 0)
        lines = [l.split() for l in open(out).read().splitlines()]
        maxi = m["zim-wikipedia-maxi"]
        self.assertIn([f"cache/sha256/{maxi['sha256']}", self.bk.cache_name(maxi), maxi["sha256"]], lines)
        self.assertNotIn(m["zim-wikipedia-mini"]["sha256"], open(out).read(), "replaced items are not cached")
        self.assertNotIn(m["js-basemaps"]["sha256"], open(out).read(), "tiny files are not worth caching")
        name = self.bk.map_cache_name(m["map-australia"], m["map-australia"]["url"])
        self.assertIn([f"cache/pmtiles/{name}", name, "-"], lines)
        for obj, name, h in lines:
            self.assertRegex(obj, r"^cache/(sha256/[0-9a-f]{64}|pmtiles/[0-9a-f]{16}-[A-Za-z0-9._-]+)$")
            self.assertRegex(name, r"^[0-9a-f]{16}-[A-Za-z0-9._-]+$")


class FakeProtomaps:
    """Stands in for build_kit.OPENER: serves builds.json and PMTiles headers from a dict url -> bytes,
    answering 404 for anything else (a pruned build). Records every URL asked for."""

    def __init__(self, files):
        self.files, self.seen = files, []

    def open(self, req, timeout=None):
        import urllib.error
        self.seen.append(req.full_url)
        if req.full_url not in self.files:
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)
        r = io.BytesIO(self.files[req.full_url])
        r.__enter__, r.__exit__ = (lambda: r), (lambda *a: False)
        return r


class GoVersion(unittest.TestCase):
    """go-pmtiles needs a minimum Go (manifest tools.go-pmtiles.go). Both builders must provide it."""

    def setUp(self):
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import build_kit
        self.bk = build_kit
        self.tool = json.load(open(os.path.join(ROOT, "tools", "kit-manifest.json")))["tools"]["go-pmtiles"]
        self.need = build_kit.go_version_tuple(self.tool["go"])

    def test_cloudbuild_golang_image_meets_requirement(self):
        # The image is pinned by digest; the trailing tag comment says which golang tag that digest is.
        import re
        text = open(os.path.join(ROOT, "cloudbuild.yaml")).read()
        m = re.search(r"name: mirror\.gcr\.io/library/golang@sha256:[0-9a-f]{64}\s+#\s*(\d+\.\d+(?:\.\d+)?)-", text)
        self.assertIsNotNone(m, "cloudbuild.yaml build step must use a digest-pinned golang image with a '# <ver>-<os>' comment")
        self.assertGreaterEqual(self.bk.go_version_tuple(m.group(1)), self.need, "golang image too old for go-pmtiles")

    def test_publish_workflow_toolchain_meets_requirement(self):
        import re
        text = open(os.path.join(ROOT, ".github", "workflows", "publish.yml")).read()
        m = re.search(r"^\s*GOTOOLCHAIN:\s*go(\d+\.\d+\.\d+)\s*$", text, re.M)
        self.assertIsNotNone(m, "publish.yml's small-kit build must pin GOTOOLCHAIN: go<x.y.z>")
        self.assertGreaterEqual(self.bk.go_version_tuple(m.group(1)), self.need, "GOTOOLCHAIN too old for go-pmtiles")

    def _check(self, goversion, toolchain):
        # Stub `go` that prints `go env GOVERSION GOTOOLCHAIN` output: a batch file on Windows, sh elsewhere.
        with tempfile.TemporaryDirectory() as d:
            if os.name == "nt":
                with open(os.path.join(d, "go.bat"), "w") as fh:
                    fh.write(f"@echo off\r\necho {goversion}\r\necho {toolchain}\r\n")
            else:
                go = os.path.join(d, "go")
                with open(go, "w") as fh:
                    fh.write(f"#!/bin/sh\necho {goversion}\necho {toolchain}\n")
                os.chmod(go, 0o755)
            env = {**os.environ, "PATH": d + os.pathsep + os.environ.get("PATH", "")}
            self.bk.check_go_version(self.tool, env)

    def test_old_go_that_cannot_download_fails_clearly(self):
        too_old = "go1.24.0"
        for tc in ("local", "path", "local+path", too_old, too_old + "+path"):
            with self.subTest(GOTOOLCHAIN=tc), self.assertRaises(SystemExit):
                self._check("go1.24.13", tc)

    def test_new_enough_or_switchable_go_passes(self):
        self._check("go" + self.tool["go"], "local")
        self._check("go" + self.tool["go"], "path")
        for tc in ("auto", "local+auto", "go1.24.0+auto", "go" + self.tool["go"], "go" + self.tool["go"] + "+path"):
            with self.subTest(GOTOOLCHAIN=tc):
                self._check("go1.24.13", tc)


class MapBuilds(unittest.TestCase):
    """Protomaps deletes old daily builds. The map uses the manifest's build while it exists, else the newest
    build builds.json still lists, and records which one (in-process; network and go-pmtiles are stubbed)."""
    BUILDS = "https://build-metadata.protomaps.dev/builds.json"

    def setUp(self):
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import build_kit
        self.bk = build_kit
        self.saved = (build_kit.OPENER, build_kit.STAMPS)
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = os.path.join(self.tmp.name, "cache")
        os.makedirs(self.cache)
        build_kit.STAMPS = build_kit.Stamps(self.cache)
        self.it = {"id": "map", "tier": "small", "kind": "pmtiles", "url": self.u("20261115"),
                   "bbox": "112.5,-44,154,-9.5", "maxzoom": 15, "tiles_major": 4, "sha256": None, "pin": "none",
                   "dest": "www/maps/au.pmtiles"}
        # 20261119 is listed but its file is missing; 20261115 (the manifest's) is pruned; 20261122 has a
        # new tile schema the pinned style can't render; 20230901 is an old v3 weekly build.
        self.listed = json.dumps([{"key": f"{d}.pmtiles", "size": 1, "version": v} for d, v in (
            ("20261116", "4.15.2"), ("20261122", "5.0.0"), ("20261121", "4.15.2"), ("20261117", "4.15.2"),
            ("20261119", "4.15.2"), ("20261118", "4.15.1"), ("20230901", "3.7.1"))]
            + [{"key": "README.txt"}, {"key": 5}, "junk"]).encode()
        self.files = {self.BUILDS: self.listed, **{self.u(d): b"PMTiles\x03" for d in ("20261116", "20261117", "20261118", "20261121", "20261122", "20230901")}}
        self.tool = self.stub_pmtiles()

    def tearDown(self):
        self.bk.OPENER, self.bk.STAMPS = self.saved
        self.tmp.cleanup()

    @staticmethod
    def u(build):
        return f"https://build.protomaps.com/{build}.pmtiles"

    def stub_pmtiles(self, fail=()):
        """A go-pmtiles that logs each source URL and writes '<url>' as the extract, or exits 1 for fail."""
        d = os.path.join(self.tmp.name, "bin")
        os.makedirs(d, exist_ok=True)
        self.calls = os.path.join(d, "calls")
        exe = os.path.join(d, "pmtiles")
        with open(exe, "w") as fh:
            fh.write(f"""#!{sys.executable}
import sys
a = sys.argv[1:]
open({self.calls!r}, "a").write(a[1] + "\\n")
if a[1] in {list(fail)!r}:
    sys.exit(1)
open(a[2], "w").write(a[1])
""")
        os.chmod(exe, 0o755)
        return exe

    def extracted(self):
        return open(self.calls).read().split() if os.path.exists(self.calls) else []

    def build(self, files=None):
        self.bk.OPENER = fake = FakeProtomaps(self.files if files is None else files)
        path = self.bk.map_cached(self.it, {"tools": {}}, self.cache, self.tool)
        return path, fake.seen

    def test_parse_builds_newest_first_dates_only(self):
        self.assertEqual(self.bk.parse_builds(self.listed), [
            ("20261122", "5.0.0"), ("20261121", "4.15.2"), ("20261119", "4.15.2"), ("20261118", "4.15.1"),
            ("20261117", "4.15.2"), ("20261116", "4.15.2"), ("20230901", "3.7.1")])
        self.assertEqual(self.bk.parse_builds(b'{"builds": ["20261120.pmtiles"]}'), [("20261120", None)])
        self.assertEqual(self.bk.parse_builds(b'[{"key": "20261120.pmtiles", "version": "4; rm"}]'), [("20261120", None)])
        for bad in (b"[]", b'[{"key": "x.pmtiles"}]', b'{"a": 1}', b'"20261120"'):
            with self.subTest(bad), self.assertRaises(ValueError):
                self.bk.parse_builds(bad)

    def test_only_https_build_protomaps_com_urls(self):
        self.assertEqual(self.bk.protomaps_build(self.u("20261121")), "20261121")
        for url in ("http://build.protomaps.com/20261121.pmtiles", "https://evil.example/20261121.pmtiles",
                    "https://build.protomaps.com.evil.example/20261121.pmtiles", "https://build.protomaps.com:8443/20261121.pmtiles",
                    "https://x@build.protomaps.com/20261121.pmtiles", "https://build.protomaps.com/a/20261121.pmtiles",
                    "https://build.protomaps.com/20261121.pmtiles?x=1", "https://build.protomaps.com/latest.pmtiles"):
            with self.subTest(url):
                self.assertIsNone(self.bk.protomaps_build(url))

    def test_manifest_rejects_other_map_hosts_and_build_values(self):
        for change in ({"url": "https://evil.example/20261121.pmtiles"}, {"build": "20261121"}, {"tiles_major": "4"}, {"tiles_major": True}):
            with tempfile.TemporaryDirectory() as d, self.subTest(change):
                p = os.path.join(d, "m.json")
                json.dump({"tools": {}, "items": [{**self.it, **change}]}, open(p, "w"))
                r = run(BUILD, "--manifest", p, "--check")
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("map", r.stderr)

    def test_manifest_build_used_while_available(self):
        self.files[self.u("20261115")] = b"PMTiles\x03"
        path, seen = self.build()
        self.assertEqual(self.extracted(), [self.u("20261115")])
        self.assertEqual([u for u in seen if u.endswith(".pmtiles")], [self.u("20261115")], "no other build is tried")
        self.assertEqual(os.path.basename(path), self.bk.map_cache_name(self.it, self.u("20261115")))
        self.assertEqual(self.it["_map"], {"url": self.u("20261115"), "protomaps_build": "20261115", "tiles_version": None,
                                           "requested_url": self.u("20261115"), "fallback": False})

    def test_pruned_manifest_build_falls_back_to_newest(self):
        path, seen = self.build()
        self.assertEqual(seen[:3], [self.u("20261115"), self.BUILDS, self.u("20261121")])
        self.assertEqual(self.extracted(), [self.u("20261121")])
        self.assertEqual(os.path.basename(path), self.bk.map_cache_name(self.it, self.u("20261121")))
        self.assertEqual(open(path).read(), self.u("20261121"))
        self.assertEqual(self.it["_map"]["protomaps_build"], "20261121")
        self.assertEqual(self.it["_map"]["tiles_version"], "4.15.2")
        self.assertTrue(self.it["_map"]["fallback"])
        self.assertEqual(self.it["_map"]["requested_url"], self.u("20261115"))
        self.assertEqual(self.it["_sha256"], sha(path))

    def test_missing_or_failing_build_tries_the_next_newest(self):
        del self.files[self.u("20261121")]  # listed but gone too
        self.tool = self.stub_pmtiles(fail=[self.u("20261118")])  # 20261119 404s; 20261118 extract fails
        path, _ = self.build()
        self.assertEqual(self.extracted(), [self.u("20261118"), self.u("20261117")])
        self.assertEqual(self.it["_map"]["protomaps_build"], "20261117")
        self.assertFalse(any(f.endswith(".part") for f in os.listdir(self.cache)))

    def test_latest_skips_the_manifest_url(self):
        self.it["build"] = "latest"
        self.files[self.u("20261115")] = b"PMTiles\x03"
        _, seen = self.build()
        self.assertNotIn(self.u("20261115"), seen)
        self.assertEqual(self.it["_map"]["protomaps_build"], "20261121")
        self.assertIsNone(self.it["_map"]["requested_url"])
        self.assertFalse(self.it["_map"]["fallback"], "nothing was asked for and not used")

    def test_newer_tile_schema_is_skipped(self):
        self.build()
        self.assertNotIn(self.u("20261122"), self.extracted())
        self.assertEqual(self.it["_map"]["protomaps_build"], "20261121")
        # with nothing of the expected major listed, no build is used (old v3 builds included)
        self.it["tiles_major"] = 6
        with self.assertRaises(SystemExit):
            self.build()
        self.assertEqual(self.extracted(), [self.u("20261121")])
        # without a tiles_major guard the newest build is taken, whatever its schema
        del self.it["tiles_major"]
        os.remove(os.path.join(self.cache, self.bk.map_cache_name(self.it, self.u("20261121"))))  # else it is reused
        self.build()
        self.assertEqual(self.it["_map"]["protomaps_build"], "20261122")

    def test_broken_http_response_tries_the_next_build(self):
        import http.client
        fake = FakeProtomaps(self.files)
        real_open = fake.open

        def open_(req, timeout=None):
            if req.full_url == self.u("20261121"):
                raise http.client.IncompleteRead(b"PM")
            return real_open(req, timeout)
        fake.open = open_
        self.bk.OPENER = fake
        self.bk.map_cached(self.it, {"tools": {}}, self.cache, self.tool)
        self.assertEqual(self.it["_map"]["protomaps_build"], "20261118")

    def test_cached_extract_of_a_listed_build_is_reused(self):
        # e.g. --fetch-only extracted 20261118 (or GCS had it), and 20261121 appeared before the build step
        name = self.bk.map_cache_name(self.it, self.u("20261118"))
        open(os.path.join(self.cache, name), "w").write("cached")
        path, _ = self.build()
        self.assertEqual(self.extracted(), [])
        self.assertEqual(os.path.basename(path), name)
        self.assertEqual(self.it["_map"]["protomaps_build"], "20261118")

    def test_stale_cache_is_not_reused_under_another_build(self):
        # an extract of a build that is neither the manifest's nor still listed must not be used
        open(os.path.join(self.cache, self.bk.map_cache_name(self.it, self.u("20261110"))), "w").write("old")
        path, _ = self.build()
        self.assertEqual(self.extracted(), [self.u("20261121")])
        self.assertEqual(open(path).read(), self.u("20261121"))
        self.assertNotEqual(self.bk.map_cache_name(self.it, self.u("20261110")), os.path.basename(path))

    def test_no_build_available_fails_clearly(self):
        for files in ({}, {self.BUILDS: self.listed}, {self.BUILDS: b"not json"}):
            with self.subTest(files=list(files)), self.assertRaises(SystemExit):
                self.build(files)

    def test_not_a_pmtiles_archive_is_skipped(self):
        self.files[self.u("20261121")] = b"<html>error page"
        self.build()
        self.assertEqual(self.it["_map"]["protomaps_build"], "20261118")

    def test_cache_plan_names_the_fallback_build(self):
        plan = os.path.join(self.tmp.name, "plan.txt")
        self.bk.OPENER = FakeProtomaps(self.files)
        self.bk.cache_plan([self.it], plan)
        name = self.bk.map_cache_name(self.it, self.u("20261121"))
        self.assertEqual(open(plan).read(), f"cache/pmtiles/{name} {name} -\n")
        self.bk.OPENER = FakeProtomaps({})
        self.bk.cache_plan([self.it], plan)  # nothing reachable: no map line, but the plan is still written
        self.assertEqual(open(plan).read(), "")

    def test_kit_info_records_the_build_used(self):
        self.build()
        out = os.path.join(self.tmp.name, "kit")
        os.makedirs(out)
        info = self.bk.write_info(out, "small", [self.it], "v1", "abc", False)["items"][0]
        self.assertEqual(info["url"], self.u("20261121"))
        self.assertEqual(info["protomaps_build"], "20261121")
        self.assertEqual(info["tiles_version"], "4.15.2")
        self.assertEqual(info["requested_url"], self.u("20261115"))
        self.assertTrue(info["fallback"])
        self.assertEqual(json.load(open(os.path.join(out, "KIT-INFO.json")))["items"][0], info)

if __name__ == "__main__":
    unittest.main(verbosity=2)
