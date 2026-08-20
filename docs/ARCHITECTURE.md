# Architecture

How the bot is put together, and the invariants each part is responsible for.
If you are changing behaviour, start here; if you are changing wording, you only
need [`Announcements.py`](../Announcements.py).

## Layers

```
                     Announcements.py        ← all wording, rewards, colours
                            │
                     hell/texts.py           ← loads it, hot-reloads it, never crashes on it
                            │
  ┌─────────────────────────┴───────────────────────────────────────────┐
  │  PURE CORE — no Discord imports, fully unit-testable                 │
  │                                                                      │
  │  timeline.py    global event clock            0 → 160h               │
  │  tracking.py    per-user session clocks       0 → Xh each            │
  │  grace.py       empty-VC grace window         15s state machine      │
  │  milestones.py  milestone table + lookups                            │
  │  leaderboard.py ranking, ties, podium rendering                      │
  │  reports.py     end-of-event stat cards                              │
  │  alivecheck.py  roll-call scheduling and resolution                  │
  │  engine.py      the state machine that ties the above together       │
  │  storage.py     SQLite persistence for all of it                     │
  └─────────────────────────┬───────────────────────────────────────────┘
                            │  Observation in  ·  DomainEvent out
  ┌─────────────────────────┴───────────────────────────────────────────┐
  │  DISCORD EDGE                                                        │
  │                                                                      │
  │  monitor.py     1s VC polling, 10s progress edit, dispatch           │
  │  embeds.py      how every message looks                              │
  │  announcer.py   how every message is delivered                       │
  │  aliveio.py     roll-call pings, kicks, reply backfill               │
  │  dm.py          stat-card delivery                                   │
  │  logsink.py     live log stream to the operator's DMs                │
  │  cog.py + ui.py slash commands, confirmations, permission checks     │
  │  health.py      startup preflight (IDs, permissions, intents)        │
  │  bot.py         wiring and lifecycle                                 │
  └──────────────────────────────────────────────────────────────────────┘
```

`launcher/` is a separate desktop front-end (`envfile.py` config I/O,
`runtime.py` supervisor thread, `gui.py` tkinter window). It imports the bot;
the bot never imports it.

## The two clocks

| | Module | Range | Moved by | Stopped by |
|---|---|---|---|---|
| Global event timeline | `timeline.py` | 0 → 160h | the wall clock only | the run ending |
| Per-user session timeline | `tracking.py` | 0 → Xh each | that person being in the VC | that person leaving |

Nothing an individual does — leaving, disconnecting, being kicked — moves the
global clock while at least one valid human remains.

## Invariants

1. **Time is absolute.** Every calculation uses POSIX timestamps; nothing
   depends on process uptime, so a restart cannot shift or reset the timer.
2. **The clock never passes 160h.** `EventTimeline.clamp()` is applied before
   any elapsed value is used or stored.
3. **Terminal is terminal.** Once FAILED / COMPLETED / CANCELLED, ticks are
   inert, the leaderboard is frozen, and only `/hell reset` clears it.
4. **A milestone fires exactly once.** `INSERT OR IGNORE` decides the winner;
   `announced` is only set after Discord accepts the message, so a crash
   between the two re-posts rather than loses it.
5. **Credit is only granted for observed seconds**, capped per tick
   (`MAX_TICK_CREDIT_SECONDS`), so downtime is never silently paid out.
6. **Bots and `@clanker` never exist** as far as the core is concerned — the
   monitor filters them out before an `Observation` is built.
7. **An empty VC opens a grace window, not an immediate failure**, and failure
   is timestamped at the moment the VC emptied, never later.
8. **Nothing in the message path can break the event.** Bad text, a missing
   placeholder, a failed send, a closed DM: all logged, none fatal.
9. **Background work never blocks the 1-second loop** — roll-call I/O and DM
   delivery run through `hell/tasks.spawn()`, which also logs failures.

## Data model (SQLite, schema v2)

| Table | Holds |
|---|---|
| `event` | one row: status, start/end timestamps, channel + message IDs, grace window |
| `user_time` | per-user accumulated seconds (timeline #2) |
| `presence` | who is in the VC right now (written only on change) |
| `milestones` / `milestone_members` | claims, timestamps, eligible snapshots |
| `final_leaderboard` | frozen rankings |
| `alive_check` / `alive_check_history` | pending roll call and past results |
| `dm_log` | who already received a stat card (resumable delivery) |
| `meta` | schema version, next roll-call time, unobserved seconds |

Migrations are additive and run at startup (`Store._migrate`).

## Testing

`tests/` mirrors the layers: the pure core is tested directly, the Discord edge
through fakes (`tests/test_integration.py` drives monitor → engine → announcer →
channel and asserts no message ever ships an unrendered `{placeholder}`).

```bash
./tools/check.sh          # compile, pyflakes, ruff, mypy, pytest, simulations
python tools/simulate.py  # print every message of a full 160h run, offline
```
