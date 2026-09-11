# A2A Auth Demo

See how one end-user identity travels through three legs:

1. **H2A** - a person logs in and talks to agent1 (the orchestrator).
2. **A2A** - agent1 calls agent2 (the GitHub agent) on behalf of that person.
3. **MCP** - agent2 calls an MCP server with that person's own GitHub token.

Production would put Okta plus TrueFoundry in front of this. The demo removes
that gateway so you see the raw protocol. Everything runs in local Docker
containers. No cloud, no Terraform, no AWS.

## Run it

```bash
cd a2a-auth-demo
cp .env.example .env
docker compose up --build
```

Four containers start: `keycloak`, `agent1`, `agent2`, `mockmcp`.
Wait until Keycloak logs `Running the server`.

Then:

1. Open http://localhost:9001
2. Click **Login as alice**. Sign in as `alice` / `alice`. The button becomes
   **Logout (alice)** once you are in.
3. Type `list my repos` and click **Ask agent1**.
4. The first ask shows a **GitHub consent** link (agent2 has no GitHub token for
   alice yet). Open the link, approve, close the tab.
5. Ask again. The **token trace** panel keeps a row for every step so far -
   login, then each ask:
   - row 1: the user token (H2A)
   - row 2: the OBO token (A2A) with `aud=agent2-github-agent` and an actor
     naming agent1
   - row 3: the GitHub token used at the MCP server
   Click **Clear token trace** to empty the panel and start over.
6. **Agent cards**: the two links under "Break it" open agent1's and agent2's
   AgentCard JSON in a new tab. Agent2's is a real A2A card (`securitySchemes`,
   `skills`). Agent1's is a plain JSON doc for comparison - agent1 is not an
   A2A server in this demo, its `/ask` endpoint is plain REST.

## Headless check

```bash
bash scripts/smoke.sh
```

It runs the whole chain with curl and exits non-zero on any bad hop.
It uses the password grant as a shortcut for the browser login.

Decode any token by hand:

```bash
python3 scripts/decode.py <token>
```

## What to look at

| file | what it teaches |
|------|-----------------|
| `keycloak/realm-export.json` | the clients, scopes and mappers, no console clicking |
| `agent1/exchange.py` | the RFC 8693 token exchange call |
| `agent2/auth.py` | the middleware that checks caller and reads the user identity |
| `agent2/agent_card.py` | the card states the auth need; the middleware enforces it |
| `agent2/github_oauth.py` | the signed ticket that carries identity into the browser leg |
| `agent2/mcp_client.py` | one MCP code path: 401 -> discovery -> PKCE -> token -> tool call |

## Two questions, kept apart

**Is this really agent1?** Three layers, all needed:

1. Keycloak only mints the OBO token for a confidential client that holds
   `AGENT1_SECRET`.
2. The token `aud` is `agent2-github-agent`, so a raw user token is refused.
3. Agent2's own allowlist on `azp` (`ALLOWED_CALLERS`). Audience alone is not
   enough, because another Keycloak client could get a token with that audience.
   The allowlist is the check that refuses an unknown agent.

**Is this really alice?** Agent2 never logs the user in. It reads `sub` out of a
token that Keycloak signed and that agent2 verifies against Keycloak's JWKS.
Agent1 cannot forge it. Identity and actor travel together in one token, and the
receiver trusts the IdP, not the caller.

## The `act` claim (honest note)

Keycloak's standard token exchange does **not** put an `act` claim on the new
token by itself. In this demo the `act` claim is added by a **hardcoded-claim
mapper** on the `actor.agent1` client scope, which only agent1 has. See
`keycloak/realm-export.json`. The actor is also provable from `azp` (the client
that asked for the token). The middleware accepts either `azp` or `act.sub`
against `ALLOWED_CALLERS`.

## Break it

The web page has five buttons. Each forces one failure:

| button | what it sends | expected |
|--------|---------------|----------|
| wrong aud | token signed by the wrong key, wrong audience | 401 |
| raw user token | the login token, not the OBO token | 401 (aud) |
| expired token | a token with `exp` in the past | 401 |
| no token | no Authorization header | 401 + `WWW-Authenticate` |
| unlisted client | a real Keycloak token with `azp=rogue-agent` | 403 (allowlist) |

## Leg 3: mock or real GitHub

`MCP_MODE=mock` (default) uses `mockmcp`. It copies the real shape: 401 with
`WWW-Authenticate`, discovery documents, authorize plus token with PKCE, and
canned repo and issue data. No network needed.

`MCP_MODE=github` points at `https://api.githubcopilot.com/mcp/`. You must
pre-register a **GitHub App** (tokens expire, unlike an OAuth App) with callback
`http://localhost:9002/github/callback`, then set `GITHUB_APP_CLIENT_ID` and
`GITHUB_APP_CLIENT_SECRET` in `.env`. Agent2 discovers the authorization server
from the `WWW-Authenticate` header, it does not hardcode a GitHub URL. Because
both modes speak the same dance, `mcp_client.py` has one code path. The real
GitHub path is optional proof and is less exercised than the mock path.

## Where this demo simplifies (read this)

- Secrets live in `.env` and the realm export in plain text. Not for production.
- `SESSION_SECRET` and `AGENT2_TICKET_KEY` are dev signing keys.
- The Keycloak realm has `sslRequired: none` and a fixed HTTP issuer
  (`http://localhost:8081`) so tokens read the same from the browser and from
  the containers.
- `agent1-orchestrator` also has the password grant on, only so `smoke.sh` can
  skip the browser. The teaching flow uses authorization code plus PKCE.
- `rogue-agent` exists only to power the "unlisted client" button.
- No push notifications, no LLM, no production secret handling.

## Keycloak field name

The client attribute that turns on standard token exchange is
`standard.token.exchange.enabled: "true"` on `agent1-orchestrator`
(Keycloak 26). Public clients cannot do standard token exchange, so agent1 is
confidential even though it also runs the browser login.
