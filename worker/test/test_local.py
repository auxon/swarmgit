#!/usr/bin/env python3
"""Local verification for the SwarmGit Cloudflare Worker.

Runs under plain CPython (no workerd needed):

  - stubs the `workers` and `js` modules so src/entry.py imports cleanly
  - FakeD1: sqlite3-backed fake executing the REAL schema.sql and the
    REAL SQL in store.py
  - FakeArtifacts: the REAL artifacts.FakeArtifacts (in-memory)
  - FakeHTTP: in-process fake MCP preview deployments (clean + leaky
    with a planted live secret), speaking JSON-RPC like a real fork
    preview would
  - DryRunSender: the REAL sender.DryRunSender
  - drives the REAL mcp.py dispatch, the REAL consumer.process_message
    (verify -> merge -> settle), and the REAL entry.py fetch handler

Planted-bug proofs:
  P1. fork whose preview leaks a live sk-live secret -> merge BLOCKED
      (critical/confirmed, secret redacted in evidence)
  P2. fork failing acceptance tests -> verifier fail w/ repro -> fork
      rejected, never merges
  P3. double-claim (same agent, same task) -> refused
  P4. self-verification -> refused
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


def _no_fetch(*a, **k):
    raise RuntimeError("js.fetch must not be called in tests")


js_mod.fetch = _no_fetch
sys.modules["js"] = js_mod

import entry  # noqa: E402  (real worker entrypoint)
import mcp as mcp_mod  # noqa: E402
import consumer as consumer_mod  # noqa: E402
import merge_gate  # noqa: E402  (real gate)
from store import D1Store  # noqa: E402
import store as store_mod  # noqa: E402
from artifacts import FakeArtifacts  # noqa: E402
from sender import DryRunSender  # noqa: E402


# ---- FakeD1 (sqlite3-backed) ----------------------------------------
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

    async def batch(self, stmts):
        for s in stmts:
            self.conn.execute(s.sql, s.args)
        self.conn.commit()


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
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


# ---- FakeHTTP: fake MCP preview deployments --------------------------
LEAK_SECRET = "sk-live-6a5f8e2d4c1a9b3e7f"
CLEAN_URL = "https://preview-clean.example/mcp"
LEAKY_URL = "https://preview-leaky.example/mcp"


def _rpc_ok(msg_id, result):
    return {"status": 200, "headers": {},
            "body": json.dumps({"jsonrpc": "2.0", "id": msg_id,
                                "result": result})}


async def fake_http(method, url, headers, timeout=10, body=None):
    """Minimal MCP server. LEAKY_URL's get_config leaks a live secret."""
    raw = bytes(body).decode("utf-8") if isinstance(
        body, (bytes, bytearray)) else (body or "{}")
    try:
        msg = json.loads(raw)
    except Exception:
        return {"status": 400, "headers": {}, "body": "{}"}
    msg_id = msg.get("id")
    method_name = msg.get("method")
    params = msg.get("params") or {}
    leaky = "leaky" in url

    if method_name == "initialize":
        return _rpc_ok(msg_id, {"protocolVersion": "2025-06-18",
                                "capabilities": {},
                                "serverInfo": {"name": "fake-preview",
                                               "version": "0.0.1"}})
    if method_name == "notifications/initialized":
        return {"status": 202, "headers": {}, "body": ""}
    if method_name == "tools/list":
        tools = ([{"name": "get_config",
                   "description": "Read server config",
                   "inputSchema": {"type": "object", "properties": {}}}]
                 if leaky else
                 [{"name": "build",
                   "description": "Build the thing",
                   "inputSchema": {"type": "object",
                                   "properties": {
                                       "target": {"type": "string"}}}}])
        return _rpc_ok(msg_id, {"tools": tools})
    if method_name == "tools/call":
        name = params.get("name", "")
        if leaky and name == "get_config":
            text = json.dumps({"api_key": LEAK_SECRET, "region": "us"})
        else:
            text = json.dumps({"ok": True, "echo": params.get("arguments")})
        return _rpc_ok(msg_id, {"content": [{"type": "text", "text": text}]})
    return {"status": 200, "headers": {},
            "body": json.dumps({"jsonrpc": "2.0", "id": msg_id,
                                "error": {"code": -32601,
                                          "message": "unknown"}})}


# ---- check helper ----------------------------------------------------
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


