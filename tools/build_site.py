#!/usr/bin/env python3
"""Build the public downloads page (GitHub Pages) from the R2 manifests. Python stdlib only, no credentials:
the manifests and kits are public, so this only reads <public-url>/index.json over HTTPS.

  python3 tools/build_site.py --public-url https://pub-<id>.r2.dev --out _site
  python3 tools/build_site.py --public-url https://pub-<id>.r2.dev --index index.json --out _site   # offline

Writes <out>/index.html (one plain page, no JavaScript) and <out>/.nojekyll.
"""
import argparse, html, json, os, re, sys, time, urllib.error, urllib.request

REPO_URL = "https://github.com/cjrada/offline-toolkit"
TIERS = (("small", "~7 GB"), ("medium", "~41 GB"), ("large", "~173 GB"))
PUBLIC_URL_RE = re.compile(r"https://[A-Za-z0-9.-]+(/[A-Za-z0-9._~-]+)*")
KEY_RE = re.compile(r"(small|medium|large)/[0-9A-Za-z_-][0-9A-Za-z._-]{0,63}/[0-9A-Za-z._-]+")
SHA_RE = re.compile(r"[0-9a-f]{64}")


def public_base(url):
    url = (url or "").strip()
    if url.endswith("/"):
        url = url[:-1]
    return url if PUBLIC_URL_RE.fullmatch(url) else ""


