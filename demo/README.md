# SwarmGit contest demo materials

Rehearsal ~Oct 12, recording ~Oct 13. Submit by Oct 14, 11:59pm PDT.

- `swarm.yml` — the task definition (mirrors `post_task`): repo, title,
  bounty (10,000 sats), acceptance tests, and the claim → work →
  submit → verify loop for the two demo agents.
- `TASK.md` — the natural-language brief the agents implement.
- `repo/` — the demo target: a minimal stdlib MCP server with **no**
  rate limiting (`server.py`), plus `acceptance_tests.py`
  (`headers_present`, `limit_values_sane`).

Proven: the acceptance tests **fail** against the unmodified server and
**pass** against a reference token-bucket implementation.

## Rehearsal setup (Richard's step — needs the SWARMSGIT_BEARER token)

1. Create the `swarmgit-demo-notes` Artifacts repo and push `repo/` into it.
2. `post_task` with the fields from `swarm.yml` → note the `task_id`.
3. Hand the bsvOS daemon: this repo's fork URL + branch (from its
   `claim_task`), `swarm.yml`, and `TASK.md`.
4. Decide the `preview_url` mechanics (tunnel vs Workers deploy) —
   the merge gate audits the winning fork's preview URL.
