# SwarmGit — build plan (Cloudflare "next Git platform" contest)

Prepared 2026-10-05. Contest: https://blog.cloudflare.com/next-git-platform-on-cloudflare/
(verified 2026-10-05 against Cloudflare's own blog). **Deadline: October 14, 2026.**

Engine: `~/workspace/testswarm/` (SPEC.md, `swarm.py`, `mcp_server/`, `worker/`);
`~/workspace/preflight/` (PLAN.md, `worker/` — merge-gate audits);
`~/workspace/skills/agent-memory/` (the "why" graph); AgentBridge boards;
the bsvOS daemon (live peer agent for the demo).

---

## 1. Entry definition

**SwarmGit** is a bounty-coordinated code forge for agents, built on
Cloudflare Workers + Artifacts. A maintainer posts a coding task with a
sats bounty held in escrow. Agents claim the task — each claim mints a
private Artifacts fork with a short-lived repo-scoped token, so N agents
work concurrently without stepping on each other. Staked verifiers run
the acceptance tests against each fork and attest. The winning change
merges only after a PreFlight security audit, and the merge commit
carries a signed, append-only record of *why* it was chosen. Escrow
settles to the worker and the verifiers; reputation accrues.

The contest's four questions, answered:

| Blog's question | SwarmGit's answer |
|---|---|
| How do agents know what others are working on? | Task board with signed claims — double-work is impossible by construction |
| What happens with conflicting changes? | Fork isolation; conflicts never touch main until a verified merge |
| How do you review everything they produce? | PreFlight audit gate on every merge candidate + staked verifiers |
| Not just *what* changed, but *why*? | Signed rationale log linked from every merge commit |

**The differentiator:** the economic layer. Bounties, staking,
reproduce-to-release, escrow, reputation — real stakes are the
coordination primitive no other entry will have. Everything else in the
contest will be workflow plumbing; SwarmGit is a coordination
*economy*.

**What it is not:** not a GitHub UI clone; not a CI system; not
real-money settlement on day one (dry-run ledger, same discipline as
TestSwarm's `settle_job` and PreFlight billing — HARD CONSTRAINT).

---

## 2. Contest requirements (verified)

- Build on Workers + Artifacts. Minimum bar: **multiple agents working
  concurrently** (our core loop, not a stretch goal).
- Submit: 5–10 minute demo video; source under a permissive license
  (MIT/Apache/BSD — we ship MIT); run instructions.
- Deadline **October 14, 2026**. Top three teams fly up to two members
  each to Cloudflare Connect (SF); winner gets **$25,000 in Cloudflare
  credits** + VIP speaker-dinner invite.
- Artifacts is in open beta for **Workers Paid** plan customers.
  Billing for Artifacts starts **October 15** — one day after close,
  so contest usage is free. No cost risk.

---

## 3. Architecture: what transfers, what's new

| His component | SwarmGit adaptation |
|---|---|
| `testswarm/worker/` scaffold (D1, Queues, bearer auth, serial consumer, idempotency) | fork → `swarmgit/worker/` (sibling; TestSwarm and PreFlight workers untouched) |
| `swarm.py` post/list/run/verify/settle | task board: `post_task` / `list_tasks` / `claim_task` / `submit_work` / `verify` / `settle` |
| stake-to-test, reproduce-to-release, per-task escrow, reputation | **kept** — this is the entry's moat |
| PreFlight worker (probe packs, report generator) | merge gate: audit every merge candidate; Critical findings block the merge |
| agent-memory skill (`bin/memory`; private boards / public x402) | "why" graph: signed rationale entries linked per merge |
| AgentBridge signed posts | agent-to-agent coordination channel (claims, verifier attestations) |
| bsvOS daemon | the live second agent in the demo video |
| **Artifacts Workers binding** | **NEW — the only truly new code**: repo adapter (create, fork, read, repo-scoped tokens, push-event subscriptions) |

### The Artifacts adapter (new code: `worker/src/artifacts.py`)

Thin module wrapping the Artifacts binding — `create_repo`,
`fork_repo(task_id)`, `read_file`/`read_commit`, `issue_repo_token`
(scoped, short-lived), and push-event subscription → queue → review
workflow. Isolated behind a narrow interface so beta API drift touches
exactly one file. **HARD:** talk to Artifacts through the binding,
never via public-URL subfetch — the `error code: 1042` same-account
loop lesson (AGENTS.md) applies here too.

### Task lifecycle (D1 state machine)

```
open → claimed → working → submitted → verifying → merging → settled
   ↘ expired (bounty returned)     ↘ disputed → arbitration
```

- `post_task`: target repo, task spec (issue text + acceptance tests),
  bounty sats, deadline. Bounty escrowed on posting (dry-run ledger).
- `claim_task`: agent presents a signed identity → fork minted,
  repo-scoped token issued. One claim per agent per task; claims are
  public on the board, so two agents never duplicate work.
- `submit_work`: agent pushes to its fork → Artifacts push event →
  verify job queued.
- `verify`: verifier agents run the acceptance tests inside the fork,
  stake sats on their verdict, attest pass/fail. A fail attestation
  must include a repro (reproduce-to-release, inherited from
  TestSwarm).
- `merge`: winner picked (first verified, or best-of-N compare);
  PreFlight audit runs against the merge candidate — **Critical
  findings block the merge**; merge commit message links the
  rationale log, the audit report id, and the verifier attestations.
- `settle`: escrow → worker + verifiers per the split rule (dry-run);
  reputation updated for all parties.

New D1 tables (extend the forked `schema.sql`): `tasks`, `claims`,
`forks`, `verifications`, `merges`, `escrow_ledger` (dry-run),
`reputation`. The TestSwarm/PreFlight tables stay untouched.

### The "why" graph

Every merge writes signed entries via the agent-memory skill:
task → attempts (per fork) → rationale (why this fork won) →
verification attestations → merge decision. The merge commit message
links the log. Append-only, signed, verifiable. **Do not promise**
semantic search over it — the memory layer's Phase 2 embedding index
is scoped but unbuilt; say so if asked.

### MCP tool surface

| Tool | Mode |
|---|---|
| `post_task` | sync, idempotent (escrow noted as dry-run in description) |
| `list_tasks` | sync, read-only (filter: open/claimed/verifying) |
| `claim_task` | sync, idempotent (mints fork + token) |
| `get_task_status` | sync, read-only (state machine + claims + verifications) |
| `submit_work` | sync, idempotent (registers the fork push) |
| `attest_verification` | sync (stake noted; dry-run) |
| `get_merge_report` | sync, read-only (rationale + audit + attestations) |
| `get_leaderboard` | sync, read-only (reputation) |

Bearer auth inherited from the scaffold. Every value-moving tool
states `settlement: dry_run` in its description.

---

## 4. Demo video script (the long pole — script by day 6, rehearse day 7)

Target 8–9 minutes. One real task, two real agents, one winner.

1. **0:00–0:45 — the problem.** N agents, one repo, chaos: stepped-on
   branches, silent conflicts, nobody knows why anything merged.
   (30 seconds of "before"; keep it visceral.)
2. **0:45–2:00 — post the task.** Maintainer posts a genuine small
   feature/fix with a sats bounty. Show the escrow line.
3. **2:00–4:00 — concurrent work.** Two agents claim — Jeeves and the
   bsvOS daemon. Show both Artifacts forks appearing live, both
   pushing. This is the contest's minimum bar, cleared on camera.
4. **4:00–5:30 — verification.** Verifiers run the acceptance tests,
   stake, attest. One fork fails — show the repro. Stakes make the
   review real.
5. **5:30–7:00 — the merge.** PreFlight audit runs on the winning
   fork; report attaches; signed rationale links from the commit.
   The "why" graph, visible.
6. **7:00–8:00 — settlement.** Escrow splits to worker + verifiers
   (dry-run ledger on screen); reputation updates.
7. **8:00–9:00 — close.** "This is what the Git platform of the
   agentic era looks like: coordination with stakes."

---

## 5. Phased build (9 days)

**Days 1–2 (Oct 6–7): the Artifacts adapter.**
Namespace setup, create/fork/read/token via the binding, push-event
subscription → queue → workflow. Prove: create repo → fork per task →
push event fires the review workflow. Verify with local Python tests +
`wrangler deploy --dry-run` (workerd cannot boot in this sandbox —
same environmental limit as the TestSwarm/PreFlight builds; deploy
from a normal network).

**Days 3–4 (Oct 8–9): task board + claim flow.**
D1 schema, `post_task`/`list_tasks`/`claim_task`, fork minting on
claim, signed agent identities, the agent work loop
(claim → fork → push → verify job queued).

**Days 5–6 (Oct 10–11): verification, merge, settlement, why-graph.**
Verifier staking + attestations (repro required on fail), merge
engine (first-verified / best-of-N), PreFlight gate on the merge
candidate, dry-run escrow ledger, reputation, signed rationale links
on the merge commit.

**Days 7–8 (Oct 12–13): rehearsal + video.**
End-to-end run with the bsvOS daemon; lock the script; record.
Buffer for Artifacts beta surprises.

**Day 9 (Oct 14): submit.**
Run instructions, MIT LICENSE, repo public, video uploaded,
submission form sent. Nothing left to the last hour.

**Deliberately deferred:** real sats settlement (needs his send
primitive); dispute arbitration UI (reputation-weighted review is
specced, not built); multi-repo organizations; a web dashboard
(the MCP surface + board is the demo).

---

## 6. Submission checklist

- [ ] Public repo under MIT license (his call on org/name)
- [ ] README run instructions: D1 create, queue create, Artifacts
      namespace, `PREFLIGHT_BEARER`-style secret, `wrangler deploy`
- [ ] 5–10 min demo video (script §4)
- [ ] Submission form before Oct 14 EOD

---

## 7. Open questions for Richard

1. **The daemon's role:** can the bsvOS daemon commit to being the
   second agent in the live demo (rehearsal ~Oct 12)? Fallback is two
   agents run by us — still real, less impressive.
2. **Settlement in the demo:** dry-run ledger (honest default; AgentPay
   has no send endpoint) or real sats (needs his send primitive
   built first)?
3. **Repo home + name** for the entry (public, MIT).
4. **Workers Paid plan:** does his Cloudflare account have it?
   (Required for the Artifacts open beta.)
5. **Solo entry?** Top three flies up to two people — he's solo,
   which is fine, but worth confirming he wants it that way.

---

## 8. Top risks

1. **Artifacts beta API drift.** Mitigation: the adapter is one
   isolated file; pin to the docs revision we build against.
2. **The video is the long pole.** Mitigation: script locked by day
   6, rehearsal day 7, record day 8 — never "we'll figure it out
   while recording."
3. **bsvOS daemon availability.** Mitigation: fallback to two
   own-agents; the economy still demos, the "two independent minds"
   story weakens.
4. **9 days is tight if the demo needs re-takes.** Mitigation: cut
   scope to maintainer + two agents + one verifier; keep the bounty
   economy (the differentiator), cut dispute arbitration.
5. **Sandbox workerd limit** (same as TestSwarm/PreFlight):
   verification is local tests + dry-run bundle check; the real
   deploy happens on his network with his Cloudflare auth.
