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


def _token_shape(dest):
    dest = dest or ""
    return dest.startswith("apw_") and len(dest) == 4 + 64 and all(
        c in "0123456789abcdefABCDEF" for c in dest[4:])


def _wallet_id_shape(dest):
    dest = dest or ""
    if _token_shape(dest):
        return False
    rest = dest[4:] if dest.startswith("apw_") else ""
    return dest.startswith("apw_") and 8 <= len(rest) <= 48 and rest.isalnum()


class FakeAgentPayHTTP:
    """In-process stand-in for POST /agent/send. Records calls.

    P2PKH dest → settlement live + 64-hex txid.
    Public apw_ dest → settlement internal + snd_ receipt.
    64-hex wallet token dest → 400.
    """

    def __init__(self, mode="ok"):
        self.mode = mode
        self.calls = []
        self.by_key = {}
        self.n = 0

    def _ok_body(self, dest, sats, key):
        self.n += 1
        if _wallet_id_shape(dest):
            rid = "snd_%08dabcd" % self.n
            body = {"ok": True, "settlement": "internal",
                    "txid": rid, "receiptId": rid,
                    "dest": dest, "sats": sats}
        else:
            txid = ("ab" * 32)[:-2] + f"{self.n:02d}"
            body = {"ok": True, "settlement": "live",
                    "txid": txid, "dest": dest, "sats": sats}
        if key:
            self.by_key[key] = body
        return body

    async def __call__(self, method, url, headers, timeout=10, body=None):
        self.calls.append({"method": method, "url": url,
                           "headers": dict(headers), "body": body})
        auth = (headers or {}).get("Authorization") or ""
        if not auth.startswith("Bearer agp_"):
            return {"ok": True, "status": 401,
                    "body": json.dumps({"error": "unauthorized"})}
        payload = json.loads(body or "{}")
        dest = payload.get("dest") or ""
        sats = payload.get("sats")
        key = payload.get("idempotency_key") or ""
        if _token_shape(dest):
            return {"ok": True, "status": 400,
                    "body": json.dumps({
                        "error": "dest looks like a wallet token; "
                                 "pass the public wallet id"})}
        if key and key in self.by_key:
            return {"ok": True, "status": 200,
                    "body": json.dumps(self.by_key[key])}
        if self.mode == "fail":
            return {"ok": True, "status": 503,
                    "body": json.dumps({"error": "treasury down"})}
        if self.mode == "no_txid":
            kind = "internal" if _wallet_id_shape(dest) else "live"
            return {"ok": True, "status": 200,
                    "body": json.dumps({"ok": True, "settlement": kind})}
        if self.mode == "hardcode_live_internal":
            # agentpay bug simulation: snd_ id claimed as live
            self.n += 1
            rid = "snd_%08dabcd" % self.n
            return {"ok": True, "status": 200,
                    "body": json.dumps({"ok": True, "settlement": "live",
                                        "txid": rid, "dest": dest,
                                        "sats": sats})}
        if self.mode == "network":
            return {"ok": False, "error": "timeout"}
        return {"ok": True, "status": 200,
                "body": json.dumps(self._ok_body(dest, sats, key))}


P2PKH = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"
WALLET_ID = "apw_m3k2abc123def456"
WALLET_TOKEN = "apw_" + "ab" * 32


async def _posted(store, artifacts, bounty=1000, agent="agent_a",
                  pay_address=P2PKH):
    await artifacts.create_repo("r")
    spec = {
        "repo": "r", "title": "t", "description": "d",
        "acceptance_tests": [{"name": "t"}],
        "bounty_sats": bounty, "poster": "p",
        "funding_txid": "ab" * 32,
        "escrow_address": P2PKH,
    }
    task = await gitlib.post_task(store, spec)
    claim = await gitlib.claim_task(
        store, artifacts, task["task_id"], agent, pay_address)
    await gitlib.submit_work(
        store, task["task_id"], claim["claim_id"], agent, "",
        pay_address)
    fork = await store.get_fork(claim["fork_id"])
    fork["status"] = "verifying"
    await store.put_fork(fork)
    return task, claim


