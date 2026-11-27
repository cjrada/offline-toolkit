#!/usr/bin/env python3
"""Static checks for scripts/setup-gcp.sh. Usage: python3 tests/test_setup_gcp.py

* Every `gcloud ... add-iam-policy-binding` call, including ones continued over several lines with a
  trailing backslash, passes an explicit --condition (or --condition-from-file). Without it gcloud asks
  interactively as soon as the policy holds a conditional binding.
* CLOUDSDK_CORE_DISABLE_PROMPTS=1 is exported before the first gcloud call, so the script never prompts.
* Secret Manager (R2 credentials for Cloud Build): the API is enabled; r2-access-key-id,
  r2-secret-access-key and r2-account-id are created if missing; kit-builder, and nobody else, gets
  roles/secretmanager.secretAccessor on each of them (not project-wide). The script never reads, adds or
  prints a secret value: no `versions access`, and `versions add ... --data-file=-` appears only in the
  printed instructions.
"""
import re
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "setup-gcp.sh"
GRANT = re.compile(r"\bgcloud\b[^;&|]*?\badd-iam-policy-binding\b")
CONDITION = re.compile(r"(?<![\w-])--condition(?:=|-from-file[= ]|\s)")


def logical_lines(text):
    """Yield (first_line_number, joined_line) with backslash continuations joined and comments dropped."""
    buf, start = [], None
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw
        if not buf and line.lstrip().startswith("#"):
            continue
        if start is None:
            start = n
        if line.rstrip().endswith("\\"):
            buf.append(line.rstrip()[:-1])
            continue
        buf.append(line)
        yield start, " ".join(s.strip() for s in buf)
        buf, start = [], None
    if buf:
        yield start, " ".join(s.strip() for s in buf)


def grants_without_condition(text):
    found, missing = 0, []
    for n, line in logical_lines(text):
        for m in GRANT.finditer(line):
            found += 1
            # The call runs until the next command separator or redirection of its output.
            call = re.split(r"\s(?:\|\||&&|;|\||>)", line[m.start():], maxsplit=1)[0]
            if not CONDITION.search(call):
                missing.append((n, call))
    return found, missing


def self_test():
    bad = ('run gcloud storage buckets add-iam-policy-binding "gs://b" --member=x \\\n'
           '  --role=roles/storage.objectCreator >/dev/null\n')
    good = ('run gcloud projects add-iam-policy-binding p --member=x \\\n'
            '  --role=r \\\n  --condition=None >/dev/null\n'
            '# gcloud x add-iam-policy-binding in a comment is ignored\n'
            'say "IAM grants (add-iam-policy-binding is idempotent)"\n'
            'run gcloud storage buckets add-iam-policy-binding b --member=x --role=r --condition="$C" >/dev/null\n')
    found, missing = grants_without_condition(bad)
    assert found == 1 and [n for n, _ in missing] == [1], "missed a multi-line grant without --condition"
    assert grants_without_condition(good) == (2, []), grants_without_condition(good)
    # --condition must be on this call, not a later one on the same line.
    two = "gcloud a add-iam-policy-binding x --role=r >/dev/null; gcloud b add-iam-policy-binding y --condition=None"
    assert len(grants_without_condition(two)[1]) == 1, "condition leaked across calls"
    real = SCRIPT.read_text()
    assert secret_manager_problems(real) == [], secret_manager_problems(real)
    for old, new, needle in (
            ("    secretmanager.googleapis.com --project", "    --project", "secretmanager.googleapis.com"),
            ('--member="serviceAccount:$BLD" \\\n    --role=roles/secretmanager', '--member="serviceAccount:$PUB" \\\n    --role=roles/secretmanager', "kit-builder"),
            ("done\n\nWIF=", 'done\nprintf x | gcloud secrets versions add r2-account-id --data-file=-\n\nWIF=', "must not run"),
            ("--project=$PROJECT_ID --data-file=-\n   gcloud secrets versions add r2-account-id", "\n   gcloud secrets versions add r2-account-id", "stdin")):
        assert real.count(old) == 1, old
        assert any(needle in p for p in secret_manager_problems(real.replace(old, new))), needle


SECRET_IDS = ("r2-access-key-id", "r2-secret-access-key", "r2-account-id")


def secret_manager_problems(text):
    problems = []
    code = list(logical_lines(text))
    joined = "\n".join(l for _, l in code)
    if not re.search(r"gcloud services enable [^\n]*\bsecretmanager\.googleapis\.com", joined):
        problems.append("setup-gcp.sh must enable secretmanager.googleapis.com")
    m = re.search(r'R2_SECRET_IDS="([^"]*)"', text)
    if not m or tuple(m.group(1).split()) != SECRET_IDS:
        problems.append(f"setup-gcp.sh must manage exactly the secrets {SECRET_IDS}")
    if not re.search(r'gcloud secrets create "\$sec"', joined):
        problems.append("setup-gcp.sh must create the R2 secrets if missing")
    grants = [l for _, l in code if "gcloud secrets add-iam-policy-binding" in l]
    if len(grants) != 1 or '--member="serviceAccount:$BLD"' not in grants[0] or "--role=roles/secretmanager.secretAccessor" not in grants[0]:
        problems.append(f"exactly one secret grant: secretAccessor to kit-builder ($BLD) on each secret, got {grants}")
    if re.search(r"gcloud projects add-iam-policy-binding[^\n]*secretmanager", joined):
        problems.append("Secret Manager access must be granted per secret, not project-wide")
    if "$PUB" in "".join(grants):
        problems.append("kit-publisher must not get the R2 secrets")
    heredoc = re.compile(r"(?ms)^cat <<EOF\n.*?^EOF$")
    outside = heredoc.sub("", text)
    for bad in ("versions access", "versions add"):
        for n, l in logical_lines(outside):
            if bad in l:
                problems.append(f"line {n}: the script must not run `gcloud secrets {bad}` (values are added by hand)")
    printed = "".join(heredoc.findall(text))
    for sid in SECRET_IDS:
        if not re.search(rf"gcloud secrets versions add {sid}\s+--project=\$PROJECT_ID --data-file=-", printed):
            problems.append(f"the printed instructions must show `gcloud secrets versions add {sid} ... --data-file=-` (stdin)")
    return problems


def main():
    self_test()
    text = SCRIPT.read_text()
    failed = False

    found, missing = grants_without_condition(text)
    if found == 0:
        print("FAIL: no add-iam-policy-binding calls found; the check is not seeing the script")
        failed = True
    for n, call in missing:
        print(f"FAIL: {SCRIPT.name}:{n}: add-iam-policy-binding without an explicit --condition: {call}")
        failed = True

    lines = text.splitlines()
    export = next((i for i, l in enumerate(lines) if re.match(r"\s*export\s+CLOUDSDK_CORE_DISABLE_PROMPTS=1\s*(#.*)?$", l)), None)
    first_gcloud = next((i for i, l in enumerate(lines) if not l.lstrip().startswith("#") and re.search(r"\bgcloud\b", l)), None)
    if export is None or (first_gcloud is not None and export > first_gcloud):
        print("FAIL: export CLOUDSDK_CORE_DISABLE_PROMPTS=1 must come before the first gcloud call")
        failed = True

    for problem in secret_manager_problems(text):
        print("FAIL:", problem)
        failed = True

    if failed:
        sys.exit(1)
    print(f"ok: {found} add-iam-policy-binding calls all pass --condition; prompts disabled")


if __name__ == "__main__":
    main()
