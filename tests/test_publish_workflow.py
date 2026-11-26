#!/usr/bin/env python3
"""Static checks for .github/workflows/publish.yml. Usage: python3 tests/test_publish_workflow.py

Offline and cheap: parses the workflow file, runs nothing, touches no artifacts or cloud.

* The build job hands the small-kit zip to the publish job with actions/upload-artifact and
  `archive: false` (the zip itself is the artifact; no zip-in-zip).
* The publish job's actions/download-artifact step sets `skip-decompress: true`. An `archive: false` zip
  is served as application/zip, and download-artifact extracts that by default, which leaves the kit's
  contents instead of the one zip whose sha256 the publish job checks.
* The two steps are tied together: the build job's artifact_id output comes from that upload step, and the
  download step fetches needs.build.outputs.artifact_id.
* The posture around the hand-off holds: both actions are pinned to a full commit SHA, the build job has
  `contents: read` only and no environment, the publish job runs in `gcs-publish`, and the workflow has
  no pull_request_target trigger.
* Gating and visibility (check_gating_and_visibility, run on the real file): the build/publish `if:`
  conditions, permissions and concurrency are exactly as reviewed; dry_run defaults to true; every action
  is SHA-pinned; only the publish job has an environment or id-token. The run list shows tier, version and
  dry run vs publish (run-name). Build and publish write a step summary even on failure; the Cloud Build id
  is recorded by its own step before the wait (project checked before submitting). The auth, gcloud,
  upload, cloudbuild, cbwait and result steps say if: success() && ... explicitly. A `summary` job runs with if: always() (not limited to
  main / v*, so it explains skipped runs), with no environment, no permissions, no secrets, no checkout.

Uses a small parser for the block-style YAML subset GitHub workflows use (no PyYAML needed on the
runner). It raises on anything it does not understand, so an unexpected layout fails the test instead of
silently passing.
"""
import re
import sys
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "publish.yml"
SHA_PIN = re.compile(r"@[0-9a-f]{40}$")
KEY = re.compile(r"""^("[^"]*"|'[^']*'|[^\s"'#\-][^:#]*?|-[^\s:#][^:#]*?)\s*:(?:\s+(.*))?$""")


# ---------------- minimal block-YAML parser ----------------
class Parser:
    def __init__(self, text):
        self.lines = text.splitlines()
        self.pos = 0

    @staticmethod
    def _indent(line):
        return len(line) - len(line.lstrip(" "))

    def _peek(self):
        """Index, indent and content of the next line that is not blank or a whole-line comment."""
        i = self.pos
        while i < len(self.lines):
            line = self.lines[i]
            if "\t" in line[: self._indent(line) + 1]:
                raise ValueError(f"line {i + 1}: tab in indentation")
            s = line.strip()
            if s and not s.startswith("#"):
                return i, self._indent(line), s
            i += 1
        return None

    def parse(self):
        nxt = self._peek()
        if nxt is None:
            return None
        node = self._node(nxt[1])
        if self._peek() is not None:
            i = self._peek()[0]
            raise ValueError(f"line {i + 1}: unexpected indentation: {self.lines[i]!r}")
        return node

    def _node(self, indent):
        _, ind, s = self._peek()
        assert ind == indent
        return self._seq(indent) if s == "-" or s.startswith("- ") else self._map(indent)

    def _map(self, indent):
        out = {}
        while True:
            nxt = self._peek()
            if nxt is None or nxt[1] < indent:
                return out
            i, ind, s = nxt
            if ind > indent or s == "-" or s.startswith("- "):
                if ind == indent:  # a sequence item where a key was expected: end of this mapping
                    return out
                raise ValueError(f"line {i + 1}: unexpected indentation: {self.lines[i]!r}")
            m = KEY.match(s)
            if not m:
                raise ValueError(f"line {i + 1}: not a 'key: value' line: {self.lines[i]!r}")
            key = m.group(1).strip("\"'")
            if key in out:
                raise ValueError(f"line {i + 1}: duplicate key {key!r}")
            self.pos = i + 1
            out[key] = self._value(m.group(2) or "", indent, i)

    def _value(self, raw, indent, lineno):
        v = raw.strip()
        if v[:1] in ("|", ">"):  # block scalar: everything more indented (or blank) belongs to it
            body = []
            while self.pos < len(self.lines):
                line = self.lines[self.pos]
                if line.strip() and self._indent(line) <= indent:
                    break
                body.append(line)
                self.pos += 1
            return "\n".join(body)
        if v and not v.startswith("#"):
            return scalar(v, lineno)  # a quoted "" stays an empty string
        nxt = self._peek()
        if nxt is None:
            return None
        _, ind, s = nxt
        if ind > indent:
            return self._node(ind)
        if ind == indent and (s == "-" or s.startswith("- ")):  # "key:\n- item" at the same indent
            return self._seq(indent)
        return None

    def _seq(self, indent):
        out = []
        while True:
            nxt = self._peek()
            if nxt is None or nxt[1] < indent:
                return out
            i, ind, s = nxt
            if ind > indent:
                raise ValueError(f"line {i + 1}: unexpected indentation: {self.lines[i]!r}")
            if not (s == "-" or s.startswith("- ")):
                return out
            rest = s[1:].lstrip()
            if not rest or rest.startswith("#"):
                self.pos = i + 1
                nxt = self._peek()
                out.append(self._node(nxt[1]) if nxt and nxt[1] > indent else None)
            elif KEY.match(rest):
                # "- key: value": re-read this line as the first key of a mapping at the item's column
                col = ind + (len(s) - len(rest))
                self.lines[i] = " " * col + rest
                self.pos = i
                out.append(self._map(col))
            else:
                self.pos = i + 1
                out.append(scalar(rest, i))


