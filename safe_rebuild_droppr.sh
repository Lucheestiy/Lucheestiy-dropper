#!/usr/bin/env bash
# ==============================================================================
# Safe Rebuild Droppr (SRDROPPR)
# - Stops the Droppr stack, rebuilds images, and restarts the stack
#
# Usage: srdroppr [--clean] [--dry-run] [-h|--help]
#   --clean     : Build with --no-cache
#   --dry-run   : Print actions without executing them
# ==============================================================================
set -euo pipefail

INVOKED_CMD="$(basename "${0:-sedrppr}")"
DISPLAY_CMD="$INVOKED_CMD"
if [[ "$DISPLAY_CMD" == "safe_rebuild_droppr.sh" ]]; then
  DISPLAY_CMD="sedrppr"
fi

usage() {
  cat <<EOF
Safe Rebuild Droppr (${DISPLAY_CMD})

Usage:
  ${DISPLAY_CMD} [--clean] [--dry-run] [-h|--help]

Notes:
  - 
    sedrppr" and 
    srdroppr" are equivalent wrappers for this script.

Options:
  --clean       Build with --no-cache.
  --dry-run     Print actions without executing.
  -h, --help    Show this help and exit.
EOF
}

CLEAN=false
DRY_RUN=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --clean)
      CLEAN=true
      shift
      ;;
    --dry-run)
      DRY_RUN=true
      shift
      ;; 
    -h|--help)
      usage
      exit 0
      ;; 
    *)
      echo "[SRDROPPR] Error: Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;; 
  esac
done

# Resolve script directory (follow symlinks) so /usr/local/bin/srdroppr works.
SOURCE="${BASH_SOURCE[0]}"
while [ -h "$SOURCE" ]; do
  DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  [[ "$SOURCE" != /* ]] && SOURCE="$DIR/$SOURCE"
done
SCRIPT_DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
cd "$SCRIPT_DIR"

COMPOSE_FILE="docker-compose.yml"
NGINX_CONTAINER="droppr"
BUILD_FLAGS=()

if $CLEAN; then
  echo "[SRDROPPR] Clean mode enabled: Disabling build cache."
  BUILD_FLAGS+=(--no-cache)
fi

if [ ! -f "$COMPOSE_FILE" ]; then
  echo "[SRDROPPR] Error: $COMPOSE_FILE not found in $SCRIPT_DIR" >&2
  exit 1
fi

if $DRY_RUN; then
  echo "[SRDROPPR] DRY RUN: Would run the following steps in $SCRIPT_DIR:"
  echo "  - docker compose -f $COMPOSE_FILE config"
  echo "  - docker compose -f $COMPOSE_FILE build ${BUILD_FLAGS[*]:-(no extra flags)}"
  echo "  - docker compose -f $COMPOSE_FILE pull $NGINX_CONTAINER"
  echo "  - docker compose -f $COMPOSE_FILE down --remove-orphans"
  echo "  - docker compose -f $COMPOSE_FILE up -d"
  echo "  - wait for container: $NGINX_CONTAINER"
  echo "  - docker compose -f $COMPOSE_FILE ps"
  exit 0
fi

echo "[SRDROPPR] Validating compose file..."
docker compose -f "$COMPOSE_FILE" config >/dev/null

if [[ ${#BUILD_FLAGS[@]} -gt 0 ]]; then
  echo "[SRDROPPR] Building Droppr images (Flags: ${BUILD_FLAGS[*]})..."
else
  echo "[SRDROPPR] Building Droppr images (Flags: None)..."
fi
docker compose -f "$COMPOSE_FILE" build "${BUILD_FLAGS[@]}"

echo "[SRDROPPR] Pulling upstream images (best-effort)..."
docker compose -f "$COMPOSE_FILE" pull "$NGINX_CONTAINER" || true

echo "[SRDROPPR] Bringing down Droppr stack (remove orphans)..."
docker compose -f "$COMPOSE_FILE" down --remove-orphans || true

echo "[SRDROPPR] Starting Droppr stack..."
docker compose -f "$COMPOSE_FILE" up -d

echo "[SRDROPPR] Waiting for Nginx container ($NGINX_CONTAINER) to be Up..."
for i in {1..60}; do
  if docker ps --format '{{.Names}} {{.Status}}' | grep -q "^$NGINX_CONTAINER .*Up"; then
    echo "[SRDROPPR] Nginx container is Up."
    break
  fi
  sleep 1
  if [ "$i" -eq 60 ]; then
    echo "[SRDROPPR] Warning: Nginx container did not report Up within 60s" >&2
  fi
done

if command -v curl >/dev/null 2>&1; then
  HOST_PORT="$(docker port "$NGINX_CONTAINER" 80/tcp 2>/dev/null | awk -F: 'NR==1{print $NF}' | tr -d '\r' || true)"
  HOST_PORT="${HOST_PORT:-8098}"
  echo "[SRDROPPR] Checking HTTP (http://localhost:${HOST_PORT}/)..."
  for i in {1..30}; do
    if curl -fsS "http://localhost:${HOST_PORT}/" >/dev/null; then
      echo "[SRDROPPR] HTTP check OK."
      break
    fi
    sleep 1
    if [ "$i" -eq 30 ]; then
      echo "[SRDROPPR] Warning: HTTP check failed at http://localhost:${HOST_PORT}/" >&2
    fi
  done
fi

echo "[SRDROPPR] Current Droppr containers:"
docker compose -f "$COMPOSE_FILE" ps

echo "[SRDROPPR] Safe rebuild (droppr) completed."