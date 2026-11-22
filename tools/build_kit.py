#!/usr/bin/env python3
"""Assemble a ready-to-copy Offline Survival Kit from tools/kit-manifest.json.

Run on an internet-connected Linux/macOS box (python3 stdlib only; plus pdftotext for the manuals index
and Go, or a `pmtiles` binary, for the map). Examples:

  python3 tools/build_kit.py --check                 # show what would be downloaded / what is unpinned
  python3 tools/build_kit.py                         # small tier -> dist/offline-toolkit-small/
  python3 tools/build_kit.py --tier medium --zip     # + dist/offline-toolkit-medium-<ver>.zip
  python3 tools/build_kit.py --lock                  # download, then write sha256/size into the manifest
  python3 tools/build_kit.py --frozen --zip          # CI: refuse anything not pinned
  python3 tools/build_kit.py --tier medium --fetch-only --jobs 4   # only fill the download cache

Security model: every download lands in a cache, is hashed, and is only used if it matches the pinned
sha256; the pin is what guarantees integrity. The Python downloader (and every metalink / redirect lookup)
is HTTPS-only, redirects included. aria2c cannot refuse a redirect, so it is only given direct https://
URLs (Kiwix metalink mirrors, or a redirect target already resolved over HTTPS); if a server still
redirected it elsewhere, the pin check catches any changed byte and the HTTPS-only fallback runs. Nothing downloaded is executed, except go-pmtiles,
which is built from source by `go install` (verified by the Go checksum database). Archives are
extracted member-by-member with path checks (no absolute paths, no '..', no links). The map is only ever
extracted from https://build.protomaps.com/<YYYYMMDD>.pmtiles: the manifest's build or, once Protomaps
has deleted it, the newest build its builds.json still lists (see map_sources).

Speed: big files are fetched with aria2c (several connections) when it is installed, and download.kiwix.org
ZIMs also from the HTTPS mirrors in Kiwix's official metalink (<url>.meta4) whose size and sha-256 match
the pin. If that fails or the bytes don't match the pin, it falls back to one plain HTTPS stream from the
manifest URL. Several items download at once (--jobs). Each download logs its source, size, time and MB/s.
Cloud Build also keeps a shared cache of pinned files in GCS (tools/gcs_cache.sh, --cache-plan); anything
that comes from a cache is checked against the pin exactly like a fresh download.
"""
import argparse, concurrent.futures, fnmatch, hashlib, json, os, re, shutil, stat, subprocess, sys, tarfile, threading, time
import urllib.error, urllib.parse, urllib.request, zipfile
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(ROOT, "tools", "kit-manifest.json")
TIERS = ["small", "medium", "large"]
UA = "offline-toolkit-build/1"
# Repo files copied into every kit (paths relative to ROOT). Generated/large content comes from the manifest.
KIT_CODE = ["start.sh", "start.bat", "stop.sh", "stop.bat", "README.md", "LICENSE", "tools", "www"]
SKIP_CODE = {"__pycache__", ".DS_Store"}
ARIA2_MIN = 64 * 1000 * 1000   # use aria2c for files at least this big (or of unknown size)
MAX_MIRRORS = 8
CACHE_PLAN_MIN = 32 * 1000 * 1000  # --cache-plan: only share files at least this big through GCS
KIWIX_HOST = "download.kiwix.org"
PROTOMAPS_HOST = "build.protomaps.com"  # map extracts only ever come from here, over HTTPS
PROTOMAPS_BUILDS = "https://build-metadata.protomaps.dev/builds.json"  # Protomaps' list of daily builds it still serves
MAP_FALLBACKS = 5  # newest listed builds to try (about a week of dailies) when the manifest's build is gone
METALINK_NS = "{urn:ietf:params:xml:ns:metalink}"


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
        if it["kind"] == "pmtiles":
            if it.get("build", "manifest") not in ("manifest", "latest"):
                die(f"{it['id']}: \"build\" must be \"manifest\" or \"latest\", got {it['build']!r}")
            if urllib.parse.urlparse(it["url"]).scheme == "https" and not protomaps_build(it["url"]):
                die(f"{it['id']}: map url must be https://{PROTOMAPS_HOST}/<YYYYMMDD>.pmtiles, got {it['url']!r}")
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


