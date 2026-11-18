#!/usr/bin/env python3
"""Assemble a ready-to-copy Offline Survival Kit from tools/kit-manifest.json.

Run on an internet-connected Linux/macOS box (python3 stdlib only; plus pdftotext for the manuals index
and Go, or a `pmtiles` binary, for the map). Examples:

  python3 tools/build_kit.py --check                 # show what would be downloaded / what is unpinned
  python3 tools/build_kit.py                         # small tier -> dist/offline-toolkit-small/
  python3 tools/build_kit.py --tier medium --zip     # + dist/offline-toolkit-medium-<ver>.zip
  python3 tools/build_kit.py --lock                  # download, then write sha256/size into the manifest
  python3 tools/build_kit.py --frozen --zip          # CI: refuse anything not pinned

Security model: every download goes over HTTPS only (redirects too), lands in a cache as *.part, is hashed,
and is only used if it matches the pinned sha256. Nothing downloaded is executed, except go-pmtiles,
which is built from source by `go install` (verified by the Go checksum database). Archives are
extracted member-by-member with path checks (no absolute paths, no '..', no links).
"""
import argparse, fnmatch, hashlib, json, os, re, shutil, stat, subprocess, sys, tarfile, time
import urllib.error, urllib.parse, urllib.request, zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(ROOT, "tools", "kit-manifest.json")
TIERS = ["small", "medium", "large"]
UA = "offline-toolkit-build/1"
# Repo files copied into every kit (paths relative to ROOT). Generated/large content comes from the manifest.
KIT_CODE = ["start.sh", "start.bat", "stop.sh", "stop.bat", "README.md", "LICENSE", "tools", "www"]
SKIP_CODE = {"__pycache__", ".DS_Store"}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def die(msg):
    log("ERROR: " + msg)
    sys.exit(1)


# ---------------------------------------------------------------- manifest
def load_manifest(path):
    with open(path, encoding="utf-8") as fh:
        m = json.load(fh)
    ids = set()
    for it in m["items"]:
        need = ("id", "tier", "kind", "dest") + (("source", "entry", "sha256") if it.get("kind") == "zim-extract" else ("url",))
        for k in need:
            if k not in it:
                die(f"manifest item {it.get('id')!r} missing {k!r}")
        if it["id"] in ids:
            die(f"duplicate manifest id {it['id']!r}")
        ids.add(it["id"])
        if it["tier"] not in TIERS:
            die(f"{it['id']}: bad tier {it['tier']!r}")
        if it["kind"] not in ("file", "tar", "zip", "pmtiles", "zim-extract", "git"):
            die(f"{it['id']}: bad kind {it['kind']!r}")
        if "url" in it:
            check_url(it["url"], it["id"])
        if it["kind"] == "git" and not re.fullmatch(r"[0-9a-f]{40}", it.get("commit") or ""):
            die(f"{it['id']}: git items need a full 40-hex \"commit\"")
        safe_rel(it["dest"].rstrip("/"), it["id"])
        if it.get("sha256") is not None and not re.fullmatch(r"[0-9a-f]{64}", it["sha256"]):
            die(f"{it['id']}: sha256 must be 64 lowercase hex chars")
    for it in m["items"]:
        if it["kind"] == "zim-extract":
            src = next((x for x in m["items"] if x["id"] == it["source"]), None)
            if not src or src["kind"] != "file" or not src["dest"].endswith(".zim"):
                die(f"{it['id']}: source {it['source']!r} must be a ZIM file item")
    return m


def check_url(url, what):
    scheme = urllib.parse.urlparse(url).scheme
    if scheme == "https":
        return
    if scheme == "file" and os.environ.get("KIT_ALLOW_FILE_URLS") == "1":  # tests only
        return
    die(f"{what}: only https:// URLs are allowed, got {url!r}")


