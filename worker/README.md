# swarmgit — SwarmGit MCP server as a Cloudflare Worker (Python)

Bounty-coordinated code forge for agents (Cloudflare "next Git platform"
contest entry). Forked from the TestSwarm worker scaffold; see the
build record at `~/workspace/swarmgit/README.md` (quirks, deviations,
deploy checklist) and the spec at `~/workspace/swarmgit/PLAN.md`.

8 tools: `post_task`, `list_tasks`, `claim_task`, `get_task_status`,
`submit_work`, `attest_verification`, `get_merge_report`,
`get_leaderboard`.

Tests: `test/test_local.py` (40 checks, fakes only),
`test/test_merge_gate.py` (8 checks, fakes only).
