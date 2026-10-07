"""Clef arbitration for SwarmGit disputes.

When a fork's verification outcome is contested, Clef (Cloudflare's
decision model, Jev-API compatible) reads the dispute state and
returns typed answers — no prose to parse, just probabilities the
worker acts on:

  genuine (noul)   — does the repro demonstrate a genuine failure?
  ruling  (choice) — uphold (contested verdict stands) or overturn
  severity (score) — how serious is the contested defect?

Threshold rule (the product decision, visible in code not prompts):
ruling confidence >= 0.70 applies automatically; anything lower
defers to a human operator (dispute stays open, ledger notes it).
AI failure (binding missing, timeout, malformed answers) also
defers — never default-allows, never default-blocks.

Called from the queue consumer (_on_arbitrate), never from an MCP
tool directly: arbitration is automatic once a dispute opens.
"""
import json

MODEL = "clef-flash"  # latency-critical queue path; swap to clef freely
MODEL_REF = "@cf/cloudflare/clef-flash"
CONFIRM_THRESHOLD = 0.70


class ArbiterError(Exception):
    pass


def _py(v):
    to_py = getattr(v, "to_py", None)
    return to_py() if callable(to_py) else v


def _deep(v):
    """Recursively convert JsProxy trees to plain Python."""
    v = _py(v)
    if isinstance(v, dict):
        return {str(_deep(k)): _deep(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_deep(x) for x in v]
    return v


def _answers(res):
    """Unwrap the binding result (JsProxy-or-dict) to the answers map.
    Strict: the response must carry an "answers" object (binding
    shape); REST's {"result": {"answers"}} is accepted too. Anything
    else is malformed — never guess."""
    r = _deep(res)
    if not isinstance(r, dict):
        raise ArbiterError("malformed Clef response")
    inner = r.get("result")
    if not isinstance(inner, dict):
        inner = None
    answers = r.get("answers")
    if not isinstance(answers, dict) and isinstance(inner, dict):
        answers = inner.get("answers")
    if not isinstance(answers, dict) or not answers:
        raise ArbiterError("malformed Clef response")
    return answers


async def ask_clef(ai, state, questions, model=MODEL):
    """Ask Clef; returns the answers map. Raises ArbiterError."""
    if ai is None:
        raise ArbiterError("AI binding not configured")
    payload = {"model": model, "state": state, "questions": questions}
    try:
        res = await ai.run(MODEL_REF, _to_jsable(payload))
    except Exception as e:
        raise ArbiterError(f"Clef call failed: {type(e).__name__}")
    try:
        answers = _answers(res)
    except ArbiterError:
        raise
    except Exception:
        raise ArbiterError("malformed Clef response")
    if not isinstance(answers, dict) or not answers:
        raise ArbiterError("malformed Clef response")
    return answers


def _to_jsable(obj):
    """Convert the payload to a real JS object for the RPC boundary.

    PROVEN (live probe): a plain Python dict arrives malformed
    (AiError 5006: required properties missing) — the runtime does not
    auto-convert here. to_js with Object.fromEntries builds a real
    plain object. Same DataClone lesson as the Artifacts adapter.
    Falls back to the json-cleaned dict (tests, fakes)."""
    clean = json.loads(json.dumps(obj, default=str))
    try:
        from js import Object
        from pyodide.ffi import to_js
        return to_js(clean, dict_converter=Object.fromEntries)
    except Exception:
        return clean


def dispute_questions():
    """The arbitration question schema (code, not prompt engineering)."""
    return {
        "genuine": {
            "type": "noul",
            "instructions": "Does the repro demonstrate a genuine"
                            " failure of the fork against its acceptance"
                            " tests? Vague complaints without a concrete"
                            " failing behavior count as no.",
        },
        "ruling": {
            "type": "choice",
            "instructions": "Should the contested verification verdict"
                            " stand or be flipped?",
            "criteria": {
                "uphold": "The contested verdict was correct; it stands.",
                "overturn": "The contested verdict was wrong; flip it.",
            },
        },
        "severity": {
            "type": "score",
            "instructions": "How serious is the contested defect?",
            "criteria": ["Trivial", "Minor", "Major", "Critical"],
        },
    }


def decide_ruling(answers):
    """Apply the threshold rule. Returns
    {"ruling","confidence","genuine","deferred","reason"}."""
    genuine = answers.get("genuine") or {}
    ruling_a = answers.get("ruling") or {}
    severity = answers.get("severity") or {}
    ruling = ruling_a.get("choice", "")
    conf = ruling_a.get("confidence", 0)
    try:
        conf = float(conf)
    except (TypeError, ValueError):
        conf = 0.0
    if ruling not in ("uphold", "overturn"):
        return {"ruling": "", "confidence": conf,
                "genuine": float(genuine.get("noul", 0) or 0),
                "severity": severity.get("score", 0),
                "deferred": True,
                "reason": "no clear ruling from Clef"}
    if conf < CONFIRM_THRESHOLD:
        return {"ruling": ruling, "confidence": conf,
                "genuine": float(genuine.get("noul", 0) or 0),
                "severity": severity.get("score", 0),
                "deferred": True,
                "reason": f"confidence {conf:.2f} below {CONFIRM_THRESHOLD}"}
    return {"ruling": ruling, "confidence": conf,
            "genuine": float(genuine.get("noul", 0) or 0),
            "severity": severity.get("score", 0),
            "deferred": False, "reason": ""}
