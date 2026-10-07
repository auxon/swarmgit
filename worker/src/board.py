"""Read-only HTML status board for the SwarmGit contest demo (GET /board).

Public and unauthenticated by design (it is the demo's visual), but
the page itself is strictly read-only: this module only calls the
store's READ methods (list_tasks, claims_for_task, forks_for_task,
verifications_for_fork, merges_for_task, why_entries_for_merge,
leaderboard). It never writes to D1, mints anything, or touches
escrow. Every dynamic value is html-escaped. Repo tokens are never
shown in full — claims persist only the last4 (see gitlib.claim_task).

The page DOES include an operator posting form (_POST_FORM): pure
client-side HTML/JS that calls the authenticated MCP endpoint
(relative 'mcp') with a bearer the operator pastes in. The bearer
lives only in the browser's memory for that request — it is never
stored, rendered, or sent anywhere else. GET /board performs zero
writes; POST /board is still 404.
"""
import html
import time


def _e(v):
    return html.escape("" if v is None else str(v), quote=True)


def _ts(ts):
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return "—"
    if ts <= 0:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))


def _truncate(s, n):
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


_STATE_CLASS = {
    "open": "st-open",
    "claimed": "st-active",
    "working": "st-active",
    "submitted": "st-active",
    "verifying": "st-warn",
    "merging": "st-warn",
    "settled": "st-done",
    "merged": "st-done",
    "gate_blocked": "st-bad",
    "disputed": "st-bad",
    "expired": "st-muted",
    "rejected": "st-bad",
    "lost": "st-muted",
    "won": "st-done",
    "active": "st-active",
}

_SEV_CLASS = {
    "critical": "sv-critical",
    "major": "sv-major",
    "minor": "sv-minor",
    "info": "sv-info",
}


def _badge(text, cls):
    return f'<span class="badge {cls}">{_e(text)}</span>'


def _state_badge(state):
    return _badge(state or "?", _STATE_CLASS.get(state, "st-muted"))


def _verdict_badge(verdict):
    cls = "sv-critical" if verdict == "pass" else "st-bad"
    if verdict == "pass":
        cls = "st-open"
    return _badge(verdict or "?", cls)


_CSS = """
:root{color-scheme:dark}
*{box-sizing:border-box}
body{background:#0b0e14;color:#e6e9f0;font:15px/1.55 -apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;margin:0;padding:32px 20px 64px}
.wrap{max-width:1080px;margin:0 auto}
header h1{font-size:30px;margin:0 0 4px;letter-spacing:.2px}
header h1 .bolt{color:#f5b942}
header .sub{color:#8b93a7;margin:0 0 28px;font-size:14px}
h2{font-size:19px;margin:34px 0 12px;color:#f2f4f8}
.card{background:#12161f;border:1px solid #232a3a;border-radius:10px;padding:16px 18px;margin:0 0 14px}
table{width:100%;border-collapse:collapse;font-size:14px}
th{text-align:left;color:#8b93a7;font-weight:600;padding:8px 10px;border-bottom:1px solid #232a3a;white-space:nowrap}
td{padding:8px 10px;border-bottom:1px solid #1a2030;vertical-align:top}
tr:last-child td{border-bottom:none}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13px;color:#aeb8cf}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;font-size:12.5px;font-weight:600;white-space:nowrap}
.st-open{background:#12351f;color:#4ade80}
.st-active{background:#12294d;color:#6aa8ff}
.st-warn{background:#3d2c10;color:#f5b942}
.st-done{background:#2b2140;color:#b79bff}
.st-bad{background:#401418;color:#ff6b6b}
.st-muted{background:#1c2230;color:#8b93a7}
.sv-critical{background:#401418;color:#ff6b6b}
.sv-major{background:#3d2c10;color:#f5b942}
.sv-minor{background:#2c3448;color:#aeb8cf}
.sv-info{background:#1c2230;color:#8b93a7}
details.task{margin:0 0 14px}
details.task>summary{cursor:pointer;list-style:none;background:#12161f;border:1px solid #232a3a;border-radius:10px;padding:14px 18px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
details.task>summary::-webkit-details-marker{display:none}
details.task>summary:hover{border-color:#33405c}
details.task[open]>summary{border-radius:10px 10px 0 0;border-bottom:none}
.task-body{background:#12161f;border:1px solid #232a3a;border-top:none;border-radius:0 0 10px 10px;padding:6px 18px 16px}
.task-title{font-size:16.5px;font-weight:650}
.task-meta{color:#8b93a7;font-size:13.5px}
h3{font-size:14.5px;color:#c6cede;margin:18px 0 8px;text-transform:uppercase;letter-spacing:.6px}
.desc{color:#aeb8cf;font-size:14px;margin:8px 0}
.empty{color:#8b93a7;font-style:italic;padding:14px 0}
footer{margin-top:40px;color:#5c6579;font-size:13px;text-align:center}
a{color:#6aa8ff}
.sg-form label{display:block;margin:10px 0 4px;color:#8b93a7;font-size:13px}
.sg-form input,.sg-form textarea{width:100%;background:#0b0e14;border:1px solid #232a3a;border-radius:8px;color:#e6e9f0;padding:9px 12px;font:14px/1.5 inherit}
.sg-form textarea{min-height:64px;resize:vertical}
.sg-form .row{display:grid;grid-template-columns:1fr 1fr;gap:0 14px}
.sg-form button{margin-top:14px;background:#f5b942;border:none;border-radius:8px;color:#14100a;font-weight:700;padding:10px 22px;font-size:14.5px;cursor:pointer}
.sg-form button:hover{background:#ffc95e}
.sg-out{margin-top:12px;font-size:13.5px}
.sg-ok{color:#4ade80}
.sg-err{color:#ff6b6b}
"""