def safe_rel(path, what):
    """Reject absolute paths and '..' so nothing is written outside the kit."""
    p = path.replace("\\", "/")
    if not p or p.startswith("/") or re.match(r"^[A-Za-z]:", p) or any(x in ("..", "") for x in p.split("/")):
        die(f"{what}: unsafe path {path!r}")
    return p


def select(m, tier, optional):
    lim = TIERS.index(tier)
    items = [it for it in m["items"] if TIERS.index(it["tier"]) <= lim and (not it.get("optional") or it["id"] in optional)]
    unknown = set(optional) - {it["id"] for it in m["items"] if it.get("optional")}
    if unknown:
        die("unknown optional item(s): " + ", ".join(sorted(unknown)))
    replaced = {r for it in items for r in it.get("replaces", [])}
    return [it for it in items if it["id"] not in replaced]


def pinned(it):
    return it.get("pin") == "none" or bool(it.get("sha256")) or (it["kind"] == "git" and bool(it.get("commit")))


# ---------------------------------------------------------------- download
class HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlparse(newurl).scheme != "https":
            raise urllib.error.URLError(f"refusing redirect to non-https URL {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


OPENER = urllib.request.build_opener(HttpsOnlyRedirect)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()


def download(url, dest, retries=5):
    """Download url to dest (resuming dest+'.part'). Returns nothing; caller verifies the hash."""
    part = dest + ".part"
    for attempt in range(1, retries + 1):
        have = os.path.getsize(part) if os.path.exists(part) else 0
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        if have:
            req.add_header("Range", f"bytes={have}-")
        try:
            with OPENER.open(req, timeout=60) as r:
                if have and r.status != 206:
                    have = 0  # server ignored Range: start again
                cl = r.headers.get("Content-Length")
                total = (int(cl) + have) if cl else None
                with open(part, "ab" if have else "wb") as fh:
                    done, last = have, time.time()
                    for b in iter(lambda: r.read(1 << 20), b""):
                        fh.write(b)
                        done += len(b)
                        if time.time() - last > 15:
                            last = time.time()
                            log(f"    {done / 1e6:,.0f} MB" + (f" / {total / 1e6:,.0f} MB" if total else ""))
            os.replace(part, dest)
            return
        except urllib.error.HTTPError as e:
            if e.code == 416 and have:  # range not satisfiable: .part is complete or bad; restart
                os.remove(part)
            elif e.code in (404, 410, 401, 403):
                die(f"download failed ({e.code}) for {url}")
            log(f"    attempt {attempt}: HTTP {e.code}")
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            log(f"    attempt {attempt}: {e}")
        time.sleep(min(60, 2 ** attempt))
    die(f"giving up on {url}")


def fetch(it, cache, lock):
    """Return path of a verified local copy of the item's URL."""
    url = it["url"]
    if urllib.parse.urlparse(url).scheme == "file":
        src = urllib.request.url2pathname(urllib.parse.urlparse(url).path)
        path = src
    else:
        key = it["sha256"] or hashlib.sha256(url.encode()).hexdigest()
        name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(urllib.parse.urlparse(url).path) or it["id"])[-80:]
        path = os.path.join(cache, f"{key[:16]}-{name}")
        if not os.path.exists(path):
            log(f"  downloading {url}")
            download(url, path)
    return verify(it, path, cache, lock)


def verify(it, path, cache, lock):
    url = it.get("url") or f"{it.get('source')}:{it.get('entry')}"
    got = sha256_file(path)
    size = os.path.getsize(path)
    if it.get("sha256"):
        if got != it["sha256"]:
            if path.startswith(cache):
                os.replace(path, path + ".bad")
            die(f"{it['id']}: sha256 mismatch\n  expected {it['sha256']}\n  got      {got}\n  ({url})")
        if it.get("size") and size != it["size"]:
            die(f"{it['id']}: size mismatch (expected {it['size']}, got {size})")
    elif lock is not None:
        lock[it["id"]] = (got, size)
    else:
        log(f"  WARNING {it['id']} is not pinned; got sha256 {got} (run --lock to pin)")
    it["_sha256"], it["_size"] = got, size
    return path


