#!/usr/bin/env python3
"""Local verification for the SwarmGit read-only status board (GET /board).

Runs under plain CPython (no workerd needed). Stubs `workers`/`js`,
uses sqlite3-backed FakeD1 with the REAL schema.sql, the REAL
artifacts.FakeArtifacts, and drives the REAL mcp.py dispatch plus the
REAL entry.py fetch handler.

Checks:
  B1. GET /board (no auth) -> 200, text/html, markers: task title,
      claim agent, "Leaderboard" heading, merge gate mode.
  B2. Token redaction: claim token last4 shown, full token absent.
  B3. Read-only: D1 row counts (claims, ledger) identical before/after.
  B4. POST /board -> 404 (no action surface); empty DB renders the
      empty state with 200.
"""
import asyncio
import json
import os
import sqlite3
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
SCHEMA = os.path.join(SRC, "schema.sql")

sys.path.insert(0, SRC)

# ---- stub the Workers-only modules ---------------------------------
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

import entry  # noqa: E402  (real worker entrypoint)
import mcp as mcp_mod  # noqa: E402
import consumer as consumer_mod  # noqa: E402
from store import D1Store  # noqa: E402
from artifacts import FakeArtifacts  # noqa: E402
from sender import DryRunSender  # noqa: E402


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
        return {"success": True,
                "meta": {"changes": cur.rowcount,
                         "last_row_id": cur.lastrowid}}


class FakeD1:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        with open(SCHEMA) as f:
            self.conn.executescript(f.read())

    def prepare(self, sql):
        return FakeStmt(self.conn, sql)


class FakeQueue:
    def __init__(self):
        self.sent = []

    async def send(self, msg, delay_seconds=0):
        self.sent.append((msg, delay_seconds))

    def drain(self):
        msgs = [m for m, _ in self.sent]
        self.sent.clear()
        return msgs


class FakeRequest:
    def __init__(self, method, url, headers=None, body=None):
        self.method = method
        self.url = url
        self.headers = dict(headers or {})
        self._body = body

    async def json(self):
        return self._body


async def _unreachable_http(method, url, headers, timeout=10, body=None):
    raise ConnectionError("no network in board test")


PASSED = 0


def check(name, cond):
    global PASSED
    assert cond, f"FAILED: {name}"
    PASSED += 1
    print(f"  [ok] {name}")


async def call_tool(store, deps, name, args):
    status, resp = await mcp_mod.dispatch(
        store, deps, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": name, "arguments": args}})
    assert status == 200, (name, status)
    return resp["result"]


def tool_ok(result):
    assert result.get("isError") is not True, result
    return json.loads(result["content"][0]["text"])


async def row_count(conn, table):
    cur = conn.execute(f"SELECT COUNT(*) FROM {table}")
    return cur.fetchone()[0]


async def main():
    fake_d1 = FakeD1()
    store = D1Store(fake_d1)
    await store.ensure_schema()
    artifacts = FakeArtifacts()
    await artifacts.create_repo("demo-repo")
    queue = FakeQueue()
    deps = {"enqueue": queue.send, "http": _unreachable_http,
            "artifacts": artifacts, "sender": DryRunSender()}

    async def drain():
        for m in queue.drain():
            await consumer_mod.process_message(store, deps, m)

    # -- build a full lifecycle: task -> 2 claims -> submit -> attest ->
    #    merge (gate falls back to diff mode: no preview URL) -> settle --
    r = tool_ok(await call_tool(store, deps, "post_task", {
        "repo": "demo-repo", "title": "Board demo task",
        "description": "A task worth screenshotting.",
        "acceptance_tests": [{"name": "t"}], "bounty_sats": 9000,
        "poster": "maintainer", "idempotency_key": "b-post-1"}))
    task_id = r["task_id"]
    c1 = tool_ok(await call_tool(store, deps, "claim_task", {
        "task_id": task_id, "agent": "agent_board_a",
        "idempotency_key": "b-claim-1"}))
    full_token = c1["repo_token"]
    last4 = full_token[-4:]
    tool_ok(await call_tool(store, deps, "claim_task", {
        "task_id": task_id, "agent": "agent_board_b",
        "idempotency_key": "b-claim-2"}))
    tool_ok(await call_tool(store, deps, "submit_work", {
        "task_id": task_id, "claim_id": c1["claim_id"],
        "agent": "agent_board_a", "preview_url": "",
        "idempotency_key": "b-sub-1"}))
    await drain()
    a = tool_ok(await call_tool(store, deps, "attest_verification", {
        "fork_id": c1["fork_id"], "verifier": "verifier_board",
        "verdict": "pass", "stake_sats": 100,
        "idempotency_key": "b-att-1"}))
    assert a["merge_queued"] is True
    await drain()
    assert (await store.get_task(task_id))["status"] == "settled"

    env = types.SimpleNamespace(
        DB=fake_d1, TASK_QUEUE=FakeQueue(), SWARMSGIT_BEARER="t",
        ARTIFACTS=FakeArtifacts())
    app = entry.Default(env)

    async def get_board():
        req = FakeRequest("GET", "https://x.workers.dev/board")
        return await app.fetch(req)

    # -- B3 (pre): snapshot row counts ---------------------------------
    claims_before = await row_count(fake_d1.conn, "claims")
    ledger_before = await row_count(fake_d1.conn, "escrow_ledger")

    # -- B1: markers ----------------------------------------------------
    resp = await get_board()
    check("B1: GET /board -> 200 (no auth)", resp.status == 200)
    ctype = resp.headers.get("Content-Type", "")
    check("B1: content-type is html", "text/html" in ctype)
    body = resp.body
    check("B1: task title marker", "Board demo task" in body)
    check("B1: claim agent marker", "agent_board_a" in body)
    check("B1: leaderboard heading", "Leaderboard" in body)
    check("B1: verifier marker", "verifier_board" in body)
    check("B1: gate mode shown", "gate mode:" in body)
    check("B1: bounty shown", "9,000 sats" in body)

    # -- B2: redaction --------------------------------------------------
    check("B2: token last4 shown", last4 in body)
    check("B2: full token never shown", full_token not in body)

    # -- B3: read-only ---------------------------------------------------
    resp2 = await get_board()
    check("B3: second GET still 200", resp2.status == 200)
    check("B3: claims unchanged",
          await row_count(fake_d1.conn, "claims") == claims_before)
    check("B3: ledger untouched",
          await row_count(fake_d1.conn, "escrow_ledger") == ledger_before)

    # -- B4: no action surface; empty DB --------------------------------
    req = FakeRequest("POST", "https://x.workers.dev/board")
    resp3 = await app.fetch(req)
    check("B4: POST /board -> 404", resp3.status == 404)

    env2 = types.SimpleNamespace(
        DB=FakeD1(), TASK_QUEUE=FakeQueue(), SWARMSGIT_BEARER="t",
        ARTIFACTS=FakeArtifacts())
    app2 = entry.Default(env2)
    req = FakeRequest("GET", "https://x.workers.dev/board")
    resp4 = await app2.fetch(req)
    check("B4: empty DB -> 200", resp4.status == 200)
    check("B4: empty state rendered", "No tasks posted yet" in resp4.body)

    print(f"\n== {PASSED} passed, 0 failed ==")


if __name__ == "__main__":
    asyncio.run(main())
