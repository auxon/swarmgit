"""Google sign-in for SwarmGit (GIS ID-token flow, no client secret).

Browser: Google Identity Services button on the board page returns an
ID token; the page POSTs it to /auth/google. Here we validate it
against Google and mint a D1-backed session cookie. MCP writes accept
the session cookie as an alternative to the operator bearer, so signed
in users never see a token.

Validation goes through Google's tokeninfo endpoint (one server-side
GET, no crypto to port into the Python runtime): we check aud == our
client id, issuer, and expiry. Only sub/email/name are stored — the
ID token itself is never logged or persisted.

Rate limits apply at Google's end; fine for demo scale.
"""
import json
import secrets
import time

TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
EXP_LEEWAY_S = 60
SESSION_TTL_S = 30 * 24 * 3600
SESSION_COOKIE = "sg_session"


class AuthError(Exception):
    pass


def _http_body(res):
    """Accept both seam shapes: http_fetch ({ok,status,body}) and test
    fakes ({status,body,headers}). Returns (status, body_text)."""
    if not isinstance(res, dict):
        raise AuthError("refused: bad upstream response")
    if res.get("ok") is False:
        raise AuthError(f"refused: upstream error: {res.get('error', '?')}")
    return int(res.get("status", 0) or 0), res.get("body", "")


async def verify_google_id_token(http, id_token, client_id):
    """Validate a GIS ID token via tokeninfo. Returns
    {"sub","email","name"} or raises AuthError."""
    token = (id_token or "").strip()
    if not token or len(token) > 8192:
        raise AuthError("refused: id_token required")
    if not (client_id or "").strip():
        raise AuthError("refused: Google login is not configured")
    status, text = _http_body(await http(
        "GET", TOKENINFO_URL + "?id_token=" + token,
        {"Accept": "application/json"}, 10))
    if status != 200:
        raise AuthError("refused: Google rejected this sign-in")
    try:
        info = json.loads(text) if isinstance(text, str) else text
    except Exception:
        raise AuthError("refused: Google rejected this sign-in")
    if not isinstance(info, dict):
        raise AuthError("refused: Google rejected this sign-in")
    if info.get("aud") != client_id:
        raise AuthError("refused: sign-in was issued for another app")
    if info.get("iss") not in ("accounts.google.com",
                               "https://accounts.google.com"):
        raise AuthError("refused: bad sign-in issuer")
    now = int(time.time())
    try:
        exp = int(info.get("exp", 0))
    except (TypeError, ValueError):
        exp = 0
    if exp < now - EXP_LEEWAY_S:
        raise AuthError("refused: sign-in expired, try again")
    sub = (info.get("sub") or "").strip()
    if not sub:
        raise AuthError("refused: sign-in has no subject")
    email = (info.get("email") or "").strip()
    name = (info.get("name") or "").strip() or email or "Google user"
    return {"sub": sub, "email": email, "name": name}


async def mint_session(store, identity, ttl_s=SESSION_TTL_S):
    """Create a session row. Returns the session id (secret)."""
    sid = secrets.token_hex(32)
    now = int(time.time())
    await store.session_put({
        "session_id": sid,
        "sub": identity["sub"],
        "email": identity.get("email", ""),
        "name": identity.get("name", ""),
        "created_at": now,
        "expires_at": now + int(ttl_s),
    })
    return sid


async def get_session(store, session_id):
    """Return the session row, or None (missing/expired; expired rows
    are deleted lazily)."""
    sid = (session_id or "").strip()
    if not sid:
        return None
    row = await store.session_get(sid)
    if not row:
        return None
    if int(row.get("expires_at", 0) or 0) < int(time.time()):
        try:
            await store.session_delete(sid)
        except Exception:
            pass
        return None
    return row


def parse_cookies(cookie_header):
    out = {}
    for part in (cookie_header or "").split(";"):
        if "=" in part:
            k, _, v = part.partition("=")
            out[k.strip()] = v.strip()
    return out


def session_cookie_header(session_id, max_age=SESSION_TTL_S):
    return (f"{SESSION_COOKIE}={session_id}; Path=/; HttpOnly; Secure; "
            f"SameSite=Lax; Max-Age={int(max_age)}")


def clear_cookie_header():
    return (f"{SESSION_COOKIE}=; Path=/; HttpOnly; Secure; "
            "SameSite=Lax; Max-Age=0")
