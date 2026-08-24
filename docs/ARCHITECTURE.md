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
  │  monitor.py     1s VC polling, timed progress edits, dispatch           │
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
3. **Terminal is terminal unless explicitly resumed.** Once FAILED / COMPLETED / CANCELLED,
   ticks are inert, the leaderboard is frozen, and only `/hell reset` clears it (or
   `/hell resume` with operator MFA to continue a failed run).
4. **A milestone fires exactly once.** `INSERT OR IGNORE` decides the winner;
   `announced` is only set after Discord accepts the message, so a crash
   between the two re-posts rather than loses it.
5. **Credit is only granted for observed seconds**, capped per tick
   (`MAX_TICK_CREDIT_SECONDS`), so downtime is never silently paid out — with one
   deliberate exception: an outage shorter than `DOWNTIME_CREDIT_SECONDS` is credited back to
   users seen in the VC both before and after it, because they demonstrably never left.
6. **Bots and `@clanker` never exist** as far as the core is concerned — the
   monitor filters them out before an `Observation` is built.
7. **An empty VC opens a grace window, not an immediate failure**, and failure
   is timestamped at the moment the VC emptied, never later.  If an Are You Alive?
   check causes the VC to become empty, a 2-minute recovery grace period is used
   instead of the normal 15-second window.
8. **Nothing in the message path can break the event.** Bad text, a missing
   placeholder, a failed send, a closed DM: all logged, none fatal.
9. **Background work never blocks the 1-second loop** — roll-call I/O and DM
   delivery run through `hell/tasks.spawn()`, which also logs failures.
10. **Crash-safe ordering: the DB is never left contradicting Discord.** Every
    stateful operation writes its DB row *before* the Discord API call that
    depends on it, and cleans up (or marks `pending`) on failure. Concretely:
    - grace expiry runs `_terminate(FAILED)` (a single `save_state` that sets
      `status` and clears `grace_started_ts`) before any in-memory grace
      cleanup, so a crash can never leave the event RUNNING with no window;
    - an alive check is persisted to `alive_check` *before* `send_check` is
      called; a failed Discord send deletes the tentative row (never
      "DB says active, Discord says it doesn't exist"), and a successful send
      is followed by a second write adding the real `channel_id`/`message_id`;
      an *exception* from `send_check` (network error) cleans up exactly like
      a `None` result, so no phantom check can later kick people who never
      saw a roll call;
    - alive-check resolution writes `alive_check_history` and clears the
      pending row *before* kicking anybody, so a crash mid-kick leaves a
      resolvable history and no stale pending check;
    - each stat-card DM is recorded in `dm_log` as `pending` *before* the DM
      API call and updated to `sent`/`blocked`/`failed` after, so a crash
      mid-delivery retries exactly the right recipients (and never
      double-messages anyone marked `sent`).
11. **Pause is an offset, not a stop.** `/hell pause` does not change the
    event status — it records `paused_ts` and banks `paused_seconds` into the
    `event` row. Every time reading is converted with `_effective_now()`
    (`now − paused_seconds − in-progress pause`), so while paused both
    timelines stand still, milestones/grace/completion are inert, and after
    `/hell resume` the clocks continue exactly where they stopped. An open
    empty-VC grace window is shifted forward by the pause on resume, so its
    countdown resumes with the time it had left; a restart while paused comes
    back paused; and the container healthcheck treats a paused event as
    healthy (a frozen `last_tick_ts` is *not* a stalled monitor).

## Data model (SQLite, schema v3)

| Table | Holds |
|---|---|
| `event` | one row: status, start/end timestamps, channel + message IDs, grace window, `last_valid_observed_ts`, pause state (`paused_ts`, `paused_seconds`, `pause_reason`) |
| `user_time` | per-user accumulated seconds (timeline #2) |
| `presence` | who is in the VC right now (written only on change) |
| `milestones` / `milestone_members` | claims, timestamps, eligible snapshots |
| `final_leaderboard` | frozen rankings |
| `alive_check` / `alive_check_history` | pending roll call and past results |
| `dm_log` | who already received a stat card (resumable delivery) |
| `meta` | schema version, next roll-call time, unobserved seconds |

Migrations are additive and run at startup (`Store._migrate`).

### Crash reconstruction fields

These fields on the `event` row are sufficient to reconstruct the full event
state after any crash — everything else is derived:

| Field | Stores |
|---|---|
| `start_ts` | when the event started |
| `status` | IDLE / RUNNING / FAILED / COMPLETED / CANCELLED |
| `last_tick_ts` | last trusted VC observation (any population) |
| `last_valid_observed_ts` | last observation with ≥1 valid human |
| `grace_started_ts` | when the empty-VC grace window began |
| `paused_ts` / `paused_seconds` | whether the event is frozen and how much time has been banked as paused |
| `end_ts` | when the event terminated (fail/complete/cancel), stored as pause-adjusted event time |
| `milestones` table | which milestones were reached and when |
| `user_time` table | per-user accumulated seconds |

Given these plus the wall clock, `resume_after_restart()` can continue watching,
resume a grace window, freeze the leaderboard, or re-send missed announcements
without guessing — the same invariants are exercised by the crash-injection
tests in `tests/test_resilience.py`.

## Testing

`tests/` mirrors the layers — no network, no Discord token, no display needed:

| Test file | Covers |
|---|---|
| `test_engine`, `test_grace`, `test_properties` | the state machine, the grace rule, and randomised chaos runs asserting the invariants above |
| `test_storage`, `test_reports`, `test_leaderboard_and_milestones` | persistence, stat cards, ranking |
| `test_alivecheck`, `test_health_and_aliveio` | roll calls end to end, preflight diagnostics |
| `test_announcer`, `test_announcements_file` | embed limits, and that `Announcements.py` really drives every message |
| `test_integration`, `test_command_flows`, `test_bot`, `test_monitor` | the Discord edge through fakes: monitor → engine → announcer → channel, every slash command callback, event routing |
| `test_gui`, `test_launcher`, `test_logging`, `test_logsink` | the desktop launcher (headless via `tests/faketk.py`) and logging |
| `test_deployment`, `test_consistency` | the container actually starts from the files it copies; config, commands and docs cannot drift apart |
| `test_resilience` | the "Discord said no" paths: forbidden channels, rate limits, deleted messages, restarts mid-roll-call — plus **crash injection** at every DB-write ↔ Discord-API boundary (grace termination, alive-check start/resolve, stat-card delivery, `last_valid_observed_ts` reconstruction) |

```bash
./tools/check.sh          # compile, pyflakes, ruff, mypy, pytest, simulations
python tools/simulate.py  # print every message of a full 160h run, offline
```
