"""End-to-end pass through the Discord-facing code with fake Discord objects.

Unit tests cover the engine and the renderers; this file exercises the wiring
in between — monitor → engine → announcer → channel — which is where signature
mistakes and unformatted templates would otherwise hide until production.
"""

from __future__ import annotations

import asyncio
import re

import discord
import pytest

from hell.announcer import Announcer
from hell.engine import EventCompleted, EventFailed, GraceRecovered, GraceStarted, MilestoneReached
from hell.milestones import MILESTONES, get_milestone
from hell.models import EventStatus
from hell.monitor import VoiceMonitor
from hell.timeutil import now_ts
from tests.conftest import GRACE, HOUR, T0, obs, start, users
from tests.test_monitor import FakeMember, FakeVoiceChannel

# `{placeholder}` left unrendered in a message = a typo in Announcements.py.
UNRENDERED = re.compile(r"\{[a-z_]+\}")


class FakeMessage:
    def __init__(self, channel, content, embeds):
        self.id = 5000
        self.channel = channel
        self.content = content
        self.embeds = embeds
        self.edits: list[dict] = []
        self.pinned = False

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        if "embed" in kwargs:
            self.embeds = [kwargs["embed"]]

    async def pin(self, reason=None):
        self.pinned = True


class FakeTextChannel(discord.abc.Messageable):
    """Subclasses Messageable so the announcer's isinstance check passes."""

    async def _get_channel(self):
        return self

    def __init__(self, cid=2):
        self.id = cid
        self.sent: list[FakeMessage] = []

    async def send(self, content=None, *, embeds=None, embed=None, allowed_mentions=None, **kw):
        payload = list(embeds or ([embed] if embed else []))
        message = FakeMessage(self, content, payload)
        message.allowed_mentions = allowed_mentions
        self.sent.append(message)
        return message

    async def fetch_message(self, mid):
        for message in self.sent:
            if message.id == mid:
                return message
        raise discord.NotFound(_Resp(404), "gone")


class _Resp:
    def __init__(self, status=404):
        self.status = status
        self.reason = "err"


class FakeBot:
    def __init__(self, text_channel, voice_channel):
        self.text = text_channel
        self.voice = voice_channel

    def is_ready(self):
        return True

    def is_closed(self):
        return False

    def get_channel(self, cid):
        if cid == self.voice.id:
            return self.voice
        return self.text

    def get_guild(self, _gid):
        return None

    def get_user(self, uid):
        return FakeUser()


def run(coro):
    return asyncio.run(coro)


def all_text(channel: FakeTextChannel) -> str:
    """Everything posted, flattened — content plus every embed field."""
    from hell.announcer import embed_to_text

    parts: list[str] = []
    for message in channel.sent:
        parts.append(message.content or "")
        parts.extend(embed_to_text(e) for e in message.embeds)
        for edit in message.edits:
            if "embed" in edit:
                parts.append(embed_to_text(edit["embed"]))
    return "\n".join(parts)


@pytest.fixture
def wired(config, engine):
    text = FakeTextChannel(config.announce_channel_id)
    voice = FakeVoiceChannel([FakeMember(1, "Alice"), FakeMember(2, "Bob")])
    bot = FakeBot(text, voice)
    announcer = Announcer(bot, config, engine)
    monitor = VoiceMonitor(bot, config, engine, announcer)
    return monitor, announcer, text, voice


# --------------------------------------------------------------- happy path

def test_full_lifecycle_posts_every_message(wired, engine, monkeypatch):
    monitor, announcer, text, voice = wired
    monkeypatch.setattr(monitor, "voice_channel", lambda: voice)

    # start
    start(engine, T0, 1, 2)
    snap = engine.snapshot(now=T0, participants=2)
    run(announcer.announce_start(snap, FakeUser(), users(1, 2)))
    run(announcer.update_progress(snap, force=True))

    # a milestone, a grace scare, a recovery, then completion
    for event in engine.tick(obs(T0 + 32 * HOUR, 1, 2)):
        run(monitor.dispatch(event))
    for event in engine.tick(obs(T0 + 32 * HOUR + 60)):          # VC empties
        run(monitor.dispatch(event))
    for event in engine.tick(obs(T0 + 32 * HOUR + 65, 1)):       # saved
        run(monitor.dispatch(event))
    for event in engine.tick(obs(T0 + 160 * HOUR, 1, 2)):        # 160h
        run(monitor.dispatch(event))

    body = all_text(text)
    assert "WELCOME TO HELL HAS STARTED" in body
    assert "32 HOURS SURVIVED" in body
    assert "THE VC IS EMPTY" in body
    assert "SAVED — THE RUN CONTINUES" in body
    assert "WELCOME TO HELL HAS BEEN COMPLETED" in body
    assert engine.status is EventStatus.COMPLETED


