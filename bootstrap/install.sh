#!/bin/sh
set -eu

INSTALLER_VERSION=v0.9.0
INSTALLER_SHA256=1dc51ec2cce25392e1eae2601c9dc1244e04cb51dbc207b51c815ead6ceeab33
INSTALLER_BASE=https://cdn.asahilinux.org/installer
PAUNINJAOS_BASE=${PAUNINJAOS_BASE:-https://pau.ninja/os/releases/current}
RELEASE_PUBLIC_KEY_SHA256=UNCONFIGURED
RELEASE_VERSION=UNCONFIGURED

if [ ! -e /System/Library/CoreServices/SystemVersion.plist ]; then
  echo "PauNinjaOS installation must start from macOS or recoveryOS." >&2
  exit 1
fi
for tool in caffeinate curl cut grep id mktemp openssl plutil rm shasum stat sysctl tar; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "PauNinjaOS needs the full macOS command-line environment; missing $tool." >&2
    exit 1
  fi
done

export LC_ALL=en_US.UTF-8
export LANG=en_US.UTF-8
export PATH="/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
export DISTRO=PauNinjaOS
export DISTRO_DOCS=https://pau.ninja/os/docs
export INSTALLER_DATA="$PAUNINJAOS_BASE/installer_data.json"
export INSTALLER_DATA_ALT="$INSTALLER_DATA"
export REPORT=
export REPORT_TAG=

fetch() {
  curl --fail --proto '=https' --proto-redir '=https' --no-progress-meter -L "$@"
}

if [ "$RELEASE_PUBLIC_KEY_SHA256" = UNCONFIGURED ] || [ "$RELEASE_VERSION" = UNCONFIGURED ]; then
  echo "PauNinjaOS release signing is not configured; installation remains locked." >&2
  exit 1
fi

work=$(mktemp -d /tmp/pauninjaos-install.XXXXXX)
cleanup() {
  cd /
  if [ -d "$work" ]; then
    rm -R -- "$work"
  fi
}
trap cleanup EXIT
cd "$work"
archive="installer-$INSTALLER_VERSION.tar.gz"
fetch -o "$archive" "$INSTALLER_BASE/$archive"
actual_installer=$(shasum -a 256 "$archive" | cut -d ' ' -f 1)
if [ "$actual_installer" != "$INSTALLER_SHA256" ]; then
  echo "PauNinjaOS boot installer failed verification." >&2
  exit 1
fi
if ! tar tf "$archive" > installer.members; then
  echo "PauNinjaOS boot installer could not be inspected." >&2
  exit 1
fi
unsafe_archive=0
while IFS= read -r member; do
  case "$member" in
    ..|/*|../*|*/../*|*/..) unsafe_archive=1; break ;;
  esac
done < installer.members
if [ "$unsafe_archive" -ne 0 ]; then
  echo "PauNinjaOS boot installer contains an unsafe path." >&2
  exit 1
fi
mkdir runtime
tar xf "$archive" -C runtime
cd runtime
export REPO_BASE="$PWD"

fetch -o release-public-key.pem "$PAUNINJAOS_BASE/release-public-key.pem"
actual_public_key=$(shasum -a 256 release-public-key.pem | cut -d ' ' -f 1)
if [ "$actual_public_key" != "$RELEASE_PUBLIC_KEY_SHA256" ]; then
  echo "PauNinjaOS release key failed verification." >&2
  exit 1
fi
fetch -o release.json "$PAUNINJAOS_BASE/release.json"
fetch -o release.json.sig "$PAUNINJAOS_BASE/release.json.sig"
if ! /usr/bin/openssl dgst -sha256 -verify release-public-key.pem -signature release.json.sig release.json >/dev/null 2>&1; then
  echo "PauNinjaOS release signature is invalid." >&2
  exit 1
