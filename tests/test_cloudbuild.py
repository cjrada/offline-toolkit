#!/usr/bin/env python3
"""Static checks for cloudbuild.yaml and the cache's place in CI. Usage: python3 tests/test_cloudbuild.py

* Every step image is pinned by digest.
* The upload step never execs a bare `python3` (the cloud-sdk image has none on PATH: the 18 Nov 2026
  medium build made a 41.5 GB kit and then lost it at upload). It runs publish_r2.py through
  tools/cloudsdk_python.sh, and the cache-pull step runs the same wrapper and script with the same image
  and the same R2 credentials first (a read-only --dry-run that lists the bucket), so a broken upload, a
  missing secret or a bad token fails in minute one. publish_gcs.py is no longer used for kits.
* R2 credentials come only from Secret Manager (availableSecrets -> secretEnv on cache-pull and upload,
  nowhere else), never from substitutions, and no step echoes them.
* Step order: cache-pull before fetch; cache-push and build both wait for fetch; gate waits for both and
  upload for gate. fetch and build record their exit code instead of failing (Cloud Build would cancel a
  running cache-push), and gate fails the build on a recorded failure, so upload never sees a partial kit.
* GCS cache writes (gcs_cache.sh push) appear only in cloudbuild.yaml, never in a GitHub workflow, and
  the PR workflow (lint.yml) can get no OIDC token, has no environment and no secrets, and there is no
  pull_request_target trigger.
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
    want = ["checkout", "cache-pull", "fetch", "cache-push", "build", "gate", "upload"]
    if order != want:
        return [f"steps must be {want}, got {order}"]
    for sid, c in st.items():
        img = field(c, "name") or ""
        if not re.fullmatch(r"[\w./-]+@sha256:[0-9a-f]{64}", img):
            problems.append(f"{sid}: image not pinned by digest: {img!r}")
    up, pull = st["upload"], st["cache-pull"]
    if field(up, "entrypoint") in ("python3", "python"):
        problems.append("upload: entrypoint python3 is not on PATH in the cloud-sdk image")
    if "tools/cloudsdk_python.sh" not in up or "tools/publish_r2.py" not in up:
        problems.append("upload must run tools/publish_r2.py via tools/cloudsdk_python.sh")
    if "publish_gcs.py" in text:
        problems.append("kits are published to R2: cloudbuild.yaml must not run publish_gcs.py")
    if "--dry-run" in up:
        problems.append("upload must really publish (no --dry-run)")
    if field(up, "name") != field(pull, "name"):
        problems.append("cache-pull (the upload preflight) must use the upload step's image")
    pre = [l.strip() for l in pull.splitlines() if "tools/cloudsdk_python.sh tools/publish_r2.py" in l]
    if not any("--dry-run" in l and "--tier" in l and "--version" in l for l in pre):
        problems.append("cache-pull must preflight publish_r2.py (--tier --version --dry-run: credentials, bucket access, "
                        "version not taken) with tools/cloudsdk_python.sh")
    if pull.find("publish_r2.py --tier") > pull.find("gcs_cache.sh pull") >= 0:
        problems.append("cache-pull must run the R2 preflight before pulling the cache (minute 1)")
    # R2 credentials: Secret Manager only, and only for the two steps that run the publisher.
    secrets = re.search(r"(?ms)^availableSecrets:\n  secretManager:\n(.*?)^\S", text)
    want_env = {"R2_ACCESS_KEY_ID": "r2-access-key-id", "R2_SECRET_ACCESS_KEY": "r2-secret-access-key", "R2_ACCOUNT_ID": "r2-account-id"}
    got = dict((env, sid) for sid, env in re.findall(
        r"versionName: projects/\$PROJECT_ID/secrets/([a-z0-9-]+)/versions/latest\n\s+env: (\w+)", secrets.group(1) if secrets else ""))
    if got != {k: v for k, v in want_env.items()}:
        problems.append(f"availableSecrets must map Secret Manager {sorted(want_env.values())} to {sorted(want_env)}, got {got}")
    for sid, c in st.items():
        se = set(re.findall(r"\w+", field(c, "secretEnv") or ""))
        if sid in ("cache-pull", "upload"):
            if se != set(want_env):
                problems.append(f"{sid}: secretEnv must be exactly {sorted(want_env)}, got {sorted(se)}")
            if "R2_BUCKET=${_R2_BUCKET}" not in (field(c, "env") or ""):
                problems.append(f"{sid}: env must set R2_BUCKET=${{_R2_BUCKET}}")
        elif se:
            problems.append(f"{sid}: only cache-pull and upload may get the R2 secrets")
        for name in want_env:
            if re.search(rf"\$\$\{{?{name}\b", c):
                problems.append(f"{sid}: must not expand ${name} in a command (it would end up in the build log)")
    subs = text.split("\nsubstitutions:\n", 1)[1].split("\n", 1)[1].split("\nserviceAccount:", 1)[0] if "\nsubstitutions:\n" in text else ""
    if re.search(r"(?i)secret|access_key|account_id|token", subs):
        problems.append("substitutions must not carry R2 credentials")
    if "_R2_BUCKET" not in subs:
        problems.append("_R2_BUCKET must be a substitution (non-secret)")
    if "gcs_cache.sh pull" not in pull.replace("gcs_cache.sh, pull", "gcs_cache.sh pull"):
        problems.append("cache-pull must run tools/gcs_cache.sh pull")
    if "push" not in st["cache-push"] or "gcs_cache.sh" not in st["cache-push"]:
        problems.append("cache-push must run tools/gcs_cache.sh push")
    if "waitFor" in st["fetch"] or "waitFor" in st["cache-pull"]:
        problems.append("fetch and cache-pull must run in order (no waitFor)")
    for sid, need in (("cache-push", {"fetch"}), ("build", {"fetch"}), ("gate", {"build", "cache-push"}), ("upload", {"gate"})):
        w = set(re.findall(r"[\w-]+", field(st[sid], "waitFor") or ""))
        if w != need:
            problems.append(f"{sid}: waitFor must be {sorted(need)}, got {sorted(w)}")
    for sid in ("fetch", "build"):
        if "--frozen" not in st[sid]:
            problems.append(f"{sid}: build_kit.py must run with --frozen")
        # Cloud Build cancels running steps when one fails, so these record their code instead of failing...
        if f'echo "$$rc" > /workspace/{sid}.rc' not in st[sid] or "|| rc=$$?" not in st[sid]:
            problems.append(f"{sid}: must record its exit code in /workspace/{sid}.rc (so cache-push is not cancelled)")
    # ...and the gate turns a recorded failure into a failed build before upload.
    for rc in ("fetch.rc", "build.rc"):
        if f"/workspace/{rc}" not in st["gate"]:
            problems.append(f"gate: must check /workspace/{rc}")
    if "exit 1" not in st["gate"]:
        problems.append("gate: must fail the build when fetch or build failed")
    if "allowFailure" in text or "allow_failure" in text:
        problems.append("no allowFailure: upload must never run after a failed build")
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
    if "id-token" in lint or "environment:" in lint or "secrets." in lint:
        problems.append("lint.yml (runs on pull requests) must not get an OIDC token, an environment or secrets")
    if not re.search(r"(?m)^permissions:\n  contents: read\njobs:", lint):
        problems.append("lint.yml permissions must be exactly contents: read")
    for t in ("tests/test_publish_r2.py", "tests/test_build_site.py"):
        if t not in lint:
            problems.append(f"lint.yml must run {t}")
    return problems


def self_test(text):
    assert check(text) == [], check(text)
    bad = re.sub(r"(?m)^(  - id: upload\n(?:    .*\n)*?)    entrypoint: sh\n", r"\1    entrypoint: python3\n", text)
    assert bad != text and any("entrypoint python3" in p for p in check(bad)), "python3 entrypoint not caught"
    bad = text.replace("waitFor: [gate]", "waitFor: [build]")
    assert any("upload: waitFor" in p for p in check(bad)), "upload waitFor not checked"
    bad = text.replace('echo "$$rc" > /workspace/build.rc', 'echo "$$rc"')
    assert any("build.rc" in p for p in check(bad)), "build rc recording not checked"
    bad = re.sub(r"golang@sha256:[0-9a-f]{64}", "golang:1.25-bookworm", text, count=1)
    assert any("not pinned by digest" in p for p in check(bad)), "tag-pinned image not caught"
    bad = text.replace("tools/cloudsdk_python.sh, tools/publish_r2.py", "tools/cloudsdk_python.sh, tools/publish_gcs.py")
    assert any("publish_r2.py via" in p for p in check(bad)), "upload with publish_gcs.py not caught"
    bad = re.sub(r"(?m)^        sh tools/cloudsdk_python.sh tools/publish_r2.py --tier.*\n", "", text)
    assert bad != text and any("preflight publish_r2.py" in p for p in check(bad)), "missing R2 preflight not caught"
    bad = text.replace("    secretEnv: [R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_ACCOUNT_ID]\n", "", 1)
    assert any("cache-pull: secretEnv" in p for p in check(bad)), "cache-pull without secrets not caught"
    bad = text.replace("  _R2_BUCKET: \"\"\n", "  _R2_BUCKET: \"\"\n  _R2_SECRET_ACCESS_KEY: \"\"\n")
    assert any("credentials" in p for p in check(bad)), "secret substitution not caught"
    bad = re.sub(r"(?m)^(  - id: fetch\n)", r"\1    secretEnv: [R2_ACCOUNT_ID]\n", text)
    assert any("only cache-pull and upload" in p for p in check(bad)), "extra secretEnv not caught"
    bad = text.replace("echo \"upload credentials (R2", "echo \"$$R2_SECRET_ACCESS_KEY (R2")
    assert bad != text and any("must not expand" in p for p in check(bad)), "echoed secret not caught"
    bad = text.replace("r2-account-id/versions/latest", "r2-acct/versions/latest")
    assert any("availableSecrets" in p for p in check(bad)), "wrong secret id not caught"


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
