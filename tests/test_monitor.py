"""VC monitoring: who counts, who gets kicked, and what the alive check sees."""

from __future__ import annotations

import asyncio

import discord
import pytest

from hell.announcer import Announcer
from hell.monitor import VoiceMonitor


class FakeRole:
    def __init__(self, rid: int):
        self.id = rid


class FakeMember:
    def __init__(self, uid: int, name: str, *, bot: bool = False, roles=()):
        self.id = uid
        self.display_name = name
        self.bot = bot
        self.roles = [FakeRole(r) for r in roles]
        self.moved_to: list[object] = []

    async def move_to(self, target, reason=None):
        self.moved_to.append(target)


class FakeVoiceChannel(discord.abc.Messageable):
    """Voice channels have their own text chat in Discord — so does this one."""

    async def _get_channel(self):
        return self

    def __init__(self, members):
        self.id = 1539756705997652079
        self.name = "hell"
        self.members = list(members)
        self.sent: list[dict] = []

    async def send(self, content=None, **kwargs):
        message = type(
            "FakeVCMessage",
            (),
            {"id": 900 + len(self.sent), "channel": self, "content": content},
        )()
        self.sent.append({"content": content, **kwargs})
        return message


class FakeBot:
    def __init__(self, channel=None, guild=None):
        self._channel = channel
        self._guild = guild

    def is_ready(self):
        return True

    def is_closed(self):
        return False

    def get_channel(self, _cid):
        return self._channel

    def get_guild(self, _gid):
        return self._guild


@pytest.fixture
def monitor(config, engine):
    bot = FakeBot()
    return VoiceMonitor(bot, config, engine, Announcer(bot, config, engine))


def run(coro):
    return asyncio.run(coro)


def test_bots_and_clankers_are_excluded_from_participants(monitor, config, monkeypatch):
    human = FakeMember(1, "Human")
    a_bot = FakeMember(2, "SomeBot", bot=True)
    clanker = FakeMember(3, "Clanker", roles=[config.clanker_role_id])
    clanker_bot = FakeMember(4, "ClankerBot", bot=True, roles=[config.clanker_role_id])
    channel = FakeVoiceChannel([human, a_bot, clanker, clanker_bot])
    monkeypatch.setattr(monitor, "voice_channel", lambda: channel)

    humans, clankers = run(monitor.collect())
    assert [p.user_id for p in humans] == [1]        # only the real human counts
    assert [m.id for m in clankers] == [3]           # the clanker bot is just ignored


def test_clankers_are_disconnected(monitor, config, monkeypatch):
    clanker = FakeMember(3, "Clanker", roles=[config.clanker_role_id])
    channel = FakeVoiceChannel([FakeMember(1, "Human"), clanker])
    monkeypatch.setattr(monitor, "voice_channel", lambda: channel)

    humans, clankers = run(monitor.collect())
    run(monitor.kick_clankers(clankers))
    assert clanker.moved_to == [None]                # kicked out of the VC
    assert [p.user_id for p in humans] == [1]


def test_kick_cooldown_avoids_api_hammering(monitor, config, monkeypatch):
    clanker = FakeMember(3, "Clanker", roles=[config.clanker_role_id])
    channel = FakeVoiceChannel([clanker])
    monkeypatch.setattr(monitor, "voice_channel", lambda: channel)
    run(monitor.kick_clankers([clanker]))
    run(monitor.kick_clankers([clanker]))            # immediately again
    assert clanker.moved_to == [None]                # only one attempt


def test_alive_check_pings_only_valid_humans(monitor, config, engine, monkeypatch):
    """The roll call gets its user list from the same filtered snapshot."""
    from tests.conftest import T0, start
    from tests.test_alivecheck import FakeIO

    io = FakeIO()
    monitor.alive_checks.io = io
    channel = FakeVoiceChannel(
        [
            FakeMember(1, "Human"),
            FakeMember(2, "Bot", bot=True),
            FakeMember(3, "Clanker", roles=[config.clanker_role_id]),
        ]
    )
    monkeypatch.setattr(monitor, "voice_channel", lambda: channel)

    start(engine, T0, 1)
    monitor.alive_checks.bind(engine.event_uid, now=T0)
    humans, _ = run(monitor.collect())
    run(monitor.alive_checks.start(T0 + 1, humans))

    _text, pinged = io.sent[0]
    assert pinged == [1]                             # no bot, no clanker


def test_untrusted_observation_returns_none(monitor, monkeypatch):
    monkeypatch.setattr(monitor, "voice_channel", lambda: None)
    assert run(monitor.collect()) is None
    assert monitor.blind_seconds >= 0
