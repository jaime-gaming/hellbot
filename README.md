# 🔥 Welcome to Hell — Discord event bot

A production-ready Discord bot that runs the **Welcome to Hell** event: keep at least one real
human in a single voice channel, **continuously, for 160 hours**. The moment that VC is empty of
valid humans, the run is dead.

* Target VC: `1539756705997652079` (configurable) · Duration: **160 consecutive hours**
* Milestones: **32h · 64h · 96h · 128h · 160h**, each with its own reward and announcement
* Started manually with `/hell start` by `@gamenight host`
* Bots never count · `@clanker` users are kicked from the VC on sight · AFK still counts
* **Random alive checks** every 1–6 h: reply `Yes` in 5 minutes or you are disconnected
* **15-second grace period** when the VC empties — a no-ping warning goes out, and the run only
  dies if nobody comes back
* Everyone gets a **personal stat card by DM** when the run ends
* **Live log stream** DM'd to the operator: joins, leaves, kicks, milestones, errors, in real time
* **All wording in one file** — [`Announcements.py`](Announcements.py) — reloadable without a restart
* Everything is timestamp-based and persisted in SQLite — **restarting the bot never resets the timer**
* Ships with a **desktop control panel** (no console) and a one-file **`.exe`** build

---

## Quick start

### Windows — double-click, no console

1. Download/clone this folder.
2. Double-click **`run_bot.bat`** (or `run_bot_silent.vbs` if you don't even want the setup window
   to flash). First run creates a virtual environment and installs the dependencies automatically.
3. The **control panel** opens. Fill in the Settings tab → **Save settings** → **Start bot**.

Prefer a single file to hand to someone else? Run **`build_exe.bat`** once and you get
`dist\WelcomeToHellBot.exe` — no Python required on the target machine, no console window, and it
keeps `.env`, `data\` and `logs\` next to itself.

For debugging with visible output there is **`run_bot_console.bat`**.

### Any OS — console

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # fill in the token, channel IDs and role IDs
python bot.py
```

### Docker

```bash
cp .env.example .env      # fill it in
docker compose up -d      # state lives in the hell-data volume
```

A systemd unit is in [`deploy/hellbot.service`](deploy/hellbot.service).

---

## The control panel

`launcher_main.py` (what the `.bat` and the `.exe` start) is a small Tk desktop app:

| Tab | What you get |
|---|---|
| **Dashboard** | Bot state, event state, live VC headcount, next milestone, a progress bar for the 160 h, and the result of the Discord-side configuration checks. |
| **Log** | The same lines that go to `logs/hellbot.log`, colour-coded and live, with a button to open the folder. |
| **Settings** | Every `.env` value with inline help, the token masked behind a *show* toggle, validation before saving, and an atomic write so a crash can't corrupt the file. |

The bot runs on its own asyncio loop in a background thread, so the window never freezes; closing
it asks for confirmation and shuts the bot down cleanly. Errors (bad token, missing intent, no
network) appear as pop-ups and on the dashboard instead of vanishing into a console nobody sees.

---

## Discord setup checklist

1. **Developer Portal → Bot → Privileged Gateway Intents**: enable **Server Members Intent**.
2. Invite the bot with these permissions:

| Permission | Why |
|---|---|
| View Channel + Connect on the target VC | to see who is inside |
| **Move Members** | to disconnect `@clanker` users |
| Send Messages / Embed Links / Read Message History in the announcement channel | announcements + editing the progress message |
| **Mention @everyone** | milestone / start / failure / completion pings |
| Manage Messages *(optional)* | lets the bot pin the live progress message |

The bot verifies all of this on startup (`hell/health.py`) and tells you exactly what is missing —
in the log, and in the launcher's Dashboard.

### Configuration