def test_no_message_contains_an_unrendered_placeholder(wired, engine, monkeypatch):
    """A typo like {reward_} in Announcements.py would show up here."""
    monitor, announcer, text, voice = wired
    monkeypatch.setattr(monitor, "voice_channel", lambda: voice)

    start(engine, T0, 1, 2)
    snap = engine.snapshot(now=T0, participants=2)
    run(announcer.announce_start(snap, FakeUser(), users(1, 2)))
    run(announcer.update_progress(snap, force=True))
    for hours in (32, 64, 96, 128):
        run(
            announcer.announce_milestone(
                MilestoneReached(
                    milestone=get_milestone(hours), reached_ts=T0, members=list(users(1, 2))
                )
            )
        )
    board = engine.leaderboard()
    run(announcer.announce_failure(EventFailed(failed_ts=T0, elapsed=40 * HOUR, leaderboard=board)))
    run(
        announcer.announce_completion(
            EventCompleted(completed_ts=T0, leaderboard=board, top3=list(users(1)))
        )
    )
    run(
        announcer.announce_grace_warning(
            GraceStarted(started_ts=T0, deadline_ts=T0 + GRACE, seconds=GRACE, elapsed=3600)
        )
    )
    run(
        announcer.announce_grace_recovered(
            GraceRecovered(
                started_ts=T0, recovered_ts=T0 + 5, empty_for=5.0, participants=list(users(1))
            )
        )
    )

    leftovers = UNRENDERED.findall(all_text(text))
    assert not leftovers, f"unrendered placeholders in a message: {sorted(set(leftovers))}"


def test_every_milestone_message_renders_and_is_unique(config, engine):
    announcer = Announcer(FakeBot(FakeTextChannel(), FakeVoiceChannel([])), config, engine)
    rendered = [
        announcer.render_milestone(
            MilestoneReached(milestone=m, reached_ts=T0, members=list(users(1)))
        )
        for m in MILESTONES
    ]
    assert len(set(rendered)) == len(MILESTONES)
    for text in rendered:
        assert not UNRENDERED.findall(text)


# ------------------------------------------------------------ message rules

def test_only_the_right_messages_ping_everyone(wired, engine):
    _monitor, announcer, text, _voice = wired
    start(engine, T0, 1)
    run(announcer.announce_start(engine.snapshot(now=T0, participants=1), FakeUser(), users(1)))
    run(
        announcer.announce_grace_warning(
            GraceStarted(started_ts=T0, deadline_ts=T0 + GRACE, seconds=GRACE, elapsed=60)
        )
    )
    start_msg, grace_msg = text.sent[0], text.sent[1]
    assert start_msg.content == "@everyone"
    assert start_msg.allowed_mentions.everyone is True
    # The empty-VC warning must never ping anybody.
    assert grace_msg.content is None
    assert grace_msg.allowed_mentions.everyone is False
    assert grace_msg.allowed_mentions.users is False
    assert grace_msg.allowed_mentions.roles is False


def test_progress_message_is_edited_not_reposted(wired, engine):
    _monitor, announcer, text, _voice = wired
    start(engine, T0, 1)
    run(announcer.update_progress(engine.snapshot(now=T0 + 60, participants=1), force=True))
    for minute in range(2, 8):
        run(announcer.update_progress(engine.snapshot(now=T0 + minute * 60, participants=1)))
    assert len(text.sent) == 1                    # one message…
    assert len(text.sent[0].edits) >= 5           # …edited repeatedly


def test_identical_progress_snapshots_skip_the_api(wired, engine):
    _monitor, announcer, text, _voice = wired
    start(engine, T0, 1)
    snap = engine.snapshot(now=T0 + 60, participants=1)
    run(announcer.update_progress(snap, force=True))
    run(announcer.update_progress(snap))
    run(announcer.update_progress(snap))
    assert text.sent[0].edits == []               # nothing changed -> no edit


def test_deleted_progress_message_is_recreated(wired, engine):
    _monitor, announcer, text, _voice = wired
    start(engine, T0, 1)
    run(announcer.update_progress(engine.snapshot(now=T0 + 60, participants=1), force=True))
    text.sent.clear()                              # somebody deleted it
    announcer.forget_progress_message()
    run(announcer.update_progress(engine.snapshot(now=T0 + 120, participants=1), force=True))
    assert len(text.sent) == 1


# ------------------------------------------------------------- monitor tick

