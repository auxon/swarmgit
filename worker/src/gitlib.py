"""SwarmGit task lifecycle: bounty-coordinated code forge state machine.

Adapted from the TestSwarm scaffold's swarmlib.py economics
(post/list/run/verify/settle, stake-to-test, reproduce-to-release,
per-task escrow, reputation) to coding tasks on Artifacts forks:

  open → claimed → working → submitted → verifying → merging → settled
     ↘ expired (bounty returned)      ↘ disputed → Clef | human close

Settlement is dry-run by default (DryRunSender). Real sats move only
through AgentPaySender (flag + AGENTPAY_KEY) or the walletd puller,
and only when a txid (on-chain) or receipt (internal apw_ credit)
comes back. A failing send leaves the release pending — never marked
paid.
"""
import re
import time

import artifacts as artifacts_mod

AUTO_VERIFIER = "auto"


class GitError(Exception):
    pass


def nid(prefix):
    import secrets
    return prefix + secrets.token_hex(8)



_B58 = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
# Public wallet id from agentpay GET /agent/me (newId("apw")), not the
# 64-hex secret token. Same shapes as auxon/agentpay parseSendDest.
_WALLET_ID = re.compile(r"^apw_[0-9A-Za-z]{8,48}$")
_WALLET_TOKEN = re.compile(r"^apw_[0-9a-fA-F]{64}$")
_RECEIPT_ID = re.compile(r"^snd_[0-9A-Za-z_-]{4,128}$")


def validate_funding_txid(txid):
    """BSV tx that funded escrow. 64 hex chars, no 0x prefix."""
    txid = (txid or "").strip().lower()
    if not is_bsv_txid(txid):
        raise GitError(
            "refused: funding_txid is required — pay SWARMSGIT_PAY_ADDRESS"
            " the bounty first, then post the 64-char txid")
    return txid


def is_bsv_txid(txid):
    """On-chain BSV txid: 64 hex chars, no 0x. Never an snd_ receipt."""
    t = (txid or "").strip().lower()
    return len(t) == 64 and all(c in "0123456789abcdef" for c in t)


def is_agentpay_receipt_id(rid):
    """agentpay internal send id (snd_…), not a BSV tx."""
    return bool(_RECEIPT_ID.match((rid or "").strip()))


def is_agentpay_wallet_token(addr):
    """Secret apw_ Bearer token (apw_ + 64 hex). Never a pay dest."""
    return bool(_WALLET_TOKEN.match((addr or "").strip()))


def is_agentpay_wallet_id(addr):
    addr = (addr or "").strip()
    if is_agentpay_wallet_token(addr):
        return False
    return bool(_WALLET_ID.match(addr))


def is_bsv_address(addr):
    addr = (addr or "").strip()
    return bool(addr.startswith(("1", "3")) and 26 <= len(addr) <= 35
                and all(c in _B58 for c in addr))


def is_payout_dest(addr):
    addr = (addr or "").strip()
    return is_bsv_address(addr) or is_agentpay_wallet_id(addr)


def validate_pay_address(addr):
    """BSV receive address (legacy base58, 1/3) or public agentpay
    wallet id (apw_… from GET /agent/me). Secret 64-hex wallet tokens
    are refused so a leaked Bearer cannot become a pay address."""
    addr = (addr or "").strip()
    if is_agentpay_wallet_token(addr):
        raise GitError(
            "refused: pay_address looks like a secret wallet token; "
            "pass the public wallet id (apw_… from GET /agent/me), "
            "never the 64-hex token")
    if is_bsv_address(addr) or is_agentpay_wallet_id(addr):
        return addr
    raise GitError(
        "refused: pay_address must be a bitcoin address (BSV, base58,"
        " starts with 1 or 3) or a public agentpay wallet id (apw_…)")


def validate_agent_name(agent):
    agent = (agent or "").strip()
    if not agent:
        raise GitError("refused: agent name is required")
    if agent == AUTO_VERIFIER:
        raise GitError("refused: agent name 'auto' is reserved"
                       " for the automatic verifier")
    return agent


