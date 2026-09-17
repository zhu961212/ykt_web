#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

PROGRAM_NAME="ykt-web deploy"
DEFAULT_INSTALL_DIR="/opt/ykt-web"
DEFAULT_REPOSITORY_URL="https://github.com/zhu961212/ykt_web.git"
DEFAULT_REF="main"
DEFAULT_SERVICE_USER="ykt-web"
DEFAULT_SERVICE_NAME="ykt-web"
DEFAULT_BIND_HOST="0.0.0.0"
DEFAULT_BIND_PORT="8765"
DEFAULT_ADMIN_USERNAME="admin"
DEFAULT_SECURE_COOKIE="0"
DEFAULT_TRUST_PROXY="0"
DEFAULT_ADMIN_RESET="0"

log() {
    printf '[%s] %s\n' "$PROGRAM_NAME" "$*"
}

warn() {
    printf '[%s] WARNING: %s\n' "$PROGRAM_NAME" "$*" >&2
}

die() {
    printf '[%s] ERROR: %s\n' "$PROGRAM_NAME" "$*" >&2
    exit 1
}

usage() {
    cat <<'EOF'
Deploy ykt_web as a password-protected systemd service.

Usage:
  sudo bash scripts/deploy.sh [options]

Options:
  --install-dir PATH   Installation root (default: /opt/ykt-web)
  --repo URL           Git repository URL
  --ref REF            Branch, tag, or full commit SHA (default: main)
  --user USER          Unprivileged service account (default: ykt-web)
  --service NAME       systemd unit name without .service (default: ykt-web)
  --python COMMAND     Python 3.10+ executable (default: python3)
  --host ADDRESS       Listen address (default: 0.0.0.0)
  --port PORT          Listen port (default: 8765)
  --domain HOSTNAME    Enable automatic HTTPS for this domain with Caddy
  --no-domain          Disable the managed HTTPS proxy
  --admin-user USER    Web administrator username (default: admin)
  --check              Fetch and report update status without deploying
  --skip-tests         Run import/compile checks, but skip the unit test suite
  --force              Redeploy even when the requested commit is installed
  -h, --help           Show this help

The script keeps config.json, session.json, and logs/ during upgrades. A new
release is built and tested before the running service is stopped. If startup
or the HTTP health check fails, the previous application directory is restored.

After deployment, run updates with:
  sudo /opt/ykt-web/admin/update.sh
EOF
}

require_option_value() {
    local option="$1"
    local value="${2-}"
    [[ -n "$value" ]] || die "$option requires a value"
}

validate_domain() {
    local label final_label
    local -a labels=()
    DOMAIN="${DOMAIN%.}"
    DOMAIN="${DOMAIN,,}"
    [[ -n "$DOMAIN" && ${#DOMAIN} -le 253 ]] || die "--domain must be 1-253 characters"
    [[ "$DOMAIN" == *.* && "$DOMAIN" != *..* ]] || \
        die "--domain must be a fully qualified hostname such as panel.example.com"
    [[ "$DOMAIN" != *[[:space:]]* ]] || die "--domain must not contain whitespace"
    [[ "$DOMAIN" =~ ^[a-z0-9.-]+$ ]] || \
        die "--domain accepts an ASCII hostname only; do not include a scheme, port, or path"
    [[ ! "$DOMAIN" =~ ^[0-9.]+$ ]] || die "--domain must not be an IP address"
    IFS='.' read -r -a labels <<< "$DOMAIN"
    for label in "${labels[@]}"; do
        [[ ${#label} -le 63 && "$label" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]] || \
            die "--domain contains an invalid DNS label: $label"
    done
    final_label="${labels[${#labels[@]}-1]}"
    [[ "$final_label" =~ [a-z] ]] || die "--domain must end in a valid public DNS suffix"
}

SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd -P)"
SOURCE_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"

for argument in "$@"; do
    if [[ "$argument" == "-h" || "$argument" == "--help" ]]; then
        usage
        exit 0
    fi
done

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
    command -v sudo >/dev/null 2>&1 || die "run this script as root (sudo is not installed)"
    exec sudo -- bash "$SCRIPT_PATH" "$@"
fi

INSTALL_DIR="${YKT_INSTALL_DIR:-$DEFAULT_INSTALL_DIR}"
REPOSITORY_URL="${YKT_REPOSITORY_URL:-}"
DEPLOY_REF="${YKT_DEPLOY_REF:-}"
SERVICE_USER="${YKT_SERVICE_USER:-}"
BUILD_USER=""
SERVICE_NAME="${YKT_SERVICE_NAME:-}"
PYTHON_COMMAND="${YKT_PYTHON:-}"
BIND_HOST="${YKT_HOST:-}"
BIND_PORT="${YKT_PORT:-}"
DOMAIN="${YKT_DOMAIN:-}"
ADMIN_USERNAME="${YKT_ADMIN_USERNAME:-}"
[[ -z "${YKT_ADMIN_PASSWORD:-}" ]] || \
    die "do not pass YKT_ADMIN_PASSWORD to deploy.sh; it generates a password and stores it in service.env"
ADMIN_PASSWORD=""
SECURE_COOKIE="${YKT_SECURE_COOKIE:-}"
TRUST_PROXY="${YKT_TRUST_PROXY:-}"
ADMIN_RESET="${YKT_ADMIN_RESET:-}"
RUN_TESTS=1
FORCE_DEPLOY=0
CHECK_ONLY=0
UPDATE_MODE=0
DISABLE_DOMAIN=0
DOMAIN_ENABLE_SEEN=0
DOMAIN_DISABLE_SEEN=0

repo_explicit=0
ref_explicit=0
user_explicit=0
service_explicit=0
python_explicit=0
host_explicit=0
port_explicit=0
domain_explicit=0
admin_user_explicit=0
secure_cookie_explicit=0
trust_proxy_explicit=0
admin_reset_explicit=0
[[ -n "$REPOSITORY_URL" ]] && repo_explicit=1
[[ -n "$DEPLOY_REF" ]] && ref_explicit=1
[[ -n "$SERVICE_USER" ]] && user_explicit=1
[[ -n "$SERVICE_NAME" ]] && service_explicit=1
[[ -n "$PYTHON_COMMAND" ]] && python_explicit=1
[[ -n "$BIND_HOST" ]] && host_explicit=1
[[ -n "$BIND_PORT" ]] && port_explicit=1
[[ -n "$DOMAIN" ]] && domain_explicit=1
[[ -n "$ADMIN_USERNAME" ]] && admin_user_explicit=1
[[ -n "$SECURE_COOKIE" ]] && secure_cookie_explicit=1
[[ -n "$TRUST_PROXY" ]] && trust_proxy_explicit=1
[[ -n "$ADMIN_RESET" ]] && admin_reset_explicit=1

args=("$@")
index=0
while (( index < ${#args[@]} )); do
    argument="${args[$index]}"
    case "$argument" in
        --install-dir)
            ((index += 1))
            require_option_value "$argument" "${args[$index]-}"
            INSTALL_DIR="${args[$index]}"
            ;;
        --install-dir=*) INSTALL_DIR="${argument#*=}" ;;
        --repo)
            ((index += 1))
            require_option_value "$argument" "${args[$index]-}"
            REPOSITORY_URL="${args[$index]}"
            repo_explicit=1
            ;;
        --repo=*) REPOSITORY_URL="${argument#*=}"; repo_explicit=1 ;;
        --ref)
            ((index += 1))
            require_option_value "$argument" "${args[$index]-}"
            DEPLOY_REF="${args[$index]}"
            ref_explicit=1
            ;;
        --ref=*) DEPLOY_REF="${argument#*=}"; ref_explicit=1 ;;
        --user)
            ((index += 1))
            require_option_value "$argument" "${args[$index]-}"
            SERVICE_USER="${args[$index]}"
            user_explicit=1
            ;;
        --user=*) SERVICE_USER="${argument#*=}"; user_explicit=1 ;;
        --service)
            ((index += 1))
            require_option_value "$argument" "${args[$index]-}"
            SERVICE_NAME="${args[$index]}"
            service_explicit=1
            ;;
        --service=*) SERVICE_NAME="${argument#*=}"; service_explicit=1 ;;
        --python)
            ((index += 1))
            require_option_value "$argument" "${args[$index]-}"
            PYTHON_COMMAND="${args[$index]}"
            python_explicit=1
            ;;
        --python=*) PYTHON_COMMAND="${argument#*=}"; python_explicit=1 ;;
        --host)
            ((index += 1))
            require_option_value "$argument" "${args[$index]-}"
            BIND_HOST="${args[$index]}"
            host_explicit=1
            ;;
        --host=*) BIND_HOST="${argument#*=}"; host_explicit=1 ;;
        --port)
            ((index += 1))
            require_option_value "$argument" "${args[$index]-}"
            BIND_PORT="${args[$index]}"
            port_explicit=1
            ;;
        --port=*) BIND_PORT="${argument#*=}"; port_explicit=1 ;;
        --domain)
            ((index += 1))
            require_option_value "$argument" "${args[$index]-}"
            DOMAIN="${args[$index]}"
            DOMAIN_ENABLE_SEEN=1
            domain_explicit=1
            ;;
        --domain=*)
            DOMAIN="${argument#*=}"
            require_option_value "--domain" "$DOMAIN"
            DOMAIN_ENABLE_SEEN=1
            domain_explicit=1
            ;;
        --no-domain)
            DOMAIN=""
            DISABLE_DOMAIN=1
            DOMAIN_DISABLE_SEEN=1
            domain_explicit=1
            ;;
        --admin-user)
            ((index += 1))
            require_option_value "$argument" "${args[$index]-}"
            ADMIN_USERNAME="${args[$index]}"
            admin_user_explicit=1
            ;;
        --admin-user=*) ADMIN_USERNAME="${argument#*=}"; admin_user_explicit=1 ;;
        --check) CHECK_ONLY=1 ;;
        --skip-tests) RUN_TESTS=0 ;;
        --force) FORCE_DEPLOY=1 ;;
        --update) UPDATE_MODE=1 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $argument" ;;
    esac
    ((index += 1))
