set -eu

action=${1:-stage}

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
  rollback)
    exec nixos-rebuild boot --rollback
    ;;
  status)
    exec nix-env --list-generations -p /nix/var/nix/profiles/system
    ;;
  *)
    echo "Usage: pauninjaos-update [stage|rollback|status]" >&2
    exit 2
    ;;
esac
