# 🔥 Welcome to Hell — Discord event bot

A production-ready Discord bot that runs the **Welcome to Hell** event: keep at least
one real human in a single voice channel, **continuously, for 160 hours**. The moment
that VC is empty of valid humans, the run is dead.

* Target VC: `1539756705997652079` (configurable)
* Duration: **160 consecutive hours**
* Milestones: **32h · 64h · 96h · 128h · 160h**
* Started manually with `/hell start` by `@gamenight host`
* Bots never count · `@clanker` users are kicked from the VC on sight · AFK still counts
* Everything is timestamp-based and persisted in SQLite — **restarting the bot never resets the timer**

---

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # then fill in the token, channel IDs and role IDs
python bot.py
```

### Discord setup checklist

1. **Developer Portal → Bot → Privileged Gateway Intents**: enable **Server Members Intent**.
   (`voice_states` and `guilds` are non-privileged and enabled in code.)
2. **Bot permissions** in the server:
   | Permission | Why |
   |---|---|
   | View Channel + Connect on the target VC | to see who is inside |
   | **Move Members** | to disconnect `@clanker` users |
   | Send Messages / Embed Links / Read Message History in the announcement channel | announcements + editing the progress message |
   | **Mention @everyone** | milestone / start / failure / completion pings |
3. Fill in `.env`:

| Variable | Required | Meaning |
|---|---|---|
| `DISCORD_TOKEN` | ✅ | bot token |
| `GUILD_ID` | ✅ | server ID (slash commands are synced to this guild instantly) |
| `VOICE_CHANNEL_ID` | ✅ (defaults to `1539756705997652079`) | the Hell VC |
| `ANNOUNCE_CHANNEL_ID` | ✅ | text channel for all announcements + the live progress message |
| `GAMENIGHT_HOST_ROLE_ID` | ✅ | `@gamenight host` — the only role allowed to start/stop/reset |
| `CLANKER_ROLE_ID` | ✅ | `@clanker` — auto-disconnected, never earns leaderboard time |
| `HELL_ROLE_ID`, `HELLIST_ROLE_ID`, `HELL_MASTER_ROLE_ID`, `COOL_PEOPLE_ROLE_ID` | optional | only used to render real role mentions in reward messages |
| `DATABASE_PATH` | optional | default `data/hell.sqlite3` |
| `MONITOR_INTERVAL` / `PROGRESS_INTERVAL` | optional | default `1` s / `10` s |
| `STARTUP_GRACE_SECONDS` | optional | default `15` — VC reads right after boot are observed but can't fail the event (cold cache guard) |
| `MAX_TICK_CREDIT_SECONDS` | optional | default `5` — max leaderboard credit per tick, so downtime is never silently credited |
| `REQUIRE_OCCUPANTS_TO_START` | optional | default `true` — refuses to start into an empty VC |

> **Rewards are announcement-only.** The bot never assigns roles; it posts exactly who is
> eligible at each milestone so a human can hand them out. (Everything needed to flip this
> on later lives in `Announcer`.)

---

## Commands

| Command | Who | What |
|---|---|---|
| `/hell start` | `@gamenight host` | Starts the event: status → `RUNNING`, records the absolute start timestamp, starts the 160h timer, begins VC monitoring + per-user tracking, posts the start announcement. Rejected if an event is already running. |
| `/hell status` | everyone | Status, elapsed, remaining, % complete, progress bar, live VC population, current + next milestone, and the milestones already reached. |
| `/hell leaderboard` | everyone | Current (or frozen final) leaderboard, Top 3 highlighted, everyone else listed below. |
| `/hell stop` | `@gamenight host` | Button confirmation → marks the event **CANCELLED** (explicitly *not* FAILED) and freezes the leaderboard. |
| `/hell reset` | `@gamenight host` | Modal that requires typing `RESET WELCOME TO HELL` → wipes all event data for a brand new run. |

---

## How it works

```
hell/
├── models.py       # EventStatus, Milestone, ParticipantRef, LeaderboardEntry, EventState
├── timeutil.py     # absolute-timestamp helpers, "142h 38m", progress bars
├── config.py       # env/.env loading, role + channel IDs, tuning knobs
├── storage.py      # ← 7. persistence (SQLite, WAL, atomic milestone claims)
├── engine.py       # ← 1./3./4. event state machine, user time tracking, milestone detection
├── leaderboard.py  # ← 6. ranking, tie handling, Top-3 rendering
├── milestones.py   # milestone table + reward definitions
├── monitor.py      # ← 2. VC monitoring: 1s tick, clanker kicks, 10s progress edit
├── announcer.py    # ← 5. every message the bot posts
├── cog.py          # ← 8. /hell slash commands, confirmations, permission checks
└── bot.py          # entrypoint / wiring
```

`engine.py` and everything below it import **zero Discord code**. The monitor feeds it plain
`Observation(now, participants)` values (bots and `@clanker` already filtered out) and gets back
plain domain events (`MilestoneReached`, `EventFailed`, `EventCompleted`, `EventCancelled`)
that the announcer turns into messages. That's why the whole rulebook is unit-testable.

### The 1-second loop

1. Read the target VC members.
2. Drop bots.
3. `@clanker` → `member.move_to(None)` immediately (plus an instant `on_voice_state_update`
   fast path), and they never appear in the participant list.
4. The remaining humans are the valid participants; each gets credited for the observed interval.
5. `valid_human_count == 0` → **FAILED**, timer stopped permanently, leaderboard frozen and saved,
   announcement posted. The event can never resume automatically.

### The 10-second progress message

One message, created once and **edited** afterwards (its ID is persisted, so it keeps being edited
after a restart; if someone deletes it, it is recreated once):

```
🔥 WELCOME TO HELL — RUNNING
█████████░░░░░░░░░░░ 73h 24m / 160h 00m
45.9% complete
👥 Currently in Hell: 7
✅ Current milestone: 64h cleared
🔥 Next milestone: 96h (in 22h 36m)
⏳ 86h 36m remaining of the 160h challenge
```

### Milestones

Driven purely by the **global** timer (`now - start_ts`), never by individual user time.
Each one is claimed with an `INSERT OR IGNORE` in SQLite: only the writer that actually inserted
the row announces, so a milestone can never fire twice — including if the bot restarts in the very
second it lands. The announcement is marked `announced` only after Discord accepts the message; a
crash in between makes the bot re-post it on the next startup instead of losing it.

Each milestone records **who was in the VC at that exact tick** and the reached timestamp, and each
has its own distinct message:

| Milestone | Reward |
|---|---|
| 32h | `@hell` — limited |
| 64h | Limited-time Verity in [Find the Verities](https://www.roblox.com/games/138268356635577/Find-the-Verities) |
| 96h | `@hell-ist` — limited |
| 128h | Music permissions for everyone, provided they are not abused |
| 160h | `@hell master` — limited |

At **160h** the event also becomes `COMPLETED`: the timer stops (it never counts past 160h),
leaderboard accumulation stops, the final rankings are frozen and displayed, and the final Top 3
are announced as receiving **every milestone reward + `@cool people :D`**.

### Leaderboard

Per-user accumulated VC seconds, credited only while the event is `RUNNING`, sorted high → low,
Top 3 with 🥇🥈🥉 and the rest numbered below. Exact ties share a rank (`1, 1, 3`). Leaving and
returning continues accumulating rather than resetting. Frozen and saved on FAILED / COMPLETED /
CANCELLED.

---

## Edge cases (all covered by tests)

| Case | Behaviour |
|---|---|
| User joins exactly at a milestone | Included in that milestone's eligible snapshot |
| User leaves exactly at a milestone | Excluded from the snapshot |
| A bot joins the VC | Ignored everywhere; cannot keep the event alive |
| A `@clanker` joins | Disconnected immediately, no leaderboard time, cannot keep the event alive |
| Last valid participant leaves | Instant permanent `FAILED` + frozen leaderboard + announcement |
| Rapid join/leave churn | 1-second sampling keeps per-user totals correct |
| Bot restarts mid-event | State reloaded from SQLite; elapsed = `now - start_ts`; nothing resets |
| Bot restarts around a milestone | Atomic DB claim prevents duplicates; unsent announcements are re-posted on boot |
| 160h hits during a 10s update | The 1s tick clamps everything to `start_ts + 160h`; completion wins over an empty VC at the deadline |
| User leaves and returns | Totals continue accumulating |
| Identical total times | Shared rank, deterministic display order |
| Bot offline for a while | Timer keeps running (timestamps), the unobserved window is **not** credited to anyone and is reported in the progress message |

Two deliberate policy calls worth knowing:

* **Downtime does not fail the event** (the bot can't prove the VC emptied while it was blind), but
  nobody earns leaderboard time for that window, and the gap is shown in the progress/status output.
* If the VC is empty **in the same tick** a milestone would land, the failure wins — nobody was
  present to claim the reward.

---

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest                 # 63 tests, no Discord connection required
python tools/simulate.py         # dry-run a full 160h event and print every message
python tools/simulate.py --fail-at 40   # dry-run a run that dies after 40 hours
```

`tools/simulate.py` drives the real engine and the real message renderers offline, which is the
fastest way to review wording or verify a rule change end to end.