_POST_FORM = """
<details class="task"><summary><span class="task-title">➕ Post a task</span><span class="task-meta">operator · bearer stays in this browser tab only</span></summary>
<div class="task-body"><form id="sg-post-form" class="sg-form" autocomplete="off">
<label>Bearer token (operator secret — typed, never stored)</label>
<input id="sg-bearer" type="password" autocomplete="off" placeholder="paste SWARMSGIT_BEARER">
<div class="row"><div><label>Repo (new Artifacts repo name)</label><input id="sg-repo" required maxlength="80"></div>
<div><label>Bounty (sats, integer)</label><input id="sg-bounty" required inputmode="numeric" pattern="[0-9]+" placeholder="1000"></div></div>
<label>Title</label><input id="sg-title" required maxlength="140">
<label>Description</label><textarea id="sg-desc" maxlength="2000"></textarea>
<label>Acceptance tests (one name per line)</label><textarea id="sg-tests" required placeholder="headers_present&#10;limit_values_sane"></textarea>
<div class="row"><div><label>Deadline (optional, unix seconds)</label><input id="sg-deadline" inputmode="numeric" pattern="[0-9]*" placeholder="0 = none"></div>
<div><label>Poster</label><input id="sg-poster" maxlength="40" placeholder="richard"></div></div>
<button type="submit">Post task (escrow locks on posting)</button>
<div id="sg-out" class="sg-out" aria-live="polite"></div>
</form></div></details>
<script>
(function(){
var f=document.getElementById('sg-post-form');if(!f)return;
var out=document.getElementById('sg-out');
f.addEventListener('submit',async function(ev){
ev.preventDefault();out.className='sg-out';out.textContent='posting…';
function v(id){return (document.getElementById(id).value||'').trim();}
var tests=v('sg-tests').split(/\\n+/).map(function(s){return s.trim();}).filter(Boolean).map(function(n){return {name:n};});
var body={jsonrpc:'2.0',id:1,method:'tools/call',params:{name:'post_task',arguments:{
repo:v('sg-repo'),title:v('sg-title'),description:v('sg-desc'),acceptance_tests:tests,
bounty_sats:parseInt(v('sg-bounty')||'0',10),deadline_at:parseInt(v('sg-deadline')||'0',10)||0,
poster:v('sg-poster')||'anon',idempotency_key:(crypto.randomUUID?crypto.randomUUID():'post-'+Date.now())}}};
try{
var r=await fetch('mcp',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+document.getElementById('sg-bearer').value},body:JSON.stringify(body)});
var j=await r.json();
var t=j&&j.result&&j.result.content&&j.result.content[0]&&j.result.content[0].text;
var p=t?JSON.parse(t):{ok:false,error:(j&&j.error&&j.error.message)||('HTTP '+r.status)};
if(p.ok){out.className='sg-out sg-ok';out.textContent='posted: '+p.task_id+' — reload to see it.';}
else{out.className='sg-out sg-err';out.textContent='refused: '+(p.error||'unknown');}
}catch(e){out.className='sg-out sg-err';out.textContent='network error: '+e;}
});})();
</script>
"""


