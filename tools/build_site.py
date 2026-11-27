#!/usr/bin/env python3
"""Build the public downloads page (GitHub Pages) from the R2 manifests. Python stdlib only, no credentials:
the manifests and kits are public, so this only reads <public-url>/<tier>/index.json over HTTPS. The three
tier indexes are the source of truth (the top-level index.json is rebuilt from them on each publish, so two
publishes of different tiers at once could leave it a step behind; the tier files cannot be).

  python3 tools/build_site.py --public-url https://pub-<id>.r2.dev --out _site
  python3 tools/build_site.py --public-url https://pub-<id>.r2.dev --index index.json --out _site   # offline

Writes <out>/index.html, <out>/.nojekyll and <out>/screenshots/*.webp (the "What to expect" gallery, copied from
site/screenshots/; the site only, never the kit: tools/build_kit.py packs only start/stop scripts, README, LICENSE,
tools/ and www/). The page is plain HTML; the only script is a small inline helper for the Support hosting
section's copy buttons (the addresses stay selectable text without it). That section reads its donation
addresses from tools/donate.json and draws their QR codes here, as inline SVG, with the stdlib-only encoder
below: the deployed page fetches nothing from any other host. The "AI can make mistakes" notice comes from
tools/ai-notice.json, the same text the kit's own pages show.
"""
import argparse, html, json, os, re, shutil, sys, time, urllib.error, urllib.request

REPO_URL = "https://github.com/cjrada/offline-toolkit"
TIERS = (("small", "~7 GB"), ("medium", "~41 GB"), ("large", "~173 GB"))
PUBLIC_URL_RE = re.compile(r"https://[A-Za-z0-9.-]+(/[A-Za-z0-9._~-]+)*")
KEY_RE = re.compile(r"(small|medium|large)/[0-9A-Za-z_-][0-9A-Za-z._-]{0,63}/[0-9A-Za-z._-]+")
SHA_RE = re.compile(r"[0-9a-f]{64}")
HERE = os.path.dirname(os.path.abspath(__file__))
DONATE_JSON = os.path.join(HERE, "donate.json")
NOTICE_JSON = os.path.join(HERE, "ai-notice.json")
SHOTS_DIR = os.path.join(os.path.dirname(HERE), "site", "screenshots")
# (file, alt text, caption). All are 1440x900 WebP captures of the small kit, unedited.
SCREENSHOTS = (
    ("kit-home.webp", "The kit's home page in a browser: tiles for Chat, Ask the Manuals, Library, Maps and PDF shelf, "
     "a safety note, and the AI model panel showing the 4B model running on the CPU only.",
     "Home page: chat, manuals, library, maps and the PDF shelf; the model panel shows CPU-only mode."),
    ("kit-ask-the-manuals.webp", "Ask the Manuals answering \"How much bleach do I add to purify water?\" below the yellow "
     "\"AI can make mistakes\" notice: 5 drops per litre of 5.25-8.25% bleach, 1/4 teaspoon per gallon, 1 teaspoon per "
     "5 gallons, 2 teaspoons per 10 gallons, stand at least 60 minutes, each with a numbered citation, and the Sources "
     "list starting below.",
     "Ask the Manuals: a cited answer from the water-treatment PDF, with the AI notice above it."),
    ("kit-ai-chat.webp", "Offline AI chat answering \"How do I purify water in an emergency?\" with headed sections "
     "on boiling and chemical treatment.",
     "AI chat (llamafile's chat page) answering \"How do I purify water in an emergency?\"."),
    ("kit-library.webp", "The offline library shelf (Kiwix) listing Wikivoyage, Water Treatment, WikiMed, Medical, "
     "Knots and Outdoors Q&A.",
     "Offline library (Kiwix): Wikivoyage, water treatment, WikiMed, medicine, knots, Outdoors Q&A."),
    ("kit-wikivoyage-brisbane.webp", "The Wikivoyage travel guide article for Brisbane, opened offline in the library.",
     "Wikivoyage's Brisbane article, offline."),
    ("kit-map-brisbane.webp", "Offline OpenStreetMap map of Brisbane, Moreton Bay and the Gold Coast.",
     "Offline map of Australia, zoomed to Brisbane, Moreton Bay and the Gold Coast."),
)
SHOT_W, SHOT_H = 1440, 900
DONATE_ID_RE = re.compile(r"[a-z0-9-]{1,16}")
DONATE_ADDR_RE = re.compile(r"[A-Za-z0-9]{20,100}")


