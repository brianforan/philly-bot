"""LLM-based relevance filter for scraped EventItems.

Sends items to Claude Haiku in batches and drops anything that doesn't
actually match its channel profile (events / music / closures).

Designed to run after scraping and dedup, before DB upsert.
Fails open: if the API is unavailable or the key is missing, all items pass.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

import anthropic

from scraper import EventItem

logger = logging.getLogger(__name__)

_BATCH_SIZE = 25  # items per API call

_SYSTEM_PROMPT = """\
You are a relevance filter for a Philadelphia city Discord bot that has three channels:
- #events: upcoming in-person Philadelphia events (concerts, festivals, markets, sports, exhibitions, community gatherings, etc.)
- #music: upcoming live music performances and shows in Philadelphia
- #closures: Philadelphia road, street, or transit closures and service alerts

You will receive a JSON array of scraped items, each with:
  - index: integer
  - title: string
  - location: string or null
  - source: the scraper source name
  - channel: "events", "music", or "closures"

Decide whether each item genuinely belongs in its assigned channel. Reject items that are:
- News articles, blog posts, or editorial content (not actual upcoming events)
- Press releases or announcements without a specific event date
- Items clearly outside Philadelphia or with no Philadelphia connection
- For #music: items that are not live performances (e.g., album reviews, interviews)
- For #closures: items that are not actual road/transit closures or service alerts
- Spam, ads, or clearly malformed scraper output
- Vague titles with no event substance (e.g., "Things to Do This Weekend")

Accept items with partial information if the title clearly indicates a real upcoming event/show/closure.
When in doubt, accept — false negatives (removing real events) are worse than false positives.

Respond with ONLY a JSON object: {"keep": [list of index integers to keep]}
No explanation, no markdown, just the JSON object."""


def _build_user_message(batch: list[tuple[int, EventItem]]) -> str:
    payload = [
        {
            "index": idx,
            "title": item.title,
            "location": item.location_name,
            "source": item.source,
            "channel": item.channel_type,
        }
        for idx, item in batch
    ]
    return json.dumps(payload, ensure_ascii=False)


async def validate_items(items: list[EventItem]) -> list[EventItem]:
    """Filter items through Haiku. Returns the kept subset. Fails open on error."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY not set — skipping LLM validation")
        return items

    if not items:
        return items

    client = anthropic.AsyncAnthropic(api_key=api_key)
    kept_indices: set[int] = set()

    # Process in batches; original list index is the stable identifier
    for batch_start in range(0, len(items), _BATCH_SIZE):
        batch = [(i, items[i]) for i in range(batch_start, min(batch_start + _BATCH_SIZE, len(items)))]
        try:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                system=[
                    {
                        "type": "text",
                        "text": _SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[
                    {"role": "user", "content": _build_user_message(batch)}
                ],
            )
            raw = response.content[0].text.strip()
            # Strip markdown code fences if model wraps the response
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            result = json.loads(raw)
            keep_list: list[int] = result.get("keep", [])
            kept_indices.update(keep_list)
            logger.info(
                "validator: batch %d–%d: kept %d/%d items",
                batch_start,
                batch_start + len(batch) - 1,
                len(keep_list),
                len(batch),
            )
        except json.JSONDecodeError as exc:
            logger.warning(
                "validator: JSON parse failed for batch %d–%d, keeping all: %s",
                batch_start, batch_start + len(batch) - 1, exc,
            )
            kept_indices.update(i for i, _ in batch)
        except anthropic.APIError as exc:
            logger.warning(
                "validator: API error for batch %d–%d, keeping all: %s",
                batch_start, batch_start + len(batch) - 1, exc,
            )
            kept_indices.update(i for i, _ in batch)

    result_items = [items[i] for i in range(len(items)) if i in kept_indices]
    logger.info(
        "validator: %d/%d items passed for channel '%s'",
        len(result_items),
        len(items),
        items[0].channel_type if items else "?",
    )
    return result_items
