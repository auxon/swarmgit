#!/usr/bin/env python3
"""Tests for Google sign-in (auth.py) + entry.py auth routes (stdlib only).

Fake http serves canned tokeninfo; FakeD1 is sqlite3 over the REAL
schema.sql; sessions go through the REAL D1Store methods.
"""
import asyncio
import json
import os
import sqlite3
import sys
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
SCHEMA = os.path.join(SRC, "schema.sql")

sys.path.insert(0, SRC)

workers_mod = types.ModuleType("workers")


class WorkerEntrypoint:
    def __init__(self, env=None):
        self.env = env


class Response:
    def __init__(self, body="", status=200, headers=None):
        self.body = body
        self.status = status
        self.headers = dict(headers or {})


workers_mod.WorkerEntrypoint = WorkerEntrypoint
workers_mod.Response = Response
sys.modules["workers"] = workers_mod

js_mod = types.ModuleType("js")
js_mod.fetch = lambda *a, **k: (_ for _ in ()).throw(
    RuntimeError("js.fetch must not be called in tests"))
sys.modules["js"] = js_mod

import auth as auth_mod  # noqa: E402
import entry  # noqa: E402
from store import D1Store  # noqa: E402
from artifacts import FakeArtifacts  # noqa: E402


class FakeStmt:
    def __init__(self, conn, sql):
        self.conn = conn
        self.sql = sql
        self.args = ()

    def bind(self, *args):
        self.args = args
        return self

    async def all(self):
        cur = self.conn.execute(self.sql, self.args)
        cols = [d[0] for d in cur.description] if cur.description else []
        return {"results": [dict(zip(cols, row))
                            for row in cur.fetchall()]}

    async def run(self):
        cur = self.conn.execute(self.sql, self.args)
        self.conn.commit()
        return {"success": True}


class FakeD1:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        with open(SCHEMA) as f:
            self.conn.executescript(f.read())

    def prepare(self, sql):
        return FakeStmt(self.conn, sql)


class FakeQueue:
    async def send(self, msg, delay_seconds=0):
        return None


class FakeRequest:
    def __init__(self, method, url, headers=None, body=None):
        self.method = method
        self.url = url
        self.headers = dict(headers or {})
        self._body = body

    async def json(self):
        return self._body


CID = "test-client-id.apps.googleusercontent.com"


def tokeninfo_http(payload, status=200):
    async def fake_http(method, url, headers, timeout=10, body=None):
        assert method == "GET", method
        assert "tokeninfo" in url, url
        return {"status": status,
                "body": json.dumps(payload),
                "headers": {"content-type": "application/json"}}
    return fake_http


def good_payload(**over):
    p = {"aud": CID, "iss": "accounts.google.com",
         "exp": int(time.time()) + 3600, "sub": "g123",
         "email": "op@example.com", "name": "Op User"}
    p.update(over)
    return p


async def test_verify_ok():
    ident = await auth_mod.verify_google_id_token(
        tokeninfo_http(good_payload()), "tok", CID)
    assert ident == {"sub": "g123", "email": "op@example.com",
                     "name": "Op User"}, ident


async def test_verify_wrong_aud():
    try:
        await auth_mod.verify_google_id_token(
            tokeninfo_http(good_payload(aud="other")), "tok", CID)
    except auth_mod.AuthError as e:
        assert "another app" in str(e), e
        return
    raise AssertionError("expected refusal")


async def test_verify_expired():
    try:
        await auth_mod.verify_google_id_token(
            tokeninfo_http(good_payload(exp=int(time.time()) - 9999)),
            "tok", CID)
    except auth_mod.AuthError as e:
        assert "expired" in str(e), e
        return
    raise AssertionError("expected refusal")


async def test_verify_bad_iss():
    try:
        await auth_mod.verify_google_id_token(
            tokeninfo_http(good_payload(iss="evil.example")), "tok", CID)
    except auth_mod.AuthError as e:
        assert "issuer" in str(e), e
        return
    raise AssertionError("expected refusal")


async def test_verify_rejected_upstream():
    try:
        await auth_mod.verify_google_id_token(
            tokeninfo_http({"error": "invalid"}, status=400), "tok", CID)
    except auth_mod.AuthError:
        return
    raise AssertionError("expected refusal")


async def test_verify_unconfigured():
    try:
        await auth_mod.verify_google_id_token(
            tokeninfo_http(good_payload()), "tok", "")
    except auth_mod.AuthError as e:
        assert "not configured" in str(e), e
        return
    raise AssertionError("expected refusal")


async def test_session_roundtrip_and_expiry():
    store = D1Store(FakeD1())
    await store.ensure_schema()
    sid = await auth_mod.mint_session(
        store, {"sub": "g1", "email": "a@b.c", "name": "A"})
    row = await auth_mod.get_session(store, sid)
    assert row and row["email"] == "a@b.c", row
    assert await auth_mod.get_session(store, "nope") is None
    # force expiry
    await store._run("UPDATE sessions SET expires_at = ? WHERE session_id = ?",
                     int(time.time()) - 1, sid)
    assert await auth_mod.get_session(store, sid) is None
    assert await store.session_get(sid) is None  # lazily deleted