# ---------------------------------------------------------------- install
def zim_extract(it, m, cache, lock):
    """Pull one entry (e.g. a PDF) out of a pinned ZIM. The ZIM is verified first, then the entry."""
    src = next(x for x in m["items"] if x["id"] == it["source"])
    zim = fetch(dict(src), cache, None)
    key = it["sha256"] or hashlib.sha256(f"{src['sha256']}|{it['entry']}".encode()).hexdigest()
    path = os.path.join(cache, f"{key[:16]}-{os.path.basename(it['dest'])}")
    if not os.path.exists(path):
        try:
            from libzim.reader import Archive
        except ImportError:
            die("zim-extract needs libzim: python3 -m pip install --require-hashes --only-binary :all: -r tools/requirements-build.txt")
        try:
            data = bytes(Archive(zim).get_entry_by_path(it["entry"]).get_item().content)
        except KeyError:
            die(f"{it['id']}: entry {it['entry']!r} not found in {src['dest']}")
        with open(path + ".part", "wb") as fh:
            fh.write(data)
        os.replace(path + ".part", path)
        log(f"  extracted {it['entry']!r} from {os.path.basename(src['dest'])}")
    return verify(it, path, cache, lock)


def git_checkout(it, out, cache):
    """Shallow-fetch exactly it['commit']. Git verifies every object against that hash, so this is as
    strong a pin as a sha256. Hooks are disabled and nothing from the repo is executed."""
    d = os.path.join(cache, "git-" + it["commit"])
    g = ["git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.file.allow=" +
         ("always" if os.environ.get("KIT_ALLOW_FILE_URLS") == "1" else "never"), "-C", d]
    if not os.path.isdir(os.path.join(d, ".git")):
        os.makedirs(d, exist_ok=True)
        log(f"  fetching {it['url']} @ {it['commit'][:12]}")
        subprocess.run(g + ["init", "-q"], check=True)
        subprocess.run(g + ["fetch", "-q", "--depth", "1", it["url"], it["commit"]], check=True)
        subprocess.run(g + ["-c", "advice.detachedHead=false", "checkout", "-q", "FETCH_HEAD"], check=True)
    head = subprocess.run(g + ["rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    if head != it["commit"]:
        die(f"{it['id']}: checked out {head}, expected {it['commit']}")
    n = 0
    for rel, p in walk(d):
        if rel.startswith(".git/") or os.path.islink(p) or not match(rel, it.get("include", ["*"])):
            continue
        place(p, os.path.join(out, safe_rel(it["dest"].rstrip("/"), it["id"]), *rel.split("/")))
        n += 1
    if not n:
        die(f"{it['id']}: nothing matched {it.get('include')}")
    it["_sha256"], it["_size"] = None, None
    log(f"  copied {n} file(s) -> {it['dest']}")


def place(src, dst, mode=None):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst):
        os.remove(dst)
    try:
        os.link(src, dst)  # same filesystem: no second copy of multi-GB files
    except OSError:
        shutil.copyfile(src, dst)
    if mode:
        os.chmod(dst, int(mode, 8))


def match(name, pats):
    return any(fnmatch.fnmatchcase(name, p) for p in pats)


def extract(it, archive, out):
    """Extract regular files matching it['include'] into out/it['dest']."""
    pats, dest = it.get("include", ["*"]), it["dest"]
    strip, flatten, n = int(it.get("strip", 0)), bool(it.get("flatten")), 0

    def target(name):
        parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".")]
        if ".." in parts or name.startswith("/"):
            die(f"{it['id']}: unsafe archive member {name!r}")
        rel = parts[-1:] if flatten else parts[strip:]
        if not rel:
            return None
        return os.path.join(out, safe_rel(dest.rstrip("/"), it["id"]), *rel)

    def write(fobj, path, mode):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            shutil.copyfileobj(fobj, fh)
        os.chmod(path, int(it["mode"], 8) if it.get("mode") else mode)

    if it["kind"] == "tar":
        with tarfile.open(archive) as tf:
            for m in tf:
                if not m.isfile() or not match(m.name, pats):
                    continue  # links, devices and dirs are never extracted
                p = target(m.name)
                if p:
                    write(tf.extractfile(m), p, 0o644)
                    n += 1
    else:
        with zipfile.ZipFile(archive) as zf:
            for m in zf.infolist():
                if m.is_dir() or not match(m.filename, pats):
                    continue
                if stat.S_ISLNK(m.external_attr >> 16):
                    continue
                p = target(m.filename)
                if p:
                    with zf.open(m) as fobj:
                        write(fobj, p, 0o644)
                    n += 1
    if not n:
        die(f"{it['id']}: nothing in the archive matched {pats}")
    log(f"  extracted {n} file(s) -> {dest}")


