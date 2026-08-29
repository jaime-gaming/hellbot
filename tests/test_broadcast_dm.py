"""Host DM broadcast: a markdown message to every participant (host-only)."""

from __future__ import annotations

from typing import Any, Optional
from unittest.mock import MagicMock

import aiohttp
import discord
import pytest

from hell.announcer import Announcer
from hell.broadcast import dm_participants
from hell.cog import HellCommands
from hell.engine import HellEngine
from hell.monitor import VoiceMonitor
from hell.storage import Store
from tests.conftest import HOUR, T0, make_config, obs, start
from tests.test_command_flows import FakeAuthor, run


class FakeDMUser:
    """Stands in for a discord.User: remembers DMs, can be configured to fail."""

    def __init__(self, uid: int) -> None:
        self.id = uid
        self.sent: list[str] = []
        self.attempts = 0
        self.exc: Optional[BaseException] = None

    async def send(self, content: str) -> None:
        self.attempts += 1
        if self.exc is not None:
            raise self.exc
        self.sent.append(content)


class FakeBot:
    """Minimal user-cache surface used by hell.broadcast.dm_participants."""

    def __init__(self, users: dict[int, FakeDMUser]) -> None:
        self._users = users
        self.fetched: list[int] = []

    def get_guild(self, _gid: int) -> None:
        return None

    def get_user(self, uid: int) -> Optional[FakeDMUser]:
        return self._users.get(uid)

    async def fetch_user(self, uid: int) -> Optional[FakeDMUser]:
        self.fetched.append(uid)
        return self._users.get(uid)


class FakeContext:
    def __init__(self, bot: Any, author: FakeAuthor, guild_id: Optional[int] = None) -> None:
        self.bot = bot
        self.author = author
        self.guild = type("G", (), {"id": guild_id})() if guild_id else None
        self.sent: list[dict[str, Any]] = []

    async def send(self, content: Optional[str] = None, **kwargs: Any) -> None:
        self.sent.append({"content": content, **kwargs})


@pytest.fixture
def bcog(tmp_path):
    """HellCommands wired to a fake bot — DM broadcasts never touch a channel."""
    config = make_config(tmp_path, dm_delay_seconds=0.0)
    store = Store(config.database_path)
    engine = HellEngine(store, config)
    bot = FakeBot({})
    announcer = Announcer(bot, config, engine)
    monitor = VoiceMonitor(bot, config, engine, announcer)
    cog = HellCommands(bot, config, engine, monitor)
    yield cog, bot, config, engine
    store.close()


def _op_ctx(bot: Any, config: Any) -> FakeContext:
    return FakeContext(bot, FakeAuthor(uid=config.log_dm_user_id, name="Op"))


MSG = "**Maintenance** at 18:00 — *see you after*"


def test_dm_broadcast_reaches_every_participant_with_markdown(bcog):
    cog, bot, config, engine = bcog
    start(engine, T0, 1, 2, 3)
    engine.tick(obs(T0 + HOUR, 1, 2, 3))
    dms = {uid: FakeDMUser(uid) for uid in (1, 2, 3)}
    bot._users.update(dms)

    ctx = _op_ctx(bot, config)
    run(cog._exec_broadcast(ctx, level="info", target="participants", message=MSG))

    for uid, user in dms.items():
        assert user.sent == [MSG], f"user {uid} got {user.sent!r}"
    summary = ctx.sent[-1]["content"]
    assert "3 of 3 participants received the message" in summary
    assert "closed DMs" not in summary
    assert "could not be delivered" not in summary


def test_dm_broadcast_counts_blocked_and_unknown_users(bcog):
    cog, bot, config, engine = bcog
    start(engine, T0, 1, 2, 3)
    engine.tick(obs(T0 + HOUR, 1, 2, 3))
    ok = FakeDMUser(1)
    blocked = FakeDMUser(2)
    blocked.exc = discord.Forbidden(MagicMock(), "user has DMs closed")
    bot._users.update({1: ok, 2: blocked})  # user 3 is unknown to the bot

    ctx = _op_ctx(bot, config)
    run(cog._exec_broadcast(ctx, level="info", target="participants", message=MSG))

    assert ok.sent == [MSG]
    assert blocked.sent == []
    assert bot.fetched == [3]  # unknown user was resolved via fetch, then failed
    summary = ctx.sent[-1]["content"]
    assert "1 of 3 participants" in summary
    assert "1 with closed DMs" in summary
    assert "could not be delivered" in summary


def test_dm_broadcast_empty_roster_says_nobody(bcog):
    cog, bot, config, _engine = bcog
    ctx = _op_ctx(bot, config)
    run(cog._exec_broadcast(ctx, level="info", target="participants", message=MSG))
    assert "nobody to dm" in ctx.sent[-1]["content"].lower()
    assert bot.fetched == []


def test_dm_broadcast_prefix_aliases_and_host_gate(bcog):
    cog, bot, config, engine = bcog
    start(engine, T0, 1)
    engine.tick(obs(T0 + HOUR, 1))
    dms = {1: FakeDMUser(1)}
    bot._users.update(dms)

    # "dm" is accepted as an alias of the participants target.
    ctx = _op_ctx(bot, config)
    run(cog._exec_broadcast(ctx, level="warning", target="dm", message=MSG))
    assert dms[1].sent == [MSG]
    assert "1 of 1 participants" in ctx.sent[-1]["content"]

    # Non-host, non-operator: refused, nothing sent.
    ctx_outsider = FakeContext(bot, FakeAuthor(uid=12345, name="Rando"))
    run(cog._exec_broadcast(ctx_outsider, level="info", target="participants", message=MSG))
    assert "cannot use this command" in ctx_outsider.sent[-1]["content"]
    assert dms[1].attempts == 1  # only the host's broadcast was delivered


def test_dm_participants_stops_on_network_down():
    """A dead connection ends the run honestly — the rest is not faked."""
    ok = FakeDMUser(1)
    dead = FakeDMUser(2)
    dead.exc = aiohttp.ClientOSError(111, "Connection refused")
    never = FakeDMUser(3)
    bot = FakeBot({1: ok, 2: dead, 3: never})

    result = run(dm_participants(bot, [1, 2, 3], MSG, delay=0))

    assert result.network_down is True
    assert result.delivered == 1
    assert result.blocked == 0
    assert result.failed == []
    assert never.attempts == 0  # did not keep burning time against a dead net


def test_dm_participants_unknown_user_counts_failed():
    bot = FakeBot({})  # nobody resolvable
    result = run(dm_participants(bot, [7, 8], MSG, delay=0))
    assert result.delivered == 0
    assert result.failed == [7, 8]
    assert result.total == 2
    assert result.ok is False
