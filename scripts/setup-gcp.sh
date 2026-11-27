#!/usr/bin/env bash
# One-time (and safe to re-run) Google Cloud setup for building kits on Cloud Build from GitHub Actions.
# No service-account keys. Kits are published to Cloudflare R2; GCS is only Cloud Build's private download cache.
# Run in Cloud Shell as a project owner:
#
#   bash scripts/setup-gcp.sh [PROJECT_ID] [--create-project --billing-account XXXXXX-XXXXXX-XXXXXX]
#                             [--region australia-southeast1] [--bucket NAME] [--repo-id N] [--dry-run]
#
# Creates or updates:
#   * billing: checks it is enabled. --billing-account links a project with NO billing account; a project
#     already linked to a different account is never moved (the script stops instead).
#   * the APIs: storage, iam, iamcredentials, sts, cloudbuild, cloudresourcemanager, logging, secretmanager
#   * a PRIVATE bucket (default <project>-kits): uniform access, public access prevention ENFORCED,
#     7-day soft delete. Any allUsers / allAuthenticatedUsers binding found is removed.
#   * service accounts: kit-publisher (the workflow impersonates it) and kit-builder (Cloud Build runs as it)
#   * a Workload Identity pool "github" + OIDC provider "offline-toolkit". Tokens are accepted only for
#     repository id N AND name cjrada/offline-toolkit, ref main or a v* tag, environment gcs-publish.
#   * least-privilege grants: both SAs may CREATE objects in the bucket but not replace or delete them,
#     except kits/<tier>/latest.json. kit-publisher may start Cloud Builds that run as kit-builder.
#     kit-builder may also READ objects under cache/ (the shared download cache of Cloud Build runs; it
#     writes them create-only like everything else). Nothing else can read the bucket.
#   * Secret Manager secrets r2-access-key-id, r2-secret-access-key, r2-account-id (created EMPTY if missing;
#     this script never sees or prints a value) that only kit-builder may read (secretAccessor on each
#     secret, nothing project-wide). Cloud Build reads them to upload medium/large kits to R2. Add the values
#     yourself from stdin (see the end of the output).
set -euo pipefail
# Never prompt: any question gcloud would ask becomes an error, which set -e turns into a non-zero exit.
export CLOUDSDK_CORE_DISABLE_PROMPTS=1

REPO="cjrada/offline-toolkit"
ENV_NAME="gcs-publish"
POOL="github"; PROVIDER="offline-toolkit"
PROJECT_ID="offline-toolkit"; REGION="australia-southeast1"; BUCKET=""; REPO_ID=""
CREATE_PROJECT=0; BILLING=""; DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --create-project) CREATE_PROJECT=1 ;;
    --billing-account) BILLING="${2:?}"; shift ;;
    --region) REGION="${2:?}"; shift ;;
    --bucket) BUCKET="${2:?}"; shift ;;
    --repo-id) REPO_ID="${2:?}"; shift ;;
    --dry-run) DRY=1 ;;
    -h|--help) awk 'NR > 1 { if (/^#/) print; else exit }' "$0"; exit 0 ;;
    -*) echo "unknown option $1" >&2; exit 2 ;;
    *) PROJECT_ID="$1" ;;
  esac
  shift
done
BUCKET="${BUCKET:-$PROJECT_ID-kits}"
[[ "$PROJECT_ID" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] || { echo "invalid project id: $PROJECT_ID" >&2; exit 2; }
[[ "$BUCKET" =~ ^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$ ]] || { echo "invalid bucket name: $BUCKET" >&2; exit 2; }
BILLING="${BILLING#billingAccounts/}"; BILLING="${BILLING^^}"
if [ -n "$BILLING" ] && ! [[ "$BILLING" =~ ^[0-9A-F]{6}-[0-9A-F]{6}-[0-9A-F]{6}$ ]]; then
  echo "invalid billing account id: $BILLING (expected XXXXXX-XXXXXX-XXXXXX; see: gcloud billing accounts list)" >&2; exit 2
fi
if [ "$CREATE_PROJECT" = 1 ] && [ -z "$BILLING" ]; then
  echo "--create-project needs --billing-account: a new project has no billing, and Cloud Build and" >&2
  echo "Cloud Storage need it. List your accounts with: gcloud billing accounts list" >&2; exit 2
fi

say() { printf '\n== %s\n' "$*"; }
run() { if [ "$DRY" = 1 ]; then { printf '  [dry-run] %q' "$1"; shift; printf ' %q' "$@"; echo; } >&2; else "$@"; fi; }
exists() { [ "$DRY" = 0 ] && "$@" >/dev/null 2>&1; }

if [ -z "$REPO_ID" ]; then
  REPO_ID=$(curl -fsS "https://api.github.com/repos/$REPO" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])') \
    || { echo "could not look up the GitHub repository id; pass --repo-id N" >&2; exit 1; }