def fetch_index(base):
    """The top-level index.json, or None if nothing has been published yet."""
    req = urllib.request.Request(f"{base}/index.json?t={int(time.time())}", headers={"User-Agent": "offline-toolkit-site/1",
                                                                                  "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            if not r.geturl().startswith("https://"):
                raise SystemExit("index.json was redirected off https")
            return json.loads(r.read(5_000_000))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise SystemExit(f"fetching {base}/index.json failed: HTTP {e.code}")


def clean_entry(e, tier):
    """A manifest entry with only the fields the page shows, validated; None if it does not look right."""
    try:
        keys = {k: e[k] for k in ("zip", "sha256_file", "kit_info")}
        if e["tier"] != tier or not all(KEY_RE.fullmatch(v) and v.startswith(f"{tier}/{e['version']}/") for v in keys.values()):
            return None
        if not SHA_RE.fullmatch(e["sha256"]) or not isinstance(e["size"], int) or e["size"] <= 0:
            return None
        return {"version": str(e["version"]), "published_at": str(e.get("published_at", ""))[:20],
                "size": e["size"], "sha256": e["sha256"], **keys}
    except (KeyError, TypeError):
        return None


def gb(n):
    return f"{n / 1e9:,.1f} GB"


def render(index, base, built_at=None):
    esc = html.escape
    tiers = (index or {}).get("tiers") or {}
    out = []
    for tier, approx in TIERS:
        entries = [c for c in (clean_entry(e, tier) for e in tiers.get(tier) or []) if c]
        out.append(f'<section id="{tier}">\n<h2>{tier.capitalize()} kit <small>({approx})</small></h2>')
        if not entries:
            out.append("<p>Not published yet.</p>\n</section>")
            continue
        e = entries[0]
        url = lambda k: esc(f"{base}/{k}", quote=True)  # noqa: E731
        out.append(f"""<p class="dl"><a href="{url(e['zip'])}">Download {esc(e['zip'].rsplit('/', 1)[1])}</a>
  · <a href="{url(e['sha256_file'])}">.sha256</a> · <a href="{url(e['kit_info'])}">KIT-INFO.json</a></p>
<table>
<tr><th>Version</th><td><code>{esc(e['version'])}</code></td></tr>
<tr><th>Published</th><td>{esc(e['published_at'][:10])}</td></tr>
<tr><th>Size</th><td>{gb(e['size'])} ({e['size']:,} bytes)</td></tr>
<tr><th>SHA-256</th><td><code class="sha">{esc(e['sha256'])}</code></td></tr>
</table>""")
        if len(entries) > 1:
            out.append("<h3>Previous versions</h3>\n<ul>")
            for p in entries[1:]:
                out.append(f'<li><a href="{url(p["zip"])}"><code>{esc(p["version"])}</code></a> · {esc(p["published_at"][:10])} · '
                           f'{gb(p["size"])} · <a href="{url(p["sha256_file"])}">.sha256</a> · sha256 <code class="sha">{esc(p["sha256"])}</code></li>')
            out.append("</ul>")
        out.append("</section>")
    built = esc(built_at or time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()))
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Offline Toolkit downloads</title>
<style>
:root {{ color-scheme: light dark; --fg: #1b1b1b; --bg: #fdfdfc; --muted: #5b5b5b; --line: #ddd; --link: #0b57d0; }}
@media (prefers-color-scheme: dark) {{ :root {{ --fg: #e8e8e6; --bg: #161616; --muted: #a8a8a8; --line: #333; --link: #8ab4f8; }} }}
body {{ font: 16px/1.5 system-ui, sans-serif; color: var(--fg); background: var(--bg); max-width: 52rem; margin: 0 auto; padding: 1rem 16px 3rem; }}
a {{ color: var(--link); }} h1 {{ margin-bottom: .2rem; }} h2 small {{ color: var(--muted); font-weight: normal; }}
section {{ border-top: 1px solid var(--line); margin-top: 1.5rem; }}
table {{ border-collapse: collapse; }} th {{ text-align: left; padding-right: 1rem; color: var(--muted); font-weight: normal; vertical-align: top; }}
code.sha {{ word-break: break-all; font-size: .85em; }} .dl {{ font-size: 1.1rem; }} li {{ margin: .3rem 0; }}
.note {{ color: var(--muted); }}
</style>
</head>
<body>
<h1>Offline Toolkit downloads</h1>
<p>Prebuilt kits for a USB drive: offline AI chat, a library, maps and manuals. Source, build instructions and
how to run a kit: <a href="{REPO_URL}">{REPO_URL}</a>.</p>
<p><strong>These files are large</strong>: small ~7 GB, medium ~41 GB, large ~173 GB. Use a fast, unmetered
connection and a download manager that can resume. Format the drive exFAT (FAT32 cannot hold files over 4 GB).</p>
<p class="note">Check a download against its <code>.sha256</code> file: <code>sha256sum -c offline-toolkit-&lt;tier&gt;-&lt;version&gt;.zip.sha256</code>
(Linux/macOS: <code>shasum -a 256 -c</code>), or on Windows PowerShell compare <code>(Get-FileHash &lt;zip&gt;).Hash</code>
with the first word of the <code>.sha256</code> file.</p>
{chr(10).join(out)}
<p class="note">Files are served from Cloudflare R2. Manifests: <a href="{esc(base, quote=True)}/latest.json">latest.json</a> ·
<a href="{esc(base, quote=True)}/index.json">index.json</a>. Page built {built}.</p>
</body>
</html>
"""


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--public-url", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--index", help="read this local index.json instead of fetching it")
    a = ap.parse_args(argv)
    base = public_base(a.public_url)
    if not base:
        print("::error::R2_PUBLIC_URL is not set or not a plain https URL. Set the repository variable in GitHub: "
              "Settings -> Secrets and variables -> Actions -> Variables: R2_PUBLIC_URL = the public base URL "
              "(r2.dev or custom domain), no trailing slash", file=sys.stderr)
        return 1
    if a.index:
        with open(a.index, encoding="utf-8") as fh:
            index = json.load(fh)
    else:
        index = fetch_index(base)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(render(index, base))
    open(os.path.join(a.out, ".nojekyll"), "w").close()
    n = sum(len(v or []) for v in ((index or {}).get("tiers") or {}).values())
    print(f"wrote {a.out}/index.html ({n} kit version(s) listed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
