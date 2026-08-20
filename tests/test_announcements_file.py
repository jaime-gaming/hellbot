"""Announcements.py is the single source of every message.

These tests are the contract that keeps it that way: edit the file, and the
bot's output changes — break the file, and the bot keeps running on the last
good text.
"""

from __future__ import annotations

import pytest

from hell import milestones, texts
from hell.announcer import Announcer
from hell.engine import EventCompleted, MilestoneReached
from hell.leaderboard import build_leaderboard, format_entry
from hell.models import EventStatus, LeaderboardEntry, ParticipantRef
from hell.reports import build_reports, render_report
from hell.texts import TEXT, say
from tests.conftest import T0, start


class FakeBot:
    def get_channel(self, _cid):
        return None


@pytest.fixture
def announcer(config, engine):
    return Announcer(FakeBot(), config, engine)


@pytest.fixture(autouse=True)
def restore_texts():
    """Every test gets the shipped file back, whatever it did to the loader."""
    yield
    texts.load(force=True)
    milestones.refresh()


def write_override(tmp_path, body: str):
    """Write a replacement Announcements.py and load it."""
    path = tmp_path / "Announcements.py"
    path.write_text(body, encoding="utf-8")
    module = texts._load_from_file(path)
    texts._module = module
    milestones.refresh()
    return path


# ------------------------------------------------------------------ loading

def test_the_shipped_file_loads():
    module = texts.load(force=True)
    assert module.MILESTONES
    assert texts.source().endswith("Announcements.py")
    assert texts.last_error() is None


def test_every_milestone_comes_from_the_file():
    from_file = {int(m["hours"]): m for m in TEXT.MILESTONES}
    for milestone in milestones.MILESTONES:
        entry = from_file[milestone.hours]
        assert milestone.title == entry["title"]
        assert milestone.reward == entry["reward"]
        assert milestone.blurb == entry["blurb"]


def test_missing_message_names_explain_themselves():
    with pytest.raises(AttributeError) as excinfo:
        _ = TEXT.THIS_MESSAGE_DOES_NOT_EXIST
    assert "Announcements.py" in str(excinfo.value)


# ------------------------------------------------ the text really is the text

def test_editing_a_milestone_changes_the_announcement(tmp_path, announcer):
    write_override(
        tmp_path,
        'MILESTONES = ({"hours": 32, "title": "CUSTOM 32H TITLE", "blurb": "Custom blurb.",'
        ' "flavour": "", "reward": "@custom", "short_reward": "@custom"},)\n'
        'TOP3_BONUS_ROLE = "@cool people :D"\n'
        'MILESTONE_REWARD_FIELD = "Reward"\n'
        'MILESTONE_CLAIM_FIELD = "Claim"\n'
        'MILESTONE_CLAIM_TEXT = "Be in the VC."\n'
        'MILESTONE_ELIGIBLE_FIELD = "Eligible ({member_count})"\n'
        'MILESTONE_REACHED_FIELD = "Reached"\n'
        'MILESTONE_REACHED_TEXT = "{reached_at}"\n'
        'MILESTONE_FOOTER = "{hours}h"\n'
        'MILESTONE_FOOTER_FINAL = "final"\n'
        'MILESTONE_NOBODY = "nobody"\n'
        'COLOR_MILESTONE = 0x111111\n'
        'COLOR_COMPLETED = 0x222222\n',
    )
    event = MilestoneReached(
        milestone=milestones.get_milestone(32),
        reached_ts=T0,
        members=[ParticipantRef(1, "User1")],
    )
    text = announcer.render_milestone(event)
    assert "CUSTOM 32H TITLE" in text
    assert "@custom" in text
    assert "Be in the VC." in text


def test_editing_the_progress_message(tmp_path, announcer, engine):
    write_override(
        tmp_path,
        "".join(
            line + "\n"
            for line in [
                'MILESTONES = ({"hours": 32, "title": "t", "blurb": "b", "flavour": "",'
                ' "reward": "r", "short_reward": "r"},)',
                'PROGRESS_TITLE = "MY EVENT {emoji}"',
                'PROGRESS_DESCRIPTION = "{elapsed} of {total} ({percent})"',
                'PROGRESS_STATUS_FIELD = "State"',
                'PROGRESS_STATUS_VALUE = "{status}"',
                'PROGRESS_STATUS_VALUE_EMPTY_VC = "{status} EMPTY"',
                'PROGRESS_PEOPLE_FIELD = "People"',
                'PROGRESS_PEOPLE_VALUE = "{participants}"',
                'PROGRESS_REMAINING_FIELD = "Left"',
                'PROGRESS_REMAINING_VALUE = "{remaining}"',
                'PROGRESS_CURRENT_FIELD = "Now"',
                'PROGRESS_CURRENT_VALUE = "{current_milestone}"',
                'PROGRESS_CURRENT_NONE = "none"',
                'PROGRESS_NEXT_FIELD = "Next"',
                'PROGRESS_NEXT_VALUE = "{next_milestone}"',
                'PROGRESS_NEXT_VALUE_RUNNING = "{next_milestone}"',
                'PROGRESS_NEXT_NONE = "done"',
                'PROGRESS_STARTED_FIELD = "Started"',
                'PROGRESS_STARTED_VALUE = "{started_at}"',
                'PROGRESS_FOOTER_LIVE = "live"',
                'PROGRESS_FOOTER_FINAL = "final"',
                'STATUS_EMOJI = {"RUNNING": "🚀"}',
                "COLOR_RUNNING = 0x123456",
            ]
        ),
    )
    start(engine, T0, 1)
    text = announcer.render_progress(engine.snapshot(now=T0 + 3600, participants=4))
    assert "MY EVENT 🚀" in text
    assert "1h 00m of 160h 00m" in text
    assert "live" in text


