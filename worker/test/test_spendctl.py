#!/usr/bin/env python3
"""Spending controls and kill switch. Fakes only. Never spends sats."""
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
sys.modules["js"] = types.ModuleType("js")

import spendctl  # noqa: E402
import gitlib  # noqa: E402
import mcp as mcp_mod  # noqa: E402
import board as board_mod  # noqa: E402
import entry  # noqa: E402
from store import D1Store  # noqa: E402
from artifacts import FakeArtifacts  # noqa: E402
from sender import AgentPaySender, DryRunSender, choose_sender  # noqa: E402


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


class FakeRequest:
    def __init__(self, method, url, headers=None, body=None):
        self.method = method
        self.url = url
        self.headers = dict(headers or {})
        self._body = body

    async def json(self):
        return self._body


P2PKH = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"
CAPS = {
    "SWARMSGIT_PAYOUT_CAP_SATS": "5000",
    "SWARMSGIT_DAILY_CAP_SATS": "20000",
    "SWARMSGIT_WEEKLY_CAP_SATS": "50000",
    "SWARMSGIT_AGENT_DAILY_CAP_SATS": "8000",
    "SWARMSGIT_APPROVAL_THRESHOLD_SATS": "3000",
}


def env(**kw):
    d = {"SWARMSGIT_LIVE_SETTLEMENT": "1", "AGENTPAY_KEY": "agp_secret"}
    d.update(CAPS)
    d.update(kw)
    return types.SimpleNamespace(**d)


def check(name, cond):
    assert cond, name
    print(f"  [ok] {name}")


async def _store():
    s = D1Store(FakeD1())
    await s.ensure_schema()
    return s


async def _posted(store, artifacts, bounty=1000, agent="worker1"):
    await artifacts.create_repo("r")
    spec = {
        "repo": "r", "title": "t", "description": "d",
        "acceptance_tests": [{"name": "t"}],
        "bounty_sats": bounty, "poster": "p",
        "funding_txid": "cd" * 32, "escrow_address": P2PKH,
    }
    task = await gitlib.post_task(store, spec)
    claim = await gitlib.claim_task(
        store, artifacts, task["task_id"], agent, P2PKH)
    await gitlib.submit_work(
        store, task["task_id"], claim["claim_id"], agent, "", P2PKH)
    fork = await store.get_fork(claim["fork_id"])
    fork["status"] = "verifying"
    await store.put_fork(fork)
    return task, claim


async def _ready(store, artifacts, bounty=1000, agent="worker1"):
    task, claim = await _posted(store, artifacts, bounty=bounty, agent=agent)
    await gitlib.attest_verification(
        store, claim["fork_id"], "verifier_v", "pass", 10)
    await gitlib.record_merge(
        store, task["task_id"], claim["fork_id"], {"mode": "diff"}, [],
        decision="merged")
    t = await store.get_task(task["task_id"])
    t["status"] = "merging"
    await store.put_task(t)
    return task, claim


class FakePayHTTP:
    def __init__(self):
        self.calls = []

    async def __call__(self, method, url, headers, timeout=10, body=None):
        self.calls.append({"method": method, "url": url, "body": body})
        data = json.loads(body)
        return {"ok": True, "status": 200, "body": json.dumps({
            "ok": True, "settlement": "live",
            "txid": "ab" * 32, "dest": data["dest"], "sats": data["sats"]})}


async def test_empty_caps_unconfigured_zero_configured():
    check("blank is unconfigured",
          spendctl.env_int(types.SimpleNamespace(), "SWARMSGIT_PAYOUT_CAP_SATS")
          is None)
    check("empty string is unconfigured",
          spendctl.env_int(types.SimpleNamespace(SWARMSGIT_PAYOUT_CAP_SATS=""),
                           "SWARMSGIT_PAYOUT_CAP_SATS") is None)
    check("0 is configured",
          spendctl.env_int(types.SimpleNamespace(SWARMSGIT_PAYOUT_CAP_SATS="0"),
                           "SWARMSGIT_PAYOUT_CAP_SATS") == 0)
    check("live_armed needs caps",
          spendctl.live_armed(types.SimpleNamespace(
              SWARMSGIT_LIVE_SETTLEMENT="1", AGENTPAY_KEY="agp_x")) is False)
    check("live_armed with caps", spendctl.live_armed(env()) is True)
    check("AGENTPAY_AGENT_KEY counts",
          spendctl.live_armed(env(AGENTPAY_KEY="",
                                  AGENTPAY_AGENT_KEY="agp_prod")) is True)
    check("flag alone is not enough",
          "SWARMSGIT_PAYOUT_CAP_SATS" in spendctl.missing_live_requirements(
              types.SimpleNamespace(SWARMSGIT_LIVE_SETTLEMENT="1",
                                    AGENTPAY_KEY="agp_x")))
    s = choose_sender(types.SimpleNamespace(
        SWARMSGIT_LIVE_SETTLEMENT="1", AGENTPAY_AGENT_KEY="agp_prod"), None)
    check("prod key without caps stays DryRun", isinstance(s, DryRunSender))