| Variable | Required | Meaning |
|---|---|---|
| `DISCORD_TOKEN` | ✅ | bot token |
| `GUILD_ID` | ✅ | server ID (slash commands sync to this guild instantly) |
| `VOICE_CHANNEL_ID` | ✅ (default `1539756705997652079`) | the Hell VC |
| `ANNOUNCE_CHANNEL_ID` | ✅ | text channel for announcements + the live progress message |
| `GAMENIGHT_HOST_ROLE_ID` | ✅ | `@gamenight host` — the only role allowed to start/stop/reset |
| `CLANKER_ROLE_ID` | ✅ | `@clanker` — auto-disconnected, never earns leaderboard time |
| `HELL_ROLE_ID`, `HELLIST_ROLE_ID`, `HELL_MASTER_ROLE_ID`, `COOL_PEOPLE_ROLE_ID` | optional | only used to render real role mentions in reward messages |
| `DATABASE_PATH` | optional | default `data/hell.sqlite3` |
| `MONITOR_INTERVAL` / `PROGRESS_INTERVAL` | optional | default `1` s / `10` s |
| `STARTUP_GRACE_SECONDS` | optional | default `15` — VC reads right after boot are observed but cannot fail the event (cold-cache guard) |
| `EMPTY_VC_GRACE_SECONDS` | optional | default `15` — how long the VC may be empty before the run fails |
| `SEND_FINAL_DMS` / `DM_DELAY_SECONDS` | optional | default `true` / `1` — end-of-event stat cards |
| `LOG_DM_ENABLED` | optional | default `true` — live log stream |
| `LOG_DM_USER_ID` | optional | default `984083829767675965` (Jaime Gaming) — who receives it |
| `LOG_DM_LEVEL` | optional | default `INFO` — `DEBUG`/`INFO`/`WARNING`/`ERROR` |
| `LOG_DM_FLUSH_SECONDS` | optional | default `3` — batching interval |
| `MAX_TICK_CREDIT_SECONDS` | optional | default `5` — cap on leaderboard credit per check, so downtime is never silently credited |
| `REQUIRE_OCCUPANTS_TO_START` | optional | default `true` — refuses to start into an empty VC |
| `HEARTBEAT_MINUTES` | optional | default `15` — proof-of-life line in the log |
| `ALIVE_CHECK_ENABLED` | optional | default `true` |
| `ALIVE_CHECK_MIN_HOURS` / `ALIVE_CHECK_MAX_HOURS` | optional | default `1` / `6` — the random window |
| `ALIVE_CHECK_TIMEOUT_MINUTES` | optional | default `5` — time to answer |
| `ALIVE_CHECK_STRICT` | optional | default `false` — `true` accepts only the exact string `Yes` |
| `ALIVE_CHECK_CHANNEL_ID` | optional | where the roll call is posted; empty = the **VC's own text chat** |
| `LOG_LEVEL` | optional | default `INFO` |

> **Rewards are announcement-only.** The bot never assigns roles; it posts exactly who is eligible
> at each milestone so a human can hand them out.

---

## Commands

| Command | Who | What |
|---|---|---|
| `/hell start` | `@gamenight host` | Starts the event: status → `RUNNING`, records the absolute start timestamp, starts the 160 h timer, begins VC monitoring + per-user tracking, posts the start announcement. Rejected if one is already running or the VC is empty. |
| `/hell status` | everyone | Status, elapsed, remaining, % complete, progress bar, live VC headcount, current + next milestone, and the milestones already reached. |
| `/hell leaderboard` | everyone | Current (or frozen final) leaderboard: Top 3 on the podium, everyone else below. |
| `/hell alivecheck` | `@gamenight host` | Runs a roll call immediately instead of waiting for the random timer. |
| `/hell reloadmessages` | `@gamenight host` | Re-read `Announcements.py` so edited wording applies immediately. |
| `/hell logs` | `@gamenight host` | Control the live log stream: `status`, `on`, `off`, `test`, `flush`, and the minimum severity. |
| `/hell mystats` | everyone | Your own stat card (time survived, rank, rewards) — handy if your DMs are closed. |
| `/hell milestones` | everyone | All five milestones, their rewards, when each was reached and how many users were eligible. |
| `/hell stop` | `@gamenight host` | Button confirmation → marks the event **CANCELLED** (explicitly *not* FAILED) and freezes the leaderboard. |
| `/hell reset` | `@gamenight host` | Modal requiring the exact phrase `RESET WELCOME TO HELL` → wipes all event data for a fresh run. |

All output is embeds. Mentions inside an embed never ping, so a milestone can list 250 eligible
users without 250 notifications — while the `@everyone` ping stays in the message content.

---

## Changing what the bot says — `Announcements.py`

Every message, title, footer, reward, emoji and colour lives in one file at the root of the
project: **[`Announcements.py`](Announcements.py)**. No logic, just text.

