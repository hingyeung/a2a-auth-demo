"""Rule-based skills. No LLM. Turn the prompt into one MCP tool call."""
from __future__ import annotations

import json

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Part, TaskState, TextPart

from common import jwt_verify, trace

import github_oauth
import mcp_client
import store
from agent_card import AUTH_URL  # noqa: F401  (keeps import graph explicit)
from auth import AUDIENCE, ISSUER, JWKS_URL, current_claims


def _pick_tool(text: str) -> tuple[str, dict]:
    t = text.lower()
    if "issue" in t:
        return "list_issues", {}
    return "list_repos", {}


def _claims_from_context(context: RequestContext):
    claims = current_claims.get()
    if claims is not None:
        return claims
    # Fallback: re-verify the bearer token from the call context headers.
    headers = {}
    if context.call_context and context.call_context.state:
        headers = context.call_context.state.get("headers", {})
    auth = headers.get("authorization", "")
    token = auth.split(None, 1)[1] if auth.lower().startswith("bearer ") else ""
    return jwt_verify.verify(token, audience=AUDIENCE, issuer=ISSUER, jwks_url=JWKS_URL)


class GitHubAgentExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.submit()
        await updater.start_work()

        claims = _claims_from_context(context)
        sub = claims.sub
        text = context.get_user_input()

        gh = store.get_token(sub)
        if not gh:
            ticket = github_oauth.mint_ticket(sub)
            url = github_oauth.consent_url(ticket)
            await updater.update_status(
                TaskState.input_required,
                message=updater.new_agent_message(
                    [Part(root=TextPart(text=f"GitHub consent needed. Open this link: {url}"))]
                ),
                final=True,
            )
            trace.add(sub, "3. MCP: no GitHub token yet", None,
                      note="agent2 returned input-required with a signed ticket link.", ok=True)
            return

        tool, args = _pick_tool(text)
        await updater.update_status(
            TaskState.working,
            message=updater.new_agent_message(
                [Part(root=TextPart(text=f"Calling MCP tool {tool} with your GitHub token..."))]
            ),
        )

        result = await mcp_client.call_tool(gh["access_token"], tool, args)

        trace.add(sub, "3. MCP GitHub token (agent2 -> MCP server)", gh["access_token"],
                  note=(f"user sub={sub} | tool={tool} | mode={mcp_client.MODE} | "
                        "GitHub tokens are opaque, not JWTs"),
                  ok="error" not in result)

        await updater.update_status(
            TaskState.working,
            message=updater.new_agent_message(
                [Part(root=TextPart(text="MCP returned. Formatting the answer."))]
            ),
        )

        pretty = json.dumps(result, indent=2)[:2000]
        await updater.add_artifact(
            [Part(root=TextPart(text=pretty))], name="mcp-result"
        )
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("cancel is not supported in this demo")
