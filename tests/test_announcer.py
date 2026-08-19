"""Rendering tests for the announcement layer (no gateway needed)."""

from __future__ import annotations

import discord
import pytest

from hell.announcer import Announcer, chunk_lines
from hell.engine import EventCompleted, EventFailed, MilestoneReached
from hell.leaderboard import build_leaderboard, top_participants
from hell.milestones import MILESTONES, get_milestone
from hell.models import EventStatus, ParticipantRef
from tests.conftest import HOUR, T0, obs, start


class FakeBot:
    """Enough of a client for the pure rendering paths."""

    def get_channel(self, _cid):
        return None


@pytest.fixture
def announcer(config, engine):
    return Announcer(FakeBot(), config, engine)


def test_progress_message_contains_every_required_field(announcer, engine):
    start(engine, T0, 1)
    engine.tick(obs(T0 + 73 * HOUR + 24 * 60, *range(1, 8)))
    text = announcer.render_progress(engine.snapshot(now=T0 + 73 * HOUR + 24 * 60))

    assert "WELCOME TO HELL" in text
    assert "RUNNING" in text                      # status
    assert "73h 24m / 160h 00m" in text           # elapsed / total
    assert "45.9%" in text                        # percentage
    assert "█" in text and "░" in text            # progress bar
    assert "Currently in Hell: **7**" in text     # VC population
    assert "Current milestone: **64h** cleared" in text  # current milestone
    assert "Next milestone: **96h**" in text      # next milestone
    assert "(in 22h 36m)" in text                 # time to next milestone
    assert "86h 36m remaining" in text            # time remaining


def test_progress_reflects_failure(announcer, engine):
    start(engine, T0, 1)
    engine.tick(obs(T0 + 10, 1))
    engine.tick(obs(T0 + 11))
    text = announcer.render_progress(engine.snapshot(now=T0 + 60))
    assert "FAILED" in text and "0.0%" in text


def test_every_milestone_message_is_distinct(announcer):
    rendered = [
        announcer.render_milestone(
            MilestoneReached(milestone=m, reached_ts=T0, members=[ParticipantRef(1, "User1")])
        )
        for m in MILESTONES
    ]
    assert len(set(rendered)) == 5
    for text, m in zip(rendered, MILESTONES):
        assert text.startswith("@everyone")
        assert m.title in text
        assert "Only users who are in the VC at this exact moment can claim" in text
        assert "<@1>" in text


def test_milestone_message_uses_role_mentions_when_ids_are_set(tmp_path, engine):
    from tests.conftest import make_config

    cfg = make_config(tmp_path, hell_role_id=555)
    ann = Announcer(FakeBot(), cfg, engine)
    text = ann.render_milestone(
        MilestoneReached(milestone=get_milestone(32), reached_ts=T0, members=[])
    )
    assert "<@&555>" in text
    assert "nobody" in text  # empty snapshot is explicit


def test_completion_message_lists_top3_bonus(announcer, engine):
    board = build_leaderboard([(1, "A", 500.0), (2, "B", 400.0), (3, "C", 300.0), (4, "D", 10.0)])
    text = announcer.render_completion(
        EventCompleted(completed_ts=T0, leaderboard=board, top3=top_participants(board, 3))
    )
    assert "COMPLETED" in text
    assert "@cool people :D" in text
    assert "every milestone reward" in text
    assert "🥇 <@1>" in text and "**4.** <@4>" in text


def test_failure_message_mentions_reason_and_leaderboard(announcer, engine):
    board = build_leaderboard([(1, "A", 3600.0)])
    text = announcer.render_failure(
        EventFailed(failed_ts=T0, elapsed=40 * HOUR, leaderboard=board)
    )
    assert "CHALLENGE FAILED" in text
    assert "completely empty" in text
    assert "**32h**" in text          # milestones secured
    assert "🥇 <@1> — **1h 00m**" in text


def test_chunk_lines_respects_discord_limit():
    lines = ["x" * 100 for _ in range(60)]
    chunks = chunk_lines(lines)
    assert len(chunks) > 1
    assert all(len(c) <= 1900 for c in chunks)
