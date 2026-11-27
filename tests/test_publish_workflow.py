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
  is SHA-pinned; only the publish job uses gcs-publish (no other job has an environment in this file), and
  only publish and the pages caller may request id-token. The run list shows tier, version and dry run vs
  publish (run-name). Build and publish write a step summary even on failure; the Cloud Build id is
  recorded by its own step before the wait (project checked before submitting). The auth, gcloud, upload,
  cloudbuild, cbwait and result steps say if: success() && ... explicitly. A `summary` job runs with
  if: always() (not limited to main / v*, so it explains skipped runs), with no environment, no
  permissions, no secrets, no checkout, and it does not wait for pages.
* Secrets and R2 (check_gating_and_visibility): the only secrets.<name> references are R2_ACCESS_KEY_ID,
  R2_SECRET_ACCESS_KEY and R2_ACCOUNT_ID, only in the publish job; their values reach only the steps
  that run tools/publish_r2.py (the validation step sees only "!= ''"), never Cloud Build substitutions,
  outputs or summaries. The validation step checks R2_BUCKET / R2_PUBLIC_URL and the GCS/WIF variables on
  every run (dry runs too). Small uploads with publish_r2.py (no Google auth); auth/gcloud run for
  medium/large only. Result links are public R2 URLs from vars.R2_PUBLIC_URL, never storage.googleapis.com.
* pages (check_pages): publish.yml's pages job only calls .github/workflows/pages.yml after a successful,
  non-dry-run publish, with contents: read + pages: write + id-token: write and no secrets. pages.yml runs on
  workflow_dispatch / workflow_call only (main, or a v* tag push via publish-kit), has workflow permissions
  {}, the github-pages environment, its own concurrency group, SHA-pinned upload-pages-artifact and
  deploy-pages, and no secrets, Google auth or GCP variables. The page artifact is named per run attempt
  (github.run_id + github.run_attempt) and both deploy-pages steps ask for that exact name, so "Re-run
  failed jobs" never sees two artifacts with the same name; the deploy is retried once, only after a failed
  first try, and a failed retry fails the job.