def scalar(v, lineno):
    if v[:1] in ("'", '"'):
        q = v[0]
        end = v.find(q, 1)
        while q == "'" and end != -1 and v[end + 1 : end + 2] == "'":  # '' is an escaped quote
            end = v.find(q, end + 2)
        if end == -1:
            raise ValueError(f"line {lineno + 1}: unterminated quote")
        tail = v[end + 1 :].strip()
        if tail and not tail.startswith("#"):
            raise ValueError(f"line {lineno + 1}: text after a quoted value")
        return v[1:end].replace("''", "'") if q == "'" else v[1:end]
    v = re.split(r"\s+#", v, maxsplit=1)[0].strip()
    return {"true": True, "false": False}.get(v, v)


def parse_yaml(text):
    return Parser(text).parse()


# ---------------- checks ----------------
def is_true(v):
    return v is True or v == "true"


def is_false(v):
    return v is False or v == "false"


def steps_using(job, action):
    return [s for s in (job.get("steps") or []) if isinstance(s, dict) and str(s.get("uses", "")).startswith(action + "@")]


def check(text):
    """Return a list of problems; empty means the workflow passes."""
    try:
        wf = parse_yaml(text)
    except ValueError as e:
        return [f"cannot parse workflow: {e}"]
    if not isinstance(wf, dict):
        return ["workflow is not a mapping"]
    problems = []

    triggers = wf.get("on")
    names = set(triggers) if isinstance(triggers, dict) else {str(triggers)}
    if "pull_request_target" in names or "pull_request_target" in str(triggers):
        problems.append("publish-kit must not have a pull_request_target trigger")

    jobs = wf.get("jobs") or {}
    build, publish = jobs.get("build"), jobs.get("publish")
    if not isinstance(build, dict) or not isinstance(publish, dict):
        return problems + ["expected jobs 'build' and 'publish'"]

    # build job: no cloud access
    if build.get("permissions") != {"contents": "read"}:
        problems.append(f"build job permissions must be exactly contents: read, got {build.get('permissions')!r}")
    if "environment" in build:
        problems.append("build job must not use an environment")
    if publish.get("environment") != "gcs-publish":
        problems.append(f"publish job must run in the gcs-publish environment, got {publish.get('environment')!r}")

    # upload in build
    ups = steps_using(build, "actions/upload-artifact")
    if len(ups) != 1:
        problems.append(f"build job needs exactly one actions/upload-artifact step, found {len(ups)}")
    else:
        up = ups[0]
        w = up.get("with") or {}
        if not SHA_PIN.search(str(up["uses"])):
            problems.append(f"upload-artifact is not pinned to a full commit SHA: {up['uses']}")
        if not is_false(w.get("archive")):
            problems.append(f"build job's upload-artifact step must set archive: false, got {w.get('archive')!r}")
        out = str((build.get("outputs") or {}).get("artifact_id", ""))
        if not up.get("id") or f"steps.{up['id']}.outputs.artifact-id" not in out:
            problems.append("build job's artifact_id output must come from its upload-artifact step")

    # download in publish
    downs = steps_using(publish, "actions/download-artifact")
    if len(downs) != 1:
        problems.append(f"publish job needs exactly one actions/download-artifact step, found {len(downs)}")
    else:
        dn = downs[0]
        w = dn.get("with") or {}
        if not SHA_PIN.search(str(dn["uses"])):
            problems.append(f"download-artifact is not pinned to a full commit SHA: {dn['uses']}")
        if not is_true(w.get("skip-decompress")):
            problems.append(
                "publish job's download-artifact step must set skip-decompress: true "
                f"(got {w.get('skip-decompress')!r}); otherwise the archive: false kit zip is extracted on download")
        if "needs.build.outputs.artifact_id" not in str(w.get("artifact-ids", "")):
            problems.append("publish job must download the artifact by needs.build.outputs.artifact_id")
    return problems


