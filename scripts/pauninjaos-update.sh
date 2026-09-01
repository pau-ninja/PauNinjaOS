set -eu

action=${1:-stage}

UPDATE_BASE=${PAUNINJAOS_UPDATE_BASE:-https://vps-308188fb.vps.ovh.us/current}
UPDATE_ALLOWED_SIGNERS=${PAUNINJAOS_UPDATE_ALLOWED_SIGNERS:-/etc/pauninjaos/update-allowed-signers}
UPDATE_BASE_SERIAL=${PAUNINJAOS_UPDATE_BASE_SERIAL:-/etc/pauninjaos/update-serial}
UPDATE_STATE=${PAUNINJAOS_UPDATE_STATE:-/var/lib/pauninjaos/update-serial}
UPDATE_HELPER=${PAUNINJAOS_UPDATE_HELPER:-pauninjaos-update-verify}

if [ "$action" != status ] && [ "$(id -u)" -ne 0 ]; then
  exec sudo --preserve-env=PAUNINJAOS_FLAKE "$0" "$@"
fi

case "$action" in
  stage)
    source=${PAUNINJAOS_FLAKE:?Set PAUNINJAOS_FLAKE to the approved release flake URL}
    case "$source" in
      *[!0-9A-Za-z:/_-]*) valid_source=false ;;
      *) valid_source=true ;;
    esac
    if [ "$valid_source" != true ] || ! printf '%s\n' "$source" | grep -Eq '^github:pau-ninja/PauNinjaOS/[0-9a-f]{40}$'; then
      echo "PAUNINJAOS_FLAKE must identify one immutable PauNinjaOS Git revision." >&2
      exit 2
    fi
    exec nixos-rebuild boot --flake "$source#pauninjaos"
    ;;
  auto)
    case "$UPDATE_BASE" in
      https://*) ;;
      *) echo "PauNinjaOS update address must use HTTPS." >&2; exit 2 ;;
    esac
    work=$(mktemp -d /tmp/pauninjaos-update.XXXXXX)
    cleanup() {
      if [ -d "$work" ]; then
        rm -R -- "$work"
      fi
    }
    trap cleanup EXIT
    curl --fail --proto '=https' --proto-redir '=https' --no-progress-meter -L --max-filesize 4096 -o "$work/update.json" "$UPDATE_BASE/update.json"
    curl --fail --proto '=https' --proto-redir '=https' --no-progress-meter -L --max-filesize 16384 -o "$work/update.json.sig" "$UPDATE_BASE/update.json.sig"
    verified=$("$UPDATE_HELPER" verify "$work/update.json" "$work/update.json.sig" "$UPDATE_ALLOWED_SIGNERS" "$UPDATE_BASE_SERIAL" "$UPDATE_STATE")
    source_url=$(printf '%s\n' "$verified" | sed -n '1p')
    source_sha256=$(printf '%s\n' "$verified" | sed -n '2p')
    source_size=$(printf '%s\n' "$verified" | sed -n '3p')
    serial=$(printf '%s\n' "$verified" | sed -n '4p')
    curl --fail --proto '=https' --proto-redir '=https' --no-progress-meter -L --max-filesize "$source_size" -o "$work/source.tar.gz" "$source_url"
    if [ "$(stat -c %s "$work/source.tar.gz")" != "$source_size" ] ||
       [ "$(sha256sum "$work/source.tar.gz" | cut -d ' ' -f 1)" != "$source_sha256" ]; then
      echo "PauNinjaOS update source failed verification." >&2
      exit 1
    fi
    tar tzf "$work/source.tar.gz" > "$work/source.members"
    while IFS= read -r member; do
      case "$member" in
        ..|/*|../*|*/../*|*/..) echo "PauNinjaOS update source contains an unsafe path." >&2; exit 1 ;;
      esac
    done < "$work/source.members"
    mkdir "$work/source"
    tar xzf "$work/source.tar.gz" -C "$work/source"
    if [ ! -f "$work/source/flake.nix" ]; then
      echo "PauNinjaOS update source lacks its system definition." >&2
      exit 1
    fi
    nixos-rebuild boot --flake "path:$work/source#pauninjaos"
    "$UPDATE_HELPER" commit "$serial" "$UPDATE_STATE"
    ;;
  rollback)
    exec nixos-rebuild boot --rollback
    ;;
  status)
    exec nix-env --list-generations -p /nix/var/nix/profiles/system
    ;;
  *)
    echo "Usage: pauninjaos-update [auto|stage|rollback|status]" >&2
    exit 2
    ;;
esac
