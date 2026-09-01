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
nix flake check --no-write-lock-file
nix build --no-write-lock-file .#diskImage
nix develop --no-write-lock-file --command python3 scripts/release.py build result "$version" "$base_url" "dist/$version" --attribution ATTRIBUTION.md --source-revision "$source_revision"
python3 scripts/release.py render-candidate-bootstrap "dist/$version" bootstrap/install-candidate.sh "dist/$version/install-candidate"

echo "Source-buildable release created in dist/$version"
sha256sum "dist/$version/install-candidate"
