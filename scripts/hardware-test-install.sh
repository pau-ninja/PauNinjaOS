#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
release_dir=${1:?Usage: scripts/hardware-test-install.sh RELEASE_DIRECTORY}
release_dir=$(CDPATH= cd -- "$release_dir" && pwd)
shift

if [ ! -e /System/Library/CoreServices/SystemVersion.plist ]; then
  echo "Hardware-test installation must start from macOS or recoveryOS." >&2
  exit 1
fi
for tool in caffeinate cp curl cut grep id mkdir mktemp plutil python3 rm shasum sysctl tar; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "Hardware-test installation is missing $tool." >&2
    exit 1
  fi
done

python3 "$script_dir/release.py" verify "$release_dir"
status=$(/usr/bin/plutil -extract status raw -o - "$release_dir/release.json")
installable=$(/usr/bin/plutil -extract installable raw -o - "$release_dir/release.json")
if [ "$status" != SOURCE_BUILDABLE ] || [ "$installable" != false ]; then
  echo "This helper accepts only an unpromoted source-buildable release." >&2
  exit 1
fi

machine=$(/usr/sbin/sysctl -n hw.model)
package=$(/usr/bin/plutil -extract os_list.0.package raw -o - "$release_dir/installer_data.json")
package_sha=$(/usr/bin/plutil -extract package.sha256 raw -o - "$release_dir/release.json")
printf 'UNTESTED release for %s. Type the package SHA-256 to continue:\n%s\n> ' "$machine" "$package_sha" >/dev/tty
IFS= read -r confirmation </dev/tty
if [ "$confirmation" != "$package_sha" ]; then
  echo "Hardware-test installation cancelled." >&2
  exit 1
fi

installer_version=v0.9.0
installer_sha=1dc51ec2cce25392e1eae2601c9dc1244e04cb51dbc207b51c815ead6ceeab33
work=$(mktemp -d /tmp/pauninjaos-hardware-test.XXXXXX)
cleanup() {
  cd /
  if [ -d "$work" ]; then
    rm -R -- "$work"
  fi
}
trap cleanup EXIT
cd "$work"
archive="installer-$installer_version.tar.gz"
curl --fail --proto '=https' --proto-redir '=https' --no-progress-meter -L -o "$archive" "https://cdn.asahilinux.org/installer/$archive"
if [ "$(shasum -a 256 "$archive" | cut -d ' ' -f 1)" != "$installer_sha" ]; then
  echo "Pinned Apple Silicon installer failed verification." >&2
  exit 1
fi
tar tf "$archive" > installer.members
while IFS= read -r member; do
  case "$member" in
    ..|/*|../*|*/../*|*/..) echo "Pinned installer contains an unsafe path." >&2; exit 1 ;;
  esac
done < installer.members
mkdir runtime
tar xf "$archive" -C runtime
cd runtime
mkdir -p os
cp "$release_dir/installer_data.json" installer_data.json
cp "$release_dir/$package" "os/$package"
expected_metadata=$(/usr/bin/plutil -extract installer_data.sha256 raw -o - "$release_dir/release.json")
if [ "$(shasum -a 256 installer_data.json | cut -d ' ' -f 1)" != "$expected_metadata" ] ||
   [ "$(shasum -a 256 "os/$package" | cut -d ' ' -f 1)" != "$package_sha" ]; then
  echo "Copied hardware-test bundle failed verification." >&2
  exit 1
fi
export DISTRO=PauNinjaOS
export DISTRO_DOCS=https://pau.ninja/os/docs
export REPO_BASE="$PWD"
export INSTALLER_DATA="$PWD/installer_data.json"
export INSTALLER_DATA_ALT="$INSTALLER_DATA"
export REPORT=
export REPORT_TAG=

echo "Starting an explicitly untested PauNinjaOS installation on $machine." >&2
if [ "$(id -u)" -ne 0 ]; then
  caffeinate -dis sudo -E ./install.sh "$@"
else
  caffeinate -dis ./install.sh "$@"
fi
