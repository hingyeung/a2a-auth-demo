#!/usr/bin/env bash
# Delete the GitHub tokens the repository agent has stored.
# The next ask then needs GitHub consent again.
#
# Usage:
#   scripts/clear-github-tokens.sh --list       show who has a stored token
#   scripts/clear-github-tokens.sh <username>   delete one user's token (e.g. alice)
#   scripts/clear-github-tokens.sh --all        delete every stored token
#
# Needs the stack running (docker compose up). Usernames are turned into
# Keycloak subs with the Keycloak admin API, because tokens are stored by sub.
set -euo pipefail

KC="${KC_URL:-http://localhost:8081}"
REALM="a2a-demo"
KC_ADMIN="${KC_ADMIN:-admin}"
KC_ADMIN_PASSWORD="${KC_ADMIN_PASSWORD:-admin}"
cd "$(dirname "$0")/.."

usage() { sed -n '5,8p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
fail() { printf 'FAIL: %s\n' "$1" >&2; exit 1; }

repo_agent_py() { docker compose exec -T repo-agent python -c "$1"; }

admin_token() {
  local body
  body=$(curl -sf "$KC/realms/master/protocol/openid-connect/token" \
    -d grant_type=password -d client_id=admin-cli \
    -d username="$KC_ADMIN" -d password="$KC_ADMIN_PASSWORD") \
    || fail "cannot get a Keycloak admin token from $KC"
  printf '%s' "$body" | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])'
}

[ $# -eq 1 ] || usage 1

case "$1" in
  -h|--help) usage ;;
  --list)
    AT=$(admin_token)
    LIST=$(mktemp)
    repo_agent_py 'import store
for sub, at in store.list_subs(): print(sub, at)' > "$LIST"
    [ -s "$LIST" ] || echo "no GitHub tokens stored"
    while read -r sub at; do
      name=$(curl -sf -H "authorization: Bearer $AT" "$KC/admin/realms/$REALM/users/$sub" \
        | python3 -c 'import sys,json;print(json.load(sys.stdin)["username"])' 2>/dev/null || echo "?")
      printf '%-10s %s  stored %s\n' "$name" "$sub" "$(date -r "$at" '+%Y-%m-%d %H:%M' 2>/dev/null || echo "$at")"
    done < "$LIST"
    rm -f "$LIST"
    ;;
  --all)
    repo_agent_py 'import store; print("deleted", store.delete_all(), "token(s)")'
    ;;
  -*) usage 1 ;;
  *)
    AT=$(admin_token)
    NAME_Q=$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1], safe=""))' "$1")
    USERS=$(curl -sf -H "authorization: Bearer $AT" \
      "$KC/admin/realms/$REALM/users?username=$NAME_Q&exact=true") \
      || fail "Keycloak user lookup failed at $KC"
    SUB=$(printf '%s' "$USERS" | python3 -c 'import sys,json;u=json.load(sys.stdin);print(u[0]["id"] if u else "")')
    [ -n "$SUB" ] || fail "no Keycloak user named $1"
    # Values go in as env vars, never pasted into the Python source.
    docker compose exec -T -e SUB="$SUB" -e NAME="$1" repo-agent python -c 'import os, store
sub, name = os.environ["SUB"], os.environ["NAME"]
had = store.get_token(sub) is not None
store.delete_token(sub)
print(f"deleted the token for {name} ({sub})" if had else f"no token stored for {name} ({sub})")'
    ;;
esac