async def test_env_pause_and_d1_pause():
    store = await _store()
    check("default not paused",
          await spendctl.is_paused(types.SimpleNamespace(), store) is False)
    check("env pause",
          await spendctl.is_paused(env(SWARMSGIT_PAUSED="1"), store) is True)
    await spendctl.set_paused(store, True, "richard")
    check("D1 pause",
          await spendctl.is_paused(env(), store) is True)
    await spendctl.set_paused(store, False, "richard")
    check("D1 resume",
          await spendctl.is_paused(env(), store) is False)
    await spendctl.set_paused(store, True, "richard")
    check("env pause overrides resume? env off, D1 on",
          await spendctl.is_paused(env(), store) is True)


async def test_pause_blocks_claim():
    store = await _store()
    artifacts = FakeArtifacts()
    await artifacts.create_repo("r")
    spec = {
        "repo": "r", "title": "t", "description": "d",
        "acceptance_tests": [{"name": "t"}],
        "bounty_sats": 100, "poster": "p",
        "funding_txid": "cd" * 32, "escrow_address": P2PKH,
    }
    task = await gitlib.post_task(store, spec)
    await spendctl.set_paused(store, True, "op")
    try:
        await gitlib.claim_task(
            store, artifacts, task["task_id"], "worker1", P2PKH)
    except gitlib.GitError as e:
        check("paused claim refused", "paused" in str(e))
    else:
        raise AssertionError("expected paused claim refusal")
    await spendctl.set_paused(store, False, "op")
    claim = await gitlib.claim_task(
        store, artifacts, task["task_id"], "worker1", P2PKH)
    check("resume allows claim", claim.get("claim_id"))


async def test_pause_blocks_settle():
    store = await _store()
    artifacts = FakeArtifacts()
    task, claim = await _ready(store, artifacts)
    await spendctl.set_paused(store, True, "op")
    http = FakePayHTTP()
    sender = AgentPaySender(http, "agp_secret")
    out = await gitlib.settle_task(
        store, sender, task["task_id"], env=env())
    check("paused settle pending", out.get("pending") is True)
    check("paused block_code", out.get("block_code") == "paused")
    check("paused did not POST send", http.calls == [])
    rec = await store.get_task(task["task_id"])
    check("paused task stays merging", rec["status"] == "merging")
    dry = DryRunSender()
    out2 = await gitlib.settle_task(
        store, dry, task["task_id"], env=env())
    check("pause also freezes dry-run settle", out2.get("pending") is True)
    check("dry-run sender unused while paused", dry.intents == [])


async def test_payout_cap():
    store = await _store()
    blocked = await spendctl.gate_payout(
        env(SWARMSGIT_PAYOUT_CAP_SATS="100"), store, "a", 101, "t1")
    check("over per-payout cap", blocked and blocked["code"] == "payout_cap")
    ok = await spendctl.gate_payout(
        env(SWARMSGIT_PAYOUT_CAP_SATS="100"), store, "a", 100, "t1")
    check("at per-payout cap allowed", ok is None)


async def test_daily_cap():
    store = await _store()
    await store.ledger_add(
        "payout", "old", "a", 15000,
        {"settlement": "live", "txid": "ab" * 32})
    wide = dict(SWARMSGIT_PAYOUT_CAP_SATS="100000",
                SWARMSGIT_APPROVAL_THRESHOLD_SATS="100000",
                SWARMSGIT_AGENT_DAILY_CAP_SATS="100000")
    blocked = await spendctl.gate_payout(
        env(SWARMSGIT_DAILY_CAP_SATS="20000", **wide), store, "b", 6000, "t2")
    check("daily cap blocks", blocked and blocked["code"] == "daily_cap")
    ok = await spendctl.gate_payout(
        env(SWARMSGIT_DAILY_CAP_SATS="20000", **wide), store, "b", 4000, "t2")
    check("daily remaining allows", ok is None)