async def _ready_to_settle(store, artifacts, bounty=1000, pay_address=P2PKH):
    task, claim = await _posted(store, artifacts, bounty=bounty,
                                pay_address=pay_address)
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
    r1 = await s.send(P2PKH, 500, "memo", "idem-1")
    check("live txid returned", r1["settlement"] == "live" and r1["txid"])
    check("live txid is 64 hex", gitlib.is_bsv_txid(r1["txid"]))
    r2 = await s.send(P2PKH, 500, "memo", "idem-1")
    check("same idempotency key returns same txid", r1["txid"] == r2["txid"])
    check("replay stays live", r2["settlement"] == "live")
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
        await s.send(P2PKH, 10, "m", "k")
    except SendError as e:
        check("no-txid is SendError", "txid" in str(e))
        return
    raise AssertionError("expected SendError")


async def test_agentpay_internal_apw_send():
    http = FakeAgentPayHTTP()
    s = AgentPaySender(http, "agp_secret")
    r1 = await s.send(WALLET_ID, 500, "bounty", "idem-apw")
    check("internal settlement", r1["settlement"] == "internal")
    check("receipt is snd_", gitlib.is_agentpay_receipt_id(r1["receipt_id"]))
    check("txid field is the send id", r1["txid"] == r1["receipt_id"])
    check("not a BSV txid", not gitlib.is_bsv_txid(r1["txid"]))
    r2 = await s.send(WALLET_ID, 500, "bounty", "idem-apw")
    check("internal replay same receipt", r1["receipt_id"] == r2["receipt_id"])
    check("internal replay stays internal", r2["settlement"] == "internal")
    body = json.loads(http.calls[0]["body"])
    check("passed public wallet id", body["dest"] == WALLET_ID)


async def test_agentpay_refuses_wallet_token():
    http = FakeAgentPayHTTP()
    s = AgentPaySender(http, "agp_secret")
    try:
        await s.send(WALLET_TOKEN, 10, "m", "k")
    except SendError as e:
        check("token dest refused before POST", "wallet token" in str(e))
        check("never posted a secret token", http.calls == [])
        return
    raise AssertionError("expected SendError")


async def test_agentpay_refuses_live_snd_txid():
    s = AgentPaySender(FakeAgentPayHTTP("hardcode_live_internal"), "agp_secret")
    try:
        await s.send(WALLET_ID, 10, "m", "k")
    except SendError as e:
        check("snd_ claimed as live is refused", "non-BSV" in str(e)
              or "txid" in str(e))
        return
    raise AssertionError("expected SendError")


async def test_agentpay_internal_no_receipt_refused():
    s = AgentPaySender(FakeAgentPayHTTP("no_txid"), "agp_secret")
    try:
        await s.send(WALLET_ID, 10, "m", "k")
    except SendError as e:
        check("no-receipt internal is SendError",
              "receipt" in str(e) or "txid" in str(e))
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
    gitlib.validate_pay_address(WALLET_ID)
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
    try:
        gitlib.validate_pay_address(WALLET_TOKEN)
    except gitlib.GitError as e:
        check("64-hex token refused", "wallet token" in str(e)
              or "public" in str(e))
    else:
        raise AssertionError("secret token should fail")
    check("apw_ public wallet id is a payout dest", True)


