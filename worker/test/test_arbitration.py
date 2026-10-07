#!/usr/bin/env python3
"""Tests for Clef arbitration (arbiter.py + gitlib disputes + consumer).

FakeAI serves canned Clef answers; FakeStore is an in-memory stand-in
with the same methods gitlib uses. No network, no account.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))
import arbiter  # noqa: E402
import gitlib  # noqa: E402
import consumer as consumer_mod  # noqa: E402


class FakeAI:
    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    async def run(self, model_ref, payload):
        self.calls.append((model_ref, payload))
        return {"answers": self.answers}


class FakeStore:
    def __init__(self):
        self.tasks = {}
        self.claims = {}
        self.forks = {}
        self.verifs = []
        self.disputes = {}
        self.ledger = []
        self.merges = []

    async def get_task(self, tid):
        t = self.tasks.get(tid)
        return dict(t) if t else None

    async def put_task(self, t):
        self.tasks[t["task_id"]] = dict(t)

    async def get_claim(self, cid):
        c = self.claims.get(cid)
        return dict(c) if c else None

    async def put_claim(self, c):
        self.claims[c["id"]] = dict(c)

    async def get_fork(self, fid):
        f = self.forks.get(fid)
        return dict(f) if f else None

    async def put_fork(self, f):
        self.forks[f["fork_id"]] = dict(f)

    async def forks_for_task(self, tid):
        return [dict(f) for f in self.forks.values()
                if f["task_id"] == tid]

    async def verifications_for_fork(self, fid):
        return [dict(v) for v in self.verifs if v["fork_id"] == fid]

    async def verifier_attested(self, fid, verifier):
        return any(v["fork_id"] == fid and v["verifier"] == verifier
                   for v in self.verifs)

    async def add_verification(self, v):
        self.verifs.append(dict(v))

    async def put_dispute(self, d):
        self.disputes[d["dispute_id"]] = dict(d)

    async def get_dispute(self, did):
        d = self.disputes.get(did)
        return dict(d) if d else None

    async def open_dispute_for_fork(self, fid):
        for d in self.disputes.values():
            if d["fork_id"] == fid and d["status"] == "open":
                return d
        return None

    async def ledger_add(self, kind, tid, agent, sats, detail):
        self.ledger.append({"kind": kind, "task_id": tid, "agent": agent,
                           "sats": sats, "detail": dict(detail)})

    async def merges_for_task(self, tid):
        return [m for m in self.merges if m["task_id"] == tid]


PASS_RULING = {
    "genuine": {"noul": 0.91},
    "ruling": {"choice": "uphold", "confidence": 0.88,
               "probabilities": {"uphold": 0.88, "overturn": 0.12}},
    "severity": {"score": 1.2},
}


def setup_rejected():
    s = FakeStore()
    s.tasks["t1"] = {"task_id": "t1", "status": "verifying",
                     "bounty_sats": 1000, "poster": "p"}
    s.claims["c1"] = {"id": "c1", "task_id": "t1", "agent": "worker1",
                      "status": "submitted"}
    s.forks["f1"] = {"fork_id": "f1", "task_id": "t1", "claim_id": "c1",
                     "status": "rejected"}
    s.verifs.append({"fork_id": "f1", "verifier": "v1", "verdict": "fail",
                     "stake_sats": 100, "repro": "run X"})
    return s


async def test_decide_uphold_confident():
    d = arbiter.decide_ruling(PASS_RULING)
    assert d["ruling"] == "uphold" and not d["deferred"], d
    assert abs(d["confidence"] - 0.88) < 1e-9


async def test_decide_low_confidence_defers():
    low = {"genuine": {"noul": 0.5},
           "ruling": {"choice": "overturn", "confidence": 0.55},
           "severity": {"score": 1.0}}
    d = arbiter.decide_ruling(low)
    assert d["deferred"] and "0.55" in d["reason"], d


async def test_decide_garbage_defers():
    d = arbiter.decide_ruling({"ruling": {"choice": "maybe",
                                          "confidence": 0.99}})
    assert d["deferred"], d


async def test_ask_clef_malformed():
    class BadAI:
        async def run(self, m, p):
            return {"nope": True}
    try:
        await arbiter.ask_clef(BadAI(), {}, arbiter.dispute_questions())
    except arbiter.ArbiterError:
        return
    raise AssertionError("expected ArbiterError")


async def test_ask_clef_no_binding():
    try:
        await arbiter.ask_clef(None, {}, arbiter.dispute_questions())
    except arbiter.ArbiterError as e:
        assert "not configured" in str(e), e
        return
    raise AssertionError("expected ArbiterError")


async def test_dispute_rejected_fork():
    s = setup_rejected()
    p = await gitlib.dispute_fork(s, "f1", "worker2", "tests lie", "run X")
    assert p["kind"] == "arbitrate", p
    assert s.forks["f1"]["status"] == "disputed"
    assert s.tasks["t1"]["status"] == "disputed"
    assert s.ledger[-1]["kind"] == "dispute_open"


async def test_dispute_needs_grounds_and_repro():
    s = setup_rejected()
    for g, r in (("", "run X"), ("bad", "")):
        try:
            await gitlib.dispute_fork(s, "f1", "worker2", g, r)
        except gitlib.GitError:
            continue
        raise AssertionError(f"expected refusal for grounds={g!r}")
    # also: worker cannot dispute own pass, attester cannot re-litigate
    s.forks["f1"]["status"] = "verifying"
    try:
        await gitlib.dispute_fork(s, "f1", "worker1", "g", "r")
    except gitlib.GitError as e:
        assert "own pass" in str(e), e
    else:
        raise AssertionError("expected own-pass refusal")
    try:
        await gitlib.dispute_fork(s, "f1", "v1", "g", "r")
    except gitlib.GitError as e:
        assert "re-votes" in str(e), e
    else:
        raise AssertionError("expected re-vote refusal")


async def test_dispute_double_and_wrong_state():
    s = setup_rejected()
    await gitlib.dispute_fork(s, "f1", "worker2", "g", "r")
    try:
        await gitlib.dispute_fork(s, "f1", "worker3", "g2", "r2")
    except gitlib.GitError as e:
        assert "already has an open dispute" in str(e), e
    else:
        raise AssertionError("expected double-dispute refusal")
    s2 = setup_rejected()
    s2.forks["f1"]["status"] = "working"
    try:
        await gitlib.dispute_fork(s2, "f1", "worker2", "g", "r")
    except gitlib.GitError as e:
        assert "decided fork" in str(e), e
    else:
        raise AssertionError("expected wrong-state refusal")


async def test_arbitrate_uphold_fail_stays_rejected():
    s = setup_rejected()
    p = await gitlib.dispute_fork(s, "f1", "worker2", "tests lie", "run X")
    ai = FakeAI(PASS_RULING)
    sent = []
    async def enq(m):
        sent.append(m)
    deps = {"ai": ai, "enqueue": enq,
            "http": None, "sender": None, "artifacts": None}
    await consumer_mod.process_message(store=s, deps=deps, msg={
        "kind": "arbitrate", "task_id": "t1", "fork_id": "f1",
        "dispute_id": p["dispute_id"]})
    assert ai.calls, "Clef was never asked"
    assert ai.calls[0][0] == "@cf/cloudflare/clef-flash", ai.calls[0][0]
    d = await s.get_dispute(p["dispute_id"])
    assert d["status"] == "decided" and d["ruling"] == "uphold", d
    assert s.forks["f1"]["status"] == "rejected"  # fail verdict stands
    assert s.tasks["t1"]["status"] == "open"  # bounty back in play
    assert not sent, "no merge for an upheld fail"


async def test_arbitrate_overturn_fail_requeues_merge():
    s = setup_rejected()
    p = await gitlib.dispute_fork(s, "f1", "worker2", "tests lie", "run X")
    over = {"genuine": {"noul": 0.9},
            "ruling": {"choice": "overturn", "confidence": 0.93},
            "severity": {"score": 2.0}}
    sent = []
    async def enq(m):
        sent.append(m)
    deps = {"ai": FakeAI(over), "enqueue": enq,
            "http": None, "sender": None, "artifacts": None}
    await consumer_mod.process_message(store=s, deps=deps, msg={
        "kind": "arbitrate", "task_id": "t1", "fork_id": "f1",
        "dispute_id": p["dispute_id"]})
    assert s.forks["f1"]["status"] == "verifying"
    assert sent and sent[0]["kind"] == "merge", sent


async def test_arbitrate_low_confidence_defers():
    s = setup_rejected()
    p = await gitlib.dispute_fork(s, "f1", "worker2", "tests lie", "run X")
    low = {"genuine": {"noul": 0.5},
           "ruling": {"choice": "uphold", "confidence": 0.4},
           "severity": {"score": 1.0}}
    sent = []
    async def enq(m):
        sent.append(m)
    deps = {"ai": FakeAI(low), "enqueue": enq,
            "http": None, "sender": None, "artifacts": None}
    await consumer_mod.process_message(store=s, deps=deps, msg={
        "kind": "arbitrate", "task_id": "t1", "fork_id": "f1",
        "dispute_id": p["dispute_id"]})
    d = await s.get_dispute(p["dispute_id"])
    assert d["status"] == "deferred", d
    assert s.forks["f1"]["status"] == "disputed"  # frozen, human decides
    assert not sent


async def main():
    tests = [
        ("decide uphold confident", test_decide_uphold_confident),
        ("decide low confidence defers", test_decide_low_confidence_defers),
        ("decide garbage defers", test_decide_garbage_defers),
        ("ask_clef malformed", test_ask_clef_malformed),
        ("ask_clef no binding", test_ask_clef_no_binding),
        ("dispute rejected fork", test_dispute_rejected_fork),
        ("dispute needs grounds+repro", test_dispute_needs_grounds_and_repro),
        ("dispute double+wrong state", test_dispute_double_and_wrong_state),
        ("arbitrate uphold fail", test_arbitrate_uphold_fail_stays_rejected),
        ("arbitrate overturn requeues", test_arbitrate_overturn_fail_requeues_merge),
        ("arbitrate low conf defers", test_arbitrate_low_confidence_defers),
    ]
    passed = failed = 0
    for name, fn in tests:
        try:
            await fn()
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
