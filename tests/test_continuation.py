"""Tests for the 160h "keep on Hell?" vote and the Hell 2 gate."""

from __future__ import annotations

from typing import Any

import discord
import pytest
from discord.ext import commands

from hell.announcer import Announcer
from hell.continuation import ContinuationManager
from hell.milestones import TOTAL_SECONDS
from hell.models import EventStatus
from hell.monitor import VoiceMonitor
from hell.timeutil import now_ts
from tests.conftest import T0, obs, start
from tests.test_command_flows import FakeAuthor, FakeInteraction, _member, run
from tests.test_integration import FakeTextChannel
from tests.test_monitor import FakeMember, FakeVoiceChannel


@pytest.fixture
def wired(config, engine):
    text = FakeTextChannel(config.announce_channel_id)
    voice = FakeVoiceChannel([FakeMember(1, "Alice"), FakeMember(2, "Bob")])

    class Bot(commands.Bot):
        def __init__(self):
            intents = discord.Intents.default()
            intents.members = True
            super().__init__(command_prefix="!", intents=intents)
            self.config = config
            self.log_stream: Any = None

        def is_ready(self):
            return True

        def is_closed(self):
            return False

        def get_channel(self, cid):
            return voice if cid == voice.id else text

        def get_guild(self, _gid):
            return None

        def get_user(self, _uid):
            return None

        async def fetch_user(self, _uid):
            return None

    bot = Bot()
    announcer = Announcer(bot, config, engine)
    monitor = VoiceMonitor(bot, config, engine, announcer)
    monitor.voice_channel = lambda: voice  # type: ignore[method-assign]
    monitor._ready_at = now_ts() - 1000
    manager = ContinuationManager(bot, config, engine, announcer)
    return manager, bot, text, engine


def _completed(engine, store, votes=None):
    start(engine, T0, 1, 2)
    engine.tick(obs(T0 + TOTAL_SECONDS, 1, 2))
    assert engine.status is EventStatus.COMPLETED
    for uid, answer in (votes or {}).items():
        store.record_continuation_vote(engine.event_uid, uid, answer, T0)
    return engine.event_uid


def test_start_posts_the_poll_and_records_votes(wired):
    manager, bot, text, engine = wired
    uid = _completed(engine, engine.store)

    assert run(manager.start(uid)) is True
    poll = engine.store.get_continuation_poll()
    assert poll["status"] == "open"
    assert poll["message_id"] == text.sent[0].id
    assert "Something has been sent to your DM" in text.sent[0].embeds[0].description or "keep on Hell" in text.sent[0].embeds[0].description

    yes = FakeInteraction(bot, _member(FakeAuthor(uid=1, name="Alice")))
    yes.type = discord.InteractionType.component
    run(manager.vote(yes, "yes"))
    counts = engine.store.continuation_vote_counts(uid)
    assert counts["yes"] == 1 and counts["no"] == 0


def test_close_requires_yes_majority(wired):
    manager, _bot, _text, engine = wired
    uid = _completed(engine, engine.store, votes={1: "yes", 2: "yes", 3: "no"})
    run(manager.start(uid))
    run(manager.close())

    poll = engine.store.get_continuation_poll()
    assert poll["status"] == "closed"
    assert poll["result"] == "yes"
    assert manager.yes_won() is True


def test_close_without_a_majority_blocks_resume(wired):
    manager, _bot, _text, engine = wired
    uid = _completed(engine, engine.store, votes={1: "yes", 2: "no"})
    run(manager.start(uid))
    run(manager.close())

    poll = engine.store.get_continuation_poll()
    assert poll["status"] == "closed"
    assert poll["result"] == "no"
    assert manager.yes_won() is False
