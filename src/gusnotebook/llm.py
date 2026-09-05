"""Inline LLM — turn a plain-English request into a Python cell.

This is the "+ AI" cell: you type what you want, the model writes the code, and
the cell becomes an ordinary code cell you can read, edit, and re-run. It is
deliberately separate from the Help button (`error_help.py`), which explains a
traceback after the fact — different prompt, different model setting.

Which model to use is a user setting, stored in the state directory (see
`paths.py`) so it survives restarts and upgrades. Credentials come from the same
AI gateway as Help.
"""

import json
import os
import re
import tempfile
import threading
from pathlib import Path

from flask import current_app, has_app_context
from werkzeug.local import LocalProxy

from . import paths

SETTINGS_PATH = LocalProxy(lambda: paths.state("settings.json"))

API_VERSION = "2025-02-01-preview"

# Offered in the settings modal. The list is a convenience, not a limit — the
# modal lets you type any deployment name the gateway exposes.
KNOWN_MODELS = [
    "DeepSeek-V4-Pro",
    "gpt-5",
    "gpt-5-mini",
    "gpt-4.1",
    "o4-mini",
    "claude-opus-5",
    "claude-sonnet-5",
]

DEFAULTS = {
    "inline_llm_model": os.environ.get("INLINE_LLM_MODEL", "DeepSeek-V4-Pro"),
    "inline_llm_temperature": 0.0,
    "inline_llm_max_tokens": 1200,
    # Extra standing instructions from the user, prepended to every request.
    "inline_llm_instructions": "",
    # Standing instructions for agent terminals — guardrails, house
    # style, things to watch out for. Separate from inline_llm_instructions
    # because they go to models doing a different job: one writes a single cell,
    # the others have tools and edit files. The legacy key name is retained for
    # settings compatibility. A session can add its own on top; see terminals.
    "claude_instructions": "",
    # What Claude Code may *not* do in a terminal: {"no_execute": true, ...},
    # keyed by terminals.RESTRICTION_RULES, plus a "deny_extra" free-text field.
    # Separate from claude_instructions because these are enforced rather than
    # asked for — Claude Code applies the deny rules itself, so unlike an
    # instruction this holds whether or not the model cooperates. A session can
    # add its own; the two are unioned (terminals.merge_restrictions).
    "claude_restrictions": {},
    # Gateway credentials, editable in the settings modal. Empty means "fall
    # back to the environment / .env", which is still the recommended place for
    # a key — see gateway_config.
    "gateway_url": "",
    "gateway_key": "",
    # Where a key typed into the modal is kept:
    #   "session" — in memory only, gone when the app stops (the default: a
    #               secret that was never written can't leak from a file)
    #   "disk"    — settings.json, so it survives a restart
    "gateway_key_store": "session",
}

# Never echoed back to the browser in full. The modal shows this instead, so a
# saved key can be *seen to exist* without being displayed or leaked into a
# screenshot; sending it back unchanged means "keep what's stored".
KEY_MASK = "••••••••"

SYSTEM_PROMPT = """You write Python for one cell of a Jupyter notebook.

Return ONLY the code — no prose, no explanation, no markdown fences. Comments
inside the code are fine and welcome where the intent isn't obvious.

Rules:
- The cell runs in a live kernel where earlier cells have already executed, so
  reuse the variables, imports, and DataFrames they defined instead of
  re-creating them.
- Import anything you newly need at the top of the cell.
- End with the expression whose value should be displayed, the way a notebook
  user would, rather than wrapping everything in print().
- Never invent column names, files, or APIs that aren't in the context. If a
  name you need isn't there, write the code the obvious way and add a short
  `# assumes ...` comment on the assumption you made.
- Prefer a few clear lines over a dense one-liner."""

_lock = threading.RLock()

# A key held for this run only, when gateway_key_store is "session". Lives here
# and nowhere else: never written to settings.json, so stopping the app forgets
# it. Survives page reloads because it's server-side, not in the browser.
_fallback_memory = {}


def _memory():
    if has_app_context():
        return current_app.extensions["gusnotebook"].settings_memory
    return _fallback_memory


# --- settings ---

