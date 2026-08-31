#!/bin/sh
set -eu

version=${1:?Usage: ./build-release.sh VERSION HTTPS_RELEASE_BASE}
base_url=${2:?Usage: ./build-release.sh VERSION HTTPS_RELEASE_BASE}

if ! git diff --quiet --ignore-submodules -- ||
   ! git diff --cached --quiet --ignore-submodules -- ||
   [ -n "$(git ls-files --others --exclude-standard)" ]; then
  echo "Refusing to build from an uncommitted source tree." >&2
  exit 1
fi
source_revision=$(git rev-parse --verify HEAD^{commit})

python3 scripts/release.py source-check .
python3 tests/check_release.py
python3 scripts/check-upstream-installer.py
nix flake check
nix build .#diskImage
nix develop --command python3 scripts/release.py build result "$version" "$base_url" "dist/$version" --attribution ATTRIBUTION.md --source-revision "$source_revision"

echo "Source-buildable release created in dist/$version"
