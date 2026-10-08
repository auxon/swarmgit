"""Queue consumer: drives the SwarmGit task lifecycle, one message at a
time (wrangler.toml: max_batch_size=1, max_concurrency=1 — serial, like
the TestSwarm/PreFlight scaffolds).

Message kinds:
  {"kind": "verify", "task_id", "fork_id"}
      submit_work queued this: flip fork submitted -> verifying, task ->
      verifying. Attestations arrive via the attest_verification MCP tool;
      the first verified pass enqueues "merge".
  {"kind": "merge", "task_id", "fork_id"}
      Run the merge gate (merge_gate.run_merge_gate against the fork's
      preview_url), record the merge decision, stage the why-graph, then
      settle (dry-run). A blocked gate returns the task to open.
  {"kind": "artifact_push", "event": {...}}
      Artifacts push-event subscription delivery: map the pushed repo to
      a fork; if that fork is still working, auto-submit it.

process_message(store, deps, msg) never raises. deps: {"http", "sender",
"enqueue"} (+ "artifacts" for future diff computation).
"""
import time

import gitlib
import merge_gate
import whygraph

# Subrequest budget per merge-gate invocation (Worker limit is 1000).
GATE_BUDGET = 800


async def process_message(store, deps, msg):
    """Handle one queue message. Never raises."""
    try:
        kind = (msg or {}).get("kind", "")
        if kind == "verify":
            await _on_verify(store, msg)
        elif kind == "merge":
            await _on_merge(store, deps, msg)
        elif kind == "artifact_push":
            await _on_push(store, deps, msg)
        # unknown kinds: ack silently
    except Exception:
        pass


async def _on_verify(store, msg):
    fork = await store.get_fork(msg.get("fork_id", ""))
    if not fork or fork["status"] != "submitted":
        return
    fork["status"] = "verifying"
    await store.put_fork(fork)
    task = await store.get_task(msg.get("task_id", ""))
    if task and task["status"] == "submitted":
        task["status"] = "verifying"
        await store.put_task(task)
    await store.ledger_add("verify_started", fork["task_id"], "",
                           0, {"fork_id": fork["fork_id"],
                               "settlement": "dry_run"})


async def _on_merge(store, deps, msg):
    task_id = msg.get("task_id", "")
    fork_id = msg.get("fork_id", "")
    task = await store.get_task(task_id)
    fork = await store.get_fork(fork_id)
    if not task or not fork:
        return
    if task["status"] == "settled":
        return  # already decided; ack
    task["status"] = "merging"
    await store.put_task(task)

    # -- the merge gate: REAL, not theater -------------------------
    preview_url = fork.get("preview_url", "")
    report = await merge_gate.run_merge_gate(
        deps["http"], preview_url, diff_text="", max_calls=GATE_BUDGET)
    blocked = merge_gate.gate_blocks(report)
    decision = "gate_blocked" if blocked else "merged"

    merge = await gitlib.record_merge(store, task_id, fork_id, report,
                                      why_refs=[], decision=decision)

    # -- why-graph ---------------------------------------------------
    forks = await store.forks_for_task(task_id)
    for f in forks:
        c = await store.get_claim(f["claim_id"])
        f["agent"] = c["agent"] if c else "?"
    verifs_by_fork = {}
    for f in forks:
        verifs_by_fork[f["fork_id"]] = \
            await store.verifications_for_fork(f["fork_id"])
    task = await store.get_task(task_id)  # refreshed statuses
    why_refs = await whygraph.stage_merge_why(
        store, merge, task, forks, verifs_by_fork, report)
    merge["why_refs"] = why_refs
    await store.put_merge(merge)

    if blocked:
        await store.ledger_add("gate_blocked", task_id, "", 0,
                               {"fork_id": fork_id,
                                "findings": len(report.get("findings", [])),
                                "settlement": "dry_run"})
        return  # task is open again; bounty still available

    # -- settle (dry-run) --------------------------------------------
    try:
        await gitlib.settle_task(store, deps["sender"], task_id)
    except Exception:
        # settlement must never lose the merge record; the task stays
        # "merging" so an operator can retry settle_task.
        pass


async def _on_push(store, deps, msg):
    """Artifacts push event -> auto-submit the matching working fork."""
    event = msg.get("event") or {}
    if event.get("type") != "cf.artifacts.repo.pushed":
        return
    repo_name = ((event.get("source") or {}).get("repoName") or "")
    fork = await store.fork_by_repo(repo_name)
    if not fork or fork.get("status") != "working":
        return
    claim = await store.get_claim(fork["claim_id"])
    if not claim:
        return
    try:
        payload = await gitlib.submit_work(
            store, fork["task_id"], claim["id"], claim["agent"], "")
    except Exception:
        return
    await deps["enqueue"](payload)