WORKER_SHARE = 0.80   # bounty fraction to the winning worker agent
VERIFIER_SHARE = 0.20  # split evenly across passing verifiers


def split_bounty(bounty_sats, verifiers):
    """(worker_sats, {verifier: sats}). Deterministic, documented."""
    worker = int(bounty_sats * WORKER_SHARE)
    rest = bounty_sats - worker
    per = rest // max(1, len(verifiers))
    return worker, {v: per for v in verifiers}


# ---------------------------------------------------------------- post

def validate_spec(spec):
    """Free shape checks for a task spec. Runs before any network/chain
    work so malformed posts fail fast with a spec error, not a chain
    error."""
    repo = (spec.get("repo") or "").strip()
    if not repo:
        raise GitError("refused: repo is required (Artifacts repo name)")
    title = (spec.get("title") or "").strip()
    if not title:
        raise GitError("refused: title is required")
    tests = spec.get("acceptance_tests")
    if not isinstance(tests, list) or not tests:
        raise GitError("refused: acceptance_tests must be a non-empty list")
    for i, t in enumerate(tests):
        if not isinstance(t, dict) or not t.get("name"):
            raise GitError(f"refused: acceptance_tests[{i}] needs a 'name'")
    bounty = spec.get("bounty_sats")
    if not isinstance(bounty, int) or isinstance(bounty, bool) \
            or bounty < 0:
        raise GitError("refused: bounty_sats must be a non-negative int")
    if bounty <= 0:
        raise GitError("refused: bounty_sats must be funded — post a"
                       " positive bounty after paying escrow")


async def post_task(store, spec, task_id=None):
    """Validate a task spec, create the task, lock escrow (dry-run)."""
    validate_spec(spec)
    repo = (spec.get("repo") or "").strip()
    title = (spec.get("title") or "").strip()
    tests = spec.get("acceptance_tests")
    bounty = spec.get("bounty_sats")
    funding_txid = validate_funding_txid(spec.get("funding_txid"))
    escrow_address = (spec.get("escrow_address") or "").strip()
    task_id = task_id or nid("task_")
    task = {
        "task_id": task_id,
        "status": "open",
        "repo": repo,
        "bounty_sats": bounty,
        "poster": spec.get("poster", "anon"),
        "title": title,
        "description": spec.get("description", ""),
        "acceptance_tests": tests,
        "deadline_at": int(spec.get("deadline_at", 0)),
        "funding_txid": funding_txid,
        "escrow_address": escrow_address,
        "created_at": int(time.time()),
    }
    await store.put_task(task)
    await store.ledger_add("escrow_lock", task_id, task["poster"], bounty,
                           {"note": "bounty funded into escrow at post",
                            "funding_txid": funding_txid,
                            "escrow_address": escrow_address,
                            "settlement": "dry_run"})
    return task


# ---------------------------------------------------------------- claim

async def claim_task(store, artifacts, task_id, agent, pay_address=""):
    """Agent claims a task: exactly one claim per agent (DDL UNIQUE),
    mints a private Artifacts fork with a short-lived repo-scoped token."""
    agent = validate_agent_name(agent)
    task = await store.get_task(task_id)
    if not task:
        raise GitError("refused: task not found")
    if task["status"] not in ("open", "claimed"):
        raise GitError(f"refused: task is {task['status']}, not claimable")
    if await store.claim_exists(task_id, agent):
        raise GitError("refused: double-claim — this agent already has a"
                       f" claim on {task_id}")
    claim_id = nid("claim_")
    fork_name = f"{task['repo']}-task-{task_id[-6:]}-{agent[:16]}"
    try:
        fork = await artifacts.fork_repo(task["repo"], fork_name)
    except artifacts_mod.ArtifactsError as e:
        raise GitError(f"refused: fork failed: {e}")
    token = await artifacts.issue_token(fork["repo_name"], ttl_s=86400)
    addr = validate_pay_address(pay_address) if (pay_address or "").strip() else ""
    claim = {
        "id": claim_id,
        "task_id": task_id,
        "agent": agent,
        "status": "active",
        "pay_address": addr,
        "fork_id": fork["fork_id"],
        # last4 only — the full token is shown once in the claim_task
        # response and never persisted anywhere (board-safe).
        "repo_token_last4": token[-4:] if token else "",
        "created_at": int(time.time()),
    }
    fork_rec = {
        "fork_id": fork["fork_id"],
        "task_id": task_id,
        "claim_id": claim_id,
        "repo_name": fork["repo_name"],
        "preview_url": "",
        "status": "working",
        "created_at": int(time.time()),
    }
    await store.put_claim(claim)
    await store.put_fork(fork_rec)
    if task["status"] == "open":
        task["status"] = "claimed"
        await store.put_task(task)
    return {"claim_id": claim_id, "fork_id": fork["fork_id"],
            "repo_name": fork["repo_name"],
            "repo_token_last4": token[-4:] if token else "",
            "_token": token}  # handed to the claiming agent only