class Stamps:
    """sha256 of files already hashed, so a multi-GB file is read once per build rather than once per step.
    Kept in memory (by device+inode, so hard links in the kit reuse it) and, for files in the cache
    folder, in <cache>/.verified as "<sha256>  <name>  <size> <mtime_ns> <inode>". A record only counts
    while the file's size, mtime and inode are unchanged. tools/gcs_cache.sh push reads this file to share
    only verified downloads."""

    def __init__(self, cache=None):
        self.dir = os.path.abspath(cache) if cache else None
        self.path = os.path.join(self.dir, ".verified") if cache else None
        self.mem = {}
        self.lock = threading.Lock()
        if not self.path or not os.path.exists(self.path):
            return
        with open(self.path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                p = line.split()
                if len(p) != 5 or not re.fullmatch(r"[0-9a-f]{64}", p[0]) or not re.fullmatch(r"[A-Za-z0-9._-]+", p[1]):
                    continue
                try:
                    st = os.stat(os.path.join(self.dir, p[1]))
                    if (st.st_size, st.st_mtime_ns, st.st_ino) == tuple(int(x) for x in p[2:]):
                        self.mem[self.key(st)] = p[0]
                except (OSError, ValueError):
                    pass

    @staticmethod
    def key(st):
        return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)

    def known(self, path):
        return self.mem.get(self.key(os.stat(path)))

    def sha(self, path):
        st = os.stat(path)
        h = self.mem.get(self.key(st))
        if h:
            return h
        h = sha256_file(path)
        if self.key(os.stat(path)) != self.key(st):
            return h  # changed while we read it: don't remember
        with self.lock:
            self.mem[self.key(st)] = h
            if self.path and os.path.dirname(os.path.abspath(path)) == self.dir:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(f"{h}  {os.path.basename(path)}  {st.st_size} {st.st_mtime_ns} {st.st_ino}\n")
        return h


STAMPS = Stamps()
PROCS = set()  # running aria2c processes, killed if another download fails


class Dl:
    """Download settings (set from the command line in main)."""
    aria2c = shutil.which("aria2c")
    mirrors = True
    jobs = 1


def fmt_dur(sec):
    sec = int(round(sec))
    return f"{sec // 3600}h{sec // 60 % 60:02d}m{sec % 60:02d}s" if sec >= 3600 else f"{sec // 60}m{sec % 60:02d}s"


def rate(nbytes, sec):
    return f"{nbytes / 1e6:,.0f} MB in {fmt_dur(sec)} ({nbytes / 1e6 / max(sec, 0.001):,.1f} MB/s)"


def download(url, dest, retries=5, tag=""):
    """Download url to dest in one HTTPS stream (resuming dest+'.part'). The caller verifies the hash."""
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
                    done, last, mark = have, time.time(), have
                    for b in iter(lambda: r.read(1 << 20), b""):
                        fh.write(b)
                        done += len(b)
                        if time.time() - last > 30:
                            speed = (done - mark) / 1e6 / (time.time() - last)
                            last, mark = time.time(), done
                            log(f"    {tag} {done / 1e6:,.0f} MB" + (f" / {total / 1e6:,.0f} MB" if total else "") + f" ({speed:,.1f} MB/s)")
            os.replace(part, dest)
            return
        except urllib.error.HTTPError as e:
            if e.code == 416 and have:  # range not satisfiable: .part is complete or bad; restart
                os.remove(part)
            elif e.code in (404, 410, 401, 403):
                die(f"download failed ({e.code}) for {url}")
            log(f"    {tag} attempt {attempt}: HTTP {e.code}")
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            log(f"    {tag} attempt {attempt}: {e}")
        time.sleep(min(60, 2 ** attempt))
    die(f"giving up on {url}")