done

(( DOMAIN_ENABLE_SEEN == 0 || DOMAIN_DISABLE_SEEN == 0 )) || \
    die "--domain and --no-domain cannot be used together"

[[ "$INSTALL_DIR" == /* ]] || die "--install-dir must be an absolute path"
[[ "$INSTALL_DIR" =~ ^/[A-Za-z0-9._/-]+$ ]] || die "--install-dir contains unsupported characters"
case "/${INSTALL_DIR#/}/" in
    */./*|*/../*) die "--install-dir must not contain '.' or '..' path components" ;;
esac
INSTALL_DIR="$(readlink -m -- "$INSTALL_DIR")"
[[ "$INSTALL_DIR" =~ ^/[A-Za-z0-9._/-]+$ ]] || die "normalized --install-dir is invalid"
case "$INSTALL_DIR" in
    /opt/*|/srv/*) : ;;
    *) die "--install-dir must be a child of /opt or /srv" ;;
esac
case "$INSTALL_DIR" in
    /|/bin|/boot|/dev|/etc|/home|/lib|/lib64|/opt|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/var)
        die "refusing to use a top-level system directory as --install-dir"
        ;;
esac
[[ ! -L "$INSTALL_DIR" ]] || die "--install-dir must not be a symbolic link"

check_trusted_directory() {
    local directory="$1"
    local owner mode mode_value
    [[ -d "$directory" && ! -L "$directory" ]] || die "untrusted directory: $directory"
    owner="$(stat -c '%u' -- "$directory")"
    mode="$(stat -c '%a' -- "$directory")"
    mode_value=$((8#$mode))
    [[ "$owner" == "0" ]] || die "$directory must be owned by root"
    (( (mode_value & 8#022) == 0 )) || die "$directory must not be group/world writable"
}

trusted_parent="$(dirname -- "$INSTALL_DIR")"
while [[ "$trusted_parent" != "/" ]]; do
    if [[ -e "$trusted_parent" ]]; then
        check_trusted_directory "$trusted_parent"
    fi
    [[ "$trusted_parent" != "/opt" && "$trusted_parent" != "/srv" ]] || break
    trusted_parent="$(dirname -- "$trusted_parent")"
done
if [[ -e "$INSTALL_DIR" ]]; then
    check_trusted_directory "$INSTALL_DIR"
fi
install -d -m 0755 -- "$INSTALL_DIR"
chown root:root "$INSTALL_DIR"
chmod 0755 "$INSTALL_DIR"
INSTALL_DIR="$(cd -- "$INSTALL_DIR" && pwd -P)"
case "$INSTALL_DIR" in
    /|/bin|/boot|/dev|/etc|/home|/lib|/lib64|/opt|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/var)
        die "normalized --install-dir resolves to a protected system directory"
        ;;
esac

ADMIN_DIR="$INSTALL_DIR/admin"
DEPLOY_CONFIG="$ADMIN_DIR/deploy.conf"
REPOSITORY_DIR="$INSTALL_DIR/repository"
STAGING_ROOT="$INSTALL_DIR/staging"
BACKUP_ROOT="$INSTALL_DIR/backups"
FAILED_ROOT="$INSTALL_DIR/failed"
VENV_ROOT="$INSTALL_DIR/venvs"
APP_DIR="$INSTALL_DIR/app"
SERVICE_ENV_FILE="$ADMIN_DIR/service.env"
SERVICE_ENV_NEXT="$ADMIN_DIR/.service.env.next.$$"
SERVICE_ENV_BACKUP="$ADMIN_DIR/.service.env.rollback.$$"
SERVICE_ENV_CHANGED=0
UNIT_CHANGED=0
ADMIN_METADATA_BACKUP="$ADMIN_DIR/.metadata.rollback.$$"
ADMIN_METADATA_CHANGED=0
PASSWORD_GENERATED=0
CADDY_BACKUP_DIR="$ADMIN_DIR/.caddy.rollback.$$"
CADDY_TRANSACTION_ACTIVE=0
CADDY_CONFIG_CHANGED=0
CADDY_WAS_ACTIVE=0
CADDY_WAS_ENABLED=0

install -d -m 0700 -- "$ADMIN_DIR" "$REPOSITORY_DIR" "$BACKUP_ROOT" "$FAILED_ROOT"
# Build children are private, while the service user needs execute-only traversal.
install -d -o root -g root -m 0711 -- "$STAGING_ROOT"
# The service needs to traverse this directory to reach the release-specific
# virtual environment, but cannot list it or modify any environment.
install -d -m 0711 -- "$VENV_ROOT"

saved_repository_url=""
saved_deploy_ref=""
saved_service_user=""
saved_service_name=""
saved_python_command=""
saved_domain=""

if [[ -f "$DEPLOY_CONFIG" ]]; then
    config_owner="$(stat -c '%u' -- "$DEPLOY_CONFIG")"
    config_mode="$(stat -c '%a' -- "$DEPLOY_CONFIG")"
    [[ "$config_owner" == "0" ]] || die "$DEPLOY_CONFIG must be owned by root"
    [[ "$config_mode" == "600" || "$config_mode" == "400" ]] || \
        die "$DEPLOY_CONFIG must have mode 0600 or 0400"
    while IFS='=' read -r key value; do
        case "$key" in
            REPOSITORY_URL) saved_repository_url="$value" ;;
            DEPLOY_REF) saved_deploy_ref="$value" ;;
            SERVICE_USER) saved_service_user="$value" ;;
            SERVICE_NAME) saved_service_name="$value" ;;
            PYTHON_COMMAND) saved_python_command="$value" ;;
            DOMAIN) saved_domain="$value" ;;
        esac
    done < "$DEPLOY_CONFIG"
fi

if (( repo_explicit == 0 )); then
    REPOSITORY_URL="${saved_repository_url:-$DEFAULT_REPOSITORY_URL}"
fi
if (( ref_explicit == 0 )); then
    DEPLOY_REF="${saved_deploy_ref:-$DEFAULT_REF}"
fi
if (( user_explicit == 0 )); then
    SERVICE_USER="${saved_service_user:-$DEFAULT_SERVICE_USER}"
fi
if (( service_explicit == 0 )); then
    SERVICE_NAME="${saved_service_name:-$DEFAULT_SERVICE_NAME}"
fi
if (( python_explicit == 0 )); then
    PYTHON_COMMAND="${saved_python_command:-python3}"
fi
if (( domain_explicit == 0 )); then
    DOMAIN="$saved_domain"
fi
if [[ -n "$DOMAIN" ]]; then
    validate_domain
fi

if [[ -d "$APP_DIR" && -n "$saved_service_user" && "$SERVICE_USER" != "$saved_service_user" ]]; then
    die "changing the service user on an existing deployment is not supported"
fi
if [[ -d "$APP_DIR" && -n "$saved_service_name" && "$SERVICE_NAME" != "$saved_service_name" ]]; then
    die "changing the systemd service name on an existing deployment is not supported"
fi

[[ -n "$REPOSITORY_URL" && "$REPOSITORY_URL" != -* ]] || die "invalid repository URL"
[[ "$REPOSITORY_URL" != *$'\n'* && "$REPOSITORY_URL" != *$'\r'* ]] || die "repository URL contains a newline"
[[ "$DEPLOY_REF" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$ ]] || die "invalid Git ref: $DEPLOY_REF"
[[ "$DEPLOY_REF" != *..* && "$DEPLOY_REF" != *//* && "$DEPLOY_REF" != */ ]] || die "unsafe Git ref: $DEPLOY_REF"
[[ "$SERVICE_USER" =~ ^[a-z_][a-z0-9_-]*[$]?$ ]] || die "invalid service user: $SERVICE_USER"
BUILD_USER="${SERVICE_USER%\$}-build"
[[ "$BUILD_USER" =~ ^[a-z_][a-z0-9_-]*$ && ${#BUILD_USER} -le 31 ]] || \
    die "derived build user is invalid; choose a shorter --user value"
[[ "$SERVICE_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9_.@-]*$ ]] || die "invalid service name: $SERVICE_NAME"
[[ "$PYTHON_COMMAND" != *$'\n'* && "$PYTHON_COMMAND" != *$'\r'* ]] || die "invalid Python command"
if [[ -n "$DOMAIN" && $host_explicit -eq 1 && "$BIND_HOST" != "127.0.0.1" ]]; then
    die "--domain manages the public listener and only supports --host 127.0.0.1"
fi
if [[ -n "$DOMAIN" && $port_explicit -eq 1 && "$BIND_PORT" == "443" ]]; then
    die "--port configures the unprivileged HTTP backend; Caddy manages public port 443"
fi

install_packages() {
    log "Installing required operating-system packages"
    if command -v apt-get >/dev/null 2>&1; then
        apt-get update
        DEBIAN_FRONTEND=noninteractive apt-get install -y \
            ca-certificates git python3 python3-pip python3-venv tar util-linux
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y ca-certificates git python3 python3-pip tar util-linux
    elif command -v yum >/dev/null 2>&1; then
        yum install -y ca-certificates git python3 python3-pip tar util-linux
    else
        die "install git, Python 3.10+ with venv, tar, and util-linux, then retry"
    fi
}

if ! command -v flock >/dev/null 2>&1; then
    install_packages
fi
command -v flock >/dev/null 2>&1 || die "flock is required (install util-linux)"
LOCK_FILE="$INSTALL_DIR/.deploy.lock"
exec 9>"$LOCK_FILE"
flock -n 9 || die "another deployment is already running"

if (( CHECK_ONLY )); then
    if ! command -v git >/dev/null 2>&1; then
        install_packages
    fi
    command -v git >/dev/null 2>&1 || die "git is required"
else
    missing_dependency=0
    for command_name in git tar systemctl runuser sync ps; do
        if ! command -v "$command_name" >/dev/null 2>&1; then
            missing_dependency=1
        fi
    done
    if ! command -v "$PYTHON_COMMAND" >/dev/null 2>&1; then
        missing_dependency=1
    fi
    if (( missing_dependency )); then
        install_packages
    fi

    command -v systemctl >/dev/null 2>&1 || die "systemd is required"
    command -v git >/dev/null 2>&1 || die "git is required"
    command -v tar >/dev/null 2>&1 || die "tar is required"
    command -v runuser >/dev/null 2>&1 || die "runuser is required"
    command -v sync >/dev/null 2>&1 || die "sync is required"
    command -v ps >/dev/null 2>&1 || die "ps is required"
    PYTHON_BIN="$(command -v "$PYTHON_COMMAND" || true)"
    [[ -n "$PYTHON_BIN" ]] || die "Python executable not found: $PYTHON_COMMAND"
    PYTHON_BIN="$(readlink -f -- "$PYTHON_BIN")"
    "$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' || \
        die "Python 3.10 or newer is required"

    existing_host=""
    existing_port=""
    existing_admin_username=""
    existing_admin_password=""
    existing_secure_cookie=""
    existing_trust_proxy=""
    existing_admin_reset=""
    if [[ -e "$SERVICE_ENV_FILE" ]]; then
        [[ -f "$SERVICE_ENV_FILE" && ! -L "$SERVICE_ENV_FILE" ]] || \
            die "$SERVICE_ENV_FILE must be a regular file"
        env_owner="$(stat -c '%u' -- "$SERVICE_ENV_FILE")"
        env_mode="$(stat -c '%a' -- "$SERVICE_ENV_FILE")"
        [[ "$env_owner" == "0" && "$env_mode" == "600" ]] || \
            die "$SERVICE_ENV_FILE must be owned by root with mode 0600"
        while IFS='=' read -r key value; do
            case "$key" in
                YKT_HOST) existing_host="$value" ;;
                YKT_PORT) existing_port="$value" ;;
                YKT_ADMIN_USERNAME) existing_admin_username="$value" ;;
                YKT_ADMIN_PASSWORD) existing_admin_password="$value" ;;
                YKT_SECURE_COOKIE) existing_secure_cookie="$value" ;;
                YKT_TRUST_PROXY) existing_trust_proxy="$value" ;;
                YKT_ADMIN_RESET) existing_admin_reset="$value" ;;
                ''|'#'*) : ;;
                *) die "unexpected setting in $SERVICE_ENV_FILE: $key" ;;
            esac
        done < "$SERVICE_ENV_FILE"
        [[ -n "$existing_host" && -n "$existing_port" && \
           -n "$existing_admin_username" && -n "$existing_admin_password" ]] || \
            die "$SERVICE_ENV_FILE is incomplete"
    fi

    (( host_explicit )) || BIND_HOST="${existing_host:-$DEFAULT_BIND_HOST}"
    (( port_explicit )) || BIND_PORT="${existing_port:-$DEFAULT_BIND_PORT}"
    (( admin_user_explicit )) || \
        ADMIN_USERNAME="${existing_admin_username:-$DEFAULT_ADMIN_USERNAME}"
    ADMIN_PASSWORD="$existing_admin_password"
    (( secure_cookie_explicit )) || \
        SECURE_COOKIE="${existing_secure_cookie:-$DEFAULT_SECURE_COOKIE}"
    (( trust_proxy_explicit )) || TRUST_PROXY="${existing_trust_proxy:-$DEFAULT_TRUST_PROXY}"
    (( admin_reset_explicit )) || ADMIN_RESET="${existing_admin_reset:-$DEFAULT_ADMIN_RESET}"
    if [[ -n "$DOMAIN" ]]; then
        if (( secure_cookie_explicit )) && [[ "$SECURE_COOKIE" != "1" ]]; then
            die "--domain requires YKT_SECURE_COOKIE=1"
        fi
        if (( trust_proxy_explicit )) && [[ "$TRUST_PROXY" != "1" ]]; then
            die "--domain requires YKT_TRUST_PROXY=1"
        fi
        BIND_HOST="127.0.0.1"
        SECURE_COOKIE="1"
        TRUST_PROXY="1"
    elif (( DISABLE_DOMAIN )); then
        (( host_explicit )) || BIND_HOST="$DEFAULT_BIND_HOST"
        (( secure_cookie_explicit )) || SECURE_COOKIE="$DEFAULT_SECURE_COOKIE"
        (( trust_proxy_explicit )) || TRUST_PROXY="$DEFAULT_TRUST_PROXY"
    fi
    if [[ -z "$ADMIN_PASSWORD" ]]; then
        ADMIN_PASSWORD="$("$PYTHON_BIN" -c 'import secrets; print(secrets.token_urlsafe(24))')"
        PASSWORD_GENERATED=1
    fi

    [[ "$BIND_HOST" =~ ^[-A-Za-z0-9.:]+$ ]] || die "invalid listen address: $BIND_HOST"
    "$PYTHON_BIN" -c '