# ---------------- gating and visibility (real workflow only) ----------------
BUILD_IF = ("github.repository == 'cjrada/offline-toolkit' && "
            "(github.ref == 'refs/heads/main' || startsWith(github.ref, 'refs/tags/v')) && "
            "(github.event_name == 'push' || inputs.tier == 'small')")
PUBLISH_IF = ("!cancelled() && github.repository == 'cjrada/offline-toolkit' && "
              "(github.ref == 'refs/heads/main' || startsWith(github.ref, 'refs/tags/v')) && "
              "(needs.build.result == 'success' || (needs.build.result == 'skipped' && "
              "github.event_name == 'workflow_dispatch' && inputs.tier != 'small'))")


def norm(v):
    return " ".join(str(v or "").split())


def job_text(text, job_id):
    """Raw text of one job (from its "  <id>:" line to the next job), to look for things the parser hides."""
    m = re.search(rf"(?ms)^  {re.escape(job_id)}:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)", text)
    return m.group(1) if m else ""


def writes_summary_always(job):
    return any(isinstance(s, dict) and "GITHUB_STEP_SUMMARY" in str(s.get("run", "")) and "always()" in str(s.get("if", ""))
               for s in job.get("steps") or [])


def check_gating_and_visibility(text):
    try:
        wf = parse_yaml(text)
    except ValueError as e:
        return [f"cannot parse workflow: {e}"]
    problems = []
    jobs = wf.get("jobs") or {}
    build, publish = jobs.get("build") or {}, jobs.get("publish") or {}

    # gating, exactly as reviewed
    if str(wf.get("permissions")) != "{}":
        problems.append(f"workflow permissions must be {{}}, got {wf.get('permissions')!r}")
    conc = wf.get("concurrency") or {}
    if not (isinstance(conc, dict) and conc.get("group") == "publish-kit-${{ github.ref }}" and is_false(conc.get("cancel-in-progress"))):
        problems.append(f"concurrency changed: {conc!r}")
    dispatch = ((wf.get("on") or {}).get("workflow_dispatch") or {}).get("inputs") or {}
    if not is_true((dispatch.get("dry_run") or {}).get("default")):
        problems.append("workflow_dispatch input dry_run must default to true")
    if norm(build.get("if")) != BUILD_IF:
        problems.append(f"build job if: changed: {norm(build.get('if'))!r}")
    if norm(publish.get("if")) != PUBLISH_IF:
        problems.append(f"publish job if: changed: {norm(publish.get('if'))!r}")
    if publish.get("needs") != "build":
        problems.append(f"publish job must need build, got {publish.get('needs')!r}")
    if publish.get("permissions") != {"contents": "read", "id-token": "write"}:
        problems.append(f"publish job permissions must be contents: read, id-token: write, got {publish.get('permissions')!r}")
    for jid, job in jobs.items():
        if not isinstance(job, dict):
            continue
        if jid != "publish" and "environment" in job:
            problems.append(f"only the publish job may use an environment, {jid} does")
        if jid != "publish" and "id-token" in str(job.get("permissions", "")):
            problems.append(f"only the publish job may request id-token, {jid} does")
        for s in job.get("steps") or []:
            if isinstance(s, dict) and "uses" in s and not SHA_PIN.search(str(s["uses"]).split()[0]):
                problems.append(f"{jid}: action not pinned to a full commit SHA: {s['uses']}")
    if re.search(r"\bsecrets\.\w", text):
        problems.append("publish-kit uses no secrets; found a secrets.<name> reference")
    if "https://storage.googleapis.com" in text:
        problems.append("the bucket is private: do not link https://storage.googleapis.com URLs")

    # run list: tier, version, dry run vs publish
    rn = str(wf.get("run-name", ""))
    for needle in ("inputs.tier", "inputs.version", "inputs.dry_run", "github.ref_name", "'dry run'", "'publish'"):
        if needle not in rn:
            problems.append(f"run-name must show {needle} (got {rn!r})")
    for jid in ("build", "publish"):
        if "small" not in str(jobs.get(jid, {}).get("name", "")) and "inputs.tier" not in str(jobs.get(jid, {}).get("name", "")):
            problems.append(f"{jid} job needs a display name that shows the tier")

    # per-job summaries
    for jid in ("build", "publish"):
        if not writes_summary_always(jobs.get(jid) or {}):
            problems.append(f"{jid} job needs a step with if: always() that writes $GITHUB_STEP_SUMMARY")
    steps = {s.get("id"): s for s in publish.get("steps") or [] if isinstance(s, dict)}
    cb, wait = steps.get("cloudbuild") or {}, steps.get("cbwait") or {}
    if not ("--async" in str(cb.get("run", "")) and "GITHUB_OUTPUT" in str(cb.get("run", ""))
            and "::notice" in str(cb.get("run", "")) and "sleep" not in str(cb.get("run", ""))):
        problems.append("Cloud Build must be submitted by a step (id: cloudbuild) that records the id and a notice before any wait")
    if "steps.cloudbuild.outputs.id" not in str(wait.get("env", "")):
        problems.append("the Cloud Build wait step (id: cbwait) must use steps.cloudbuild.outputs.id")
    # Steps that act on (or report) an earlier step's work must not run after a failure. GitHub adds success()
    # implicitly to an if: without a status function; require it explicitly so the intent is reviewable.
    for sid in ("auth", "gcloud", "upload", "cloudbuild", "cbwait", "result"):
        cond = norm((steps.get(sid) or {}).get("if"))
        if not cond.startswith("success() && "):
            problems.append(f"publish step {sid!r} must have if: success() && ..., got {cond!r}")
    cb_lines = str(cb.get("run", "")).splitlines()
    check_at = [i for i, l in enumerate(cb_lines) if '"$project"' in l and "grep -Eqx" in l]
    submit_at = [i for i, l in enumerate(cb_lines) if "gcloud builds submit" in l]
    if not (check_at and submit_at and check_at[0] < submit_at[0]):
        problems.append("the cloudbuild step must check the derived project before gcloud builds submit")
    res = steps.get("result") or {}
    if "::notice" not in str(res.get("run", "")) or "console.cloud.google.com" not in str(res.get("run", "")):
        problems.append("publish job must record console links and a ::notice for a published kit (id: result)")
    outs = publish.get("outputs") or {}
    for o in ("tier", "version", "dry_run", "gs_uri", "zip_url", "sha256_url", "kit_info_url", "latest_url", "cloud_build_id", "cloud_build_url"):
        if o not in outs:
            problems.append(f"publish job must export output {o!r} for the summary job")
    for o in ("tier", "version", "dry_run", "zip_name", "sha256", "size"):
        if o not in (build.get("outputs") or {}):
            problems.append(f"build job must export output {o!r} for the summary job")

    # overall summary job
    summ = jobs.get("summary")
    if not isinstance(summ, dict):
        return problems + ["expected a 'summary' job"]
    cond = norm(summ.get("if"))
    if not cond.startswith("always()"):
        problems.append(f"summary job must run with if: always(), got {cond!r}")
    if "refs/heads/main" in cond or "refs/tags" in cond:
        problems.append("summary job must not be limited to main / v* or it cannot explain skipped runs")
    needs = str(summ.get("needs", ""))
    if "build" not in needs or "publish" not in needs:
        problems.append(f"summary job must need build and publish, got {needs!r}")
    if "environment" in summ:
        problems.append("summary job must not use an environment")
    if str(summ.get("permissions")) != "{}":
        problems.append(f"summary job permissions must be {{}}, got {summ.get('permissions')!r}")
    raw = job_text(text, "summary")
    for bad in ("id-token", "secrets", "actions/checkout", "GCP_WIF_PROVIDER", "GCP_SERVICE_ACCOUNT", "vars."):
        if bad in raw:
            problems.append(f"summary job must not reference {bad}")
    if "GITHUB_STEP_SUMMARY" not in raw or "needs.publish.result" not in raw or "needs.build.result" not in raw:
        problems.append("summary job must write $GITHUB_STEP_SUMMARY with both jobs' results")
    if "not main or a v* tag" not in raw:
        problems.append("summary job must explain why build/publish are skipped on other refs")
    return problems


