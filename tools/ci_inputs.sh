#!/bin/sh
# Validate the publish-kit workflow inputs and write tier/version/dry_run to $GITHUB_OUTPUT.
# Used by both jobs in .github/workflows/publish.yml so the rules live in one place.
# Inputs (env): IN_TIER, IN_VERSION, IN_DRY_RUN, EVENT, REF_NAME.
set -eu
tier="${IN_TIER:-small}"
version="${IN_VERSION:-}"
if [ -z "$version" ]; then
  if [ "${EVENT:-}" = push ]; then version="${REF_NAME:-}"; else version="$(date -u +%Y%m%d)-$(git rev-parse --short=8 HEAD)"; fi
fi
dry_run=false
if [ "${EVENT:-}" = workflow_dispatch ] && [ "${IN_DRY_RUN:-}" = true ]; then dry_run=true; fi
echo "$tier" | grep -Eqx '^(small|medium|large)$' || { echo "::error::invalid tier"; exit 1; }
{ echo "$version" | grep -Eqx '^[0-9A-Za-z._-]+$' && [ ${#version} -le 64 ]; } || { echo "::error::invalid version"; exit 1; }
case "$version" in .*) echo "::error::invalid version"; exit 1 ;; esac
python3 tools/build_kit.py --tier "$tier" --frozen --check
{ echo "tier=$tier"; echo "version=$version"; echo "dry_run=$dry_run"; } >> "${GITHUB_OUTPUT:-/dev/stdout}"
echo "tier=$tier version=$version dry_run=$dry_run"