async def test_weekly_cap():
    store = await _store()
    await store.ledger_add(
        "payout", "w1", "a", 40000,
        {"settlement": "internal", "receipt_id": "snd_abcde"})
    wide = dict(SWARMSGIT_PAYOUT_CAP_SATS="100000",
                SWARMSGIT_DAILY_CAP_SATS="100000",
                SWARMSGIT_APPROVAL_THRESHOLD_SATS="100000",
                SWARMSGIT_AGENT_DAILY_CAP_SATS="100000")
    blocked = await spendctl.gate_payout(
        env(SWARMSGIT_WEEKLY_CAP_SATS="50000", **wide), store, "b", 15000, "t3")
    check("weekly cap blocks", blocked and blocked["code"] == "weekly_cap")
    ok = await spendctl.gate_payout(
        env(SWARMSGIT_WEEKLY_CAP_SATS="50000", **wide), store, "b", 9000, "t3")
    check("weekly remaining allows", ok is None)


async def test_agent_daily_cap():
    store = await _store()
    await store.ledger_add(
        "payout", "t", "alice", 7000,
        {"settlement": "live", "txid": "cd" * 32})
    wide = dict(SWARMSGIT_PAYOUT_CAP_SATS="100000",
                SWARMSGIT_DAILY_CAP_SATS="100000",
                SWARMSGIT_WEEKLY_CAP_SATS="100000",
                SWARMSGIT_APPROVAL_THRESHOLD_SATS="100000")
    blocked = await spendctl.gate_payout(
        env(SWARMSGIT_AGENT_DAILY_CAP_SATS="8000", **wide),
        store, "alice", 2000, "t4")
    check("agent daily cap blocks",
          blocked and blocked["code"] == "agent_daily_cap")
    ok = await spendctl.gate_payout(
        env(SWARMSGIT_AGENT_DAILY_CAP_SATS="8000", **wide),
        store, "bob", 2000, "t4")
    check("other agent not charged", ok is None)


async def test_approval_threshold():
    store = await _store()
    blocked = await spendctl.gate_payout(
        env(SWARMSGIT_APPROVAL_THRESHOLD_SATS="3000"),
        store, "alice", 4000, "taskA")
    check("above threshold needs approval",
          blocked and blocked["code"] == "needs_approval")
    rows = await store.list_payout_approvals("pending")
    check("approval row created",
          len(rows) == 1 and rows[0]["agent"] == "alice")
    again = await spendctl.gate_payout(
        env(SWARMSGIT_APPROVAL_THRESHOLD_SATS="3000"),
        store, "alice", 4000, "taskA")
    check("repeat still needs approval",
          again and again["code"] == "needs_approval")
    check("no duplicate approval rows",
          len(await store.list_payout_approvals("pending")) == 1)
    await spendctl.decide_approval(store, "taskA", "alice", True, "op", "ok")
    ok = await spendctl.gate_payout(
        env(SWARMSGIT_APPROVAL_THRESHOLD_SATS="3000"),
        store, "alice", 4000, "taskA")
    check("approved payout proceeds", ok is None)
    store2 = await _store()
    await spendctl.gate_payout(
        env(SWARMSGIT_APPROVAL_THRESHOLD_SATS="10"),
        store2, "bob", 50, "taskB")
    await spendctl.decide_approval(store2, "taskB", "bob", False, "op", "no")
    rejected = await spendctl.gate_payout(
        env(SWARMSGIT_APPROVAL_THRESHOLD_SATS="10"),
        store2, "bob", 50, "taskB")
    check("rejected payout stays blocked",
          rejected and rejected["code"] == "approval_rejected")


async def test_live_settle_respects_cap():
    store = await _store()
    artifacts = FakeArtifacts()
    task, claim = await _ready(store, artifacts, bounty=1000)
    http = FakePayHTTP()
    sender = AgentPaySender(http, "agp_secret")
    out = await gitlib.settle_task(
        store, sender, task["task_id"],
        env=env(SWARMSGIT_PAYOUT_CAP_SATS="100"))
    check("capped live settle pending", out.get("pending") is True)
    check("capped did not send", http.calls == [])
    rec = await store.get_task(task["task_id"])
    check("capped task stays merging", rec["status"] == "merging")


