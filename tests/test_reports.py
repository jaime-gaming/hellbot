"""End-of-event stat cards: content, ranking, rewards and delivery."""

from __future__ import annotations

import asyncio

import pytest

from hell.dm import FinalReportDM
from hell.models import EventStatus, LeaderboardEntry, MilestoneRecord, ParticipantRef
from hell.reports import build_reports, render_report
from hell.timeutil import format_clock
from tests.conftest import HOUR, T0, empty_out, obs, start

TOTAL = 160 * HOUR


def entries(*pairs) -> list[LeaderboardEntry]:
    out, last, rank = [], None, 0
    for index, (uid, seconds) in enumerate(sorted(pairs, key=lambda p: -p[1]), start=1):
        rank = rank if seconds == last else index
        out.append(LeaderboardEntry(rank=rank, user_id=uid, display_name=f"User{uid}", seconds=seconds))
        last = seconds
    return out


def records(*pairs) -> list[MilestoneRecord]:
    return [
        MilestoneRecord(
            hours=hours,
            reached_ts=T0 + hours * HOUR,
            announced=True,
            members=[ParticipantRef(u, f"User{u}") for u in members],
        )
        for hours, members in pairs
    ]


# ------------------------------------------------------------- the numbers

def test_clock_format_matches_the_requested_shape():
    assert format_clock(0) == "0:00:00"
    assert format_clock(128 * HOUR + 42 * 60 + 15) == "128:42:15"
    assert format_clock(160 * HOUR) == "160:00:00"


def test_report_has_time_rank_and_rewards():
    board = entries((1, 100 * HOUR), (2, 50 * HOUR), (3, 10 * HOUR))
    reps = build_reports(
        board,
        records((32, [1, 2]), (64, [1])),
        status=EventStatus.FAILED,
        event_elapsed=70 * HOUR,
    )
    by_id = {r.user_id: r for r in reps}
    assert by_id[1].rank == 1 and by_id[1].survived == "100:00:00"
    assert by_id[1].milestones == [32, 64] and by_id[1].reward_count == 2
    assert by_id[2].milestones == [32] and by_id[2].reward_count == 1
    assert by_id[3].milestones == [] and by_id[3].reward_count == 0
    assert by_id[1].participants == 3


def test_rendered_card_uses_the_requested_lines():
    board = entries((1, 128 * HOUR + 42 * 60 + 15), (2, 10 * HOUR))
    report = build_reports(
        board, records((32, [1])), status=EventStatus.FAILED, event_elapsed=40 * HOUR
    )[0]
    text = render_report(report)
    lines = [line for line in text.split("\n") if line]
    assert lines[0] == "**WELCOME TO HELL**"
    assert lines[1] == "**128:42:15 SURVIVED**"
    assert lines[2] == "**YOU WERE... TOP 1**"
    assert "**YOU WON 1 REWARD**" in text
    assert "32h — @hell (limited)" in text


def test_top3_get_every_reward_plus_the_bonus_when_completed():
    board = entries((1, 160 * HOUR), (2, 150 * HOUR), (3, 140 * HOUR), (4, 10 * HOUR))
    reps = {
        r.user_id: r
        for r in build_reports(
            board, records((32, [4])), status=EventStatus.COMPLETED, event_elapsed=TOTAL
        )
    }
    assert reps[1].bonus and reps[1].reward_count == 6      # 5 milestones + @cool people :D
    assert "@cool people :D" in render_report(reps[1])
    assert reps[3].bonus is True
    assert reps[4].bonus is False and reps[4].reward_count == 1


def test_top3_bonus_only_applies_to_a_completed_run():
    board = entries((1, 100 * HOUR), (2, 90 * HOUR))
    reps = build_reports(board, records(), status=EventStatus.FAILED, event_elapsed=100 * HOUR)
    assert all(not r.bonus for r in reps)
    assert "no rewards this time" in render_report(reps[0]).lower()


def test_ties_share_a_rank_on_the_card():
    board = entries((1, 50 * HOUR), (2, 50 * HOUR), (3, 10 * HOUR))
    reps = {r.user_id: r for r in build_reports(
        board, records(), status=EventStatus.CANCELLED, event_elapsed=60 * HOUR
    )}
    assert reps[1].rank == reps[2].rank == 1
    assert reps[3].rank == 3


