#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
release_dir=${1:?Usage: scripts/sign-release.sh RELEASE_DIRECTORY PRIVATE_KEY}
private_key=${2:?Usage: scripts/sign-release.sh RELEASE_DIRECTORY PRIVATE_KEY}
release_dir=$(CDPATH= cd -- "$release_dir" && pwd)
private_key=$(CDPATH= cd -- "$(dirname -- "$private_key")" && printf '%s/%s\n' "$PWD" "$(basename -- "$private_key")")
temporary=$(mktemp -d "$release_dir/.signing.XXXXXX")
cleanup() {
  if [ -d "$temporary" ]; then
    rm -R -- "$temporary"
  fi
}
trap cleanup EXIT

python3 "$script_dir/release.py" prepare-signing "$release_dir" "$temporary/release.json"
openssl pkey -in "$private_key" -pubout -out "$temporary/release-public-key.pem"
openssl dgst -sha256 -sign "$private_key" -out "$temporary/release.json.sig" "$temporary/release.json"
openssl dgst -sha256 -verify "$temporary/release-public-key.pem" -signature "$temporary/release.json.sig" "$temporary/release.json" >/dev/null
python3 "$script_dir/release.py" render-bootstrap "$release_dir" "$temporary/release.json" "$temporary/release-public-key.pem" "$temporary/release.json.sig" "$script_dir/../bootstrap/install.sh" "$temporary/install"
mv "$temporary/release.json" "$release_dir/release.json"
mv "$temporary/release-public-key.pem" "$release_dir/release-public-key.pem"
mv "$temporary/release.json.sig" "$release_dir/release.json.sig"
mv "$temporary/install" "$release_dir/install"

echo "Signed release in $release_dir"