# ---------------------------------------------------------------- submit

async def submit_work(store, task_id, claim_id, agent, preview_url="", pay_address=""):
    """Agent submits a fork for verification. Returns the queue payload
    the caller should enqueue (kind=verify)."""
    claim = await store.get_claim(claim_id)
    if not claim or claim["task_id"] != task_id:
        raise GitError("refused: claim not found for this task")
    if claim["agent"] != agent:
        raise GitError("refused: claim belongs to another agent")
    if claim["status"] != "active":
        raise GitError(f"refused: claim is {claim['status']}, not active")
    # Completion requires a receive address. A claim may already carry
    # one; submit may set or replace it. Settlement pays this address.
    supplied = (pay_address or "").strip()
    if supplied:
        claim["pay_address"] = validate_pay_address(supplied)
    if not (claim.get("pay_address") or "").strip():
        raise GitError(
            "refused: pay_address is required to complete a task"
            " (BSV address or public apw_ wallet id the agent receives on)")
    fork = await store.get_fork(claim["fork_id"])
    if not fork or fork["status"] != "working":
        raise GitError("refused: fork is not in working state")
    fork["status"] = "submitted"
    if preview_url:
        fork["preview_url"] = preview_url
    await store.put_fork(fork)
    claim["status"] = "submitted"
    await store.put_claim(claim)
    task = await store.get_task(task_id)
    task["status"] = "submitted"
    await store.put_task(task)
    return {"kind": "verify", "task_id": task_id,
            "fork_id": fork["fork_id"]}


# ---------------------------------------------------------------- verify