def parse_meta4(data, it, fname):
    """HTTPS mirror URLs for fname from a Kiwix metalink (RFC 5854), best priority first. Returns [] if the
    metalink's size or sha-256 disagrees with the pin, so a changed upstream file is never mirrored."""
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise ValueError("metalink has a DOCTYPE/ENTITY")
    files = [f for f in ET.fromstring(data).iter(METALINK_NS + "file") if f.get("name") == fname]
    if len(files) != 1:
        raise ValueError(f"metalink has {len(files)} entries for {fname}")
    f = files[0]
    size = (f.findtext(METALINK_NS + "size") or "").strip()
    if size and it.get("size") and size != str(it["size"]):
        raise ValueError(f"metalink size {size} != pinned {it['size']}")
    for h in f.findall(METALINK_NS + "hash"):
        if h.get("type") == "sha-256" and it.get("sha256") and (h.text or "").strip().lower() != it["sha256"]:
            raise ValueError("metalink sha-256 differs from the pin")
    urls = []
    for u in f.findall(METALINK_NS + "url"):
        url = (u.text or "").strip()
        p = urllib.parse.urlparse(url)
        if p.scheme != "https" or not p.hostname or os.path.basename(p.path) != fname or len(url) > 1000 \
                or re.search(r"[\s\x00-\x1f]", url):
            continue
        try:
            prio = int(u.get("priority") or 999999)
        except ValueError:
            prio = 999999
        urls.append((prio, len(urls), url))
    return [u for _, _, u in sorted(urls)]


def https_target(url):
    """Follow url's redirects with the HTTPS-only opener (HEAD) and return where it ends up."""
    with OPENER.open(urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA}), timeout=30) as r:
        final = r.geturl()
    if urllib.parse.urlparse(final).scheme != "https":
        raise urllib.error.URLError(f"redirected to non-https {final}")
    return final


def aria2_sources(it, url):
    """Direct HTTPS URLs to hand aria2c, or [] to skip it. aria2c can't refuse a redirect to plain http, so
    it never gets a URL that redirects: for download.kiwix.org files the HTTPS mirrors from Kiwix's
    official metalink (size and sha-256 must match the pin), otherwise the end of url's redirect chain,
    resolved with the HTTPS-only opener. A signed redirect target that expires mid-download just makes
    aria2c fail, and the single-stream fallback (from the manifest URL, HTTPS-only) takes over."""
    p = urllib.parse.urlparse(url)
    if Dl.mirrors and p.hostname == KIWIX_HOST:
        try:
            with OPENER.open(urllib.request.Request(url + ".meta4", headers={"User-Agent": UA}), timeout=30) as r:
                data = r.read(2 << 20)
            found = [u for u in dict.fromkeys(parse_meta4(data, it, os.path.basename(p.path)))
                     if urllib.parse.urlparse(u).hostname != KIWIX_HOST]  # that one redirects
            if found:
                return found[:MAX_MIRRORS]
            log(f"  [{it['id']}] metalink lists no HTTPS mirrors")
        except Exception as e:  # any problem: fall through to the redirect target
            log(f"  [{it['id']}] no mirrors ({type(e).__name__}: {e})")
    try:
        return [https_target(url)]
    except Exception as e:
        log(f"  [{it['id']}] could not resolve {url} over HTTPS ({type(e).__name__}: {e}); not using aria2c")
        return []


