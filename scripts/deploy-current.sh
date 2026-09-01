#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
release_dir=${1:?Usage: scripts/deploy-current.sh RELEASE_DIRECTORY UPDATE_MANIFEST UPDATE_SIGNATURE SOURCE_ARCHIVE}
update_manifest=${2:?Usage: scripts/deploy-current.sh RELEASE_DIRECTORY UPDATE_MANIFEST UPDATE_SIGNATURE SOURCE_ARCHIVE}
update_signature=${3:?Usage: scripts/deploy-current.sh RELEASE_DIRECTORY UPDATE_MANIFEST UPDATE_SIGNATURE SOURCE_ARCHIVE}
source_archive=${4:?Usage: scripts/deploy-current.sh RELEASE_DIRECTORY UPDATE_MANIFEST UPDATE_SIGNATURE SOURCE_ARCHIVE}
host=${PAUNINJAOS_DEPLOY_HOST:-ubuntu@51.81.87.170}
key=${PAUNINJAOS_DEPLOY_KEY:-$HOME/.ssh/pau_ovh}
public_base=${PAUNINJAOS_PUBLIC_BASE:-https://vps-308188fb.vps.ovh.us/current}
remote_root=/home/ubuntu/pauninja-os

release_dir=$(CDPATH= cd -- "$release_dir" && pwd)
update_manifest=$(CDPATH= cd -- "$(dirname -- "$update_manifest")" && printf '%s/%s\n' "$PWD" "$(basename -- "$update_manifest")")
update_signature=$(CDPATH= cd -- "$(dirname -- "$update_signature")" && printf '%s/%s\n' "$PWD" "$(basename -- "$update_signature")")
source_archive=$(CDPATH= cd -- "$(dirname -- "$source_archive")" && printf '%s/%s\n' "$PWD" "$(basename -- "$source_archive")")
python3 "$script_dir/release.py" verify "$release_dir"
python3 - "$release_dir/release.json" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
if value.get("status") != "SOURCE_BUILDABLE" or value.get("installable") is not False:
    raise SystemExit("Only a locked source-buildable release can be deployed as the test candidate")
PY

ssh-keygen -Y verify -f "$script_dir/../release/update-allowed-signers" \
  -I pauninjaos-update -n pauninjaos-update -s "$update_signature" \
  < "$update_manifest" >/dev/null
python3 - "$release_dir/release.json" "$update_manifest" "$source_archive" "$script_dir/update.py" "$public_base" <<'PY'
import hashlib, importlib.util, json, pathlib, sys
release = json.load(open(sys.argv[1], encoding="utf-8"))
spec = importlib.util.spec_from_file_location("pauninjaos_update", sys.argv[4])
update = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update)
manifest = update.load_manifest(pathlib.Path(sys.argv[2]))
source = pathlib.Path(sys.argv[3])
public_base = sys.argv[5].rstrip("/")
metadata = json.load(open(pathlib.Path(sys.argv[1]).with_name("installer_data.json"), encoding="utf-8"))
package = metadata["os_list"][0]["package"]
if (
    manifest["version"] != release["version"]
    or manifest["source_revision"] != release["source_revision"]
    or manifest["source_url"] != f"{public_base}/source.tar.gz"
    or release["package"]["url"] != f"{public_base}/{package}"
    or source.stat().st_size != manifest["source_size"]
    or hashlib.sha256(source.read_bytes()).hexdigest() != manifest["source_sha256"]
):
    raise SystemExit("Update source does not match the release")
PY

package=$(python3 - "$release_dir/installer_data.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["os_list"][0]["package"])
PY
)
temporary=$(mktemp -d /tmp/pauninjaos-publish.XXXXXX)
cleanup() {
  if [ -d "$temporary" ]; then
    rm -R -- "$temporary"
  fi
}
trap cleanup EXIT

for name in release.json installer_data.json install-candidate "$package"; do
  cp "$release_dir/$name" "$temporary/$name"
done
cp "$update_manifest" "$temporary/update.json"
cp "$update_signature" "$temporary/update.json.sig"
cp "$source_archive" "$temporary/source.tar.gz"
cp "$script_dir/../release/update-allowed-signers" "$temporary/update-allowed-signers"

ssh -i "$key" -o BatchMode=yes "$host" "mkdir -p '$remote_root/.incoming'"
rsync -a --delete -e "ssh -i $key -o BatchMode=yes" "$temporary/" "$host:$remote_root/.incoming/"
ssh -i "$key" -o BatchMode=yes "$host" "python3 - '$remote_root'" <<'PY'
from pathlib import Path
import ctypes, os, shutil, sys
root = Path(sys.argv[1])
incoming, current = root / ".incoming", root / "current"
note = root / "AI-NOTE.txt"
if root != Path("/home/ubuntu/pauninja-os") or root.is_symlink() or not incoming.is_dir():
    raise SystemExit("Unsafe PauNinjaOS deployment target")
if not note.is_file() or note.is_symlink():
    raise SystemExit("PauNinjaOS AI maintenance note is missing or unsafe")
if current.exists():
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.renameat2(-100, os.fsencode(incoming), -100, os.fsencode(current), 2)
    if result != 0:
        raise OSError(ctypes.get_errno(), "Atomic PauNinjaOS release exchange failed")
else:
    os.replace(incoming, current)
directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(directory)
finally:
    os.close(directory)
if incoming.exists():
    shutil.rmtree(incoming)
directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(directory)
finally:
    os.close(directory)
PY

echo "Current PauNinjaOS release deployed to $host:$remote_root/current"
