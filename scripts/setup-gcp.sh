#!/usr/bin/env bash
# One-time (and safe to re-run) Google Cloud setup for publishing kits from GitHub Actions. No keys.
# Run in Cloud Shell as a project owner:
#
#   bash scripts/setup-gcp.sh [PROJECT_ID] [--create-project --billing-account XXXXXX-XXXXXX-XXXXXX]
#                             [--region australia-southeast1] [--bucket NAME] [--repo-id N] [--dry-run]
#
# Creates or updates:
#   * the APIs: storage, iam, iamcredentials, sts, cloudbuild, cloudresourcemanager, logging
#   * a PRIVATE bucket (default <project>-kits): uniform access, public access prevention ENFORCED,
#     7-day soft delete. Any allUsers / allAuthenticatedUsers binding found is removed.
#   * service accounts: kit-publisher (the workflow impersonates it) and kit-builder (Cloud Build runs as it)
#   * a Workload Identity pool "github" + OIDC provider "offline-toolkit". Tokens are accepted only for
#     repository id N AND name cjrada/offline-toolkit, ref main or a v* tag, environment gcs-publish.
#   * least-privilege grants: both SAs may CREATE objects in the bucket but not replace or delete them,
#     except kits/<tier>/latest.json. kit-publisher may start Cloud Builds that run as kit-builder.
set -euo pipefail

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
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    -*) echo "unknown option $1" >&2; exit 2 ;;
    *) PROJECT_ID="$1" ;;
  esac
  shift
done
BUCKET="${BUCKET:-$PROJECT_ID-kits}"
[[ "$PROJECT_ID" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] || { echo "invalid project id: $PROJECT_ID" >&2; exit 2; }
[[ "$BUCKET" =~ ^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$ ]] || { echo "invalid bucket name: $BUCKET" >&2; exit 2; }

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
if [ -n "$BILLING" ]; then run gcloud billing projects link "$PROJECT_ID" --billing-account="$BILLING"; fi
PROJECT_NUM=$( [ "$DRY" = 1 ] && echo 123456789012 || gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')

say "APIs"
run gcloud services enable storage.googleapis.com iam.googleapis.com iamcredentials.googleapis.com \
  sts.googleapis.com cloudbuild.googleapis.com cloudresourcemanager.googleapis.com logging.googleapis.com \
  --project="$PROJECT_ID"

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
  run gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --member="serviceAccount:$sa" --role=roles/storage.objectCreator >/dev/null
  run gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --member="serviceAccount:$sa" --role=roles/storage.objectUser \
    --condition="$LATEST_COND" >/dev/null
done
# Publisher: start builds and check their status, acting only as kit-builder.
for role in roles/cloudbuild.builds.editor roles/serviceusage.serviceUsageConsumer; do
  run gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$PUB" --role="$role" --condition=None >/dev/null
done
run gcloud iam service-accounts add-iam-policy-binding "$BLD" --project="$PROJECT_ID" \
  --role=roles/iam.serviceAccountUser --member="serviceAccount:$PUB" --condition=None >/dev/null
# Builder: write build logs (cloudbuild.yaml uses CLOUD_LOGGING_ONLY).
run gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$BLD" --role=roles/logging.logWriter --condition=None >/dev/null

WIF="projects/$PROJECT_NUM/locations/global/workloadIdentityPools/$POOL/providers/$PROVIDER"
cat <<EOF

== Done. Now configure GitHub (no secrets needed - these values are not sensitive)

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

3. Run Actions -> publish-kit -> Run workflow (dry_run first), or push a tag: git tag v1.0.0 && git push origin v1.0.0

Download a kit later (private bucket; you need storage.objects.get on it, e.g. as project owner):
   gcloud storage cat gs://$BUCKET/kits/small/latest.json
   gcloud storage cp "gs://$BUCKET/kits/small/<version>/offline-toolkit-small-<version>.zip*" .
EOF