async def render(store):
    """Build the full board page. Read-only: store reads only."""
    tasks = await store.list_tasks()
    cards = []
    for t in tasks:
        cards.append(await _task_card(store, t))
    lb = await store.leaderboard(25)
    now = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    body = f"""
<header>
  <h1><span class="bolt">⚡</span> SwarmGit <span style="font-weight:400;color:#8b93a7">live task board</span></h1>
  <p class="sub">bounty-coordinated code forge · read-only demo view · {len(tasks)} task(s) · generated {_e(now)}</p>
</header>
<h2>🏆 Leaderboard</h2>
{_leaderboard_table(lb)}
{_POST_FORM}
<h2>📋 Tasks</h2>
{''.join(cards) if cards else '<div class="card empty">No tasks posted yet.</div>'}
<footer>read-only · settlement is dry-run until the send primitive exists · never shows full repo tokens</footer>
"""
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>SwarmGit — live task board</title>"
            f"<style>{_CSS}</style></head>"
            f"<body><div class=\"wrap\">{body}</div></body></html>")


def _leaderboard_table(lb):
    if not lb:
        return '<div class="card empty">No reputation recorded yet.</div>'
    rows = []
    for i, r in enumerate(lb, 1):
        rows.append(
            f"<tr><td class=\"mono\">#{i}</td>"
            f"<td><strong>{_e(r.get('agent'))}</strong></td>"
            f"<td class=\"mono\">{int(r.get('score', 0))}</td>"
            f"<td class=\"mono\">{int(r.get('tasks_won', 0))}</td>"
            f"<td class=\"mono\">{int(r.get('tasks_verified', 0))}</td>"
            f"<td class=\"mono\">{int(r.get('false_reports', 0))}</td></tr>")
    return ("<div class=\"card\"><table><tr><th>#</th><th>agent</th>"
            "<th>score</th><th>tasks won</th><th>verified</th>"
            "<th>false reports</th></tr>" + "".join(rows) + "</table></div>")


async def _task_card(store, t):
    task_id = t.get("task_id", "")
    claims = await store.claims_for_task(task_id)
    forks = await store.forks_for_task(task_id)
    forks_by_claim = {f.get("claim_id"): f for f in forks}
    for f in forks:
        f["_verifs"] = await store.verifications_for_fork(f.get("fork_id"))
    merges = await store.merges_for_task(task_id)
    for m in merges:
        m["_why"] = await store.why_entries_for_merge(m.get("merge_id"))
    bounty = int(t.get("bounty_sats", 0) or 0)
    deadline = _ts(t.get("deadline_at"))
    summary = (
        f"{_state_badge(t.get('status'))}"
        f"<span class=\"task-title\">{_e(_truncate(t.get('title'), 90))}</span>"
        f"<span class=\"task-meta mono\">{bounty:,} sats · {len(claims)} claim(s)"
        f" · deadline {deadline}</span>")
    return (f"<details class=\"task\"><summary>{summary}</summary>"
            f"<div class=\"task-body\">"
            f"<p class=\"desc\">{_e(_truncate(t.get('description'), 400))}</p>"
            f"<p class=\"task-meta mono\">repo <strong>{_e(t.get('repo'))}</strong>"
            f" · poster {_e(t.get('poster'))} · id {_e(task_id)}</p>"
            f"{_acceptance_tests(t)}"
            f"<h3>Claims ({len(claims)})</h3>{_claims_table(claims, forks_by_claim)}"
            f"<h3>Forks &amp; verification</h3>{_forks_section(forks)}"
            f"<h3>Merge</h3>{_merges_section(merges)}"
            f"</div></details>")


def _acceptance_tests(t):
    tests = t.get("acceptance_tests") or []
    if not tests:
        return ""
    items = "".join(
        f"<li class=\"mono\">{_e(x.get('name') if isinstance(x, dict) else x)}</li>"
        for x in tests)
    return f"<p class=\"task-meta\">acceptance tests:</p><ul>{items}</ul>"


