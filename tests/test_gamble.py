"""Tests for the gambling system — bookkeeping, session stats, clocks and limits.

The pure rules (parsing, odds, cooldowns) live in `hell/gamble.py`; these tests
drive the full `/hell gamble` flow through `HellCommands._perform_gamble` with
a deterministic roll.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from hell.gamble import (
    GambleBook,
    GambleStats,
    gamble_time_loss_multiplier,
    odds_summary,
    resolve_gamble,
    win_chance_for_bet,
    win_multiplier_for_bet,
)
from tests.conftest import HOUR, obs, start


def make_cog(engine, config):
    from hell.announcer import Announcer
    from hell.cog import HellCommands
    from hell.monitor import VoiceMonitor

    bot = MagicMock()
    announcer = Announcer(bot, config, engine)
    monitor = VoiceMonitor(bot, config, engine, announcer)
    cog = HellCommands(bot, config, engine, monitor)
    return cog


def start_d3(engine, now, *ids):
    """Start a run 97h ago (Difficulty 3 / Torment) and give the users a tick."""
    start(engine, now - 97 * HOUR, *ids)
    engine.tick(obs(now, *ids))


def user_mock(uid: int = 1) -> MagicMock:
    return MagicMock(id=uid, display_name=f"User{uid}", mention=f"<@{uid}>")


def bet(cog, engine, user, hours=None, clock="real", roll=0.1):
    with patch("random.random", return_value=roll):
        return asyncio.run(cog._perform_gamble(user, hours=hours, clock=clock))


def clear_cooldown(cog, engine, uid: int = 1) -> None:
    """Treat the last bet as ancient (the quota window stays real)."""
    cog._gamble_book.store.set_meta(f"gamble_cd:{engine.event_uid}:{uid}", "0")


# ----------------------------------------------------------------- bookkeeping


def test_gamble_win_message_matches_the_ledger(engine, config):
    """The 'gain' shown to the player must be exactly what is credited."""
    cog = make_cog(engine, config)
    now = now_ts_safe()
    start_d3(engine, now, 1)
    engine.store.add_user_time(engine.event_uid, [(1, "User1", 3600.0, now)])
    initial = next(e.seconds for e in engine.leaderboard() if e.user_id == 1)

    ok, msg = bet(cog, engine, user_mock(1), roll=0.1)  # win, 0.25h bet
    assert ok
    # Diff 3: 1.5x payout on a 900s stake -> +1350s credited.
    new = next(e.seconds for e in engine.leaderboard() if e.user_id == 1)
    assert new == pytest.approx(initial + 1350.0)
    # The message shows the credited amount (0h 22m) and the 1.5x payout.
    assert "0h 22m" in msg
    assert "1.5x" in msg
    # Session stats line: 1 bet, 1 win, no losses, net = what was credited.
    assert "1 bet · 1W/0L · net +0h 22m" in msg


def test_gamble_stats_persist_and_track_jackpots(engine, store, config):
    cog = make_cog(engine, config)
    now = now_ts_safe()
    start_d3(engine, now, 1)
    engine.store.add_user_time(engine.event_uid, [(1, "User1", 4 * HOUR, now)])
    user = user_mock(1)

    # Win (1.5x on 900s = +1350s), jackpot (+1.0 -> 2.5x = +2250s), loss (-900s).
    ok1, _ = bet(cog, engine, user, roll=0.1)
    clear_cooldown(cog, engine, 1)
    ok2, _ = bet(cog, engine, user, roll=0.01)
    clear_cooldown(cog, engine, 1)
    ok3, msg3 = bet(cog, engine, user, roll=0.99)
    assert ok1 and ok2 and ok3

    stats = cog._gamble_book.stats(engine.event_uid, 1)
    assert stats.bets == 3
    assert stats.wins == 2
    assert stats.losses == 1
    assert stats.jackpots == 1
    assert stats.won_seconds == pytest.approx(1350.0 + 2250.0)
    assert stats.lost_seconds == pytest.approx(900.0)
    assert stats.net_seconds == pytest.approx(1350.0 + 2250.0 - 900.0)
    assert "2W/1L" in msg3
    assert "· 1 💎" not in msg3  # the stats line has no jackpot count; that lives in mystats

    # Survives a new GambleBook (same store, same event): stats are persisted.
    stats_again = GambleBook(engine.store).stats(engine.event_uid, 1)
    assert stats_again.bets == 3 and stats_again.jackpots == 1


def test_gamble_stats_start_clean_for_each_event(engine, config):
    cog = make_cog(engine, config)
    now = now_ts_safe()
    start_d3(engine, now, 1)
    engine.store.add_user_time(engine.event_uid, [(1, "User1", HOUR, now)])
    bet(cog, engine, user_mock(1), roll=0.1)

    uid_a = engine.event_uid
    engine.reset()
    start_d3(engine, now, 1)
    engine.store.add_user_time(engine.event_uid, [(1, "User1", HOUR, now)])

    assert engine.event_uid != uid_a
    assert cog._gamble_book.stats(uid_a, 1).bets == 1
    assert cog._gamble_book.stats(engine.event_uid, 1).bets == 0


def test_gamble_cooldowns_do_not_leak_across_events(engine, config):
    """A bet in one run must not block the first bet of a brand-new run."""
    cog = make_cog(engine, config)
    now = now_ts_safe()
    start_d3(engine, now, 1)
    engine.store.add_user_time(engine.event_uid, [(1, "User1", HOUR, now)])
    ok, _ = bet(cog, engine, user_mock(1), roll=0.1)
    assert ok

    engine.reset()
    start_d3(engine, now, 1)
    engine.store.add_user_time(engine.event_uid, [(1, "User1", HOUR, now)])
    # Immediately, without any artificial cooldown reset: the new event has
    # its own (empty) book, so this must succeed.
    ok2, _msg = bet(cog, engine, user_mock(1), roll=0.1)
    assert ok2


# --------------------------------------------------------------------- clocks


def test_gamble_clock_loss_costs_more_than_the_stake(engine, config):
    cog = make_cog(engine, config)
    now = now_ts_safe()
    start_d3(engine, now, 1)
    engine.add_gamble_seconds(1, "User1", 10 * HOUR)
    wallet0 = engine.gamble_wallet(1)

    ok, msg = bet(cog, engine, user_mock(1), hours=0.25, clock="gamble", roll=0.99)
    assert ok
    # 15m chip at the min stake: the loss is 1.5x the stake.
    assert engine.gamble_wallet(1) == pytest.approx(wallet0 - 900.0 * 1.5)
    assert "no mute" in msg.lower()
    assert "Gamble Time" in msg


def test_gamble_clock_max_bet_loss_costs_2x(engine, config):
    cog = make_cog(engine, config)
    now = now_ts_safe()
    start_d3(engine, now, 1)
    engine.add_gamble_seconds(1, "User1", 10 * HOUR)
    wallet0 = engine.gamble_wallet(1)

    ok, _ = bet(cog, engine, user_mock(1), hours=1.0, clock="gamble", roll=0.99)
    assert ok
    assert engine.gamble_wallet(1) == pytest.approx(wallet0 - 3600.0 * 2.0)


def test_gamble_clock_no_time_error_shows_the_real_requirement(engine, config):
    cog = make_cog(engine, config)
    now = now_ts_safe()
    start_d3(engine, now, 1)
    # Wallet covers the stake (900s) but not the worst-case loss (1350s).
    # (start_d3 granted the 1h starting wallet — top it up to exactly 1000s.)
    engine.add_gamble_seconds(1, "User1", 1000.0 - 3600.0)
    assert engine.gamble_wallet(1) == pytest.approx(1000.0)

    ok, msg = bet(cog, engine, user_mock(1), hours=0.25, clock="gamble", roll=0.1)
    assert not ok
    assert "need more than" in msg.lower()
    assert "Gamble Time" in msg
    # The required amount is the worst-case loss, not the bare stake.
    assert "0h 22m" in msg  # 1350s, not 0h 15m


def test_gamble_win_credits_the_wallet(engine, config):
    cog = make_cog(engine, config)
    now = now_ts_safe()
    start_d3(engine, now, 1)
    engine.add_gamble_seconds(1, "User1", 10 * HOUR)
    wallet0 = engine.gamble_wallet(1)

    ok, msg = bet(cog, engine, user_mock(1), hours=0.25, clock="gamble", roll=0.1)
    assert ok
    assert engine.gamble_wallet(1) == pytest.approx(wallet0 + 900.0 * 1.5)
    assert "Gamble Time" in msg


# --------------------------------------------------------------------- report


def test_mystats_shows_the_gamble_section(engine, config):
    cog = make_cog(engine, config)
    now = now_ts_safe()
    start_d3(engine, now, 1)
    engine.store.add_user_time(engine.event_uid, [(1, "User1", HOUR, now)])
    bet(cog, engine, user_mock(1), roll=0.1)

    embed, err = cog._build_mystats_payload(1)
    assert err is None
    fields = {f.name: f.value for f in embed.fields}
    assert "🎰 Gambling" in fields
    assert "1W / 0L" in fields["🎰 Gambling"]
    assert "+0h 22m" in fields["🎰 Gambling"]


def test_mystats_hides_the_gamble_section_without_any_gambling(engine, config):
    cog = make_cog(engine, config)
    now = now_ts_safe()
    start_d3(engine, now, 1)  # starting wallet exists, but nobody has bet yet

    embed, err = cog._build_mystats_payload(1)
    assert err is None
    # With a starting wallet but zero bets the section still shows (wallet line).
    fields = {f.name for f in embed.fields}
    assert "🎰 Gambling" in fields

    # A brand-new user with no wallet and no bets gets no gambling field.
    from hell.models import ParticipantRef

    engine.store.touch_users(engine.event_uid, [ParticipantRef(2, "User2")], now)
    embed2, err2 = cog._build_mystats_payload(2)
    assert err2 is None
    assert "🎰 Gambling" not in {f.name for f in embed2.fields}


# ----------------------------------------------------------- pure rules (unit)


def test_gamble_stats_round_trip():
    s = GambleStats(bets=2, wins=1, losses=1, jackpots=1,
                    staked_seconds=1800, won_seconds=2700, lost_seconds=900)
    data = s.to_dict()
    again = GambleStats.from_dict(data)
    assert again == s
    assert again.win_rate == pytest.approx(0.5)
    assert GambleStats().win_rate == 0.0
    assert GambleStats.from_dict(None) == GambleStats()
    assert GambleStats.from_dict({"bets": "x", "won": "y"}) == GambleStats()


def test_odds_and_payout_curve():
    from hell.difficulty import get_difficulty_by_level

    d3 = get_difficulty_by_level(3)
    assert win_chance_for_bet(d3, 0.25) == pytest.approx(0.38)
    assert win_chance_for_bet(d3, 1.0) == pytest.approx(0.38 * 0.70)
    assert win_multiplier_for_bet(d3, 0.25) == pytest.approx(1.5)
    assert win_multiplier_for_bet(d3, 1.0) == pytest.approx(2.0)

    # Every bet the player can make is negative expected value (with a real
    # margin, not floating-point noise): the house is Hell and Hell wins.
    for level in (3, 4):
        d = get_difficulty_by_level(level)
        for bet_h in (0.25, 0.5, 1.0, 2.0):
            if bet_h > d.gamble_max_bet_hours:
                continue
            p = win_chance_for_bet(d, bet_h)
            gain = win_multiplier_for_bet(d, bet_h)
            # Real Timer: win credits bet*mult, loss costs the stake.
            ev_real = p * gain - (1 - p) * 1.0
            # Gamble Time: loss costs up to 2x the stake.
            ev_wallet = p * gain - (1 - p) * gamble_time_loss_multiplier(d, bet_h)
            assert ev_real < -0.01, f"Real Timer EV not negative enough at D{level}, bet {bet_h}h: {ev_real}"
            assert ev_wallet < -0.01, f"Gamble Time EV not negative enough at D{level}, bet {bet_h}h: {ev_wallet}"

    # A roll just under the chance wins; one over it loses; deep in the band a
    # jackpot (0.02 < 0.38*0.08 = 0.0304).
    r_win = resolve_gamble(d3, 0.25, roll=0.35)
    r_lose = resolve_gamble(d3, 0.25, roll=0.39)
    r_jack = resolve_gamble(d3, 0.25, roll=0.02)
    assert r_win.won and not r_win.jackpot
    assert not r_lose.won
    assert r_jack.won and r_jack.jackpot
    assert r_jack.multiplier == pytest.approx(1.5 + 1.0)


def test_odds_summary_reports_both_bet_endpoints():
    from hell.difficulty import get_difficulty_by_level

    d3 = get_difficulty_by_level(3)
    s3 = odds_summary(d3)
    assert s3 is not None
    assert s3.level == 3 and s3.name == "Torment"
    assert s3.base_chance == pytest.approx(0.38)
    assert s3.base_multiplier == pytest.approx(1.5)
    assert s3.max_chance == pytest.approx(0.38 * 0.70)
    assert s3.max_multiplier == pytest.approx(2.0)
    assert s3.max_bet_hours == 1.0
    assert s3.hourly_limit == 2
    assert s3.overflow_cooldown_seconds == 2700.0
    assert s3.loss_mute_seconds == 60

    s4 = odds_summary(get_difficulty_by_level(4))
    assert s4.base_chance == pytest.approx(0.28)
    assert s4.base_multiplier == pytest.approx(2.5)
    assert s4.max_multiplier == pytest.approx(3.0)

    # Locked tiers produce no odds card.
    for lvl in (0, 1, 2):
        assert odds_summary(get_difficulty_by_level(lvl)) is None


def test_odds_embed_shows_the_current_numbers(engine, config):
    from hell.announcer import Announcer
    from hell.difficulty import get_difficulty_by_level
    from hell.embeds import embed_to_text

    embeds = Announcer(MagicMock(), config, engine).embeds
    unlocked = embed_to_text(embeds.gamble_odds(get_difficulty_by_level(3)))
    assert "38%" in unlocked and "1.5x" in unlocked
    assert "26%" in unlocked and "2.0x" in unlocked
    assert "jackpot" in unlocked.lower()
    assert "overflow" in unlocked.lower()

    locked = embed_to_text(embeds.gamble_odds(get_difficulty_by_level(2)))
    assert "locked" in locked.lower()
    assert "96h" in locked


def now_ts_safe() -> float:
    from hell.timeutil import now_ts

    return now_ts()
