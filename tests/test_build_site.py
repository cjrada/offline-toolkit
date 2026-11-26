#!/usr/bin/env python3
"""Tests tools/build_site.py offline: the page lists the latest kit of each tier with size, sha256 and R2
links, previous versions, the repo link and size warnings; bad manifest entries are dropped and escaped.
Usage: python3 tests/test_build_site.py"""
import os, sys, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import build_site as bs  # noqa: E402

BASE = "https://pub-abc.r2.dev"


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
        self.assertNotIn("<script", page)

    def test_empty_and_bad_entries(self):
        page = bs.render(None, BASE)
        self.assertEqual(page.count("Not published yet."), 3)
        bad = entry("small", "v1")
        bad["zip"] = "javascript:alert(1)"
        evil = entry("small", "v2")
        evil["version"] = "<b>"
        page = bs.render({"tiers": {"small": [bad, evil, entry("medium", "v3")]}}, BASE)
        self.assertNotIn("javascript:", page)
        self.assertNotIn("<b>", page)
        self.assertEqual(page.count("Not published yet."), 3)  # a medium entry under small is ignored too

    def test_main_offline(self):
        d = tempfile.mkdtemp()
        idx = os.path.join(d, "index.json")
        with open(idx, "w") as fh:
            fh.write('{"tiers": {"small": []}}')
        self.assertEqual(bs.main(["--public-url", BASE + "/", "--index", idx, "--out", os.path.join(d, "site")]), 0)
        self.assertTrue(os.path.exists(os.path.join(d, "site", "index.html")))
        self.assertTrue(os.path.exists(os.path.join(d, "site", ".nojekyll")))
        self.assertEqual(bs.main(["--public-url", "", "--index", idx, "--out", d]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
