#!/bin/sh
set -eu

version=${1:?Usage: ./build-release.sh VERSION HTTPS_RELEASE_BASE}
base_url=${2:?Usage: ./build-release.sh VERSION HTTPS_RELEASE_BASE}

python3 scripts/release.py source-check .
python3 tests/check_release.py
nix flake check
nix build .#diskImage
nix develop --command python3 scripts/release.py build result "$version" "$base_url" "dist/$version" --attribution ATTRIBUTION.md

echo "Source-buildable release created in dist/$version"