fi
[[ "$REPO_ID" =~ ^[0-9]+$ ]] || { echo "invalid repo id: $REPO_ID" >&2; exit 2; }

say "Project $PROJECT_ID"
if ! exists gcloud projects describe "$PROJECT_ID"; then
  if [ "$CREATE_PROJECT" = 1 ]; then
    run gcloud projects create "$PROJECT_ID" --name="Offline toolkit" || {
      echo "Could not create project $PROJECT_ID. Project IDs are global across all of Google Cloud, so the" >&2
      echo "default is probably taken: try e.g. bash $0 offline-toolkit-$(date +%y%m)-$RANDOM --create-project ..." >&2
      exit 1; }
  elif [ "$DRY" = 0 ]; then
    echo "project $PROJECT_ID not found or not yours (add --create-project to create it; project IDs are" >&2
    echo "global, so pick a unique one such as offline-toolkit-<suffix>)" >&2; exit 1
  fi
fi

say "Billing"
# Storage buckets and Cloud Build need billing, so check it before enabling APIs. `billing projects link`
# MOVES a project that is already linked elsewhere, so only link a project that has no billing account.
if [ "$DRY" = 1 ]; then
  if [ -n "$BILLING" ]; then run gcloud billing projects link "$PROJECT_ID" --billing-account="$BILLING"; fi
else
  BINFO=$(gcloud billing projects describe "$PROJECT_ID" --format='value(billingEnabled,billingAccountName)') ||
    { echo "could not read the billing info of $PROJECT_ID (you need billing.resourceAssociations.list on it)" >&2; exit 1; }
  read -r B_ENABLED B_ACCOUNT <<<"$BINFO" || true
  B_ACCOUNT="${B_ACCOUNT#billingAccounts/}"
  if [ -z "$B_ACCOUNT" ]; then
    if [ -z "$BILLING" ]; then
      echo "project $PROJECT_ID has no billing account. Re-run with --billing-account XXXXXX-XXXXXX-XXXXXX" >&2
      echo "(list yours with: gcloud billing accounts list)" >&2; exit 1
    fi
    run gcloud billing projects link "$PROJECT_ID" --billing-account="$BILLING" >/dev/null
    echo "  linked to billing account $BILLING"
  elif [ -n "$BILLING" ] && [ "$B_ACCOUNT" != "$BILLING" ]; then
    echo "project $PROJECT_ID is already linked to billing account $B_ACCOUNT, not $BILLING." >&2
    echo "This script does not move projects between billing accounts. Re-run without --billing-account to" >&2
    echo "keep $B_ACCOUNT, or move it yourself first: gcloud billing projects link $PROJECT_ID --billing-account=$BILLING" >&2
    exit 1
  elif [ "${B_ENABLED,,}" != "true" ]; then
    echo "project $PROJECT_ID is linked to billing account $B_ACCOUNT, but billing is not enabled" >&2
    echo "(the account is probably closed or suspended). Fix it in the console: Billing -> Account management." >&2
    exit 1
  else
    echo "  billing account $B_ACCOUNT (enabled)"
  fi
