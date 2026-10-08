"""SwarmGit MCP Worker — Cloudflare Workers entrypoint (Python).

  fetch : bearer-token auth -> MCP Streamable HTTP JSON-RPC on POST /mcp
  queue : task-lifecycle consumer (verify / merge / artifact_push)

Auth: every request needs `Authorization: Bearer <SWARMSGIT_BEARER>`,
set via `wrangler secret put SWARMSGIT_BEARER` (never in code or
wrangler.toml). Artifacts is reached ONLY through the ARTIFACTS binding
(never a public-URL subfetch — same-account loop protection, AGENTS.md).
"""
import json
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from workers import WorkerEntrypoint, Response  # noqa: E402

import mcp as mcp_mod  # noqa: E402
from store import D1Store  # noqa: E402
import consumer as consumer_mod  # noqa: E402
import artifacts as artifacts_mod  # noqa: E402
from sender import AgentPaySender, DryRunSender  # noqa: E402
import board as board_mod  # noqa: E402

JSON = {"Content-Type": "application/json"}

_SCHEMA_READY = False


def _to_py(v):
    to_py = getattr(v, "to_py", None)
    return to_py() if callable(to_py) else v


def _path(url):
    try:
        return urllib.parse.urlsplit(url).path or "/"
    except Exception:
        return "/"


class Default(WorkerEntrypoint):
    # -- fetch -----------------------------------------------------
    async def fetch(self, request):
        return await self._fetch_inner(request)

    async def _db(self):
        """D1-backed store; creates tables on first use per isolate."""
        global _SCHEMA_READY
        store = D1Store(self.env.DB)
        if not _SCHEMA_READY:
            await store.ensure_schema()
            _SCHEMA_READY = True
        return store

    def _deps(self):
        """Per-request dependency bundle (fakes injected in tests)."""
        return {
            "enqueue": self._enqueue,
            "http": self._http,
            "artifacts": artifacts_mod.from_env(self.env),
            "sender": self._sender(),
        }

    def _sender(self):
        """Live AgentPaySender when the agent key is configured, else dry-run."""
        key = getattr(self.env, "AGENTPAY_AGENT_KEY", "") or ""
        if key:
            return AgentPaySender(agent_key=key)
        return DryRunSender()

    async def _fetch_inner(self, request):
        path = _path(getattr(request, "url", "/"))
        method = (getattr(request, "method", "GET") or "GET").upper()

        if path == "/" and method == "GET":
            return Response(json.dumps({
                "name": "SwarmGit",
                "mcp": "/mcp",
                "tools": [t["name"] for t in mcp_mod.TOOLS],
            }), headers=JSON)

        if path == "/board" and method == "GET":
            # Public, read-only demo view (contest video). No auth, no
            # writes: board.render only calls the store's read methods.
            # Never 502/504 — 503 on unexpected failure.
            try:
                store = await self._db()
                page = await board_mod.render(store)
            except Exception:
                return Response("board unavailable", status=503,
                                headers={"Content-Type": "text/plain"})
            return Response(page, headers={
                "Content-Type": "text/html; charset=utf-8"})

        if path == "/mcp" and method == "GET":
            return Response(json.dumps({"error": "method not allowed: "
                                                 "this endpoint is "
                                                 "stateless POST only"}),
                            status=405, headers=JSON)

        if path != "/mcp" or method != "POST":
            return Response(json.dumps({"error": "not found"}),
                            status=404, headers=JSON)

        if not self._authed(request):
            # Never 502/504 from this worker (Cloudflare edge strips
            # them); 401 passes through untouched.
            return Response(json.dumps({"error": "unauthorized: valid "
                                                 "bearer token required"}),
                            status=401, headers=JSON)
        try:
            body = _to_py(await request.json())
        except Exception:
            return Response(json.dumps(
                {"jsonrpc": "2.0", "id": None,
                 "error": {"code": -32700, "message": "parse error"}}),
                status=400, headers=JSON)

        store = await self._db()
        status, resp = await mcp_mod.dispatch(
            store, self._deps(), body)
        if resp is None:  # notification
            return Response("", status=status)
        return Response(json.dumps(resp), status=status, headers=JSON)

    def _authed(self, request):
        try:
            headers = request.headers
            auth = headers.get("Authorization") or ""
        except Exception:
            return False
        expected = getattr(self.env, "SWARMSGIT_BEARER", "") or ""
        if not expected:
            return False  # fail closed: no secret configured, no access
        return str(auth) == f"Bearer {expected}"

    async def _enqueue(self, msg):
        await self.env.TASK_QUEUE.send(msg)

    async def _http(self, method, url, headers, timeout=10, body=None):
        from http_fetch import http_request  # lazy: imports `js`
        return await http_request(method, url, headers, timeout, body=body)

    def _sender_for(self, env):
        """Live AgentPaySender when the agent key is configured, else dry-run."""
        key = getattr(env, "AGENTPAY_AGENT_KEY", "") or ""
        if key:
            return AgentPaySender(agent_key=key)
        return DryRunSender()

    # -- queue (task-lifecycle consumer) -----------------------------
    # Note: the runtime invokes this as queue(batch, env, ctx).
    async def queue(self, batch, env, ctx):
        store = await self._db()
        deps = {
            "enqueue": self._enqueue,
            "http": self._http,
            "artifacts": artifacts_mod.from_env(env),
            "sender": self._sender_for(env),
        }
        for message in batch.messages:
            body = _to_py(message.body)
            if isinstance(body, dict):
                # process_message never raises, but ack defensively
                try:
                    await consumer_mod.process_message(store, deps, body)
                except Exception:
                    pass
            try:
                message.ack()
            except Exception:
                pass
