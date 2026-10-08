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

import qrcodegen


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
.pay{display:flex;gap:18px;align-items:center;flex-wrap:wrap}
.pay svg{background:#fff;border-radius:8px;padding:8px}
.pay .addr{font-size:15px;word-break:break-all}
"""

_POST_FORM = """
<details class="task"><summary><span class="task-title">➕ Post a task</span><span class="task-meta">sign in with Google — no tokens to handle</span></summary>
<div class="task-body"><div id="sg-auth">
<div id="sg-gbtn"></div>
<div id="sg-who" class="task-meta" style="margin:8px 0"></div>
</div>
<form id="sg-post-form" class="sg-form" autocomplete="off">
<div id="sg-bearer-row"><label>Bearer token (operator fallback — only if not signed in)</label>
<input id="sg-bearer" type="password" autocomplete="off" placeholder="paste SWARMSGIT_BEARER once"></div>
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
<script src="https://accounts.google.com/gsi/client" async defer></script>
<script>
(function(){
var f=document.getElementById('sg-post-form');if(!f)return;
var out=document.getElementById('sg-out');
var bEl=document.getElementById('sg-bearer');
var who=document.getElementById('sg-who');
// Bearer fallback: remembered for this tab session only (sessionStorage
// clears when the tab closes; never touches disk, cookies, or server).
try{var s=sessionStorage.getItem('sg_bearer')||'';if(s){bEl.value=s;bEl.placeholder='saved for this tab session';}}catch(e){}
bEl.addEventListener('input',function(){try{sessionStorage.setItem('sg_bearer',bEl.value);}catch(e){}});
// Signed-in state: the session cookie (HttpOnly) rides along automatically;
// if the browser dropped the cookie, fall back to the sessionStorage copy.
function meHeaders(){var h={};try{var s=sessionStorage.getItem('sg_session')||'';if(s)h['Authorization']='Bearer '+s;}catch(e){}return h;}
fetch('auth/me',{credentials:'same-origin',headers:meHeaders()}).then(function(r){return r.json().then(function(j){return {s:r.status,j:j};});}).then(function(p){
if(p.s===200&&p.j.signedIn){who.textContent='signed in as '+(p.j.name||p.j.email||'Google user');document.getElementById('sg-bearer-row').style.display='none';}
}).catch(function(){});
// Google button (rendered once GIS loads and the client id is known).
function gbtn(cid){
if(!window.google||!google.accounts||!google.accounts.id)return;
google.accounts.id.initialize({client_id:cid,callback:onGoogle,auto_select:false});
google.accounts.id.renderButton(document.getElementById('sg-gbtn'),{theme:'filled_black',size:'large',text:'signin_with'});
}
function onGoogle(resp){
fetch('auth/google',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({id_token:resp.credential})}).then(function(r){return r.json();}).then(function(j){
if(j.ok){try{if(j.session)sessionStorage.setItem('sg_session',j.session);}catch(e){}location.reload();}else{out.className='sg-out sg-err';out.textContent='sign-in refused: '+(j.error||'unknown');}
}).catch(function(e){out.className='sg-out sg-err';out.textContent='sign-in error: '+e;});
}
fetch('auth/config',{credentials:'same-origin'}).then(function(r){return r.json();}).then(function(c){
if(c&&c.clientId){var t=0;var iv=setInterval(function(){t++;if(window.google&&google.accounts&&google.accounts.id){clearInterval(iv);gbtn(c.clientId);}else if(t>50){clearInterval(iv);}},100);}
}).catch(function(){});
f.addEventListener('submit',async function(ev){
ev.preventDefault();out.className='sg-out';out.textContent='posting…';
function v(id){return (document.getElementById(id).value||'').trim();}
var tests=v('sg-tests').split(/\\n+/).map(function(s){return s.trim();}).filter(Boolean).map(function(n){return {name:n};});
var body={jsonrpc:'2.0',id:1,method:'tools/call',params:{name:'post_task',arguments:{
repo:v('sg-repo'),title:v('sg-title'),description:v('sg-desc'),acceptance_tests:tests,
bounty_sats:parseInt(v('sg-bounty')||'0',10),deadline_at:parseInt(v('sg-deadline')||'0',10)||0,
poster:v('sg-poster')||'anon',idempotency_key:(crypto.randomUUID?crypto.randomUUID():'post-'+Date.now())}}};
try{
var hdrs={'Content-Type':'application/json'};
var sess='';try{sess=sessionStorage.getItem('sg_session')||'';}catch(e){}
if(bEl.value)hdrs['Authorization']='Bearer '+bEl.value;
else if(sess)hdrs['Authorization']='Bearer '+sess;
var r=await fetch('mcp',{method:'POST',credentials:'same-origin',headers:hdrs,body:JSON.stringify(body)});
var j=await r.json();
var t=j&&j.result&&j.result.content&&j.result.content[0]&&j.result.content[0].text;
var p=t?JSON.parse(t):{ok:false,error:(j&&j.error&&j.error.message)||('HTTP '+r.status)};
if(p.ok){out.className='sg-out sg-ok';out.textContent='posted: '+p.task_id+' — reload to see it.';}
else{out.className='sg-out sg-err';out.textContent='refused: '+(p.error||'unknown');}
}catch(e){out.className='sg-out sg-err';out.textContent='network error: '+e;}
});})();
</script>
"""


def _qr_svg(text, scale=4):
    """Public-domain Nayuki encoder. Quiet zone included. Empty on failure."""
    if not text:
        return ""
    try:
        qr = qrcodegen.QrCode.encode_text(
            text, qrcodegen.QrCode.Ecc.MEDIUM)
    except Exception:
        return ""
    n = qr.get_size()
    dim = (n + 2) * scale
    rects = []
    for y in range(n):
        for x in range(n):
            if qr.get_module(x, y):
                rects.append(
                    '<rect x="%d" y="%d" width="%d" height="%d"/>'
                    % ((x + 1) * scale, (y + 1) * scale, scale, scale))
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
        'viewBox="0 0 %d %d" role="img" aria-label="payment QR">'
        '<rect width="100%%" height="100%%" fill="#fff"/>'
        '<g fill="#111">%s</g></svg>' % (dim, dim, dim, dim, "".join(rects)))


def _bip21(address, sats):
    try:
        sats = int(sats or 0)
    except (TypeError, ValueError):
        sats = 0
    if sats <= 0:
        return "bitcoin:%s?sv" % address
    return "bitcoin:%s?sv&amount=%.8f" % (address, sats / 1e8)


def _pay_panel(address):
    address = (address or "").strip()
    if not address:
        return (
            '<div class="card pay"><div>'
            '<h2 style="margin-top:0">Pay a bounty</h2>'
            '<p class="task-meta">No receive address configured. '
            'Set <span class="mono">SWARMSGIT_PAY_ADDRESS</span> '
            'in wrangler.toml and redeploy. Settlement stays dry-run '
            'until the send primitive exists; this panel is the '
            'human payment rail.</p></div></div>')
    uri = _bip21(address, 0)
    return (
        '<div class="card pay">%s<div>'
        '<h2 style="margin-top:0">Pay a bounty</h2>'
        '<p class="task-meta">Scan to pay SwarmGit. Put the task bounty '
        'in the wallet. Ledger settlement is still dry-run.</p>'
        '<p class="mono addr">%s</p>'
        '<p class="mono">%s</p></div></div>' % ( _qr_svg(uri), _e(address), _e(uri)))


_CLOSE_JS = r"""
<script>
(function(){var f=document.getElementById('sg-close-form');if(!f)return;
var out=document.getElementById('sg-close-out');
f.addEventListener('submit',async function(ev){
ev.preventDefault();out.className='sg-out';out.textContent='closing...';
function v(id){return (document.getElementById(id).value||'').trim();}
var body={jsonrpc:'2.0',id:1,method:'tools/call',params:{name:'close_dispute',arguments:{
dispute_id:v('sg-dispute'),ruling:v('sg-ruling'),operator:v('sg-operator')||'operator',
note:v('sg-note'),idempotency_key:(crypto.randomUUID?crypto.randomUUID():'close-'+Date.now())}}};
try{
var hdrs={'Content-Type':'application/json'};
var b='';try{b=sessionStorage.getItem('sg_bearer')||'';}catch(e){}
var sess='';try{sess=sessionStorage.getItem('sg_session')||'';}catch(e){}
if(b)hdrs['Authorization']='Bearer '+b;else if(sess)hdrs['Authorization']='Bearer '+sess;
var r=await fetch('mcp',{method:'POST',credentials:'same-origin',headers:hdrs,body:JSON.stringify(body)});
var j=await r.json();
var t=j&&j.result&&j.result.content&&j.result.content[0]&&j.result.content[0].text;
var p=t?JSON.parse(t):{ok:false,error:(j&&j.error&&j.error.message)||('HTTP '+r.status)};
if(p.ok){out.className='sg-out sg-ok';out.textContent='closed: '+p.ruling+(p.slashed_sats?(' / slashed '+p.slashed_sats+' sats'):'')+' - reload.';}
else{out.className='sg-out sg-err';out.textContent='refused: '+(p.error||'unknown');}
}catch(e){out.className='sg-out sg-err';out.textContent='network error: '+e;}
});})();
</script>
"""


def _close_form(deferred):
    head = (
        '<details class="task" id="sg-close"><summary>'
        '<span class="task-title">Close a deferred dispute</span>'
        '<span class="task-meta">human ruling - a bad dispute slashes the disputer</span>'
        '</summary><div class="task-body">')
    if not deferred:
        body = '<p class="empty">No deferred disputes. Clef still rules the confident ones.</p>'
    else:
        opts = "".join(
            '<option value="%s">%s / %s / contested %s by %s</option>' % (
                _e(d.get("dispute_id")), _e(d.get("dispute_id")),
                _e(_truncate(d.get("title"), 40)),
                _e(d.get("contested")), _e(d.get("disputer")))
            for d in deferred)
        body = (
            '<form id="sg-close-form" class="sg-form" autocomplete="off">'
            '<label>Deferred dispute</label>'
            '<select id="sg-dispute" required style="width:100%;background:#0b0e14;'
            'border:1px solid #232a3a;border-radius:8px;color:#e6e9f0;padding:9px 12px">'
            + opts + '</select>'
            '<div class="row"><div><label>Ruling</label>'
            '<select id="sg-ruling" required style="width:100%;background:#0b0e14;'
            'border:1px solid #232a3a;border-radius:8px;color:#e6e9f0;padding:9px 12px">'
            '<option value="uphold">uphold - verdict stands, slash disputer</option>'
            '<option value="overturn">overturn - flip the verdict</option>'
            '</select></div>'
            '<div><label>Operator</label>'
            '<input id="sg-operator" maxlength="40" placeholder="richard"></div></div>'
            '<label>Note</label>'
            '<textarea id="sg-note" maxlength="500" placeholder="why this ruling"></textarea>'
            '<button type="submit">Close dispute</button>'
            '<div id="sg-close-out" class="sg-out" aria-live="polite"></div>'
            '</form>' + _CLOSE_JS)
    return head + body + "</div></details>\n"


def _disputes_section(disputes):
    if not disputes:
        return '<div class="empty">No disputes on this task.</div>'
    rows = []
    for d in disputes:
        rows.append(
            '<tr><td class="mono">%s</td><td><strong>%s</strong></td>'
            '<td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>' % (
                _e(d.get("dispute_id")), _e(d.get("disputer")),
                _e(d.get("contested")), _state_badge(d.get("status")),
                _e(d.get("ruling") or "-"),
                _e(_truncate(d.get("grounds"), 80))))
    return (
        '<table><tr><th>id</th><th>disputer</th><th>contested</th>'
        '<th>status</th><th>ruling</th><th>grounds</th></tr>'
        + "".join(rows) + "</table>"
        '<p class="task-meta">An upheld dispute is a bad dispute: '
        'the disputer is slashed (dry-run ledger + false report).</p>')

async def render(store, pay_address=""):
    """Build the full board page. Read-only: store reads only."""
    tasks = await store.list_tasks()
    cards = []
    deferred = []
    for t in tasks:
        cards.append(await _task_card(store, t))
        try:
            disputes = await store.disputes_for_task(t.get("task_id"))
        except Exception:
            disputes = []
        for d in disputes:
            if d.get("status") == "deferred":
                d = dict(d)
                d["title"] = t.get("title")
                deferred.append(d)
    lb = await store.leaderboard(25)
    now = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    body = f"""
<header>
  <h1><span class="bolt">⚡</span> SwarmGit <span style="font-weight:400;color:#8b93a7">live task board</span></h1>
  <p class="sub">bounty-coordinated code forge · read-only demo view · {len(tasks)} task(s) · generated {_e(now)}</p>
</header>
<h2>🏆 Leaderboard</h2>
{_leaderboard_table(lb)}
{_pay_panel(pay_address)}
{_POST_FORM}
{_close_form(deferred)}
<h2>📋 Tasks</h2>
{''.join(cards) if cards else '<div class="card empty">No tasks posted yet.</div>'}
<footer>settlement is dry-run until the send primitive exists · pay the address above · never shows full repo tokens</footer>
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
            f"<h3>Disputes</h3>{_disputes_section(await store.disputes_for_task(task_id))}"
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