def aria2c(urls, path, tag):
    """Fetch one file from all urls (the same file on several servers) with many connections.
    Returns True on success; the caller still checks the sha256."""
    tmp = path + ".dl"
    cmd = [Dl.aria2c, "--dir", os.path.dirname(path), "--out", os.path.basename(tmp),
           "--continue=true", "--auto-file-renaming=false", "--allow-overwrite=true", "--file-allocation=none",
           "--split=16", "--max-connection-per-server=8", "--min-split-size=32M", "--uri-selector=adaptive",
           "--max-tries=8", "--retry-wait=10", "--connect-timeout=30", "--timeout=120",
           "--follow-metalink=false", "--follow-torrent=false", "--remote-time=false",
           "--summary-interval=60", "--console-log-level=warn", "--download-result=hide",
           "--show-console-readout=false", "--user-agent=" + UA, "--", *urls]
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
    except OSError as e:
        log(f"  [{tag}] aria2c failed to start: {e}")
        return False
    PROCS.add(p)
    try:
        rc = p.wait()
    finally:
        PROCS.discard(p)
    if rc == 0 and os.path.isfile(tmp):
        os.replace(tmp, path)
        return True
    log(f"  [{tag}] aria2c exited with {rc}")
    for f in (tmp, tmp + ".aria2"):
        if os.path.exists(f):
            os.remove(f)
    return False


def obtain(it, url, path):
    """Download url to path: aria2c (with mirrors) if available, else / on failure one HTTPS stream."""
    tag, pin = it["id"], it.get("sha256")
    t0, how = time.time(), None
    if Dl.aria2c and (it.get("size") or ARIA2_MIN) >= ARIA2_MIN:
        urls = aria2_sources(it, url)
        if urls:
            log(f"  [{tag}] downloading with aria2c from {len(urls)} source(s): " + ", ".join(urllib.parse.urlparse(u).hostname for u in urls))
        if urls and aria2c(urls, path, tag):
            if pin and STAMPS.sha(path) != pin:
                log(f"  [{tag}] aria2c result does not match the pinned sha256; discarding it")
                os.replace(path, path + ".bad")
            else:
                how = f"aria2c, {len(urls)} source(s)"
        if not how:
            log(f"  [{tag}] falling back to a single HTTPS stream from {url}")
    if not how:
        log(f"  [{tag}] downloading {url}")
        download(url, path, tag=f"[{tag}]")
        how = "https, 1 stream"
    sec, n = time.time() - t0, os.path.getsize(path)
    it.setdefault("_dl", {"how": how, "bytes": n, "seconds": round(sec, 1)})
    log(f"  [{tag}] downloaded {rate(n, sec)} via {how}")


def cache_name(it):
    url = it["url"]
    key = it.get("sha256") or hashlib.sha256(url.encode()).hexdigest()
    name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(urllib.parse.urlparse(url).path) or it["id"])[-80:]
    return f"{key[:16]}-{name}"


def gcs_pulled(cache):
    """Names tools/gcs_cache.sh pull fetched from the shared GCS cache (for the logs only)."""
    try:
        with open(os.path.join(cache, ".gcs-pulled"), encoding="utf-8") as fh:
            return {line.strip() for line in fh}
    except OSError:
        return set()


def fetch(it, cache, lock):
    """Return path of a verified local copy of the item's URL."""
    url = it["url"]
    if urllib.parse.urlparse(url).scheme == "file":
        src = urllib.request.url2pathname(urllib.parse.urlparse(url).path)
        path = src
    else:
        path = os.path.join(cache, cache_name(it))
        if os.path.exists(path):
            how = "GCS cache" if os.path.basename(path) in gcs_pulled(cache) else "local cache"
            if it.get("sha256") and STAMPS.sha(path) != it["sha256"]:
                log(f"  [{it['id']}] {how} copy does not match the pinned sha256; moved aside, downloading again")
                os.replace(path, path + ".bad")
            else:
                it.setdefault("_dl", {"how": how, "bytes": os.path.getsize(path), "seconds": 0})
        if not os.path.exists(path):
            obtain(it, url, path)
    return verify(it, path, cache, lock)