fi
PROJECT_NUM=$( [ "$DRY" = 1 ] && echo 123456789012 || gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')

say "APIs"
# A billing link can take a minute to reach Service Usage, so retry a fresh link a few times.
for try in 1 2 3 4; do
  run gcloud services enable storage.googleapis.com iam.googleapis.com iamcredentials.googleapis.com \
    sts.googleapis.com cloudbuild.googleapis.com cloudresourcemanager.googleapis.com logging.googleapis.com \
    secretmanager.googleapis.com --project="$PROJECT_ID" && break
  [ "$try" -lt 4 ] || { echo "could not enable the APIs; check billing on $PROJECT_ID and re-run" >&2; exit 1; }
  echo "  enabling APIs failed (billing may still be propagating); retrying in 30 s" >&2; sleep 30
done

say "Bucket gs://$BUCKET (private)"
if exists gcloud storage buckets describe "gs://$BUCKET" --project="$PROJECT_ID"; then
  run gcloud storage buckets update "gs://$BUCKET" --uniform-bucket-level-access --public-access-prevention --soft-delete-duration=7d
else
  run gcloud storage buckets create "gs://$BUCKET" --project="$PROJECT_ID" --location="$REGION" \
    --uniform-bucket-level-access --public-access-prevention --soft-delete-duration=7d
fi
for m in allUsers allAuthenticatedUsers; do
  if [ "$DRY" = 0 ] && gcloud storage buckets get-iam-policy "gs://$BUCKET" --format=json | grep -q "\"$m\""; then
    for r in $(gcloud storage buckets get-iam-policy "gs://$BUCKET" --format=json |
               python3 -c "import json,sys;[print(b['role']) for b in json.load(sys.stdin).get('bindings',[]) if '$m' in b.get('members',[])]"); do
      run gcloud storage buckets remove-iam-policy-binding "gs://$BUCKET" --member="$m" --role="$r" --all
    done
  fi
done

say "Service accounts"
PUB="kit-publisher@$PROJECT_ID.iam.gserviceaccount.com"
BLD="kit-builder@$PROJECT_ID.iam.gserviceaccount.com"
exists gcloud iam service-accounts describe "$PUB" --project="$PROJECT_ID" ||
  run gcloud iam service-accounts create kit-publisher --project="$PROJECT_ID" --display-name="GitHub publish-kit workflow"
exists gcloud iam service-accounts describe "$BLD" --project="$PROJECT_ID" ||
  run gcloud iam service-accounts create kit-builder --project="$PROJECT_ID" --display-name="Cloud Build kit builder"

say "Workload Identity Federation (GitHub OIDC)"
COND="assertion.repository_id=='$REPO_ID' && assertion.repository=='$REPO' && (assertion.ref=='refs/heads/main' || assertion.ref.startsWith('refs/tags/v')) && assertion.environment=='$ENV_NAME'"
MAP="google.subject=assertion.sub,attribute.repository_id=assertion.repository_id,attribute.repository=assertion.repository,attribute.ref=assertion.ref,attribute.environment=assertion.environment"
exists gcloud iam workload-identity-pools describe "$POOL" --project="$PROJECT_ID" --location=global ||
  run gcloud iam workload-identity-pools create "$POOL" --project="$PROJECT_ID" --location=global --display-name="GitHub Actions"
if exists gcloud iam workload-identity-pools providers describe "$PROVIDER" --project="$PROJECT_ID" --location=global --workload-identity-pool="$POOL"; then
  run gcloud iam workload-identity-pools providers update-oidc "$PROVIDER" --project="$PROJECT_ID" --location=global \
    --workload-identity-pool="$POOL" --attribute-mapping="$MAP" --attribute-condition="$COND"
else
  run gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" --project="$PROJECT_ID" --location=global \
    --workload-identity-pool="$POOL" --display-name="cjrada/offline-toolkit" \
    --issuer-uri="https://token.actions.githubusercontent.com" --attribute-mapping="$MAP" --attribute-condition="$COND"
fi
PRINCIPAL="principalSet://iam.googleapis.com/projects/$PROJECT_NUM/locations/global/workloadIdentityPools/$POOL/attribute.repository_id/$REPO_ID"

say "IAM grants (add-iam-policy-binding is idempotent)"
run gcloud iam service-accounts add-iam-policy-binding "$PUB" --project="$PROJECT_ID" \
  --role=roles/iam.workloadIdentityUser --member="$PRINCIPAL" --condition=None >/dev/null
LATEST_COND="title=latest-pointer-only,expression=resource.name.startsWith(\"projects/_/buckets/$BUCKET/objects/kits/\") && resource.name.endsWith(\"/latest.json\")"
for sa in "$PUB" "$BLD"; do
  # --condition=None: once the conditional latest.json binding exists, gcloud otherwise needs to ask
  # which condition to use (from the second grant of the first run, and on every re-run), and with
  # prompts disabled that is an error. tests/test_setup_gcp.py checks every grant names a condition.
  run gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --member="serviceAccount:$sa" --role=roles/storage.objectCreator \
    --condition=None >/dev/null
  run gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --member="serviceAccount:$sa" --role=roles/storage.objectUser \
    --condition="$LATEST_COND" >/dev/null
done
# Builder: read the download cache (cache/...). Writes stay create-only through objectCreator above, so
# a cached file can never be replaced; every cached file is checked against its pinned sha256 before use.
CACHE_COND="title=download-cache-read,expression=resource.name.startsWith(\"projects/_/buckets/$BUCKET/objects/cache/\")"
run gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --member="serviceAccount:$BLD" --role=roles/storage.objectViewer \
  --condition="$CACHE_COND" >/dev/null
# Publisher: start builds and check their status, acting only as kit-builder.
for role in roles/cloudbuild.builds.editor roles/serviceusage.serviceUsageConsumer; do
  run gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$PUB" --role="$role" --condition=None >/dev/null
done
run gcloud iam service-accounts add-iam-policy-binding "$BLD" --project="$PROJECT_ID" \
  --role=roles/iam.serviceAccountUser --member="serviceAccount:$PUB" --condition=None >/dev/null
# Builder: write build logs (cloudbuild.yaml uses CLOUD_LOGGING_ONLY).
run gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$BLD" --role=roles/logging.logWriter --condition=None >/dev/null

say "Secret Manager: R2 credentials for Cloud Build (values are never read or printed here)"
# cloudbuild.yaml maps these to R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / R2_ACCOUNT_ID (availableSecrets).
# Only kit-builder may read them, per secret. kit-publisher (GitHub) does not need them: GitHub has its own copy.
R2_SECRET_IDS="r2-access-key-id r2-secret-access-key r2-account-id"
MISSING_VERSIONS=""
for sec in $R2_SECRET_IDS; do
  exists gcloud secrets describe "$sec" --project="$PROJECT_ID" ||
    run gcloud secrets create "$sec" --project="$PROJECT_ID" --replication-policy=automatic >/dev/null
  run gcloud secrets add-iam-policy-binding "$sec" --project="$PROJECT_ID" --member="serviceAccount:$BLD" \
    --role=roles/secretmanager.secretAccessor --condition=None >/dev/null
  # Lists version names only (never payloads), to say which secrets still need a value.
  if [ "$DRY" = 1 ] || [ -z "$(gcloud secrets versions list "$sec" --project="$PROJECT_ID" --filter=state:ENABLED --limit=1 --format='value(name)' 2>/dev/null)" ]; then
    MISSING_VERSIONS="$MISSING_VERSIONS $sec"
  fi
done

WIF="projects/$PROJECT_NUM/locations/global/workloadIdentityPools/$POOL/providers/$PROVIDER"
cat <<EOF

== Done. Now configure GitHub (steps 1-2: the gcs-publish values below are not sensitive)

Cloud Shell may not have the GitHub CLI (gh). Everything below can also be done in the web UI:
https://github.com/$REPO/settings/environments -> gcs-publish (create it, then "Add environment variable").

1. Create the protected environment (repo admin). Either use Settings -> Environments -> New environment
   "$ENV_NAME": Required reviewers = Chris (cjrada); Deployment branches and tags = Selected:
   branch "main" and tag "v*". Or use gh:

   CHRIS_ID=\$(gh api users/cjrada --jq .id)
   gh api -X PUT repos/$REPO/environments/$ENV_NAME --input - <<JSON
   {"reviewers":[{"type":"User","id":\$CHRIS_ID}],"deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}
   JSON
   gh api -X POST repos/$REPO/environments/$ENV_NAME/deployment-branch-policies -f name=main -f type=branch
   gh api -X POST repos/$REPO/environments/$ENV_NAME/deployment-branch-policies -f name='v*' -f type=tag

2. Set the environment variables:

   gh variable set GCP_WIF_PROVIDER    --env $ENV_NAME --body "$WIF" -R $REPO
   gh variable set GCP_SERVICE_ACCOUNT --env $ENV_NAME --body "$PUB" -R $REPO
   gh variable set GCS_BUCKET          --env $ENV_NAME --body "$BUCKET" -R $REPO

3. Kits go to Cloudflare R2. Set the repository variables (Settings -> Secrets and variables -> Actions ->
   Variables) R2_BUCKET = the R2 bucket name and R2_PUBLIC_URL = its public base URL (r2.dev or custom
   domain, no trailing slash). Put the secrets R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_ACCOUNT_ID in the
   gcs-publish environment (Settings -> Environments -> gcs-publish -> Environment secrets), not at repository
   level, and give the Cloudflare token Object Read & Write on the kit bucket only.

4. Give Cloud Build (medium/large) the same R2 credentials in Secret Manager. Paste each value on stdin,
   so it is not in your shell history or on the command line, then press Enter and Ctrl-D:

   gcloud secrets versions add r2-access-key-id     --project=$PROJECT_ID --data-file=-
   gcloud secrets versions add r2-secret-access-key --project=$PROJECT_ID --data-file=-
   gcloud secrets versions add r2-account-id        --project=$PROJECT_ID --data-file=-

   (A trailing newline is fine: the publisher strips whitespace.) To rotate a token later, add a new
   version the same way and disable the old one: gcloud secrets versions disable <n> --secret=<id>
EOF
if [ -n "$MISSING_VERSIONS" ]; then
  echo
  echo "   Still without a value:$MISSING_VERSIONS  (medium/large builds fail before their first step until set)"
fi
cat <<EOF

5. Run Actions -> publish-kit -> Run workflow (dry_run first), or push a tag: git tag v1.0.0 && git push origin v1.0.0
   Public downloads: https://cjrada.github.io/offline-toolkit/ (GitHub Pages, built from the R2 manifests).
   Old kits under gs://$BUCKET/kits/ are no longer written or deleted by CI; see docs/SETUP.md to retire them.
EOF
