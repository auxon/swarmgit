# Demo task: add rate-limit headers to every tool response

## The repo

`demo/repo/` is a minimal MCP server (`server.py`, stdlib only, no
dependencies). Three tools over Streamable HTTP (`POST /mcp`):

- `add_note {text}` → `{id}`
- `list_notes {}` → `{notes: [{id, text}]}`
- `delete_note {id}` → `{deleted: true|false}`

It has **no rate limiting**. That's what you're adding.

## The task

Implement a per-client token-bucket rate limiter and emit rate-limit
headers on **every** tool response:

1. `X-RateLimit-Limit` — the bucket size (positive integer; pick a
   sane default, e.g. 100/minute).
2. `X-RateLimit-Remaining` — tokens left in the current window
   (integer, `0 <= remaining <= limit`, decrements by exactly 1 per
   call).
3. Headers go on **every** response: successful calls, error
   responses (unknown tool, bad args), everything.
4. When the bucket is empty, return **HTTP 429** — with the headers
   still present (`X-RateLimit-Remaining: 0`).
5. Key the bucket per client (IP is fine for the demo).

## Constraints

- **Do not** change tool names, input schemas, or existing behavior.
- **Do not** add dependencies — stdlib only, like the starting point.
- **Do not** leak anything sensitive in responses (the merge gate
  hunts for secrets and blocks on them).

## How you'll be judged

The verifier runs `demo/repo/acceptance_tests.py` against your fork's
preview URL:

- `headers_present` — all four probe calls (three tools + one unknown
  tool) return both headers.
- `limit_values_sane` — values are sane ints, remaining decrements by
  exactly 1, and exhausting the bucket yields a 429 with headers.

Run the tests against your **local** server before you push — if they
don't pass locally, they won't pass on your preview.

## Definition of done

`submit_work` with your `preview_url`, both acceptance tests green,
merge gate clean, fork merged.