def prefetch(items, cache, lock):
    """Download every plain download item, Dl.jobs at a time. Any failure stops the build."""
    todo = [it for it in items if it["kind"] in ("file", "tar", "zip")]
    if Dl.jobs <= 1 or len(todo) <= 1:
        for it in todo:
            fetch(it, cache, lock)
        return
    log(f"downloading {len(todo)} item(s), {Dl.jobs} at a time")
    ex = concurrent.futures.ThreadPoolExecutor(Dl.jobs)
    futs = {ex.submit(fetch, it, cache, lock): it for it in todo}
    for f in concurrent.futures.as_completed(futs):
        try:
            f.result()
        except BaseException as e:  # die() raises SystemExit in the worker
            if not isinstance(e, SystemExit):
                log(f"ERROR: {futs[f]['id']}: {type(e).__name__}: {e}")
            for p in list(PROCS):
                p.terminate()
            sys.stderr.flush()
            os._exit(1)  # don't wait hours for the other downloads to finish
    ex.shutdown()


def summary(items):
    rows = [(it["id"], it["_dl"]) for it in items if it.get("_dl")]
    if not rows:
        return
    log("download summary (source, size, time, speed):")
    tot_b = tot_s = 0
    for iid, d in rows:
        s = f"{d['bytes'] / 1e6:>10,.0f} MB"
        if d["seconds"]:
            s += f"  {fmt_dur(d['seconds']):>10}  {d['bytes'] / 1e6 / max(d['seconds'], 0.001):8,.1f} MB/s"
            tot_b += d["bytes"]; tot_s += d["seconds"]
        log(f"  {iid:<24} {d['how']:<22} {s}")
    if tot_s:
        log(f"  downloaded {tot_b / 1e9:,.1f} GB, {fmt_dur(tot_s)} summed over items")


def cache_plan(items, path, cache=None):
    """Write the objects tools/gcs_cache.sh may share for these items: "<object> <cache file> <sha256|->".
    Pinned downloads are stored under cache/sha256/<sha256>; map extracts (unpinnable) under
    cache/pmtiles/<cache file>, with their hash in a .sha256 object next to them. The map's build is
    resolved now (network), so the object is the extract of the build the fetch step will use."""
    lines = []
    for it in items:
        if it["kind"] in ("file", "tar", "zip") and it.get("sha256") and (it.get("size") or 0) >= CACHE_PLAN_MIN \
                and urllib.parse.urlparse(it["url"]).scheme == "https":
            lines.append(f"cache/sha256/{it['sha256']} {cache_name(it)} {it['sha256']}")
        elif it["kind"] == "pmtiles":
            url = resolve_map(it, cache)  # the build the fetch step will use (normally the same one)
            if not url:
                log(f"  WARNING {it['id']}: no Protomaps build available right now; not in the cache plan")
                continue
            name = map_cache_name(it, url)
            lines.append(f"cache/pmtiles/{name} {name} -")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("".join(x + "\n" for x in lines))
    log(f"wrote {len(lines)} cache entr{'y' if len(lines) == 1 else 'ies'} to {path}")


def verify(it, path, cache, lock):
    url = it.get("url") or f"{it.get('source')}:{it.get('entry')}"
    got = STAMPS.sha(path)
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


# Protomaps publishes a planet build every day and deletes old ones after about a week, so a dated URL in
# the manifest stops working on its own. The map item names a preferred build; when that one is gone (or
# the item says "build": "latest") the newest builds Protomaps still lists are used instead. The extract
# is map data, not code, and is hashed into SHA256SUMS; KIT-INFO.json records the build actually used.
def protomaps_build(url):
    """'20261121' for https://build.protomaps.com/20261121.pmtiles; None for any other URL."""
    p = urllib.parse.urlparse(url)
    m = re.fullmatch(r"/(\d{8})\.pmtiles", p.path)
    if p.scheme != "https" or p.netloc != PROTOMAPS_HOST or p.query or p.fragment or not m:
        return None  # netloc, not hostname: no port, user info or other casing slips through
    return m.group(1)


def protomaps_url(build):
    return f"https://{PROTOMAPS_HOST}/{build}.pmtiles"


