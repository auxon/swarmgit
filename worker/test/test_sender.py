#!/usr/bin/env python3
"""AgentPaySender + choose_sender + settle_task payout safety.

Fakes only. Never dials entangleit.com. Never spends sats.
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

from sender import (  # noqa: E402
    AgentPaySender, DryRunSender, SendError, choose_sender)
from store import D1Store  # noqa: E402
import gitlib  # noqa: E402
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


class FakeAgentPayHTTP:
    """In-process stand-in for POST /agent/send. Records calls."""

    def __init__(self, mode="ok"):
        self.mode = mode
        self.calls = []
        self.by_key = {}
        self.n = 0

    async def __call__(self, method, url, headers, timeout=10, body=None):
        self.calls.append({"method": method, "url": url,
                           "headers": dict(headers), "body": body})
        auth = (headers or {}).get("Authorization") or ""
        if not auth.startswith("Bearer agp_"):
            return {"ok": True, "status": 401,
                    "body": json.dumps({"error": "unauthorized"})}
        payload = json.loads(body or "{}")
        key = payload.get("idempotency_key") or ""
        if key and key in self.by_key:
            prior = self.by_key[key]
            return {"ok": True, "status": 200,
                    "body": json.dumps({"ok": True, "settlement": "live",
                                        "txid": prior, "dest": payload.get("dest"),
                                        "sats": payload.get("sats")})}
        if self.mode == "fail":
            return {"ok": True, "status": 503,
                    "body": json.dumps({"error": "treasury down"})}
        if self.mode == "no_txid":
            return {"ok": True, "status": 200,
                    "body": json.dumps({"ok": True, "settlement": "live"})}
        if self.mode == "network":
            return {"ok": False, "error": "timeout"}
        self.n += 1
        txid = ("ab" * 32)[:-2] + f"{self.n:02d}"
        if key:
            self.by_key[key] = txid
        return {"ok": True, "status": 200,
                "body": json.dumps({"ok": True, "settlement": "live",
                                    "txid": txid, "dest": payload.get("dest"),
                                    "sats": payload.get("sats")})}


async def _posted(store, artifacts, bounty=1000, agent="agent_a"):
    await artifacts.create_repo("r")
    spec = {
        "repo": "r", "title": "t", "description": "d",
        "acceptance_tests": [{"name": "t"}],
        "bounty_sats": bounty, "poster": "p",
        "funding_txid": "ab" * 32,
        "escrow_address": "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa",
    }
    task = await gitlib.post_task(store, spec)
    claim = await gitlib.claim_task(
        store, artifacts, task["task_id"], agent,
        "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa")
    await gitlib.submit_work(
        store, task["task_id"], claim["claim_id"], agent, "",
        "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa")
    fork = await store.get_fork(claim["fork_id"])
    fork["status"] = "verifying"
    await store.put_fork(fork)
    return task, claim


async def _ready_to_settle(store, artifacts, bounty=1000):
    task, claim = await _posted(store, artifacts, bounty=bounty)
    await gitlib.attest_verification(
        store, claim["fork_id"], "verifier_v", "pass", 10)
    await gitlib.record_merge(
        store, task["task_id"], claim["fork_id"], {"mode": "diff"}, [],
        decision="merged")
    t = await store.get_task(task["task_id"])
    t["status"] = "merging"
    await store.put_task(t)
    return task, claim


def check(name, cond):
    assert cond, name
    print(f"  [ok] {name}")


async def test_choose_sender_default_dry_run():
    env = types.SimpleNamespace()
    s = choose_sender(env, None)
    check("default is DryRunSender", isinstance(s, DryRunSender))


async def test_choose_sender_needs_flag_and_key():
    http = FakeAgentPayHTTP()
    env = types.SimpleNamespace(AGENTPAY_KEY="agp_secret")
    s = choose_sender(env, http)
    check("key without flag stays dry-run", isinstance(s, DryRunSender))
    env = types.SimpleNamespace(SWARMSGIT_LIVE_SETTLEMENT="1")
    s = choose_sender(env, http)
    check("flag without key stays dry-run", isinstance(s, DryRunSender))
    env = types.SimpleNamespace(SWARMSGIT_LIVE_SETTLEMENT="1",
                                AGENTPAY_KEY="agp_secret")
    s = choose_sender(env, http)
    check("flag+key is AgentPaySender", isinstance(s, AgentPaySender))


async def test_agentpay_send_ok_and_idempotent():
    http = FakeAgentPayHTTP()
    s = AgentPaySender(http, "agp_secret")
    r1 = await s.send("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa", 500,
                      "memo", "idem-1")
    check("live txid returned", r1["settlement"] == "live" and r1["txid"])
    r2 = await s.send("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa", 500,
                      "memo", "idem-1")
    check("same idempotency key returns same txid", r1["txid"] == r2["txid"])
    check("agentpay saw Bearer agp_",
          http.calls[0]["headers"]["Authorization"] == "Bearer agp_secret")
    body = json.loads(http.calls[0]["body"])
    check("body has dest/sats/idempotency_key",
          body["dest"].startswith("1") and body["sats"] == 500
          and body["idempotency_key"] == "idem-1")
    check("posts /agent/send",
          http.calls[0]["url"].endswith("/agent/send"))


async def test_agentpay_refuses_no_txid():
    s = AgentPaySender(FakeAgentPayHTTP("no_txid"), "agp_secret")
    try:
        await s.send("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa", 10, "m", "k")
    except SendError as e:
        check("no-txid is SendError", "txid" in str(e))
        return
    raise AssertionError("expected SendError")


async def test_failing_payout_stays_pending():
    store = D1Store(FakeD1())
    await store.ensure_schema()
    artifacts = FakeArtifacts()
    task, claim = await _ready_to_settle(store, artifacts)
    sender = AgentPaySender(FakeAgentPayHTTP("fail"), "agp_secret")
    out = await gitlib.settle_task(store, sender, task["task_id"])
    check("failing live send is pending", out.get("pending") is True)
    check("not funds_moved", out.get("funds_moved") is not True)
    rec = await store.get_task(task["task_id"])
    check("task stays merging", rec["status"] == "merging")
    ledger = await store.ledger_for_task(task["task_id"])
    check("payout_failed recorded",
          any(e["kind"] == "payout_failed" for e in ledger))
    check("no live payout without txid",
          not any(e["kind"] == "payout" and e["settlement"] == "live"
                  for e in ledger))


async def test_double_payout_prevented():
    store = D1Store(FakeD1())
    await store.ensure_schema()
    artifacts = FakeArtifacts()
    task, claim = await _ready_to_settle(store, artifacts)
    http = FakeAgentPayHTTP()
    sender = AgentPaySender(http, "agp_secret")
    out1 = await gitlib.settle_task(store, sender, task["task_id"])
    check("first live settle paid", out1.get("funds_moved") is True
          and out1["payouts"][0]["txid"])
    n_after_first = len(http.calls)
    rec = await store.get_task(task["task_id"])
    check("task settled", rec["status"] == "settled")
    try:
        await gitlib.settle_task(store, sender, task["task_id"])
    except gitlib.GitError as e:
        check("second settle refused (not merging)", "not merging" in str(e))
    else:
        raise AssertionError("expected second settle to refuse")
    check("agentpay not called again after settle",
          len(http.calls) == n_after_first)
    # Even if status were forced back to merging, the live txid
    # short-circuit must not broadcast a second payment.
    rec["status"] = "merging"
    await store.put_task(rec)
    out2 = await gitlib.settle_task(store, sender, task["task_id"])
    check("replay reuses txid",
          out2["payouts"][0]["txid"] == out1["payouts"][0]["txid"])
    check("replay did not POST a new send",
          len(http.calls) == n_after_first)


async def test_apw_pay_address_accepted():
    gitlib.validate_pay_address("apw_abcdEFGH1234")
    try:
        gitlib.validate_pay_address("apw_short")
    except gitlib.GitError:
        pass
    else:
        raise AssertionError("short apw_ should fail")
    try:
        gitlib.validate_pay_address("auto")
    except gitlib.GitError:
        pass
    else:
        raise AssertionError("bare name should fail")
    check("apw_ wallet id is a payout dest", True)


async def test_reserved_auto_name():
    try:
        gitlib.validate_agent_name("auto")
    except gitlib.GitError as e:
        check("agent name auto reserved", "reserved" in str(e))
        return
    raise AssertionError("expected reserved-name refusal")


async def test_dry_run_still_settles_locally():
    store = D1Store(FakeD1())
    await store.ensure_schema()
    artifacts = FakeArtifacts()
    task, claim = await _ready_to_settle(store, artifacts)
    sender = DryRunSender()
    out = await gitlib.settle_task(store, sender, task["task_id"])
    check("dry-run ok", out.get("ok") is True and out.get("dry_run") is True)
    rec = await store.get_task(task["task_id"])
    check("dry-run task settled", rec["status"] == "settled")
    check("dry-run moved nothing on chain",
          out.get("funds_moved") is not True
          and all(i["txid"] is None for i in sender.intents))


async def main():
    tests = [
        ("choose_sender default dry-run", test_choose_sender_default_dry_run),
        ("choose_sender flag+key", test_choose_sender_needs_flag_and_key),
        ("agentpay send + idempotency", test_agentpay_send_ok_and_idempotent),
        ("agentpay no txid refused", test_agentpay_refuses_no_txid),
        ("failing payout stays pending", test_failing_payout_stays_pending),
        ("double payout prevented", test_double_payout_prevented),
        ("apw_ pay_address accepted", test_apw_pay_address_accepted),
        ("reserved auto name", test_reserved_auto_name),
        ("dry-run still settles locally", test_dry_run_still_settles_locally),
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
    print(f"== {passed} passed, {failed} failed ==")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