def go_version_tuple(v):
    """'go1.25.14' / '1.25' -> (1, 25, 14) / (1, 25, 0)."""
    m = re.match(r"(?:go)?(\d+)\.(\d+)(?:\.(\d+))?", v.strip())
    return tuple(int(x or 0) for x in m.groups()) if m else None


def check_go_version(t, env):
    """Fail clearly, before `go install`, when the Go toolchain is older than the module needs and the go
    command will not download a newer one: GOTOOLCHAIN=local (as in the official golang images), path,
    local+path, goX.Y.Z+path, or a pinned goX.Y.Z below the minimum. Only auto / <name>+auto can fetch."""
    need = t.get("go")
    if not need:
        return
    go = shutil.which("go", path=env.get("PATH")) or "go"  # which() honours PATHEXT, so go.bat/go.cmd work on Windows
    r = subprocess.run([go, "env", "GOVERSION", "GOTOOLCHAIN"], capture_output=True, text=True, env=env)
    have, toolchain = [x.strip() for x in (r.stdout.splitlines() + ["", ""])[:2]]
    hv, nv = go_version_tuple(have), go_version_tuple(need)
    if r.returncode != 0 or not hv or not nv or not toolchain or hv >= nv:
        return
    base, _, mode = toolchain.partition("+")
    if base in ("auto", "path"):  # shorthand for local+auto / local+path
        base, mode = "local", base
    if mode == "auto":
        return
    pinned = go_version_tuple(base) if base.startswith("go") else None
    if pinned and pinned >= nv:
        return
    die(f"{t['module']}@{t['version']} needs Go >= {need}, but `go` is {have} with GOTOOLCHAIN={toolchain}, "
        f"which will not download a newer toolchain. "
        f"Install Go {need} or newer, set GOTOOLCHAIN=go{need} (or newer), or pass --pmtiles /path/to/pmtiles")


def pmtiles_tool(m, cache, given):
    if given:
        return given
    for name in ("pmtiles", "go-pmtiles"):
        if shutil.which(name):
            return shutil.which(name)
    t = m["tools"]["go-pmtiles"]
    gobin = os.path.join(cache, "gobin-" + t["version"])
    exe = os.path.join(gobin, "go-pmtiles")
    if not os.path.exists(exe):
        if not shutil.which("go"):
            die("the map needs go-pmtiles: install Go, pass --pmtiles /path/to/pmtiles, or use --skip map-australia")
        env = {k: v for k, v in os.environ.items() if k not in ("GONOSUMDB", "GONOSUMCHECK", "GOINSECURE", "GOPRIVATE", "GOFLAGS")}
        env.update(GOBIN=gobin, GOSUMDB="sum.golang.org")  # never let the environment turn checksum verification off
        check_go_version(t, env)
        log(f"  building {t['module']}@{t['version']} with go install (checksum-verified)")
        subprocess.run(["go", "install", f"{t['module']}@{t['version']}"], check=True, env=env)
    return exe


