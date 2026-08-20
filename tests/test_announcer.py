"""Rendering tests for the announcement layer (no gateway needed)."""

from __future__ import annotations

import pytest

from hell.announcer import Announcer, chunk_lines
from hell.engine import EventCompleted, EventFailed, MilestoneReached
from hell.leaderboard import build_leaderboard, top_participants
from hell.milestones import MILESTONES, get_milestone
from hell.models import ParticipantRef
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
    assert "Currently in Hell" in text and "**7**" in text   # VC population
    assert "Current milestone" in text and "**64h** cleared" in text
    assert "Next milestone" in text and "**96h**" in text
    assert "in 22h 36m" in text                   # time to next milestone
    assert "Time remaining" in text and "**86h 36m**" in text


def test_progress_reflects_failure(announcer, engine):
    from tests.conftest import empty_out

    start(engine, T0, 1)
    engine.tick(obs(T0 + 10, 1))
    empty_out(engine, T0 + 11)
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
        assert "the ones in the VC at this exact moment" in text
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


def test_milestone_with_a_huge_vc_stays_within_discord_limits(announcer):
    """Regression: 90+ members used to produce a single 2000+ char line (HTTP 400)."""
    members = [ParticipantRef(100000000000000000 + i, f"User{i}") for i in range(250)]
    embed = announcer.build_milestone(
        MilestoneReached(milestone=get_milestone(64), reached_ts=T0, members=members)
    )
    assert len(embed) <= 6000
    assert len(embed.description or "") <= 4096
    assert len(embed.fields) <= 25
    for field in embed.fields:
        assert len(field.value) <= 1024, f"field '{field.name}' is too long"
        assert len(field.name) <= 256


def test_start_and_completion_survive_a_packed_vc(announcer, engine):
    from hell.leaderboard import build_leaderboard, top_participants

    members = [ParticipantRef(200000000000000000 + i, f"Person{i}") for i in range(300)]
    start(engine, T0, 1)
    start_embed = announcer.build_start(engine.snapshot(now=T0), "<@1>", members)
    assert len(start_embed) <= 6000
    assert all(len(f.value) <= 1024 for f in start_embed.fields)

    board = build_leaderboard([(m.user_id, m.display_name, 3600.0 + i) for i, m in enumerate(members)])
    embeds = announcer.build_completion(
        EventCompleted(completed_ts=T0, leaderboard=board, top3=top_participants(board, 3))
    )
    assert len(embeds) <= 10
    for embed in embeds:
        assert len(embed) <= 6000
        assert len(embed.description or "") <= 4096
        assert all(len(f.value) <= 1024 for f in embed.fields)


def test_split_text_hard_splits_a_single_monster_line():
    from hell.announcer import split_text

    line = ", ".join(f"<@{100000000000000000 + i}>" for i in range(400))
    chunks = split_text(line, 1000)
    assert all(len(c) <= 1000 for c in chunks)
    assert "".join(chunks).count("<@") == 400  # nothing lost

    solid = "x" * 5000
    assert all(len(c) <= 1000 for c in split_text(solid, 1000))


def test_progress_message_is_stable_between_identical_ticks(announcer, engine):
    start(engine, T0, 1)
    snap = engine.snapshot(now=T0 + 60, participants=3)
    assert announcer.render_progress(snap) == announcer.render_progress(snap)


def test_chunk_lines_respects_discord_limit():
    lines = ["x" * 100 for _ in range(60)]
    chunks = chunk_lines(lines)
    assert len(chunks) > 1
    assert all(len(c) <= 1900 for c in chunks)


def test_send_refuses_an_empty_payload(announcer):
    """Discord rejects a message with neither content nor embeds."""
    import asyncio

    assert asyncio.run(announcer.send([])) is None


def test_a_broken_colour_falls_back_instead_of_crashing(announcer, monkeypatch):
    from hell import announcer as announcer_module

    monkeypatch.setattr(announcer_module.TEXT, "COLOR_RUNNING", "bright orange", raising=False)
    assert announcer_module.theme_color("RUNNING") == 0xE25822


def test_leaderboard_embeds_take_a_plain_color_kwarg(announcer):
    from hell.leaderboard import build_leaderboard

    embeds = announcer.build_leaderboard_embeds(
        build_leaderboard([(1, "A", 60.0)]), title="X", color=0x123456
    )
    assert embeds[0].colour.value == 0x123456
