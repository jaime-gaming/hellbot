"""Offline dry-run of a full Welcome to Hell event.

Fast-forwards the real engine (no Discord connection) and prints every message
the bot would post, so wording and edge cases can be reviewed before going live.

    python tools/simulate.py            # full 160h success run
    python tools/simulate.py --fail-at 40   # run that dies after 40 hours
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hell.alivecheck import AliveCheckManager
from hell.announcer import Announcer
from hell.config import Config
from hell.engine import (
    EventCancelled,
    EventCompleted,
    EventFailed,
    GraceRecovered,
    GraceStarted,
    HellEngine,
    MilestoneReached,
    Observation,
)
from hell.milestones import TOTAL_SECONDS
from hell.models import ParticipantRef
from hell.storage import Store

HOUR = 3600.0
T0 = 1_760_000_000.0


class FakeBot:
    def get_channel(self, _cid):
        return None


class FakeUser:
    mention = "<@111>"


class _PrintIO:
    """Prints what the alive check would post instead of calling Discord."""

    async def send_check(self, text, user_ids):
        print(" ".join(f"<@{u}>" for u in user_ids))
        print(text)
        return (1, 2)

    async def send_result(self, text):
        print()
        print(text)

    async def kick(self, user_ids, reason):
        return list(user_ids)

    async def replies_since(self, channel_id, message_id, user_ids):
        return set()


async def _alive_preview(manager) -> None:
    people = crowd_at(0)[:4]
    await manager.start(T0, people)
    manager.register_reply(people[0].user_id, "Yes", 1)
    manager.register_reply(people[1].user_id, "yes", 1)
    await manager.resolve(T0 + 300, people)


NAMES = ["Ash", "Vera", "Milo", "Juno", "Kai", "Nova", "Rex", "Sol", "Wren", "Zed"]


def person(i: int) -> ParticipantRef:
    return ParticipantRef(1000 + i, NAMES[i % len(NAMES)])


def crowd_at(hours: float) -> tuple[ParticipantRef, ...]:
    """A deterministic, wobbling VC population: 3-8 people, everyone in and out."""
    base = [0, 1, 2]  # the diehards, always present
    extras = [i for i in range(3, 10) if (int(hours * 3) + i * 7) % 11 < 5]
    return tuple(person(i) for i in base + extras)


def _grace_demo(engine, ann, cfg) -> None:
    """Show what happens the moment the VC empties (on a throwaway engine)."""
    import tempfile as _tempfile
    from pathlib import Path as _Path

    from hell.engine import HellEngine as _Engine
    from hell.engine import Observation as _Obs
    from hell.storage import Store as _Store

    with _tempfile.TemporaryDirectory() as tmp:
        cfg2 = replace(cfg, database_path=_Path(tmp) / "grace.sqlite3")
        store = _Store(cfg2.database_path)
        eng = _Engine(store, cfg2)
        eng.start(
            now=T0,
            guild_id=1,
            voice_channel_id=cfg2.voice_channel_id,
            announce_channel_id=cfg2.announce_channel_id,
            started_by=111,
            initial_participants=crowd_at(0),
        )
        ann2 = Announcer(FakeBot(), cfg2, eng)
        for ev in eng.tick(_Obs(now=T0 + 3600, participants=())):
            print(ann2.render_grace_warning(ev))
        print()
        for ev in eng.tick(_Obs(now=T0 + 3608, participants=crowd_at(1)[:2])):
            print(ann2.render_grace_recovered(ev))
        store.close()


def _stat_card_demo(engine, cfg) -> None:
    from hell.reports import build_reports, render_report

    reports = build_reports(
        engine.leaderboard(),
        engine.milestone_records(),
        status=engine.status,
        event_elapsed=engine.elapsed(),
    )
    for report in reports[:2]:
        print(render_report(report))
        print("-" * 40)


def banner(text: str) -> None:
    print("\n" + "=" * 78)
    print(text)
    print("=" * 78)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fail-at", type=float, default=None, help="hours after which the VC empties")
    ap.add_argument("--step", type=float, default=300.0, help="simulated seconds per tick")
    args = ap.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(
            token="dry-run",
            guild_id=1,
            voice_channel_id=1539756705997652079,
            announce_channel_id=2,
            gamenight_host_role_id=3,
            clanker_role_id=4,
            database_path=Path(tmp) / "sim.sqlite3",
            # The simulator jumps `step` seconds per tick instead of 1s, so the
            # per-tick credit cap is widened to match. The live bot uses 1s/5s.
            max_tick_credit=args.step,
        )
        store = Store(cfg.database_path)
        engine = HellEngine(store, cfg)
        ann = Announcer(FakeBot(), cfg, engine)

        participants = crowd_at(0)
        engine.start(
            now=T0,
            guild_id=1,
            voice_channel_id=cfg.voice_channel_id,
            announce_channel_id=cfg.announce_channel_id,
            started_by=111,
            initial_participants=participants,
        )
        banner("START ANNOUNCEMENT")
        print(ann.render_start(engine.snapshot(now=T0, participants=len(participants)), FakeUser.mention, participants))

        printed_progress = False
        t = T0
        end = T0 + TOTAL_SECONDS + args.step
        while t < end and not engine.status.is_terminal:
            t += args.step
            hours = (t - T0) / HOUR
            people: tuple[ParticipantRef, ...] = () if (args.fail_at and hours >= args.fail_at) else crowd_at(hours)
            for ev in engine.tick(Observation(now=t, participants=people)):
                if isinstance(ev, MilestoneReached):
                    banner(f"MILESTONE {ev.milestone.hours}h")
                    print(ann.render_milestone(ev))
                elif isinstance(ev, GraceStarted):
                    banner("EMPTY-VC WARNING (no pings)")
                    print(ann.render_grace_warning(ev))
                elif isinstance(ev, GraceRecovered):
                    banner("RECOVERED")
                    print(ann.render_grace_recovered(ev))
                elif isinstance(ev, EventFailed):
                    banner("FAILURE ANNOUNCEMENT")
                    print(ann.render_failure(ev))
                elif isinstance(ev, EventCompleted):
                    banner("COMPLETION ANNOUNCEMENT")
                    print(ann.render_completion(ev))
                elif isinstance(ev, EventCancelled):
                    banner("CANCELLED ANNOUNCEMENT")
                    print(ann.render_cancelled(ev))
            if not printed_progress and hours >= 73.4:
                printed_progress = True
                banner("LIVE PROGRESS MESSAGE (edited every 20s)")
                print(ann.render_progress(engine.snapshot(now=t, participants=len(people))))

        banner("EMPTY-VC GRACE PERIOD")
        _grace_demo(engine, ann, cfg)

        banner("ALIVE CHECK (random every 1-6h)")
        preview = AliveCheckManager(cfg, store, _PrintIO())
        preview.bind("preview", now=T0)
        import asyncio as _asyncio

        _asyncio.run(_alive_preview(preview))

        banner("LEADERBOARD")
        print(ann.render_leaderboard_message(engine.leaderboard(), engine.status.is_terminal))
        banner("END-OF-EVENT DM (sent to every contestant)")
        _stat_card_demo(engine, cfg)

        banner(f"FINAL STATUS: {engine.status.value}  (elapsed {engine.elapsed(t) / HOUR:.2f}h)")
        store.close()


if __name__ == "__main__":
    main()
