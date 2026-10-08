"""SwarmGit task lifecycle: bounty-coordinated code forge state machine.

Adapted from the TestSwarm scaffold's swarmlib.py economics
(post/list/run/verify/settle, stake-to-test, reproduce-to-release,
per-task escrow, reputation) to coding tasks on Artifacts forks:

  open → claimed → working → submitted → verifying → merging → settled
     ↘ expired (bounty returned)      ↘ disputed → Clef | human close

Settlement is DRY-RUN ONLY until Richard's POST /agent/send primitive
exists. Every ledger event carries settlement="dry_run"; the Sender
interface (sender.py) is real-ready, not a stub.
"""
import time

import artifacts as artifacts_mod


class GitError(Exception):
    pass


def nid(prefix):
    import secrets
    return prefix + secrets.token_hex(8)



_B58 = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")


def validate_pay_address(addr):
    """BSV receive address. Legacy base58, starts with 1 or 3."""
    addr = (addr or "").strip()
    if not addr.startswith(("1", "3")) or not (26 <= len(addr) <= 35) \
            or any(c not in _B58 for c in addr):
        raise GitError(
            "refused: pay_address must be a bitcoin address the agent"
            " can receive on (BSV, base58, starts with 1 or 3)")
    return addr


WORKER_SHARE = 0.80   # bounty fraction to the winning worker agent
VERIFIER_SHARE = 0.20  # split evenly across passing verifiers


def split_bounty(bounty_sats, verifiers):
    """(worker_sats, {verifier: sats}). Deterministic, documented."""
    worker = int(bounty_sats * WORKER_SHARE)
    rest = bounty_sats - worker
    per = rest // max(1, len(verifiers))
    return worker, {v: per for v in verifiers}


# ---------------------------------------------------------------- post

async def post_task(store, spec, task_id=None):
    """Validate a task spec, create the task, lock escrow (dry-run)."""
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
        "created_at": int(time.time()),
    }
    await store.put_task(task)
    await store.ledger_add("escrow_lock", task_id, task["poster"], bounty,
                           {"note": "bounty escrowed on posting",
                            "settlement": "dry_run"})
    return task


# ---------------------------------------------------------------- claim

async def claim_task(store, artifacts, task_id, agent, pay_address=""):
    """Agent claims a task: exactly one claim per agent (DDL UNIQUE),
    mints a private Artifacts fork with a short-lived repo-scoped token."""
    agent = (agent or "").strip() or "anon"
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
            " (bitcoin address the agent receives on)")
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
                              stake_sats=0, repro=""):
    """Verifier attests pass/fail on a fork. Stake is locked (dry-run).
    A fail verdict MUST include a repro (reproduce-to-release).
    Returns {"ready_for_merge": bool} — first verified pass wins."""
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

async def settle_task(store, sender, task_id, idempotency_key=""):
    """DRY-RUN settlement: escrow release + payouts recorded in the
    ledger via the Sender interface. No funds move: DryRunSender is the
    only implementation until Richard's POST /agent/send exists."""
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
    if not dest:
        raise GitError(
            "refused: winning agent has no pay address — agents must"
            " provide one when they complete the task")
    verifs = await store.verifications_for_fork(winner_fork_id)
    passing = sorted({v["verifier"] for v in verifs
                      if v.get("verdict") == "pass"})
    bounty = int(task["bounty_sats"])
    worker_sats, verifier_sats = split_bounty(bounty, passing)
    payouts = []
    r = await sender.send(dest, worker_sats,
                          f"swarmgit bounty: {task_id} winner")
    payouts.append({"to": dest, "agent": winner, "sats": worker_sats,
                    "settlement": r.get("settlement", "dry_run"),
                    "txid": r.get("txid")})
    await store.ledger_add("payout", task_id, winner, worker_sats,
                           {"role": "winner", "pay_address": dest,
                            "settlement": "dry_run",
                            "txid": r.get("txid")})
    for v, sats in verifier_sats.items():
        rv = await sender.send(v, sats, f"swarmgit verifier: {task_id}")
        payouts.append({"to": v, "sats": sats,
                        "settlement": rv.get("settlement", "dry_run"),
                        "txid": rv.get("txid")})
        await store.ledger_add("payout", task_id, v, sats,
                               {"role": "verifier", "settlement": "dry_run",
                                "txid": rv.get("txid")})
    await store.ledger_add("escrow_release", task_id, task["poster"],
                           bounty,
                           {"note": "escrow released to payouts",
                            "settlement": "dry_run"})
    await store.bump_reputation(winner, score=10, tasks_won=1)
    task["status"] = "settled"
    await store.put_task(task)
    return {"ok": True, "task_id": task_id, "dry_run": True,
            "funds_moved": False, "bounty_sats": bounty,
            "winner": winner, "payouts": payouts,
            "note": "DRY-RUN ONLY. Ledger entries above are local"
                    " accounting. Live sats move only via Richard's"
                    " POST /agent/send primitive — not wired yet."}


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
