"""Ad-hoc Philadelphia Q&A via Claude + web search (the /ask command backend).

A user asks a free-text question ("why is this park fenced off?", "is the
Parkway closed this weekend?") and Claude answers using the server-side
web_search tool, grounded in real pages rather than model memory. Returns the
answer text plus the source URLs it cited so the asker can verify.

Reuses ANTHROPIC_API_KEY and the Philadelphia-biased web_search tool from
discovery.py — no new secret, one place for the search config. Fails soft: any
problem returns an AskResult with ok=False and a short user-facing message;
it never raises.

Accuracy posture: the system prompt forbids guessing — if search turns up
nothing specific, the model says so instead of confabulating, and sources are
returned for verification. Cost posture: web_search uses are capped
(config.ASK_MAX_SEARCHES) and the answer is length-capped; bot.py adds a
per-user cooldown and a hard per-day call ceiling on top.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

import anthropic

import config
from discovery import _web_search_tool, _message_text

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are a local-information assistant for a Philadelphia community Discord.
A user asks a question about the city — events, venues, road or transit closures, parks, \
construction, civic happenings, or "why is X like this right now".

Rules:
- ALWAYS use the web_search tool to find CURRENT, real information before answering. Prefer \
official and local sources: phila.gov and its departments, septa.org, established Philadelphia \
news outlets, official venue/park pages.
- Answer ONLY what the search results actually support. Be concrete and brief.
- If you cannot find a specific answer, say so plainly (e.g. "I couldn't find a specific reason \
for that"), and at most point to who to ask (e.g. Philadelphia Parks & Recreation, SEPTA, 311). \
DO NOT guess, invent reasons, or fill gaps with generic speculation.
- Stay within Philadelphia and the immediate area. If a question is off-topic or not local, \
decline in one sentence.
- Never include personal data about private individuals or unsafe/illegal instructions.

Keep the final answer under ~150 words."""


@dataclass
class AskResult:
    """Outcome of an /ask query. `sources` is a list of (title, url) pairs."""

    text: str
    sources: list[tuple[str, str]] = field(default_factory=list)
    ok: bool = True


def _collect_citations(message) -> list[tuple[str, str]]:
    """Dedup (title, url) pairs the model actually cited from web_search results."""
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for block in getattr(message, "content", None) or []:
        if getattr(block, "type", None) != "text":
            continue
        for cit in getattr(block, "citations", None) or []:
            url = getattr(cit, "url", None)
            if not url or url in seen:
                continue
            seen.add(url)
            out.append((getattr(cit, "title", None) or url, url))
    return out


async def answer_question(question: str, history: list[dict] | None = None) -> AskResult:
    """Answer a Philadelphia question via Claude + web search. Fails soft.

    `history`, when given, is the prior turns of a conversation as plain
    text-only messages ([{"role": "user"|"assistant", "content": str}, ...]) so
    the model can resolve clarifying follow-ups. It is replayed before the new
    question; the caller (bot.py) owns trimming and expiry.

    Returns an AskResult; ok=False carries a short, user-safe message and never
    raises so the calling slash command can always reply.
    """
    question = (question or "").strip()
    if not question:
        return AskResult(text="Ask me a question about Philadelphia.", ok=False)

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.warning("ask: ANTHROPIC_API_KEY not set — cannot answer")
        return AskResult(text="The question service isn't configured right now.", ok=False)

    messages = list(history or []) + [{"role": "user", "content": question}]
    client = anthropic.AsyncAnthropic(api_key=api_key)
    try:
        message = await client.messages.create(
            model=config.ASK_MODEL,
            max_tokens=config.ASK_MAX_TOKENS,
            system=[{"type": "text", "text": _SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=messages,
            tools=[_web_search_tool(config.ASK_MAX_SEARCHES)],
        )
    except anthropic.APIError as exc:
        logger.warning("ask: API error: %s", exc)
        return AskResult(text="I couldn't reach the search service just now — try again later.", ok=False)
    except Exception as exc:
        logger.error("ask: unexpected error: %s", exc, exc_info=True)
        return AskResult(text="Something went wrong answering that — try again later.", ok=False)

    text = _message_text(message).strip()
    if not text:
        logger.info("ask: empty answer for question %r", question[:80])
        return AskResult(text="I couldn't find a specific answer to that.", ok=False)
    return AskResult(text=text, sources=_collect_citations(message), ok=True)
