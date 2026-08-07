"""Error help — one LLM call that explains a traceback and proposes a fix.

Talks to an Azure-OpenAI-compatible gateway. Credentials come from
AI_GATEWAY_URL / AI_GATEWAY_KEY in the environment or .env — resolved by
`llm.gateway_config()`, so there's one answer for the whole app.
"""

import os

MODEL = os.environ.get("HELP_MODEL", "DeepSeek-V4-Pro")
API_VERSION = "2025-02-01-preview"

SYSTEM_PROMPT = """You help a developer fix an error in a Python notebook cell.

Answer in GitHub-flavored markdown, tightly scoped and skimmable:

**Cause** — one or two sentences on what actually went wrong.
**Fix** — the corrected code in a ```python block. Keep the user's intent and
variable names; change only what's needed.
**Why** — one or two sentences, only if it isn't obvious from the fix.

Rules: no preamble, no restating the traceback, no "I hope this helps". If the
cause is ambiguous, state the most likely one and mention the alternative in a
single clause. Never invent APIs or columns that aren't in the code shown."""


def _config():
    """Same gateway credentials the inline LLM uses, settings included.

    Deferred import: llm imports nothing from here, so this stays one-way.
    """
    from . import llm
    return llm.gateway_config()


def _client():
    url, key = _config()
    if not key:
        raise RuntimeError(
            "no API key — set AI_GATEWAY_KEY in the environment or .env")
    if not url:
        raise RuntimeError(
            "no gateway URL — set AI_GATEWAY_URL in the environment or .env, "
            "or fill in Gateway URL in ⚙ Settings")
    from openai import AzureOpenAI
    return AzureOpenAI(
        api_key=key,
        api_version=API_VERSION,
        azure_endpoint=f"{url}/azure-openai",
        timeout=90.0,
        max_retries=1,
    )


def _error_text(outputs, limit=4000):
    """Pull the error text out of a cell's nbformat outputs."""
    parts = []
    for o in outputs or []:
        if o.get("output_type") == "error":
            tb = "\n".join(o.get("traceback") or [])
            parts.append(tb.strip() or f"{o.get('ename')}: {o.get('evalue')}")
        elif o.get("output_type") == "stream" and o.get("name") == "stderr":
            parts.append(o.get("text", ""))
    text = "\n".join(p for p in parts if p).strip()
    if len(text) > limit:
        # Keep the head and tail — the exception line is usually last.
        text = text[: limit // 2] + "\n...\n" + text[-limit // 2:]
    return text


def strip_ansi(text):
    import re
    return re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)


def explain(source, outputs, context=None):
    """One call to the model. Returns {'markdown': str, 'model': str, 'usage': dict}."""
    error = strip_ansi(_error_text(outputs))
    if not error:
        raise ValueError("this cell has no error output to explain")

    prompt = [
        "The code in this notebook cell raised an error.",
        "",
        "```python",
        (source or "").strip() or "# (empty cell)",
        "```",
        "",
        "Traceback:",
        "```",
        error,
        "```",
    ]
    if context:
        prompt += ["", "Earlier cells in the same kernel session (for context):",
                   "```python", context.strip(), "```"]

    client = _client()
    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "\n".join(prompt)},
        ],
        max_completion_tokens=900,
    )

    choice = response.choices[0].message.content or ""
    usage = response.usage
    return {
        "markdown": choice.strip(),
        "model": MODEL,
        "usage": {
            "prompt_tokens": getattr(usage, "prompt_tokens", None),
            "completion_tokens": getattr(usage, "completion_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
        } if usage else {},
    }