```python
    {
        "hours": 32,
        "title": "🔥 32 HOURS SURVIVED",
        "blurb": "Welcome to Hell has reached the first milestone.",
        "flavour": "The first gate is behind you. 128 hours to go — the easy part is over.",
        "reward": "@hell (limited)",
        "short_reward": "@hell",
    },
```

It is organised in numbered sections — milestones, start, live progress, grace period, failure,
cancellation, completion, leaderboard, alive checks, stat cards, command replies, colours — and
each block lists the `{placeholders}` it accepts.

* **Edit the text between the quotes**, keep the `{placeholders}` you want, save.
* Run **`/hell reloadmessages`** (host only) and the new wording is live — no restart, no risk to a
  running 160-hour event.
* If your edit has a syntax error or a missing name, the bot **keeps the previously loaded text**
  and tells you exactly what broke (`hell/texts.py`). It never crashes on bad copy.
* An unknown `{placeholder}` degrades to the raw template and is logged, rather than killing the
  announcement it belongs to.
* `python tools/simulate.py` prints every message offline, so you can proofread before going live.
* In a frozen build the file is copied next to `WelcomeToHellBot.exe` and read from there, so
  wording can be changed without rebuilding.

## How it works

See **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** for the full module map, the
invariants each layer guarantees, and the database schema.

```
Announcements.py   every message the bot sends, in one editable file
hell/              the bot: pure core (engine, timelines, grace, leaderboard…)
                   + Discord edge (monitor, embeds, announcer, commands…)
launcher/          desktop control panel (config editor, supervisor, tkinter UI)
tools/             check.sh (lint+types+tests) and simulate.py (offline dry-run)
docs/              architecture notes
```

`engine.py` and everything below it import **zero Discord code**. The monitor feeds it plain
`Observation(now, participants)` values (bots and `@clanker` already filtered out) and gets back
plain domain events (`MilestoneReached`, `EventFailed`, `EventCompleted`, `EventCancelled`) that
the announcer turns into messages. That is why the entire rulebook is unit-testable.

### Two explicit timelines

The code keeps these two clocks in separate modules on purpose — mixing them up is the classic way
this kind of bot goes wrong.

| | Module | Range | Moved by | Stopped by |
|---|---|---|---|---|
| **Global event timeline** | [`hell/timeline.py`](hell/timeline.py) — `EventTimeline` | **0 → 160h** | nothing but the wall clock | the run ending (FAILED / COMPLETED / CANCELLED) |
| **Per-user session timeline** | [`hell/tracking.py`](hell/tracking.py) — `UserTimeTracker` | **0 → Xh** per person | that person being in the VC | that person leaving (their clock only) |

A user disconnecting, leaving, being alive-check-kicked or being removed as `@clanker` **never**
touches the global 0 → 160h progress, as long as at least one valid participant remains. Their own
0 → Xh clock simply pauses and resumes where it left off when they come back.

### The 1-second loop

1. Read the target VC members.
2. Drop bots.
3. `@clanker` → `member.move_to(None)` immediately (plus an instant `on_voice_state_update` fast
   path), and they never appear in the participant list.
4. The remaining humans are the valid participants; each one's own clock is credited for the
   observed interval.
5. `valid_human_count == 0` → the **grace period** opens (below). Only when it expires is the run
   FAILED, permanently, with the leaderboard frozen and saved.

### Empty-VC grace period ([`hell/grace.py`](hell/grace.py))

```
VC becomes empty ──► GRACE OPEN (15s) ──► somebody joins in time ──► run continues
                                     └──► nobody joins ──────────► run FAILED
```

* A warning embed goes to the progress/announcement channel **with pings explicitly disabled**
  (`AllowedMentions(everyone=False, users=False, roles=False)`) — it alerts whoever is already
  watching without waking the server.
* If a valid human joins before the deadline, a "✅ SAVED — THE RUN CONTINUES" notice is posted
  (also without pings) and the 160h clock — which never stopped — carries on.
* Nobody accrues per-user time during the window, and milestones are not triggered while the VC is
  empty (there would be nobody to claim them); a milestone that lands mid-window fires on the first
  tick with people back in.
* If the window expires the event fails **as of the moment the VC emptied**, not the moment the
  window ran out, so grace can never inflate the survived time.
* The window is persisted (`event.grace_started_ts`), so a restart mid-countdown resumes it.
* Length is `EMPTY_VC_GRACE_SECONDS` (default 15; `0` restores instant failure).