async def test_mcp_and_admin_and_board():
    store = await _store()
    e = env()
    deps = {"env": e, "role": "operator", "actor": "richard",
            "enqueue": lambda m: None, "artifacts": FakeArtifacts(),
            "sender": DryRunSender()}
    st, resp = await mcp_mod.dispatch(store, deps, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "set_pause",
                   "arguments": {"paused": True, "operator": "richard"}}})
    payload = json.loads(resp["result"]["content"][0]["text"])
    check("MCP set_pause", payload.get("paused") is True)
    st, resp = await mcp_mod.dispatch(store, deps, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "get_spend_controls", "arguments": {}}})
    snap = json.loads(resp["result"]["content"][0]["text"])
    check("MCP snapshot paused", snap.get("paused") is True)
    check("MCP snapshot no key secret",
          "agp_secret" not in json.dumps(snap)
          and snap.get("agentpay_key_present") is True)
    await spendctl.set_paused(store, False, "richard")
    await spendctl.gate_payout(
        env(SWARMSGIT_APPROVAL_THRESHOLD_SATS="10"),
        store, "alice", 50, "taskZ")
    st, resp = await mcp_mod.dispatch(store, deps, {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "approve_payout",
                   "arguments": {"task_id": "taskZ", "agent": "alice",
                                 "approve": True, "operator": "richard"}}})
    ap = json.loads(resp["result"]["content"][0]["text"])
    check("MCP approve_payout", ap.get("status") == "approved")

    page = await board_mod.render(store, spend=await spendctl.snapshot(e, store))
    check("board spend panel", 'id="sg-spend"' in page)
    check("board shows cap", "per-payout cap" in page)
    check("board shows pause control", 'id="sg-pause-form"' in page)

    fake_d1 = FakeD1()
    store2 = D1Store(fake_d1)
    await store2.ensure_schema()
    app = entry.Default(types.SimpleNamespace(
        DB=fake_d1, TASK_QUEUE=types.SimpleNamespace(),
        SWARMSGIT_BEARER="t", ARTIFACTS=FakeArtifacts(),
        **CAPS, SWARMSGIT_LIVE_SETTLEMENT="0"))
    req = FakeRequest("POST", "https://x.workers.dev/admin/pause",
                      headers={"Authorization": "Bearer t"},
                      body={"paused": True, "operator": "op"})
    resp = await app.fetch(req)
    check("admin pause 200", resp.status == 200)
    body = json.loads(resp.body)
    check("admin pause applied", body.get("paused") is True)
    req = FakeRequest("GET", "https://x.workers.dev/admin/spend",
                      headers={"Authorization": "Bearer t"})
    resp = await app.fetch(req)
    check("admin spend 200", resp.status == 200)
    check("admin spend paused", json.loads(resp.body).get("paused") is True)
    req = FakeRequest("POST", "https://x.workers.dev/admin/pause",
                      body={"paused": True})
    resp = await app.fetch(req)
    check("admin pause without bearer 401", resp.status == 401)
    req = FakeRequest("GET", "https://x.workers.dev/board")
    resp = await app.fetch(req)
    check("board GET shows spend controls",
          resp.status == 200 and "Spend controls" in resp.body)


async def main():
    tests = [
        ("empty caps unconfigured; 0 configured",
         test_empty_caps_unconfigured_zero_configured),
        ("env pause and D1 pause", test_env_pause_and_d1_pause),
        ("pause blocks claim", test_pause_blocks_claim),
        ("pause blocks settle", test_pause_blocks_settle),
        ("per-payout cap", test_payout_cap),
        ("daily cap", test_daily_cap),
        ("weekly cap", test_weekly_cap),
        ("per-agent daily cap", test_agent_daily_cap),
        ("approval threshold", test_approval_threshold),
        ("live settle respects cap", test_live_settle_respects_cap),
        ("MCP + admin + board", test_mcp_and_admin_and_board),
    ]
    passed = failed = 0
    for name, fn in tests:
        try:
            await fn()
            passed += 1
            print(f"PASS {name}")
        except Exception as e:
            failed += 1
            print(f"FAIL {name}: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
    print(f"== {passed} passed, {failed} failed ==")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