def mutation_test(text):
    """The real file must fail when any of the guarded lines is changed."""
    def bad(old, new, needle, what, count=1):
        assert text.count(old) == count, f"{what}: {old!r} found {text.count(old)} times"
        problems = check_gating_and_visibility(text.replace(old, new))
        assert any(needle in p for p in problems), f"{what}: expected a problem mentioning {needle!r}, got {problems}"
    bad(re.search(r"(?m)^run-name:.*\n", text).group(0), "", "run-name", "run-name removed")
    bad("    if: always() && github.repository == 'cjrada/offline-toolkit'\n",
        "    if: always() && github.ref == 'refs/heads/main'\n", "summary job must not be limited", "summary limited to main")
    bad("    if: always() && github.repository == 'cjrada/offline-toolkit'\n",
        "    if: success()\n", "if: always()", "summary not always")
    bad("  summary:\n", "  summary:\n    environment: gcs-publish\n", "summary job must not use an environment", "summary env")
    bad("    timeout-minutes: 5\n    permissions: {}\n", "    timeout-minutes: 5\n    permissions:\n      id-token: write\n",
        "id-token", "summary id-token")
    bad("        type: boolean\n        default: true\n", "        type: boolean\n        default: false\n", "dry_run", "dry_run default")
    bad("      (github.event_name == 'push' || inputs.tier == 'small')\n", "      true\n", "build job if", "build if changed")
    bad("      !cancelled() &&\n", "      always() &&\n", "publish job if", "publish if changed")
    bad("concurrency:\n  group: publish-kit-${{ github.ref }}\n", "concurrency:\n  group: publish-kit\n", "concurrency", "concurrency")
    bad("        id: cloudbuild\n", "        id: submit\n", "Cloud Build", "cloudbuild id renamed")
    for sid in ("result", "cbwait", "upload", "auth"):
        m = re.search(rf"(?m)^        id: {sid}\n        if: success\(\) && ", text)
        assert m, f"{sid}: expected 'if: success() && ' right after its id"
        bad(m.group(0), f"        id: {sid}\n        if: ", f"publish step {sid!r}", f"{sid} success() removed")


