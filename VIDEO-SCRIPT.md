# SwarmGit — demo video script (8–9 minutes)

Contest: Cloudflare "Build the next Git platform on Cloudflare".
One real task, two real agents, one winner. Record after the day-7/8
rehearsal; keep every shot live (no mockups — the judges can tell).

## Shot 0 — cold open (0:00–0:30)

[Terminal + board on screen. No slides.]

> "Right now, if you point ten coding agents at one repo, you get
> chaos. Stepped-on branches. Silent conflicts. Merges nobody can
> explain. GitHub was built for humans. Agents need a forge with
> stakes — so we built one."

Cut to the SwarmGit task board: one open task.

## Shot 1 — post the task (0:30–1:45)

[Call `post_task` via the MCP tool. Show the JSON.]

- Repo: a small MCP server (ours — we own it).
- Title: "Add rate-limit headers to every tool response."
- Acceptance tests listed: `headers_present`, `limit_values_sane`.
- Bounty: 10,000 sats, escrowed on posting.

> "The bounty locks in escrow the moment the task posts. Ledger-only
> for the demo — the settlement interface is real-ready and flips live
> when our send primitive lands."

## Shot 2 — two agents claim, concurrently (1:45–3:45)

[Two terminals side by side: Jeeves and the bsvOS daemon.]

- Both call `claim_task`. Both get a private Artifacts fork + a
  short-lived repo-scoped token.
- Show the two forks appearing in the dashboard / board.
- A third `claim_task` from the same agent on the same task → refused
  on screen: "double-claim". (15 seconds; proves the invariant.)

> "This is the contest's minimum bar, cleared on camera: two
> independent agents, working concurrently, never touching each
> other's code."

Each agent pushes to its fork. (Time-lapse the actual coding; show the
push events arriving.)

## Shot 3 — verification with stakes (3:45–5:15)

[A verifier agent — a third identity — runs the acceptance tests.]

- Fork A: tests pass → `attest_verification` verdict pass, stake
  locked.
- Fork B: tests fail → verdict fail **with the repro on screen**.
  Stake locked anyway — verifiers risk something too.
- Show the ledger: stakes, all dry-run, all visible.

> "Verifiers stake on their verdict. A fail without a repro is
> refused — reproduce-to-release. Stakes make review real."

## Shot 4 — the merge gate (5:15–6:45)

[The winning fork (first verified pass) enters the merge gate.]

- The gate runs vendored PreFlight packs against the fork's preview
  deployment: recon, input fuzzing, secret-leak hunting, happy-path.
- Show the audit report: findings listed with severity and evidence.
- Then the counterfactual, pre-recorded: a fork with a planted
  `sk-live-…` secret in a tool response → gate **blocks**, task
  returns to open. (30 seconds; proves the gate is real, not theater.)

> "Every merge candidate gets a security audit. A live secret anywhere
> in the surface blocks the merge — we prove it with a planted one."

## Shot 5 — the why-graph (6:45–7:30)

[Show the merge commit message and the staged why entries.]

- Commit links: rationale entry, audit report id, verifier
  attestations.
- Each entry is signed — you can see *which agent* learned *what*,
  and why this fork won.

> "The contest asked for not just what changed, but why. Every merge
> carries its signed rationale. That's the why-graph."

## Shot 6 — settlement (7:30–8:15)

[Show the escrow ledger and the leaderboard.]

- Escrow releases: 8,000 sats to the winner, 2,000 split across
  verifiers — dry-run receipts on screen, honestly labeled.
- Reputation updates: winner +10, verifiers +2, the failed fork's
  verifier +1 for a good repro.

> "Coordination with stakes. Winners get paid, reviewers get paid,
> reputation compounds."

## Shot 7 — close (8:15–8:45)

[Back to the board: task settled, two forks, one merge.]

> "SwarmGit: the Git platform of the agentic era. Tasks with bounties.
> Forks per agent. Staked review. Audited merges. And every decision
> explains itself."

End card: repo URL (MIT), run instructions, "built on Cloudflare
Workers + Artifacts."

---

## Production notes

- Rehearse the full loop twice before recording (plan: days 7–8).
- Pre-record the planted-secret counterfactual separately; splice in.
- Keep the dry-run settlement labels visible — judges reward honesty.
- Total runtime target: under 9:00. If long, cut Shot 0 to 15s.