async def attest_verification(store, fork_id, verifier, verdict,
                              stake_sats=0, repro="", evidence=None):
    """Verifier attests pass/fail on a fork. Stake is locked (dry-run).
    A fail verdict MUST include a repro (reproduce-to-release).
    Returns {"ready_for_merge": bool} — first verified pass wins.
    evidence: optional dict recorded on the verification (auto-verify
    stores test output + Clef ruling here)."""
    verifier = (verifier or "").strip() or "anon"
    fork = await store.get_fork(fork_id)
    if not fork:
        raise GitError("refused: fork not found")
    if fork["status"] not in ("submitted", "verifying"):
        raise GitError(f"refused: fork is {fork['status']}, not verifiable")
    claim = await store.get_claim(fork["claim_id"])
    if claim and claim["agent"] == verifier:
        raise GitError("refused: cannot verify your own work")
    if await store.verifier_attested(fork_id, verifier):
        raise GitError("refused: this verifier already attested this fork")
    if verdict not in ("pass", "fail"):
        raise GitError("refused: verdict must be 'pass' or 'fail'")
    if verdict == "fail" and not (repro or "").strip():
        raise GitError("refused: a fail verdict must include a repro"
                       " (reproduce-to-release)")
    if not isinstance(stake_sats, int) or isinstance(stake_sats, bool) \
            or stake_sats < 0:
        raise GitError("refused: stake_sats must be a non-negative int")
    verif = {
        "fork_id": fork_id,
        "verifier": verifier,
        "verdict": verdict,
        "stake_sats": stake_sats,
        "repro": repro or "",
        "created_at": int(time.time()),
    }
    if evidence:
        verif["evidence"] = evidence
    await store.add_verification(verif)
    await store.ledger_add("stake_lock", fork["task_id"], verifier,
                           stake_sats,
                           {"fork_id": fork_id, "verdict": verdict,
                            "settlement": "dry_run"})
    await store.bump_reputation(verifier, tasks_verified=1)
    if verdict == "fail":
        fork["status"] = "rejected"
        await store.put_fork(fork)
        if claim:
            claim["status"] = "lost"
            await store.put_claim(claim)
        await store.bump_reputation(verifier, score=1)
        return {"ok": True, "verdict": "fail", "ready_for_merge": False}
    # pass: first verified pass wins the task
    task = await store.get_task(fork["task_id"])
    existing = await store.merges_for_task(task["task_id"])
    if existing:
        # task already decided; this pass is recorded but moot
        return {"ok": True, "verdict": "pass", "ready_for_merge": False,
                "note": "task already has a merge; attestation recorded"}
    fork["status"] = "verifying"
    await store.put_fork(fork)
    task["status"] = "verifying"
    await store.put_task(task)
    await store.bump_reputation(verifier, score=2)
    return {"ok": True, "verdict": "pass", "ready_for_merge": True,
            "merge_payload": {"kind": "merge", "task_id": task["task_id"],
                              "fork_id": fork_id}}


# ---------------------------------------------------------------- merge

async def record_merge(store, task_id, fork_id, audit_report, why_refs,
                       decision="merged"):
    """Persist the merge decision. Called by the queue consumer after
    the merge gate runs. why_refs: list of why-entry ids."""
    merge_id = nid("merge_")
    fork = await store.get_fork(fork_id)
    task = await store.get_task(task_id)
    merge = {
        "merge_id": merge_id,
        "task_id": task_id,
        "fork_id": fork_id,
        "decision": decision,
        "audit_report": audit_report or {},
        "why_refs": why_refs or [],
        "merged_at": int(time.time()),
    }
    await store.put_merge(merge)
    if decision == "merged":
        fork["status"] = "merged"
        task["status"] = "merging"
        claim = await store.get_claim(fork["claim_id"])
        if claim:
            claim["status"] = "won"
            await store.put_claim(claim)
        # losing forks: mark lost
        for f in await store.forks_for_task(task_id):
            if f["fork_id"] != fork_id and f["status"] in (
                    "submitted", "verifying", "working"):
                f["status"] = "rejected"
                await store.put_fork(f)
                c = await store.get_claim(f["claim_id"])
                if c and c["status"] in ("active", "submitted"):
                    c["status"] = "lost"
                    await store.put_claim(c)
    else:  # gate_blocked
        fork["status"] = "gate_blocked"
        task["status"] = "open"  # bounty stays available for other forks
        claim = await store.get_claim(fork["claim_id"])
        if claim:
            claim["status"] = "gate_blocked"
            await store.put_claim(claim)
    await store.put_fork(fork)
    await store.put_task(task)
    return merge


# ---------------------------------------------------------------- settle

def payout_idempotency_key(task_id, role, who=""):
    who = (who or "").strip()
    if who:
        return f"swarmgit:{task_id}:{role}:{who}"[:120]
    return f"swarmgit:{task_id}:{role}"[:120]


async def _agent_pay_dest(store, agent):
    getter = getattr(store, "agent_pay_address", None)
    if not callable(getter):
        return ""
    try:
        return (await getter(agent) or "").strip()
    except Exception:
        return ""