async def test_parse_cookies():
    cs = auth_mod.parse_cookies("a=1; sg_session=abc123; b=2")
    assert cs["sg_session"] == "abc123", cs
    assert auth_mod.parse_cookies("") == {}


def make_app(cid=CID, bearer="opaque-bearer"):
    env = types.SimpleNamespace(
        DB=FakeD1(), TASK_QUEUE=FakeQueue(), SWARMSGIT_BEARER=bearer,
        GOOGLE_CLIENT_ID=cid, ARTIFACTS=FakeArtifacts())
    return entry.Default(env)


async def test_auth_routes():
    app = make_app()

    # config reflects env
    r = await app.fetch(FakeRequest("GET", "https://x/auth/config"))
    assert r.status == 200, r.status
    cfg = json.loads(r.body)
    assert cfg == {"configured": True, "clientId": CID}, cfg

    # google login with stubbed http: monkeypatch instance _http
    async def fake_http(method, url, headers, timeout=10, body=None):
        return {"status": 200, "body": json.dumps(good_payload()),
                "headers": {}}
    app._http = fake_http
    r = await app.fetch(FakeRequest(
        "POST", "https://x/auth/google",
        {"Content-Type": "application/json"}, {"id_token": "tok"}))
    assert r.status == 200, r.body
    assert "Set-Cookie" in r.headers, r.headers
    assert "sg_session=" in r.headers["Set-Cookie"], r.headers
    assert "HttpOnly" in r.headers["Set-Cookie"]
    login = json.loads(r.body)
    assert login["ok"] is True and login.get("session"), login
    sid = login["session"]
    cookie = r.headers["Set-Cookie"].split(";")[0]

    # me with cookie
    r = await app.fetch(FakeRequest(
        "GET", "https://x/auth/me", {"Cookie": cookie}))
    assert r.status == 200, r.body
    me = json.loads(r.body)
    assert me["signedIn"] is True and me["email"] == "op@example.com", me

    # me without cookie
    r = await app.fetch(FakeRequest("GET", "https://x/auth/me"))
    assert r.status == 401, r.status

    # MCP via session cookie, no bearer
    r = await app.fetch(FakeRequest(
        "POST", "https://x/mcp", {"Cookie": cookie,
                                  "Content-Type": "application/json"},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}))
    assert r.status == 200, r.body
    assert "redrive_task" in r.body or "post_task" in r.body, r.body[:200]

    # MCP via session id as Bearer (fallback transport), no cookie
    r = await app.fetch(FakeRequest(
        "POST", "https://x/mcp", {"Authorization": "Bearer " + sid,
                                  "Content-Type": "application/json"},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}))
    assert r.status == 200, r.body

    # /auth/me via session id as Bearer (fallback transport)
    r = await app.fetch(FakeRequest(
        "GET", "https://x/auth/me", {"Authorization": "Bearer " + sid}))
    assert r.status == 200, r.body

    # MCP with neither: tools/list is public discovery now
    r = await app.fetch(FakeRequest(
        "POST", "https://x/mcp", {"Content-Type": "application/json"},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}))
    assert r.status == 200, r.status
    tools = json.loads(r.body)["result"]["tools"]
    assert any(t["name"] == "register_agent" for t in tools), tools

    # MCP with neither: non-public tools still 401
    r = await app.fetch(FakeRequest(
        "POST", "https://x/mcp", {"Content-Type": "application/json"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "claim_task",
                    "arguments": {"task_id": "t", "agent": "a",
                                  "idempotency_key": "k"}}}))
    assert r.status == 401, r.status

    # logout clears
    r = await app.fetch(FakeRequest(
        "POST", "https://x/auth/logout", {"Cookie": cookie}))
    assert r.status == 200, r.status
    assert "Max-Age=0" in r.headers.get("Set-Cookie", ""), r.headers
    r = await app.fetch(FakeRequest(
        "GET", "https://x/auth/me", {"Cookie": cookie}))
    assert r.status == 401, r.status


async def test_auth_unconfigured():
    app = make_app(cid="")
    r = await app.fetch(FakeRequest("GET", "https://x/auth/config"))
    assert json.loads(r.body) == {"configured": False, "clientId": ""}


async def main():
    tests = [
        ("verify ok", test_verify_ok),
        ("verify wrong aud", test_verify_wrong_aud),
        ("verify expired", test_verify_expired),
        ("verify bad iss", test_verify_bad_iss),
        ("verify rejected upstream", test_verify_rejected_upstream),
        ("verify unconfigured", test_verify_unconfigured),
        ("session roundtrip+expiry", test_session_roundtrip_and_expiry),
        ("parse cookies", test_parse_cookies),
        ("auth routes end-to-end", test_auth_routes),
        ("auth unconfigured", test_auth_unconfigured),
    ]
    passed = failed = 0
    for name, fn in tests:
        try:
            r = fn()
            if asyncio.iscoroutine(r):
                await r
        except AssertionError as e:
            failed += 1
            print(f"FAIL {name}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {name}: {type(e).__name__}: {e}")
        else:
            passed += 1
            print(f"PASS {name}")
    print(f"== {passed} passed, {failed} failed ==")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
