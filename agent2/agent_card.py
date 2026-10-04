"""The repository agent's public AgentCard. It states the auth requirement.
The middleware enforces it."""
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
                    scopes={REQUIRED_SCOPE: "Call the repository agent on behalf of the signed-in user."},
                )
            ),
        )
    )
    skill = AgentSkill(
        id="github",
        name="Read your GitHub repos",
        description="Uses an MCP tool to read the signed-in user's GitHub repos (and "
        "open issues), with that user's own GitHub token.",
        tags=["github", "mcp"],
        examples=["list my repos", "show my open issues"],
    )
    return AgentCard(
        name="Repository agent",
        description="Reads the user's GitHub repos: it calls a GitHub MCP tool with the "
        "end user's own GitHub token. The orchestrator agent calls it over A2A on "
        "that user's behalf.",
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
