"""The bot object itself: wiring, event routing and shutdown.

`hell/bot.py` is the one module a unit test cannot reach through the engine, so
it gets its own — no gateway connection is made anywhere here.
"""

from __future__ import annotations

import asyncio
import sqlite3
from unittest.mock import MagicMock

import discord
import pytest

from hell.bot import HellBot, build_bot
from hell.config import ConfigError
from tests.test_monitor import FakeMember


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def bot(config):
    instance = build_bot(config)
    yield instance
    instance.store.close()


# ------------------------------------------------------------------- wiring


def test_every_subsystem_is_wired_together(bot, config):
    assert isinstance(bot, HellBot)
    assert bot.engine.store is bot.store
    assert bot.announcer.engine is bot.engine
    assert bot.monitor.engine is bot.engine
    assert bot.monitor.announcer is bot.announcer
    assert bot.monitor.reports.engine is bot.engine
    assert bot.log_stream.config is config


def test_required_intents_are_requested(bot):
    assert bot.intents.members       # needed to see who is in the VC
    assert bot.intents.voice_states
    assert bot.intents.guilds


def test_the_log_stream_captures_records_before_login(bot):
    import logging

    logging.getLogger("hell.test").warning("early warning")
    lines, _dropped = bot.log_stream.handler.drain()
    assert any("early warning" in line for line in lines)


# ------------------------------------------------------------ event routing


def test_messages_are_offered_to_the_alive_check(bot, monkeypatch):
    seen: list[str] = []

    async def handler(message):
        seen.append(message.content)

    monkeypatch.setattr(bot.monitor, "handle_message", handler)
    monkeypatch.setattr(bot, "process_commands", _noop)

    message = MagicMock(spec=discord.Message)
    message.guild = MagicMock()
    message.author.bot = False
    message.content = "Yes"

    run(bot.on_message(message))
    assert seen == ["Yes"]


def test_dms_and_bot_messages_are_ignored(bot, monkeypatch):
    seen: list[str] = []

    async def handler(message):
        seen.append(message.content)

    monkeypatch.setattr(bot.monitor, "handle_message", handler)
    monkeypatch.setattr(bot, "process_commands", _noop)

    dm = MagicMock(spec=discord.Message)
    dm.guild = None
    dm.author.bot = False
    dm.content = "Yes"
    run(bot.on_message(dm))

    from_bot = MagicMock(spec=discord.Message)
    from_bot.guild = MagicMock()
    from_bot.author.bot = True
    from_bot.content = "Yes"
    run(bot.on_message(from_bot))

    assert seen == []


def test_a_failing_message_handler_never_escapes(bot, monkeypatch, caplog):
    import logging

    async def boom(_message):
        raise RuntimeError("handler exploded")

    monkeypatch.setattr(bot.monitor, "handle_message", boom)
    monkeypatch.setattr(bot, "process_commands", _noop)

    message = MagicMock(spec=discord.Message)
    message.guild = MagicMock()
    message.author.bot = False
    message.content = "Yes"

    with caplog.at_level(logging.ERROR, logger="hell"):
        run(bot.on_message(message))          # must not raise
    assert "handler exploded" in caplog.text


def test_clankers_are_kicked_the_moment_they_join(bot, config):
    member = FakeMember(9, "Clank3r", roles=[config.clanker_role_id])
    after = MagicMock(spec=discord.VoiceState)
    after.channel = MagicMock()
    after.channel.id = config.voice_channel_id

    run(bot.on_voice_state_update(member, MagicMock(spec=discord.VoiceState), after))

    assert member.moved_to == [None]


def test_joining_another_channel_is_ignored(bot, config):
    member = FakeMember(9, "Clank3r", roles=[config.clanker_role_id])
    after = MagicMock(spec=discord.VoiceState)
    after.channel = MagicMock()
    after.channel.id = 999999                  # a different VC

    run(bot.on_voice_state_update(member, MagicMock(spec=discord.VoiceState), after))

    assert member.moved_to == []


def test_regular_members_are_left_alone(bot, config):
    member = FakeMember(3, "Alice")
    after = MagicMock(spec=discord.VoiceState)
    after.channel = MagicMock()
    after.channel.id = config.voice_channel_id

    run(bot.on_voice_state_update(member, MagicMock(spec=discord.VoiceState), after))

    assert member.moved_to == []


# ---------------------------------------------------------------- shutdown


def test_close_stops_the_loops_and_the_database(config):
    instance = build_bot(config)

    async def scenario():
        await instance.close()

    run(scenario())

    assert not instance.monitor._monitor_loop.is_running()
    with pytest.raises(sqlite3.ProgrammingError):
        instance.store.load_state()            # the connection really is closed


# ------------------------------------------------------------ configuration


def test_main_exits_cleanly_without_configuration(monkeypatch, capsys):
    from hell import bot as bot_module

    def missing():
        raise ConfigError("Missing required environment variable: DISCORD_TOKEN")

    monkeypatch.setattr(bot_module.Config, "from_env", staticmethod(missing))

    with pytest.raises(SystemExit) as exit_info:
        bot_module.main()

    assert exit_info.value.code == 2
    assert "DISCORD_TOKEN" in capsys.readouterr().err


async def _noop(*_args, **_kwargs):
    return None
