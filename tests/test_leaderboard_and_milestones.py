"""Leaderboard, formatting and milestone table tests."""

from __future__ import annotations

import pytest

from hell.leaderboard import build_leaderboard, render_leaderboard, top_n
from hell.milestones import MILESTONES, TOTAL_SECONDS, current_milestone, milestone_hours, next_milestone
from hell.timeutil import (
    format_hm,
    format_hms,
    milestone_progress_bar,
    progress_bar,
)

HOUR = 3600


# ------------------------------------------------------------- formatting

@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, "0h 00m"),
        (59, "0h 00m"),
        (60, "0h 01m"),
        (128 * HOUR + 42 * 60, "128h 42m"),
        (142 * HOUR + 38 * 60 + 59, "142h 38m"),
        (-10, "0h 00m"),
    ],
)
def test_format_hm(seconds, expected):
    assert format_hm(seconds) == expected


def test_format_hms():
    assert format_hms(3 * HOUR + 4 * 60 + 5) == "3h 04m 05s"


def test_progress_bar():
    assert progress_bar(0) == "░" * 20
    assert progress_bar(1) == "█" * 20
    assert progress_bar(0.5) == "█" * 10 + "░" * 10
    assert progress_bar(0.999).count("░") == 1  # never looks finished early
    assert progress_bar(0.0001).count("█") == 1  # but shows any progress


HOURS = [32, 64, 96, 128, 160]
MILESTONE_SECS = [h * 3600 for h in HOURS]


def test_milestone_progress_bar_starts_empty():
    bar = milestone_progress_bar(0, MILESTONE_SECS)
    assert bar == "▱" * 4 + "┃" + "▱" * 4 + "┃" + "▱" * 4 + "┃" + "▱" * 4 + "┃" + "▱" * 4


def test_milestone_progress_bar_partial_fills_toward_current_milestone():
    # Halfway to the first milestone (16h): first segment should progress but
    # not yet be full; every later segment stays empty.
    bar = milestone_progress_bar(16 * 3600, MILESTONE_SECS)
    segs = bar.split("┃")
    assert segs[0].count("▰") == 2
    assert segs[0] != "▰" * 4
    assert all(s == "▱" * 4 for s in segs[1:])


def test_milestone_progress_bar_keeps_previous_full_and_never_rounds_current_early():
    # 40h: the 32h milestone is reached (full), the 64h segment is filling up
    # but must never display a full block before 64h.
    bar = milestone_progress_bar(40 * 3600, MILESTONE_SECS)
    segs = bar.split("┃")
    assert segs[0] == "▰" * 4
    assert 0 <= segs[1].count("▰") < 4
    assert all(s == "▱" * 4 for s in segs[2:])


def test_milestone_progress_bar_completes_all_segments():
    bar = milestone_progress_bar(160 * 3600, MILESTONE_SECS)
    assert bar.count("▰") == 20
    assert bar.count("▱") == 0


# ------------------------------------------------------------ leaderboard

def rows(*pairs):
    return [(uid, f"User{uid}", secs) for uid, secs in pairs]


def test_sorted_descending_with_ranks():
    entries = build_leaderboard(rows((1, 10), (2, 100), (3, 50)))
    assert [e.user_id for e in entries] == [2, 3, 1]
    assert [e.rank for e in entries] == [1, 2, 3]


def test_exact_ties_share_a_rank():
    entries = build_leaderboard(rows((1, 100), (2, 100), (3, 50)))
    assert [e.rank for e in entries] == [1, 1, 3]


def test_top_n_is_tie_aware():
    entries = build_leaderboard(rows((1, 100), (2, 100), (3, 100), (4, 100), (5, 10)))
    assert len(top_n(entries, 3)) == 4  # four-way tie for first


def test_render_highlights_top3_and_lists_the_rest():
    entries = build_leaderboard(
        rows((1, 128 * HOUR), (2, 117 * HOUR), (3, 104 * HOUR), (4, 83 * HOUR), (5, 61 * HOUR))
    )
    text = render_leaderboard(entries)
    assert "🥇" in text and "<@1>" in text and "128h 00m" in text
    assert "🥈" in text and "🥉" in text
    assert "#4" in text and "<@4>" in text
    # Top 3 are separated from the rest by a blank line
    assert text.index("🥉") < text.index("#4")


def test_render_empty():
    assert "Nobody has spent time in Hell yet" in render_leaderboard([])


def test_render_limit_mentions_hidden_players():
    entries = build_leaderboard(rows(*[(i, 100 - i) for i in range(1, 40)]))
    text = render_leaderboard(entries, limit=10)
    assert "and 29 more participant(s)" in text


# -------------------------------------------------------------- milestones

def test_exactly_five_milestones():
    assert milestone_hours() == (32, 64, 96, 128, 160)
    assert TOTAL_SECONDS == 160 * HOUR


def test_every_milestone_has_a_unique_message_and_reward():
    titles = {m.title for m in MILESTONES}
    blurbs = {m.blurb for m in MILESTONES}
    rewards = {m.reward for m in MILESTONES}
    assert len(titles) == len(blurbs) == len(rewards) == 5


def test_rewards_match_the_specification():
    by_hours = {m.hours: m.reward for m in MILESTONES}
    assert by_hours[32] == "@hell (limited)"
    assert "roblox.com/games/138268356635577/Find-the-Verities" in by_hours[64]
    assert by_hours[96] == "@hell-ist (limited)"
    assert "Music permissions" in by_hours[128]
    assert by_hours[160] == "@hell master (limited)"


def test_current_and_next_milestone():
    assert current_milestone(0) is None
    assert next_milestone(0).hours == 32
    assert current_milestone(32 * HOUR).hours == 32
    assert next_milestone(32 * HOUR).hours == 64
    assert next_milestone(TOTAL_SECONDS) is None
    assert current_milestone(TOTAL_SECONDS).hours == 160
