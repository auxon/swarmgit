"""Why-graph: the signed "why" record behind every merge.

The contest asks for "not just what changed, but *why*". SwarmGit
answers with an append-only, signed rationale log per merge, built on
Richard's agent-memory layer (~/workspace/skills/agent-memory/):
private entries post to the AgentBridge `muse` board (signed +
encrypted), public ones to UsenetBSV (paid, permanent).

The worker STAGES entries in D1 (why_entries). It never posts to any
board itself — the operator exports staged entries with
tools/export_why.py, which shells to `bin/memory remember`. That keeps
board writes an explicit human step.

Entry kinds, in merge order:
  task_posted -> attempt (per fork) -> verification -> rationale -> decision

Honest limit, stated everywhere: recall is chronological/topic-filtered.
The memory layer's Phase 2 semantic index is scoped but NOT built —
we do not promise semantic search over the why-graph.
"""
import time

import gitlib
from gitlib import nid

KINDS = ("task_posted", "attempt", "verification", "rationale", "decision")


def make_entry(kind, merge_id, agent, text, refs=None, topic=None):
    """Build one why-graph entry (staged, not yet posted)."""
    if kind not in KINDS:
        raise ValueError(f"unknown why kind: {kind}")
    text = (text or "").strip()
    if not text:
        raise ValueError("refused: why entries must not be empty")
    return {
        "id": nid("why_"),
        "merge_id": merge_id,
        "kind": kind,
        "agent": agent or "swarmgit",
        "text": text,
        "refs": refs or [],
        "topic": topic or f"swarmgit-{merge_id[:12]}",
        "created_at": int(time.time()),
    }


async def stage_merge_why(store, merge, task, forks, verifs_by_fork,
                          audit_report):
    """Stage the full why-graph for a merge decision. Returns the list
    of staged entry ids (stored on the merge record as why_refs)."""
    merge_id = merge["merge_id"]
    staged = []

    async def put(kind, agent, text, refs=None):
        e = make_entry(kind, merge_id, agent, text, refs=refs)
        await store.stage_why_entry(e)
        staged.append(e["id"])
        return e

    await put("task_posted", task.get("poster", "anon"),
              f"[swarmgit] task {task['task_id']}: {task.get('title','')}"
              f" — bounty {task.get('bounty_sats',0)} sats, repo"
              f" {task.get('repo','')}",
              refs=[task["task_id"]])
    for f in forks:
        claim_agent = f.get("agent", "?")
        await put("attempt", claim_agent,
                  f"[swarmgit] attempt by {claim_agent} on fork"
                  f" {f['repo_name']} (status {f.get('status','')})",
                  refs=[f["fork_id"]])
        for v in verifs_by_fork.get(f["fork_id"], []):
            await put("verification", v["verifier"],
                      f"[swarmgit] {v['verifier']} attested"
                      f" {v['verdict']} on {f['repo_name']}"
                      f" (stake {v.get('stake_sats',0)} sats)"
                      + (f" — repro: {v['repro'][:200]}"
                         if v.get("verdict") == "fail" else ""),
                      refs=[f["fork_id"]])
    crit = [x for x in (audit_report or {}).get("findings", [])
            if x.get("severity") == "critical"]
    await put("rationale", "swarmgit",
              f"[swarmgit] merge rationale for {task['task_id']}: fork"
              f" {merge['fork_id']} won — first verified pass; merge gate"
              f" {audit_report.get('mode','?')} mode:"
              f" {len((audit_report or {}).get('findings', []))} findings,"
              f" {len(crit)} critical. Decision:"
              f" {merge.get('decision','')}.",
              refs=[merge["fork_id"], task["task_id"]])
    await put("decision", "swarmgit",
              f"[swarmgit] DECISION {merge['merge_id']}:"
              f" {merge.get('decision','')} — task {task['task_id']},"
              f" fork {merge['fork_id']}",
              refs=[merge["merge_id"]])
    return staged


def format_for_memory_cli(entry):
    """Render a staged entry as `memory remember` arguments.
    One memory, one point; topics are cheap (skill operating rules)."""
    return {
        "text": entry["text"],
        "topic": entry["topic"],
        "visibility": "private",  # signed + encrypted board post
    }
