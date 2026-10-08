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
from sender import DryRunSender, WalletDSender  # noqa: E402
import board as board_mod  # noqa: E402
import auth as auth_mod  # noqa: E402

JSON = {"Content-Type": "application/json"}

# Mount prefix for the zone route (entangleit.com/swarmgit/*).
# Stripped so routes read the same on workers.dev and the zone —
# same idiom as the trust worker's MOUNT/routePath.
MOUNT = "/swarmgit"


def _route_path(pathname):
    if pathname == MOUNT or pathname.startswith(MOUNT + "/"):
        return pathname[len(MOUNT):] or "/"
    return pathname

_SCHEMA_READY = False


def _to_py(v):
    to_py = getattr(v, "to_py", None)
    return to_py() if callable(to_py) else v


def _queue_body(raw):
    """Coerce a queue message body to a dict.

    The producer sends Python dicts, but the runtime may deliver the
    body as a JSON string, bytes, or a JsProxy — all of which fail the
    isinstance(dict) check and were silently acked-and-dropped (killed
    the pilot's verify+merge flow: 4 messages, zero effects). Parse
    defensively; return None when the body is not a lifecycle message.
    """
    body = _to_py(raw)
    if isinstance(body, (bytes, bytearray)):
        try:
            body = bytes(body).decode("utf-8")
        except Exception:
            return None
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except Exception:
            return None
    # JsProxy of a JS object exposes no .to_py in some runtimes but
    # supports keys(); last resort before giving up.
    if not isinstance(body, dict) and hasattr(body, "keys"):
        try:
            body = {k: _queue_body(v) for k, v in body.items()}
        except Exception:
            return None
    return body if isinstance(body, dict) else None


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
        try:
            ai = getattr(self.env, "AI", None)
        except Exception:
            ai = None
        return {
            "enqueue": self._enqueue,
            "http": self._http,
            "artifacts": artifacts_mod.from_env(self.env),
            "sender": self._sender(),
            "walletd_url": self._walletd_url(),
            "ai": ai,
        }

    async def _fetch_inner(self, request):
        path = _path(getattr(request, "url", "/"))
        method = (getattr(request, "method", "GET") or "GET").upper()
        path = _route_path(path)

        if path == "/" and method == "GET":
            return Response(json.dumps({
                "name": "SwarmGit",
                "mcp": "/mcp",
                "tools": [t["name"] for t in mcp_mod.TOOLS],
            }), headers=JSON)


        if path == "/walletd/pending" and method == "GET":
            why = self._walletd_auth_error(request)
            if why:
                return Response(json.dumps({"error": why}),
                                status=401, headers=JSON)
            store = await self._db()
            return Response(json.dumps({"ok": True,
                                        "pending": await self._pending_releases(store)}),
                            headers=JSON)

        if path == "/walletd/release" and method == "POST":
            why = self._walletd_auth_error(request)
            if why:
                return Response(json.dumps({"error": why}),
                                status=401, headers=JSON)
            try:
                body = _to_py(await request.json()) or {}
            except Exception:
                return Response(json.dumps({"ok": False, "error": "parse error"}),
                                status=400, headers=JSON)
            store = await self._db()
            try:
                out = await self._ack_release(store, body)
            except Exception as ex:
                return Response(json.dumps({"ok": False, "error": str(ex)}),
                                status=400, headers=JSON)
            return Response(json.dumps(out), headers=JSON)

        if path == "/board" and method == "GET":
            # Public, read-only demo view (contest video). No auth, no
            # writes: board.render only calls the store's read methods.
            # Never 502/504 — 503 on unexpected failure.
            try:
                store = await self._db()
                try:
                    pay = (getattr(self.env, "SWARMSGIT_PAY_ADDRESS", "")
                           or "").strip()
                except Exception:
                    pay = ""
                page = await board_mod.render(store, pay_address=pay)
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

        if path == "/auth/config" and method == "GET":
            cid = (getattr(self.env, "GOOGLE_CLIENT_ID", "") or "").strip()
            return Response(json.dumps({"configured": bool(cid),
                                        "clientId": cid}), headers=JSON)

        if path == "/auth/me" and method == "GET":
            store = await self._db()
            sess = await auth_mod.get_session(
                store, self._session_id(request))
            if not sess:
                # Fallback transport: session id as Bearer credential.
                try:
                    auth = request.headers.get("Authorization") or ""
                except Exception:
                    auth = ""
                token = ""
                if str(auth).startswith("Bearer "):
                    token = str(auth)[len("Bearer "):].strip()
                if token:
                    sess = await auth_mod.get_session(store, token)
            if not sess:
                return Response(json.dumps({"signedIn": False}),
                                status=401, headers=JSON)
            return Response(json.dumps(
                {"signedIn": True, "email": sess.get("email", ""),
                 "name": sess.get("name", "")}), headers=JSON)

        if path == "/auth/logout" and method == "POST":
            store = await self._db()
            sid = self._session_id(request)
            if sid:
                try:
                    await store.session_delete(sid)
                except Exception:
                    pass
            return Response(json.dumps({"ok": True}), headers={
                **JSON, "Set-Cookie": auth_mod.clear_cookie_header()})

        if path == "/auth/google" and method == "POST":
            try:
                rpc = _to_py(await request.json()) or {}
            except Exception:
                return Response(json.dumps(
                    {"ok": False, "error": "parse error"}),
                    status=400, headers=JSON)
            cid = (getattr(self.env, "GOOGLE_CLIENT_ID", "") or "").strip()
            try:
                ident = await auth_mod.verify_google_id_token(
                    self._http, rpc.get("id_token"), cid)
            except auth_mod.AuthError as e:
                return Response(json.dumps(
                    {"ok": False, "error": str(e)}),
                    status=401, headers=JSON)
            store = await self._db()
            sid = await auth_mod.mint_session(store, ident)
            return Response(json.dumps(
                {"ok": True, "email": ident["email"],
                 "name": ident["name"],
                 # Dual transport: HttpOnly cookie (primary) + body
                 # session id (fallback — some browsers/containers drop
                 # cross-context Set-Cookie; the page keeps this in
                 # sessionStorage and sends it as a Bearer credential).
                 "session": sid}), headers={
                **JSON,
                "Set-Cookie": auth_mod.session_cookie_header(sid)})

        if path != "/mcp" or method != "POST":
            return Response(json.dumps({"error": "not found"}),
                            status=404, headers=JSON)
        try:
            body = _to_py(await request.json())
        except Exception:
            return Response(json.dumps(
                {"jsonrpc": "2.0", "id": None,
                 "error": {"code": -32700, "message": "parse error"}}),
                status=400, headers=JSON)

        store = await self._db()
        import agents
        role, actor = await self._role(request, store)
        if role == "anon" and not agents.public_call(body):
            return Response(json.dumps({
                "error": "unauthorized: agents call register_agent with"
                         " no auth, then send Authorization: Bearer"
                         " <agent_token>. Reads need no auth."}),
                            status=401, headers=JSON)
        deps = self._deps()
        deps["role"] = role
        deps["actor"] = actor
        status, resp = await mcp_mod.dispatch(store, deps, body)
        if resp is None:  # notification
            return Response("", status=status)
        return Response(json.dumps(resp), status=status, headers=JSON)

    def _session_id(self, request):
        try:
            cookies = auth_mod.parse_cookies(
                request.headers.get("Cookie") or "")
        except Exception:
            return ""
        return cookies.get(auth_mod.SESSION_COOKIE, "")

    async def _role(self, request, store):
        """operator, agent, or anon. Agent tokens are sga_ and hashed."""
        import agents
        if await self._authed(request):
            return "operator", ""
        token = agents.bearer(request)
        if token.startswith(agents.PREFIX):
            row = await store.agent_by_hash(agents.hash_token(token))
            if row:
                return "agent", row.get("agent") or ""
        return "anon", ""

    async def _authed(self, request):
        """Operator bearer, Google session cookie, or Google session id
        as a Bearer credential (page fallback transport). Bearer first
        (no DB); session second (one indexed lookup). Fail closed."""
        try:
            headers = request.headers
            auth = headers.get("Authorization") or ""
        except Exception:
            return False
        expected = getattr(self.env, "SWARMSGIT_BEARER", "") or ""
        token = ""
        if str(auth).startswith("Bearer "):
            token = str(auth)[len("Bearer "):].strip()
        if expected and token == expected:
            return True
        try:
            store = await self._db()
            if token:
                sess = await auth_mod.get_session(store, token)
                if sess:
                    return True
            sess = await auth_mod.get_session(
                store, self._session_id(request))
        except Exception:
            return False
        return sess is not None

    def _walletd_url(self):
        return (getattr(self.env, "SWARMSGIT_WALLETD_URL", "") or "").strip()

    def _walletd_auth_error(self, request):
        """None if the bearer matches. A string if the puller should stop."""
        expected = (getattr(self.env, "SWARMSGIT_WALLETD_TOKEN", "") or "").strip()
        if not expected:
            return "worker secret SWARMSGIT_WALLETD_TOKEN is not set"
        try:
            auth = request.headers.get("Authorization") or ""
        except Exception:
            auth = ""
        token = str(auth)[7:].strip() if str(auth).startswith("Bearer ") else ""
        if not token:
            return "missing bearer"
        if token != expected:
            return "bearer does not match SWARMSGIT_WALLETD_TOKEN"
        return None

    async def _pending_releases(self, store):
        rows = await store.ledger_all()
        done = {r.get("detail", {}).get("task_id") for r in rows
                if r.get("kind") == "release_done"}
        pending = []
        for r in rows:
            if r.get("kind") != "release_requested":
                continue
            detail = r.get("detail") or {}
            if detail.get("task_id") in done:
                continue
            pending.append({"task_id": detail.get("task_id"),
                            "to": detail.get("to"),
                            "sats": int(r.get("sats") or 0),
                            "funding_txid": detail.get("funding_txid", ""),
                            "label": detail.get("label") or "swarmgit release"})
        return pending

    async def _ack_release(self, store, body):
        task_id = (body.get("task_id") or "").strip()
        txid = (body.get("txid") or "").strip().lower()
        if not task_id or len(txid) != 64:
            raise RuntimeError("task_id and 64-char txid required")
        pending = await self._pending_releases(store)
        hit = next((p for p in pending if p["task_id"] == task_id), None)
        if not hit:
            raise RuntimeError("no pending release for that task")
        await store.ledger_add("release_done", task_id, hit["to"], hit["sats"],
                               {"task_id": task_id, "txid": txid,
                                "funding_txid": hit.get("funding_txid", ""),
                                "settlement": "live", "signed_by": "bsv-walletd"})
        return {"ok": True, "task_id": task_id, "txid": txid}

    def _sender(self):
        url = self._walletd_url()
        if url:
            return WalletDSender(self._http, url)
        return DryRunSender()

    async def _enqueue(self, msg):
        await self.env.TASK_QUEUE.send(msg)

    async def _http(self, method, url, headers, timeout=10, body=None):
        from http_fetch import http_request  # lazy: imports `js`
        return await http_request(method, url, headers, timeout, body=body)

    # -- queue (task-lifecycle consumer) -----------------------------
    # Note: the runtime invokes this as queue(batch, env, ctx).
    # LESSON (pilot incident): the `env` parameter does NOT carry the
    # ARTIFACTS binding in queue context (from_env raised "not
    # configured" and every message crashed before processing). Use
    # self.env like the fetch path, and degrade artifacts to None —
    # the consumer only needs it for future diff computation.
    async def queue(self, batch, env, ctx):
        store = await self._db()
        try:
            artifacts = artifacts_mod.from_env(self.env)
        except Exception:
            artifacts = None
        try:
            ai = getattr(self.env, "AI", None)
        except Exception:
            ai = None
        deps = {
            "enqueue": self._enqueue,
            "http": self._http,
            "artifacts": artifacts,
            "sender": self._sender(),
            "walletd_url": self._walletd_url(),
            "ai": ai,
        }
        for message in batch.messages:
            body = _queue_body(message.body)
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
