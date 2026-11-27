#!/usr/bin/env python3
"""Rebuild www/docs/index.js from the PDFs in www/docs/pdf.
Needs: python3 (stdlib only) + pdftotext (poppler-utils).
Not needed to USE the kit - only to rebake the built-in index.
(You can also add PDFs straight from the Docs page in the browser, no Python.)"""
import json, os, re, subprocess, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PDF_DIR = os.path.join(ROOT, "www", "docs", "pdf")
OUT = os.path.join(ROOT, "www", "docs", "index.js")
# Display titles of the PDFs. Kept in tools/, not next to the PDFs, so the PDF shelf (a directory listing of
# www/docs/pdf/) shows only PDFs. An old www/docs/pdf/titles.json is still read, and wins, if present.
titles = {}
for TITLES_FILE in (os.path.join(ROOT, "tools", "pdf-titles.json"), os.path.join(PDF_DIR, "titles.json")):
    if os.path.exists(TITLES_FILE):
        with open(TITLES_FILE, encoding="utf-8") as fh:
            titles.update(json.load(fh))
CHUNK, OVERLAP = 900, 150

def pages(path):
    txt = subprocess.run(["pdftotext", "-layout", path, "-"], capture_output=True, text=True).stdout
    return txt.split("\f")

def chunks(text):
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\.{4,}", " ", text)  # TOC leaders
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    i = 0
    while i < len(text):
        j = min(len(text), i + CHUNK)
        if j < len(text):
            k = text.rfind(" ", i + CHUNK - 200, j)
            j = k if k > i else j
        yield text[i:j].strip()
        if j >= len(text): break
        i = j - OVERLAP

NOTES_DIR = os.path.join(ROOT, "www", "docs", "notes")
docs, out = [], []
# Plain-text/markdown notes (e.g. Australian first aid). Sections become chunks; url points at the note.
if os.path.isdir(NOTES_DIR):
    for fn in sorted(os.listdir(NOTES_DIR)):
        if not fn.lower().endswith((".md", ".txt")): continue
        body = open(os.path.join(NOTES_DIR, fn), encoding="utf-8").read()
        title = body.splitlines()[0].lstrip("# ").strip()
        docs.append({"file": fn, "title": title})
        for sec in re.split(r"\n(?=## )", body)[1:]:
            head = sec.splitlines()[0].lstrip("# ").strip()
            for c in chunks(sec):
                out.append({"f": fn, "p": head, "t": c, "u": "notes/" + fn, "boost": 1.6})
        print(f"{fn}: {sum(1 for o in out if o['f']==fn)} chunks", file=sys.stderr)
for fn in sorted(os.listdir(PDF_DIR)) if os.path.isdir(PDF_DIR) else []:
    if not fn.lower().endswith(".pdf"): continue
    title = titles.get(fn, fn[:-4])
    docs.append({"file": fn, "title": title})
    for pno, ptxt in enumerate(pages(os.path.join(PDF_DIR, fn)), 1):
        for c in chunks(ptxt):
            if len(c) > 80:
                out.append({"f": fn, "p": pno, "t": c})
    print(f"{fn}: {sum(1 for o in out if o['f']==fn)} chunks", file=sys.stderr)
with open(OUT, "w") as fh:
    fh.write("window.DOCS_INDEX = ")
    json.dump({"docs": docs, "chunks": out}, fh, ensure_ascii=False)
    fh.write(";\n")
print(f"wrote {OUT}: {len(out)} chunks from {len(docs)} PDFs", file=sys.stderr)