import ipaddress
import sys

host = sys.argv[1]
if host != "localhost":
    ipaddress.ip_address(host)
' "$BIND_HOST" >/dev/null 2>&1 || die "listen address must be an IP address or localhost"
    [[ "$BIND_PORT" =~ ^[0-9]{1,5}$ ]] || die "invalid listen port: $BIND_PORT"
    (( 10#$BIND_PORT >= 1024 && 10#$BIND_PORT <= 65535 )) || \
        die "listen port must be between 1024 and 65535 for the unprivileged service"
    [[ "$ADMIN_USERNAME" =~ ^[-A-Za-z0-9._@]+$ ]] || die "invalid admin username"
    (( ${#ADMIN_USERNAME} <= 128 )) || die "admin username is too long"
    [[ "$ADMIN_PASSWORD" =~ ^[-A-Za-z0-9._~]+$ ]] || \
        die "admin password may contain only URL-safe letters, digits, '.', '_', '~', and '-'"
    (( ${#ADMIN_PASSWORD} >= 12 && ${#ADMIN_PASSWORD} <= 256 )) || \
        die "admin password must be between 12 and 256 characters"
    [[ "$SECURE_COOKIE" == "0" || "$SECURE_COOKIE" == "1" ]] || \
        die "YKT_SECURE_COOKIE must be 0 or 1"
    [[ "$TRUST_PROXY" == "0" || "$TRUST_PROXY" == "1" ]] || \
        die "YKT_TRUST_PROXY must be 0 or 1"
    [[ "$ADMIN_RESET" == "0" || "$ADMIN_RESET" == "1" ]] || \
        die "YKT_ADMIN_RESET must be 0 or 1"

    {
        printf 'YKT_HOST=%s\n' "$BIND_HOST"
        printf 'YKT_PORT=%s\n' "$BIND_PORT"
        printf 'YKT_ADMIN_USERNAME=%s\n' "$ADMIN_USERNAME"
        printf 'YKT_ADMIN_PASSWORD=%s\n' "$ADMIN_PASSWORD"
        printf 'YKT_SECURE_COOKIE=%s\n' "$SECURE_COOKIE"
        printf 'YKT_TRUST_PROXY=%s\n' "$TRUST_PROXY"
        printf 'YKT_ADMIN_RESET=%s\n' "$ADMIN_RESET"
    } > "$SERVICE_ENV_NEXT"
    chmod 0600 "$SERVICE_ENV_NEXT"
    trap 'if (( ${CADDY_TRANSACTION_ACTIVE:-0} )); then restore_https_proxy 2>/dev/null || warn "Caddy rollback failed; backup retained at ${CADDY_BACKUP_DIR:-unknown}"; fi; if (( ${SERVICE_ENV_CHANGED:-0} )); then restore_service_env 2>/dev/null || true; fi; if (( ${UNIT_CHANGED:-0} )); then restore_systemd_unit 2>/dev/null || true; fi; rm -f -- "${SERVICE_ENV_NEXT:-}" "${SERVICE_ENV_BACKUP:-}" "${UNIT_TEMP:-}" "${UNIT_BACKUP:-}" 2>/dev/null || true' EXIT

    if ! getent group "$SERVICE_USER" >/dev/null 2>&1; then
        groupadd --system "$SERVICE_USER"
    fi
    if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
        nologin_shell="$(command -v nologin || true)"
        [[ -n "$nologin_shell" ]] || nologin_shell="/usr/sbin/nologin"
        useradd --system --gid "$SERVICE_USER" --home-dir "$INSTALL_DIR" \
            --no-create-home --shell "$nologin_shell" "$SERVICE_USER"
    fi
    [[ "$(id -u "$SERVICE_USER")" != "0" ]] || die "service user must not have UID 0"

    if ! getent group "$BUILD_USER" >/dev/null 2>&1; then
        groupadd --system "$BUILD_USER"
    fi
    if ! id -u "$BUILD_USER" >/dev/null 2>&1; then
        nologin_shell="$(command -v nologin || true)"
        [[ -n "$nologin_shell" ]] || nologin_shell="/usr/sbin/nologin"
        useradd --system --gid "$BUILD_USER" --home-dir "$STAGING_ROOT" \
            --no-create-home --shell "$nologin_shell" "$BUILD_USER"
    fi
    [[ "$(id -u "$BUILD_USER")" != "0" ]] || die "build user must not have UID 0"
    [[ "$(id -u "$BUILD_USER")" != "$(id -u "$SERVICE_USER")" ]] || \
        die "build user and service user must have different UIDs"

    UNIT_PATH="/etc/systemd/system/$SERVICE_NAME.service"
    UNIT_TEMP="$STAGING_ROOT/$SERVICE_NAME.service.$$"
    UNIT_BACKUP="$ADMIN_DIR/.systemd-unit.rollback.$$"
    cat > "$UNIT_TEMP" <<EOF
[Unit]
Description=Yuketang answer panel
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
EnvironmentFile=$SERVICE_ENV_FILE
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/server.py
Restart=on-failure
RestartSec=5s
TimeoutStopSec=30s
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$APP_DIR
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
ProtectClock=true
ProtectHostname=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6

[Install]
WantedBy=multi-user.target
EOF
fi

if [[ ! -d "$REPOSITORY_DIR/.git" ]]; then
    if [[ -n "$(find "$REPOSITORY_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
        die "$REPOSITORY_DIR exists but is not a Git repository"
    fi
    log "Cloning $REPOSITORY_URL"
    git clone -- "$REPOSITORY_URL" "$REPOSITORY_DIR"
else
    current_origin="$(git -C "$REPOSITORY_DIR" remote get-url origin 2>/dev/null || true)"
    if [[ "$current_origin" != "$REPOSITORY_URL" ]]; then
        log "Changing deployment repository origin"
        git -C "$REPOSITORY_DIR" remote set-url origin "$REPOSITORY_URL"
    fi
fi

log "Fetching $DEPLOY_REF"
git -C "$REPOSITORY_DIR" fetch --force --prune --tags origin \
    '+refs/heads/*:refs/remotes/origin/*'

TARGET_COMMIT=""
if git -C "$REPOSITORY_DIR" show-ref --verify --quiet "refs/remotes/origin/$DEPLOY_REF"; then
    TARGET_COMMIT="$(git -C "$REPOSITORY_DIR" rev-parse --verify "refs/remotes/origin/$DEPLOY_REF^{commit}")"
elif git -C "$REPOSITORY_DIR" show-ref --verify --quiet "refs/tags/$DEPLOY_REF"; then
    TARGET_COMMIT="$(git -C "$REPOSITORY_DIR" rev-parse --verify "refs/tags/$DEPLOY_REF^{commit}")"
elif [[ "$DEPLOY_REF" =~ ^[0-9a-fA-F]{40}$ ]] && \
     git -C "$REPOSITORY_DIR" cat-file -e "$DEPLOY_REF^{commit}" 2>/dev/null; then
    TARGET_COMMIT="$(git -C "$REPOSITORY_DIR" rev-parse --verify "$DEPLOY_REF^{commit}")"
fi
[[ -n "$TARGET_COMMIT" ]] || die "could not resolve '$DEPLOY_REF' from origin"
SHORT_COMMIT="${TARGET_COMMIT:0:12}"

worktree_changes="$(git -C "$REPOSITORY_DIR" status --porcelain --untracked-files=normal)"
if [[ -n "$worktree_changes" ]]; then
    printf '%s\n' "$worktree_changes" >&2
    die "deployment repository is not clean; refusing to overwrite local changes"
fi

LOCAL_COMMIT="$(git -C "$REPOSITORY_DIR" rev-parse --verify 'HEAD^{commit}')"
CURRENT_COMMIT_FILE="$ADMIN_DIR/current_commit"
CURRENT_COMMIT=""
if [[ -f "$CURRENT_COMMIT_FILE" && ! -L "$CURRENT_COMMIT_FILE" ]]; then
    CURRENT_COMMIT="$(tr -d '\r\n' < "$CURRENT_COMMIT_FILE")"
    [[ "$CURRENT_COMMIT" =~ ^[0-9a-fA-F]{40}$ ]] || die "$CURRENT_COMMIT_FILE is invalid"
    git -C "$REPOSITORY_DIR" cat-file -e "$CURRENT_COMMIT^{commit}" 2>/dev/null || \
        die "installed commit is not present in the deployment repository"
fi
APP_COMMIT=""
if [[ -f "$APP_DIR/.release" && ! -L "$APP_DIR/.release" ]]; then
    APP_COMMIT="$(tr -d '\r\n' < "$APP_DIR/.release")"
    [[ "$APP_COMMIT" =~ ^[0-9a-fA-F]{40}$ ]] || die "$APP_DIR/.release is invalid"
fi
if [[ -n "$CURRENT_COMMIT" && -n "$APP_COMMIT" && "$CURRENT_COMMIT" != "$APP_COMMIT" ]]; then
    die "installed release metadata disagree; inspect $CURRENT_COMMIT_FILE and $APP_DIR/.release"
fi
[[ -n "$CURRENT_COMMIT" ]] || CURRENT_COMMIT="$APP_COMMIT"

if (( UPDATE_MODE )) && [[ -z "$CURRENT_COMMIT" || ! -d "$APP_DIR" ]]; then
    die "no managed deployment was found; run deploy.sh first"
fi

if (( CHECK_ONLY == 0 )) && [[ ! -d "$APP_DIR" && -z "$CURRENT_COMMIT" ]] && \
   [[ "$LOCAL_COMMIT" != "$TARGET_COMMIT" ]]; then
    log "Initializing deployment repository at requested ref $DEPLOY_REF"
    git -C "$REPOSITORY_DIR" checkout --detach "$TARGET_COMMIT"
    LOCAL_COMMIT="$TARGET_COMMIT"
fi
STATUS_COMMIT="${CURRENT_COMMIT:-$LOCAL_COMMIT}"
LOCAL_SUMMARY="$(git -C "$REPOSITORY_DIR" show -s --format='%h %s' "$STATUS_COMMIT")"
UPSTREAM_SUMMARY="$(git -C "$REPOSITORY_DIR" show -s --format='%h %s' "$TARGET_COMMIT")"
UPDATE_STATUS=""
if [[ "$STATUS_COMMIT" == "$TARGET_COMMIT" ]]; then
    UPDATE_STATUS="up-to-date"
elif git -C "$REPOSITORY_DIR" merge-base --is-ancestor "$STATUS_COMMIT" "$TARGET_COMMIT"; then
    UPDATE_STATUS="update-available"
elif git -C "$REPOSITORY_DIR" merge-base --is-ancestor "$TARGET_COMMIT" "$STATUS_COMMIT"; then
    UPDATE_STATUS="local-ahead"
else
    UPDATE_STATUS="diverged"
fi

log "Update status: $UPDATE_STATUS"
log "Installed/current: $LOCAL_SUMMARY"
log "Upstream ($DEPLOY_REF): $UPSTREAM_SUMMARY"
case "$UPDATE_STATUS" in
    update-available)
        pending_count="$(git -C "$REPOSITORY_DIR" rev-list --count "$STATUS_COMMIT..$TARGET_COMMIT")"
        log "$pending_count fast-forward commit(s) available"
        ;;
    local-ahead)
        ahead_count="$(git -C "$REPOSITORY_DIR" rev-list --count "$TARGET_COMMIT..$STATUS_COMMIT")"
        log "Local HEAD is $ahead_count commit(s) ahead; automatic update is disabled"
        ;;
    diverged)
        log "Local HEAD and upstream have diverged; automatic update is disabled"
        ;;
esac

if (( CHECK_ONLY )); then
    exit 0
fi

write_deploy_config() {
    local temp_config="$ADMIN_DIR/.deploy.conf.$$"
    {
        printf 'REPOSITORY_URL=%s\n' "$REPOSITORY_URL"
        printf 'DEPLOY_REF=%s\n' "$DEPLOY_REF"
        printf 'SERVICE_USER=%s\n' "$SERVICE_USER"
        printf 'SERVICE_NAME=%s\n' "$SERVICE_NAME"
        printf 'PYTHON_COMMAND=%s\n' "$PYTHON_BIN"
        printf 'DOMAIN=%s\n' "$DOMAIN"
    } > "$temp_config" || return 1
    chmod 0600 "$temp_config" || return 1
    mv -f -- "$temp_config" "$DEPLOY_CONFIG" || return 1
}

backup_admin_metadata() {
    local name source
    install -d -o root -g root -m 0700 -- "$ADMIN_METADATA_BACKUP" || return 1
    for name in deploy.sh update.sh deploy.conf current_commit; do
        source="$ADMIN_DIR/$name"
        if [[ -e "$source" ]]; then
            [[ -f "$source" && ! -L "$source" ]] || return 1
            cp -a -- "$source" "$ADMIN_METADATA_BACKUP/$name" || return 1
            : > "$ADMIN_METADATA_BACKUP/.had-$name" || return 1
        fi
    done
    ADMIN_METADATA_CHANGED=1
}

restore_admin_metadata() {
    local name
    if (( ADMIN_METADATA_CHANGED == 0 )); then
        return 0
    fi
    for name in deploy.sh update.sh deploy.conf current_commit; do
        if [[ -f "$ADMIN_METADATA_BACKUP/.had-$name" ]]; then
            cp -a -- "$ADMIN_METADATA_BACKUP/$name" "$ADMIN_DIR/$name" || return 1
        else
            rm -f -- "$ADMIN_DIR/$name" || return 1
        fi
    done
    rm -rf -- "$ADMIN_METADATA_BACKUP" || return 1
    ADMIN_METADATA_CHANGED=0
}

finalize_admin_metadata() {
    local result=0
    rm -rf -- "$ADMIN_METADATA_BACKUP" || result=1
    ADMIN_METADATA_CHANGED=0
    return "$result"
}

activate_systemd_unit() {
    if [[ -f "$UNIT_PATH" ]] && cmp -s -- "$UNIT_TEMP" "$UNIT_PATH"; then
        rm -f -- "$UNIT_TEMP" || return 1
        return 0
    fi
    if [[ -f "$UNIT_PATH" ]]; then
        cp -a -- "$UNIT_PATH" "$UNIT_BACKUP" || return 1
        chmod 0600 "$UNIT_BACKUP" || return 1
    fi
    install -o root -g root -m 0644 -- "$UNIT_TEMP" "$UNIT_PATH" || return 1
    UNIT_CHANGED=1
    systemctl daemon-reload || return 1
    log "Installed systemd unit $UNIT_PATH"
}

restore_systemd_unit() {
    if (( UNIT_CHANGED == 0 )); then
        return 0
    fi
    if [[ -f "$UNIT_BACKUP" ]]; then
        install -o root -g root -m 0644 -- "$UNIT_BACKUP" "$UNIT_PATH" || return 1
    else
        rm -f -- "$UNIT_PATH" || return 1
        systemctl disable "$SERVICE_NAME" >/dev/null 2>&1 || true
    fi
    systemctl daemon-reload || return 1
    UNIT_CHANGED=0
}

finalize_systemd_unit() {
    local result=0
    rm -f -- "$UNIT_BACKUP" "$UNIT_TEMP" || result=1
    UNIT_CHANGED=0
    return "$result"
}

activate_service_env() {
    if [[ -f "$SERVICE_ENV_FILE" ]] && cmp -s -- "$SERVICE_ENV_NEXT" "$SERVICE_ENV_FILE"; then
        rm -f -- "$SERVICE_ENV_NEXT" || return 1
        return 0
    fi
    if [[ -f "$SERVICE_ENV_FILE" ]]; then
        cp -a -- "$SERVICE_ENV_FILE" "$SERVICE_ENV_BACKUP" || return 1
        chmod 0600 "$SERVICE_ENV_BACKUP" || return 1
    fi
    mv -f -- "$SERVICE_ENV_NEXT" "$SERVICE_ENV_FILE" || return 1
    SERVICE_ENV_CHANGED=1
    chown root:root "$SERVICE_ENV_FILE" || return 1
    chmod 0600 "$SERVICE_ENV_FILE" || return 1
}

restore_service_env() {
    if (( SERVICE_ENV_CHANGED == 0 )); then
        return 0
    fi
    if [[ -f "$SERVICE_ENV_BACKUP" ]]; then
        mv -f -- "$SERVICE_ENV_BACKUP" "$SERVICE_ENV_FILE" || return 1
    else
        rm -f -- "$SERVICE_ENV_FILE" || return 1
    fi
    SERVICE_ENV_CHANGED=0
}

finalize_service_env() {
    local result=0
    rm -f -- "$SERVICE_ENV_BACKUP" || result=1
    SERVICE_ENV_CHANGED=0
    return "$result"
}

set_caddy_paths() {
    CADDY_MAIN_CONFIG="/etc/caddy/Caddyfile"
    CADDY_SITE_DIR="/etc/caddy/ykt-web.d"
    CADDY_SITE_FILE="$CADDY_SITE_DIR/$SERVICE_NAME.caddy"
    CADDY_IMPORT_LINE="import /etc/caddy/ykt-web.d/*.caddy"
    CADDY_IMPORT_BEGIN="# BEGIN ykt-web managed import"
    CADDY_IMPORT_END="# END ykt-web managed import"
    CADDY_SITE_MARKER="# Managed by ykt-web deploy ($SERVICE_NAME)"
}

capture_caddy_transaction() {
    if (( CADDY_TRANSACTION_ACTIVE )); then
        return 0
    fi
    [[ ! -e "$CADDY_BACKUP_DIR" ]] || {
        warn "Refusing to overwrite an existing Caddy rollback directory: $CADDY_BACKUP_DIR"
        return 1
    }
    CADDY_WAS_ACTIVE=0
    CADDY_WAS_ENABLED=0
    systemctl is-active --quiet caddy 2>/dev/null && CADDY_WAS_ACTIVE=1
    systemctl is-enabled --quiet caddy 2>/dev/null && CADDY_WAS_ENABLED=1
    install -d -o root -g root -m 0700 -- "$CADDY_BACKUP_DIR" || return 1
    CADDY_TRANSACTION_ACTIVE=1
}

install_caddy_package() {
    command -v caddy >/dev/null 2>&1 && return 0
    log "Installing Caddy for automatic HTTPS"
    if command -v apt-get >/dev/null 2>&1; then
        apt-get update || return 1
        DEBIAN_FRONTEND=noninteractive apt-get install -y caddy || return 1
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y caddy || return 1
    elif command -v yum >/dev/null 2>&1; then
        yum install -y caddy || return 1
    else
        warn "No supported package manager was found; install Caddy and retry"
        return 1
    fi
    command -v caddy >/dev/null 2>&1 || {
        warn "Caddy is unavailable from the configured package repositories"
        return 1
    }
}

check_caddy_file() {
    local path="$1"
    local owner mode mode_value
    [[ ! -e "$path" ]] && return 0
    [[ -f "$path" && ! -L "$path" ]] || {
        warn "Refusing unsafe Caddy configuration path: $path"
        return 1
    }
    owner="$(stat -c '%u' -- "$path")" || return 1
    mode="$(stat -c '%a' -- "$path")" || return 1
    mode_value=$((8#$mode))
    [[ "$owner" == "0" && $((mode_value & 8#022)) -eq 0 ]] || {
        warn "$path must be root-owned and not group/world writable"
        return 1
    }
}

backup_caddy_files() {
    [[ -f "$CADDY_BACKUP_DIR/.files-captured" ]] && return 0
    check_caddy_file "$CADDY_MAIN_CONFIG" || return 1
    check_caddy_file "$CADDY_SITE_FILE" || return 1
    if [[ -f "$CADDY_SITE_FILE" ]] && \
       [[ "$(head -n 1 -- "$CADDY_SITE_FILE")" != "$CADDY_SITE_MARKER" ]]; then
        warn "Refusing to replace unmanaged Caddy site: $CADDY_SITE_FILE"
        return 1
    fi
    if [[ -f "$CADDY_MAIN_CONFIG" ]]; then
        cp -a -- "$CADDY_MAIN_CONFIG" "$CADDY_BACKUP_DIR/Caddyfile" || return 1
        : > "$CADDY_BACKUP_DIR/.had-main" || return 1
    fi
    if [[ -f "$CADDY_SITE_FILE" ]]; then
        cp -a -- "$CADDY_SITE_FILE" "$CADDY_BACKUP_DIR/site.caddy" || return 1
        : > "$CADDY_BACKUP_DIR/.had-site" || return 1
    fi
    : > "$CADDY_BACKUP_DIR/.files-captured" || return 1
}

restore_https_proxy() {
    local restore_temp result=0
    (( CADDY_TRANSACTION_ACTIVE )) || return 0
    set_caddy_paths
    if [[ -f "$CADDY_BACKUP_DIR/.files-captured" ]]; then
        if [[ -f "$CADDY_BACKUP_DIR/.had-main" ]]; then
            restore_temp="$CADDY_MAIN_CONFIG.restore.$$"
            rm -f -- "$restore_temp" || result=1
            if ! cp -a -- "$CADDY_BACKUP_DIR/Caddyfile" "$restore_temp" || \
               ! mv -f -- "$restore_temp" "$CADDY_MAIN_CONFIG"; then
                result=1
            fi
        else
            rm -f -- "$CADDY_MAIN_CONFIG" || result=1
        fi
        if [[ -f "$CADDY_BACKUP_DIR/.had-site" ]]; then
            install -d -o root -g root -m 0755 -- "$CADDY_SITE_DIR" || result=1
            restore_temp="$CADDY_SITE_FILE.restore.$$"
            rm -f -- "$restore_temp" || result=1
            if ! cp -a -- "$CADDY_BACKUP_DIR/site.caddy" "$restore_temp" || \
               ! mv -f -- "$restore_temp" "$CADDY_SITE_FILE"; then
                result=1
            fi
        else
            rm -f -- "$CADDY_SITE_FILE" || result=1
        fi
    fi
    if command -v caddy >/dev/null 2>&1; then
        if (( CADDY_WAS_ENABLED )); then
            systemctl enable caddy >/dev/null 2>&1 || result=1
        else
            systemctl disable caddy >/dev/null 2>&1 || result=1
        fi
        if (( CADDY_WAS_ACTIVE )); then
            systemctl restart caddy >/dev/null 2>&1 || result=1
        else
            systemctl stop caddy >/dev/null 2>&1 || result=1
        fi
    elif (( CADDY_WAS_ACTIVE || CADDY_WAS_ENABLED )); then
        result=1
    fi
    if (( result )); then
        warn "Caddy rollback was incomplete; backup retained at $CADDY_BACKUP_DIR"
        return 1
    fi
    rm -rf -- "$CADDY_BACKUP_DIR" || return 1
    CADDY_CONFIG_CHANGED=0
    CADDY_TRANSACTION_ACTIVE=0
}

finalize_https_proxy() {
    local result=0
    (( CADDY_TRANSACTION_ACTIVE )) || return 0
    rm -rf -- "$CADDY_BACKUP_DIR" || result=1
    CADDY_CONFIG_CHANGED=0
    CADDY_TRANSACTION_ACTIVE=0
    return "$result"
}

activate_https_proxy() {
    local unit_definition main_next site_next main_changed=0 site_changed=0
    local tls_ready=0 attempt
    if [[ -z "$DOMAIN" && $DISABLE_DOMAIN -eq 0 ]]; then
        return 0
    fi
    set_caddy_paths
    exec 8>"/run/lock/ykt-web-caddy.lock"
    flock -n 8 || {
        warn "Another Caddy configuration change is in progress"
        return 1
    }
    capture_caddy_transaction || return 1

    if [[ -z "$DOMAIN" ]]; then
        if [[ ! -e "$CADDY_SITE_FILE" ]]; then
            return 0
        fi
        command -v caddy >/dev/null 2>&1 || {
            warn "The managed Caddy site exists but the caddy command is unavailable"
            return 1
        }
        backup_caddy_files || return 1
        rm -f -- "$CADDY_SITE_FILE" || return 1
        CADDY_CONFIG_CHANGED=1
    else
        install_caddy_package || {
            warn "Automatic HTTPS requires the Caddy package"
            return 1
        }
        unit_definition="$(systemctl cat caddy 2>/dev/null)" || {
            warn "The Caddy systemd service is unavailable"
            return 1
        }
        [[ "$unit_definition" == *"/etc/caddy/Caddyfile"* ]] || {
            warn "The existing Caddy service uses a non-standard config path; configure it manually"
            return 1
        }
        [[ ! -e "$CADDY_SITE_DIR" || ( -d "$CADDY_SITE_DIR" && ! -L "$CADDY_SITE_DIR" ) ]] || {
            warn "Refusing unsafe Caddy site directory: $CADDY_SITE_DIR"
            return 1
        }
        install -d -o root -g root -m 0755 -- "$CADDY_SITE_DIR" || return 1
        backup_caddy_files || return 1
        main_next="$ADMIN_DIR/.Caddyfile.next.$$"
        site_next="$ADMIN_DIR/.caddy-site.next.$$"
        rm -f -- "$main_next" "$site_next" || return 1
        if [[ -f "$CADDY_MAIN_CONFIG" ]]; then
            cp -a -- "$CADDY_MAIN_CONFIG" "$main_next" || return 1
        else
            : > "$main_next" || return 1
        fi
        if ! grep -Fqx -- "$CADDY_IMPORT_LINE" "$main_next"; then
            if grep -Fq -- "$CADDY_IMPORT_BEGIN" "$main_next" || \
               grep -Fq -- "$CADDY_IMPORT_END" "$main_next"; then
                warn "The managed import markers in $CADDY_MAIN_CONFIG are incomplete"
                return 1
            fi
            printf '\n%s\n%s\n%s\n' \
                "$CADDY_IMPORT_BEGIN" "$CADDY_IMPORT_LINE" "$CADDY_IMPORT_END" \
                >> "$main_next" || return 1
        fi
        cat > "$site_next" <<EOF
$CADDY_SITE_MARKER
$DOMAIN {
    encode zstd gzip
    reverse_proxy 127.0.0.1:$BIND_PORT {
        header_up X-Forwarded-For {remote_host}
        header_up X-Forwarded-Proto {scheme}
        header_up X-Forwarded-Host {host}
    }
}
EOF
        if [[ ! -f "$CADDY_MAIN_CONFIG" ]] || ! cmp -s -- "$main_next" "$CADDY_MAIN_CONFIG"; then
            main_changed=1
        fi
        if [[ ! -f "$CADDY_SITE_FILE" ]] || ! cmp -s -- "$site_next" "$CADDY_SITE_FILE"; then
            site_changed=1
        fi
        if (( site_changed )); then
            install -o root -g root -m 0644 -- "$site_next" "$CADDY_SITE_FILE.next.$$" || return 1
            mv -f -- "$CADDY_SITE_FILE.next.$$" "$CADDY_SITE_FILE" || return 1
            CADDY_CONFIG_CHANGED=1
        fi
        if (( main_changed )); then
            if [[ -f "$CADDY_MAIN_CONFIG" ]]; then
                cp -a -- "$main_next" "$CADDY_MAIN_CONFIG.next.$$" || return 1
            else
                install -o root -g root -m 0644 -- \
                    "$main_next" "$CADDY_MAIN_CONFIG.next.$$" || return 1
            fi
            mv -f -- "$CADDY_MAIN_CONFIG.next.$$" "$CADDY_MAIN_CONFIG" || return 1
            CADDY_CONFIG_CHANGED=1
        fi
        rm -f -- "$main_next" "$site_next"
    fi

    caddy validate --config "$CADDY_MAIN_CONFIG" --adapter caddyfile || {
        warn "Caddy rejected the generated HTTPS configuration"
        return 1
    }
    if [[ -z "$DOMAIN" ]]; then
        if (( CADDY_WAS_ACTIVE && CADDY_CONFIG_CHANGED )); then
            systemctl reload caddy || return 1
        fi
        return 0
    fi
    systemctl enable caddy >/dev/null || return 1
    if systemctl is-active --quiet caddy; then
        if (( CADDY_CONFIG_CHANGED )); then
            systemctl reload caddy || return 1
        fi
    else
        systemctl start caddy || return 1
    fi
    systemctl is-active --quiet caddy || return 1

    if [[ -n "$DOMAIN" ]]; then
        if ! getent ahosts "$DOMAIN" >/dev/null 2>&1; then
            warn "$DOMAIN does not resolve yet; Caddy will obtain a certificate after DNS is ready"
        fi
        if command -v curl >/dev/null 2>&1; then
            for attempt in 1 2 3 4 5; do
                if curl --fail --silent --show-error --max-time 3 \
                    --resolve "$DOMAIN:443:127.0.0.1" \
                    "https://$DOMAIN/api/health" >/dev/null 2>&1; then
                    tls_ready=1
                    break
                fi
                sleep 2
            done
            (( tls_ready )) || warn "HTTPS is not ready yet; verify DNS and inbound TCP ports 80/443"
        fi
    fi
    return 0
}

if [[ "$BIND_HOST" == "0.0.0.0" || "$BIND_HOST" == "127.0.0.1" || \
      "$BIND_HOST" == "localhost" ]]; then
    HEALTH_HOST="127.0.0.1"
elif [[ "$BIND_HOST" == "::" || "$BIND_HOST" == "::1" ]]; then
    HEALTH_HOST="[::1]"
else
    HEALTH_HOST="$BIND_HOST"
fi
HEALTH_URL="http://$HEALTH_HOST:$BIND_PORT/api/health"

health_check() {
    local attempts="${1:-30}"
    local counter
    for ((counter = 1; counter <= attempts; counter += 1)); do
        if systemctl is-active --quiet "$SERVICE_NAME" && \
           YKT_HEALTH_URL="$HEALTH_URL" "$APP_DIR/.venv/bin/python" -c '
import json
import os
import urllib.request

response = urllib.request.urlopen(os.environ["YKT_HEALTH_URL"], timeout=1)
with response:
    payload = json.load(response)
raise SystemExit(0 if payload.get("ok") is True else 1)
' \
               >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    return 1
}

print_access_details() {
    local access_host="$BIND_HOST"
    local detected_ip=""
    if [[ "$BIND_HOST" == "0.0.0.0" ]]; then
        detected_ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
        access_host="${detected_ip:-SERVER_IP}"
    elif [[ "$BIND_HOST" == "::" ]]; then
        detected_ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
        access_host="${detected_ip:-SERVER_IP}"
    fi
    if [[ "$access_host" == *:* && "$access_host" != \[*\] ]]; then
        access_host="[$access_host]"
    fi
    if [[ -n "$DOMAIN" ]]; then
        log "Access URL: https://$DOMAIN/"
    else
        log "Access URL: http://$access_host:$BIND_PORT/"
    fi
    log "Admin username: $ADMIN_USERNAME"
    if (( PASSWORD_GENERATED )); then
        printf 'Initial admin password (shown once): %s\n' "$ADMIN_PASSWORD"
    fi
    if [[ -n "$DOMAIN" ]]; then
        warn "Allow inbound TCP 80/443 in the cloud security group and firewall; do not expose backend port $BIND_PORT."
    else
        warn "For Internet access, rerun this deployer with --domain and restrict port $BIND_PORT."
    fi
}

if [[ -n "$CURRENT_COMMIT" && -d "$APP_DIR" ]]; then
    case "$UPDATE_STATUS" in
        local-ahead)
            die "local HEAD is ahead of upstream; only fast-forward updates are allowed"
            ;;
        diverged)
            die "local HEAD has diverged from upstream; only fast-forward updates are allowed"
            ;;
    esac
fi

if (( UPDATE_MODE && FORCE_DEPLOY == 0 && domain_explicit == 0 )) && \
   [[ "$CURRENT_COMMIT" == "$TARGET_COMMIT" ]]; then
    log "No update is available; leaving the running service unchanged"
    exit 0
fi

if (( FORCE_DEPLOY == 0 )) && [[ "$CURRENT_COMMIT" == "$TARGET_COMMIT" ]] && \
   [[ -f "$APP_DIR/server.py" && -x "$APP_DIR/.venv/bin/python" ]]; then
    log "Commit $SHORT_COMMIT is already installed"
    existing_service_active=0
    systemctl is-active --quiet "$SERVICE_NAME" && existing_service_active=1
    activate_systemd_unit || die "could not install the systemd unit"
    activate_service_env || die "could not install the protected service environment file"
    if ! systemctl enable --now "$SERVICE_NAME"; then
        restore_service_env || true
        restore_systemd_unit || true
        (( existing_service_active )) && systemctl restart "$SERVICE_NAME" || true
        die "systemd could not start the installed service"
    fi
    if (( (SERVICE_ENV_CHANGED || UNIT_CHANGED) && existing_service_active )); then
        if ! systemctl restart "$SERVICE_NAME"; then
            restore_service_env || true
            restore_systemd_unit || true
            systemctl restart "$SERVICE_NAME" || true
            die "systemd could not apply the service environment"
        fi
    fi
    if ! health_check 10; then
        if (( SERVICE_ENV_CHANGED || UNIT_CHANGED )); then
            warn "Service settings failed their health check; restoring the previous settings"
            restore_service_env
            restore_systemd_unit
            systemctl restart "$SERVICE_NAME" || true
            health_check 20 || warn "Previous service environment was restored but the service is unhealthy"
            journalctl -u "$SERVICE_NAME" -n 40 --no-pager >&2 || true
            die "the service settings change was rolled back"
        else
            warn "Existing service is unhealthy; restarting it once"
            systemctl restart "$SERVICE_NAME" || true
            if ! health_check 30; then
                journalctl -u "$SERVICE_NAME" -n 40 --no-pager >&2 || true
                die "the installed service did not pass its health check"
            fi
        fi
    fi
    if ! activate_https_proxy; then
        restore_https_proxy || true
        restore_service_env || true
        restore_systemd_unit || true
        if (( existing_service_active )); then
            systemctl restart "$SERVICE_NAME" || true
        else
            systemctl stop "$SERVICE_NAME" >/dev/null 2>&1 || true
        fi
        die "automatic HTTPS setup failed; previous service settings were restored"
    fi
    if ! write_deploy_config; then
        restore_https_proxy || true
        restore_service_env || true
        restore_systemd_unit || true
        systemctl restart "$SERVICE_NAME" || true
        die "could not write deployment metadata; previous settings were restored"
    fi
    finalize_service_env || warn "could not remove the service-environment rollback copy"
    finalize_systemd_unit || warn "could not remove the systemd-unit rollback copy"
    finalize_https_proxy || warn "could not remove the Caddy rollback copy"
    print_access_details
    exit 0
fi

release_stamp="$(date -u +%Y%m%dT%H%M%SZ)"
CANDIDATE_DIR="$STAGING_ROOT/release-$release_stamp-$SHORT_COMMIT-$$"
VENV_DIR="$VENV_ROOT/$release_stamp-$SHORT_COMMIT-$$"
ADMIN_DEPLOY_NEXT="$ADMIN_DIR/.deploy.sh.next.$$"
ADMIN_UPDATE_NEXT="$ADMIN_DIR/.update.sh.next.$$"
candidate_live=1
candidate_activated=0
service_was_active=0
service_stopped=0
backup_dir=""

cleanup_staging() {
    if (( ${service_stopped:-0} )); then
        systemctl stop "$SERVICE_NAME" >/dev/null 2>&1 || true
        if (( ${candidate_activated:-0} )) && [[ -n "${backup_dir:-}" && -d "$backup_dir" ]]; then
            emergency_failed="$FAILED_ROOT/interrupted-$release_stamp-$SHORT_COMMIT-$$"
            [[ ! -d "$APP_DIR" ]] || mv -- "$APP_DIR" "$emergency_failed" 2>/dev/null || true
            [[ -d "$APP_DIR" ]] || mv -- "$backup_dir" "$APP_DIR" 2>/dev/null || true
        elif [[ ! -d "$APP_DIR" && -n "${backup_dir:-}" && -d "$backup_dir" ]]; then
            mv -- "$backup_dir" "$APP_DIR" 2>/dev/null || true
        fi
    fi
    if (( ${CADDY_TRANSACTION_ACTIVE:-0} )); then
        restore_https_proxy || \
            warn "Caddy rollback failed; backup retained at $CADDY_BACKUP_DIR"
    fi
    if (( ${SERVICE_ENV_CHANGED:-0} )); then
        restore_service_env 2>/dev/null || true
    fi
    if (( ${UNIT_CHANGED:-0} )); then
        restore_systemd_unit 2>/dev/null || true
    fi
    if (( ${ADMIN_METADATA_CHANGED:-0} )); then
        restore_admin_metadata 2>/dev/null || true
    fi
    if (( ${service_stopped:-0} && ${service_was_active:-0} )) && [[ -d "$APP_DIR" ]]; then
        systemctl start "$SERVICE_NAME" >/dev/null 2>&1 || true
    fi
    if (( candidate_live )) && [[ -d "${CANDIDATE_DIR:-}" ]]; then
        abandoned="$FAILED_ROOT/abandoned-$release_stamp-$SHORT_COMMIT-$$"
        mv -- "$CANDIDATE_DIR" "$abandoned" 2>/dev/null || true
        warn "Unfinished candidate retained at $abandoned"
        rm -rf -- "${VENV_DIR:-}" 2>/dev/null || true
    fi
    rm -f -- "${ADMIN_DEPLOY_NEXT:-}" "${ADMIN_UPDATE_NEXT:-}" \
        "${SERVICE_ENV_NEXT:-}" "${SERVICE_ENV_BACKUP:-}" \
        "${UNIT_TEMP:-}" "${UNIT_BACKUP:-}" 2>/dev/null || true
    rm -rf -- "${ADMIN_METADATA_BACKUP:-}" 2>/dev/null || true
    rm -rf -- "${BUILD_HOME:-}" 2>/dev/null || true
}
trap cleanup_staging EXIT

terminate_builder_processes() {
    local process_id
    while IFS= read -r process_id; do
        process_id="${process_id//[[:space:]]/}"
        [[ -z "$process_id" ]] || kill -KILL "$process_id" 2>/dev/null || true
    done < <(ps -u "$(id -u "$BUILD_USER")" -o pid=)
}

terminate_builder_processes
BUILD_HOME="$STAGING_ROOT/build-home-$release_stamp-$$"
install -d -o "$BUILD_USER" -g "$BUILD_USER" -m 0700 -- "$BUILD_HOME"
install -d -o "$BUILD_USER" -g "$BUILD_USER" -m 0700 -- "$CANDIDATE_DIR"
log "Exporting commit $TARGET_COMMIT"
git -C "$REPOSITORY_DIR" archive --format=tar "$TARGET_COMMIT" | \
    tar -xf - -C "$CANDIDATE_DIR"
# Stage privileged helpers directly from the root-owned Git object before any
# candidate dependency or test code runs under the build account.
git -C "$REPOSITORY_DIR" show "$TARGET_COMMIT:scripts/deploy.sh" \
    > "$ADMIN_DEPLOY_NEXT" || die "candidate deploy.sh is missing"
git -C "$REPOSITORY_DIR" show "$TARGET_COMMIT:scripts/update.sh" \
    > "$ADMIN_UPDATE_NEXT" || die "candidate update.sh is missing"
chmod 0755 "$ADMIN_DEPLOY_NEXT" "$ADMIN_UPDATE_NEXT"
chown --recursive --no-dereference "$BUILD_USER:$BUILD_USER" "$CANDIDATE_DIR"

run_as_builder() {
    runuser --user "$BUILD_USER" -- env -i \
        HOME="$BUILD_HOME" \
        PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
        LANG="C.UTF-8" LC_ALL="C.UTF-8" \
        PIP_NO_CACHE_DIR=1 PIP_CONFIG_FILE=/dev/null PYTHONNOUSERSITE=1 \
        PYTHONDONTWRITEBYTECODE=1 "$@"
}

create_venv() {
    install -d -o "$BUILD_USER" -g "$BUILD_USER" -m 0700 -- "$VENV_DIR" || return 1
    run_as_builder "$PYTHON_BIN" -m venv --clear "$VENV_DIR"
}

if ! create_venv; then
    warn "Python venv creation failed; attempting to install the venv package"
    install_packages
    create_venv || die "could not create Python virtual environment"
fi

log "Installing Python dependencies in an isolated virtual environment"
run_as_builder "$VENV_DIR/bin/python" -m pip install --disable-pip-version-check --upgrade pip
run_as_builder "$VENV_DIR/bin/python" -m pip install --disable-pip-version-check \
    --requirement "$CANDIDATE_DIR/requirements.txt"

log "Running compile and import checks"
(
    cd -- "$CANDIDATE_DIR"
    run_as_builder "$VENV_DIR/bin/python" -m compileall -q server.py ykt_core.py email_notifier.py
    run_as_builder "$VENV_DIR/bin/python" -c 'import server; app = server.make_app(); assert app is not None'
)

if (( RUN_TESTS )); then
    log "Running offline unit tests"
    (
        cd -- "$CANDIDATE_DIR"
        run_as_builder "$VENV_DIR/bin/python" -m unittest -q \
            test_ykt_web.py test_email_notifier.py test_deploy_script.py
    )
else
    warn "Unit tests were skipped by request"
fi
terminate_builder_processes

# Freeze dependencies after unprivileged validation; the runtime can only read them.
chown --recursive --no-dereference "root:$SERVICE_USER" "$VENV_DIR"
chmod -R u=rwX,g=rX,o= "$VENV_DIR"

copy_state_file_atomic() {
    local source_file="$1"
    local destination_file="$2"
    local temp_file="$(dirname -- "$destination_file")/.${destination_file##*/}.state.$$"
    rm -f -- "$temp_file" || return 1
    cp -a -- "$source_file" "$temp_file" || {
        rm -f -- "$temp_file" 2>/dev/null || true
        return 1
    }
    chmod 0600 "$temp_file" || { rm -f -- "$temp_file" 2>/dev/null || true; return 1; }
    sync -f "$temp_file" || { rm -f -- "$temp_file" 2>/dev/null || true; return 1; }
    mv -f -- "$temp_file" "$destination_file" || { rm -f -- "$temp_file" 2>/dev/null || true; return 1; }
}

copy_persistent_state() {
    local source_dir="$1"
    local destination_dir="$2"
    local state_file
    [[ -d "$source_dir" && ! -L "$source_dir" ]] || return 0
    for state_file in config.json session.json; do
        if [[ -L "$source_dir/$state_file" ]]; then
            warn "refusing symbolic link in persistent state: $source_dir/$state_file"
            return 1
        fi
        if [[ -f "$source_dir/$state_file" ]]; then
            copy_state_file_atomic \
                "$source_dir/$state_file" "$destination_dir/$state_file" || return 1
        fi
    done
    if [[ -L "$source_dir/logs" ]]; then
        warn "refusing symbolic link in persistent state: $source_dir/logs"
        return 1
    fi
    if [[ -d "$source_dir/logs" ]]; then
        install -d -m 0700 -- "$destination_dir/logs" || return 1
        cp -a -- "$source_dir/logs/." "$destination_dir/logs/" || return 1
    fi
}

state_source=""
if [[ -d "$APP_DIR" && ! -L "$APP_DIR" ]]; then
    state_source="$APP_DIR"
elif [[ "$SOURCE_ROOT" != "$INSTALL_DIR" && -d "$SOURCE_ROOT" ]]; then
    if [[ -f "$SOURCE_ROOT/config.json" || -f "$SOURCE_ROOT/session.json" || -d "$SOURCE_ROOT/logs" ]]; then
        state_source="$SOURCE_ROOT"
        log "Importing existing local configuration and session state from $SOURCE_ROOT"
    fi
fi

if systemctl is-active --quiet "$SERVICE_NAME"; then
    service_was_active=1
fi
if [[ -d "$APP_DIR" ]]; then
    log "Stopping $SERVICE_NAME for the final state copy"
    systemctl stop "$SERVICE_NAME" || die "could not stop $SERVICE_NAME"
    service_stopped=1
fi

if [[ -n "$state_source" ]]; then
    if ! copy_persistent_state "$state_source" "$CANDIDATE_DIR"; then
        die "persistent state could not be copied; the existing release was not changed"
    fi
fi
install -d -m 0700 -- "$CANDIDATE_DIR/logs"

printf '%s\n' "$TARGET_COMMIT" > "$CANDIDATE_DIR/.release"
chown --recursive --no-dereference "$SERVICE_USER:$SERVICE_USER" "$CANDIDATE_DIR"
chmod 0700 "$CANDIDATE_DIR"
[[ ! -f "$CANDIDATE_DIR/config.json" ]] || chmod 0600 "$CANDIDATE_DIR/config.json"
[[ ! -f "$CANDIDATE_DIR/session.json" ]] || chmod 0600 "$CANDIDATE_DIR/session.json"
chmod 0700 "$CANDIDATE_DIR/logs"
ln -s -- "$VENV_DIR" "$CANDIDATE_DIR/.venv"

if [[ -e "$APP_DIR" ]]; then
    [[ -d "$APP_DIR" && ! -L "$APP_DIR" ]] || {
        die "$APP_DIR is not a regular directory"
    }
    backup_dir="$BACKUP_ROOT/release-$release_stamp-$$"
    mv -- "$APP_DIR" "$backup_dir"
fi

if ! mv -- "$CANDIDATE_DIR" "$APP_DIR"; then
    [[ -z "$backup_dir" ]] || mv -- "$backup_dir" "$APP_DIR"
    die "could not activate the candidate release"
fi
candidate_live=0
candidate_activated=1

prune_release_directories() {
    local root="$1"
    local keep="$2"
    local index
    local -a directories=()
    mapfile -t directories < <(
        find "$root" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' \
            | sort -nr | cut -d' ' -f2-
    )
    for ((index = keep; index < ${#directories[@]}; index += 1)); do
        rm -rf -- "${directories[$index]}" || return 1
    done
}

venv_is_referenced() {
    local candidate="$1"
    local release target
    local -a releases=("$APP_DIR")
    while IFS= read -r release; do releases+=("$release"); done < <(
        find "$BACKUP_ROOT" "$FAILED_ROOT" -mindepth 1 -maxdepth 1 -type d -print
    )
    for release in "${releases[@]}"; do
        [[ -L "$release/.venv" ]] || continue
        target="$(readlink -f -- "$release/.venv" 2>/dev/null || true)"
        [[ "$target" != "$candidate" ]] || return 0
    done
    return 1
}

prune_unused_venvs() {
    local old_venv
    while IFS= read -r old_venv; do
        if ! venv_is_referenced "$old_venv"; then
            rm -rf -- "$old_venv" || return 1
        fi
    done < <(find "$VENV_ROOT" -mindepth 1 -maxdepth 1 -type d -print)
}

rollback_release() {
    local reason="$1"
    warn "$reason"
    systemctl stop "$SERVICE_NAME" >/dev/null 2>&1 || true
    failed_dir="$FAILED_ROOT/release-$release_stamp-$SHORT_COMMIT-$$"
    if [[ -d "$APP_DIR" ]]; then
        mv -- "$APP_DIR" "$failed_dir" || true
    fi
    if [[ -n "$backup_dir" && -d "$backup_dir" ]]; then
        mv -- "$backup_dir" "$APP_DIR"
        restore_service_env || warn "could not restore the previous service environment"
        restore_systemd_unit || warn "could not restore the previous systemd unit"
        restore_admin_metadata || warn "could not restore previous update metadata"
        systemctl start "$SERVICE_NAME" || true
        if health_check 20; then
            warn "Previous release was restored successfully"
        else
            warn "Previous release was restored but is not healthy; inspect the journal"
        fi
    fi
    if [[ -z "$backup_dir" ]]; then
        restore_service_env || warn "could not remove the failed service environment"
        restore_systemd_unit || warn "could not remove the failed systemd unit"
        restore_admin_metadata || warn "could not restore previous update metadata"
    fi
    if (( CADDY_TRANSACTION_ACTIVE )); then
        restore_https_proxy || warn "could not restore the previous Caddy configuration"
    fi
    prune_release_directories "$FAILED_ROOT" 1 || warn "could not prune old failed releases"
    prune_unused_venvs || warn "could not prune unused environments"
    candidate_activated=0
    service_stopped=0
    journalctl -u "$SERVICE_NAME" -n 40 --no-pager >&2 || true
    die "deployment failed; failed candidate retained at $failed_dir"
}

activate_systemd_unit || rollback_release "could not install the systemd unit"
activate_service_env || rollback_release "could not install the protected service environment file"
systemctl enable "$SERVICE_NAME" >/dev/null || rollback_release "could not enable the systemd service"
systemctl restart "$SERVICE_NAME" || rollback_release "systemd could not start the candidate release"
health_check 30 || rollback_release "candidate release did not pass the HTTP health check"
activate_https_proxy || rollback_release "automatic HTTPS setup failed"

if [[ "$LOCAL_COMMIT" != "$TARGET_COMMIT" ]]; then
    if ! git -C "$REPOSITORY_DIR" merge --ff-only "$TARGET_COMMIT"; then
        rollback_release "candidate was healthy, but the deployment repository could not fast-forward"
    fi
fi

backup_admin_metadata || rollback_release "could not back up update metadata"
mv -f -- "$ADMIN_DEPLOY_NEXT" "$ADMIN_DIR/deploy.sh" || \
    rollback_release "could not install the managed deploy command"
mv -f -- "$ADMIN_UPDATE_NEXT" "$ADMIN_DIR/update.sh" || \
    rollback_release "could not install the managed update command"
chmod 0755 "$ADMIN_DIR/deploy.sh" "$ADMIN_DIR/update.sh" || \
    rollback_release "could not secure the managed update commands"
write_deploy_config || rollback_release "could not write deployment metadata"
printf '%s\n' "$TARGET_COMMIT" > "$ADMIN_DIR/.current_commit.$$" || \
    rollback_release "could not stage installed-version metadata"
chmod 0600 "$ADMIN_DIR/.current_commit.$$" || \
    rollback_release "could not secure installed-version metadata"
mv -f -- "$ADMIN_DIR/.current_commit.$$" "$CURRENT_COMMIT_FILE" || \
    rollback_release "could not commit installed-version metadata"
service_stopped=0
candidate_activated=0
finalize_service_env || warn "could not remove the service-environment rollback copy"
finalize_systemd_unit || warn "could not remove the systemd rollback copy"
finalize_admin_metadata || warn "could not remove the update-metadata rollback copy"
finalize_https_proxy || warn "could not remove the Caddy rollback copy"

prune_release_directories "$BACKUP_ROOT" 1 || warn "could not prune old rollback release"
prune_release_directories "$FAILED_ROOT" 1 || warn "could not prune old failed release"
prune_unused_venvs || warn "could not prune unused environments"

log "Deployed commit $SHORT_COMMIT successfully"
print_access_details
log "Future updates: sudo $ADMIN_DIR/update.sh"
[[ -z "$backup_dir" ]] || log "Rollback copy retained at $backup_dir"
