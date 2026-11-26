# Setup: building, publishing and downloading kits

Kits are built from `tools/kit-manifest.json`, published to **Cloudflare R2** (public, free egress) and
listed on the **GitHub Pages downloads site**, <https://cjrada.github.io/offline-toolkit/>.
**Google Cloud Storage is now only the private download cache of Cloud Build** (plus any kits published
there before this change; see [Retiring the old GCS kits](#retiring-the-old-gcs-kits)). CI no longer
uploads kits to GCS and never deletes anything there.

Credentials:
* **R2:** an R2 API token with **Object Read & Write on the kit bucket only** (no account-wide R2 admin).
  GitHub keeps it as three **`gcs-publish` environment secrets** (`R2_ACCESS_KEY_ID`,
  `R2_SECRET_ACCESS_KEY`, `R2_ACCOUNT_ID`), so it is released only to the `publish` job, only after the
  required approval, and only on `main` or a `v*` tag. Within that job only the two steps that run
  `tools/publish_r2.py` get the values. Cloud Build reads the same values from Secret Manager. They are
  never Cloud Build substitutions, job outputs or summary text, and the publisher never prints them or its
  signed request headers.
* **This is a deliberate exception** to the repository's otherwise keyless design ("WIF only, no stored
  secrets"): R2 has no keyless federation for GitHub Actions or Google Cloud, so a scoped, stored token is the
  only option. Keep it narrow (one bucket, object read/write) and rotate it if it might have leaked.
* **Google:** no stored secrets. GitHub Actions proves who it is with a short-lived OIDC token, and Google
  accepts that token only for this repository, `main` or a `v*` tag, and jobs in the protected
  `gcs-publish` environment (Workload Identity Federation). It is now only needed to start Cloud Build for
  medium/large.

| Tier | Built on | Size (approx.) | How |
|---|---|---|---|
| small | GitHub runner | 7 GB | `publish-kit` workflow, or push a `v*` tag |
| medium | Cloud Build (600 GB disk) | 41 GB | `publish-kit` workflow, tier = medium |
| large | Cloud Build (600 GB disk) | 173 GB | `publish-kit` workflow, tier = large |

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

* **APIs:** storage, iam, iamcredentials, sts, cloudbuild, cloudresourcemanager, logging and secretmanager.
* **Bucket `<project>-kits`** in `australia-southeast1` (now the Cloud Build download cache, `cache/`):
  * uniform bucket-level access;
  * **public access prevention enforced**;
  * 7-day soft delete (an undo window);
  * any `allUsers`/`allAuthenticatedUsers` binding is removed. The bucket stays private: public kit
    downloads come from R2, never from GCS.
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
  | kit-builder | `secretmanager.secretAccessor` | each of the 3 R2 secrets (not the project) | read the R2 credentials in Cloud Build |
  | GitHub principal set (this repo id) | `iam.workloadIdentityUser` | kit-publisher | — |

  Neither account can replace or delete anything in the GCS bucket (the old `kits/<tier>/latest.json`
  grant is still there but nothing writes it any more). kit-publisher gets no Secret Manager access: GitHub
  has its own copy of the R2 secrets.
* **Secret Manager secrets** `r2-access-key-id`, `r2-secret-access-key`, `r2-account-id` (automatic
  replication), created **empty** if missing. The script never reads, writes or prints a secret value; at
  the end it lists the secrets that still have no version. See step 2.4 below.

At the end the script prints the exact `gh` commands for step 2, and the Secret Manager commands.

## 2. One-time GitHub, Cloudflare R2 and Secret Manager setup

Already done (don't redo): the R2 bucket (Standard class, public access on), an R2 API token with
read/write on that bucket, the secrets `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_ACCOUNT_ID`, and
Settings → Pages → Source = **GitHub Actions**.

**Move the three R2 secrets into the `gcs-publish` environment.** Repository secrets can be read by any
workflow run that someone with write access pushes on any branch; environment secrets are released only to
jobs that pass the environment's approval and branch/tag rules. The workflow code is the same either way
(an environment secret overrides a repository secret of the same name):
1. Settings → Environments → **gcs-publish** → *Environment secrets* → add `R2_ACCESS_KEY_ID`,
   `R2_SECRET_ACCESS_KEY`, `R2_ACCOUNT_ID` with the same values.
2. Settings → Secrets and variables → Actions → *Repository secrets*: delete the three R2 ones.
3. In the Cloudflare dashboard, check the token is **Object Read & Write**, limited to the kit bucket only
   (not *Admin Read & Write*, not all buckets). If it is broader, make a new narrow one and replace the
   values in GitHub and Secret Manager.

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
   `GCS_BUCKET` is still needed: Cloud Build keeps its download cache there.
3. **Repository variables for R2 (still to do).** Settings → Secrets and variables → Actions →
   **Variables** → *New repository variable*:
   * `R2_BUCKET` = the R2 bucket name
   * `R2_PUBLIC_URL` = the public base URL (r2.dev or custom domain), no trailing slash, e.g.
     `https://pub-<id>.r2.dev` or `https://downloads.<your-domain>`

   ```sh
   gh variable set R2_BUCKET     --body "<bucket name>" -R cjrada/offline-toolkit
   gh variable set R2_PUBLIC_URL --body "https://<public base URL>" -R cjrada/offline-toolkit
   ```
   Every publish-kit run checks them first, **dry runs included**, and fails in its first step with this
   hint if either is missing or malformed (and if any of the three R2 secrets is empty). A dry run also lists
   the bucket with the token (read-only), so a bad token shows up before a real publish.
   Cloudflare rate-limits `r2.dev` URLs and calls them unsuitable for production; for downloads this big a
   custom domain on the bucket is better, and only `R2_PUBLIC_URL` changes.
4. **Secret Manager values for Cloud Build (medium/large; still to do).** Re-run `scripts/setup-gcp.sh`
   once (it enables the API, creates the three empty secrets and grants kit-builder access), then in Cloud
   Shell add the **same values that are in the GitHub secrets**, pasted on stdin so they are not on the
   command line or in your shell history (paste, Enter, Ctrl-D):
   ```sh
   gcloud secrets versions add r2-access-key-id     --project=<project> --data-file=-
   gcloud secrets versions add r2-secret-access-key --project=<project> --data-file=-
   gcloud secrets versions add r2-account-id        --project=<project> --data-file=-
   ```
   | Secret Manager id | env in Cloud Build | same value as GitHub secret |
   |---|---|---|
   | `r2-access-key-id` | `R2_ACCESS_KEY_ID` | `R2_ACCESS_KEY_ID` |
   | `r2-secret-access-key` | `R2_SECRET_ACCESS_KEY` | `R2_SECRET_ACCESS_KEY` |
   | `r2-account-id` | `R2_ACCOUNT_ID` | `R2_ACCOUNT_ID` |

   Until they have a version, a medium/large build fails before its first step (Cloud Build cannot resolve
   the secret); the small kit doesn't need them. To rotate the token: update the GitHub secrets, add a new
   version here, then `gcloud secrets versions disable <old> --secret=<id>`.
5. **`github-pages` environment.** GitHub creates it when Pages uses GitHub Actions. It usually allows
   deployments from the default branch only. If you publish with `v*` tags, add a `v*` tag rule to it
   (Settings → Environments → github-pages → Deployment branches and tags), or the page update after a tag
   publish is refused (the kit is still published; rebuild the page with Actions → **pages** → Run workflow).
6. **Settings → Actions → General.**
   * Fork pull request workflows: *Require approval for all outside collaborators*.
   * Workflow permissions: **Read repository contents**.
7. **Protect `main`.** Require a pull request and the `lint` check, so everything the publish job runs has been reviewed.
8. **Protect `v*` tags.** Add a tag ruleset limited to maintainers.

## 3. Publish

* **Small kit from a tag:** `git tag v1.0.0 && git push origin v1.0.0`. The version is the tag name.
* **Any tier by hand:** Actions → **publish-kit** → Run workflow on `main`.
  * Pick the tier and optionally a version. The default version is `yyyymmdd-<commit>`.
  * `dry_run` is on by default. For small it builds and checks everything without uploading. For medium/large it only validates the inputs, pins and R2 access. A dry run never writes or deletes anything; its check step logs which old versions a real publish would delete.
  * Untick it to publish.
* **Reading a run:** the run list shows `publish-kit · <tier> · <version> · dry run|publish` (a run on a
  ref other than `main` / `v*` also says it was skipped). The run's **Summary** tab has the inputs, each
  job's result and the outcome, plus the Cloud Build link (medium/large) and, after a publish, the **public
  R2 download links** for the zip, its `.sha256`, `KIT-INFO.json` and `<tier>/latest.json`, and a link to
  the downloads page <https://cjrada.github.io/offline-toolkit/>. No sign-in is needed for any of them.
  The `pages` job rebuilds the downloads page a minute after the publish job.
* **Rebuild the downloads page only:** Actions → **pages** → Run workflow on `main`. It reads the public
  manifests and redeploys; no publish, no `gcs-publish` approval, no secrets.

What happens:

* Both jobs validate the inputs with `tools/ci_inputs.sh` (tier, version pattern, manifest pins).
* **small** runs as two jobs, so build code never shares a runner with credentials:
  * **`build`** has `contents: read` only: no OIDC token, no environment, no environment variables.
    It frees about 30 GB of disk (fails early below 25 GB), installs poppler and the hash-pinned libzim,
    builds with `--frozen`, runs `publish_r2.py --dry-run --offline` (kit checks only, no network), and
    records the zip's sha256. Then it uploads the zip as-is (`archive: false`) as a workflow artifact with
    1-day retention. It has no secrets.
  * **`publish`** runs in the `gcs-publish` environment and is the only job that reads the R2 secrets. It
    installs and builds nothing. It checks the R2 variables and secrets and the gcs-publish variables,
    re-validates everything the build job passed on, downloads the artifact by id into a new empty
    directory with `skip-decompress: true` (an `archive: false` zip would otherwise be extracted on
    download), and checks two things: the file's sha256 against the build job's output, and that
    `KIT-INFO.json` inside the zip has the expected tier and version. It then repeats the `--dry-run`
    against the real R2 bucket (read-only: token works, version not taken, retention plan). Only after
    that does it upload with `tools/publish_r2.py`. Small needs no Google credentials any more.
  * A small **dry run** still runs both jobs, so it also tests the hand-off, the variables and the R2 token.
    The publish job still waits for the `gcs-publish` approval, but it uploads and deletes nothing.
  * **`pages`** runs after a successful real publish only, via `.github/workflows/pages.yml`: no secrets,
    `contents: read` + `pages: write` + `id-token: write` (for deploy-pages), environment `github-pages`, and
    its own concurrency group so two deploys never overlap.
* **Artifact size and timing (small, ~7 GB zip):**
  * GitHub documents no size limit for a single artifact. upload-artifact's README lists only 500 artifacts per job and the account's storage quota.
  * Artifact storage is metered only for private repositories. This repo is public, so the zip doesn't count against a quota.
  * Expect the upload and the download to take a few minutes each. This is an estimate; check the step timings in the first real run.
  * **Approve the publish job within a day.** After 1 day the artifact expires and the download fails, and you have to re-run the whole workflow.
  * In a public repository, anyone signed in to GitHub can download a run's artifacts while they exist. Only
    redistributable content goes in the small kit (the uploader enforces it), but it will be public for that day.
* **medium/large:** the `build` job is skipped and the `publish` job runs `gcloud builds submit --no-source --config cloudbuild.yaml`.
  * Cloud Build clones this repo at exactly the workflow's commit and verifies the hash.
  * It builds on an E2_HIGHCPU_8 machine with a 600 GB disk and uploads **straight to R2** with the same
    `tools/publish_r2.py`, as `kit-builder`, with the R2 credentials from Secret Manager (`availableSecrets`
    → `secretEnv` on the preflight and upload steps only). The substitutions are `_TIER`, `_VERSION`,
    `_COMMIT`, `_BUCKET` (the GCS cache) and `_R2_BUCKET`: no secrets, because substitutions appear in logs.
  * The workflow polls until the build finishes (up to 350 minutes; a failed status query is retried, not fatal).
    **A cold large build (empty cache) can take longer: `wikipedia_en_all_maxi` alone is ~127 GB. Then the
    publish run turns red after 350 minutes while Cloud Build keeps going (up to 12 h) and still uploads
    if it succeeds; that is expected.** Check the result with `gcloud builds describe <id> --project <project>`
    (the id is in the publish log) or `curl <R2_PUBLIC_URL>/large/latest.json`, then rebuild the page with
    Actions → **pages** → Run workflow.
  * Images are pinned by digest. There are no Cloud Build GitHub triggers.
  * Steps: `checkout` → `cache-pull` → `fetch` → `cache-push` ∥ `build` → `gate` → `upload` (see the top of
    `cloudbuild.yaml`). `cache-pull` first runs the publisher with the upload step's image, Python and
    credentials (`publish_r2.py --tier --version --dry-run`: secrets present, token can list the bucket,
    version not taken), so a broken upload step, a missing secret or a bad token fails in the first minute
    rather than after a 5-hour build.
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

### What a publish writes (R2)

The bucket holds only kits, so keys have no `kits/` prefix; the public URL of an object is
`<R2_PUBLIC_URL>/<key>`.

```
<tier>/<version>/offline-toolkit-<tier>-<version>.zip          create-only (never overwritten)
<tier>/<version>/offline-toolkit-<tier>-<version>.zip.sha256   create-only
<tier>/<version>/KIT-INFO.json                                 create-only
<tier>/latest.json      newest version of the tier          (rewritten after a verified publish)
<tier>/index.json       kept versions of the tier, newest first
latest.json             newest version of each tier
index.json              kept versions of all tiers (what the downloads page is built from)
```

Order, each step only after the previous one succeeded (`tools/publish_r2.py`):

1. **Refuse** kits with non-redistributable content (`--with post-disaster`; it checks the `KIT-INFO.json`
   *inside* the zip), kits from a dirty tree, kits with unpinned downloads (the map extract excepted),
   unsafe names, and **any version that already exists** in the bucket.
2. **Upload** the zip as a multipart upload (64 MiB parts, 4 in parallel; 173 GB is ~2,600 parts, well under
   S3's 10,000), each part retried up to 8 times. Each part is signed with its SHA-256 and sent with its
   Content-MD5, so R2 rejects corrupted bytes. On failure the multipart upload is aborted, so no partial
   object is left. Then the `.sha256` sidecar and `KIT-INFO.json`, create-only (`If-None-Match: *`).
3. **Verify:** HeadObject must report `Content-Length` = the local size and the `sha256` metadata = the
   local sha256 (a multipart ETag is not an MD5 and is not used); the sidecar and `KIT-INFO.json` must read
   back byte for byte. If anything fails, the job fails and the manifests and older versions are untouched.
4. **Manifests:** write `<tier>/index.json`, `<tier>/latest.json`, `index.json`, `latest.json`, then read
   them back and check they name this version with this sha256 and size.
5. **Retention**, only now: delete versions of this tier beyond the newest N. The new version always
   counts and is never deleted, so for large (keep 1) the previous kit goes only after the new one is
   verified and listed, and a failed upload can never leave the tier empty.

| Tier | Versions kept (default) |
|---|---|
| small | 3 |
| medium | 2 |
| large | 1 |

* Change the defaults in `DEFAULT_KEEP` at the top of `tools/publish_r2.py` (reviewed in a PR), or for a
  one-off run pass `--keep small=5,large=2` / set `KIT_KEEP=small=5` (minimum 1).
* Only versions listed in `<tier>/index.json` are ever deleted, and only if their folder holds nothing but
  the three files above. A folder that is not in the index (for example the objects of a publish whose
  verification failed) is left alone and reported as a warning on every publish: delete it in the
  Cloudflare dashboard if you don't want it. A failed delete is a warning too; the version is already
  unlisted.
* A dry run prints the same plan ("would delete …") and writes and deletes nothing.

### Downloads page (GitHub Pages)

`.github/workflows/pages.yml` runs `tools/build_site.py`, which fetches the three
`<R2_PUBLIC_URL>/<tier>/index.json` files over HTTPS (no credentials; they are the source of truth, so a
small and a medium publish at the same time cannot hide one from the page) and writes one plain HTML page: the latest kit of each tier with version, date,
size, sha256 and links to the zip and `.sha256`, the previous versions still kept, size warnings, and a link
to this repository. publish-kit calls it after each real publish; run it by hand to rebuild the page.

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

## Retiring the old GCS kits

Kits published before this change are still in `gs://<GCS_BUCKET>/kits/`. CI no longer writes there and
never deletes there. **Leftover kit objects keep costing GCS Standard storage (~US$0.023/GB-month in
australia-southeast1) until you delete them**, and the bucket's 7-day soft delete bills deleted objects for
one more week. Once the R2 path is proven (a real publish of each tier you care about, downloaded and
checked from the Pages site), do this by hand in Cloud Shell. Nothing in this repository runs it.

```sh
B=<GCS_BUCKET>                                   # the gcs-publish variable
gcloud storage ls -r "gs://$B/kits/"             # what is still there
gcloud storage du -s "gs://$B/kits/"             # how much
# For anything you want to keep: download it, or confirm the same version is on R2
# (<R2_PUBLIC_URL>/index.json lists what R2 has).
gcloud storage rm -r "gs://$B/kits/"             # delete ONLY kits/
```

**Leave `gs://$B/cache/` alone:** it is Cloud Build's download cache and saves re-downloading ~190 GB for a
large build.

## 5. Build locally (optional)

```sh
sudo apt install poppler-utils golang-go python3-venv
go version   # go-pmtiles needs Go >= 1.25; if older, install a newer Go or `export GOTOOLCHAIN=go1.25.14`
python3 -m venv .venv && .venv/bin/pip install --require-hashes --only-binary :all: -r tools/requirements-build.txt
.venv/bin/python tools/build_kit.py --zip                    # small -> dist/offline-toolkit-small/ (+ zip)
.venv/bin/python tools/build_kit.py --tier medium --with post-disaster   # personal use only; cannot be published
```

## Rough costs (USD, Nov 2026)

These are estimates. Check <https://developers.cloudflare.com/r2/pricing/>,
<https://cloud.google.com/storage/pricing> and <https://cloud.google.com/build/pricing> for current prices.

**Storage and downloads (Cloudflare R2, Standard):** the first 10 GB-month is free, then about
$0.015/GB-month. **Egress is free**, so a download costs nothing however many people fetch a kit
(operations are billed per million and are negligible here).

| | small | medium | large | all kept (3/2/1) |
|---|---|---|---|---|
| Size per version | ~7 GB | ~41 GB | ~173 GB | |
| Kept versions | 3 | 2 | 1 | |
| Stored | ~21 GB | ~82 GB | ~173 GB | **~276 GB ≈ $4/month** |
| One download | free | free | free | |

For comparison, had kits stayed public on GCS, every download would have been internet egress at roughly
$0.12-0.19/GB: about $1 (small), $6 (medium) and $25-33 (large) per download, plus ~$0.023/GB-month storage.

**Builds (unchanged):**

| | small | medium | large |
|---|---|---|---|
| Build | free (GitHub runner) | ~$1-2 (E2_HIGHCPU_8 ~$0.016/min + disk) | ~$3-6 |
| Upload to R2 | free (GitHub runner) | GCP internet egress, ~$5-8 | GCP internet egress, ~$20-33 |
| GCS download cache | | ~$1/month | ~$4/month (~190 GB for a full large cache) |

* Uploading from Cloud Build to R2 leaves Google's network, so Google bills it as internet egress (the old
  same-region upload to GCS was free). That is per publish, not per download.
* During a publish the new and the old versions both exist for a few minutes (R2 bills by average storage,
  so this is negligible). A large publish whose verification fails leaves its upload behind (see
  Retention): delete it so it doesn't cost ~$2.60/month.
* Old GCS kits keep costing until you retire them (above).
