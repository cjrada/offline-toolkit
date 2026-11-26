#!/usr/bin/env python3
"""Publish a built kit zip to Cloudflare R2 (S3 API, SigV4). Python stdlib only.

  # credentials come from the environment only (never flags, so they are not in `ps` or Cloud Build logs):
  #   R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_ACCOUNT_ID  (secrets)   R2_BUCKET  (not secret)
  python3 tools/publish_r2.py --zip dist/offline-toolkit-small-<ver>.zip            # publish
  python3 tools/publish_r2.py --zip dist/...zip --dry-run                           # read-only: plan, no writes
  python3 tools/publish_r2.py --zip dist/...zip --dry-run --offline                 # no network, no credentials
  python3 tools/publish_r2.py --tier large --version V --dry-run                    # preflight: credentials,
                                                                                    # bucket access, V is new

Layout (a dedicated bucket, so there is no kits/ prefix; keys are the same as the public URL paths):
  <tier>/<version>/offline-toolkit-<tier>-<version>.zip          create-only
  <tier>/<version>/offline-toolkit-<tier>-<version>.zip.sha256   create-only
  <tier>/<version>/KIT-INFO.json                                 create-only
  <tier>/latest.json      the newest version of this tier         (rewritten after a verified publish)
  <tier>/index.json       retained versions of this tier, newest first
  latest.json             the newest version of each tier
  index.json              retained versions of every tier, newest first

Order of a publish (each step only after the previous one succeeded):
  1. refuse a kit that is not redistributable, is from a dirty tree, has unpinned downloads or unsafe names;
     refuse if anything already exists under <tier>/<version>/ (versions are immutable)
  2. multipart-upload the zip (64 MiB+ parts, parallel, each part retried; aborted on failure, so no partial
     object is left). CompleteMultipartUpload is create-only too: a HEAD just before, and If-None-Match: * on
     the request (if the store says it does not implement that header, the HEAD alone stands, with a warning).
     Then the .sha256 sidecar and KIT-INFO.json (If-None-Match: *)
  3. verify: HeadObject Content-Length == local size and x-amz-meta-sha256 == local sha256 (a multipart ETag
     is NOT an MD5 and is not used); the sidecar and KIT-INFO.json read back byte for byte. Each part was
     checked by R2 against the SHA-256 in its SigV4 signature and its Content-MD5.
  4. write <tier>/index.json, <tier>/latest.json, index.json, latest.json, then read them back and check they
     name this version with this sha256 and size
  5. retention, planned from a fresh read of <tier>/index.json taken after verification (so a version another
     publish indexed during a long upload is kept or retired, not dropped): only now delete versions of this tier beyond the newest N (the new one always counts and is
     never deleted). Defaults below; change DEFAULT_KEEP or pass --keep tier=N / KIT_KEEP="small=3,...".
A dry run does steps 1 and the read-only parts of the plan and writes nothing.
"""
import argparse, base64, hashlib, hmac, json, os, re, sys, threading, time, urllib.error, urllib.parse, urllib.request, zipfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed

DEFAULT_KEEP = {"small": 3, "medium": 2, "large": 1}  # versions kept per tier, including the one just published
TIERS = ("small", "medium", "large")
PART = 64 * 1024 * 1024          # minimum part size; raised so a kit never needs more than MAX_PARTS parts
MAX_PARTS = 9000                 # S3 allows 10,000
UA = "offline-toolkit-publish/1"
SAFE_VERSION = re.compile(r"[0-9A-Za-z_-][0-9A-Za-z._-]{0,63}")
BUCKET_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]")
ACCOUNT_RE = re.compile(r"[0-9a-f]{32}")
PUBLIC_URL_RE = re.compile(r"https://[A-Za-z0-9.-]+(/[A-Za-z0-9._~-]+)*")

GITHUB_HELP = """Set the repository variables in GitHub: Settings -> Secrets and variables -> Actions -> Variables:
  R2_BUCKET = the R2 bucket name
  R2_PUBLIC_URL = the public base URL (r2.dev or custom domain), no trailing slash"""