def build_map(it, m, out, cache, tool):
    dst = os.path.join(out, it["dest"])
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    key = hashlib.sha256(f"{it['url']}|{it['bbox']}|{it['maxzoom']}".encode()).hexdigest()[:16]
    cached = os.path.join(cache, f"{key}-{os.path.basename(dst)}")
    if not os.path.exists(cached):
        exe = pmtiles_tool(m, cache, tool)
        log(f"  extracting map {it['bbox']} z0-{it['maxzoom']} from {it['url']} (~GBs, takes a while)")
        subprocess.run([exe, "extract", it["url"], cached + ".part", f"--bbox={it['bbox']}",
                        f"--maxzoom={it['maxzoom']}", "--download-threads=8"], check=True)
        os.replace(cached + ".part", cached)
    it["_sha256"], it["_size"] = sha256_file(cached), os.path.getsize(cached)
    place(cached, dst)


def copy_code(out):
    """Copy the repo's own files. Only git-tracked files, so stray local files (e.g. PDFs dropped into
    www/docs/pdf/, which .gitignore hides) can never end up in a published kit."""
    try:
        r = subprocess.run(["git", "-C", ROOT, "ls-files", "-z", "--", *KIT_CODE], capture_output=True, check=True)
        files = [f for f in r.stdout.decode().split("\0") if f]
    except (OSError, subprocess.CalledProcessError):
        files = []
    if files:
        for rel in files:
            dst = os.path.join(out, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(os.path.join(ROOT, rel), dst)
        return
    log("  (not a git checkout: copying repo folders as they are)")
    for rel in KIT_CODE:
        src = os.path.join(ROOT, rel)
        dst = os.path.join(out, rel)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True, ignore=lambda d, names: [n for n in names if n in SKIP_CODE])
        else:
            shutil.copy2(src, dst)


def build_index(out):
    if not shutil.which("pdftotext"):
        die("pdftotext is needed to build the manuals index (apt install poppler-utils)")
    subprocess.run([sys.executable, "-I", os.path.join(out, "tools", "build_index.py")], check=True)