async def test_internal_settle_records_receipt():
    store = D1Store(FakeD1())
    await store.ensure_schema()
    artifacts = FakeArtifacts()
    task, claim = await _ready_to_settle(store, artifacts,
                                         pay_address=WALLET_ID)
    http = FakeAgentPayHTTP()
    sender = AgentPaySender(http, "agp_secret")
    out = await gitlib.settle_task(store, sender, task["task_id"])
    check("internal settle paid", out.get("funds_moved") is True)
    check("winner settlement is internal",
          out["payouts"][0]["settlement"] == "internal")
    rid = out["payouts"][0].get("receipt_id")
    check("receipt_id is snd_", gitlib.is_agentpay_receipt_id(rid))
    check("internal payout has no BSV txid",
          not gitlib.is_bsv_txid(out["payouts"][0].get("txid")))
    rec = await store.get_task(task["task_id"])
    check("internal task settled", rec["status"] == "settled")
    ledger = await store.ledger_for_task(task["task_id"])
    pays = [e for e in ledger if e["kind"] == "payout"
            and e["agent"] == out["winner"]]
    check("ledger settlement internal",
          pays and pays[0]["settlement"] == "internal")
    check("ledger stores receipt_id not txid",
          pays[0]["detail"].get("receipt_id") == rid
          and not pays[0]["detail"].get("txid"))
    import board
    html = board.payout_proof_html("internal", pays[0]["detail"])
    check("board labels receipt", "receipt" in html and rid in html)
    check("board has no WhatsOnChain for internal",
          "whatsonchain.com" not in html)
    check("helper refuses WOC for snd_",
          board.whatsonchain_tx_url("internal", rid) == ""
          and board.whatsonchain_tx_url("live", rid) == "")


async def test_internal_double_payout_prevented():
    store = D1Store(FakeD1())
    await store.ensure_schema()
    artifacts = FakeArtifacts()
    task, claim = await _ready_to_settle(store, artifacts,
                                         pay_address=WALLET_ID)
    http = FakeAgentPayHTTP()
    sender = AgentPaySender(http, "agp_secret")
    out1 = await gitlib.settle_task(store, sender, task["task_id"])
    n_after_first = len(http.calls)
    rec = await store.get_task(task["task_id"])
    rec["status"] = "merging"
    await store.put_task(rec)
    out2 = await gitlib.settle_task(store, sender, task["task_id"])
    check("internal replay reuses receipt",
          out2["payouts"][0]["receipt_id"] == out1["payouts"][0]["receipt_id"])
    check("internal replay did not POST a new send",
          len(http.calls) == n_after_first)
    check("replay still internal",
          out2["payouts"][0]["settlement"] == "internal")


async def test_live_settle_records_bsv_txid():
    store = D1Store(FakeD1())
    await store.ensure_schema()
    artifacts = FakeArtifacts()
    task, claim = await _ready_to_settle(store, artifacts, pay_address=P2PKH)
    sender = AgentPaySender(FakeAgentPayHTTP(), "agp_secret")
    out = await gitlib.settle_task(store, sender, task["task_id"])
    check("live settle paid", out.get("funds_moved") is True)
    check("winner settlement is live",
          out["payouts"][0]["settlement"] == "live")
    txid = out["payouts"][0]["txid"]
    check("live txid is 64 hex", gitlib.is_bsv_txid(txid))
    ledger = await store.ledger_for_task(task["task_id"])
    pays = [e for e in ledger if e["kind"] == "payout"
            and e["agent"] == out["winner"]]
    check("ledger stores txid not receipt_id",
          pays[0]["detail"].get("txid") == txid
          and not pays[0]["detail"].get("receipt_id"))
    import board
    html = board.payout_proof_html("live", pays[0]["detail"])
    check("board links WhatsOnChain for live",
          "whatsonchain.com/tx/" + txid in html)
    check("helper WOC url for live",
          board.whatsonchain_tx_url("live", txid)
          == "https://whatsonchain.com/tx/" + txid)


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
        ("agentpay internal apw send", test_agentpay_internal_apw_send),
        ("agentpay wallet token refused", test_agentpay_refuses_wallet_token),
        ("agentpay live snd_ refused", test_agentpay_refuses_live_snd_txid),
        ("agentpay internal no receipt", test_agentpay_internal_no_receipt_refused),
        ("failing payout stays pending", test_failing_payout_stays_pending),
        ("double payout prevented", test_double_payout_prevented),
        ("apw_ pay_address accepted", test_apw_pay_address_accepted),
        ("internal settle records receipt", test_internal_settle_records_receipt),
        ("internal double payout prevented", test_internal_double_payout_prevented),
        ("live settle records BSV txid", test_live_settle_records_bsv_txid),
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
