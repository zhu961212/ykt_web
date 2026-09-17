#!/usr/bin/env bash

set -Eeuo pipefail

PROGRAM_NAME="ykt-web update"

die() {
    printf '[%s] ERROR: %s\n' "$PROGRAM_NAME" "$*" >&2
    exit 1
}

SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd -P)"

for argument in "$@"; do
    if [[ "$argument" == "--help" || "$argument" == "-h" ]]; then
        cat <<'EOF'
Check for or install a safe fast-forward update.

Usage:
  sudo /opt/ykt-web/admin/update.sh [--check] [deploy options]

  --check  Fetch upstream and report whether the project is up to date,
           updateable by fast-forward, locally ahead, or diverged.

Without --check, the same comparison runs first. An available update is built,
tested, switched into service, and health-checked; otherwise no release switch
or service restart occurs.
EOF
        exit 0
    fi
done

install_dir="${YKT_INSTALL_DIR:-}"
if [[ -z "$install_dir" ]]; then
    case "$SCRIPT_DIR" in
        */admin) install_dir="$(cd -- "$SCRIPT_DIR/.." && pwd -P)" ;;
        */app/scripts) install_dir="$(cd -- "$SCRIPT_DIR/../.." && pwd -P)" ;;
        *) install_dir="/opt/ykt-web" ;;
    esac
fi

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
    command -v sudo >/dev/null 2>&1 || die "run this script as root (sudo is not installed)"
    exec sudo -- bash "$SCRIPT_PATH" --install-dir "$install_dir" "$@"
fi

managed_deployer="$install_dir/admin/deploy.sh"
if [[ -f "$managed_deployer" && ! -L "$managed_deployer" ]]; then
    exec bash "$managed_deployer" --install-dir "$install_dir" --update "$@"
fi

source_deployer="$SCRIPT_DIR/deploy.sh"
[[ -f "$source_deployer" && ! -L "$source_deployer" ]] || \
    die "deploy.sh was not found; rerun the initial deployment"
exec bash "$source_deployer" --install-dir "$install_dir" --update "$@"
