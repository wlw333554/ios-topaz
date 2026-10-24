#!/bin/bash
# Split the three iOS DMGs into <2 GiB parts and upload them as GitHub Release
# assets, so the "ios16-root image" workflow can reassemble and use them.
# GitHub caps release / artifact files at 2 GiB, and OS.dmg (4.7G) /
# SystemOS.dmg (2.7G) both exceed that, so splitting is mandatory for the
# in-repo (Release) path. AppOS.dmg (10M) is a single file.
#
# Usage:  tools/split_dmgs.sh <dir-with-dmgs> [release-tag]
#
# Run this on the machine that has the DMGs (Git Bash / WSL / Linux).
# Make sure `gh` is authenticated (gh auth login) and pointed at the repo.
set -euo pipefail

SRC="${1:-.}"
TAG="${2:-dmgs}"
REPO="${GH_REPO:-wlw333554/ios-topaz}"

for f in OS.dmg SystemOS.dmg AppOS.dmg; do
  p="$SRC/$f"
  [ -f "$p" ] || { echo "ERROR: $p not found"; exit 1; }
  echo "splitting $f ($(du -h "$p" | cut -f1))"
  split -b 1900M -d "$p" "$p.part"
done

echo
echo "Upload all *.part* files to the GitHub Release tag '$TAG':"
echo "  gh release create $TAG --repo $REPO --title 'iOS DMGs' --notes 'split DMG parts' || true"
echo "  gh release upload $TAG --repo $REPO \\"
echo "    $SRC/OS.dmg.part* $SRC/SystemOS.dmg.part* $SRC/AppOS.dmg.part*"
echo
echo "Then in GitHub -> Actions -> 'ios16-root image' -> Run workflow,"
echo "set dmgs_tag=$TAG (or set dmgs_base_url if you host them elsewhere)."
