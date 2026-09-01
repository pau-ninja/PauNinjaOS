#!/bin/sh
set -eu

INSTALLER_VERSION=v0.9.0
INSTALLER_SHA256=1dc51ec2cce25392e1eae2601c9dc1244e04cb51dbc207b51c815ead6ceeab33
INSTALLER_SIZE=22211382
INSTALLER_BASE=https://cdn.asahilinux.org/installer
PAUNINJAOS_BASE=UNCONFIGURED
RELEASE_VERSION=UNCONFIGURED
RELEASE_SHA256=UNCONFIGURED
RELEASE_SIZE=UNCONFIGURED
SUPPORTED_TEST_MODELS=UNCONFIGURED

if [ ! -e /System/Library/CoreServices/SystemVersion.plist ]; then
  echo "PauNinjaOS candidate installation requires a full macOS environment." >&2
  exit 1
fi
if ! (exec 3<>/dev/tty) 2>/dev/null; then
  echo "PauNinjaOS candidate installation requires an interactive terminal." >&2
  exit 1
fi
export LC_ALL=en_US.UTF-8
export LANG=en_US.UTF-8
export PATH="/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
for tool in caffeinate cp curl cut id mkdir mktemp mv plutil rm shasum stat sysctl tar; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "PauNinjaOS needs the full macOS command-line environment; missing $tool." >&2
    exit 1
  fi
done
if [ "$PAUNINJAOS_BASE" = UNCONFIGURED ] || [ "$RELEASE_VERSION" = UNCONFIGURED ] ||
   [ "$RELEASE_SHA256" = UNCONFIGURED ] || [ "$RELEASE_SIZE" = UNCONFIGURED ] ||
   [ "$SUPPORTED_TEST_MODELS" = UNCONFIGURED ]; then
  echo "PauNinjaOS candidate metadata is not configured." >&2
  exit 1
fi

export DISTRO=PauNinjaOS
export DISTRO_DOCS=https://pau.ninja/os/docs
export REPORT=
export REPORT_TAG=

fetch() {
  curl --fail --proto '=https' --proto-redir '=https' --no-progress-meter -L "$@"
}

work=$(mktemp -d /tmp/pauninjaos-candidate.XXXXXX)
cleanup() {
  cd /
  if [ -d "$work" ]; then
    rm -R -- "$work"
  fi
}
trap cleanup EXIT
downloads="$work/downloads"
mkdir "$downloads"
cd "$downloads"

case "$RELEASE_SIZE" in
  ""|*[!0-9]*) echo "PauNinjaOS candidate release size is invalid." >&2; exit 1 ;;
esac
fetch --max-filesize "$RELEASE_SIZE" -o release.json "$PAUNINJAOS_BASE/release.json"
if [ "$(stat -f %z release.json)" != "$RELEASE_SIZE" ] ||
   [ "$(shasum -a 256 release.json | cut -d ' ' -f 1)" != "$RELEASE_SHA256" ]; then
  echo "PauNinjaOS candidate release metadata failed verification." >&2
  exit 1
fi

schema=$(/usr/bin/plutil -extract schema raw -o - release.json)
version=$(/usr/bin/plutil -extract version raw -o - release.json)
status=$(/usr/bin/plutil -extract status raw -o - release.json)
installable=$(/usr/bin/plutil -extract installable raw -o - release.json)
approved_installer_version=$(/usr/bin/plutil -extract boot_installer.version raw -o - release.json)
approved_installer_sha256=$(/usr/bin/plutil -extract boot_installer.sha256 raw -o - release.json)
source_revision=$(/usr/bin/plutil -extract source_revision raw -o - release.json)
case "$installable" in
  false|0) ;;
  *) echo "Candidate installer refuses a release marked installable." >&2; exit 1 ;;
esac
if [ "$schema" != PAUNINJAOS_RELEASE_V1 ] || [ "$version" != "$RELEASE_VERSION" ] ||
   [ "$status" != SOURCE_BUILDABLE ]; then
  echo "Candidate installer accepts only the exact unpromoted release." >&2
  exit 1
fi
if [ "$approved_installer_version" != "$INSTALLER_VERSION" ] ||
   [ "$approved_installer_sha256" != "$INSTALLER_SHA256" ]; then
  echo "Candidate release targets a different boot installer." >&2
  exit 1
fi

expected_metadata=$(/usr/bin/plutil -extract installer_data.sha256 raw -o - release.json)
expected_metadata_size=$(/usr/bin/plutil -extract installer_data.size raw -o - release.json)
case "$expected_metadata_size" in
  ""|*[!0-9]*) echo "PauNinjaOS installer metadata size is invalid." >&2; exit 1 ;;