def load_settings():
    """User settings merged over the defaults, with the session key applied."""
    data = {}
    try:
        with open(str(SETTINGS_PATH), encoding="utf-8") as f:
            data = json.load(f) or {}
    except (OSError, ValueError):
        data = {}
    s = {**DEFAULTS, **{k: v for k, v in data.items() if v is not None}}
    if _memory().get("key"):
        s["gateway_key"] = _memory().get("key")
    return s


def save_settings(updates):
    """Merge `updates` into settings.json. Returns the new settings.

    A key is routed by `gateway_key_store`: "session" keeps it in memory for
    this run, "disk" writes it to settings.json.
    """
    with _lock:
        current = load_settings()
        incoming = {k: v for k, v in (updates or {}).items() if k in DEFAULTS}

        # The mask is what the browser was shown in place of the stored key, so
        # getting it back means "unchanged" — not "set the key to bullets".
        if incoming.get("gateway_key") == KEY_MASK:
            incoming.pop("gateway_key")

        store = incoming.get("gateway_key_store") or current.get("gateway_key_store")
        on_disk = dict(current)
        on_disk.pop("gateway_key", None)      # decided below, never carried over
        on_disk.update(incoming)

        if "gateway_key" in incoming:
            key = incoming["gateway_key"]
            if store == "disk":
                _memory()["key"] = None
                on_disk["gateway_key"] = key
            else:
                _memory()["key"] = key or None
                on_disk["gateway_key"] = ""
        elif store == "disk":
            # Switching to "disk" persists the key already held in memory,
            # so the toggle alone is enough — no need to retype it.
            if _memory().get("key"):
                on_disk["gateway_key"] = _memory().get("key")
                _memory()["key"] = None
            else:
                on_disk["gateway_key"] = current.get("gateway_key", "")
        else:
            # Switching to "session": stop storing it, keep it usable this run.
            if current.get("gateway_key"):
                _memory()["key"] = _memory().get("key") or current["gateway_key"]
            on_disk["gateway_key"] = ""

        # Same directory as the target, so os.replace below is an atomic rename
        # rather than a cross-device copy.
        fd, tmp = tempfile.mkstemp(dir=str(SETTINGS_PATH.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(on_disk, f, indent=2, sort_keys=True)
            # Settings may hold a credential: readable by this user only.
            os.chmod(tmp, 0o600)
            os.replace(tmp, str(SETTINGS_PATH))
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return load_settings()


def settings_view():
    """Settings plus the read-only facts the modal displays alongside them.

    The stored key is replaced with KEY_MASK: the modal needs to show *that a
    key is set*, not what it is.
    """
    from . import terminals               # deferred: terminals imports llm

    s = load_settings()
    url, key, source = gateway_config(with_source=True)
    safe = dict(s)
    safe["gateway_key"] = KEY_MASK if s.get("gateway_key") else ""
    return {
        "settings": safe,
        "models": KNOWN_MODELS,
        # The restriction presets, so the modal and the per-session editor build
        # their checkboxes from the same list the rules come from. Sent rather
        # than duplicated in the template: a label that disagrees with what its
        # rules actually block is worse than no label.
        "restrictions": [
            {"key": k, "label": terminals.RESTRICTION_LABELS.get(k, k),
             "rules": len(terminals.RESTRICTION_RULES[k])}
            for k in terminals.RESTRICTION_RULES
        ],
        "gateway": url,
        "has_key": bool(key),
        "key_source": source,
        # Where all this is written. Shown in the modal because "installed with
        # uv, so where did my settings go?" is otherwise unanswerable from the
        # UI, and read by the test suites so they check the file the app is
        # actually using rather than one relative to their own cwd.
        "state_dir": str(paths.state_dir()),
        "settings_path": str(SETTINGS_PATH),
    }


# --- gateway ---

def _dotenv():
    """`.env` values, from the project you launched in and from the state dir.

    Two places because they answer different questions. The project's own `.env`
    is where a key for *this* work belongs and is what a user expects to be
    picked up; the state directory's is the machine-wide fallback for someone who
    doesn't want to repeat it per project. Project wins — it's the more specific
    of the two.
    """
    try:
        from dotenv import dotenv_values
    except Exception:
        return {}
    out = {}
    for path in (paths.state_dir() / ".env", paths.work_dir() / ".env"):
        try:
            out.update({k: v for k, v in dotenv_values(path).items() if v})
        except Exception:
            continue
    return out


def gateway_config(with_source=False):
    """The gateway URL and key, and where the key came from.

    Settings win over the environment: the modal is the thing the user just
    edited, so it would be confusing for a stale shell variable to override it.
    An empty setting falls through to the environment, then .env.
    """
    s = load_settings()
    url = s.get("gateway_url") or ""
    key = s.get("gateway_key") or ""
    # load_settings() overlays the session key, so distinguish the two here:
    # "this run only" and "written to settings.json" read very differently.
    source = ("session" if _memory().get("key") else "settings") if key else None

    if not url or not key:
        env_url = os.environ.get("AI_GATEWAY_URL")
        env_key = os.environ.get("AI_GATEWAY_KEY")
        if not env_url or not env_key:
            cfg = _dotenv()
            env_url = env_url or cfg.get("AI_GATEWAY_URL")
            env_key = env_key or cfg.get("AI_GATEWAY_KEY")
            fallback = ".env"
        else:
            fallback = "environment"
        # No default: a gateway hostname is site-specific, and one baked in here
        # would name the author's employer in every copy of this file. Absent a
        # URL, client() raises and says which variable to set.
        url = url or env_url or ""
        if not key:
            key = env_key or ""
            source = fallback if key else None

    return (url, key, source) if with_source else (url, key)


def client():
    url, key = gateway_config()
    if not key:
        raise ValueError(
            "no API key — set AI_GATEWAY_KEY in the environment or .env")
    # Checked here rather than defaulted: without this, an unset URL would build
    # "/azure-openai" and fail as an obscure connection error instead of saying
    # which variable is missing.
    if not url:
        raise ValueError(
            "no gateway URL — set AI_GATEWAY_URL in the environment or .env, "
            "or fill in Gateway URL in ⚙ Settings")
    from openai import AzureOpenAI
    return AzureOpenAI(
        api_key=key,
        api_version=API_VERSION,
        azure_endpoint=f"{url}/azure-openai",
        timeout=120.0,
        max_retries=1,
    )


# --- generation ---

FENCE = re.compile(r"^\s*```[a-zA-Z0-9_+-]*\s*\n(.*?)\n?\s*```\s*$", re.S)


def strip_fences(text):
    """Models fence code even when told not to — unwrap a single fenced block."""
    m = FENCE.match(text or "")
    return (m.group(1) if m else (text or "")).strip("\n")


def _context_block(context):
    """Earlier cells, oldest first, as one prompt section."""
    parts = []
    for c in context or []:
        source = (c.get("source") or "").strip()
        if not source:
            continue
        block = ["```python", source, "```"]
        out = (c.get("output") or "").strip()
        if out:
            block += ["output:", "```", out[:1200], "```"]
        parts.append("\n".join(block))
    return "\n\n".join(parts)


def generate(prompt, context=None, variables=None):
    """Translate `prompt` into notebook Python. One model call.

    `context` is a list of {source, output} for earlier cells; `variables` is
    a short description of what's live in the kernel. Returns
    {'code', 'model', 'usage'}.
    """
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("type what you want the code to do first")

    s = load_settings()
    model = s["inline_llm_model"]

    user = [f"Write the code for this request:\n\n{prompt}"]
    ctx = _context_block(context)
    if ctx:
        user += ["", "Earlier cells in this kernel session, oldest first:", ctx]
    if variables:
        user += ["", "Names currently defined in the kernel:", variables]

    system = SYSTEM_PROMPT
    extra = (s.get("inline_llm_instructions") or "").strip()
    if extra:
        system += "\n\nAdditional instructions from the user:\n" + extra

    response = client().chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": "\n".join(user)},
        ],
        max_completion_tokens=int(s.get("inline_llm_max_tokens") or 1200),
    )

    code = strip_fences((response.choices[0].message.content or "").strip())
    if not code:
        raise ValueError(f"{model} returned nothing — try rephrasing")

    usage = response.usage
    return {
        "code": code,
        "model": model,
        "usage": {
            "prompt_tokens": getattr(usage, "prompt_tokens", None),
            "completion_tokens": getattr(usage, "completion_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
        } if usage else {},
    }
