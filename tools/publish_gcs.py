#!/usr/bin/env python3
"""Publish a built kit zip to Google Cloud Storage. Python stdlib only; no service-account keys.

  # In CI, after google-github-actions/auth (Workload Identity Federation) and setup-gcloud,
  # or inside Cloud Build (the build's service account), or on your own machine after `gcloud auth login`:
  python3 tools/publish_gcs.py --zip dist/offline-toolkit-small-<ver>.zip --bucket my-bucket

Layout written (everything under a version is create-only: an existing object is never overwritten):
  gs://BUCKET/kits/<tier>/<version>/offline-toolkit-<tier>-<version>.zip
  gs://BUCKET/kits/<tier>/<version>/offline-toolkit-<tier>-<version>.zip.sha256
  gs://BUCKET/kits/<tier>/<version>/KIT-INFO.json
  gs://BUCKET/kits/<tier>/latest.json      (updated last, only after everything else succeeded)

Refuses to publish a kit whose KIT-INFO.json (read from inside the zip) says redistributable=false,
is built from a dirty git tree, or has unpinned downloads.
"""
import argparse, base64, hashlib, json, os, re, subprocess, sys, time, urllib.error, urllib.parse, urllib.request, zipfile

GCS_UPLOAD = "https://storage.googleapis.com/upload/storage/v1/b/{}/o"
CHUNK = 64 * 1024 * 1024  # multiple of 256 KiB, as GCS requires
UA = "offline-toolkit-publish/1"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def die(msg):
    log("ERROR: " + msg)
    sys.exit(1)


def mask(secret):
    if os.environ.get("GITHUB_ACTIONS") == "true" and secret:
        print(f"::add-mask::{secret}", flush=True)


