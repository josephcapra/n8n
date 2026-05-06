#!/usr/bin/env bash
# deploy-brevard-pages.sh
# Deploys Brevard County area pages to production (paradiserealtyfla.com via Real Geeks / FTP / gcloud).
# Review each step before running. DO NOT run this script automatically.
#
# Usage: bash deploy-brevard-pages.sh [--dry-run]
#
# Pre-flight checklist:
#   1. Confirm all 22 Brevard pages render correctly locally
#   2. Confirm brevard-county-beta.html community grid shows 21+ cards
#   3. Run sitemap.xml validation
#   4. Get sign-off from Joe before pushing to production

set -euo pipefail
DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
BREVARD_DIR="$REPO_ROOT/brevard-county-beta"

PAGES=(
  "brevard-county-beta.html"
  "brevard-county-beta/heritage-isle-viera-55-plus.html"
  "brevard-county-beta/viera-master-planned-brevard.html"
  "brevard-county-beta/sawgrass-lakes-palm-bay.html"
  "brevard-county-beta/farallon-fields-cocoa.html"
  "brevard-county-beta/riverwalk-of-cocoa.html"
  "brevard-county-beta/watermark-cocoa.html"
  "brevard-county-beta/crossmolina-melbourne.html"
  "brevard-county-beta/meridian-at-mayfair-melbourne.html"
  "brevard-county-beta/fawn-lake-mims.html"
  "brevard-county-beta/indian-river-preserve-mims.html"
  "brevard-county-beta/aspire-at-palm-bay.html"
  "brevard-county-beta/country-club-estates-palm-bay.html"
  "brevard-county-beta/everlands-palm-vista-medley-palm-bay.html"
  "brevard-county-beta/everlands-riverwood-palm-bay.html"
  "brevard-county-beta/everlands-the-timbers-palm-bay.html"
  "brevard-county-beta/gardens-at-waterstone-palm-bay.html"
  "brevard-county-beta/naples-village-at-verona-palm-bay.html"
  "brevard-county-beta/st-johns-preserve-palm-bay.html"
  "brevard-county-beta/tillman-lakes-palm-bay.html"
  "brevard-county-beta/catamaran-cove-rockledge.html"
  "brevard-county-beta/brooks-landing-titusville.html"
  "brevard-county-beta/brookshire-titusville.html"
  "llms.txt"
)

echo "=== Brevard Pages Deploy Script ==="
echo "Mode: $([ "$DRY_RUN" = true ] && echo 'DRY RUN' || echo 'LIVE')"
echo "Pages to deploy: ${#PAGES[@]}"
echo ""

# Pre-flight: verify all files exist
echo "--- Pre-flight: verifying files ---"
ALL_OK=true
for PAGE in "${PAGES[@]}"; do
  if [[ -f "$REPO_ROOT/$PAGE" ]]; then
    echo "  ✓ $PAGE"
  else
    echo "  ✗ MISSING: $PAGE"
    ALL_OK=false
  fi
done

if [[ "$ALL_OK" != "true" ]]; then
  echo ""
  echo "ERROR: Missing files detected. Aborting."
  exit 1
fi

echo ""
echo "--- All files verified ---"
echo ""

if [[ "$DRY_RUN" = true ]]; then
  echo "DRY RUN complete. No files deployed."
  echo "Remove --dry-run flag and configure deployment target below to deploy."
  exit 0
fi

# ── DEPLOYMENT TARGET ─────────────────────────────────────────────────────────
# Configure one of the following deployment methods:
#
# Option A: rsync to VPS / server
# REMOTE="user@yourserver.com:/var/www/paradiserealtyfla.com/public_html/"
# for PAGE in "${PAGES[@]}"; do
#   rsync -av "$REPO_ROOT/$PAGE" "$REMOTE/$PAGE"
# done
#
# Option B: gcloud storage sync (if hosting on GCS)
# gsutil -m rsync -r "$REPO_ROOT/brevard-county-beta" gs://your-bucket/brevard-county-beta/
# gsutil cp "$REPO_ROOT/brevard-county-beta.html" gs://your-bucket/
# gsutil cp "$REPO_ROOT/llms.txt" gs://your-bucket/
#
# Option C: FTP via lftp
# lftp -e "mirror -R $REPO_ROOT/brevard-county-beta /public_html/brevard-county-beta; quit" sftp://user:pass@yourhost
#
# ── END DEPLOYMENT TARGET ─────────────────────────────────────────────────────

echo "Deployment target not configured."
echo "Edit this script and uncomment the appropriate deployment method above."
exit 1
