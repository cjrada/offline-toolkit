# Setup: building, publishing and downloading kits

Kits are built from `tools/kit-manifest.json` and stored in a **private** Google Cloud Storage bucket.
This repository is public, so publishing uses **no stored secrets**. GitHub Actions proves who it is with a
short-lived OIDC token, and Google accepts that token only for this repository, `main` or a `v*` tag, and
jobs in the protected `gcs-publish` environment (Workload Identity Federation).

| Tier | Built on | Size (approx.) | How |
|---|---|---|---|
| small | GitHub runner | 6 GB | `publish-kit` workflow, or push a `v*` tag |
| medium | Cloud Build (600 GB disk) | 42 GB | `publish-kit` workflow, tier = medium |
| large | Cloud Build (600 GB disk) | 175 GB | `publish-kit` workflow, tier = large |

## 1. One-time Google Cloud setup

Open [Cloud Shell](https://shell.cloud.google.com) as a project owner, clone the repo and run:

```sh
git clone https://github.com/cjrada/offline-toolkit && cd offline-toolkit
bash scripts/setup-gcp.sh my-existing-project      # bucket "<project>-kits"
# or create one: bash scripts/setup-gcp.sh offline-toolkit-<suffix> --create-project --billing-account 0X0X0X-0X0X0X-0X0X0X
# preview without changing anything: add --dry-run
```

**Project IDs are global** across all of Google Cloud, so the default `offline-toolkit` is almost certainly
taken. Pass an existing project of yours, or a unique new ID such as `offline-toolkit-2611` with
`--create-project`. The script prints this hint if creation fails.

**Billing.** Cloud Storage and Cloud Build need a billing account, so the script checks billing before
anything else:
* `--create-project` requires `--billing-account` (list yours with `gcloud billing accounts list`).
* `--billing-account` links only a project that has **no** billing account yet. If the project is already
  linked to a different account, the script stops and doesn't move it. Re-run without the flag to keep
  the current account, or move it yourself with `gcloud billing projects link`.
* An existing project without billing, or one whose billing account is closed, stops the script with a hint.

The script never prompts (`CLOUDSDK_CORE_DISABLE_PROMPTS=1`): anything gcloud would ask about is an
error, and the script stops with a non-zero exit. Every IAM grant names its condition explicitly
(`--condition=None` or the `latest.json` condition); `tests/test_setup_gcp.py` checks this in CI.

You can re-run it at any time. It only creates what is missing and resets the security settings. It sets up:

* **APIs:** storage, iam, iamcredentials, sts, cloudbuild, cloudresourcemanager and logging.
* **Bucket `<project>-kits`** in `australia-southeast1`:
  * uniform bucket-level access;
  * **public access prevention enforced**;
  * 7-day soft delete (an undo window);
  * any `allUsers`/`allAuthenticatedUsers` binding is removed. Kits are private.
* **Workload Identity pool `github`, provider `offline-toolkit`.** Tokens are accepted only when *all* of these hold:
  * `repository_id` is this repo's numeric id, so a deleted and re-created repo with the same name doesn't qualify;
  * `repository == 'cjrada/offline-toolkit'`;
  * `ref` is `refs/heads/main` or starts with `refs/tags/v`;
  * `environment == 'gcs-publish'`.
* **Service accounts.** Neither has a key.
  * `kit-publisher`: the workflow impersonates it.
  * `kit-builder`: Cloud Build runs as it.
* **Grants (least privilege):**

  | Who | Role | On | Allows |
  |---|---|---|---|
  | both SAs | `storage.objectCreator` | bucket | create objects; no replace, no delete |
  | both SAs | `storage.objectUser`, IAM condition `kits/…/latest.json` | bucket | update the "latest" pointers only |
  | kit-publisher | `cloudbuild.builds.editor`, `serviceusage.serviceUsageConsumer` | project | start builds and check their status |
  | kit-publisher | `iam.serviceAccountUser` | kit-builder | builds can only run as kit-builder |
  | kit-builder | `logging.logWriter` | project | build logs (`CLOUD_LOGGING_ONLY`) |
  | kit-builder | `storage.objectViewer`, IAM condition `cache/…` | bucket | read the download cache (no list) |
  | GitHub principal set (this repo id) | `iam.workloadIdentityUser` | kit-publisher | — |

  Together with the uploader's `ifGenerationMatch=0` precondition, **a published version can never be
  changed or deleted** by CI. Only `kits/<tier>/latest.json` moves.

At the end the script prints the exact `gh` commands for step 2.

## 2. One-time GitHub setup

1. **Environment `gcs-publish`.** In Settings → Environments → New environment:
   * *Required reviewers*: **Chris**. Every publish waits for this approval.
   * *Deployment branches and tags*: **Selected** → branch `main` and tag `v*`.

   Or use the `gh api` commands the script printed.
2. **Environment variables.** These are not secrets and nothing here is sensitive. Paste the three lines the
   script printed into any shell where `gh` is logged in. **Cloud Shell may not have `gh`.** In that case use
   the web UI: Settings → Environments → **gcs-publish** → *Add environment variable*, once each for
   `GCP_WIF_PROVIDER`, `GCP_SERVICE_ACCOUNT` and `GCS_BUCKET`, with the values the script printed:
   ```sh
   gh variable set GCP_WIF_PROVIDER    --env gcs-publish --body "projects/<num>/locations/global/workloadIdentityPools/github/providers/offline-toolkit" -R cjrada/offline-toolkit
   gh variable set GCP_SERVICE_ACCOUNT --env gcs-publish --body "kit-publisher@<project>.iam.gserviceaccount.com" -R cjrada/offline-toolkit
   gh variable set GCS_BUCKET          --env gcs-publish --body "<project>-kits" -R cjrada/offline-toolkit
   ```
3. **Settings → Actions → General.**
   * Fork pull request workflows: *Require approval for all outside collaborators*.
   * Workflow permissions: **Read repository contents**.
4. **Protect `main`.** Require a pull request and the `lint` check, so everything the publish job runs has been reviewed.
5. **Protect `v*` tags.** Add a tag ruleset limited to maintainers.

## 3. Publish

* **Small kit from a tag:** `git tag v1.0.0 && git push origin v1.0.0`. The version is the tag name.
* **Any tier by hand:** Actions → **publish-kit** → Run workflow on `main`.
  * Pick the tier and optionally a version. The default version is `yyyymmdd-<commit>`.
  * `dry_run` is on by default. For small it builds and checks everything without uploading. For medium/large it only validates the inputs and pins.
  * Untick it to publish.
* **Reading a run:** the run list shows `publish-kit · <tier> · <version> · dry run|publish` (a run on a
  ref other than `main` / `v*` also says it was skipped). The run's **Summary** tab has the inputs, each
  job's result and the outcome, plus the Cloud Build link (medium/large) and, after an upload, the `gs://`
  path and Cloud Console links to the zip, its `.sha256`, `KIT-INFO.json` and `latest.json`. The bucket is
  private, so those links need a Google account with access; plain `storage.googleapis.com` URLs won't work.

What happens:

* Both jobs validate the inputs with `tools/ci_inputs.sh` (tier, version pattern, manifest pins).
* **small** runs as two jobs, so build code never shares a runner with Google credentials:
  * **`build`** has `contents: read` only: no OIDC token, no environment, no environment variables.
    It frees about 30 GB of disk (fails early below 25 GB), installs poppler and the hash-pinned libzim,
    builds with `--frozen`, runs `publish_gcs.py --dry-run`, and records the zip's sha256. Then it uploads
    the zip as-is (`archive: false`) as a workflow artifact with 1-day retention.
  * **`publish`** runs in the `gcs-publish` environment and is the only job that may request an OIDC token.
    It installs and builds nothing. It re-validates everything the build job passed on, downloads the
    artifact by id into a new empty directory with `skip-decompress: true` (an `archive: false` zip would
    otherwise be extracted on download), and checks two things: the file's sha256 against the build
    job's output, and that `KIT-INFO.json` inside the zip has the expected tier and version. It then repeats
    the `--dry-run` with the real bucket. Only after that does it authenticate and upload with
    `tools/publish_gcs.py`.
  * A small **dry run** still runs both jobs, so it also tests the hand-off and the environment's variables.
    The publish job still waits for the `gcs-publish` approval, but it skips authentication and upload.
* **Artifact size and timing (small, ~6 GB zip):**
  * GitHub documents no size limit for a single artifact. upload-artifact's README lists only 500 artifacts per job and the account's storage quota.
  * Artifact storage is metered only for private repositories. This repo is public, so the zip doesn't count against a quota.
  * Expect the upload and the download to take a few minutes each. This is an estimate; check the step timings in the first real run.
  * **Approve the publish job within a day.** After 1 day the artifact expires and the download fails, and you have to re-run the whole workflow.
  * In a public repository, anyone signed in to GitHub can download a run's artifacts while they exist. Only
    redistributable content goes in the small kit (the uploader enforces it), but it will be public for that day.
* **medium/large:** the `build` job is skipped and the `publish` job runs `gcloud builds submit --no-source --config cloudbuild.yaml`.
  * Cloud Build clones this repo at exactly the workflow's commit and verifies the hash.
  * It builds on an E2_HIGHCPU_8 machine with a 600 GB disk and uploads the same way, as `kit-builder`.
  * The workflow polls until the build finishes (up to 350 minutes; a failed status query is retried, not fatal).
    **A cold large build (empty cache) can take longer: `wikipedia_en_all_maxi` alone is ~127 GB. Then the
    publish run turns red after 350 minutes while Cloud Build keeps going (up to 12 h) and still uploads
    if it succeeds; that is expected.** Check the result with `gcloud builds describe <id> --project <project>`
    (the id is in the publish log) or `gcloud storage cat gs://BUCKET/kits/large/latest.json`.
  * Images are pinned by digest. There are no Cloud Build GitHub triggers.
  * Steps: `checkout` → `cache-pull` → `fetch` → `cache-push` ∥ `build` → `gate` → `upload` (see the top of
    `cloudbuild.yaml`). `cache-pull` also runs the upload tooling once, so a broken upload step fails in the
    first minute rather than after the build.
  * Cloud Build cancels all running steps as soon as one fails, so `fetch` and `build` record their exit
    code instead of failing. `cache-push` then always finishes sharing what was already downloaded and
    verified, even after a failed fetch or build, and `gate` fails the build before `upload`, so a
    partial kit is never published.

### Download cache (medium/large)

Big downloads are kept in the kit bucket so each file comes from upstream once:

```
gs://BUCKET/cache/sha256/<sha256>                     a pinned download (model, ZIM, binary), named by its hash
gs://BUCKET/cache/pmtiles/<key>-australia.pmtiles     the map extract (+ .sha256 next to it); <key> is from the build actually used
```

* `build_kit.py --cache-plan` lists the files of the tier (≥ 32 MB). `tools/gcs_cache.sh pull` copies the
  ones already cached; `fetch` downloads the rest; `tools/gcs_cache.sh push` uploads new ones.
* **The cache is a faster source, not a trusted one.** Every pinned file is checked against the manifest's
  sha256 before use, wherever it came from; a mismatching cached copy is set aside (`*.bad`) and downloaded
  again from upstream. A map extract (which cannot be pinned) must match its `.sha256`.
* Only Cloud Build writes the cache, as `kit-builder`, create-only: only files `build_kit.py` verified are
  pushed, with `--if-generation-match=0`, and the account cannot replace or delete objects. Pull requests
  run only `lint` (no credentials). `kit-builder` may read `cache/` only (IAM condition); listing is not granted.
* After this change, re-run `scripts/setup-gcp.sh` once to add the `cache/` read grant. Without it every
  pull is a miss and the build downloads from upstream as before.
* A cache push failure is a warning, never a failed build. To drop a bad or old cache object, delete it as
  project owner (`gcloud storage rm gs://BUCKET/cache/sha256/<sha256>`). Storage for a full large-tier
  cache is roughly 190 GB.
* Downloads not in the cache use `aria2c` (16 connections). ZIMs from `download.kiwix.org` come from the
  HTTPS mirrors listed in Kiwix's official metalink (`<url>.meta4`), used only if its size and sha-256
  match the pin. If aria2c fails or the bytes don't match, the build falls back to one HTTPS stream from
  the manifest URL. No BitTorrent: seeding from Cloud Build would be paid egress.
* **On the aria2c path, integrity comes from the sha256 pin, not from HTTPS alone.** aria2c can't refuse a
  redirect to plain `http://`. So it only gets direct `https://` URLs: the metalink mirrors, or the end of
  the manifest URL's redirect chain, resolved first with the HTTPS-only opener. It never gets
  `download.kiwix.org` itself, which redirects. A redirect target that turns out to be `http://` means no
  aria2c for that file. If a server still redirected aria2c somewhere else, the pin check rejects any
  changed byte and the HTTPS-only single stream takes over.
* Every item logs its source (GCS cache, aria2c + number of mirrors, single stream, map extract), size,
  time and MB/s, and each build step ends with a summary table.

Each publish writes:

```
gs://BUCKET/kits/<tier>/<version>/offline-toolkit-<tier>-<version>.zip
gs://BUCKET/kits/<tier>/<version>/offline-toolkit-<tier>-<version>.zip.sha256
gs://BUCKET/kits/<tier>/<version>/KIT-INFO.json
gs://BUCKET/kits/<tier>/latest.json            (written last)
```

Publishing an existing version fails on purpose. The uploader also refuses:

* kits with non-redistributable content (`--with post-disaster`). It checks the `KIT-INFO.json` *inside* the zip.
* kits built from a dirty tree.
* kits with unpinned downloads.

GCS rejects any upload whose MD5 differs from what was sent.

## 4. Updating the manifest

Every item is pinned: a sha256 for downloads, or a git commit for `map-assets`. To bump something,
edit its URL and set `"sha256": null`, then on a trusted machine:

```sh
python3 tools/build_kit.py --lock --only <item-id>     # downloads it, writes sha256 + size
git diff tools/kit-manifest.json                       # review, then commit via a PR
```

Notes:

* Three manuals (FM 4-25.11, USMC Wilderness Medicine, WA DoH water) are `zim-extract` items. They are pulled from the
  pinned zimgit ZIMs, and their own sha256 is checked. This needs `libzim`:
  `pip install --require-hashes --only-binary :all: -r tools/requirements-build.txt`.
* The map (`map-australia`) is the only unpinnable item. It is a go-pmtiles extract of a daily Protomaps
  build: map data, not code. Its hash goes into the kit's `SHA256SUMS`. Protomaps deletes builds after
  about a week, so the manifest URL is only the *preferred* build. If it returns 404 (or the item has
  `"build": "latest"`), `build_kit.py` reads <https://build-metadata.protomaps.dev/builds.json> and uses
  the newest listed build, or the next newest if that one fails (at most 5). Only builds whose tile
  schema major version (`version` in builds.json) equals the item's `tiles_major` are used, because the
  pinned `js-basemaps` style only renders that schema: bump both together. It always builds the URL
  itself, as `https://build.protomaps.com/<YYYYMMDD>.pmtiles`. An extract already in the cache is reused
  only if it is of the manifest's build or one of those 5 candidates. The build actually used is in
  `KIT-INFO.json` (`url`, `protomaps_build`, `tiles_version`, `requested_url`, `fallback`).
* Updating the date is optional, but worth doing now and then. Once the manifest's build is gone, each
  run falls back to the newest build, which changes daily. So most runs miss the GCS map cache and push
  a new ~GB object under `cache/pmtiles/`. A lifecycle rule on that prefix also keeps it in check.

## 5. Build locally (optional)

```sh
sudo apt install poppler-utils golang-go python3-venv
go version   # go-pmtiles needs Go >= 1.25; if older, install a newer Go or `export GOTOOLCHAIN=go1.25.14`
python3 -m venv .venv && .venv/bin/pip install --require-hashes --only-binary :all: -r tools/requirements-build.txt
.venv/bin/python tools/build_kit.py --zip                    # small -> dist/offline-toolkit-small/ (+ zip)
.venv/bin/python tools/build_kit.py --tier medium --with post-disaster   # personal use only; cannot be published
```

## Rough costs (australia-southeast1, USD, Nov 2026)

These are estimates. Check <https://cloud.google.com/storage/pricing> and
<https://cloud.google.com/build/pricing> for current prices.

| | small | medium | large |
|---|---|---|---|
| Storage per kept version (Standard, ~$0.023/GB-month) | ~$0.15/month | ~$1/month | ~$4/month |
| One download to a home PC (internet egress, ~$0.12-0.19/GB) | ~$1 | ~$6 | ~$25-33 |
| Build | free (GitHub runner) | ~$1-2 (E2_HIGHCPU_8 ~$0.016/min + disk) | ~$3-6 |

* Delete old versions you no longer need. CI cannot delete them, so you have to, as owner:
  `gcloud storage rm -r gs://BUCKET/kits/<tier>/<old-version>/`.
* Soft-deleted objects are billed for 7 more days.
* Downloads within Google Cloud in the same region (for example from Cloud Shell) are free, but then
  you still have to get the file to your PC.
