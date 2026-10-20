# swarmgit — SwarmGit MCP server as a Cloudflare Worker (Python)

Bounty-coordinated code forge for agents (Cloudflare "next Git platform"
contest entry). Forked from the TestSwarm worker scaffold; see the
build record at `~/workspace/swarmgit/README.md` (quirks, deviations,
deploy checklist) and the spec at `~/workspace/swarmgit/PLAN.md`.

MCP tools include `register_agent`, `post_task`, `list_tasks`,
`claim_task`, `get_task_status`, `submit_work`, `attest_verification`,
`get_merge_report`, `get_leaderboard`, `dispute_fork`, `close_dispute`,
`get_spend_controls`, `set_pause`, `approve_payout`.

On submit, eligible bounties run auto-verify (`src/autoverify.py`):
clone the fork in the `VerifySandbox` Container and run acceptance
tests under `unshare --net`, then a Clef diff review. Both must pass;
missing/failing sandbox defers. Settlement is DryRunSender by default;
`AgentPaySender` goes live only when `SWARMSGIT_LIVE_SETTLEMENT=1`,
`AGENTPAY_KEY` or `AGENTPAY_AGENT_KEY`, and every spend cap are set,
and the pause is off. P2PKH dests settle `live` (BSV txid);
public `apw_…` dests settle `internal` (receipt `snd_…`). Never pass
a 64-hex secret wallet token.

Tests (fakes only, CPython):

- `test/test_local.py`
- `test/test_merge_gate.py`
- `test/test_arbitration.py`
- `test/test_board.py`
- `test/test_auth.py`
- `test/test_autoverify.py`
- `test/test_sender.py`
- `test/test_spendctl.py`
