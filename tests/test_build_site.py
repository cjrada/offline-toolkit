#!/usr/bin/env python3
"""Tests tools/build_site.py offline: the page lists the latest kit of each tier with size, sha256 and R2
links, previous versions, the repo link and size warnings; bad manifest entries are dropped and escaped. The
Support hosting section shows the tools/donate.json addresses exactly, with QR codes that this file decodes with
its own reader (stdlib) back to those exact strings; the page loads nothing from any other host.
Usage: python3 tests/test_build_site.py"""
import json, os, re, sys, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import build_site as bs  # noqa: E402

BASE = "https://pub-abc.r2.dev"
BTC = "bc1qeka2tvn0rzyxulqhqqluvd2lcdz3x5q56uwav6"
ETH = "0xe3A1aa18Fbc704DDA7A7e0A465AaF09aE11009ab"


# --- an independent QR reader: SVG path -> module grid -> format info -> unmask -> RS check -> byte-mode text ----
M_BLOCKS = {1: (1, 10), 2: (1, 16), 3: (1, 26), 4: (2, 18), 5: (2, 24), 6: (4, 16)}  # ISO/IEC 18004 table 9, level M
ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34]}
MASKS = {0: lambda r, c: (r + c) % 2 == 0, 1: lambda r, c: r % 2 == 0, 2: lambda r, c: c % 3 == 0,
         3: lambda r, c: (r + c) % 3 == 0, 4: lambda r, c: (r // 2 + c // 3) % 2 == 0,
         5: lambda r, c: (r * c) % 2 + (r * c) % 3 == 0, 6: lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
         7: lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0}


def svg_grid(svg):
    side = int(re.search(r'viewBox="0 0 (\d+) \1"', svg).group(1))
    n = side - 8
    g = [[False] * n for _ in range(n)]
    d = re.search(r'<path fill="#000" d="([^"]*)"', svg).group(1)
    segs = re.findall(r"M(\d+) (\d+)h(\d+)v1h-\3z", d)
    assert "".join(f"M{x} {y}h{w}v1h-{w}z" for x, y, w in segs) == d, "unexpected path data"
    for x, y, w in segs:
        for i in range(int(w)):
            g[int(y) - 4][int(x) - 4 + i] = True
    return g


def gf_mul(a, b):
    r = 0
    while b:
        if b & 1:
            r ^= a
        a <<= 1
        if a & 0x100:
            a ^= 0x11D
        b >>= 1
    return r


def qr_read(g):
    n = len(g)
    v = (n - 17) // 4
    assert n == 4 * v + 17 and v in M_BLOCKS, n
    # format info, both copies (MSB first along the standard paths)
    a = [g[8][c] for c in (0, 1, 2, 3, 4, 5, 7, 8)] + [g[r][8] for r in (7, 5, 4, 3, 2, 1, 0)]
    b = [g[r][8] for r in range(n - 1, n - 8, -1)] + [g[8][c] for c in range(n - 8, n)]
    assert g[n - 8][8], "dark module"
    word = int("".join("1" if x else "0" for x in a), 2)
    assert word == int("".join("1" if x else "0" for x in b), 2), "format copies differ"
    codes = {}
    for data in range(32):
        rem = data << 10
        for i in range(14, 9, -1):
            if rem >> i & 1:
                rem ^= 0x537 << (i - 10)
        codes[((data << 10) | rem) ^ 0x5412] = data
    data = codes[word]
    assert data >> 3 == 0, "error correction level is not M"
    mask = MASKS[data & 7]
    # function modules: finders + separators + format areas, timing, alignment
    fn = [[False] * n for _ in range(n)]
    for r in range(n):
        for c in range(n):
            if (r < 9 and c < 9) or (r < 9 and c >= n - 8) or (r >= n - 8 and c < 9) or r == 6 or c == 6:
                fn[r][c] = True
    for ar in ALIGN[v]:
        for ac in ALIGN[v]:
            if not fn[ar][ac]:
                for r in range(ar - 2, ar + 3):
                    for c in range(ac - 2, ac + 3):
                        fn[r][c] = True
    bits, col, up = [], n - 1, True
    while col > 0:
        if col == 6:
            col -= 1
        for r in (range(n - 1, -1, -1) if up else range(n)):
            for c in (col, col - 1):
                if not fn[r][c]:
                    bits.append(g[r][c] ^ mask(r, c))
        up, col = not up, col - 2
    words = [int("".join("1" if x else "0" for x in bits[i:i + 8]), 2) for i in range(0, len(bits) - 7, 8)]
    nb, ne = M_BLOCKS[v]
    total = len(words) // nb
    nd = total - ne
    blocks = [[words[i * nb + k] for i in range(nd)] + [words[nd * nb + i * nb + k] for i in range(ne)] for k in range(nb)]
    for blk in blocks:  # a valid RS codeword has every syndrome S_i = C(alpha^i) == 0
        alpha = 1
        for _ in range(ne):
            acc = 0
            for cw in blk:
                acc = gf_mul(acc, alpha) ^ cw
            assert acc == 0, "Reed-Solomon syndrome is not zero"
            alpha = gf_mul(alpha, 2)
    stream = "".join(f"{w:08b}" for blk in blocks for w in blk[:nd])
    assert stream[:4] == "0100", "not byte mode"
    count = int(stream[4:12], 2)
    return bytes(int(stream[12 + 8 * i:20 + 8 * i], 2) for i in range(count)).decode("utf-8")


# --- address checksums, so a typo in tools/donate.json fails here -----------------------------------------------
def bech32_ok(addr):
    charset = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
    hrp, _, data = addr.rpartition("1")
    if addr.lower() != addr or hrp != "bc" or any(ch not in charset for ch in data):
        return False
    chk = 1
    for x in [ord(ch) >> 5 for ch in hrp] + [0] + [ord(ch) & 31 for ch in hrp] + [charset.index(ch) for ch in data]:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ x
        for i, gen in enumerate((0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)):
            chk ^= gen if top >> i & 1 else 0
    return chk == 1  # BIP-173 (bech32, segwit v0)


def keccak256(msg):
    rol = lambda a, k: ((a << k) | (a >> (64 - k))) & (2 ** 64 - 1) if k else a  # noqa: E731
    msg = bytearray(msg) + b"\x01"
    msg += b"\x00" * (-len(msg) % 136)
    msg[-1] |= 0x80
    lanes = [[0] * 5 for _ in range(5)]
    for off in range(0, len(msg), 136):
        for i in range(17):
            lanes[i % 5][i // 5] ^= int.from_bytes(msg[off + 8 * i:off + 8 * i + 8], "little")
        R = 1
        for _ in range(24):
            C = [lanes[x][0] ^ lanes[x][1] ^ lanes[x][2] ^ lanes[x][3] ^ lanes[x][4] for x in range(5)]
            D = [C[(x + 4) % 5] ^ rol(C[(x + 1) % 5], 1) for x in range(5)]
            lanes = [[lanes[x][y] ^ D[x] for y in range(5)] for x in range(5)]
            x, y, cur = 1, 0, lanes[1][0]
            for t in range(24):
                x, y = y, (2 * x + 3 * y) % 5
                cur, lanes[x][y] = lanes[x][y], rol(cur, (t + 1) * (t + 2) // 2 % 64)
            for y in range(5):
                T = [lanes[x][y] for x in range(5)]
                for x in range(5):
                    lanes[x][y] = T[x] ^ (~T[(x + 1) % 5] & T[(x + 2) % 5])
            for j in range(7):
                R = ((R << 1) ^ ((R >> 7) * 0x71)) % 256
                if R & 2:
                    lanes[0][0] ^= 1 << ((1 << j) - 1)
    return b"".join(lanes[i % 5][i // 5].to_bytes(8, "little") for i in range(4))


def eip55_ok(addr):
    body = addr[2:]
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", addr):
        return False
    h = keccak256(body.lower().encode()).hex()
    return body == "".join(ch.upper() if int(h[i], 16) >= 8 else ch for i, ch in enumerate(body.lower()))


def entry(tier, v, size=7_000_000_000, sha="a" * 64):
    z = f"{tier}/{v}/offline-toolkit-{tier}-{v}.zip"
    return {"tier": tier, "version": v, "published_at": "2026-11-26T01:02:03Z", "size": size, "sha256": sha,
            "zip": z, "sha256_file": z + ".sha256", "kit_info": f"{tier}/{v}/KIT-INFO.json"}


class Page(unittest.TestCase):
    def test_render(self):
        idx = {"tiers": {"small": [entry("small", "v3"), entry("small", "v2"), entry("small", "v1")],
                         "medium": [entry("medium", "m2", 41_000_000_000), entry("medium", "m1")],
                         "large": [entry("large", "l1", 173_000_000_000, "b" * 64)]}}
        page = bs.render(idx, BASE, built_at="x")
        self.assertIn(f'href="{BASE}/small/v3/offline-toolkit-small-v3.zip"', page)
        self.assertIn(f'href="{BASE}/small/v3/offline-toolkit-small-v3.zip.sha256"', page)
        self.assertIn(f'href="{BASE}/large/l1/offline-toolkit-large-l1.zip"', page)
        self.assertIn("b" * 64, page)
        self.assertIn("173.0 GB", page)
        self.assertIn("2026-11-26", page)
        self.assertIn("https://github.com/cjrada/offline-toolkit", page)
        for s in ("~7 GB", "~41 GB", "~173 GB"):
            self.assertIn(s, page)
        large = page.split('id="large"')[1]
        self.assertNotIn("Previous versions", large)
        small = page.split('id="small"')[1].split('id="medium"')[0]
        self.assertEqual(small.count("<li>"), 2)
        medium = page.split('id="medium"')[1].split('id="large"')[0]
        self.assertEqual(medium.count("<li>"), 1)
        self.assert_no_third_party(page)

    def assert_no_third_party(self, page):
        """Nothing on the page is loaded from another host: no src= at all, no <link>/@import/url(), one inline
        script with no URLs in it, every href is the repo, the R2 base or a gallery image, and the only src= are
        the gallery's own screenshots/*.webp next to index.html."""
        srcs = re.findall(r"(?i)\ssrc\s*=\s*\"?([^\"\s>]*)", page)
        self.assertEqual(sorted(srcs), sorted(f"screenshots/{f}" for f, _, _ in bs.SCREENSHOTS))
        self.assertEqual(len(re.findall(r"(?i)<img\b", page)), len(bs.SCREENSHOTS))
        self.assertNotRegex(page, r"(?i)<link|@import|url\(|<iframe|<object|<embed|<source|srcset")
        self.assertNotIn("javascript:", page)
        scripts = re.findall(r"(?is)<script([^>]*)>(.*?)</script>", page)
        self.assertEqual(len(scripts), 1, "only the inline copy helper")
        self.assertEqual(scripts[0][0], "")
        self.assertNotRegex(scripts[0][1], r"(?i)https?:|//[a-z]|fetch\(|XMLHttpRequest|import\(|\.src\b")
        self.assertIn("navigator.clipboard.writeText", scripts[0][1])
        for href in re.findall(r'href="([^"]*)"', page):
            self.assertTrue(href.startswith((BASE + "/", "https://github.com/cjrada/offline-toolkit"))
                            or re.fullmatch(r"screenshots/kit-[a-z-]+\.webp", href), href)
        for svg in re.findall(r"(?s)<svg.*?</svg>", page):
            self.assertEqual(re.findall(r"https?://[^\s\"]*", svg), ["http://www.w3.org/2000/svg"])  # xmlns only

    def test_support_section(self):
        page = bs.render(None, BASE, built_at="x")
        self.assertEqual(page.count('<section id="support">'), 1)
        self.assertIn("<h2>Support hosting</h2>", page)
        self.assertIn("Cloudflare R2", page.split('id="support"')[1])
        self.assertLess(page.index('id="large"'), page.index('id="support"'))  # below the downloads
        self.assertIn("Ethereum network (mainnet)", page)
        self.assertIn("USDC (ERC-20)", page)
        # the addresses, character for character, as selectable text with a copy button (hidden until JS runs)
        self.assertIn(f'<code class="wallet" id="addr-btc">{BTC}</code>', page)
        self.assertIn(f'<code class="wallet" id="addr-eth">{ETH}</code>', page)
        self.assertIn('<button type="button" data-copy="addr-btc" hidden>Copy</button>', page)
        self.assertIn('<button type="button" data-copy="addr-eth" hidden>Copy</button>', page)
        # one QR per address, and each decodes to exactly that address
        svgs = re.findall(r"(?s)<svg class=\"qr\".*?</svg>", page)
        self.assertEqual([qr_read(svg_grid(x)) for x in svgs], [BTC, ETH])
        self.assert_no_third_party(page)

    def test_expect_section(self):
        """What to expect: below the downloads and above Support hosting, the shared AI notice, the six
        screenshots (lazy, sized, described), honest speed notes and the hardware figures from the README."""
        idx = {"tiers": {"small": [entry("small", "v3")]}}
        page = bs.render(idx, BASE, built_at="x")
        self.assertEqual(page.count('<section id="expect">'), 1)
        self.assertIn("<h2>What to expect</h2>", page)
        self.assertLess(page.index('id="large"'), page.index('id="expect"'))   # downloads stay first
        self.assertLess(page.index('id="expect"'), page.index('id="support"'))
        self.assertLess(page.index(f'href="{BASE}/small/v3/offline-toolkit-small-v3.zip"'), page.index('id="expect"'))
        sec = page.split('<section id="expect">')[1].split("</section>")[0]
        # the notice: the same text as the kit pages (tools/ai-notice.json), always visible (not in <details>)
        with open(bs.NOTICE_JSON, encoding="utf-8") as fh:
            notice = json.load(fh)["sources"]
        self.assertIn(f'<p class="warn ai-notice" role="note">{notice}</p>', sec)
        for s in ("AI can make mistakes.", "Always cross-check answers with the linked sources.",
                  "Sources can be wrong or out of date too, but they're more reliable than the AI alone."):
            self.assertIn(s, notice)
        self.assertNotIn("<details", sec)
        imgs = re.findall(r"<img [^>]*>", sec)
        self.assertEqual(len(imgs), 6)
        for (f, alt, cap), img in zip(bs.SCREENSHOTS, imgs):
            self.assertIn(f'src="screenshots/{f}"', img)
            self.assertIn('width="1440" height="900"', img)
            self.assertIn('loading="lazy"', img)
            m = re.search(r'alt="([^"]+)"', img)
            self.assertTrue(m and len(m.group(1)) > 30, img)
            self.assertIn(f"<figcaption>{bs.html.escape(cap)}</figcaption>", sec)
            self.assertTrue(os.path.exists(os.path.join(bs.SHOTS_DIR, f)), f)
        names = ["kit-home", "kit-ask-the-manuals", "kit-ai-chat", "kit-library", "kit-wikivoyage-brisbane", "kit-map-brisbane"]
        self.assertEqual([f for f, _, _ in bs.SCREENSHOTS], [n + ".webp" for n in names])  # home first, then Ask the Manuals
        for f in names:
            self.assertIn(f"screenshots/{f}.webp", sec)
        self.assertIn("AI can make mistakes", dict((f, a) for f, a, _ in bs.SCREENSHOTS)["kit-ask-the-manuals.webp"])
        for s in ("7-14 tokens per second", "1-2.5 minutes", "Ask the Manuals answer about a\nminute", "8-core CPU",
                  "much faster", "Qwen3-4B-Instruct-2507", "Qwen3-8B (5.0 GB", "Qwen3-30B-A3B-Instruct-2507 (18.6 GB)",
                  "8 GB of RAM", "16 GB of RAM", "32 GB of RAM", '"mini" edition'):
            self.assertIn(s, sec)
        self.assert_no_third_party(page)

    def test_screenshots_stay_out_of_the_kit(self):
        """site/ is for the website only: build_kit.py packs only KIT_CODE, which does not include it."""
        sys.path.insert(0, os.path.dirname(bs.__file__))
        src = open(os.path.join(os.path.dirname(bs.__file__), "build_kit.py"), encoding="utf-8").read()
        kit_code = re.search(r"^KIT_CODE = (\[.*\])$", src, re.M).group(1)
        self.assertNotIn("site", kit_code)
        self.assertTrue(bs.SHOTS_DIR.endswith(os.path.join("site", "screenshots")))

    def test_donate_config(self):
        """tools/donate.json is the one place the addresses live, and both checksums are valid."""
        with open(bs.DONATE_JSON, encoding="utf-8") as fh:
            raw = json.load(fh)
        self.assertEqual([a["address"] for a in raw["addresses"]], [BTC, ETH])
        self.assertEqual([a["address"] for a in bs.load_donate()], [BTC, ETH])
        self.assertTrue(bech32_ok(BTC))
        self.assertFalse(bech32_ok(BTC[:-1] + "7"))
        self.assertEqual(keccak256(b"").hex(), "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470")
        self.assertTrue(eip55_ok("0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"))  # EIP-55 test vector
        self.assertTrue(eip55_ok(ETH))
        self.assertFalse(eip55_ok(ETH.lower()))
        self.assertFalse(eip55_ok(ETH[:2] + ETH[2:].swapcase()))
        # the addresses are not in the generator's markup, only in the config
        with open(bs.__file__, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn(BTC, src)
        self.assertNotIn(ETH, src)
        # a bad entry fails the build instead of rendering
        d = tempfile.mkdtemp()
        for bad in ({"id": "x", "label": "X", "address": "bc1 q"}, {"id": "<x>", "label": "X", "address": BTC},
                    {"id": "x", "label": "", "address": BTC}):
            path = os.path.join(d, "donate.json")
            with open(path, "w") as fh:
                json.dump({"addresses": [bad]}, fh)
            with self.assertRaises(SystemExit):
                bs.load_donate(path)
        self.assertNotIn('id="support"', bs.render(None, BASE, donate=[]))
        self.assertNotIn("<script", bs.render(None, BASE, donate=[]))

    def test_qr_round_trip(self):
        """The encoder against the reader across every version it supports (1-6) and every mask."""
        for text in ("a", "x" * 14, "y" * 15, "z" * 42, "0xAbC" * 12, "q" * 106, "\u00e9t\u00e9"):
            self.assertEqual(qr_read(svg_grid(bs.qr_svg(text, "t"))), text)
        orig = bs._penalty
        try:
            for m in range(8):
                calls = iter(range(8))
                bs._penalty = lambda g, m=m, calls=calls: 0 if next(calls) == m else 1
                self.assertEqual(qr_read(svg_grid(bs.qr_svg(ETH, "t"))), ETH)
        finally:
            bs._penalty = orig
        with self.assertRaises(ValueError):
            bs.qr_matrix("x" * 107)

    def test_empty_and_bad_entries(self):
        page = bs.render(None, BASE)
        self.assertEqual(page.count("Not published yet."), 3)
        bad = entry("small", "v1")
        bad["zip"] = "javascript:alert(1)"
        evil = entry("small", "v2")
        evil["version"] = "<b>"
        page = bs.render({"tiers": {"small": [bad, evil, entry("medium", "v3")]}}, BASE)
        self.assertNotIn("javascript:", page)
        self.assertNotIn("<b>", page.replace(bs.load_notice(), ""))  # the notice's own <b> aside
        self.assertEqual(page.count("Not published yet."), 3)  # a medium entry under small is ignored too

    def test_fetch_reads_the_tier_indexes(self):
        docs = {f"{BASE}/small/index.json": {"versions": [entry("small", "v2")]},
                f"{BASE}/large/index.json": {"versions": [entry("large", "l1")]}}
        seen = []
        idx = bs.fetch_index(BASE, fetch=lambda u: seen.append(u) or docs.get(u))
        self.assertEqual(seen, [f"{BASE}/{t}/index.json" for t in ("small", "medium", "large")])
        self.assertEqual([e["version"] for e in idx["tiers"]["small"]], ["v2"])
        self.assertEqual(idx["tiers"]["medium"], [])
        self.assertIsNone(bs.fetch_index(BASE, fetch=lambda u: None))

    def test_main_offline(self):
        d = tempfile.mkdtemp()
        idx = os.path.join(d, "index.json")
        with open(idx, "w") as fh:
            fh.write('{"tiers": {"small": []}}')
        self.assertEqual(bs.main(["--public-url", BASE + "/", "--index", idx, "--out", os.path.join(d, "site")]), 0)
        self.assertTrue(os.path.exists(os.path.join(d, "site", "index.html")))
        self.assertTrue(os.path.exists(os.path.join(d, "site", ".nojekyll")))
        self.assertEqual(sorted(os.listdir(os.path.join(d, "site"))), [".nojekyll", "index.html", "screenshots"])  # QRs are inline
        # the gallery's images are copied next to index.html, byte for byte, and every <img> points at one
        shots = sorted(os.listdir(os.path.join(d, "site", "screenshots")))
        self.assertEqual(shots, sorted(f for f, _, _ in bs.SCREENSHOTS))
        for f in shots:
            with open(os.path.join(d, "site", "screenshots", f), "rb") as a, open(os.path.join(bs.SHOTS_DIR, f), "rb") as b:
                data = a.read()
                self.assertEqual(data, b.read())
            self.assertEqual(data[:4] + data[8:12], b"RIFFWEBP", f)
            self.assertLess(len(data), 200_000, f)
        with open(os.path.join(d, "site", "index.html"), encoding="utf-8") as fh:
            page = fh.read()
        self.assertIn(BTC, page)
        self.assertIn(ETH, page)
        self.assertEqual(len(re.findall(r'<svg class="qr"', page)), 2)
        self.assertEqual(bs.main(["--public-url", "", "--index", idx, "--out", d]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
