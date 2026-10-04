"""Rule-based skills. No LLM. Turn the prompt into one MCP tool call."""
from __future__ import annotations

import json

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Part, TaskState, TextPart

from common import events, jwt_verify, trace

import github_oauth
import mcp_client
import store
from agent_card import AUTH_URL  # noqa: F401  (keeps import graph explicit)
from auth import AUDIENCE, ISSUER, JWKS_URL, current_claims


def _pick_tool(text: str) -> tuple[str, dict]:
    """mockmcp speaks a toy tool set (list_repos/list_issues, no args).

    The real GitHub MCP server has neither tool. There is no "list my repos"
    tool at all there - the closest is search_repositories, which requires a
    query string. "user:@me" is GitHub search syntax for the signed-in user's
    own repos. list_issues on the real server requires an owner/repo pair we
    do not have from free text, so search_issues (query-only) is used for
    "my issues" instead.
    """
    t = text.lower()
    wants_issues = "issue" in t

    if mcp_client.MODE == "github":
        if wants_issues:
            return "search_issues", {"query": "is:issue is:open author:@me"}
        return "search_repositories", {"query": "user:@me"}

    if wants_issues:
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


async def _ask_for_consent(updater: TaskUpdater, sub: str, why: str) -> None:
    """End the task as input-required with a fresh consent link."""
    ticket = github_oauth.mint_ticket(sub)
    url = github_oauth.consent_url(ticket)
    await events.forward(sub, events.make(
        "consent.needed", leg="CONSENT", kind="result", src="agent2", dst="agent1",
        note=f"{why} The repository agent answers input-required with a consent link. The link "
             "holds a signed ticket with the user's sub, valid 5 minutes.",
        data={"ticket_url": url, "a2a_state": "input-required"}))
    await updater.update_status(
        TaskState.input_required,
        message=updater.new_agent_message(
            [Part(root=TextPart(text=f"GitHub consent needed. Open this link: {url}"))]
        ),
        final=True,
    )
    trace.add(sub, "MCP: no usable GitHub token", None,
              note=f"{why} The repository agent returned input-required with a signed ticket link.",
              ok=True)


class GitHubAgentExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.submit()
        await updater.start_work()

        claims = _claims_from_context(context)
        sub = claims.sub
        text = context.get_user_input()

        gh = store.get_token(sub)
        await events.forward(sub, events.make(
            "mcp.token.lookup", leg="MCP", kind="check", src="agent2",
            note=("The repository agent looks in its token store for this sub's GitHub "
                  "token. Found it." if gh else
                  "The repository agent looks in its token store for this sub's GitHub token. "
                  "None yet: the user has to give consent first."),
            check={"name": "GitHub token stored", "claim": "store[sub]",
                   "expected": "a GitHub token", "actual": "found" if gh else "none",
                   "ok": bool(gh)}))
        if not gh:
            await _ask_for_consent(updater, sub, "No GitHub token stored yet.")
            return

        tool, args = _pick_tool(text)
        await updater.update_status(
            TaskState.working,
            message=updater.new_agent_message(
                [Part(root=TextPart(text=f"Calling MCP tool {tool} with your GitHub token..."))]
            ),
        )

        await events.forward(sub, events.make(
            "mcp.call", leg="MCP", kind="request", src="agent2", dst="mcp",
            note=f"The repository agent calls the MCP tool {tool} with the user's own GitHub token "
                 "as a Bearer token.",
            token=events.token_view(gh["access_token"], "GitHub token"),
            data={"tool": tool, "arguments": args, "mode": mcp_client.MODE}))

        result = await mcp_client.call_tool(gh["access_token"], tool, args)

        err = result.get("error")
        rejected = isinstance(err, str) and err.startswith("MCP rejected the token")
        await events.forward(sub, events.make(
            "mcp.result", leg="MCP", kind="result", src="mcp", dst="agent2",
            note=("The MCP server checks the GitHub token and runs the tool. Treasure!"
                  if "error" not in result else
                  "The MCP server refused the stored GitHub token (401). It may be revoked or "
                  "expired. The repository agent deletes it and asks the user for consent again."
                  if rejected else f"The MCP server refused: {result['error']}"),
            http={"status": 401 if rejected else (502 if "error" in result else 200), "body": ""},
            data={"ok": "error" not in result, "preview": json.dumps(result)[:800]}))

        if rejected:
            store.delete_token(sub)
            trace.add(sub, "MCP GitHub token (repository agent -> MCP server)", gh["access_token"],
                      note="MCP returned 401. Stored GitHub token deleted.", ok=False)
            await _ask_for_consent(updater, sub, "The old GitHub token no longer works.")
            return

        trace.add(sub, "MCP GitHub token (repository agent -> MCP server)", gh["access_token"],
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