esac
fetch --max-filesize "$expected_metadata_size" -o installer_data.json "$PAUNINJAOS_BASE/installer_data.json"
if [ "$(stat -f %z installer_data.json)" != "$expected_metadata_size" ] ||
   [ "$(shasum -a 256 installer_data.json | cut -d ' ' -f 1)" != "$expected_metadata" ]; then
  echo "PauNinjaOS installer metadata failed verification." >&2
  exit 1
fi

package_url=$(/usr/bin/plutil -extract package.url raw -o - release.json)
package_sha256=$(/usr/bin/plutil -extract package.sha256 raw -o - release.json)
package_size=$(/usr/bin/plutil -extract package.size raw -o - release.json)
package_name=$(/usr/bin/plutil -extract os_list.0.package raw -o - installer_data.json)
case "$package_name" in
  "pauninjaos-$RELEASE_VERSION-apple-silicon.zip") ;;
  *) echo "PauNinjaOS candidate package name is invalid." >&2; exit 1 ;;
esac
if [ "$package_url" != "$PAUNINJAOS_BASE/$package_name" ]; then
  echo "PauNinjaOS candidate package URL is invalid." >&2
  exit 1
fi
case "$package_size" in
  ""|*[!0-9]*) echo "PauNinjaOS candidate package size is invalid." >&2; exit 1 ;;
esac
fetch --max-filesize "$package_size" -o "$package_name" "$package_url"
if [ "$(stat -f %z "$package_name")" != "$package_size" ] ||
   [ "$(shasum -a 256 "$package_name" | cut -d ' ' -f 1)" != "$package_sha256" ]; then
  echo "PauNinjaOS candidate package failed verification." >&2
  exit 1
fi

machine=$(/usr/sbin/sysctl -n hw.model)
case " $SUPPORTED_TEST_MODELS " in
  *" $machine "*) ;;
  *) echo "PauNinjaOS hardware testing is not enabled for $machine." >&2; exit 1 ;;
esac
printf '%s\n' \
  "UNTESTED PauNinjaOS $RELEASE_VERSION candidate for $machine." \
  "This may fail to boot and may require recovery." \
  "Compare this package SHA-256 with the tagged source release:" \
  "$package_sha256" \
  "Source revision: $source_revision" \
  "Type the complete SHA-256 to continue:" > /dev/tty
printf '> ' > /dev/tty
IFS= read -r confirmation < /dev/tty
if [ "$confirmation" != "$package_sha256" ]; then
  echo "Candidate installation cancelled." >&2
  exit 1
fi

archive="installer-$INSTALLER_VERSION.tar.gz"
fetch --max-filesize "$INSTALLER_SIZE" -o "$archive" "$INSTALLER_BASE/$archive"
if [ "$(stat -f %z "$archive")" != "$INSTALLER_SIZE" ] ||
   [ "$(shasum -a 256 "$archive" | cut -d ' ' -f 1)" != "$INSTALLER_SHA256" ]; then
  echo "Pinned Apple Silicon installer failed verification." >&2
  exit 1
fi
if ! tar tf "$archive" > installer.members; then
  echo "Pinned installer could not be inspected." >&2
  exit 1
fi
while IFS= read -r member; do
  case "$member" in
    ..|/*|../*|*/../*|*/..) echo "Pinned installer contains an unsafe path." >&2; exit 1 ;;
  esac
done < installer.members
mkdir "$work/runtime"
tar xf "$archive" -C "$work/runtime"
cd "$work/runtime"
mkdir -p os
cp "$downloads/installer_data.json" installer_data.json
mv "$downloads/$package_name" "os/$package_name"
if [ "$(shasum -a 256 installer_data.json | cut -d ' ' -f 1)" != "$expected_metadata" ] ||
   [ "$(shasum -a 256 "os/$package_name" | cut -d ' ' -f 1)" != "$package_sha256" ]; then
  echo "Verified candidate files changed before installation." >&2
  exit 1
fi

export REPO_BASE="$PWD"
export INSTALLER_DATA="$PWD/installer_data.json"
export INSTALLER_DATA_ALT="$INSTALLER_DATA"
echo "Starting an explicitly untested PauNinjaOS installation on $machine." >&2
if [ "$(id -u)" -ne 0 ]; then
  if ! command -v sudo >/dev/null 2>&1; then
    echo "PauNinjaOS needs sudo when installation starts outside recoveryOS." >&2
    exit 1
  fi
  caffeinate -dis /usr/bin/sudo /usr/bin/env \
    DISTRO="$DISTRO" DISTRO_DOCS="$DISTRO_DOCS" REPO_BASE="$REPO_BASE" \
    INSTALLER_DATA="$INSTALLER_DATA" INSTALLER_DATA_ALT="$INSTALLER_DATA_ALT" \
    REPORT="$REPORT" REPORT_TAG="$REPORT_TAG" ./install.sh "$@"
else
  caffeinate -dis ./install.sh "$@"
fi