# ---------------------------------------------------------------- outputs
def git_rev():
    try:
        rev = subprocess.run(["git", "-C", ROOT, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", ROOT, "status", "--porcelain"], capture_output=True, text=True).stdout.strip()
        ts = int(subprocess.run(["git", "-C", ROOT, "log", "-1", "--format=%ct"], capture_output=True, text=True).stdout.strip())
        return rev, bool(dirty), ts
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        return "unknown", True, int(time.time())


def walk(out):
    for d, dirs, files in os.walk(out):
        dirs.sort()
        for f in sorted(files):
            p = os.path.join(d, f)
            yield os.path.relpath(p, out).replace(os.sep, "/"), p


def write_info(out, tier, items, version, rev, dirty):
    redistributable = all(it.get("redistributable", True) for it in items)
    info = {
        "name": f"offline-toolkit-{tier}", "tier": tier, "version": version,
        "git_commit": rev, "git_dirty": dirty, "redistributable": redistributable,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "items": [{"id": it["id"], "version": it.get("version"), "licence": it.get("licence"),
                   "url": it.get("url") or f"zim:{it.get('source')}/{it.get('entry')}", "commit": it.get("commit"),
                   "dest": it["dest"], "sha256": it.get("_sha256"), "size": it.get("_size"),
                   "pinned": pinned(it) and it.get("pin") != "none", "redistributable": it.get("redistributable", True)}
                  for it in items],
    }
    with open(os.path.join(out, "KIT-INFO.json"), "w", encoding="utf-8") as fh:
        json.dump(info, fh, indent=1)
        fh.write("\n")
    return info


def write_sums(out):
    lines = [f"{sha256_file(p)}  {rel}" for rel, p in walk(out) if rel != "SHA256SUMS"]
    with open(os.path.join(out, "SHA256SUMS"), "w") as fh:
        fh.write("\n".join(lines) + "\n")
    log(f"wrote SHA256SUMS ({len(lines)} files)")


def write_zip(out, zpath, epoch, prefix):
    """Deterministic ZIP64 (stored: models/ZIMs/tiles are already compressed). Unix modes are kept so
    start.sh and the Linux binaries stay executable after `unzip`."""
    dt = time.gmtime(max(epoch, 315532800))[:6]  # zip cannot store dates before 1980
    tmp = zpath + ".part"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
        for rel, p in walk(out):
            zi = zipfile.ZipInfo(f"{prefix}/{rel}", date_time=dt)
            mode = 0o755 if os.stat(p).st_mode & 0o111 else 0o644
            zi.external_attr = (stat.S_IFREG | mode) << 16
            zi.create_system = 3
            zi.file_size = os.path.getsize(p)
            with open(p, "rb") as src, zf.open(zi, "w", force_zip64=zi.file_size > 0x7FFFFFFF) as dst:
                shutil.copyfileobj(src, dst, 1 << 22)
    os.replace(tmp, zpath)
    log(f"wrote {zpath} ({os.path.getsize(zpath) / 1e9:.2f} GB)")


def write_lock(path, lock):
    """Fill sha256/size into the manifest text in place (keeps its hand-written layout)."""
    text = open(path, encoding="utf-8").read()
    for iid, (h, size) in sorted(lock.items()):
        start = re.search(r'"id"\s*:\s*"%s"' % re.escape(iid), text).start()
        nxt = re.compile(r'"id"\s*:\s*"').search(text, start + 1)
        end = nxt.start() if nxt else len(text)
        block = text[start:end]
        block = re.sub(r'"sha256"\s*:\s*null', f'"sha256": "{h}"', block, count=1)
        block = re.sub(r'"size"\s*:\s*null', f'"size": {size}', block, count=1)
        block = re.sub(r'"verified_url"\s*:\s*false', '"verified_url": true', block, count=1)
        text = text[:start] + block + text[end:]
    got = {i["id"]: i.get("sha256") for i in json.loads(text)["items"]}  # must still parse
    missing = [i for i in lock if got.get(i) != lock[i][0]]
    if missing:
        die("could not write hashes for: " + ", ".join(missing) + " (each item needs a \"sha256\": null field)")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    log(f"pinned {len(lock)} item(s) in {os.path.relpath(path, ROOT)} - review `git diff` and commit")


# ---------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tier", choices=TIERS, default="small")
    ap.add_argument("--manifest", default=MANIFEST)
    ap.add_argument("--out", help="kit folder (default dist/offline-toolkit-<tier>)")
    ap.add_argument("--cache", help="download cache (default dist/cache)")
    ap.add_argument("--zip", nargs="?", const="auto", help="also write a zip (default dist/offline-toolkit-<tier>-<version>.zip)")
    ap.add_argument("--with", dest="optional", action="append", default=[], metavar="ID",
                    help="include an optional item (e.g. --with post-disaster; personal use only)")
    ap.add_argument("--with-post-disaster", dest="optional", action="append_const", const="post-disaster")
    ap.add_argument("--skip", action="append", default=[], metavar="ID", help="leave an item out")
    ap.add_argument("--only", action="append", default=[], metavar="ID", help="only fetch these items (with --lock)")
    ap.add_argument("--frozen", action="store_true", help="fail if any selected item is not pinned (CI)")
    ap.add_argument("--lock", action="store_true", help="download and write sha256/size of unpinned items into the manifest")
    ap.add_argument("--check", action="store_true", help="list the selected items and exit")
    ap.add_argument("--pmtiles", help="path to an existing pmtiles/go-pmtiles binary")
    ap.add_argument("--version", help="kit version label (default <yyyymmdd>-<commit>); [0-9A-Za-z._-]+")
    ap.add_argument("--no-index", action="store_true", help="skip rebuilding www/docs/index.js")
    a = ap.parse_args(argv)

    if a.version is not None and not re.fullmatch(r"[0-9A-Za-z._-]{1,64}", a.version):
        die(f"--version must match [0-9A-Za-z._-]+, got {a.version!r}")
    m = load_manifest(a.manifest)
    items = select(m, a.tier, a.optional)
    known = {it["id"] for it in m["items"]}
    for x in a.skip + a.only:
        if x not in known:
            die(f"unknown item {x!r}")
    items = [it for it in items if it["id"] not in a.skip and (not a.only or it["id"] in a.only)]
    unpinned = [it["id"] for it in items if not pinned(it)]

    if a.check:
        tot = 0
        for it in items:
            tot += it.get("size") or 0
            flag = ("pinned" if it.get("sha256") else "git-pinned" if pinned(it) else
                    "unpinnable" if it.get("pin") == "none" else "UNPINNED")
            url_ok = "" if it.get("verified_url", True) else "  (url not yet verified)"
            log(f"  {it['id']:<24} {flag:<10} {(it.get('size') or 0) / 1e9:6.2f} GB  -> {it['dest']}{url_ok}")
        log(f"{len(items)} items, {tot / 1e9:.1f} GB known, {len(unpinned)} unpinned")
        return 1 if (a.frozen and unpinned) else 0
    if a.frozen and unpinned:
        die("--frozen: these items have no sha256 yet: " + ", ".join(unpinned) + "\n  run `python3 tools/build_kit.py --lock` and commit the manifest")

    dist = os.path.join(ROOT, "dist")
    cache = os.path.abspath(a.cache or os.path.join(dist, "cache"))
    os.makedirs(cache, exist_ok=True)

    if a.lock:
        lock = {}
        for it in items:
            if it["kind"] in ("pmtiles", "git") or it.get("sha256"):
                continue
            log(f"[{it['id']}]")
            zim_extract(it, m, cache, lock) if it["kind"] == "zim-extract" else fetch(it, cache, lock)
        if lock:
            write_lock(a.manifest, lock)
        else:
            log("nothing to pin")
        return 0

    out = os.path.abspath(a.out or os.path.join(dist, f"offline-toolkit-{a.tier}"))
    if os.path.commonpath([out, ROOT]) == ROOT and os.path.commonpath([out, dist]) != dist:
        die("--out inside the repository must be under dist/")
    os.makedirs(out, exist_ok=True)
    log(f"building {a.tier} kit in {out}")
    copy_code(out)
    for it in items:
        log(f"[{it['id']}]")
        if it["kind"] == "pmtiles":
            build_map(it, m, out, cache, a.pmtiles)
            continue
        if it["kind"] == "git":
            git_checkout(it, out, cache)
            continue
        if it["kind"] == "zim-extract":
            place(zim_extract(it, m, cache, None), os.path.join(out, it["dest"]))
            continue
        path = fetch(it, cache, None)
        if it["kind"] == "file":
            place(path, os.path.join(out, it["dest"]), it.get("mode"))
        else:
            extract(it, path, out)
    if not a.no_index:
        build_index(out)

    rev, dirty, epoch = git_rev()
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", epoch))
    version = a.version or (time.strftime("%Y%m%d", time.gmtime(epoch)) + "-" + rev[:8] + ("-dirty" if dirty else ""))
    info = write_info(out, a.tier, items, version, rev, dirty)
    write_sums(out)
    if not info["redistributable"]:
        log("NOTE: this kit contains non-redistributable content (personal use only; publishing is blocked)")
    if a.zip:
        zpath = os.path.abspath(os.path.join(dist, f"{info['name']}-{version}.zip") if a.zip == "auto" else a.zip)
        os.makedirs(os.path.dirname(zpath), exist_ok=True)
        write_zip(out, zpath, epoch, info["name"])
    log("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