### Live log stream ([`hell/logsink.py`](hell/logsink.py))

Everything the bot logs is mirrored to the operator's DMs in real time, batched into code blocks:

```
23:57:12 • [monitor]    ➕ Alice joined the VC (3 valid human(s) inside)
23:57:14 • [monitor]    ➖ Bob left the VC (2 valid human(s) inside)
23:57:20 • [monitor]    Kicked @clanker Clank3r (555) from the VC
23:57:31 • [alivecheck] Alive check a1b2c3 started for 4 user(s); deadline in 300s
23:58:02 ⚠️ [engine]     VC is EMPTY — grace period of 15s started
23:58:17 ⚠️ [engine]     Event FAILED — VC empty since …, grace expired at …
23:58:19 ❌ [bot]        Unhandled exception in on_message
                        Traceback (most recent call last): …
```

* Recipient is `LOG_DM_USER_ID` (defaults to **984083829767675965**, Jaime Gaming).
* One pipeline: the same records go to `logs/hellbot.log`, the launcher's Log tab and the DM, so
  nothing can be visible in one place but missing in another.
* Rate-limit safe: lines are buffered and flushed every `LOG_DM_FLUSH_SECONDS` (3 s), at most three
  messages per flush; floods are summarised as `… N line(s) dropped` instead of spamming.
* Self-protecting: records from the stream itself and from discord.py's HTTP layer are excluded (no
  feedback loops), and if the operator's DMs are closed the stream disables itself and says so in
  the file log.
* Attached before the gateway connects, so startup problems (bad token, missing intent, failed
  preflight) are delivered as soon as the DM channel opens.
* `/hell logs` toggles it live, changes severity, or sends a test line.

### End-of-event stat cards ([`hell/reports.py`](hell/reports.py), [`hell/dm.py`](hell/dm.py))

When the run ends — completed, failed or cancelled — every contestant is DM'd their own card:

```
WELCOME TO HELL

128:42:15 SURVIVED

YOU WERE... TOP 3
out of 41 contestant(s)

YOU WON 2 REWARDS
• 32h — @hell (limited)
• 64h — Limited-time Verity in Find the Verities
```

The final Top 3 of a completed run get all five milestone rewards plus `@cool people :D` listed.
Delivery is resumable (every attempt is written to `dm_log`, so a restart never double-messages),
paced at one DM per second, and users with DMs closed are reported in the channel summary — they
can run `/hell mystats` to see the same card.

### The 10-second progress message

One embed, created once and **edited** afterwards (its ID is persisted, so it keeps being edited
after a restart; if someone deletes it, it is recreated). It shows status, `73h 24m / 160h 00m`,
percentage, a `█████████░░░░░░░░░░░` bar, live headcount, current milestone, next milestone with a
live countdown, and time remaining. Identical renders are skipped, and once the event ends the
final state is written once and the loop stops editing.

### Milestones

Driven purely by the **global** timer (`now - start_ts`), never by individual user time. Each is
claimed with an `INSERT OR IGNORE` in SQLite: only the writer that actually inserted the row
announces, so a milestone can never fire twice — including if the bot restarts in the very second
it lands. The `announced` flag is only set after Discord accepts the message, so a crash in
between re-posts it on the next startup instead of losing it.