def test_zero_time_contestant_still_gets_a_card():
    board = entries((1, 100 * HOUR), (2, 0.0))
    reps = build_reports(board, records(), status=EventStatus.FAILED, event_elapsed=100 * HOUR)
    assert reps[-1].survived == "0:00:00"


# ---------------------------------------------------------------- delivery

class FakeUser:
    def __init__(self, uid, *, blocked=False):
        self.id = uid
        self.blocked = blocked
        self.messages = []

    async def send(self, **kwargs):
        if self.blocked:
            import discord

            raise discord.Forbidden(_FakeResponse(), "DMs closed")
        self.messages.append(kwargs)


class _FakeResponse:
    status = 403
    reason = "Forbidden"


class FakeBot:
    def __init__(self, blocked=()):
        self.users = {}
        self.blocked = set(blocked)

    def get_user(self, uid):
        return self.users.setdefault(uid, FakeUser(uid, blocked=uid in self.blocked))

    def get_channel(self, _cid):
        return None


class FakeAnnouncer:
    def __init__(self):
        self.sent = []

    async def send(self, embeds, **kwargs):
        self.sent.append(embeds)


@pytest.fixture
def finished_engine(engine):
    """A run with two users that ends in failure."""
    start(engine, T0, 1, 2)
    for i in range(1, 121):
        engine.tick(obs(T0 + i, 1, 2))
    empty_out(engine, T0 + 121)
    assert engine.status is EventStatus.FAILED
    return engine


def test_every_contestant_is_messaged(finished_engine, config):
    bot = FakeBot()
    sender = FinalReportDM(bot, config, finished_engine, FakeAnnouncer())
    config.dm_delay_seconds = 0
    summary = asyncio.run(sender.send_all())
    assert summary["sent"] == 2 and summary["total"] == 2
    assert all(u.messages for u in bot.users.values())


def test_delivery_is_not_repeated_after_a_restart(finished_engine, config, store):
    config.dm_delay_seconds = 0
    bot = FakeBot()
    sender = FinalReportDM(bot, config, finished_engine, FakeAnnouncer())
    asyncio.run(sender.send_all())

    bot2 = FakeBot()
    sender2 = FinalReportDM(bot2, config, finished_engine, FakeAnnouncer())
    assert sender2.pending() == []
    summary = asyncio.run(sender2.send_all())
    assert summary["total"] == 0
    assert store.dm_summary(finished_engine.event_uid)["sent"] == 2


def test_partial_delivery_resumes(finished_engine, config, store):
    config.dm_delay_seconds = 0
    store.record_dm(finished_engine.event_uid, 1, "sent")
    bot = FakeBot()
    sender = FinalReportDM(bot, config, finished_engine, FakeAnnouncer())
    assert [r.user_id for r in sender.pending()] == [2]
    summary = asyncio.run(sender.send_all())
    assert summary["total"] == 1


def test_closed_dms_are_recorded_not_retried_forever(finished_engine, config, store):
    config.dm_delay_seconds = 0
    bot = FakeBot(blocked={2})
    announcer = FakeAnnouncer()
    sender = FinalReportDM(bot, config, finished_engine, announcer)
    summary = asyncio.run(sender.send_all())
    assert summary["sent"] == 1 and summary["blocked"] == 1
    assert store.dm_summary(finished_engine.event_uid)["blocked"] == 1
    assert sender.pending() == []
    assert announcer.sent, "a delivery summary is posted in the channel"


def test_nothing_is_sent_while_the_event_is_running(engine, config):
    start(engine, T0, 1)
    engine.tick(obs(T0 + 5, 1))
    sender = FinalReportDM(FakeBot(), config, engine, FakeAnnouncer())
    assert asyncio.run(sender.send_all())["total"] == 0


def test_dms_can_be_disabled(finished_engine, config):
    config.send_final_dms = False
    sender = FinalReportDM(FakeBot(), config, finished_engine, FakeAnnouncer())
    assert asyncio.run(sender.send_all())["total"] == 0


def test_mystats_lookup(finished_engine, config):
    sender = FinalReportDM(FakeBot(), config, finished_engine, FakeAnnouncer())
    assert sender.report_for(1) is not None
    assert sender.report_for(999) is None