def _paid_proof(settlement, txid=None, receipt_id=None):
    """Normalize a paid send. Internal is a receipt, live is a BSV txid."""
    settlement = (settlement or "").strip()
    rid = (receipt_id or "").strip()
    txid = (txid or "").strip()
    if settlement == "internal":
        rid = rid or txid
        if not rid:
            return None
        return {"settlement": "internal", "receipt_id": rid, "txid": None}
    if settlement == "live":
        if not is_bsv_txid(txid):
            return None
        return {"settlement": "live", "txid": txid.lower(), "receipt_id": None}
    return None


async def _existing_paid(store, task_id, agent):
    getter = getattr(store, "paid_payout", None)
    if callable(getter):
        try:
            hit = await getter(task_id, agent)
            if isinstance(hit, dict) and hit.get("settlement"):
                return _paid_proof(hit.get("settlement"),
                                   hit.get("txid"), hit.get("receipt_id"))
        except Exception:
            pass
    getter = getattr(store, "live_payout_txid", None)
    if not callable(getter):
        return None
    try:
        existing = await getter(task_id, agent)
    except Exception:
        return None
    if not existing:
        return None
    if is_agentpay_receipt_id(existing):
        return _paid_proof("internal", receipt_id=existing)
    if is_bsv_txid(existing):
        return _paid_proof("live", txid=existing)
    return None


async def _send_payout(store, sender, task_id, agent, dest, sats, role, memo,
                       live_mode):
    """Send one payout. Never reports paid without a txid or receipt.

    Returns (receipt_or_none, pending_reason). pending_reason set means
    this send must not mark the task paid.
    """
    existing = await _existing_paid(store, task_id, agent)
    if existing:
        return ({"ok": True, "to": dest, "agent": agent, "sats": sats,
                 "idempotent": True, **existing}, None)
    if live_mode and dest and not is_payout_dest(dest):
        await store.ledger_add(
            "payout_skipped", task_id, agent, sats,
            {"role": role, "to": dest, "reason": "dest is not a BSV address"
             " or public apw_ wallet id", "settlement": "pending"})
        return None, "invalid dest for live send"
    key = payout_idempotency_key(task_id, role, agent)
    try:
        r = await sender.send(dest, sats, memo, key)
    except Exception as e:
        await store.ledger_add(
            "payout_failed", task_id, agent, sats,
            {"role": role, "to": dest, "error": str(e)[:300],
             "idempotency_key": key, "settlement": "pending"})
        return None, str(e)
    settlement = (r.get("settlement") or "dry_run").strip()
    if settlement in ("live", "internal"):
        proof = _paid_proof(settlement, r.get("txid"), r.get("receipt_id"))
        if not proof:
            err = ("internal send returned no receipt" if settlement == "internal"
                   else "live send returned no txid")
            await store.ledger_add(
                "payout_failed", task_id, agent, sats,
                {"role": role, "to": dest, "error": err,
                 "idempotency_key": key, "settlement": "pending"})
            return None, err
        settlement = proof["settlement"]
        txid = proof.get("txid")
        receipt_id = proof.get("receipt_id")
    elif settlement == "dry_run":
        txid = None
        receipt_id = None
    else:
        await store.ledger_add(
            "payout_failed", task_id, agent, sats,
            {"role": role, "to": dest,
             "error": "unknown settlement %s" % settlement,
             "idempotency_key": key, "settlement": "pending"})
        return None, "unknown settlement"
    detail = {"role": role, "pay_address": dest,
              "settlement": settlement, "idempotency_key": key}
    if settlement == "live" and txid:
        detail["txid"] = txid
    if settlement == "internal" and receipt_id:
        detail["receipt_id"] = receipt_id
    receipt = {"to": dest, "agent": agent, "sats": sats,
               "settlement": settlement, "txid": txid,
               "receipt_id": receipt_id}
    await store.ledger_add("payout", task_id, agent, sats, detail)
    return receipt, None


def _winner_paid(payouts, winner):
    for p in payouts:
        if p.get("agent") != winner:
            continue
        if p.get("settlement") == "live" and p.get("txid"):
            return p
        if p.get("settlement") == "internal" and (p.get("receipt_id") or p.get("txid")):
            return p
    return None