fi
status=$(/usr/bin/plutil -extract status raw -o - release.json)
installable=$(/usr/bin/plutil -extract installable raw -o - release.json)
schema=$(/usr/bin/plutil -extract schema raw -o - release.json)
version=$(/usr/bin/plutil -extract version raw -o - release.json)
approved_installer_version=$(/usr/bin/plutil -extract boot_installer.version raw -o - release.json)
approved_installer_sha256=$(/usr/bin/plutil -extract boot_installer.sha256 raw -o - release.json)
if [ "$schema" != PAUNINJAOS_RELEASE_V1 ] || [ "$version" != "$RELEASE_VERSION" ] || [ "$status" != HARDWARE_TESTED ] || [ "$installable" != true ]; then
  echo "This PauNinjaOS release is not approved for installation on real hardware." >&2
  exit 1
fi
if [ "$approved_installer_version" != "$INSTALLER_VERSION" ] || [ "$approved_installer_sha256" != "$INSTALLER_SHA256" ]; then
  echo "This PauNinjaOS release targets a different boot installer." >&2
  exit 1
fi
machine=$(/usr/sbin/sysctl -n hw.model)
if ! /usr/bin/plutil -extract supported_models xml1 -o - release.json | grep -Fq "<string>$machine</string>"; then
  echo "This PauNinjaOS release was not hardware-tested on $machine." >&2
  exit 1
fi

fetch -o installer_data.json "$INSTALLER_DATA"
expected_metadata=$(/usr/bin/plutil -extract installer_data.sha256 raw -o - release.json)
expected_metadata_size=$(/usr/bin/plutil -extract installer_data.size raw -o - release.json)
actual_metadata=$(shasum -a 256 installer_data.json | cut -d ' ' -f 1)
actual_metadata_size=$(stat -f %z installer_data.json)
if [ "$actual_metadata" != "$expected_metadata" ] || [ "$actual_metadata_size" != "$expected_metadata_size" ]; then
  echo "PauNinjaOS installer metadata failed verification." >&2
  exit 1
fi

package_url=$(/usr/bin/plutil -extract package.url raw -o - release.json)
package_sha256=$(/usr/bin/plutil -extract package.sha256 raw -o - release.json)
case "$package_url" in
  https://*) ;;
  *) echo "PauNinjaOS package URL is unsafe." >&2; exit 1 ;;
esac
package_name=${package_url##*/}
case "$package_name" in
  ""|*/*|*..*) echo "PauNinjaOS package name is unsafe." >&2; exit 1 ;;
esac
mkdir -p os
fetch -o "os/$package_name" "$package_url"
actual_package=$(shasum -a 256 "os/$package_name" | cut -d ' ' -f 1)
if [ "$actual_package" != "$package_sha256" ]; then
  echo "PauNinjaOS package failed verification." >&2
  exit 1
fi
expected_package_size=$(/usr/bin/plutil -extract package.size raw -o - release.json)
actual_package_size=$(stat -f %z "os/$package_name")
if [ "$actual_package_size" != "$expected_package_size" ]; then
  echo "PauNinjaOS package size failed verification." >&2
  exit 1
fi
actual_metadata=$(shasum -a 256 installer_data.json | cut -d ' ' -f 1)
actual_package=$(shasum -a 256 "os/$package_name" | cut -d ' ' -f 1)
if [ "$actual_metadata" != "$expected_metadata" ] || [ "$actual_package" != "$package_sha256" ]; then
  echo "PauNinjaOS verified files changed before installation." >&2
  exit 1
fi
export INSTALLER_DATA="$PWD/installer_data.json"
export INSTALLER_DATA_ALT="$INSTALLER_DATA"

if [ "$(id -u)" -ne 0 ]; then
  if ! command -v sudo >/dev/null 2>&1; then
    echo "PauNinjaOS needs sudo when installation starts outside recoveryOS." >&2
    exit 1
  fi
  caffeinate -dis sudo -E ./install.sh "$@"
else
  caffeinate -dis ./install.sh "$@"
fi
