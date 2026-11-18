# philly-bot

A Discord bot that surfaces Philadelphia events, live music and road/transit closures as
auto-updating weekly digests, sorted into themed channels. It polls public sources, filters
to Philly, de-duplicates, classifies each event with Claude, and posts one digest per channel
each morning, then edits it in place through the week. A separate `#traffic-alerts` channel
shows nearby PennDOT road work and incidents, refreshed every 15 minutes.

## Quick start

Requires Python 3.11 (or Docker).

```bash
cp .env.example .env        # then fill in the values
python3.11 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python bot.py
```

Or with Docker (the SQLite DB lives on the `bot-data` volume):

```bash
docker compose up -d --build
```

Invite the bot with the Manage Channels permission, then run `/setup` in your server. All
slash commands are admin-only.

## Configuration

| Variable | Purpose | Required |
|---|---|---|
| `DISCORD_BOT_TOKEN` | Discord bot token | Yes |
| `CENTER_NAME`, `CENTER_LAT`, `CENTER_LON` | Center point for distance labels and near-home filters | Yes |
| `ANTHROPIC_API_KEY` | Classification, relevance filter, major-event discovery, `/ask` | Recommended |
| `TICKETMASTER_API_KEY` | Ticketmaster Discovery source | No |
| `GOOGLE_MAPS_API_KEY` | Geocoding (falls back to Nominatim) | No |
| `PENNDOT_RCRS_USERNAME`, `PENNDOT_RCRS_PASSWORD` | PennDOT RCRS feed for `#traffic-alerts` | No |
| `DB_PATH` | SQLite file path (default `philly_bot.db`) | No |

## Project structure

| File | Role |
|---|---|
| `bot.py` | Discord client and slash commands |
| `scheduler.py` | Daily digest job, 15-minute PennDOT job, channel management |
| `scraper.py` | Per-source fetchers and the filter/dedup pipeline |
| `classify_events.py`, `validator.py`, `discovery.py`, `ask.py` | Claude-backed classification, filtering, web-search discovery and Q&A |
| `digest.py` | Formats events into Discord embeds |
| `penndot.py` | PennDOT RCRS client |
| `store.py` | SQLite access |
| `tools/` | Standalone diagnostic scripts, not used at runtime |

`CLAUDE.md` has the detailed architecture notes.

## Design decisions

See [DECISIONS.md](DECISIONS.md).

## License

MIT, see [LICENSE](LICENSE).