async def settle_task(store, sender, task_id, idempotency_key=""):
    """Release escrow to the winner (and passing verifiers).

    DryRunSender: local ledger only, task becomes settled, no txid.
    Paying sender: task becomes settled only when the winner payout has
    a BSV txid (settlement=live) or an agentpay receipt (settlement=
    internal). A failing or empty send leaves the task merging and the
    release pending. Repeating settle with the same keys will not
    double-pay (ledger receipt/txid short-circuit + agentpay idempotency).
    """
    task = await store.get_task(task_id)
    if not task:
        raise GitError("refused: task not found")
    if task["status"] != "merging":
        raise GitError(f"refused: task is {task['status']}, not merging")
    merges = await store.merges_for_task(task_id)
    merged = [m for m in merges if m.get("decision") == "merged"]
    if not merged:
        raise GitError("refused: no merged fork to settle")
    winner_fork_id = merged[0]["fork_id"]
    winner_fork = await store.get_fork(winner_fork_id)
    winner_claim = await store.get_claim(winner_fork["claim_id"])
    winner = winner_claim["agent"]
    dest = (winner_claim.get("pay_address") or "").strip()
    already = await _existing_paid(store, task_id, winner)
    if already:
        task["status"] = "settled"
        await store.put_task(task)
        return {"ok": True, "task_id": task_id, "dry_run": False,
                "funds_moved": True, "pending": False,
                "bounty_sats": int(task["bounty_sats"]),
                "funding_txid": task.get("funding_txid", ""),
                "winner": winner,
                "payouts": [{"to": dest, "agent": winner,
                             "sats": int(task["bounty_sats"]),
                             "idempotent": True, **already}],
                "note": "idempotent: payout already recorded"}
    if not dest:
        raise GitError(
            "refused: winning agent has no pay address — agents must"
            " provide one when they complete the task")
    verifs = await store.verifications_for_fork(winner_fork_id)
    passing = sorted({v["verifier"] for v in verifs
                      if v.get("verdict") == "pass"
                      and v.get("verifier") != AUTO_VERIFIER})
    bounty = int(task["bounty_sats"])
    if passing:
        worker_sats, verifier_sats = split_bounty(bounty, passing)
    else:
        worker_sats, verifier_sats = bounty, {}
    live_mode = getattr(sender, "settlement_mode", "dry_run") == "live"
    payouts = []
    r, pending = await _send_payout(
        store, sender, task_id, winner, dest, worker_sats, "winner",
        f"swarmgit bounty: {task_id} winner", live_mode)
    if r:
        r["funding_txid"] = task.get("funding_txid", "")
        payouts.append(r)
    for v, sats in verifier_sats.items():
        vdest = await _agent_pay_dest(store, v) or v
        rv, _vpend = await _send_payout(
            store, sender, task_id, v, vdest, sats, "verifier",
            f"swarmgit verifier: {task_id}", live_mode)
        if rv:
            payouts.append(rv)
    if pending and live_mode:
        return {"ok": False, "pending": True, "task_id": task_id,
                "funds_moved": False, "dry_run": False,
                "bounty_sats": bounty,
                "funding_txid": task.get("funding_txid", ""),
                "winner": winner, "payouts": payouts,
                "error": pending,
                "note": "release pending — send did not return a"
                        " txid or receipt; not marked paid"}
    paid = _winner_paid(payouts, winner)
    settlement = (paid or {}).get("settlement") or "dry_run"
    await store.ledger_add("escrow_release", task_id, task["poster"],
                           bounty,
                           {"note": "escrow released to payouts",
                            "settlement": settlement})
    await store.bump_reputation(winner, score=10, tasks_won=1)
    task["status"] = "settled"
    await store.put_task(task)
    if settlement == "live":
        note = "live settlement (on-chain BSV tx)"
    elif settlement == "internal":
        note = "internal settlement (agentpay receipt, not a BSV tx)"
    else:
        note = ("DRY-RUN. Ledger entries are local accounting."
                " Live sats move via AgentPaySender or the"
                " walletd puller.")
    return {"ok": True, "task_id": task_id, "dry_run": paid is None,
            "funds_moved": paid is not None, "bounty_sats": bounty,
            "funding_txid": task.get("funding_txid", ""),
            "winner": winner, "payouts": payouts,
            "pending": False, "note": note}