def parse_builds(data):
    """Build dates (YYYYMMDD) listed in Protomaps' builds.json, newest first. Only the date is taken from
    each entry ({"key": "20261121.pmtiles", ...}); the URL is always rebuilt on build.protomaps.com."""
    doc = json.loads(data)
    if isinstance(doc, dict):
        doc = doc.get("builds")
    if not isinstance(doc, list):
        raise ValueError("builds.json is not a list of builds")
    dates = set()
    for e in doc:
        key = e.get("key") if isinstance(e, dict) else e
        m = re.fullmatch(r"(\d{8})(?:\.pmtiles)?", key.strip()) if isinstance(key, str) else None
        if m:
            dates.add(m.group(1))
    if not dates:
        raise ValueError("builds.json lists no builds")
    return sorted(dates, reverse=True)


def list_builds():
    with OPENER.open(urllib.request.Request(PROTOMAPS_BUILDS, headers={"User-Agent": UA}), timeout=30) as r:
        return parse_builds(r.read(4 << 20))


def probe_map(url):
    """None if url serves a PMTiles archive (checks its 7-byte magic), else why not, e.g. 'HTTP 404'."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": "bytes=0-6"})
    try:
        with OPENER.open(req, timeout=30) as r:
            head = r.read(7)
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}"
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as e:
        return f"{type(e).__name__}: {e}"
    return None if head == b"PMTiles" else "not a PMTiles archive"


def map_cache_name(it, url):
    """Cache file of the extract of the build at url: keyed on the build actually used, so an extract of
    one build is never reused under another's name."""
    key = hashlib.sha256(f"{url}|{it['bbox']}|{it['maxzoom']}".encode()).hexdigest()[:16]
    return f"{key}-{os.path.basename(it['dest'])}"


def map_sources(it, cache):
    """URLs of the Protomaps builds to try, in order (lazily: builds.json is only read if needed). First the
    manifest URL, unless "build": "latest". Then from builds.json: a still-listed build whose extract is
    already in the cache (so --fetch-only and the build after it, or a GCS cache hit, agree), then the
    newest MAP_FALLBACKS listed builds, newest first."""
    first = None if it.get("build") == "latest" else it["url"]
    if first:
        yield first
    try:
        builds = list_builds()
    except Exception as e:
        log(f"  [{it['id']}] could not read the list of Protomaps builds ({type(e).__name__}: {e})")
        return
    urls = [u for u in map(protomaps_url, builds) if u != first]
    cached = [u for u in urls if cache and os.path.exists(os.path.join(cache, map_cache_name(it, u)))]
    yield from cached[:1] + [u for u in urls if u not in cached[:1]][:MAP_FALLBACKS]


def resolve_map(it, cache=None):
    """The URL map_cached would use (cached, or answering as a PMTiles archive), or None."""
    for url in map_sources(it, cache):
        if (cache and os.path.exists(os.path.join(cache, map_cache_name(it, url)))) or not probe_map(url):
            return url
        log(f"  [{it['id']}] {url} is not available")
    return None


def note_map(it, url):
    req = None if it.get("build") == "latest" else it["url"]
    build = protomaps_build(url)
    it["_map"] = {"url": url, "protomaps_build": build, "requested_url": req, "fallback": url != req}
    if req and url != req:
        log(f"  [{it['id']}] WARNING the manifest's Protomaps build is no longer available; used build {build} "
            f"instead. Consider updating the map url in tools/kit-manifest.json.")
    log(f"  [{it['id']}] map from Protomaps build {build or url}")


