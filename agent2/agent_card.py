"""The public AgentCard. It states the auth requirement. The middleware enforces it."""
from __future__ import annotations

import os

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    AuthorizationCodeOAuthFlow,
    OAuth2SecurityScheme,
    OAuthFlows,
    SecurityScheme,
)

ISSUER = os.environ["KEYCLOAK_ISSUER"]
BASE_URL = os.environ["AGENT2_BASE_URL"]
REQUIRED_SCOPE = os.environ.get("REQUIRED_SCOPE", "github.act")

AUTH_URL = f"{ISSUER}/protocol/openid-connect/auth"
TOKEN_URL = f"{ISSUER}/protocol/openid-connect/token"


def build_card() -> AgentCard:
    scheme = SecurityScheme(
        root=OAuth2SecurityScheme(
            description="Keycloak token with the github.act scope and aud=agent2-github-agent.",
            flows=OAuthFlows(
                authorization_code=AuthorizationCodeOAuthFlow(
                    authorization_url=AUTH_URL,
                    token_url=TOKEN_URL,
                    scopes={REQUIRED_SCOPE: "Call agent2 on behalf of the signed-in user."},
                )
            ),
        )
    )
    skill = AgentSkill(
        id="github",
        name="GitHub read",
        description="List repos and issues for the signed-in user via the GitHub MCP server.",
        tags=["github", "mcp"],
        examples=["list my repos", "show my open issues"],
    )
    return AgentCard(
        name="GitHub agent (agent2)",
        description="Calls the GitHub MCP server with the end user's own token.",
        url=f"{BASE_URL}/a2a",
        version="0.1.0",
        preferred_transport="JSONRPC",
        protocol_version="0.3.0",
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[skill],
        security_schemes={"keycloak_oauth": scheme},
        security=[{"keycloak_oauth": [REQUIRED_SCOPE]}],
    )