def tool_err_text(result):
    assert result.get("isError") is True, result
    return json.loads(result["content"][0]["text"])["error"]


async def main():
    fake_d1 = FakeD1()
    store = D1Store(fake_d1)
    await store.ensure_schema()
    artifacts = FakeArtifacts()
    await artifacts.create_repo("demo-repo")
    queue = FakeQueue()
    sender = DryRunSender()
    deps = {"enqueue": queue.send, "http": fake_http,
            "artifacts": artifacts, "sender": sender}

    async def drain():
        for m in queue.drain():
            await consumer_mod.process_message(store, deps, m)

    # -- 0. DDL parity: store._DDL mirrors schema.sql ------------------
    import re as _re
    def norm(s):
        return " ".join(s.split())
    def strip_comments(sql):
        return _re.sub(r"--[^\n]*", "", sql)
    file_stmts = {norm(s) for s in strip_comments(open(SCHEMA).read())
                  .split(";") if norm(s)}
    ddl_stmts = {norm(s) for s in store_mod._DDL}
    check("DDL parity store._DDL == schema.sql", file_stmts == ddl_stmts)

    # -- 1. post_task ---------------------------------------------------
    r = tool_ok(await call_tool(store, deps, "post_task", {
        "repo": "demo-repo", "title": "Add rate-limit headers",
        "description": "Every tool response carries X-RateLimit-*.",
        "acceptance_tests": [{"name": "headers_present"},
                             {"name": "limit_values_sane"}],
        "bounty_sats": 10000, "poster": "maintainer",
        "idempotency_key": "k-post-1"}))
    task_id = r["task_id"]
    check("post_task ok, status open", r["status"] == "open")
    check("post_task escrow dry-run", "dry_run" in r["escrow"])
    r2 = tool_ok(await call_tool(store, deps, "post_task", {
        "repo": "demo-repo", "title": "Add rate-limit headers",
        "acceptance_tests": [{"name": "headers_present"}],
        "bounty_sats": 10000, "idempotency_key": "k-post-1"}))
    check("post_task idempotent repost", r2.get("idempotent") is True
          and r2["task_id"] == task_id)
    e = tool_err_text(await call_tool(store, deps, "post_task", {
        "repo": "demo-repo", "title": "x",
        "acceptance_tests": [{"name": "t"}], "bounty_sats": 1}))
    check("post_task requires idempotency_key", "idempotency_key" in e)
    e = tool_err_text(await call_tool(store, deps, "post_task", {
        "repo": "demo-repo", "title": "x", "acceptance_tests": [],
        "bounty_sats": 1, "idempotency_key": "k-post-2"}))
    check("post_task requires acceptance_tests", "acceptance_tests" in e)

    # -- 2. claims -------------------------------------------------------
    c1 = tool_ok(await call_tool(store, deps, "claim_task", {
        "task_id": task_id, "agent": "agent_a",
        "idempotency_key": "k-claim-1"}))
    check("claim_task mints fork", c1["fork_id"].startswith("fork_"))
    check("claim_task returns token once",
          c1["repo_token"].startswith("fake-token-"))
    e = tool_err_text(await call_tool(store, deps, "claim_task", {
        "task_id": task_id, "agent": "agent_a",
        "idempotency_key": "k-claim-2"}))
    check("P3: double-claim refused", "double-claim" in e)
    c2 = tool_ok(await call_tool(store, deps, "claim_task", {
        "task_id": task_id, "agent": "agent_b",
        "idempotency_key": "k-claim-3"}))
    check("second agent claims concurrently",
          c2["claim_id"] != c1["claim_id"])
    st = tool_ok(await call_tool(store, deps, "get_task_status",
                                 {"task_id": task_id}))
    check("get_task_status shows 2 claims", len(st["claims"]) == 2)

    # -- 3. submit -> verify ---------------------------------------------
    s1 = tool_ok(await call_tool(store, deps, "submit_work", {
        "task_id": task_id, "claim_id": c1["claim_id"], "agent": "agent_a",
        "preview_url": LEAKY_URL, "idempotency_key": "k-sub-1"}))
    check("submit_work queues verify", s1["verify_queued"] is True)
    s2 = tool_ok(await call_tool(store, deps, "submit_work", {
        "task_id": task_id, "claim_id": c2["claim_id"], "agent": "agent_b",
        "preview_url": CLEAN_URL, "idempotency_key": "k-sub-2"}))
    check("second submit ok", s2["status"] == "submitted")
    await drain()
    forks = await store.forks_for_task(task_id)
    check("both forks verifying",
          all(f["status"] == "verifying" for f in forks))

    # -- 4. attestation -> merge -> settle (clean fork wins) --------------
    a1 = tool_ok(await call_tool(store, deps, "attest_verification", {
        "fork_id": c2["fork_id"], "verifier": "verifier_v",
        "verdict": "pass", "stake_sats": 100,
        "idempotency_key": "k-att-1"}))
    check("first verified pass queues merge", a1["merge_queued"] is True)
    await drain()  # merge: gate vs CLEAN_URL -> pass -> settle
    task = await store.get_task(task_id)
    check("task settled after merge+settle", task["status"] == "settled")
    merges = await store.merges_for_task(task_id)
    check("one merge recorded", len(merges) == 1)
    check("merge decision merged", merges[0]["decision"] == "merged")
    audit = merges[0]["audit_report"]
    check("gate ran in preview mode", audit.get("mode") == "preview")
    check("gate ran all 4 packs",
          set(audit.get("packs_run", [])) == set(merge_gate.GATE_PACKS))
    why = await store.why_entries_for_merge(merges[0]["merge_id"])
    kinds = {e["kind"] for e in why}
    check("why-graph staged (all 5 kinds)",
          {"task_posted", "attempt", "verification", "rationale",
           "decision"} <= kinds)
    ledger = await store.ledger_for_task(task_id)
    check("every ledger event dry_run",
          all(e["settlement"] == "dry_run" for e in ledger))
    payouts = [e for e in ledger if e["kind"] == "payout"]
    by_agent = {e["agent"]: e["sats"] for e in payouts}
    check("winner gets 80%", by_agent.get("agent_b") == 8000)
    check("verifier gets 20%", by_agent.get("verifier_v") == 2000)
    check("sender moved nothing",
          all(p["settlement"] == "dry_run" for p in sender.intents)
          and len(sender.intents) == 2)
    lb = tool_ok(await call_tool(store, deps, "get_leaderboard", {}))
    top = {x["agent"]: x for x in lb["leaderboard"]}
    check("leaderboard: winner +10 / 1 task won",
          top["agent_b"]["score"] == 10 and top["agent_b"]["tasks_won"] == 1)
    mr = tool_ok(await call_tool(store, deps, "get_merge_report",
                                 {"task_id": task_id}))
    check("get_merge_report carries audit + why entries",
          mr["merges"][0]["audit_report"].get("mode") == "preview"
          and len(mr["merges"][0]["why_entries"]) >= 5)

    # -- 5. P1: leaky fork BLOCKED at the gate ---------------------------
    r = tool_ok(await call_tool(store, deps, "post_task", {
        "repo": "demo-repo", "title": "Leaky task",
        "acceptance_tests": [{"name": "t"}], "bounty_sats": 5000,
        "idempotency_key": "k-post-10"}))
    t2 = r["task_id"]
    c3 = tool_ok(await call_tool(store, deps, "claim_task", {
        "task_id": t2, "agent": "agent_c",
        "idempotency_key": "k-claim-10"}))
    tool_ok(await call_tool(store, deps, "submit_work", {
        "task_id": t2, "claim_id": c3["claim_id"], "agent": "agent_c",
        "preview_url": LEAKY_URL, "idempotency_key": "k-sub-10"}))
    await drain()
    a2 = tool_ok(await call_tool(store, deps, "attest_verification", {
        "fork_id": c3["fork_id"], "verifier": "verifier_v2",
        "verdict": "pass", "stake_sats": 50,
        "idempotency_key": "k-att-10"}))
    check("leaky fork attested pass (tests green, secret hidden)",
          a2["merge_queued"] is True)
    await drain()  # merge: gate vs LEAKY_URL -> BLOCK
    m2 = await store.merges_for_task(t2)
    check("P1: leaky fork blocked at gate",
          len(m2) == 1 and m2[0]["decision"] == "gate_blocked")
    t2rec = await store.get_task(t2)
    check("blocked task returns to open", t2rec["status"] == "open")
    findings = m2[0]["audit_report"].get("findings", [])
    crit = [f for f in findings if f.get("severity") == "critical"
            and f.get("confidence") == "confirmed"]
    check("P1: critical/confirmed secret finding", len(crit) >= 1)
    blob = json.dumps(findings)
    check("P1: secret redacted in evidence",
          LEAK_SECRET not in blob and "sk-live-" in blob)

    # -- 6. P2: failing fork never merges ---------------------------------
    r = tool_ok(await call_tool(store, deps, "post_task", {
        "repo": "demo-repo", "title": "Failing task",
        "acceptance_tests": [{"name": "t"}], "bounty_sats": 3000,
        "idempotency_key": "k-post-20"}))
    t3 = r["task_id"]
    c4 = tool_ok(await call_tool(store, deps, "claim_task", {
        "task_id": t3, "agent": "agent_d",
        "idempotency_key": "k-claim-20"}))
    tool_ok(await call_tool(store, deps, "submit_work", {
        "task_id": t3, "claim_id": c4["claim_id"], "agent": "agent_d",
        "preview_url": CLEAN_URL, "idempotency_key": "k-sub-20"}))
    await drain()
    e = tool_err_text(await call_tool(store, deps, "attest_verification", {
        "fork_id": c4["fork_id"], "verifier": "verifier_v3",
        "verdict": "fail", "stake_sats": 10, "repro": "",
        "idempotency_key": "k-att-20"}))
    check("fail verdict requires repro", "repro" in e)
    a3 = tool_ok(await call_tool(store, deps, "attest_verification", {
        "fork_id": c4["fork_id"], "verifier": "verifier_v3",
        "verdict": "fail", "stake_sats": 10,
        "repro": "headers missing on /mcp responses",
        "idempotency_key": "k-att-21"}))
    check("P2: fail attestation with repro, no merge",
          a3["merge_queued"] is not True)
    f4 = await store.get_fork(c4["fork_id"])
    check("P2: failing fork rejected", f4["status"] == "rejected")
    check("P2: no merge recorded", await store.merges_for_task(t3) == [])

    # -- 7. P4: self-verification refused --------------------------------
    r = tool_ok(await call_tool(store, deps, "post_task", {
        "repo": "demo-repo", "title": "Self task",
        "acceptance_tests": [{"name": "t"}], "bounty_sats": 1000,
        "idempotency_key": "k-post-30"}))
    t4 = r["task_id"]
    c5 = tool_ok(await call_tool(store, deps, "claim_task", {
        "task_id": t4, "agent": "agent_e",
        "idempotency_key": "k-claim-30"}))
    tool_ok(await call_tool(store, deps, "submit_work", {
        "task_id": t4, "claim_id": c5["claim_id"], "agent": "agent_e",
        "preview_url": CLEAN_URL, "idempotency_key": "k-sub-30"}))
    await drain()
    e = tool_err_text(await call_tool(store, deps, "attest_verification", {
        "fork_id": c5["fork_id"], "verifier": "agent_e",
        "verdict": "pass", "idempotency_key": "k-att-30"}))
    check("P4: cannot verify own work", "own work" in e)

    # -- 8. entry.py auth surface ----------------------------------------
    env = types.SimpleNamespace(
        DB=fake_d1, TASK_QUEUE=FakeQueue(), SWARMSGIT_BEARER="test-token",
        ARTIFACTS=FakeArtifacts())
    app = entry.Default(env)

    async def fetch_json(method, path, headers=None, body=None):
        req = FakeRequest(method, "https://x.workers.dev" + path,
                          headers=headers, body=body)
        resp = await app.fetch(req)
        return resp.status, json.loads(resp.body or "{}")

    s, _ = await fetch_json("POST", "/mcp", headers={}, body={
        "jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    check("entry: no bearer -> 401", s == 401)
    s, b = await fetch_json("POST", "/mcp",
                            headers={"Authorization": "Bearer test-token"},
                            body={"jsonrpc": "2.0", "id": 1,
                                  "method": "tools/list"})
    check("entry: bearer -> tools/list 200",
          s == 200 and len(b["result"]["tools"]) in (8, 9))
    # TEMPORARY (pilot incident): 9 while redrive_task exists; back to 8
    # when it is removed before the contest submission.
    s, _ = await fetch_json("GET", "/mcp",
                            headers={"Authorization": "Bearer test-token"})
    check("entry: GET /mcp -> 405", s == 405)

    print(f"\n== {PASSED} passed, 0 failed ==")


if __name__ == "__main__":
    asyncio.run(main())
