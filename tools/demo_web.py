"""Demo harness: serve the real web dashboard with a simulated live bot.

Runs the exact production stack (hell.web server + HellBot._push_web_snapshot)
against an in-memory event with fake contestants, so the dashboard can be
checked in a browser without connecting to Discord.
"""

from __future__ import annotations

import asyncio
import random
import tempfile
from pathlib import Path

from hell import web as hellweb
from hell.bot import build_bot
from hell.config import Config
from hell.models import ParticipantRef
from hell.timeutil import now_ts

PORT = 8080


async def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    config = Config(
        token="demo",
        guild_id=1,
        voice_channel_id=1539756705997652079,
        announce_channel_id=2,
        gamenight_host_role_id=3,
        clanker_role_id=4,
        database_path=tmp / "demo.sqlite3",
    )
    bot = build_bot(config)

    # A fake, already-running event with a lively leaderboard.
    now = now_ts()
    start_ts = now - 73 * 3600 - 24 * 60  # 73h 24m elapsed
    contestants = [
        ParticipantRef(100, "Ash"),
        ParticipantRef(200, "Vera"),
        ParticipantRef(300, "Milo"),
        ParticipantRef(400, "Jaime"),
        ParticipantRef(500, "Kira"),
    ]
    bot.engine.start(
        now=start_ts,
        guild_id=1,
        voice_channel_id=config.voice_channel_id,
        announce_channel_id=2,
        started_by=99,
        initial_participants=contestants,
    )
    rng = random.Random(11)
    for p in contestants:
        seconds = rng.uniform(40 * 3600, 72 * 3600)
        bot.engine.add_user_bonus_seconds(p.user_id, p.display_name, seconds, start_ts)
    bot.engine._last_participants = tuple(contestants[:3])
    bot.log_stream = None

    runner = await hellweb.start_server(PORT)
    if runner is None:
        raise SystemExit("could not start the web server")

    print(f"Dashboard live on http://0.0.0.0:{PORT}/  (operator page: /dev)")
    cycle = 0
    while True:
        await bot._push_web_snapshot()  # exactly what the bot does every 5s
        cycle += 1
        if cycle % 12 == 0:  # nudge the clock every minute so numbers move
            bot.engine._last_participants = tuple(
                random.sample(contestants, k=rng.randint(2, len(contestants)))
            )
        await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