def test_tick_kicks_clankers_and_feeds_the_engine(wired, engine, config, monkeypatch):
    monitor, _announcer, _text, voice = wired
    clanker = FakeMember(9, "Clank3r", roles=[config.clanker_role_id])
    voice.members.append(clanker)
    voice.members.append(FakeMember(8, "SomeBot", bot=True))
    monkeypatch.setattr(monitor, "voice_channel", lambda: voice)
    now = now_ts()
    monitor._ready_at = now - 1000                 # past the startup grace
    start(engine, now, 1, 2)

    run(monitor._tick_once())
    assert clanker.moved_to == [None]
    assert {p.user_id for p in engine.last_participants} == {1, 2}


def test_tick_during_startup_grace_cannot_fail_the_event(wired, engine, monkeypatch):
    monitor, _announcer, _text, voice = wired
    voice.members = []                             # empty VC, cold cache
    monkeypatch.setattr(monitor, "voice_channel", lambda: voice)
    start(engine, now_ts(), 1)
    run(monitor._tick_once())                      # inside the startup grace
    assert engine.status is EventStatus.RUNNING


def test_alive_check_runs_off_the_monitor_thread(wired, engine, monkeypatch):
    """A slow roll call must not stall the 1s VC loop."""
    monitor, _announcer, _text, voice = wired
    monkeypatch.setattr(monitor, "voice_channel", lambda: voice)
    now = now_ts()
    monitor._ready_at = now - 1000
    start(engine, now, 1, 2)

    slow = asyncio.Event()

    async def slow_tick(*_args, **_kwargs):
        await slow.wait()

    monkeypatch.setattr(monitor.alive_checks, "tick", slow_tick)

    async def scenario():
        await monitor._tick_once()                 # schedules the roll call
        assert monitor._alive_task is not None and not monitor._alive_task.done()
        await monitor._tick_once()                 # the VC loop keeps ticking
        slow.set()
        await asyncio.sleep(0)
        monitor._alive_task.cancel()

    run(scenario())
    assert engine.status is EventStatus.RUNNING


class FakeUser:
    id = 99
    mention = "<@99>"

    def __init__(self):
        self.dms: list[dict] = []

    async def send(self, **kwargs):
        self.dms.append(kwargs)


# ------------------------------------------- difficulty escalation announcements


def test_difficulty_escalation_is_announced_with_the_milestone(wired, engine, monkeypatch):
    """The 32h milestone unlocks Difficulty 1 — the guild must be told in the
    announcement channel, right after the milestone itself."""
    monitor, _announcer, text, voice = wired
    monkeypatch.setattr(monitor, "voice_channel", lambda: voice)

    start(engine, T0, 1, 2)
    for event in engine.tick(obs(T0 + 32 * HOUR, 1, 2)):
        run(monitor.dispatch(event))

    body = all_text(text)
    assert "32 HOURS SURVIVED" in body
    assert "DIFFICULTY UPDATE" in body
    assert "Heating Up" in body  # the tier name made it into the post


def test_every_unlocking_milestone_announces_its_tier(wired, engine, monkeypatch):
    monitor, _announcer, text, voice = wired
    monkeypatch.setattr(monitor, "voice_channel", lambda: voice)

    start(engine, T0, 1, 2)
    for hours, tier in ((32, "Heating Up"), (64, "Inferno"), (96, "Torment"), (128, "Cataclysm")):
        for event in engine.tick(obs(T0 + hours * HOUR + 1, 1, 2)):
            run(monitor.dispatch(event))
        assert tier in all_text(text), f"the {hours}h escalation to {tier} was never posted"


def test_milestone_without_tier_change_posts_no_escalation(wired, engine, monkeypatch):
    """160h completes the run but changes no tier -> no escalation post."""
    monitor, _announcer, text, voice = wired
    monkeypatch.setattr(monitor, "voice_channel", lambda: voice)

    start(engine, T0, 1, 2)
    events = engine.tick(obs(T0 + 160 * HOUR, 1, 2))
    milestone_160 = next(
        e for e in events if isinstance(e, MilestoneReached) and e.milestone.hours == 160
    )
    run(monitor.dispatch(milestone_160))
    assert "DIFFICULTY UPDATE" not in all_text(text)


def test_late_milestone_reannouncement_skips_the_escalation(wired, engine, monkeypatch):
    """Crash recovery re-posts claimed milestones — the escalation already
    went out before the crash and must not be duplicated."""
    monitor, _announcer, text, voice = wired
    monkeypatch.setattr(monitor, "voice_channel", lambda: voice)

    start(engine, T0, 1, 2)
    run(
        monitor.dispatch(
            MilestoneReached(
                milestone=get_milestone(32), reached_ts=T0, members=list(users(1, 2)), late=True
            )
        )
    )
    body = all_text(text)
    assert "32 HOURS SURVIVED" in body       # the milestone itself is re-posted
    assert "DIFFICULTY UPDATE" not in body   # the escalation is not