def map_cached(it, m, cache, tool):
    failed = []
    for url in map_sources(it, cache):
        cached = os.path.join(cache, map_cache_name(it, url))
        if os.path.exists(cached):
            how = "GCS cache" if os.path.basename(cached) in gcs_pulled(cache) else "local cache"
            it.setdefault("_dl", {"how": how, "bytes": os.path.getsize(cached), "seconds": 0})
        else:
            why = probe_map(url)
            if why:
                log(f"  [{it['id']}] {url}: {why}; trying the next Protomaps build")
                failed.append(f"{url}: {why}")
                continue
            exe = pmtiles_tool(m, cache, tool)
            log(f"  extracting map {it['bbox']} z0-{it['maxzoom']} from {url} (~GBs, takes a while)")
            t0 = time.time()
            try:
                subprocess.run([exe, "extract", url, cached + ".part", f"--bbox={it['bbox']}",
                                f"--maxzoom={it['maxzoom']}", "--download-threads=8"], check=True)
            except subprocess.CalledProcessError as e:
                log(f"  [{it['id']}] extract from {url} failed (exit {e.returncode}); trying the next Protomaps build")
                failed.append(f"{url}: extract exit {e.returncode}")
                if os.path.exists(cached + ".part"):
                    os.remove(cached + ".part")
                continue
            os.replace(cached + ".part", cached)
            sec, n = time.time() - t0, os.path.getsize(cached)
            it.setdefault("_dl", {"how": "go-pmtiles extract", "bytes": n, "seconds": round(sec, 1)})
            log(f"  [{it['id']}] extracted {rate(n, sec)}")
        note_map(it, url)
        it["_sha256"], it["_size"] = STAMPS.sha(cached), os.path.getsize(cached)
        return cached
    die(f"{it['id']}: no Protomaps build could be used" + ("".join("\n  " + f for f in failed) or
        f" (and {PROTOMAPS_BUILDS} could not be read)"))


def build_map(it, m, out, cache, tool):
    dst = os.path.join(out, it["dest"])
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    place(map_cached(it, m, cache, tool), dst)


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
                   "pinned": pinned(it) and it.get("pin") != "none", "redistributable": it.get("redistributable", True),
                   **it.get("_map", {})}  # map: the Protomaps build actually used (its url replaces the manifest's)
                  for it in items],
    }
    with open(os.path.join(out, "KIT-INFO.json"), "w", encoding="utf-8") as fh:
        json.dump(info, fh, indent=1)
        fh.write("\n")
    return info


def write_sums(out):
    # Files hard-linked from the cache were hashed when they were verified; only hash the rest.
    lines = [f"{STAMPS.known(p) or sha256_file(p)}  {rel}" for rel, p in walk(out) if rel != "SHA256SUMS"]
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
    ap.add_argument("--jobs", type=int, default=3, metavar="N", help="download N items at once (default 3)")
    ap.add_argument("--no-aria2c", action="store_true", help="never use aria2c, even if it is installed")
    ap.add_argument("--no-mirrors", action="store_true", help="only use the manifest URL (no Kiwix metalink mirrors)")
    ap.add_argument("--fetch-only", action="store_true", help="only fill the cache (downloads and map extract); build no kit")
    ap.add_argument("--cache-plan", metavar="FILE", help="write the GCS cache plan for the selected items (no downloads) and exit")
    a = ap.parse_args(argv)
    Dl.jobs = max(1, min(a.jobs, 16))
    Dl.aria2c = None if a.no_aria2c else shutil.which("aria2c")
    Dl.mirrors = not a.no_mirrors

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
    if a.cache_plan:
        cache_plan(items, a.cache_plan, os.path.abspath(a.cache) if a.cache else None)
        return 0

    dist = os.path.join(ROOT, "dist")
    cache = os.path.abspath(a.cache or os.path.join(dist, "cache"))
    os.makedirs(cache, exist_ok=True)
    global STAMPS
    STAMPS = Stamps(cache)
    log(f"downloader: {'aria2c ' + Dl.aria2c if Dl.aria2c else 'python (single stream)'}, {Dl.jobs} job(s), "
        f"Kiwix mirrors {'on' if Dl.mirrors and Dl.aria2c else 'off'}")

    if a.fetch_only:
        t0 = time.time()
        prefetch(items, cache, None)
        for it in items:
            if it["kind"] == "pmtiles":
                log(f"[{it['id']}]")
                map_cached(it, m, cache, a.pmtiles)
        summary(items)
        log(f"cache filled in {fmt_dur(time.time() - t0)}")
        return 0

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
    t0 = time.time()
    prefetch(items, cache, None)
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
    summary(items)
    log(f"downloads and assembly took {fmt_dur(time.time() - t0)}")
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
