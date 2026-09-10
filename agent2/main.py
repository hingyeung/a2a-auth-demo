"""Agent 2: the GitHub agent. A2A server plus the GitHub consent routes."""
from __future__ import annotations

from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from starlette.responses import JSONResponse

import github_oauth
from agent_card import build_card
from auth import OBOAuthMiddleware
from common import trace
from executor import GitHubAgentExecutor

RPC_URL = "/a2a"

_handler = DefaultRequestHandler(
    agent_executor=GitHubAgentExecutor(),
    task_store=InMemoryTaskStore(),
)

_a2a = A2AStarletteApplication(agent_card=build_card(), http_handler=_handler)
app = _a2a.build(rpc_url=RPC_URL)


async def health(_request):
    return JSONResponse({"ok": True})


async def debug_trace(request):
    sub = request.query_params.get("sub", "")
    return JSONResponse({"rows": trace.get(sub)})


async def debug_trace_clear(request):
    trace.clear(request.query_params.get("sub", ""))
    return JSONResponse({"ok": True})


app.add_route("/health", health, methods=["GET"])
app.add_route("/debug/trace", debug_trace, methods=["GET"])
app.add_route("/debug/trace/clear", debug_trace_clear, methods=["POST"])
app.add_route("/github/login", github_oauth.github_login, methods=["GET"])
app.add_route("/github/callback", github_oauth.github_callback, methods=["GET"])

# Middleware wraps the whole app. GET and /github and /health are let through
# inside the middleware itself.
app.add_middleware(OBOAuthMiddleware)