# ---------------------------------------------------------------- expiry

async def maybe_expire(store, task_id, now=None):
    """Mark a task expired past its deadline; bounty returns to poster
    (dry-run ledger). Returns True if it expired."""
    now = int(now or time.time())
    task = await store.get_task(task_id)
    if not task or task["status"] in ("settled", "expired"):
        return False
    if not task.get("deadline_at") or now < int(task["deadline_at"]):
        return False
    task["status"] = "expired"
    await store.put_task(task)
    await store.ledger_add("escrow_release", task_id, task["poster"],
                           int(task["bounty_sats"]),
                           {"note": "bounty returned on expiry",
                            "settlement": "dry_run"})
    return True


# ---------------------------------------------------------------- disputes
# Fork-level arbitration decided by Clef (see arbiter.py). Adapted from
# the winning pilot fork: the neutral-arbiter panel is replaced by the
# decision model; low confidence defers to a human operator.

async def dispute_fork(store, fork_id, disputer, grounds="", repro=""):
    """Contest a fork's verification outcome. Decided forks only
    (rejected | verifying); grounds AND repro required
    (reproduce-to-release applies to disputes too); the worker cannot
    dispute their own pass; attesters cannot re-litigate; one open
    dispute per fork. Freezes fork + task at disputed and returns the
    queue payload (kind=arbitrate) the caller should enqueue."""
    disputer = (disputer or "").strip() or "anon"
    fork = await store.get_fork(fork_id)
    if not fork:
        raise GitError("refused: fork not found")
    if await store.open_dispute_for_fork(fork_id):
        raise GitError("refused: fork already has an open dispute")
    if fork["status"] not in ("rejected", "verifying"):
        raise GitError(f"refused: fork is {fork['status']} — only a"
                       " decided fork (rejected | verifying) can be"
                       " disputed")
    claim = await store.get_claim(fork["claim_id"])
    worker = claim["agent"] if claim else ""
    contested = "fail" if fork["status"] == "rejected" else "pass"
    if disputer == worker and contested == "pass":
        raise GitError("refused: you cannot dispute your own pass")
    if await store.verifier_attested(fork_id, disputer):
        raise GitError("refused: you already attested this fork —"
                       " Clef arbitrates, not re-votes")
    grounds = (grounds or "").strip()
    repro = (repro or "").strip()
    if not grounds:
        raise GitError("refused: a dispute needs grounds"
                       " (what is contested)")
    if not repro:
        raise GitError("refused: a dispute needs a repro"
                       " (reproduce-to-release)")
    dispute = {
        "dispute_id": nid("dispute_"),
        "fork_id": fork_id,
        "task_id": fork["task_id"],
        "disputer": disputer,
        "grounds": grounds,
        "repro": repro,
        "contested": contested,
        "status": "open",
        "ruling": "",
        "decided_at": 0,
    }
    await store.put_dispute(dispute)
    fork["status"] = "disputed"
    await store.put_fork(fork)
    task = await store.get_task(fork["task_id"])
    task["status"] = "disputed"
    await store.put_task(task)
    await store.ledger_add("dispute_open", fork["task_id"], disputer, 0,
                           {"fork_id": fork_id,
                            "contested_verdict": contested,
                            "settlement": "dry_run"})
    return {"kind": "arbitrate", "task_id": fork["task_id"],
            "fork_id": fork_id, "dispute_id": dispute["dispute_id"]}


# A bad dispute is an uphold: the contested verdict stands, so the
# disputer was wrong. Slash is dry-run ledger + reputation. Floor 100
# sats, otherwise 10% of the task bounty.
SLASH_SCORE = 5
SLASH_SATS_FLOOR = 100


