#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

BASE_URL="${DROPPR_BASE_URL:-http://localhost:${DROPPR_HOST_PORT:-8098}}"
ADMIN_USER="${DROPPR_ADMIN_USER:-admin}"
ADMIN_PASS="${DROPPR_ADMIN_PASS:-}"
USER_ROOT="${DROPPR_USER_ROOT:-/users}"
DATA_DIR="${DROPPR_USER_DATA_DIR:-$ROOT_DIR/data}"
MOVE_FILES="${DROPPR_MOVE_USER_FILES:-1}"

if [[ $# -ne 1 ]]; then
  echo "usage: $(basename "$0") <username>" >&2
  exit 1
fi

if [[ -z "${ADMIN_PASS}" ]]; then
  if [[ -f "$ROOT_DIR/config/admin-password.txt" ]]; then
    ADMIN_PASS="$(<"$ROOT_DIR/config/admin-password.txt")"
  else
    echo "Missing admin password. Set DROPPR_ADMIN_PASS or create config/admin-password.txt." >&2
    exit 1
  fi
fi

username="$1"

token="$(curl -fsS -X POST "$BASE_URL/api/login" \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASS\"}")"

users_json="$(curl -fsS -H "X-Auth: $token" "$BASE_URL/api/users")"
user_json="$(echo "$users_json" | jq -c --arg u "$username" '.[] | select(.username==$u)')"

if [[ -z "$user_json" ]]; then
  echo "User not found: $username" >&2
  exit 1
fi

if [[ "$(echo "$user_json" | jq -r '.perm.admin')" == "true" ]]; then
  echo "Refusing to change admin user scope." >&2
  exit 1
fi

if [[ "$USER_ROOT" == "/" ]]; then
  desired_scope="/$username"
  user_root_fs="$DATA_DIR"
else
  user_root_fs="${DATA_DIR%/}${USER_ROOT%/}"
  desired_scope="${USER_ROOT%/}/$username"
fi

current_scope="$(echo "$user_json" | jq -r '.scope')"
if [[ "$current_scope" == "$desired_scope" ]]; then
  echo "Scope already set: $current_scope"
else
  updated_user="$(echo "$user_json" | jq --arg scope "$desired_scope" '.scope=$scope')"
  payload="$(jq -n --argjson data "$updated_user" '{what:"user", which:["scope"], data:$data}')"
  curl -fsS -X PUT "$BASE_URL/api/users/$(echo "$user_json" | jq -r '.id')" \
    -H "X-Auth: $token" \
    -H "Content-Type: application/json" \
    -d "$payload" >/dev/null
  echo "Updated scope: $current_scope -> $desired_scope"
fi

dest_dir="$user_root_fs/$username"
if [[ ! -d "$dest_dir" ]]; then
  mkdir -p "$dest_dir"
  echo "Ensured directory: $dest_dir"
fi

if [[ "$MOVE_FILES" == "1" ]]; then
  src_dir="$DATA_DIR/$username"
  if [[ "$src_dir" != "$dest_dir" && -d "$src_dir" ]]; then
    if [[ -z "$(ls -A "$dest_dir")" ]]; then
      mv "$src_dir" "$dest_dir"
      echo "Moved files: $src_dir -> $dest_dir"
    else
      echo "Skip move; destination not empty: $dest_dir" >&2
    fi
  fi
fi