| Milestone | Reward |
|---|---|
| 32h | `@hell` — limited |
| 64h | Limited-time Verity in [Find the Verities](https://www.roblox.com/games/138268356635577/Find-the-Verities) |
| 96h | `@hell-ist` — limited |
| 128h | Music permissions for everyone, provided they are not abused |
| 160h | `@hell master` — limited |

At **160 h** the event also becomes `COMPLETED`: the timer stops (it never counts past 160 h),
leaderboard accumulation stops, the rankings are frozen and displayed, and the final Top 3 are
announced as receiving **every milestone reward + `@cool people :D`**.

### Alive checks ("roll call")

At a **random interval between 1 and 6 hours**, while the event is running, the bot posts in the
VC chat (or `ALIVE_CHECK_CHANNEL_ID`):

```
@alice @bob @carol
🚨 ARE YOU ALIVE? Say: Yes
Reply with Yes in this channel within 5 minutes or you will be disconnected from the VC.
You keep all your leaderboard time and can rejoin immediately.
```

* Everyone **currently in the VC** is pinged — bots and `@clanker` users are never included.
* Each has **5 minutes** to reply `Yes` in that channel (case-insensitive by default; set
  `ALIVE_CHECK_STRICT=true` to demand the exact string). Counted answers get a ✅ reaction.
* Whoever stays silent is **disconnected from the VC**. Their accumulated leaderboard time is
  **not** touched and they may **rejoin immediately** — tracking resumes as normal.
* A disconnect never fails the event by itself; the run only ends if the VC is left with no valid
  humans at all (e.g. literally nobody answered).
* People who join *during* a check are not required to answer; people who already left are not
  chased.
* The pending check is persisted. If the bot restarts mid-check it resumes and even reads back
  answers posted while it was offline; if the 5 minutes expired during the downtime the check is
  **cancelled** — nobody is punished for the bot being away.
* The next check time is never announced (that would defeat the point); `/hell status` only says
  that checks happen randomly every 1–6 h.

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
| Last valid participant leaves | 15 s grace window + no-ping warning; `FAILED` only if nobody returns |
| Someone rejoins with 1 s to spare | Window closes, "SAVED" notice, run continues untouched |
| VC empties repeatedly | Each empty period gets its own window; survived time is never inflated |
| Milestone lands while the VC is empty | Held back, then awarded to whoever is present when the VC recovers (or lost with the run) |
| Restart mid-grace-window | Window resumes from the persisted `grace_started_ts` |
| Event ends while some DMs are unsent | `dm_log` resumes delivery on the next start, without duplicates |
| Contestant has DMs closed | Recorded as blocked, reported in the summary, `/hell mystats` still works |
| Slow Discord API during a roll call | Kicks/announcements run off the 1-second loop, so VC monitoring never stalls |
| A milestone edited into a broken shape | Rejected with a readable reason; the previous table stays live |
| Event length edited mid-run | Refused and logged — the running clock is never reshaped |
| No internet / bad token / missing intent | Clean one-line error and a distinct exit code, full traceback in `logs/` |
| Rapid join/leave churn | 1-second sampling keeps per-user totals correct |
| Bot restarts mid-event | State reloaded from SQLite; elapsed = `now - start_ts`; nothing resets |
| Bot restarts around a milestone | Atomic DB claim prevents duplicates; unsent announcements are re-posted on boot |
| 160 h hits during a 10 s update | The 1 s tick clamps everything to `start_ts + 160h`; completion wins over an empty VC at the deadline |
| User leaves and returns | Totals continue accumulating |
| Identical total times | Shared rank, deterministic display order |
| 250 people in the VC at a milestone | Message split across embed fields; never exceeds Discord's limits |
| Bot offline for a while | Timer keeps running (timestamps), the unobserved window is **not** credited to anyone and is reported in the progress message |
| Alive check + restart | Check state is persisted; replies sent while offline are recovered, and an expired check is cancelled instead of kicking people |
| Alive check ignored by everyone | Everyone is disconnected, the VC empties, and the normal failure rule ends the run |
| Someone joins mid-check | Not pinged, not required to answer, never kicked for it |
| Discord API hiccup on a message | Logged and retried on the next cycle; the event state is untouched |

Two deliberate policy calls worth knowing:

* **Downtime does not fail the event** (the bot cannot prove the VC emptied while it was blind),
  but nobody earns leaderboard time for that window, and the gap is shown in the progress/status
  output.
* If the VC is empty **in the same tick** a milestone would land, the failure wins — nobody was
  present to claim the reward.

---

## Development

```bash
pip install -r requirements-dev.txt

./tools/check.sh          # compile + pyflakes + ruff + mypy + 225 tests + simulations
./tools/check.sh --fast   # same, without the simulations
python -m pytest          # tests only — no Discord connection required

python tools/simulate.py              # print every message of a full 160h run, offline
python tools/simulate.py --fail-at 40 # …of a run that dies after 40 hours
```

Tooling lives in `pyproject.toml` (pytest, mypy and ruff are configured there).

`tools/simulate.py` drives the real engine and the real message renderers offline — the fastest way
to review wording or verify a rule change end to end. A ready-made GitHub Actions workflow (tests on Python
3.10–3.12, lint, and both simulation paths) is in `deploy/github-actions-ci.yml` — copy it to
`.github/workflows/ci.yml` to enable it.
