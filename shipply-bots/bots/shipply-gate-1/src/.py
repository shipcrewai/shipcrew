"""Bot handler for shipply-gate-1."""

from __future__ import annotations

from pacto_bot_sdk import Bot, parse_command

bot = Bot(bot_id="shipply-gate-1")


def _command_args(event) -> list[str]:
    """Return the positional arguments passed after the command name.

    Example: `/price btc` -> `['btc']`; `/hello` -> `[]`.
    """
    parsed = parse_command(event.content)
    if not parsed:
        return []
    return parsed.get("args") or []



@bot.command("/vote")
async def vote_handler(event, bot):
    bot.log(f"received /vote: event_id={event.event_id}")
    response = {
        "event_id": event.event_id,
        "action": "reply",
        "content": "vote placeholder response",
    }
    bot.log(f"handled /vote: action={response['action']}")
    return response

@bot.default
async def unknown(event, bot):
    bot.log(f"ignoring unknown command: event_id={event.event_id}")
    return {"event_id": event.event_id, "action": "ignore"}


def main():
    bot.run()


if __name__ == "__main__":
    main()