def dispute_slash_sats(bounty):
    try:
        bounty = int(bounty or 0)
    except (TypeError, ValueError):
        bounty = 0
    return max(SLASH_SATS_FLOOR, bounty // 10)


async def apply_arbitration(store, dispute_id, ruling, confidence,
                            genuine=0.0, severity=0, deferred=False,
                            note="", actor="clef"):
    """Apply a Clef or human ruling (or record a deferral). uphold =
    contested verdict stands (bad dispute, disputer slashed); overturn
    = flipped. Returns the outcome record."""
    dispute = await store.get_dispute(dispute_id)
    if not dispute:
        raise GitError("refused: dispute not found")
    if dispute["status"] != "open":
        raise GitError("refused: dispute is already decided")
    fork = await store.get_fork(dispute["fork_id"])
    task = await store.get_task(dispute["task_id"])
    if deferred or ruling not in ("uphold", "overturn"):
        dispute["status"] = "deferred"
        await store.put_dispute(dispute)
        await store.ledger_add("arbitration_deferred", dispute["task_id"],
                               "clef", 0,
                               {"dispute_id": dispute_id,
                                "confidence": confidence,
                                "note": note or "low confidence",
                                "settlement": "dry_run"})
        return {"ok": True, "dispute_id": dispute_id, "deferred": True}
    contested = dispute.get("contested", "") or "pass"
    if ruling == "uphold":
        new_fork = "rejected" if contested == "fail" else "verifying"
        new_task = "open" if contested == "fail" else "verifying"
    else:  # overturn
        new_fork = "verifying" if contested == "fail" else "rejected"
        new_task = "verifying" if contested == "fail" else "open"
    dispute["status"] = "decided"
    dispute["ruling"] = ruling
    dispute["decided_at"] = int(time.time())
    await store.put_dispute(dispute)
    if fork:
        fork["status"] = new_fork
        await store.put_fork(fork)
    if task:
        task["status"] = new_task
        await store.put_task(task)
    await store.ledger_add("dispute_resolved", dispute["task_id"], actor,
                           0, {"dispute_id": dispute_id, "ruling": ruling,
                               "confidence": confidence, "genuine": genuine,
                               "severity": severity, "actor": actor,
                               "settlement": "dry_run"})
    slashed = 0
    if ruling == "uphold":
        slashed = dispute_slash_sats(
            (task or {}).get("bounty_sats", 0))
        await store.ledger_add(
            "dispute_slash", dispute["task_id"], dispute["disputer"],
            slashed, {"dispute_id": dispute_id, "reason": "bad dispute",
                      "ruling": ruling, "actor": actor,
                      "settlement": "dry_run"})
        await store.bump_reputation(
            dispute["disputer"], score=-SLASH_SCORE, false_reports=1)
    return {"ok": True, "dispute_id": dispute_id, "ruling": ruling,
            "fork_id": dispute["fork_id"], "task_id": dispute["task_id"],
            "fork_status": new_fork, "task_status": new_task,
            "needs_merge": new_fork == "verifying",
            "slashed_sats": slashed, "bad_dispute": ruling == "uphold",
            "actor": actor}


async def close_deferred_dispute(store, dispute_id, ruling, operator,
                                 note=""):
    """Human close of a deferred dispute. Same uphold/overturn outcomes
    as Clef, including the slash on a bad (upheld) dispute. Does not
    touch an open dispute — that one is Clef's."""
    operator = (operator or "").strip() or "operator"
    dispute = await store.get_dispute(dispute_id)
    if not dispute:
        raise GitError("refused: dispute not found")
    if dispute["status"] != "deferred":
        raise GitError(
            f"refused: dispute is {dispute['status']}, not deferred")
    ruling = (ruling or "").strip()
    if ruling not in ("uphold", "overturn"):
        raise GitError("refused: ruling must be uphold or overturn")
    dispute["status"] = "open"
    await store.put_dispute(dispute)
    return await apply_arbitration(
        store, dispute_id, ruling, 1.0, deferred=False,
        note=f"human:{operator}: {(note or '').strip()}",
        actor=operator)
