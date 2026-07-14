# Agent Instructions for shipply-doc-review

This file guides AI assistants working on the `shipply-doc-review` bot handler.

## Bot overview

`bots/shipply-doc-review/src/shipply_doc_review/shipply_doc_review.py` is a Pacto bot handler built on the
`pacto_bot_sdk` SDK. It connects to the `pacto-bot-api` daemon and responds to
incoming events.

## Capabilities

Check `pacto-bot-api.toml` (project root) for the configured capabilities for
this bot. Common capabilities:

- `ReadMessages` — receive decrypted DMs and group messages
- `SendMessages` — send replies as the bot
- `ManageProfile` — update the bot's kind:0 profile

## SDK reference

The bot depends on the `pacto-bot-sdk` PyPI package. Use the `python-pacto-bot`
skill for the SDK API and patterns.

## How to modify this bot

1. Read the `python-pacto-bot` skill for the SDK API and patterns.
2. Open `bots/shipply-doc-review/src/shipply_doc_review/shipply_doc_review.py`.
3. Use `@bot.command("/name")` to add slash commands or edit existing handlers.
4. Keep `@bot.default` for unrecognized commands.
5. Run tests:
   ```bash
   cd bots/shipply-doc-review
   pytest
   ```

## How to run the bot locally

```bash
cd bots/shipply-doc-review
python -m shipply_doc_review
```

Tests import the public API via `from shipply_doc_review import bot`.

## How to add commands

Add a new handler function:

```python
@bot.command("/price")
async def price(event, bot):
    return {
        "event_id": event.event_id,
        "action": "reply",
        "content": "price placeholder response",
    }
```

After adding commands, update the tests in `bots/shipply-doc-review/tests/test_bot.py`
if necessary.

## Packaging and deployment

- `Dockerfile` — builds a container image for this bot.
- `systemd.service` — runs the bot as a non-root `pacto-bot` service on the host.
- `README.md` — human-facing run and deploy instructions for this bot.

Do not delete or rename these files without updating the project-level
`docker-compose.yml` and `pacto-bot-api.toml`.