def test_editing_the_leaderboard_format(tmp_path):
    write_override(
        tmp_path,
        'MILESTONES = ({"hours": 160, "title": "t", "blurb": "b", "flavour": "", "reward": "r", "short_reward": "r"},)\n'
        'LEADERBOARD_ENTRY = "{medal} {who} :: {time}"\n'
        'LEADERBOARD_MEDALS = {1: "[1st]", 2: "[2nd]", 3: "[3rd]"}\n'
        'LEADERBOARD_RANK = "[{rank}]"\n',
    )
    entries = build_leaderboard([(1, "A", 3600.0), (2, "B", 60.0), (3, "C", 30.0), (4, "D", 1.0)])
    assert format_entry(entries[0]) == "[1st] <@1> :: 1h 00m"
    assert format_entry(entries[3]) == "[4] <@4> :: 0h 00m"


def test_editing_the_stat_card(tmp_path):
    write_override(
        tmp_path,
        'MILESTONES = ({"hours": 160, "title": "t", "blurb": "b", "flavour": "", "reward": "r", "short_reward": "r"},)\n'
        'TOP3_BONUS_ROLE = "@bonus"\n'
        'CARD_BODY = "SURVIVED {survived} / RANK {rank} / {reward_count}"\n'
        'CARD_REWARD_LINE = "- {reward}"\n'
        'CARD_REWARD_TOP3_NOTE = "top3"\n'
        'CARD_REWARD_NOTE = "note"\n'
        'CARD_NO_REWARDS = "nothing"\n'
        'CARD_MILESTONE_LINE = "{hours}h {reward}"\n'
        'CARD_TOP3_BONUS_LINE = "bonus {bonus_role}"\n'
        'CARD_OUTCOME = {"FAILED": "it failed"}\n'
        'CARD_OUTCOME_DEFAULT = "over"\n'
        'CARD_EVENT_CLOCK = "clock {event_clock}"\n',
    )
    entries = [LeaderboardEntry(rank=1, user_id=1, display_name="A", seconds=3661.0)]
    report = build_reports(entries, [], status=EventStatus.FAILED, event_elapsed=7200.0)[0]
    text = render_report(report)
    assert text.startswith("SURVIVED 1:01:01 / RANK 1 / 0")
    assert "nothing" in text and "it failed" in text and "clock 2:00:00" in text


def test_editing_the_alive_check_headline(tmp_path, config, store):
    import asyncio
    import random

    from hell.alivecheck import AliveCheckManager
    from tests.test_alivecheck import FakeIO

    write_override(
        tmp_path,
        'MILESTONES = ({"hours": 160, "title": "t", "blurb": "b", "flavour": "", "reward": "r", "short_reward": "r"},)\n'
        'ALIVE_CHECK_TEXT = "WAKE UP! Type Yes"\n'
        'ALIVE_CHECK_INSTRUCTIONS = "you have {minutes} minutes"\n',
    )
    io = FakeIO()
    manager = AliveCheckManager(config, store, io, rng=random.Random(1))
    manager.bind("uid", now=T0)
    asyncio.run(manager.start(T0, [ParticipantRef(1, "A")]))
    body, _pinged = io.sent[0]
    assert body.startswith("WAKE UP! Type Yes")
    assert "you have 5 minutes" in body


def test_editing_the_completion_message(tmp_path, announcer):
    write_override(
        tmp_path,
        'MILESTONES = ({"hours": 160, "title": "t", "blurb": "b", "flavour": "",'
        ' "reward": "@master", "short_reward": "@master"},)\n'
        'TOP3_BONUS_ROLE = "@vip"\n'
        'COMPLETION_TITLE = "WE DID IT"\n'
        'COMPLETION_DESCRIPTION = "{completed_at}"\n'
        'COMPLETION_REWARD_FIELD = "Reward"\n'
        'COMPLETION_REWARD_TEXT = "{final_reward}"\n'
        'COMPLETION_TOP3_FIELD = "Top3"\n'
        'COMPLETION_TOP3_TEXT = "{all_rewards} + {bonus_role}"\n'
        'COMPLETION_PODIUM_FIELD = "Podium"\n'
        'COMPLETION_PODIUM_NONE = "none"\n'
        'COMPLETION_FOOTER = "f"\n'
        'COMPLETION_LEADERBOARD_TITLE = "Final"\n'
        'LEADERBOARD_ENTRY = "{medal} {who} — **{time}**"\n'
        'LEADERBOARD_MEDALS = {1: "1)", 2: "2)", 3: "3)"}\n'
        'LEADERBOARD_RANK = "{rank})"\n'
        'LEADERBOARD_FOOTER = "{total}"\n'
        'LEADERBOARD_EMPTY = "empty"\n'
        'LEADERBOARD_NO_PODIUM = "no podium"\n'
        'LEADERBOARD_REST_TITLE = "rest"\n'
        'LEADERBOARD_REST_TITLE_CONT = "rest cont"\n'
        'LEADERBOARD_MORE = "{hidden} more"\n'
        'COLOR_COMPLETED = 0x333333\n',
    )
    board = build_leaderboard([(1, "A", 100.0)])
    text = announcer.render_completion(
        EventCompleted(completed_ts=T0, leaderboard=board, top3=[])
    )
    assert "WE DID IT" in text
    assert "@master" in text and "@vip" in text


