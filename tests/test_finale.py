"""Tests for the 160-Hour Finale subsystem."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from hell.announcer import Announcer
from hell.engine import EventCompleted, EventFailed, EventStatus
from hell.finale import (
    COUNTDOWN_SECONDS,
    FINAL_HOUR_SECONDS,
    STAGE_5M_SECONDS,
    STAGE_10M_SECONDS,
    STAGE_30M_SECONDS,
    TOTAL_SECONDS,
    FinaleManager,
    FinaleStage,
)
from hell.models import ParticipantRef
from hell.timeutil import now_ts
from tests.conftest import HOUR, obs, start


def test_finale_stages_and_thresholds(engine, config, store):
    bot = MagicMock()
    announcer = Announcer(bot, config, engine)
    announcer.send = AsyncMock(return_value=MagicMock())
    mgr = FinaleManager(config, store, engine=engine, announcer=announcer)

    now = now_ts()
    start(engine, now, 100)
    mgr.bind(engine.event_uid)

    participants = [ParticipantRef(100, "Alice")]

    # 100h: not final hour
    assert not mgr.is_final_hour(100 * HOUR)
    assert not mgr.is_countdown(100 * HOUR)

    # 159h: FINAL_HOUR triggers
    ann159 = asyncio.run(mgr.tick(now, FINAL_HOUR_SECONDS, participants))
    assert len(ann159) == 1
    assert ann159[0].stage is FinaleStage.FINAL_HOUR
    assert mgr.is_final_hour(FINAL_HOUR_SECONDS)
    assert not mgr.is_countdown(FINAL_HOUR_SECONDS)

    # Calling tick again at 159h + 10s does NOT duplicate announcement
    ann_dup = asyncio.run(mgr.tick(now, FINAL_HOUR_SECONDS + 10.0, participants))
    assert len(ann_dup) == 0

    # 159h30m: 30 MINUTES REMAIN
    ann30m = asyncio.run(mgr.tick(now, STAGE_30M_SECONDS, participants))
    assert len(ann30m) == 1
    assert ann30m[0].stage is FinaleStage.REMAIN_30M

    # 159h50m: 10 MINUTES REMAIN
    ann10m = asyncio.run(mgr.tick(now, STAGE_10M_SECONDS, participants))
    assert len(ann10m) == 1
    assert ann10m[0].stage is FinaleStage.REMAIN_10M

    # 159h55m: 5 MINUTES REMAIN
    ann5m = asyncio.run(mgr.tick(now, STAGE_5M_SECONDS, participants))
    assert len(ann5m) == 1
    assert ann5m[0].stage is FinaleStage.REMAIN_5M

    # 159h59m: COUNTDOWN (final minute)
    assert mgr.is_countdown(COUNTDOWN_SECONDS)
    assert mgr.countdown_seconds_left(COUNTDOWN_SECONDS) == 60
    assert mgr.countdown_seconds_left(COUNTDOWN_SECONDS + 30.0) == 30
    assert mgr.countdown_seconds_left(COUNTDOWN_SECONDS + 59.0) == 1


def test_final_countdown_progress_embed(engine, config):
    bot = MagicMock()
    announcer = Announcer(bot, config, engine)

    now = now_ts()
    # 59 seconds left
    start(engine, now - (COUNTDOWN_SECONDS + 1.0), 100)

    snap = engine.snapshot(now)
    assert snap.is_final_hour
    assert snap.countdown_seconds == 59

    embed = announcer.embeds.progress(snap)
    assert "FINAL COUNTDOWN" in embed.title
    assert "59" in embed.description
    assert "DO NOT LET HELL GO EMPTY" in embed.description


def test_atomic_completion_at_160h(engine, config, store):
    bot = MagicMock()
    announcer = Announcer(bot, config, engine)
    announcer.send = AsyncMock(return_value=MagicMock())

    now = now_ts()
    start(engine, now - TOTAL_SECONDS, 100, 200, 300)

    # Feed observation at 160h completion
    events = engine.tick(obs(now, 100, 200, 300))

    comp_event = next((e for e in events if isinstance(e, EventCompleted)), None)
    assert comp_event is not None
    assert engine.status is EventStatus.COMPLETED
    assert engine.elapsed(now) == pytest.approx(TOTAL_SECONDS)
    assert comp_event.peak_population == 3

    # Render completion embed
    embeds = announcer.embeds.completion(comp_event)
    head = embeds[0]
    assert "COMPLETED" in head.title
    assert "160:00:00 SURVIVED" in head.description
    assert "HELL HAS BEEN CONQUERED" in head.description
    assert any("Peak Hell Population" in f.name for f in head.fields)


def test_failure_during_final_hour(engine, config):
    now = now_ts()
    # At 159h30m
    start(engine, now - STAGE_30M_SECONDS, 100)
    engine.tick(obs(now, 100))

    # VC empties
    engine.tick(obs(now + 1.0))
    assert engine.grace.is_open

    # Grace period (15s) expires
    ev2 = engine.tick(obs(now + 20.0))
    fail_event = next((e for e in ev2 if isinstance(e, EventFailed)), None)
    assert fail_event is not None
    assert engine.status is EventStatus.FAILED
    assert engine.status.is_terminal


def test_finale_restart_recovery_preserves_stages(engine, config, store):
    now = now_ts()
    start(engine, now - STAGE_30M_SECONDS, 100)

    mgr = FinaleManager(config, store, engine=engine)
    mgr.bind(engine.event_uid)

    # Trigger stages up to 30m
    asyncio.run(mgr.tick(now, STAGE_30M_SECONDS, [ParticipantRef(100, "Alice")]))
    assert FinaleStage.FINAL_HOUR.value in mgr.completed_stages()
    assert FinaleStage.REMAIN_30M.value in mgr.completed_stages()

    # Simulate restart
    mgr_recovered = FinaleManager(config, store, engine=engine)
    mgr_recovered.bind(engine.event_uid)

    # Tick at 159h35m: must not re-trigger final_hour or remain_30m
    ann = asyncio.run(mgr_recovered.tick(now, STAGE_30M_SECONDS + 300.0, [ParticipantRef(100, "Alice")]))
    assert len(ann) == 0