# ---------------- self-test ----------------
SAMPLE = """\
name: publish-kit
on:
  push:
    tags: ['v*']
  workflow_dispatch:
permissions: {}
jobs:
  build:
    permissions:
      contents: read  # nothing else
    outputs:
      artifact_id: ${{ steps.upload.outputs.artifact-id }}
    steps:
      - name: Not a step
        run: |
          echo "- uses: actions/download-artifact@0000000000000000000000000000000000000000"
          # skip-decompress: true
      - name: Hand over
        id: upload
        uses: actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9 # v7.0.2
        with:
          path: x.zip
          archive: false          # upload the zip as-is
  publish:
    needs: build
    if: >-
      !cancelled() &&
      needs.build.result == 'success'
    environment: gcs-publish
    steps:
      - name: Download
        uses: actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333 # v8.0.2
        with:
          artifact-ids: ${{ needs.build.outputs.artifact_id }}
          skip-decompress: true
"""


def must_fail(text, needle, what):
    problems = check(text)
    assert any(needle in p for p in problems), f"{what}: expected a problem mentioning {needle!r}, got {problems}"


def self_test():
    wf = parse_yaml(SAMPLE)
    assert wf["on"]["push"]["tags"] == "['v*']" and wf["permissions"] == "{}", wf
    assert wf["jobs"]["publish"]["if"].strip().startswith("!cancelled()"), wf["jobs"]["publish"]["if"]
    assert len(wf["jobs"]["build"]["steps"]) == 2, "a run: | body was parsed as steps"
    assert check(SAMPLE) == [], check(SAMPLE)

    skip = "          skip-decompress: true\n"
    must_fail(SAMPLE.replace(skip, ""), "skip-decompress", "skip-decompress removed")
    must_fail(SAMPLE.replace(skip, skip.replace("true", "false")), "skip-decompress", "skip-decompress false")
    assert check(SAMPLE.replace(skip, skip.replace("true", "'true'"))) == [], "quoted 'true' is valid for actions"
    must_fail(SAMPLE.replace("archive: false", "archive: true"), "archive: false", "archive true")
    must_fail(SAMPLE.replace("          archive: false          # upload the zip as-is\n", ""), "archive: false", "archive removed")
    must_fail(SAMPLE.replace("@9000827ccba6bdab643e8b6fd33ac0654aef8333", "@v8"), "full commit SHA", "tag-pinned download")
    must_fail(SAMPLE.replace("        id: upload\n", ""), "artifact_id output", "upload step id removed")
    must_fail(SAMPLE.replace("    environment: gcs-publish\n", ""), "gcs-publish", "publish environment removed")
    must_fail(SAMPLE.replace("      contents: read  # nothing else\n",
                             "      contents: read\n      id-token: write\n"), "build job permissions", "build id-token")
    must_fail(SAMPLE.replace("  workflow_dispatch:\n", "  workflow_dispatch:\n  pull_request_target:\n"),
              "pull_request_target", "pull_request_target added")
    must_fail(SAMPLE.replace("  build:\n", "  build:\n    environment: gcs-publish\n"), "build job must not", "build env")
    must_fail(SAMPLE.replace("    steps:\n      - name: Download\n",
                             "    steps:\n      - uses: actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333\n"
                             "      - name: Download\n"), "exactly one actions/download-artifact", "two downloads")
    must_fail(SAMPLE.replace("        with:\n          path: x.zip", "        with:\n         path: x.zip"),
              "cannot parse", "bad indentation")


def main():
    self_test()
    text = WORKFLOW.read_text(encoding="utf-8")
    problems = check(text)
    # The real file must also fail without the line this test exists for.
    without = re.sub(r"(?m)^[ \t]*skip-decompress:.*\n", "", text)
    assert without != text and any("skip-decompress" in p for p in check(without)), \
        "removing skip-decompress from publish.yml was not detected"
    problems += check_gating_and_visibility(text)
    mutation_test(text)
    if problems:
        for p in problems:
            print(f"FAIL {WORKFLOW.relative_to(WORKFLOW.parents[2])}: {p}")
        return 1
    print("OK: publish.yml hands the kit zip over with archive: false and downloads it with skip-decompress: true")
    print("OK: gating unchanged; run-name, job summaries and the always-running summary job are in place")
    return 0


if __name__ == "__main__":
    sys.exit(main())
