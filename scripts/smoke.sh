#!/usr/bin/env bash
# End-to-end check with curl. Exits non-zero on any bad hop.
# Run it after: docker compose up --build
set -euo pipefail

KC="${KC_URL:-http://localhost:8081}"
REALM="a2a-demo"
A1="http://localhost:9001"
A2="http://localhost:9002"
TOKEN_EP="$KC/realms/$REALM/protocol/openid-connect/token"
HERE="$(cd "$(dirname "$0")" && pwd)"

say() { printf '\n=== %s ===\n' "$1"; }
fail() { printf 'FAIL: %s\n' "$1" >&2; exit 1; }

say "1. user logs in (password grant shortcut)"
UT=$(curl -s "$TOKEN_EP" \
  -d grant_type=password -d client_id=agent1-orchestrator \
  -d client_secret=agent1-dev-secret \
  -d username=alice -d password=alice \
  -d scope="openid" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("access_token",""))')
[ -n "$UT" ] || fail "no user token"
python3 "$HERE/decode.py" "$UT"
echo "$UT" | python3 -c 'import sys,json,base64
b=sys.stdin.read().strip().split(".")[1]; b+="="*(-len(b)%4)
c=json.loads(base64.urlsafe_b64decode(b)); aud=c.get("aud")
aud=[aud] if isinstance(aud,str) else (aud or [])
assert "agent2-github-agent" not in aud, "user token must NOT already target agent2"
print("ok: user token aud =", aud)'

say "2. RFC 8693 token exchange -> OBO token for agent2"
OBO=$(curl -s "$TOKEN_EP" \
  -d grant_type=urn:ietf:params:oauth:grant-type:token-exchange \
  -d client_id=agent1-orchestrator -d client_secret=agent1-dev-secret \
  --data-urlencode "subject_token=$UT" \
  -d subject_token_type=urn:ietf:params:oauth:token-type:access_token \
  -d requested_token_type=urn:ietf:params:oauth:token-type:access_token \
  -d audience=agent2-github-agent \
  -d scope="openid agent2-audience actor.agent1 github.act" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("access_token",""))')
[ -n "$OBO" ] || fail "token exchange returned no token"
python3 "$HERE/decode.py" "$OBO"
echo "$OBO" | python3 -c 'import sys,json,base64
b=sys.stdin.read().strip().split(".")[1]; b+="="*(-len(b)%4)
c=json.loads(base64.urlsafe_b64decode(b)); aud=c.get("aud")
aud=[aud] if isinstance(aud,str) else (aud or [])
assert "agent2-github-agent" in aud, "OBO token must target agent2"
assert "github.act" in c.get("scope","").split(), "OBO token must carry github.act"
actor = (c.get("act") or {}).get("sub") or c.get("azp")
assert actor == "agent1-orchestrator", f"actor should name agent1, got {actor}"
print("ok: OBO aud =", aud, "actor =", actor)'

say "3. agent2 AgentCard is public and names the Keycloak scheme"
curl -sf "$A2/.well-known/agent-card.json" | python3 -c 'import sys,json
c=json.load(sys.stdin); s=c.get("securitySchemes") or {}
assert "keycloak_oauth" in s, "card is missing the keycloak scheme"
print("ok: securitySchemes =", list(s))'

say "4. agent2 /a2a with no token -> 401 + WWW-Authenticate"
code=$(curl -s -o /tmp/smoke_body -w '%{http_code}' -D /tmp/smoke_hdr -X POST "$A2/a2a" -d '{}')
[ "$code" = "401" ] || fail "no-token call should be 401, got $code"
grep -qi '^www-authenticate:' /tmp/smoke_hdr || fail "missing WWW-Authenticate header"
echo "ok: 401 with $(grep -i '^www-authenticate:' /tmp/smoke_hdr)"

say "5. agent2 /a2a with the OBO token -> A2A task runs"
REQ='{"jsonrpc":"2.0","id":"smoke","method":"message/send","params":{"message":{"messageId":"m1","role":"user","parts":[{"kind":"text","text":"list my repos"}]}}}'
resp=$(curl -s -X POST "$A2/a2a" -H "authorization: Bearer $OBO" -H 'content-type: application/json' -d "$REQ")
echo "$resp"
echo "$resp" | python3 -c 'import sys,json
r=json.load(sys.stdin)
assert "result" in r, f"expected a result, got {r}"
state=(r["result"].get("status") or {}).get("state")
assert state in ("input-required","working","completed"), f"unexpected state {state}"
print("ok: task state =", state, "(input-required is expected with no stored GitHub token)")'

say "6. agent2 /a2a as an unlisted client -> 403"
ROGUE=$(curl -s "$TOKEN_EP" \
  -d grant_type=client_credentials -d client_id=rogue-agent \
  -d client_secret=rogue-dev-secret -d scope="github.act agent2-audience" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("access_token",""))')
[ -n "$ROGUE" ] || fail "no rogue token"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$A2/a2a" -H "authorization: Bearer $ROGUE" -H 'content-type: application/json' -d "$REQ")
[ "$code" = "403" ] || fail "rogue client should be 403, got $code"
echo "ok: rogue client -> 403"

say "7. bob (no github-caller role) -> OBO token with no github.act -> 403"
BOB_UT=$(curl -s "$TOKEN_EP" \
  -d grant_type=password -d client_id=agent1-orchestrator \
  -d client_secret=agent1-dev-secret \
  -d username=bob -d password=bob \
  -d scope="openid" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("access_token",""))')
[ -n "$BOB_UT" ] || fail "no bob user token"
# Mirrors exactly what agent1 does for a user without the github-caller
# role: agent2-audience and actor.agent1 are still requested (every user
# gets a token that is FOR agent2), github.act is deliberately left out.
BOB_OBO=$(curl -s "$TOKEN_EP" \
  -d grant_type=urn:ietf:params:oauth:grant-type:token-exchange \
  -d client_id=agent1-orchestrator -d client_secret=agent1-dev-secret \
  --data-urlencode "subject_token=$BOB_UT" \
  -d subject_token_type=urn:ietf:params:oauth:token-type:access_token \
  -d requested_token_type=urn:ietf:params:oauth:token-type:access_token \
  -d audience=agent2-github-agent \
  -d scope="openid agent2-audience actor.agent1" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("access_token",""))')
[ -n "$BOB_OBO" ] || fail "no bob OBO token"
python3 "$HERE/decode.py" "$BOB_OBO"
echo "$BOB_OBO" | python3 -c 'import sys,json,base64
b=sys.stdin.read().strip().split(".")[1]; b+="="*(-len(b)%4)
c=json.loads(base64.urlsafe_b64decode(b)); aud=c.get("aud")
aud=[aud] if isinstance(aud,str) else (aud or [])
assert "agent2-github-agent" in aud, "bob OBO token must still target agent2"
assert "github.act" not in c.get("scope","").split(), "bob must NOT have github.act"
print("ok: bob OBO aud =", aud, "scope =", c.get("scope"))'
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$A2/a2a" -H "authorization: Bearer $BOB_OBO" -H 'content-type: application/json' -d "$REQ")
[ "$code" = "403" ] || fail "bob should be 403 (missing github.act), got $code"
echo "ok: bob -> 403 missing scope github.act"

say "ALL HOPS PASSED"
