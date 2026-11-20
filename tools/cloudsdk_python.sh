#!/bin/sh
# Run a stdlib-only repo script (e.g. tools/publish_gcs.py) with a Python inside the cloud-sdk image.
# The pinned cloud-sdk image has no python3 on PATH (the medium build of 18 Nov 2026 built its kit and then
# failed with: exec "python3": executable file not found), but gcloud ships its own interpreter and
# `gcloud info` says where it is. Tried in order: python3 on PATH, $CLOUDSDK_PYTHON, gcloud's python.
#   sh tools/cloudsdk_python.sh tools/publish_gcs.py --zip ... --bucket ...
set -eu
py=""
if command -v python3 >/dev/null 2>&1; then
  py=$(command -v python3)
elif [ -n "${CLOUDSDK_PYTHON:-}" ] && [ -x "$CLOUDSDK_PYTHON" ]; then
  py=$CLOUDSDK_PYTHON
else
  py=$(gcloud info --format='value(basic.python_location)' 2>/dev/null || true)
fi
if [ -z "$py" ] || [ ! -x "$py" ]; then
  echo "cloudsdk_python.sh: no Python found (no python3 on PATH, \$CLOUDSDK_PYTHON unset, gcloud info gave '${py}')" >&2
  exit 127
fi
# -I: ignore PYTHON* variables and user site-packages; the scripts need only the standard library.
exec "$py" -I "$@"