Uses a small parser for the block-style YAML subset GitHub workflows use (no PyYAML needed on the
runner). It raises on anything it does not understand, so an unexpected layout fails the test instead of
silently passing.
"""
import re
import sys
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "publish.yml"
PAGES = WORKFLOW.with_name("pages.yml")
R2_SECRETS = {"R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_ACCOUNT_ID"}
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


def code_only(t):
    """t without whole-line comments."""
    return "\n".join(l for l in t.splitlines() if not l.lstrip().startswith("#"))


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
        if jid not in ("publish", "pages") and "id-token" in str(job.get("permissions", "")):
            problems.append(f"only the publish and pages jobs may request id-token, {jid} does")
        if "uses" in job and job["uses"] != "./.github/workflows/pages.yml":
            problems.append(f"{jid}: the only reusable workflow allowed is ./.github/workflows/pages.yml")
        if "secrets" in job:
            problems.append(f"{jid}: must not pass secrets to a called workflow")
        for s in job.get("steps") or []:
            if isinstance(s, dict) and "uses" in s and not SHA_PIN.search(str(s["uses"]).split()[0]):
                problems.append(f"{jid}: action not pinned to a full commit SHA: {s['uses']}")

    # secrets: the three R2 ones, only in the publish job, values only where the publisher runs
    names = set(re.findall(r"\bsecrets\.(\w+)", text))
    if names - R2_SECRETS:
        problems.append(f"the only secrets allowed are {sorted(R2_SECRETS)}; found secrets.{sorted(names - R2_SECRETS)}")
    for jid in jobs:
        if jid != "publish" and re.search(r"\bsecrets\s*[.:\[]", code_only(job_text(text, jid))):
            problems.append(f"only the publish job may reference secrets, {jid} does")
    if re.search(r"secrets\s*:\s*inherit", text):
        problems.append("secrets: inherit is not allowed")
    psteps = [s for s in publish.get("steps") or [] if isinstance(s, dict)]
    value_steps = set()
    for st in psteps:
        blob = str(st.get("env", "")) + str(st.get("run", "")) + str(st.get("with", "")) + str(st.get("if", ""))
        refs = re.findall(r"secrets\.\w+[^}]*", blob)
        if not refs:
            continue
        if all(re.fullmatch(r"secrets\.\w+ != ''\s*", r) for r in refs):
            continue  # only "is it set"
        value_steps.add(st.get("id"))
        if "tools/publish_r2.py" not in str(st.get("run", "")):
            problems.append(f"publish step {st.get('id')!r} gets an R2 secret value but does not run tools/publish_r2.py")
        if re.search(r"GITHUB_OUTPUT|GITHUB_STEP_SUMMARY|GITHUB_ENV|::notice|echo .*\$R2_(SECRET|ACCESS|ACCOUNT)", str(st.get("run", ""))):
            problems.append(f"publish step {st.get('id')!r} has R2 secrets and writes outputs/summaries/env or echoes them")
        if "secrets." in str(st.get("run", "")) or "secrets." in str(st.get("with", "")):
            problems.append(f"publish step {st.get('id')!r}: pass secrets via env only")
    if value_steps != {"check", "upload"}:
        problems.append(f"R2 secret values must reach exactly the check and upload steps, got {sorted(map(str, value_steps))}")
    for o, v in (publish.get("outputs") or {}).items():
        if "secrets" in str(v):
            problems.append(f"publish output {o} must not come from secrets")
    for bad in ("https://storage.googleapis.com", "storage.cloud.google.com", "console.cloud.google.com/storage"):
        if bad in text:
            problems.append(f"kits are downloaded from R2: do not link {bad} URLs")
    if "publish_gcs.py" in text:
        problems.append("kits are published to R2: publish.yml must not run tools/publish_gcs.py")

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
    rrun = str(res.get("run", ""))
    if "::notice" not in rrun or "vars.R2_PUBLIC_URL" not in str(res.get("env", "")):
        problems.append("publish job must record public R2 links (from vars.R2_PUBLIC_URL) and a ::notice (id: result)")
    for line in ("zip_url=$base/$key", "sha256_url=$base/$key.sha256", "kit_info_url=$base/", "latest_url=$base/$TIER/latest.json", "pages_url="):
        if line not in rrun:
            problems.append(f"result step must write {line}...")
    if "console.cloud.google.com" in rrun or "gs://" in rrun or "gcloud" in rrun:
        problems.append("result step must not link GCS or need Google credentials")
    if "console.cloud.google.com/cloud-build" not in str(cb.get("run", "")):
        problems.append("the cloudbuild step must keep the Cloud Build console link")
    outs = publish.get("outputs") or {}
    for o in ("tier", "version", "dry_run", "zip_url", "sha256_url", "kit_info_url", "latest_url", "pages_url", "cloud_build_id", "cloud_build_url"):
        if o not in outs:
            problems.append(f"publish job must export output {o!r} for the summary job")
    for o in ("tier", "version", "dry_run", "zip_name", "sha256", "size"):
        if o not in (build.get("outputs") or {}):
            problems.append(f"build job must export output {o!r} for the summary job")

    # R2 configuration is validated on every publish-job run, dry runs included; GCS/WIF too
    cfg = steps.get("cfg") or {}
    crun, cenv = str(cfg.get("run", "")), cfg.get("env") or {}
    if "tools/publish_r2.py --check-config" not in crun or cfg.get("if"):
        problems.append("the cfg step must run tools/publish_r2.py --check-config on every run")
    for v, want in (("R2_BUCKET", "${{ vars.R2_BUCKET }}"), ("R2_PUBLIC_URL", "${{ vars.R2_PUBLIC_URL }}"),
                    ("GCS_BUCKET", "${{ vars.GCS_BUCKET }}"), ("GCP_WIF_PROVIDER", "${{ vars.GCP_WIF_PROVIDER }}"),
                    ("GCP_SERVICE_ACCOUNT", "${{ vars.GCP_SERVICE_ACCOUNT }}")):
        if cenv.get(v) != want:
            problems.append(f"cfg step must validate {v} from {want}")
    for name in sorted(R2_SECRETS):
        if cenv.get(f"HAS_{name}") != f"${{{{ secrets.{name} != '' }}}}":
            problems.append(f"cfg step must check that secret {name} is set (HAS_{name})")
    if "gcs-publish environment secret" not in crun or "repository secret" in crun:
        problems.append("cfg step must tell Chris to keep the R2 secrets in the gcs-publish environment, not the repository")
    if "for v in GCS_BUCKET GCP_WIF_PROVIDER GCP_SERVICE_ACCOUNT" not in crun:
        problems.append("cfg step must still require the gcs-publish variables")
    chk = steps.get("check") or {}
    if chk.get("if") or "--dry-run" not in str(chk.get("run", "")) or "tools/publish_r2.py" not in str(chk.get("run", "")):
        problems.append("the check step must run publish_r2.py --dry-run for every tier, dry runs included")
    up = steps.get("upload") or {}
    if "python3 -I tools/publish_r2.py --zip" not in str(up.get("run", "")) or "--dry-run" in str(up.get("run", "")):
        problems.append("the small upload step must publish with tools/publish_r2.py")
    if "tier == 'small'" not in norm(up.get("if")) or "dry_run != 'true'" not in norm(up.get("if")):
        problems.append("the upload step must run only for small and not on a dry run")
    for sid in ("auth", "gcloud"):
        c = norm((steps.get(sid) or {}).get("if"))
        if "tier != 'small'" not in c or "dry_run != 'true'" not in c:
            problems.append(f"{sid} must run only for medium/large real publishes (small uploads to R2 without Google)")
    crun_cb = str(cb.get("run", ""))
    subs = re.search(r'--substitutions "([^"]*)"', crun_cb)
    if not subs or subs.group(1) != "_TIER=$TIER,_VERSION=$VERSION,_COMMIT=$COMMIT,_BUCKET=$GCS_BUCKET,_R2_BUCKET=$R2_BUCKET":
        problems.append(f"Cloud Build substitutions must be exactly the non-secret _TIER,_VERSION,_COMMIT,_BUCKET,_R2_BUCKET, "
                        f"got {subs.group(1) if subs else None!r}")
    if "secrets." in str(cb.get("env", "")) or "secrets." in str(wait.get("env", "")):
        problems.append("Cloud Build steps must not get secrets")

    # pages caller
    pg = jobs.get("pages")
    if not isinstance(pg, dict):
        problems.append("expected a 'pages' job that calls .github/workflows/pages.yml")
    else:
        if pg.get("uses") != "./.github/workflows/pages.yml" or pg.get("needs") != "publish":
            problems.append("pages job must need publish and call ./.github/workflows/pages.yml")
        if pg.get("permissions") != {"contents": "read", "pages": "write", "id-token": "write"}:
            problems.append(f"pages job permissions must be contents: read, pages: write, id-token: write, got {pg.get('permissions')!r}")
        c = norm(pg.get("if"))
        for need in ("!cancelled()", "needs.publish.result == 'success'", "needs.publish.outputs.published == 'true'",
                     "needs.publish.outputs.dry_run != 'true'", "github.repository == 'cjrada/offline-toolkit'"):
            if need not in c:
                problems.append(f"pages job must run only after a successful real publish (missing {need!r})")
        raw = code_only(job_text(text, "pages"))
        for bad in ("secrets", "GCP_", "google-github-actions", "gcs-publish"):
            if bad in raw:
                problems.append(f"pages job must not reference {bad}")

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
    if "pages" in needs:
        problems.append("summary job must not wait for the pages job")
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
    if "needs.publish.outputs.zip_url" not in raw or "needs.publish.outputs.pages_url" not in raw or "public download" not in raw:
        problems.append("summary job must show the public R2 download links and the downloads page")
    return problems


# ---------------- a tiny evaluator for GitHub `if:` expressions (the subset these workflows use) ----------------
TOKEN = re.compile(r"\s*(?:(\|\||&&|==|!=|!|\(|\)|,)|'((?:[^']|'')*)'|([A-Za-z_][A-Za-z0-9_.-]*))")


def eval_if(expr, ctx):
    """Evaluate expr with ctx {"github.ref": ..., ...}. Supports ||, &&, !, ==, !=, parentheses, string
    literals, context names and startsWith(a, b). Raises ValueError on anything else."""
    toks, pos, expr = [], 0, expr.strip()
    while pos < len(expr):
        m = TOKEN.match(expr, pos)
        if not m or m.end() == pos:
            raise ValueError(f"cannot read {expr[pos:]!r}")
        op, lit, name = m.groups()
        toks.append(("op", op) if op else ("str", lit.replace("''", "'")) if lit is not None else ("name", name))
        pos = m.end()
    i = 0

    def peek():
        return toks[i] if i < len(toks) else (None, None)

    def take(kind=None, val=None):
        nonlocal i
        t = peek()
        if (kind and t[0] != kind) or (val and t[1] != val):
            raise ValueError(f"expected {val or kind}, got {t}")
        i += 1
        return t

    def orx():
        v = andx()
        while peek() == ("op", "||"):
            take(); w = andx(); v = v or w
        return v

    def andx():
        v = cmp()
        while peek() == ("op", "&&"):
            take(); w = cmp(); v = v and w
        return v

    def cmp():
        v = unary()
        if peek() in (("op", "=="), ("op", "!=")):
            op = take()[1]; w = unary()
            return (v == w) if op == "==" else (v != w)
        return v

    def unary():
        if peek() == ("op", "!"):
            take(); return not unary()
        if peek() == ("op", "("):
            take(); v = orx(); take("op", ")"); return v
        kind, val = take()
        if kind == "str":
            return val
        if kind == "name" and peek() == ("op", "("):
            take(); a = orx(); take("op", ","); b = orx(); take("op", ")")
            if val != "startsWith":
                raise ValueError(f"unsupported function {val}")
            return str(a).lower().startswith(str(b).lower())
        if kind == "name":
            if val not in ctx:
                raise ValueError(f"unknown name {val}")
            return ctx[val]
        raise ValueError(f"unexpected {val}")

    v = orx()
    if i != len(toks):
        raise ValueError(f"trailing {toks[i:]}")
    return v


# (event as the called workflow sees it, ref, should deploy). A called workflow's github context is the
# caller's, so after a tag publish event_name may read push (GitHub's docs) or workflow_call; accept both.
PAGES_CASES = [
    ("workflow_dispatch", "refs/heads/main", True),
    ("workflow_dispatch", "refs/tags/v1.0.0", False),
    ("workflow_dispatch", "refs/heads/feature", False),
    ("workflow_call", "refs/heads/main", True),
    ("workflow_call", "refs/tags/v1.0.0", True),
    ("push", "refs/tags/v1.0.0", True),
    ("workflow_call", "refs/heads/feature", False),
    ("workflow_call", "refs/tags/x1", False),
    ("push", "refs/heads/feature", False),
]


PAGES_ARTIFACT = "github-pages-${{ github.run_id }}-${{ github.run_attempt }}"


def check_pages_artifact(steps):
    """Per-attempt artifact name shared by upload and both deploys; one retry, only after a failure."""
    problems = []
    up = [st for st in steps if str(st.get("uses", "")).startswith("actions/upload-pages-artifact@")]
    deps = [st for st in steps if str(st.get("uses", "")).startswith("actions/deploy-pages@")]
    if len(up) != 1 or len(deps) != 2:
        return problems
    name = norm((up[0].get("with") or {}).get("name"))
    if name != PAGES_ARTIFACT:
        problems.append(f"upload-pages-artifact name must be {PAGES_ARTIFACT} (unique per run attempt, so a "
                        f"re-run never finds two github-pages artifacts), got {name!r}")
    for st in deps:
        an = norm((st.get("with") or {}).get("artifact_name"))
        if an != name or not an:
            problems.append(f"deploy-pages step {st.get('id')!r} artifact_name must match the uploaded name {name!r}, got {an!r}")
    first, retry = deps
    if first.get("id") != "deploy" or norm(first.get("if")) or not is_true(first.get("continue-on-error")):
        problems.append("first deploy-pages step must be id: deploy, unconditional, continue-on-error: true")
    if retry.get("id") != "deploy_retry" or norm(retry.get("if")) != "steps.deploy.outcome == 'failure'":
        problems.append("the deploy retry must be id: deploy_retry and run only if steps.deploy.outcome == 'failure'")
    if "continue-on-error" in retry:
        problems.append("the deploy retry must not continue on error: a failed retry has to fail the job")
    return problems


def check_pages(text):
    """Static checks for .github/workflows/pages.yml."""
    try:
        wf = parse_yaml(text)
    except ValueError as e:
        return [f"cannot parse pages.yml: {e}"]
    problems = []
    on = wf.get("on")
    if not isinstance(on, dict) or set(on) != {"workflow_dispatch", "workflow_call"}:
        problems.append(f"pages.yml triggers must be exactly workflow_dispatch and workflow_call, got {on!r}")
    if str(wf.get("permissions")) != "{}":
        problems.append("pages.yml workflow permissions must be {}")
    jobs = wf.get("jobs") or {}
    if list(jobs) != ["deploy"]:
        return problems + [f"pages.yml must have exactly one job 'deploy', got {list(jobs)}"]
    job = jobs["deploy"]
    if job.get("permissions") != {"contents": "read", "pages": "write", "id-token": "write"}:
        problems.append(f"pages deploy permissions must be contents: read, pages: write, id-token: write, got {job.get('permissions')!r}")
    env = job.get("environment")
    if not (isinstance(env, dict) and env.get("name") == "github-pages"):
        problems.append(f"pages deploy must use the github-pages environment, got {env!r}")
    conc = job.get("concurrency") or {}
    if not (isinstance(conc, dict) and conc.get("group") == "pages-deploy" and is_false(conc.get("cancel-in-progress"))):
        problems.append(f"pages deploy needs its own concurrency group (pages-deploy, no cancel), got {conc!r}")
    c = norm(job.get("if"))
    if "github.event_name == 'push'" in c:
        problems.append("pages deploy must not require event_name == 'push' for tags: a called workflow sees the "
                        "caller's event, so key the tag case on excluding workflow_dispatch instead of on push")
    try:
        for event, ref, want in PAGES_CASES:
            for repo, ok in (("cjrada/offline-toolkit", want), ("someone/fork", False)):
                got = eval_if(c, {"github.repository": repo, "github.ref": ref, "github.event_name": event})
                if bool(got) != ok:
                    problems.append(f"pages deploy if: gives {bool(got)} for {event} on {ref} in {repo}, expected {ok} "
                                    "(manual runs: main only; publish-kit's call: main and v* tags; this repo only)")
    except ValueError as e:
        problems.append(f"pages deploy if: cannot be evaluated ({e}): {c!r}")
    steps = [st for st in job.get("steps") or [] if isinstance(st, dict)]
    used = [str(st.get("uses", "")).split("@")[0] for st in steps if "uses" in st]
    if used != ["actions/checkout", "actions/upload-pages-artifact", "actions/deploy-pages", "actions/deploy-pages"]:
        problems.append(f"pages deploy must use checkout, upload-pages-artifact, deploy-pages (+ one retry), got {used}")
    problems += check_pages_artifact(steps)
    for st in job.get("steps") or []:
        if isinstance(st, dict) and "uses" in st and not SHA_PIN.search(str(st["uses"]).split()[0]):
            problems.append(f"pages.yml: action not pinned to a full commit SHA: {st['uses']}")
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    for bad in ("secrets", "google-github-actions", "GCP_", "gcloud", "gcs-publish", "pull_request"):
        if bad in code:
            problems.append(f"pages.yml must not reference {bad}")
    if "tools/build_site.py" not in code or "vars.R2_PUBLIC_URL" not in code:
        problems.append("pages.yml must build the page with tools/build_site.py from vars.R2_PUBLIC_URL")
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
    pub_if = ("      !cancelled() &&\n      github.repository == 'cjrada/offline-toolkit' &&\n"
              "      (github.ref == 'refs/heads/main' || startsWith(github.ref, 'refs/tags/v')) &&\n      (needs.build.result")
    bad(pub_if, pub_if.replace("!cancelled()", "always()"), "publish job if", "publish if changed")
    bad("concurrency:\n  group: publish-kit-${{ github.ref }}\n", "concurrency:\n  group: publish-kit\n", "concurrency", "concurrency")
    bad("        id: cloudbuild\n", "        id: submit\n", "Cloud Build", "cloudbuild id renamed")
    # secrets
    bad("      - name: Free disk space\n", "      - name: Free disk space\n        env:\n          K: ${{ secrets.R2_ACCOUNT_ID }}\n",
        "only the publish job may reference secrets", "secret in build job")
    bad("          ACTOR: ${{ github.actor }}\n", "          ACTOR: ${{ github.actor }}\n          K: ${{ secrets.R2_SECRET_ACCESS_KEY }}\n",
        "summary job must not reference secrets", "secret in summary job")
    bad("          R2_PUBLIC_URL: ${{ vars.R2_PUBLIC_URL }}\n          BUILD_SHA256:",
        "          R2_PUBLIC_URL: ${{ vars.R2_PUBLIC_URL }}\n          K: ${{ secrets.R2_ACCESS_KEY_ID }}\n          BUILD_SHA256:",
        "does not run tools/publish_r2.py", "secret in result step")
    bad("          COMMIT: ${{ github.sha }}\n", "          COMMIT: ${{ github.sha }}\n          K: ${{ secrets.R2_SECRET_ACCESS_KEY }}\n",
        "Cloud Build steps must not get secrets", "secret in cloudbuild step")
    bad(",_R2_BUCKET=$R2_BUCKET\")", ",_R2_BUCKET=$R2_BUCKET,_KEY=$R2_SECRET_ACCESS_KEY\")", "substitutions", "secret substitution")
    bad("HAS_R2_ACCOUNT_ID: ${{ secrets.R2_ACCOUNT_ID != '' }}", "HAS_R2_ACCOUNT_ID: ${{ secrets.GITHUB_TOKEN != '' }}",
        "the only secrets allowed", "other secret")
    bad("    uses: ./.github/workflows/pages.yml\n", "    uses: ./.github/workflows/pages.yml\n    secrets: inherit\n",
        "secrets", "secrets: inherit")
    # R2 config validation and the R2 upload
    bad("          python3 -I tools/publish_r2.py --check-config --no-secrets\n", "", "--check-config", "R2 config check removed")
    bad("          R2_BUCKET: ${{ vars.R2_BUCKET }}\n          R2_PUBLIC_URL: ${{ vars.R2_PUBLIC_URL }}\n          # Only whether",
        "          R2_PUBLIC_URL: ${{ vars.R2_PUBLIC_URL }}\n          # Only whether", "R2_BUCKET", "R2_BUCKET not validated")
    bad('        run: python3 -I tools/publish_r2.py --zip "$ZIP"\n', '        run: python3 tools/publish_gcs.py --zip "$ZIP"\n',
        "publish_r2.py", "upload back to GCS")
    m = re.search(r"(?m)^        id: auth\n        if: (.*)\n", text)
    bad(m.group(0), m.group(0).replace(" && steps.cfg.outputs.tier != 'small'", ""), "auth must run only", "auth for small")
    bad('            echo "zip_url=$base/$key"\n', '            echo "zip_url=https://storage.googleapis.com/b/$key"\n',
        "storage.googleapis.com", "GCS download link")
    # pages
    bad("      needs.publish.outputs.dry_run != 'true'\n", "      true\n", "pages job must run only", "pages on dry run")
    bad("    needs: [build, publish]\n", "    needs: [build, publish, pages]\n", "must not wait for the pages", "summary waits for pages")
    bad("      pages: write\n      id-token: write  # deploy-pages only", "      pages: write\n      id-token: write\n      actions: write  # deploy-pages only",
        "pages job permissions", "pages permissions widened")
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
    pages = PAGES.read_text(encoding="utf-8")
    pages_problems = check_pages(pages)
    for old_s, new_s, needle in (
            ("  workflow_call:\n", "  workflow_call:\n  pull_request:\n", "triggers"),
            ("      name: github-pages\n", "      name: gcs-publish\n", "github-pages"),
            ("      group: pages-deploy\n", "      group: other\n", "concurrency"),
            ("          R2_PUBLIC_URL: ${{ vars.R2_PUBLIC_URL }}\n", "          R2_PUBLIC_URL: ${{ vars.R2_PUBLIC_URL }}\n          K: ${{ secrets.R2_SECRET_ACCESS_KEY }}\n", "secrets"),
            ("uses: actions/deploy-pages@368f82528645a54fb793d4d04e342629a3f51346 # v5.0.1\n        with:\n          artifact_name: github-pages-${{ github.run_id }}-${{ github.run_attempt }}\n\n      - name: Wait",
             "uses: actions/deploy-pages@v5\n        with:\n          artifact_name: github-pages-${{ github.run_id }}-${{ github.run_attempt }}\n\n      - name: Wait",
             "full commit SHA"),
            ("uses: actions/deploy-pages@368f82528645a54fb793d4d04e342629a3f51346 # v5.0.1\n        with:\n          artifact_name: github-pages-${{ github.run_id }}-${{ github.run_attempt }}\n\n      - name: Job",
             "uses: actions/deploy-pages@v5\n        with:\n          artifact_name: github-pages-${{ github.run_id }}-${{ github.run_attempt }}\n\n      - name: Job",
             "full commit SHA"),
            # the first deploy must not hard-fail (else the retry never runs) and the retry must ask for the same artifact
            ("        id: deploy\n        continue-on-error: true\n", "        id: deploy\n", "continue-on-error: true"),
            ("artifact_name: github-pages-${{ github.run_id }}-${{ github.run_attempt }}\n\n      - name: Job",
             "artifact_name: github-pages\n\n      - name: Job", "must match the uploaded name"),
            ("    if: >-\n      github.repository == 'cjrada/offline-toolkit' &&\n", "    if: >-\n", "someone/fork"),
            # the tag clause this replaces: it keyed the tag case on the caller's event being push; keep the rule
            # that it must not be required, so the condition never depends on how a called run reports its event
            ("(startsWith(github.ref, 'refs/tags/v') && github.event_name != 'workflow_dispatch')",
             "(github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v'))", "event_name == 'push'"),
            # tag clause dropped: a v* tag publish would not update the page
            (" || (startsWith(github.ref, 'refs/tags/v') && github.event_name != 'workflow_dispatch')", "",
             "workflow_call on refs/tags/v1.0.0"),
            # manual runs from a tag allowed
            (" && github.event_name != 'workflow_dispatch'", "", "workflow_dispatch on refs/tags/v1.0.0"),
            # any branch allowed
            ("github.ref == 'refs/heads/main' ||", "startsWith(github.ref, 'refs/heads/') ||", "refs/heads/feature"),
            # back to a fixed artifact name: a re-run would find two github-pages artifacts
            ("          name: github-pages-${{ github.run_id }}-${{ github.run_attempt }}\n", "          name: github-pages\n",
             "unique per run attempt"),
            ("          name: github-pages-${{ github.run_id }}-${{ github.run_attempt }}\n", "",
             "unique per run attempt"),
            # retry runs unconditionally, or fails softly
            ("        id: deploy_retry\n        if: steps.deploy.outcome == 'failure'\n", "        id: deploy_retry\n",
             "deploy retry must be"),
            ("        id: deploy_retry\n", "        id: deploy_retry\n        continue-on-error: true\n",
             "must not continue on error")):
        assert pages.count(old_s) == 1, f"pages.yml: {old_s!r}"
        got = check_pages(pages.replace(old_s, new_s))
        assert any(needle in p for p in got), f"pages.yml mutation {needle!r} not caught: {got}"
    problems += [f"(pages.yml) {p}" for p in pages_problems]
    # Docs: the R2 secrets belong to the gcs-publish environment, never the repository.
    root = WORKFLOW.parents[2]
    for doc in ("docs/SETUP.md", "README.md", "scripts/setup-gcp.sh"):
        t = (root / doc).read_text(encoding="utf-8")
        if "gcs-publish" not in t or not re.search(r"gcs-publish(`)? environment( secrets|\s*\(Settings)", t):
            problems.append(f"{doc} must say the R2 secrets are gcs-publish environment secrets")
        if re.search(r"(?i)(set|add|store|keep)[^.\n]{0,40}repository secrets? `?R2_", t):
            problems.append(f"{doc} must not tell anyone to store the R2 secrets as repository secrets")
    if problems:
        for p in problems:
            print(f"FAIL {WORKFLOW.relative_to(WORKFLOW.parents[2])}: {p}")
        return 1
    print("OK: publish.yml hands the kit zip over with archive: false and downloads it with skip-decompress: true")
    print("OK: gating unchanged; run-name, job summaries and the always-running summary job are in place")
    print("OK: only the publish job reads the three R2 secrets, only for publish_r2.py; links are public R2 URLs")
    print("OK: pages job and pages.yml deploy only after a real publish, without secrets, in github-pages")
    return 0


if __name__ == "__main__":
    sys.exit(main())