# --- QR code (ISO/IEC 18004), byte mode, error correction level M, versions 1-6, stdlib only ------------------
# Enough for any donation address (v6-M holds 106 bytes). Follows the structure of Project Nayuki's public QR
# generator. tests/test_build_site.py decodes the result with its own reader and checks the RS codewords.
QR_M_BLOCKS = {1: (1, 10), 2: (1, 16), 3: (1, 26), 4: (2, 18), 5: (2, 24), 6: (4, 16)}  # (blocks, ec per block)
QR_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34]}


def _gf_mul(x, y):
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_ecc(data, degree):
    div, root = [0] * (degree - 1) + [1], 1
    for _ in range(degree):
        for j in range(degree):
            div[j] = _gf_mul(div[j], root)
            if j + 1 < degree:
                div[j] ^= div[j + 1]
        root = _gf_mul(root, 2)
    res = [0] * degree
    for b in data:
        f = b ^ res.pop(0)
        res.append(0)
        for i, c in enumerate(div):
            res[i] ^= _gf_mul(c, f)
    return res


def _raw_modules(v):
    n = (16 * v + 128) * v + 64
    if v >= 2:
        k = v // 7 + 2
        n -= (25 * k - 10) * k - 55
    return n


QR_MASKS = (lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0,
            lambda x, y: (x + y) % 3 == 0, lambda x, y: (x // 3 + y // 2) % 2 == 0,
            lambda x, y: x * y % 2 + x * y % 3 == 0, lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
            lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0)


def qr_matrix(text):
    """The QR code for text (UTF-8, byte mode, level M) as rows of booleans (True = dark), no quiet zone."""
    data = text.encode("utf-8")
    for v, (nblocks, necc) in QR_M_BLOCKS.items():
        ndata = _raw_modules(v) // 8 - nblocks * necc
        if 4 + 8 + 8 * len(data) <= 8 * ndata:
            break
    else:
        raise ValueError(f"too long for a version 1-6 QR code: {len(data)} bytes")
    bits = [0, 1, 0, 0] + [(len(data) >> i) & 1 for i in range(7, -1, -1)]
    bits += [(b >> i) & 1 for b in data for i in range(7, -1, -1)]
    bits += [0] * min(4, 8 * ndata - len(bits))
    bits += [0] * (-len(bits) % 8)
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    words += [(0xEC, 0x11)[i % 2] for i in range(ndata - len(words))]
    per = ndata // nblocks  # equal-sized blocks for every version in QR_M_BLOCKS
    blocks = [words[i * per:(i + 1) * per] for i in range(nblocks)]
    eccs = [_rs_ecc(b, necc) for b in blocks]
    stream = [b[i] for i in range(per) for b in blocks] + [e[i] for i in range(necc) for e in eccs]

    size = 4 * v + 17
    mods = [[False] * size for _ in range(size)]
    func = [[False] * size for _ in range(size)]

    def put(x, y, dark):
        mods[y][x], func[y][x] = dark, True

    for i in range(size):
        put(6, i, i % 2 == 0)
        put(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                if 0 <= cx + dx < size and 0 <= cy + dy < size:
                    put(cx + dx, cy + dy, max(abs(dx), abs(dy)) not in (2, 4))
    al = QR_ALIGN[v]
    for i, ax in enumerate(al):
        for j, ay in enumerate(al):
            if (i, j) not in ((0, 0), (0, len(al) - 1), (len(al) - 1, 0)):
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        put(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)

    def put_format(mask):
        d = mask  # level M's format bits are 00
        rem = d
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        f = ((d << 10) | rem) ^ 0x5412
        bit = lambda i: (f >> i) & 1 == 1  # noqa: E731
        for i in range(6):
            put(8, i, bit(i))
        put(8, 7, bit(6))
        put(8, 8, bit(7))
        put(7, 8, bit(8))
        for i in range(9, 15):
            put(14 - i, 8, bit(i))
        for i in range(8):
            put(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            put(8, size - 15 + i, bit(i))
        put(8, size - 8, True)

    put_format(0)
    i, right = 0, size - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(size):
            for j in range(2):
                x = right - j
                y = size - 1 - vert if ((right + 1) & 2) == 0 else vert
                if not func[y][x] and i < len(stream) * 8:
                    mods[y][x] = (stream[i >> 3] >> (7 - (i & 7))) & 1 == 1
                    i += 1
        right -= 2

    def masked(m):
        return [[mods[y][x] ^ (not func[y][x] and QR_MASKS[m](x, y)) for x in range(size)] for y in range(size)]

    best = None
    for m in range(8):
        put_format(m)
        cand = masked(m)
        score = _penalty(cand)
        if best is None or score < best[0]:
            best = (score, m)
    put_format(best[1])
    return masked(best[1])


def _penalty(g):
    n, score = len(g), 0
    lines = g + [list(c) for c in zip(*g)]
    for row in lines:
        run = 1
        for a, b in zip(row, row[1:]):
            if a == b:
                run += 1
            else:
                score += run - 2 if run >= 5 else 0
                run = 1
        score += run - 2 if run >= 5 else 0
        s = "0000" + "".join("1" if c else "0" for c in row) + "0000"
        score += 40 * sum(s.count(p) for p in ("00001011101", "10111010000"))
    for y in range(n - 1):
        for x in range(n - 1):
            if g[y][x] == g[y][x + 1] == g[y + 1][x] == g[y + 1][x + 1]:
                score += 3
    dark = sum(map(sum, g))
    score += 10 * (abs(dark * 20 - n * n * 10) // (n * n))
    return score


def qr_svg(text, label):
    """An inline SVG of text's QR code: dark modules on a white square with a 4-module quiet zone."""
    g = qr_matrix(text)
    n, q = len(g), 4
    path = []
    for y, row in enumerate(g):
        x = 0
        while x < n:
            if row[x]:
                w = 1
                while x + w < n and row[x + w]:
                    w += 1
                path.append(f"M{x + q} {y + q}h{w}v1h-{w}z")
                x += w
            else:
                x += 1
    side = n + 2 * q
    return (f'<svg class="qr" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {side} {side}" role="img" '
            f'aria-label="{html.escape(label, quote=True)}" shape-rendering="crispEdges">'
            f'<rect width="{side}" height="{side}" fill="#fff"/><path fill="#000" d="{"".join(path)}"/></svg>')


def load_donate(path=DONATE_JSON):
    """The donation addresses from tools/donate.json, validated: [{id, label, network, address}, ...]."""
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    out = []
    for a in doc.get("addresses") or []:
        if not (isinstance(a, dict) and DONATE_ID_RE.fullmatch(str(a.get("id", "")))
                and DONATE_ADDR_RE.fullmatch(str(a.get("address", ""))) and a.get("label")):
            raise SystemExit(f"{path}: bad donation entry {a!r} (needs id, label and a plain alphanumeric address)")
        out.append({k: str(a.get(k, "")) for k in ("id", "label", "network", "address")})
    if len({a["id"] for a in out}) != len(out):
        raise SystemExit(f"{path}: donation ids must be unique")
    return out


COPY_JS = """<script>
document.querySelectorAll("button[data-copy]").forEach(function (b) {
  b.hidden = false;
  b.addEventListener("click", function () {
    var el = document.getElementById(b.getAttribute("data-copy")), text = el.textContent;
    function select() { var r = document.createRange(), s = window.getSelection(); r.selectNodeContents(el); s.removeAllRanges(); s.addRange(r); }
    function say(m) { b.textContent = m; setTimeout(function () { b.textContent = "Copy"; }, 2000); }
    function fallback() { select(); var ok = false; try { ok = document.execCommand("copy"); } catch (e) {} say(ok ? "Copied" : "Selected, press Ctrl+C"); }
    if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(function () { say("Copied"); }, fallback);
    else fallback();
  });
});
</script>"""


def render_support(addresses):
    """The Support hosting section: what donations are for, then each address as text, a copy button and a QR."""
    esc = html.escape
    if not addresses:
        return ""
    cards = []
    for a in addresses:
        cards.append(f"""<div class="addr">
{qr_svg(a['address'], f"QR code for the {a['label']} address")}
<div>
<h3>{esc(a['label'])}</h3>
<p class="note">{esc(a['network'])}</p>
<p><code class="wallet" id="addr-{a['id']}">{esc(a['address'])}</code>
<button type="button" data-copy="addr-{a['id']}" hidden>Copy</button></p>
</div>
</div>""")
    return f"""<section id="support">
<h2>Support hosting</h2>
<p>The kits are free and stay free. If they are useful to you and you would like to chip in, donations help cover
hosting: Cloudflare R2 storage for the kits and the cost of building and publishing the large ones. Entirely
optional, and thank you.</p>
{chr(10).join(cards)}
<p class="note">Each QR code is just the address shown next to it. Check the address in your wallet before sending.</p>
</section>"""


def load_notice(path=NOTICE_JSON):
    """The kit's "AI can make mistakes" notice (HTML: <b> plus text), shared with www/ pages."""
    with open(path, encoding="utf-8") as fh:
        text = json.load(fh)["sources"]
    if re.search(r"<(?!/?b>)", text):
        raise SystemExit(f"{path}: the notice may only contain <b> tags")
    return text


def render_expect(notice):
    """The What to expect section: a screenshot gallery, honest speed and hardware notes, the AI notice."""
    esc = html.escape
    figs = "\n".join(
        f'<figure><a href="screenshots/{f}"><img src="screenshots/{f}" alt="{esc(alt, quote=True)}" '
        f'width="{SHOT_W}" height="{SHOT_H}" loading="lazy" decoding="async"></a>\n<figcaption>{esc(cap)}</figcaption></figure>'
        for f, alt, cap in SCREENSHOTS)
    return f"""<section id="expect">
<h2>What to expect</h2>
<p class="warn ai-notice" role="note">{notice}</p>
<p>Unzip a kit onto a USB drive, run <code>start.bat</code> (Windows) or <code>sh start.sh</code> (Linux) and open
<code>http://localhost:8000/</code>. Everything below runs from the drive with no internet. These are real, unedited
screenshots of the small kit on a Linux laptop. The Ask the Manuals one is from the current kit pages; the others
were taken before the kit's own chat page and this notice were added: the Chat tile now opens a kit page with the
notice pinned on screen, and llamafile's chat page (shown here) is still one click away.</p>
<div class="gallery">
{figs}
</div>
<h3>Speed and hardware</h3>
<ul>
<li><strong>Measured without a GPU</strong> (8-core CPU, CPU only): the small kit's 4B model (Qwen3-4B-Instruct-2507, Q4_K_M)
wrote about 7-14 tokens per second. A full chat answer took about 1-2.5 minutes, and an Ask the Manuals answer about a
minute. With a supported GPU the AI is much faster; the kit uses one automatically.</li>
<li><strong>Small</strong> (~7 GB, 16 GB drive): Qwen3-4B-Instruct-2507 (2.5 GB) for 8 GB of RAM, and Qwen3-1.7B (1.1 GB) for
4 GB laptops.</li>
<li><strong>Medium</strong> (~41 GB, 64 GB drive): adds Qwen3-8B (5.0 GB, noticeably smarter) and a Gemma-3-4B vision model;
16 GB of RAM recommended.</li>
<li><strong>Large</strong> (~173 GB, 256 GB drive): adds Qwen3-30B-A3B-Instruct-2507 (18.6 GB); 32 GB of RAM recommended.</li>
<li>The kit picks the biggest bundled model that fits: model size + about 1 GB of GPU memory, or model size + about
4 GB of RAM, and you can switch models on the home page. Bigger models answer better but more slowly.</li>
<li><strong>Library pictures:</strong> the small kit's WikiMed is the "mini" edition (article introductions, text only), so its
image spaces stay empty; Wikivoyage is the no-pictures edition. The medium kit adds WikiMed and Wikivoyage with images.</li>
</ul>
</section>"""


def copy_screenshots(out, src=SHOTS_DIR):
    """Copy the gallery's images next to index.html (self-hosted: the page loads nothing from other hosts)."""
    dst = os.path.join(out, "screenshots")
    os.makedirs(dst, exist_ok=True)
    for f, _, _ in SCREENSHOTS:
        shutil.copyfile(os.path.join(src, f), os.path.join(dst, f))


def public_base(url):
    url = (url or "").strip()
    if url.endswith("/"):
        url = url[:-1]
    return url if PUBLIC_URL_RE.fullmatch(url) else ""


def fetch_json(url):
    """Parsed JSON at url, or None on 404."""
    req = urllib.request.Request(f"{url}?t={int(time.time())}", headers={"User-Agent": "offline-toolkit-site/1",
                                                                      "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            if not r.geturl().startswith("https://"):
                raise SystemExit(f"{url} was redirected off https")
            return json.loads(r.read(5_000_000))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise SystemExit(f"fetching {url} failed: HTTP {e.code}")


def fetch_index(base, fetch=fetch_json):
    """{"tiers": {tier: versions}} from the three <tier>/index.json files, or None if none exists yet."""
    tiers, found = {}, False
    for tier, _ in TIERS:
        doc = fetch(f"{base}/{tier}/index.json")
        found = found or doc is not None
        tiers[tier] = (doc or {}).get("versions") or []
    return {"tiers": tiers} if found else None


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


def render(index, base, built_at=None, donate=None, notice=None):
    esc = html.escape
    support = render_support(load_donate() if donate is None else donate)
    expect = render_expect(load_notice() if notice is None else notice)
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
.addr {{ display: flex; flex-wrap: wrap; gap: 1rem; align-items: flex-start; margin: 1rem 0; }}
.addr h3 {{ margin: 0; }} .addr p {{ margin: .3rem 0; }}
svg.qr {{ width: 9rem; height: 9rem; flex: none; border: 1px solid var(--line); }}
code.wallet {{ word-break: break-all; user-select: all; }}
button {{ font: inherit; font-size: .85em; margin-left: .4rem; }}
.warn {{ background: #fff3cd; color: #1b1b1b; padding: .8rem; border-radius: 6px; }}
@media (prefers-color-scheme: dark) {{ .warn {{ background: #4a3d10; color: #f3ead2; }} }}
.ai-notice {{ font-size: 1.05rem; }}
.gallery {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 20rem), 1fr)); gap: 1rem; }}
figure {{ margin: 0; }} figure img {{ display: block; width: 100%; height: auto; border: 1px solid var(--line); border-radius: 4px; }}
figcaption {{ color: var(--muted); font-size: .9rem; margin-top: .3rem; }}
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
{expect}
{support}
<p class="note">Files are served from Cloudflare R2. Manifests: <a href="{esc(base, quote=True)}/latest.json">latest.json</a> ·
<a href="{esc(base, quote=True)}/index.json">index.json</a>. Page built {built}.</p>
{COPY_JS if support else ""}
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
    copy_screenshots(a.out)
    with open(os.path.join(a.out, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(render(index, base))
    open(os.path.join(a.out, ".nojekyll"), "w").close()
    n = sum(len(v or []) for v in ((index or {}).get("tiers") or {}).values())
    print(f"wrote {a.out}/index.html ({n} kit version(s) listed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
