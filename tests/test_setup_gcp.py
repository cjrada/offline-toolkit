#!/usr/bin/env python3
"""Static checks for scripts/setup-gcp.sh. Usage: python3 tests/test_setup_gcp.py

* Every `gcloud ... add-iam-policy-binding` call, including ones continued over several lines with a
  trailing backslash, passes an explicit --condition (or --condition-from-file). Without it gcloud asks
  interactively as soon as the policy holds a conditional binding.
* CLOUDSDK_CORE_DISABLE_PROMPTS=1 is exported before the first gcloud call, so the script never prompts.
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

    if failed:
        sys.exit(1)
    print(f"ok: {found} add-iam-policy-binding calls all pass --condition; prompts disabled")


if __name__ == "__main__":
    main()