# ------------------------------------------------------------- hot reloading

def test_reload_picks_up_changes(tmp_path, monkeypatch, announcer, engine):
    original = texts._candidates()
    override = tmp_path / "Announcements.py"
    override.write_text(
        'MILESTONES = ({"hours": 32, "title": "RELOADED TITLE", "blurb": "b", "flavour": "",'
        ' "reward": "@r", "short_reward": "@r"},)\n'
        'TOP3_BONUS_ROLE = "@x"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(texts, "_candidates", lambda: [override, *original])
    ok, detail = texts.reload()
    assert ok and str(override) == detail
    assert milestones.get_milestone(32).title == "RELOADED TITLE"


def test_a_broken_file_keeps_the_previous_text(tmp_path, monkeypatch):
    before = milestones.get_milestone(32).title
    broken = tmp_path / "Announcements.py"
    broken.write_text("MILESTONES = (this is not python\n", encoding="utf-8")
    monkeypatch.setattr(texts, "_candidates", lambda: [broken])
    ok, detail = texts.reload()
    assert not ok
    assert "SyntaxError" in detail
    assert milestones.get_milestone(32).title == before   # nothing broke
    assert TEXT.PROGRESS_TITLE                              # still serving text


def test_bad_placeholder_does_not_raise():
    """A typo'd {name} degrades to the raw template instead of crashing."""
    assert say("hello {nope}", other=1) == "hello {nope}"
    assert say("hello {name}", name="world") == "hello world"


def test_message_count_and_source_are_reportable():
    assert texts.message_count() > 50
    assert "Announcements.py" in texts.source()


# --------------------------------------------------- milestone table safety

def test_empty_milestone_table_is_rejected(tmp_path, monkeypatch):
    broken = tmp_path / "Announcements.py"
    broken.write_text("MILESTONES = ()\n", encoding="utf-8")
    monkeypatch.setattr(texts, "_candidates", lambda: [broken])
    ok, detail = texts.reload()
    assert not ok and "empty" in detail
    assert milestones.MILESTONES                      # previous table intact


@pytest.mark.parametrize(
    "table,expected",
    [
        ('({"hours": "soon", "title": "t", "blurb": "b", "reward": "r"},)', "non-numeric"),
        ('({"title": "t", "blurb": "b", "reward": "r"},)', "no 'hours'"),
        ('({"hours": 32, "blurb": "b", "reward": "r"},)', "missing 'title'"),
        ('({"hours": 32, "title": "t", "blurb": "b", "reward": ""},)', "missing 'reward'"),
        ('({"hours": -5, "title": "t", "blurb": "b", "reward": "r"},)', "hours > 0"),
        ('({"hours": 32, "title": "a", "blurb": "b", "reward": "r"},'
         ' {"hours": 32, "title": "c", "blurb": "d", "reward": "e"},)', "twice"),
        ('("just a string",)', "must be a"),
    ],
)
def test_broken_milestone_entries_are_explained(tmp_path, monkeypatch, table, expected):
    broken = tmp_path / "Announcements.py"
    broken.write_text(f"MILESTONES = {table}\n", encoding="utf-8")
    monkeypatch.setattr(texts, "_candidates", lambda: [broken])
    ok, detail = texts.reload()
    assert not ok
    assert expected in detail
    assert len(milestones.MILESTONES) == 5            # the real table survived


def test_event_length_cannot_change_mid_run(tmp_path, monkeypatch, caplog):
    """Editing the last milestone must not silently reshape a running event."""
    import logging

    before = milestones.TOTAL_SECONDS
    override = tmp_path / "Announcements.py"
    override.write_text(
        'MILESTONES = ({"hours": 12, "title": "t", "blurb": "b", "reward": "r"},)\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(texts, "_candidates", lambda: [override])
    with caplog.at_level(logging.ERROR, logger="hell.milestones"):
        ok, _ = texts.reload()
    assert ok                                          # the wording still loads
    assert milestones.TOTAL_SECONDS == before          # …but the clock does not move
    assert "restart the bot to change the event length" in caplog.text.lower()
