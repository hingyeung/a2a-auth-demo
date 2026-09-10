"""A2A client. Resolve agent2's card, then stream one message with the OBO token."""
from __future__ import annotations

import os
import uuid

import httpx
from a2a.client import ClientConfig, ClientFactory
from a2a.client.auth.credentials import CredentialService
from a2a.client.auth.interceptor import AuthInterceptor
from a2a.client.card_resolver import A2ACardResolver
from a2a.types import Message, Part, Role, TaskState, TextPart, TransportProtocol

AGENT2 = os.environ["AGENT2_INTERNAL_URL"]


class StaticCredentialService(CredentialService):
    """Hands the same Bearer token to every security scheme the card names."""

    def __init__(self, token: str):
        self._token = token

    async def get_credentials(self, security_scheme_name, context=None):  # noqa: D102
        return self._token


def _user_message(text: str) -> Message:
    return Message(
        message_id=uuid.uuid4().hex,
        role=Role.user,
        parts=[Part(root=TextPart(text=text))],
    )


def _text_of(obj) -> str:
    parts = getattr(obj, "parts", None) or []
    out = []
    for p in parts:
        root = getattr(p, "root", p)
        t = getattr(root, "text", None)
        if t:
            out.append(t)
    return " ".join(out)


async def ask_agent2(obo: str, prompt: str) -> dict:
    """Return dict: states list, final_text, input_required flag, ticket_url."""
    async with httpx.AsyncClient(timeout=30) as hc:
        card = await A2ACardResolver(hc, AGENT2).get_agent_card()
        # The public card names the browser-facing URL. Inside the Docker network
        # agent1 must reach agent2 by its service name.
        card.url = f"{AGENT2}/a2a"
        cfg = ClientConfig(
            httpx_client=hc,
            streaming=True,
            supported_transports=[TransportProtocol.jsonrpc],
        )
        client = ClientFactory(cfg).create(
            card,
            interceptors=[AuthInterceptor(StaticCredentialService(obo))],
        )

        states: list[dict] = []
        final_text = ""
        ticket_url = None
        input_required = False

        async for event in client.send_message(_user_message(prompt)):
            if isinstance(event, Message):
                final_text = _text_of(event)
                continue
            task, update = event
            state = task.status.state.value if task.status else "unknown"
            msg_text = ""
            if task.status and task.status.message:
                msg_text = _text_of(task.status.message)
            states.append({"state": state, "note": msg_text})
            if task.status and task.status.state == TaskState.input_required:
                input_required = True
                if "ticket=" in msg_text:
                    ticket_url = msg_text.split()[-1].strip()
            if task.artifacts:
                for art in task.artifacts:
                    final_text = _text_of(art) or final_text

        return {
            "states": states,
            "final_text": final_text,
            "input_required": input_required,
            "ticket_url": ticket_url,
        }


async def raw_a2a_call(token: str | None) -> dict:
    """Used by the break-it buttons. Bypass the SDK. Report the raw HTTP result."""
    body = {
        "jsonrpc": "2.0",
        "id": "break-it",
        "method": "message/send",
        "params": {
            "message": {
                "messageId": uuid.uuid4().hex,
                "role": "user",
                "parts": [{"kind": "text", "text": "list my repos"}],
            }
        },
    }
    headers = {"content-type": "application/json"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(timeout=15) as hc:
        r = await hc.post(f"{AGENT2}/a2a", json=body, headers=headers)
    return {
        "status": r.status_code,
        "www_authenticate": r.headers.get("www-authenticate"),
        "body": r.text[:400],
    }