SECRETS_HELP_GITHUB = ("Set R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY and R2_ACCOUNT_ID as secrets of the gcs-publish "
                       "environment (Settings -> Environments -> gcs-publish -> Environment secrets), so they are "
                       "released only after that approval on main or a v* tag.")
SECRETS_HELP_GCP = ("Cloud Build reads them from Secret Manager: add a version to each of r2-access-key-id, "
                    "r2-secret-access-key and r2-account-id, e.g. `gcloud secrets versions add r2-secret-access-key "
                    "--data-file=-` and paste the value on stdin (docs/SETUP.md, scripts/setup-gcp.sh).")


class Fail(Exception):
    pass


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def on_github():
    return os.environ.get("GITHUB_ACTIONS") == "true"


def mask(secret):
    # GitHub masks its secrets already; this also covers values that reach us some other way.
    if on_github() and secret:
        print(f"::add-mask::{secret}", flush=True)


def warn(msg):
    log(("::warning::" if on_github() else "WARNING: ") + msg)


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------- config
def config_problems(env, need_public_url=False, need_secrets=True):
    """Return a list of human-readable problems with the R2 configuration in env (no values are echoed)."""
    p = []
    bucket = env.get("R2_BUCKET", "")
    if not bucket:
        p.append("R2_BUCKET is not set.")
    elif not BUCKET_RE.fullmatch(bucket):
        p.append("R2_BUCKET is not a valid R2 bucket name (3-63 lowercase letters, digits, hyphens).")
    if need_public_url:
        url = public_base(env.get("R2_PUBLIC_URL", ""))
        if not env.get("R2_PUBLIC_URL"):
            p.append("R2_PUBLIC_URL is not set.")
        elif not url:
            p.append("R2_PUBLIC_URL must look like https://pub-<id>.r2.dev or https://downloads.example.org "
                     "(https, no query, no trailing slash).")
    if need_secrets:
        missing = [n for n in ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_ACCOUNT_ID") if not env.get(n, "").strip()]
        if missing:
            p.append("empty or missing: " + ", ".join(missing) + ". "
                     + (SECRETS_HELP_GITHUB if on_github() else SECRETS_HELP_GCP))
        elif not ACCOUNT_RE.fullmatch(env["R2_ACCOUNT_ID"].strip().lower()):
            p.append("R2_ACCOUNT_ID does not look like a Cloudflare account id (32 hex characters).")
    if any(x.startswith("R2_BUCKET") or x.startswith("R2_PUBLIC_URL") for x in p):
        p.append(GITHUB_HELP)
    return p


def public_base(url):
    """The public base URL without a trailing slash, or "" if it is not a plain https URL."""
    url = (url or "").strip()
    if url.endswith("/"):
        url = url[:-1]
    return url if PUBLIC_URL_RE.fullmatch(url) else ""


# ---------------------------------------------------------------- HTTP + SigV4
def http(method, url, body=None, headers=None, timeout=120):
    """Small HTTPS helper. Returns (status, headers, bytes). Never logs request headers (they carry the signature)."""
    if urllib.parse.urlparse(url).scheme != "https":
        raise ValueError("https only")
    req = urllib.request.Request(url, data=body, method=method, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def hget(headers, name):
    if headers is None:
        return None
    v = headers.get(name)
    if v is None and isinstance(headers, dict):
        v = {k.lower(): x for k, x in headers.items()}.get(name.lower())
    return v


def s3_error(status, body):
    """'HTTP 403 AccessDenied: ...' from an S3 error body. Only Code and Message: S3 error XML can echo the
    access key id, the string to sign and the canonical request, none of which belongs in a log."""
    code = msg = ""
    try:
        root = ET.fromstring(body or b"<x/>")
        code = (root.findtext("{*}Code") or root.findtext("Code") or "")[:80]
        msg = (root.findtext("{*}Message") or root.findtext("Message") or "")[:200]
    except ET.ParseError:
        pass
    return f"HTTP {status}" + (f" {code}" if code else "") + (f": {msg}" if msg else "")


def q(s):
    return urllib.parse.quote(s, safe="-_.~")


def sigv4(method, uri, canonical_query, headers, key_id, secret, region="auto"):
    """AWS Signature Version 4 Authorization header. headers: lowercase names, incl. host, x-amz-date and
    x-amz-content-sha256; all of them are signed."""
    amzdate = headers["x-amz-date"]
    date = amzdate[:8]
    signed = ";".join(sorted(headers))
    creq = "\n".join([method, uri, canonical_query, "".join(f"{k}:{headers[k]}\n" for k in sorted(headers)), signed,
                      headers["x-amz-content-sha256"]])
    scope = f"{date}/{region}/s3/aws4_request"
    sts = "\n".join(["AWS4-HMAC-SHA256", amzdate, scope, hashlib.sha256(creq.encode()).hexdigest()])
    k = hmac.new(("AWS4" + secret).encode(), date.encode(), hashlib.sha256).digest()
    for part in (region, "s3", "aws4_request"):
        k = hmac.new(k, part.encode(), hashlib.sha256).digest()
    sig = hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()
    return f"AWS4-HMAC-SHA256 Credential={key_id}/{scope}, SignedHeaders={signed}, Signature={sig}"


def part_size_for(size, base):
    """base, or the smallest whole-MiB size that keeps size within MAX_PARTS parts (173 GB / 64 MiB is 2,578)."""
    if -(-size // base) <= MAX_PARTS:
        return base
    need = -(-size // MAX_PARTS)
    return -(-need // (1 << 20)) * (1 << 20)


class R2:
    def __init__(self, account, key_id, secret, bucket, transport=None):
        self.host = f"{account}.r2.cloudflarestorage.com"
        self._key_id, self._secret, self.bucket = key_id, secret, bucket
        self.transport = transport or http

    def __repr__(self):
        return f"R2(bucket={self.bucket!r})"

    def send(self, method, key="", query=None, body=b"", headers=None, timeout=120):
        path = "/" + self.bucket + ("/" + key if key else "")
        uri = urllib.parse.quote(path, safe="/-_.~")
        cq = "&".join(f"{q(k)}={q(v)}" for k, v in sorted((query or {}).items()))
        payload = body if body is not None else b""
        hd = {"host": self.host, "x-amz-date": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
              # signed: R2 rejects a body that does not match this hash
              "x-amz-content-sha256": hashlib.sha256(payload).hexdigest()}
        for k, v in (headers or {}).items():
            hd[k.lower()] = str(v).strip()
        out = {k: v for k, v in hd.items() if k != "host"}
        out["Authorization"] = sigv4(method, uri, cq, hd, self._key_id, self._secret)
        url = f"https://{self.host}{uri}" + (f"?{cq}" if cq else "")
        return self.transport(method, url, payload if method in ("PUT", "POST") else None, out, timeout)

    def call(self, method, key="", query=None, body=b"", headers=None, ok=(200,), what="", tries=6, timeout=120):
        """send() with retries on network errors, 408, 429 and 5xx. Returns (status, headers, body) for any
        status in ok or a non-retryable 4xx (the caller decides); raises Fail when retries run out."""
        last = ""
        for attempt in range(1, tries + 1):
            try:
                st, hd, b = self.send(method, key, query, body, headers, timeout)
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                st, hd, b, last = 0, {}, b"", f"network error ({type(e).__name__})"
            else:
                if st in ok or (400 <= st < 500 and st not in (408, 429)):
                    return st, hd, b
                last = s3_error(st, b)
            if attempt < tries:
                time.sleep(min(60, 2 ** attempt))
        raise Fail(f"{what or method + ' ' + key} kept failing: {last}")

    # ---- operations
    def list(self, prefix, delimiter=None):
        """(objects {key: (size, last_modified)}, common prefixes) under prefix, all pages."""
        objs, prefixes, token = {}, [], None
        while True:
            query = {"list-type": "2", "prefix": prefix}
            if delimiter:
                query["delimiter"] = delimiter
            if token:
                query["continuation-token"] = token
            st, _, b = self.call("GET", query=query, what=f"listing {prefix}")
            if st != 200:
                raise Fail(f"listing {self.bucket}/{prefix} failed: {s3_error(st, b)}")
            root = ET.fromstring(b)
            for c in root.findall("{*}Contents"):
                objs[c.findtext("{*}Key")] = (int(c.findtext("{*}Size") or 0), c.findtext("{*}LastModified") or "")
            prefixes += [p.findtext("{*}Prefix") for p in root.findall("{*}CommonPrefixes")]
            if (root.findtext("{*}IsTruncated") or "").lower() != "true":
                return objs, prefixes
            token = root.findtext("{*}NextContinuationToken")

    def get(self, key):
        st, _, b = self.call("GET", key, what=f"reading {key}")
        if st == 404:
            return None
        if st != 200:
            raise Fail(f"reading {key} failed: {s3_error(st, b)}")
        return b

    def head(self, key):
        st, hd, _ = self.call("HEAD", key, what=f"checking {key}")
        if st == 404:
            return None
        if st != 200:
            raise Fail(f"checking {key} failed: HTTP {st}")
        return hd

    def put(self, key, data, ctype, create_only=True, cache_control=None, meta=None):
        h = {"Content-Type": ctype, "Content-MD5": base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode()}
        if create_only:
            h["If-None-Match"] = "*"
        if cache_control:
            h["Cache-Control"] = cache_control
        for k, v in (meta or {}).items():
            h[f"x-amz-meta-{k}"] = v
        st, _, b = self.call("PUT", key, body=data, headers=h, what=f"uploading {key}")
        if st == 412:
            raise Fail(f"{key} already exists - versions are immutable; publish a new version")
        if st != 200:
            raise Fail(f"uploading {key} failed: {s3_error(st, b)}")
        log(f"  uploaded {key} ({len(data):,} bytes)")

    def delete(self, key):
        st, _, b = self.call("DELETE", key, ok=(200, 204), what=f"deleting {key}")
        if st not in (200, 204, 404):
            raise Fail(f"deleting {key} failed: {s3_error(st, b)}")

    def _complete(self, key, upload_id, body):
        """CompleteMultipartUpload, create-only: the key is checked just before, and the request carries
        If-None-Match: * so the store itself refuses to replace an object that appeared in between (S3 and moto
        honour it). If the store answers that it does not implement the header, the HEAD check alone stands
        and a warning says so: then a window of the few seconds between the check and the complete remains."""
        gone = f"{key} appeared while this upload ran - versions are immutable; nothing was replaced"
        if self.head(key) is not None:
            raise Fail(gone)
        h = {"Content-Type": "application/xml", "If-None-Match": "*"}
        for _ in range(2):
            st, _, b = self.call("POST", key, query={"uploadId": upload_id}, body=body, headers=h, ok=(200, 501),
                                 what=f"completing {key}", timeout=600)
            if st == 412 or b"<Code>PreconditionFailed</Code>" in b:
                raise Fail(gone)
            if "If-None-Match" in h and (st == 501 or b"<Code>NotImplemented</Code>" in b):
                warn("the bucket does not accept If-None-Match on CompleteMultipartUpload; relying on the HEAD check "
                     "made just before (a window of seconds remains)")
                if self.head(key) is not None:
                    raise Fail(gone)
                h = {"Content-Type": "application/xml"}
                continue
            # S3 can answer 200 with an <Error> body for CompleteMultipartUpload.
            if st != 200 or b"<Error>" in b:
                raise Fail(f"completing {key} failed: {s3_error(st, b)}")
            return

    def multipart(self, key, path, ctype, meta, jobs=4, part_size=None):
        size = os.path.getsize(path)
        part = part_size_for(size, part_size or PART)
        count = max(1, -(-size // part))
        h = {"Content-Type": ctype, **{f"x-amz-meta-{k}": v for k, v in meta.items()}}
        st, _, b = self.call("POST", key, query={"uploads": ""}, headers=h, what=f"starting upload of {key}")
        if st != 200:
            raise Fail(f"starting upload of {key} failed: {s3_error(st, b)}")
        upload_id = ET.fromstring(b).findtext("{*}UploadId")
        if not upload_id:
            raise Fail(f"starting upload of {key}: no UploadId in the reply")
        log(f"  uploading {key}: {size / 1e9:,.2f} GB in {count} parts of {part >> 20} MiB, {jobs} at a time")
        stop, lock, done = threading.Event(), threading.Lock(), [0]
        fd = os.open(path, os.O_RDONLY)

        def one(n):
            if stop.is_set():
                raise Fail("stopped")
            off = (n - 1) * part
            data = os.pread(fd, min(part, size - off), off)
            md5 = base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode()
            st, hd, b = self.call("PUT", key, query={"partNumber": str(n), "uploadId": upload_id}, body=data,
                                  # explicit type: urllib would otherwise label the body form-urlencoded
                                  headers={"Content-MD5": md5, "Content-Type": "application/octet-stream"},
                                  what=f"part {n}/{count} of {key}", tries=8, timeout=600)
            etag = hget(hd, "ETag")
            if st != 200 or not etag:
                raise Fail(f"part {n}/{count} of {key} failed: {s3_error(st, b)}")
            with lock:
                done[0] += 1
                if count > 1 and (done[0] == count or done[0] % max(1, count // 20) == 0):
                    log(f"    {done[0]}/{count} parts")
            return n, etag

        try:
            etags = {}
            with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
                futs = [ex.submit(one, n) for n in range(1, count + 1)]
                try:
                    for f in as_completed(futs):
                        n, etag = f.result()
                        etags[n] = etag
                except BaseException:
                    stop.set()
                    for f in futs:
                        f.cancel()
                    raise
            body = ("<CompleteMultipartUpload>" + "".join(
                f"<Part><PartNumber>{n}</PartNumber><ETag>{etags[n]}</ETag></Part>" for n in sorted(etags))
                + "</CompleteMultipartUpload>").encode()
            self._complete(key, upload_id, body)
        except BaseException:
            try:
                self.send("DELETE", key, query={"uploadId": upload_id})
                log(f"  aborted the multipart upload of {key}; nothing was left behind")
            except Exception:
                warn(f"could not abort the multipart upload of {key}; R2 removes incomplete uploads after 7 days by default")
            raise
        finally:
            os.close(fd)
        log(f"  uploaded {key} ({size / 1e6:,.1f} MB)")


# ---------------------------------------------------------------- kit + manifests
def read_info(zpath):
    with zipfile.ZipFile(zpath) as zf:
        names = [n for n in zf.namelist() if n.count("/") == 1 and n.endswith("/KIT-INFO.json")]
        if len(names) != 1:
            raise Fail("zip does not contain exactly one top-level <kit>/KIT-INFO.json")
        return json.loads(zf.read(names[0])), zf.read(names[0])


def check_info(info):
    if info.get("redistributable") is not True:
        raise Fail("KIT-INFO.json says this kit is NOT redistributable (e.g. it includes the post-disaster ZIM). Refusing to publish.")
    if info.get("git_dirty"):
        raise Fail("kit was built from a dirty git tree; publish only from a clean commit")
    unpinned = [i["id"] for i in info.get("items", []) if not i.get("pinned") and not i["dest"].endswith(".pmtiles")]
    if unpinned:
        raise Fail("kit contains unpinned downloads: " + ", ".join(unpinned))
    name, version, tier = info.get("name"), info.get("version"), info.get("tier")
    if tier not in TIERS or name != f"offline-toolkit-{tier}":
        raise Fail(f"unexpected kit name/tier in KIT-INFO.json: {name!r} / {tier!r}")
    check_version(version)
    return name, version, tier


def check_version(version):
    if not isinstance(version, str) or not SAFE_VERSION.fullmatch(version):
        raise Fail(f"unsafe version {version!r}")


def keys_for(tier, version):
    z = f"offline-toolkit-{tier}-{version}.zip"
    d = f"{tier}/{version}"
    return {"zip": f"{d}/{z}", "sha256_file": f"{d}/{z}.sha256", "kit_info": f"{d}/KIT-INFO.json"}


def parse_keep(spec, base=None):
    keep = dict(base or DEFAULT_KEEP)
    for item in filter(None, re.split(r"[,\s]+", spec or "")):
        m = re.fullmatch(r"(small|medium|large)=([1-9][0-9]?)", item)
        if not m:
            raise Fail(f"bad retention {item!r}: use tier=N with N >= 1, e.g. small=3,medium=2,large=1")
        keep[m.group(1)] = int(m.group(2))
    return keep


def load_index(r2, tier):
    """Entries of <tier>/index.json, newest first ([] if there is none). Refuses a malformed index rather
    than overwriting it."""
    raw = r2.get(f"{tier}/index.json")
    if raw is None:
        return []
    try:
        doc = json.loads(raw)
        entries = doc["versions"]
        for e in entries:
            check_version(e["version"])
            if e.get("tier") != tier or e.get("zip") != keys_for(tier, e["version"])["zip"]:
                raise ValueError(e.get("version"))
        return entries
    except (ValueError, KeyError, TypeError, Fail) as e:
        raise Fail(f"{tier}/index.json is malformed ({type(e).__name__}); fix or remove it by hand before publishing")


def plan_retention(r2, tier, version, keep):
    """(previous index entries to keep, entries to delete, unindexed version prefixes) for publishing version."""
    entries = load_index(r2, tier)
    _, prefixes = r2.list(f"{tier}/", delimiter="/")
    present = {p[len(tier) + 1:].rstrip("/") for p in prefixes}
    prev = [e for e in entries if e["version"] in present and e["version"] != version]
    indexed = {e["version"] for e in entries}
    orphans = sorted(v for v in present if v not in indexed and v != version)
    n = keep[tier]
    return prev[: n - 1], prev[n - 1:], orphans


def manifest_doc(tier, entries, keep):
    return {"tier": tier, "keep": keep, "updated_at": now_iso(), "versions": entries}


def enc(doc):
    return (json.dumps(doc, indent=1) + "\n").encode()


MANIFEST_CC = "no-cache, max-age=0"


def write_manifests(r2, tier, entries, keep):
    r2.put(f"{tier}/index.json", enc(manifest_doc(tier, entries, keep[tier])), "application/json", False, MANIFEST_CC)
    r2.put(f"{tier}/latest.json", enc(entries[0]), "application/json", False, MANIFEST_CC)
    # The top level is rebuilt from the three tier indexes, so it heals itself if an earlier write was lost.
    tiers = {}
    for t in TIERS:
        tiers[t] = entries if t == tier else load_index(r2, t)
    now = now_iso()
    r2.put("index.json", enc({"updated_at": now, "keep": keep, "tiers": tiers}), "application/json", False, MANIFEST_CC)
    r2.put("latest.json", enc({"updated_at": now, "tiers": {t: (v[0] if v else None) for t, v in tiers.items()}}),
           "application/json", False, MANIFEST_CC)


def verify_manifests(r2, entry):
    t, v = entry["tier"], entry["version"]

    def want(doc, where):
        if not (isinstance(doc, dict) and doc.get("version") == v and doc.get("sha256") == entry["sha256"]
                and doc.get("size") == entry["size"] and doc.get("zip") == entry["zip"]):
            raise Fail(f"{where} does not point at {t} {v} with the expected sha256 and size after writing it")

    def load(key):
        raw = r2.get(key)
        if raw is None:
            raise Fail(f"{key} is missing right after writing it")
        return json.loads(raw)

    want(load(f"{t}/latest.json"), f"{t}/latest.json")
    want((load(f"{t}/index.json").get("versions") or [None])[0], f"{t}/index.json")
    want(load("latest.json").get("tiers", {}).get(t), "latest.json")
    want((load("index.json").get("tiers", {}).get(t) or [None])[0], "index.json")


def verify_version(r2, keys, size, sha, sidecar, info_bytes):
    hd = r2.head(keys["zip"])
    if hd is None:
        raise Fail(f"{keys['zip']} is missing after upload")
    got = hget(hd, "Content-Length")
    if str(got) != str(size):
        raise Fail(f"{keys['zip']}: size after upload is {got}, expected {size}")
    if (hget(hd, "x-amz-meta-sha256") or "") != sha:
        raise Fail(f"{keys['zip']}: sha256 metadata after upload does not match")
    if r2.get(keys["sha256_file"]) != sidecar:
        raise Fail(f"{keys['sha256_file']} does not read back as written")
    if r2.get(keys["kit_info"]) != info_bytes:
        raise Fail(f"{keys['kit_info']} does not read back as written")
    log(f"  verified {keys['zip']}: {size:,} bytes, sha256 {sha} (metadata, sidecar and KIT-INFO.json)")


def delete_version(r2, tier, version, current):
    check_version(version)
    if version == current:
        raise Fail("refusing to delete the version just published")
    prefix = f"{tier}/{version}/"
    objs, _ = r2.list(prefix)
    allowed = set(keys_for(tier, version).values())
    extra = sorted(k for k in objs if k not in allowed)
    if extra:
        warn(f"not deleting {prefix}: it holds objects this tool did not write ({', '.join(extra[:3])})")
        return False
    for k in sorted(objs):
        r2.delete(k)
    log(f"  deleted {prefix} ({len(objs)} objects)")
    return True


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()


def client_from_env(env, bucket):
    probs = config_problems({**env, "R2_BUCKET": bucket})
    if probs:
        raise Fail("R2 configuration:\n  " + "\n  ".join(probs))
    key_id, secret, account = (env[n].strip() for n in ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_ACCOUNT_ID"))
    for s in (key_id, secret, account):
        mask(s)
    return R2(account.lower(), key_id, secret, bucket)


# ---------------------------------------------------------------- main
def run(a, env, make_client=client_from_env):
    keep = parse_keep(a.keep, parse_keep(env.get("KIT_KEEP", "")))
    pub = public_base(a.public_url or env.get("R2_PUBLIC_URL", ""))

    if a.check_config:
        probs = config_problems(env, need_public_url=True, need_secrets=not a.no_secrets)
        if probs:
            raise Fail("R2 configuration:\n  " + "\n  ".join(probs))
        log("R2 configuration OK (bucket and public URL well-formed" + ("" if a.no_secrets else ", secrets set") + ")")
        return 0

    if a.zip:
        if not os.path.isfile(a.zip):
            raise Fail(f"{a.zip} not found")
        info, info_bytes = read_info(a.zip)
        name, version, tier = check_info(info)
    else:
        if not (a.tier and a.version and a.dry_run):
            raise Fail("without --zip, pass --tier, --version and --dry-run (a read-only preflight)")
        tier, version, info = a.tier, a.version, None
        check_version(version)
    keys = keys_for(tier, version)

    sha = size = None
    if a.zip:
        log(f"hashing {a.zip} ...")
        sha, size = sha256_file(a.zip), os.path.getsize(a.zip)
        log(f"kit {name} {version}: {size / 1e9:.2f} GB sha256 {sha}")
    log("would write:" if a.dry_run else "will write:")
    for k in list(keys.values()) + [f"{tier}/index.json", f"{tier}/latest.json", "index.json", "latest.json"]:
        log(f"  {k}" + (f"  ->  {pub}/{k}" if pub else ""))
    if a.offline:
        if not a.dry_run:
            raise Fail("--offline needs --dry-run")
        log(f"offline dry run: kit checked, R2 not contacted; retention would keep {keep[tier]} {tier} version(s)")
        return 0

    bucket = a.bucket or env.get("R2_BUCKET", "")
    r2 = make_client(env, bucket)
    existing, _ = r2.list(f"{tier}/{version}/")
    if existing:
        raise Fail(f"{tier}/{version}/ already exists in the bucket - versions are immutable; publish a new version")
    keep_prev, delete, orphans = plan_retention(r2, tier, version, keep)
    log(f"retention: keep {keep[tier]} {tier} version(s): {', '.join([version] + [e['version'] for e in keep_prev])}")
    for e in delete:
        log(f"  {'would delete' if a.dry_run else 'will delete (after verify)'} {tier}/{e['version']}/")
    for v in orphans:
        warn(f"{tier}/{v}/ is in the bucket but not in {tier}/index.json (an unverified upload?); left alone, "
             f"delete it by hand if it is not wanted")
    if a.dry_run:
        log(f"dry run: R2 bucket reachable with these credentials, {tier}/{version}/ is free; nothing written or deleted")
        return 0

    zname = keys["zip"].rsplit("/", 1)[1]
    sidecar = f"{sha}  {zname}\n".encode()
    meta = {"sha256": sha, "git-commit": str(info.get("git_commit") or "")[:40]}
    r2.multipart(keys["zip"], a.zip, "application/zip", meta, jobs=a.jobs, part_size=(a.part_size << 20) if a.part_size else None)
    r2.put(keys["sha256_file"], sidecar, "text/plain; charset=utf-8")
    r2.put(keys["kit_info"], info_bytes, "application/json")
    verify_version(r2, keys, size, sha, sidecar, info_bytes)

    # Plan again now, from a fresh read: an upload can take an hour, and another publish of this tier (a
    # tag push and a main dispatch, or a Cloud Build the workflow stopped waiting for) may have indexed a
    # version meanwhile. Planning from the read made before the upload would drop it from the index.
    keep_prev, delete, _ = plan_retention(r2, tier, version, keep)
    entry = {"tier": tier, "name": name, "version": version, "published_at": now_iso(), "size": size, "sha256": sha,
             "git_commit": info.get("git_commit"), **keys}
    write_manifests(r2, tier, [entry] + keep_prev, keep)
    verify_manifests(r2, entry)
    log(f"  manifests point at {tier} {version}")

    failed = []
    for e in delete:
        try:
            delete_version(r2, tier, e["version"], version)
        except Fail as ex:
            failed.append(f"{e['version']} ({ex})")
    if failed:
        warn("published, but could not delete old version(s) " + "; ".join(failed) + ". They are no longer listed; "
             "delete them by hand to stop paying for their storage.")
    log(f"published {pub}/{keys['zip']}" if pub else f"published {bucket}/{keys['zip']}")
    return 0


def main(argv=None, env=None, make_client=client_from_env):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--zip")
    ap.add_argument("--tier", choices=TIERS, help="with --version and --dry-run, instead of --zip: preflight only")
    ap.add_argument("--version")
    ap.add_argument("--bucket", help="R2 bucket (default: $R2_BUCKET)")
    ap.add_argument("--public-url", help="public base URL, only for log lines (default: $R2_PUBLIC_URL)")
    ap.add_argument("--keep", default="", help="retention, e.g. small=3,medium=2,large=1 (default: DEFAULT_KEEP, or $KIT_KEEP)")
    ap.add_argument("--jobs", type=int, default=4, help="parts uploaded in parallel (default 4)")
    ap.add_argument("--part-size", type=int, default=0, help="part size in MiB (default 64, raised for huge files)")
    ap.add_argument("--dry-run", action="store_true", help="check everything and print the plan; write and delete nothing")
    ap.add_argument("--offline", action="store_true", help="with --dry-run: check the kit only, do not contact R2")
    ap.add_argument("--check-config", action="store_true",
                    help="only check R2_BUCKET, R2_PUBLIC_URL and the three secrets in the environment")
    ap.add_argument("--no-secrets", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    try:
        return run(a, os.environ if env is None else env, make_client)
    except Fail as e:
        msg = str(e)
        if on_github():  # annotations take %0A for newlines
            print("::error::" + msg.replace("%", "%25").replace("\r", "").replace("\n", "%0A"), flush=True)
        log("ERROR: " + msg)
        return 1


if __name__ == "__main__":
    sys.exit(main())