def _claims_table(claims, forks_by_claim):
    if not claims:
        return '<div class="empty">No claims yet.</div>'
    rows = []
    for c in claims:
        f = forks_by_claim.get(c.get("id")) or {}
        last4 = c.get("repo_token_last4") or "—"
        rows.append(
            f"<tr><td><strong>{_e(c.get('agent'))}</strong></td>"
            f"<td class=\"mono\">{_e(f.get('repo_name') or '—')}</td>"
            f"<td class=\"mono\">…{_e(last4)}</td>"
            f"<td>{_state_badge(c.get('status'))}</td></tr>")
    return ("<table><tr><th>agent</th><th>fork</th><th>token</th>"
            "<th>status</th></tr>" + "".join(rows) + "</table>")


def _forks_section(forks):
    if not forks:
        return '<div class="empty">No forks yet.</div>'
    parts = []
    for f in forks:
        verifs = f.get("_verifs") or []
        if verifs:
            vrows = "".join(
                f"<tr><td><strong>{_e(v.get('verifier'))}</strong></td>"
                f"<td>{_verdict_badge(v.get('verdict'))}</td>"
                f"<td class=\"mono\">{int(v.get('stake_sats', 0) or 0):,} sats</td>"
                f"<td>{_e(_truncate(v.get('repro'), 200))}</td></tr>"
                for v in verifs)
            vtable = ("<table><tr><th>verifier</th><th>verdict</th>"
                      "<th>stake</th><th>repro</th></tr>" + vrows + "</table>")
        else:
            vtable = '<div class="empty">No verifications yet.</div>'
        preview = f.get("preview_url") or ""
        preview_html = (f"<a href=\"{_e(preview)}\">{_e(_truncate(preview, 60))}</a>"
                        if preview else "—")
        parts.append(
            f"<div class=\"card\" style=\"margin:10px 0\">"
            f"<p class=\"mono\" style=\"margin:0 0 6px\"><strong>{_e(f.get('repo_name'))}</strong>"
            f" {_state_badge(f.get('status'))}</p>"
            f"<p class=\"task-meta\" style=\"margin:0 0 10px\">preview: {preview_html}</p>"
            f"{vtable}</div>")
    return "".join(parts)


def _merges_section(merges):
    if not merges:
        return '<div class="empty">No merge recorded yet.</div>'
    parts = []
    for m in merges:
        audit = m.get("audit_report") or {}
        mode = audit.get("mode", "?")
        findings = audit.get("findings") or []
        counts = {}
        for f in findings:
            sev = (f.get("severity") or "info").lower()
            counts[sev] = counts.get(sev, 0) + 1
        sev_order = ["critical", "major", "minor", "info"]
        count_badges = " ".join(
            _badge(f"{sev}: {counts[sev]}", _SEV_CLASS.get(sev, "sv-info"))
            for sev in sev_order if sev in counts) or _badge("no findings", "sv-info")
        top = sorted(findings,
                     key=lambda f: sev_order.index((f.get("severity") or "info").lower())
                     if (f.get("severity") or "info").lower() in sev_order else 99)[:5]
        frows = "".join(
            f"<tr><td>{_badge(f.get('severity', '?'), _SEV_CLASS.get((f.get('severity') or 'info').lower(), 'sv-info'))}</td>"
            f"<td>{_e(_truncate(f.get('title'), 110))}</td>"
            f"<td class=\"mono\">{_e(f.get('confidence', ''))}</td></tr>"
            for f in top)
        ftable = (f"<table><tr><th>severity</th><th>finding</th><th>confidence</th></tr>"
                  + frows + "</table>") if frows else ""
        why = m.get("_why") or []
        rationale = next((w for w in why if w.get("kind") == "rationale"), None)
        why_html = ""
        if rationale:
            why_html = (f"<p class=\"desc\"><strong>why it won:</strong> "
                        f"{_e(_truncate(rationale.get('text'), 500))}</p>")
        parts.append(
            f"<div class=\"card\" style=\"margin:10px 0\">"
            f"<p style=\"margin:0 0 8px\">{_state_badge(m.get('decision'))}"
            f" <span class=\"task-meta mono\">gate mode: {_e(mode)} · "
            f"{len(why)} why-graph entries</span></p>"
            f"<p style=\"margin:0 0 8px\">{count_badges}</p>"
            f"{ftable}{why_html}</div>")
    return "".join(parts)