def http(method, url, body=None, headers=None, timeout=120):
    """Small HTTPS helper. Returns (status, headers, bytes). Never logs request headers (tokens)."""
    if urllib.parse.urlparse(url).scheme != "https":
        raise ValueError("https only")
    req = urllib.request.Request(url, data=body, method=method, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


# ---------------------------------------------------------------- auth
class Token:
    def __init__(self, fn):
        self.fn, self.value, self.at = fn, None, 0

    def get(self, force=False):
        if force or not self.value or time.time() - self.at > 45 * 60:  # tokens live 60 min
            self.value, self.at = self.fn(), time.time()
            mask(self.value)
        return self.value


def gcloud_token():
    try:
        return subprocess.run(["gcloud", "auth", "print-access-token"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as e:
        die(f"`gcloud auth print-access-token` failed ({e}); run `gcloud auth login` first")


# ---------------------------------------------------------------- upload
def md5_b64(path):
    h = hashlib.md5(usedforsecurity=False)
    sha = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 22), b""):
            h.update(b)
            sha.update(b)
    return base64.b64encode(h.digest()).decode(), sha.hexdigest()


def upload(token, bucket, name, path=None, data=None, ctype="application/octet-stream",
           cache_control=None, create_only=True, md5=None, meta=None):
    """Resumable upload. GCS checks the md5Hash we send and rejects the object if the bytes differ.
    create_only uses ifGenerationMatch=0 so an existing object is never replaced."""
    size = os.path.getsize(path) if path else len(data)
    if md5 is None:
        md5 = base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode() if data is not None else md5_b64(path)[0]
    q = {"uploadType": "resumable", "name": name}
    if create_only:
        q["ifGenerationMatch"] = "0"
    body = {"name": name, "contentType": ctype, "md5Hash": md5}
    if cache_control:
        body["cacheControl"] = cache_control
    if meta:
        body["metadata"] = meta
    for attempt in range(1, 6):
        st, hd, b = http("POST", GCS_UPLOAD.format(urllib.parse.quote(bucket, safe="")) + "?" + urllib.parse.urlencode(q),
                         json.dumps(body).encode(), {"Authorization": "Bearer " + token.get(), "Content-Type": "application/json; charset=UTF-8",
                                                     "X-Upload-Content-Type": ctype, "X-Upload-Content-Length": str(size)})
        if st == 200:
            session = hd["Location"]
            break
        if st == 412:
            die(f"gs://{bucket}/{name} already exists - versions are immutable; rebuild with a new commit")
        if st == 401:
            token.get(force=True)
        elif st < 500 and st != 429:
            die(f"starting upload of {name} failed: HTTP {st}: {b[:500].decode(errors='replace')}")
        time.sleep(2 ** attempt)
    else:
        die(f"could not start upload of {name}")

    def done(b):
        obj = json.loads(b)
        if obj.get("md5Hash") != md5:
            die(f"{name}: md5 mismatch after upload")
        log(f"  uploaded gs://{bucket}/{name} ({size / 1e6:,.1f} MB)")
        return obj

    offset, fails = 0, 0
    src = open(path, "rb") if path else None
    try:
        while True:
            if src:
                src.seek(offset)
                chunk = src.read(CHUNK)
            else:
                chunk = data[offset:offset + CHUNK]
            end = offset + len(chunk) - 1
            rng = f"bytes {offset}-{end}/{size}" if chunk else f"bytes */{size}"
            try:
                st, hd, b = http("PUT", session, chunk, {"Content-Range": rng, "Content-Length": str(len(chunk))}, timeout=600)
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                st, hd, b = 0, {}, str(e).encode()
            if st in (200, 201):
                return done(b)
            if st == 308:  # chunk accepted; Range says how much GCS has
                r = hd.get("Range")
                offset = int(r.split("-")[1]) + 1 if r else 0
                fails = 0
                if size > CHUNK:
                    log(f"    {offset / 1e6:,.0f} / {size / 1e6:,.0f} MB")
                continue
            if st == 412:
                die(f"gs://{bucket}/{name} already exists - versions are immutable")
            if st and st < 500 and st not in (408, 429):
                die(f"upload of {name} failed: HTTP {st}: {b[:500].decode(errors='replace')}")
            fails += 1
            if fails > 8:
                die(f"upload of {name} kept failing (last: {st} {b[:200]!r})")
            time.sleep(min(60, 2 ** fails))
            # ask GCS how much it has before resending
            try:
                st, hd, b = http("PUT", session, b"", {"Content-Range": f"bytes */{size}", "Content-Length": "0"})
            except (urllib.error.URLError, OSError, TimeoutError):
                continue  # keep the old offset and retry
            if st == 308:
                r = hd.get("Range")
                offset = int(r.split("-")[1]) + 1 if r else 0
            elif st in (200, 201):
                return done(b)  # it had finished after all
    finally:
        if src:
            src.close()


# ---------------------------------------------------------------- main
def read_info(zpath):
    with zipfile.ZipFile(zpath) as zf:
        names = [n for n in zf.namelist() if n.count("/") == 1 and n.endswith("/KIT-INFO.json")]
        if len(names) != 1:
            die("zip does not contain exactly one top-level <kit>/KIT-INFO.json")
        return json.loads(zf.read(names[0])), names[0]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--zip", required=True)
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--prefix", default="kits", help="object prefix (default: kits)")
    ap.add_argument("--auth", choices=["gcloud", "env"], default="gcloud",
                    help="gcloud = `gcloud auth print-access-token` (default); env = $GCS_ACCESS_TOKEN")
    ap.add_argument("--no-latest", action="store_true", help="do not update latest.json")
    ap.add_argument("--dry-run", action="store_true", help="check everything, upload nothing")
    a = ap.parse_args(argv)

    if not os.path.isfile(a.zip):
        die(f"{a.zip} not found")
    info, info_name = read_info(a.zip)
    if info.get("redistributable") is not True:
        die("KIT-INFO.json says this kit is NOT redistributable (e.g. it includes the post-disaster ZIM). Refusing to publish.")
    if info.get("git_dirty"):
        die("kit was built from a dirty git tree; publish only from a clean commit")
    unpinned = [i["id"] for i in info.get("items", []) if not i.get("pinned") and not i["dest"].endswith(".pmtiles")]
    if unpinned:
        die("kit contains unpinned downloads: " + ", ".join(unpinned))
    name, version, tier = info["name"], info["version"], info.get("tier")
    if tier not in ("small", "medium", "large") or name != f"offline-toolkit-{tier}":
        die(f"unexpected kit name/tier in KIT-INFO.json: {name!r} / {tier!r}")
    if not re.fullmatch(r"[0-9A-Za-z._-]{1,64}", version or "") or version.startswith("."):
        die(f"unsafe version {version!r}")
    if not re.fullmatch(r"[0-9A-Za-z_-]+(/[0-9A-Za-z_-]+)*", a.prefix.strip("/")):
        die(f"unsafe prefix {a.prefix!r}")

    log(f"hashing {a.zip} ...")
    md5, sha = md5_b64(a.zip)
    base = f"{a.prefix.strip('/')}/{tier}"
    zname = f"{name}-{version}.zip"
    vdir = f"{base}/{version}"
    zsize = os.path.getsize(a.zip)
    log(f"kit {name} {version}: {zsize / 1e9:.2f} GB sha256 {sha}")
    log(f"target gs://{a.bucket}/{vdir}/")
    if a.dry_run:
        log("dry run: nothing uploaded")
        return 0

    if a.auth == "gcloud":
        token = Token(gcloud_token)
    else:
        if not os.environ.get("GCS_ACCESS_TOKEN"):
            die("GCS_ACCESS_TOKEN is empty")
        token = Token(lambda: os.environ["GCS_ACCESS_TOKEN"])

    with zipfile.ZipFile(a.zip) as zf:
        info_bytes = zf.read(info_name)
    meta = {"sha256": sha, "git_commit": info.get("git_commit", "")}
    upload(token, a.bucket, f"{vdir}/{zname}", path=a.zip, ctype="application/zip", md5=md5, meta=meta)
    upload(token, a.bucket, f"{vdir}/{zname}.sha256", data=f"{sha}  {zname}\n".encode(), ctype="text/plain")
    upload(token, a.bucket, f"{vdir}/KIT-INFO.json", data=info_bytes, ctype="application/json")
    if not a.no_latest:
        latest = {"name": name, "tier": tier, "version": version, "zip": f"{version}/{zname}", "sha256": sha, "size": zsize,
                  "git_commit": info.get("git_commit"), "published_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        upload(token, a.bucket, f"{base}/latest.json", data=(json.dumps(latest, indent=1) + "\n").encode(),
               ctype="application/json", cache_control="no-cache, max-age=0", create_only=False)
    log(f"published https://storage.googleapis.com/{a.bucket}/{vdir}/{zname}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
