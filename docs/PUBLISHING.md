# Building and publishing kits to Google Cloud Storage

This repository is public, so publishing is set up with **no stored secrets**: GitHub Actions proves
who it is with a short-lived OIDC token, and Google Cloud accepts that token only for this repository,
the `main` branch and the `gcs-publish` environment (Workload Identity Federation).

## 1. Pin every download (once, and whenever the manifest changes)

`tools/kit-manifest.json` lists every file a kit contains. CI builds with `--frozen`, which refuses any
item without a `sha256`. To pin, on a trusted machine with internet access:

```sh
python3 tools/build_kit.py --check            # what is pinned / unpinned / unverified URL
python3 tools/build_kit.py --lock             # downloads unpinned small-tier items (~6 GB), writes sha256 + size
python3 tools/build_kit.py --lock --tier large   # if you also want medium/large pinned (~190 GB)
git diff tools/kit-manifest.json              # review, then commit
```

Before locking, check the items marked `"verified_url": false`. They follow each publisher's naming
scheme but had not been fetched when the manifest was written: Kiwix ZIM dates change every month or so,
and the PDF mirrors vary. A 404 stops the build with the URL that failed. Also replace `refs/heads/main`
in the `map-assets` URL with a commit SHA of `protomaps/basemaps-assets` so that archive cannot change.
Prefer Hugging Face `resolve/<commit>/` URLs over `resolve/main/` for the same reason. With a pinned hash
a changed file only fails the build; it is never used.

Only the map (`map-australia`, extracted from a dated Protomaps build) is unpinnable. It is map data,
not code, and its hash is recorded in the kit's `SHA256SUMS` and `KIT-INFO.json`.

## 2. Build locally (optional)

```sh
sudo apt install poppler-utils golang-go      # pdftotext for the manuals index, Go for go-pmtiles
python3 tools/build_kit.py --zip              # -> dist/offline-toolkit-small/ and dist/offline-toolkit-small-<ver>.zip
python3 tools/build_kit.py --tier medium --with post-disaster   # personal kit; can never be published
```

To make a USB drive, copy `dist/offline-toolkit-small/` to an exFAT drive, or unzip the zip onto it.

## 3. One-time Google Cloud setup

Replace the values in the first block. Run this as a project owner, in Cloud Shell or with `gcloud`.

```sh
PROJECT=my-project
BUCKET=my-offline-kits                      # globally unique name
REGION=australia-southeast1
REPO=cjrada/offline-toolkit
REPO_ID=$(curl -s https://api.github.com/repos/$REPO | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
PROJECT_NUM=$(gcloud projects describe $PROJECT --format='value(projectNumber)')

gcloud services enable iam.googleapis.com sts.googleapis.com iamcredentials.googleapis.com storage.googleapis.com --project $PROJECT

# Bucket: uniform access, soft delete as an undo window. Add public read only if you want public downloads.
gcloud storage buckets create gs://$BUCKET --project $PROJECT --location $REGION --uniform-bucket-level-access --soft-delete-duration=7d
gcloud storage buckets add-iam-policy-binding gs://$BUCKET --member=allUsers --role=roles/storage.objectViewer   # public downloads (optional)

# Workload Identity pool + GitHub provider. The condition is the security boundary: only this repo
# (by numeric id, so a deleted and re-created repo with the same name cannot use it), only main,
# only jobs running in the gcs-publish environment.
gcloud iam workload-identity-pools create github --project $PROJECT --location global --display-name "GitHub Actions"
gcloud iam workload-identity-pools providers create-oidc offline-toolkit --project $PROJECT --location global \
  --workload-identity-pool github --issuer-uri https://token.actions.githubusercontent.com \
  --attribute-mapping 'google.subject=assertion.sub,attribute.repository_id=assertion.repository_id,attribute.ref=assertion.ref,attribute.environment=assertion.environment' \
  --attribute-condition "assertion.repository_id=='$REPO_ID' && assertion.ref=='refs/heads/main' && assertion.environment=='gcs-publish'"

# Grant the federated identity (no service account, no key) create-only access to the bucket...
PRINCIPAL="principalSet://iam.googleapis.com/projects/$PROJECT_NUM/locations/global/workloadIdentityPools/github/attribute.repository_id/$REPO_ID"
gcloud storage buckets add-iam-policy-binding gs://$BUCKET --member "$PRINCIPAL" --role roles/storage.objectCreator
# ...plus overwrite rights limited to the latest.json pointers (IAM condition on the object name).
gcloud storage buckets add-iam-policy-binding gs://$BUCKET --member "$PRINCIPAL" --role roles/storage.objectUser \
  --condition 'title=latest-pointer-only,expression=resource.name.startsWith("projects/_/buckets/'$BUCKET'/objects/kits/") && resource.name.endsWith("/latest.json")'

echo "GCP_WIF_PROVIDER = projects/$PROJECT_NUM/locations/global/workloadIdentityPools/github/providers/offline-toolkit"
echo "GCS_BUCKET       = $BUCKET"
```

`roles/storage.objectCreator` can add objects but cannot delete or replace them. Together with the
uploader's `ifGenerationMatch=0` precondition, a published version can never be changed afterwards.
Only `kits/*/latest.json` can be overwritten. If you would rather use a service account, grant it the
same two roles, give the principal set `roles/iam.workloadIdentityUser` on it, and set
`GCP_SERVICE_ACCOUNT`. Never create a service-account key.

## 4. One-time GitHub setup

1. **Settings → Environments → New environment** `gcs-publish`:
   * *Required reviewers*: you (each publish then waits for your approval).
   * *Deployment branches and tags*: **Selected branches → `main`**.
   * *Environment variables* (not secrets, nothing here is sensitive): `GCP_WIF_PROVIDER`, `GCS_BUCKET`,
     and optionally `GCP_SERVICE_ACCOUNT`.
2. **Settings → Actions → General**: set *Fork pull request workflows from outside collaborators* to
   *Require approval for all outside collaborators*, and keep *Workflow permissions* at **Read repository
   contents**.
3. Protect `main` (require a pull request and the `lint` check), so the code the publish job runs is reviewed.

## 5. Publish

**Actions → publish-kit → Run workflow** on `main`. `dry_run` is on by default: it builds and checks
everything but uploads nothing. Run it again with `dry_run` off to publish. Each run creates:

```
gs://BUCKET/kits/offline-toolkit-small/<yyyymmdd>-<commit>/offline-toolkit-small-<ver>.zip   (+ .sha256, KIT-INFO.json)
gs://BUCKET/kits/offline-toolkit-small/latest.json                                           (points at the newest)
```

Publishing the same commit twice fails on purpose, because versions are immutable. The uploader also refuses:
* kits with non-redistributable content, such as anything built `--with post-disaster`. It checks the
  `KIT-INFO.json` inside the zip, not a separate file.
* kits built from a dirty git tree.
* kits with unpinned downloads.

GCS checks every upload against the MD5 the uploader sends.

Downloaders can check the zip with `sha256sum -c offline-toolkit-small-<ver>.zip.sha256`. Inside the kit,
`sha256sum -c SHA256SUMS` checks every file. Public buckets pay internet egress for every download: a 6 GB kit
costs roughly US$0.70-1.20 per download, so watch the billing or keep the bucket private and share signed URLs.

## Why not the usual actions?

The workflow uses only `actions/checkout`, pinned to a commit SHA. Authentication and upload are about 250
lines of standard-library Python in `tools/publish_gcs.py`: OIDC → STS token exchange → resumable upload.
There are no third-party actions or extra dependencies to audit or pin, and the same script works from a
laptop with `--auth gcloud`.
