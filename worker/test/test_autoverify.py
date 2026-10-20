#!/usr/bin/env python3
"""Auto-verify: tests + Clef review, fail-closed deferrals.

Fakes only. No network, no containers, no sats.
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

import autoverify  # noqa: E402
import arbiter  # noqa: E402
import gitlib  # noqa: E402
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


class FakeAI:
    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    async def run(self, model_ref, payload):
        self.calls.append((model_ref, payload))
        return {"answers": self.answers}


class FakeQueue:
    def __init__(self):
        self.sent = []

    async def send(self, msg, delay_seconds=0):
        self.sent.append(msg)


PASS_REVIEW = {
    "implements": {"noul": 0.92},
    "ruling": {"choice": "pass", "confidence": 0.88,
               "probabilities": {"pass": 0.88, "fail": 0.12}},
    "scope": {"score": 2.5},
}

LOW_REVIEW = {
    "implements": {"noul": 0.4},
    "ruling": {"choice": "pass", "confidence": 0.41},
    "scope": {"score": 1.0},
}

FAIL_REVIEW = {
    "implements": {"noul": 0.1},
    "ruling": {"choice": "fail", "confidence": 0.91},
    "scope": {"score": 0.2},
}


async def fake_http_ok(method, url, headers, timeout=10, body=None):
    return {"ok": True, "status": 200, "headers": {},
            "body": json.dumps({"ok": True})}


async def fake_http_down(method, url, headers, timeout=10, body=None):
    return {"ok": False, "status": 0, "headers": {}, "error": "timeout"}


async def _task(store, artifacts, bounty=5000, preview="https://preview.example"):
    await artifacts.create_repo("r")
    artifacts.write_file("r", "server.py", "print(1)\n")
    spec = {
        "repo": "r", "title": "Add headers",
        "description": "Every tool response carries X-RateLimit-*.",
        "acceptance_tests": [{"name": "headers_present"}],
        "bounty_sats": bounty, "poster": "p",
        "funding_txid": "cd" * 32,
        "escrow_address": "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa",
    }
    task = await gitlib.post_task(store, spec)
    claim = await gitlib.claim_task(
        store, artifacts, task["task_id"], "worker1",
        "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa")
    await gitlib.submit_work(
        store, task["task_id"], claim["claim_id"], "worker1",
        preview, "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa")
    return task, claim


def check(name, cond):
    assert cond, name
    print(f"  [ok] {name}")


async def test_decide_review_threshold():
    d = autoverify.decide_review(PASS_REVIEW)
    check("confident pass not deferred", d["ruling"] == "pass" and not d["deferred"])
    d = autoverify.decide_review(LOW_REVIEW)
    check("low confidence defers", d["deferred"] is True)
    d = autoverify.decide_review({"ruling": {"choice": "maybe", "confidence": 0.99}})
    check("garbage ruling defers", d["deferred"] is True)


async def test_interpret_acceptance():
    check("ok true is pass",
          autoverify.interpret_acceptance_response(200, '{"ok": true}') == "pass")
    check("ok false is fail",
          autoverify.interpret_acceptance_response(200, '{"ok": false}') == "fail")
    check("mcp error envelope defers",
          autoverify.interpret_acceptance_response(
              200, '{"error": {"code": -32601}}') == "defer")
    check("404 defers",
          autoverify.interpret_acceptance_response(404, "nope") == "defer")
    check("503 defers",
          autoverify.interpret_acceptance_response(503, "") == "defer")
    check("PASS line passes",
          autoverify.interpret_acceptance_response(200, "PASS t — ok") == "pass")


async def test_auto_pass_records_auto_and_queues_merge():
    store = D1Store(FakeD1())
    await store.ensure_schema()
    artifacts = FakeArtifacts()
    task, claim = await _task(store, artifacts)
    q = FakeQueue()
    deps = {
        "http": fake_http_ok,
        "ai": FakeAI(PASS_REVIEW),
        "artifacts": artifacts,
        "sender": DryRunSender(),
        "enqueue": q.send,
        "sandbox": autoverify.FakeSandbox("pass", "PASS headers_present"),
        "auto_verify": True,
        "auto_verify_max_sats": 10000,
    }
    fork = await store.get_fork(claim["fork_id"])
    # consumer verify path
    await consumer_mod.process_message(
        store, deps, {"kind": "verify", "task_id": task["task_id"],
                      "fork_id": claim["fork_id"]})
    verifs = await store.verifications_for_fork(claim["fork_id"])
    check("verifier is auto",
          len(verifs) == 1 and verifs[0]["verifier"] == "auto")
    check("verdict pass", verifs[0]["verdict"] == "pass")
    ev = verifs[0].get("evidence") or {}
    check("evidence has test output", "PASS" in (ev.get("tests") or {}).get("output", ""))
    check("evidence has Clef ruling+confidence",
          (ev.get("clef") or {}).get("ruling") == "pass"
          and (ev.get("clef") or {}).get("confidence") >= 0.70)
    check("merge queued", q.sent and q.sent[0]["kind"] == "merge")
    check("sandbox ran", len(deps["sandbox"].calls) == 1)
    import board as board_mod
    page = await board_mod.render(store)
    check("board shows auto verifier", ">auto<" in page or ">auto</strong>" in page)
    check("board shows auto evidence", "auto evidence" in page and "Clef" in page)


async def test_auto_test_fail_rejects_fork():
    store = D1Store(FakeD1())
    await store.ensure_schema()
    artifacts = FakeArtifacts()
    task, claim = await _task(store, artifacts)
    q = FakeQueue()
    deps = {
        "http": fake_http_ok,
        "ai": FakeAI(PASS_REVIEW),
        "artifacts": artifacts,
        "sender": DryRunSender(),
        "enqueue": q.send,
        "sandbox": autoverify.FakeSandbox("fail", "FAIL headers_present — missing",
                                          exit_code=1),
        "auto_verify_max_sats": 10000,
    }
    await consumer_mod.process_message(
        store, deps, {"kind": "verify", "task_id": task["task_id"],
                      "fork_id": claim["fork_id"]})
    fork = await store.get_fork(claim["fork_id"])
    check("failing tests reject the fork", fork["status"] == "rejected")
    check("no merge queued", not q.sent)
    verifs = await store.verifications_for_fork(claim["fork_id"])
    check("auto fail has repro",
          verifs[0]["verdict"] == "fail" and verifs[0]["repro"])


async def test_clef_low_confidence_defers():
    store = D1Store(FakeD1())
    await store.ensure_schema()
    artifacts = FakeArtifacts()
    task, claim = await _task(store, artifacts)
    q = FakeQueue()
    deps = {
        "http": fake_http_ok,
        "ai": FakeAI(LOW_REVIEW),
        "artifacts": artifacts,
        "sender": DryRunSender(),
        "enqueue": q.send,
        "sandbox": autoverify.FakeSandbox("pass", "PASS all"),
        "auto_verify_max_sats": 10000,
    }
    await consumer_mod.process_message(
        store, deps, {"kind": "verify", "task_id": task["task_id"],
                      "fork_id": claim["fork_id"]})
    fork = await store.get_fork(claim["fork_id"])
    check("low Clef confidence leaves fork verifying",
          fork["status"] == "verifying")
    check("no auto attestation",
          await store.verifications_for_fork(claim["fork_id"]) == [])
    check("no merge queued", not q.sent)
    ledger = await store.ledger_for_task(task["task_id"])
    check("auto_verify_deferred ledger line",
          any(e["kind"] == "auto_verify_deferred" for e in ledger))


async def test_missing_ai_defers():
    store = D1Store(FakeD1())
    await store.ensure_schema()
    artifacts = FakeArtifacts()
    task, claim = await _task(store, artifacts)
    deps = {
        "http": fake_http_ok,
        "ai": None,
        "artifacts": artifacts,
        "sender": DryRunSender(),
        "enqueue": FakeQueue().send,
        "sandbox": autoverify.FakeSandbox("pass", "PASS"),
        "auto_verify_max_sats": 10000,
    }
    await consumer_mod.process_message(
        store, deps, {"kind": "verify", "task_id": task["task_id"],
                      "fork_id": claim["fork_id"]})
    check("missing AI defers (no attest)",
          await store.verifications_for_fork(claim["fork_id"]) == [])
    fork = await store.get_fork(claim["fork_id"])
    check("fork still verifying", fork["status"] == "verifying")


async def test_preview_unknown_protocol_defers():
    store = D1Store(FakeD1())
    await store.ensure_schema()
    artifacts = FakeArtifacts()
    task, claim = await _task(store, artifacts)

    async def mcpish(method, url, headers, timeout=10, body=None):
        return {"ok": True, "status": 200, "headers": {},
                "body": json.dumps({"jsonrpc": "2.0", "error": {"code": -32601}})}

    deps = {
        "http": mcpish,
        "ai": FakeAI(PASS_REVIEW),
        "artifacts": artifacts,
        "sender": DryRunSender(),
        "enqueue": FakeQueue().send,
        "auto_verify_max_sats": 10000,
    }
    await consumer_mod.process_message(
        store, deps, {"kind": "verify", "task_id": task["task_id"],
                      "fork_id": claim["fork_id"]})
    check("unknown protocol never default-allows",
          await store.verifications_for_fork(claim["fork_id"]) == [])
    ledger = await store.ledger_for_task(task["task_id"])
    check("deferred ledger",
          any(e["kind"] == "auto_verify_deferred" for e in ledger))


async def test_bounty_above_threshold_skips_auto():
    store = D1Store(FakeD1())
    await store.ensure_schema()
    artifacts = FakeArtifacts()
    task, claim = await _task(store, artifacts, bounty=50_000)
    sandbox = autoverify.FakeSandbox("pass", "PASS")
    deps = {
        "http": fake_http_ok,
        "ai": FakeAI(PASS_REVIEW),
        "artifacts": artifacts,
        "sender": DryRunSender(),
        "enqueue": FakeQueue().send,
        "sandbox": sandbox,
        "auto_verify_max_sats": 10000,
    }
    await consumer_mod.process_message(
        store, deps, {"kind": "verify", "task_id": task["task_id"],
                      "fork_id": claim["fork_id"]})
    check("high bounty did not run sandbox", sandbox.calls == [])
    check("high bounty waits for human verifier",
          await store.verifications_for_fork(claim["fork_id"]) == [])
    fork = await store.get_fork(claim["fork_id"])
    check("high bounty fork verifying", fork["status"] == "verifying")


async def test_clef_fail_after_green_tests():
    store = D1Store(FakeD1())
    await store.ensure_schema()
    artifacts = FakeArtifacts()
    task, claim = await _task(store, artifacts)
    q = FakeQueue()
    deps = {
        "http": fake_http_ok,
        "ai": FakeAI(FAIL_REVIEW),
        "artifacts": artifacts,
        "sender": DryRunSender(),
        "enqueue": q.send,
        "sandbox": autoverify.FakeSandbox("pass", "PASS all"),
        "auto_verify_max_sats": 10000,
    }
    await consumer_mod.process_message(
        store, deps, {"kind": "verify", "task_id": task["task_id"],
                      "fork_id": claim["fork_id"]})
    fork = await store.get_fork(claim["fork_id"])
    check("Clef fail rejects fork", fork["status"] == "rejected")
    check("no merge", not q.sent)
    verifs = await store.verifications_for_fork(claim["fork_id"])
    check("auto fail attestation",
          verifs and verifs[0]["verifier"] == "auto"
          and verifs[0]["verdict"] == "fail")


async def test_infra_crash_defers():
    store = D1Store(FakeD1())
    await store.ensure_schema()
    artifacts = FakeArtifacts()
    task, claim = await _task(store, artifacts)

    class Boom:
        async def run_tests(self, payload):
            raise RuntimeError("container exploded")

    deps = {
        "http": fake_http_down,
        "ai": FakeAI(PASS_REVIEW),
        "artifacts": artifacts,
        "sender": DryRunSender(),
        "enqueue": FakeQueue().send,
        "sandbox": Boom(),
        "auto_verify_max_sats": 10000,
    }
    await consumer_mod.process_message(
        store, deps, {"kind": "verify", "task_id": task["task_id"],
                      "fork_id": claim["fork_id"]})
    check("infra failure does not attest",
          await store.verifications_for_fork(claim["fork_id"]) == [])
    fork = await store.get_fork(claim["fork_id"])
    check("infra failure leaves verifying", fork["status"] == "verifying")


async def main():
    tests = [
        ("decide_review threshold", test_decide_review_threshold),
        ("interpret_acceptance", test_interpret_acceptance),
        ("auto pass records auto + queues merge",
         test_auto_pass_records_auto_and_queues_merge),
        ("auto test fail rejects", test_auto_test_fail_rejects_fork),
        ("clef low confidence defers", test_clef_low_confidence_defers),
        ("missing AI defers", test_missing_ai_defers),
        ("unknown protocol defers", test_preview_unknown_protocol_defers),
        ("bounty above threshold skips", test_bounty_above_threshold_skips_auto),
        ("clef fail after green tests", test_clef_fail_after_green_tests),
        ("infra crash defers", test_infra_crash_defers),
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
