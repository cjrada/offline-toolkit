# Publishing built kits to Google Cloud Storage

`.github/workflows/publish-kit` (file `publish.yml`) builds a kit with `tools/build-kit.sh --strict` and
uploads it to a public GCS bucket:

```
gs://<bucket>/kits/<tier>/<version>/offline-survival-kit-<tier>-<version>.zip
                                    SHA256SUMS  manifest.json  kit.lock
gs://<bucket>/kits/<tier>/latest.json        <- points at the newest version
```

Download URL: `https://storage.googleapis.com/<bucket>/kits/small/latest.json` (then follow `url`).
Users should check the zip against `SHA256SUMS` (Linux `sha256sum -c SHA256SUMS`, Windows
`certutil -hashfile <zip> SHA256`).

Hosted runners have limited disk, so CI publishes the **small** (~7 GB) and **base** (no models,
ZIMs or map) tiers. Medium/large are built locally with `tools/build-kit.sh` - they download from
the original publishers with the same checksum checks.

## Why this is safe in a public repository

| Threat | Mitigation |
|---|---|
| Fork / PR runs the publisher | Workflow has no `pull_request*` trigger; jobs require `github.repository == 'cjrada/offline-toolkit'` and ref `main` or `kit-v*`. |
| Leaked cloud key | There is no key. GitHub OIDC -> Workload Identity Federation gives a ~1 h token. |
| Token usable from other repos/branches/workflows | WIF attribute condition pins the numeric repo ID, the `gcs-publish` environment subject and this workflow file. |
| Someone pushes a branch and dispatches | `gcs-publish` environment: deployment refs limited to `main` + `kit-v*` tags, **required reviewer**. |
| Malicious/compromised upstream download | `--strict`: every file must match a SHA-256 pinned in `tools/kit.lock`, which changes only through reviewed commits (CODEOWNERS). Downloads are https-only (also on redirects). |
| Compromised download running code during build | The build job has no `id-token` permission and no secrets; the publish job only runs `sha256sum`, `unzip -Z1`, python stdlib and the runner's `gcloud`. |
| Overwriting / deleting published kits | Service identity may only *create* objects (`objectCreator`); versions are write-once (`if-generation-match=0`). Only `kits/*/latest.json` may be replaced (IAM condition). |
| Publishing non-redistributable content | `--with-post-disaster` is never used in CI, and the publish job rejects kits whose manifest says `redistributable: false` or that contain the post-disaster ZIM. |
| Hijacked third-party actions | Only first-party `actions/*` are used, pinned to commit SHAs; Dependabot proposes updates. Auth uses no third-party action. |
| Script injection via inputs | Inputs are validated (`tier` is a choice, `version` matches `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`) and reach the shell only through `env:`. |

## One-time setup

### 1. Google Cloud (run in Cloud Shell or with `gcloud`)

```sh
PROJECT=your-project-id
BUCKET=your-public-kits-bucket            # globally unique
LOCATION=australia-southeast1
REPO=cjrada/offline-toolkit
REPO_ID=$(curl -s https://api.github.com/repos/$REPO | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
NUM=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')

gcloud services enable iam.googleapis.com sts.googleapis.com iamcredentials.googleapis.com --project "$PROJECT"

# Workload Identity pool + GitHub provider, locked to this repo, environment and workflow
gcloud iam workload-identity-pools create github --project "$PROJECT" --location global \
  --display-name "GitHub Actions"
gcloud iam workload-identity-pools providers create-oidc offline-toolkit --project "$PROJECT" \
  --location global --workload-identity-pool github \
  --issuer-uri https://token.actions.githubusercontent.com \
  --attribute-mapping 'google.subject=assertion.sub,attribute.repository_id=assertion.repository_id,attribute.workflow_ref=assertion.workflow_ref' \
  --attribute-condition "assertion.repository_id == '$REPO_ID' && assertion.sub == 'repo:$REPO:environment:gcs-publish' && assertion.workflow_ref.startsWith('$REPO/.github/workflows/publish.yml@')"

# Public-read bucket
gcloud storage buckets create "gs://$BUCKET" --project "$PROJECT" --location "$LOCATION" \
  --uniform-bucket-level-access --no-public-access-prevention
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \
  --member allUsers --role roles/storage.objectViewer

# Publisher identity (no service account, no key): the GitHub environment subject
P="principal://iam.googleapis.com/projects/$NUM/locations/global/workloadIdentityPools/github/subject/repo:$REPO:environment:gcs-publish"
# create new objects only
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --member "$P" --role roles/storage.objectCreator
# replace only kits/<tier>/latest.json
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --member "$P" --role roles/storage.objectUser \
  --condition "title=latest-pointer-only,expression=resource.name.startsWith('projects/_/buckets/$BUCKET/objects/kits/') && resource.name.endsWith('/latest.json')"

echo "GCP_WIF_PROVIDER = projects/$NUM/locations/global/workloadIdentityPools/github/providers/offline-toolkit"
echo "GCS_BUCKET       = $BUCKET"
```

If your organisation policy forbids `allUsers` (domain-restricted sharing), put Cloud CDN / a load
balancer in front instead, or keep the bucket private and share signed URLs.
Consider a budget alert: public egress of a 7 GB file is billed per download.

### 2. GitHub repository settings

1. **Settings -> Environments -> New environment `gcs-publish`**
   * Required reviewers: yourself (and tick *Prevent self-review* once there is a second maintainer).
   * Deployment branches and tags: *Selected* -> branch `main`, tag `kit-v*`.
   * Environment **variables** (not secrets - they are not sensitive): `GCP_WIF_PROVIDER`, `GCS_BUCKET`
     with the values printed above.
2. **Settings -> Actions -> General**: Workflow permissions *Read repository contents*; fork pull
   request workflows *Require approval for all external contributors*.
3. **Rulesets**: protect `main` (PR + review from code owners, no force push) and tags `kit-v*`
   (only maintainers may create; no update/delete).
4. `.github/CODEOWNERS` already requires owner review for workflows, `tools/` and `tools/kit.lock`.

### 3. Pin the downloads (first time, and whenever you update versions)

`--strict` refuses anything not pinned, so the first CI run fails until `tools/kit.lock` is complete.
On a Linux box with internet:

```sh
bash tools/build-kit.sh --tier small --update --no-zip   # resolves newest builds, records SHA-256s
less dist/small/kit.lock                                 # review URLs; read the WARNINGS it printed
cp dist/small/kit.lock tools/kit.lock
git diff tools/kit.lock                                  # commit via a reviewed PR
```

Hashes come from the publisher where one exists (Hugging Face LFS sha256 via immutable
`/resolve/<commit>/` URLs, Kiwix `.sha256`, GitHub release digests, npm sha512 integrity);
items flagged *trusting first download* (e.g. the PDFs and map style assets) have no publisher
checksum - spot-check them before committing. The map extract is derived data: its source build
is pinned and its hash is recorded, a mismatch only warns.

## Publishing

* **Manual:** Actions -> *publish-kit* -> Run workflow (branch `main`) -> tier, optional version ->
  approve the `gcs-publish` deployment when prompted.
* **Tag:** `git tag kit-v2026.11.16 && git push origin kit-v2026.11.16` publishes the small tier
  as version `2026.11.16` (after approval).

A version can be published once; re-running with the same version fails at upload by design.
