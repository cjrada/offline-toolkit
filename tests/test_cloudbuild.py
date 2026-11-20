#!/usr/bin/env python3
"""Static checks for cloudbuild.yaml and the cache's place in CI. Usage: python3 tests/test_cloudbuild.py

* Every step image is pinned by digest.
* The upload step never execs a bare `python3` (the cloud-sdk image has none on PATH: the 18 Nov 2026
  medium build made a 41.5 GB kit and then lost it at upload). It runs publish_gcs.py through
  tools/cloudsdk_python.sh, and the cache-pull step runs the same wrapper and script with the same image
  first, so a broken upload fails in minute one.
* Step order: cache-pull before fetch; cache-push and build both wait for fetch; upload waits for both.
* GCS cache writes (gcs_cache.sh push) appear only in cloudbuild.yaml, never in a GitHub workflow, and
  the PR workflow (lint.yml) can get no OIDC token and has no pull_request_target trigger.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CB = ROOT / "cloudbuild.yaml"


def steps(text):
    """Split the steps: list into {id: text}; keeps order."""
    body = text.split("\nsteps:\n", 1)[1]
    out = {}
    for chunk in re.split(r"(?m)^  - (?=id: )", body)[1:]:
        sid = re.match(r"id: ([\w-]+)", chunk).group(1)
        out[sid] = chunk
    return out


def field(chunk, name):
    m = re.search(rf"(?m)^    {name}: (.*)$", chunk)
    return m.group(1).split(" #")[0].strip() if m else None


def check(text):
    problems = []
    st = steps(text)
    order = list(st)
    want = ["checkout", "cache-pull", "fetch", "cache-push", "build", "upload"]
    if order != want:
        return [f"steps must be {want}, got {order}"]
    for sid, c in st.items():
        img = field(c, "name") or ""
        if not re.fullmatch(r"[\w./-]+@sha256:[0-9a-f]{64}", img):
            problems.append(f"{sid}: image not pinned by digest: {img!r}")
    up, pull = st["upload"], st["cache-pull"]
    if field(up, "entrypoint") in ("python3", "python"):
        problems.append("upload: entrypoint python3 is not on PATH in the cloud-sdk image")
    if "tools/cloudsdk_python.sh" not in up or "tools/publish_gcs.py" not in up:
        problems.append("upload must run tools/publish_gcs.py via tools/cloudsdk_python.sh")
    if field(up, "name") != field(pull, "name"):
        problems.append("cache-pull (the upload preflight) must use the upload step's image")
    if "tools/cloudsdk_python.sh tools/publish_gcs.py" not in pull:
        problems.append("cache-pull must preflight publish_gcs.py with tools/cloudsdk_python.sh")
    if "gcs_cache.sh pull" not in pull.replace("gcs_cache.sh, pull", "gcs_cache.sh pull"):
        problems.append("cache-pull must run tools/gcs_cache.sh pull")
    if "push" not in st["cache-push"] or "gcs_cache.sh" not in st["cache-push"]:
        problems.append("cache-push must run tools/gcs_cache.sh push")
    if "waitFor" in st["fetch"] or "waitFor" in st["cache-pull"]:
        problems.append("fetch and cache-pull must run in order (no waitFor)")
    for sid, need in (("cache-push", {"fetch"}), ("build", {"fetch"}), ("upload", {"build", "cache-push"})):
        w = set(re.findall(r"[\w-]+", field(st[sid], "waitFor") or ""))
        if w != need:
            problems.append(f"{sid}: waitFor must be {sorted(need)}, got {sorted(w)}")
    for sid in ("fetch", "build"):
        if "--frozen" not in st[sid]:
            problems.append(f"{sid}: build_kit.py must run with --frozen")
    return problems


def check_workflows():
    problems = []
    for wf in sorted((ROOT / ".github" / "workflows").glob("*.y*ml")):
        t = "\n".join(l for l in wf.read_text(encoding="utf-8").splitlines() if not l.lstrip().startswith("#"))
        if "tools/gcs_cache.sh" in t or re.search(r"gs://[^\s]*cache/", t):
            problems.append(f"{wf.name}: GCS cache access belongs only in cloudbuild.yaml")
        if "pull_request_target" in t:
            problems.append(f"{wf.name}: pull_request_target trigger")
        for m in re.finditer(r"(?m)^\s*-?\s*uses:\s*(\S+)", t):
            if not m.group(1).startswith("./") and not re.search(r"@[0-9a-f]{40}$", m.group(1)):
                problems.append(f"{wf.name}: action not pinned to a full SHA: {m.group(1)}")
    lint = (ROOT / ".github" / "workflows" / "lint.yml").read_text(encoding="utf-8")
    if "id-token" in lint or "environment:" in lint:
        problems.append("lint.yml (runs on pull requests) must not get an OIDC token or an environment")
    return problems


def self_test(text):
    assert check(text) == [], check(text)
    bad = re.sub(r"(?m)^(  - id: upload\n(?:    .*\n)*?)    entrypoint: sh\n", r"\1    entrypoint: python3\n", text)
    assert bad != text and any("entrypoint python3" in p for p in check(bad)), "python3 entrypoint not caught"
    bad = text.replace("waitFor: [build, cache-push]", "waitFor: [build]")
    assert any("upload: waitFor" in p for p in check(bad)), "upload waitFor not checked"
    bad = re.sub(r"golang@sha256:[0-9a-f]{64}", "golang:1.25-bookworm", text, count=1)
    assert any("not pinned by digest" in p for p in check(bad)), "tag-pinned image not caught"


def main():
    text = CB.read_text(encoding="utf-8")
    self_test(text)
    problems = check(text) + check_workflows()
    for p in problems:
        print("FAIL:", p)
    if problems:
        return 1
    print("OK: cloudbuild.yaml steps pinned and ordered, upload uses cloudsdk_python.sh, cache writes only in Cloud Build")
    return 0


if __name__ == "__main__":
    sys.exit(main())
