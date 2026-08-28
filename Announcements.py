"""
================================================================================
 Announcements.py — EVERY message the Welcome to Hell bot sends lives here
================================================================================

Edit this file to change wording, emojis, rewards or flavour text.  You never
need to touch the bot's logic: the code reads its text from here at render
time, so a change takes effect on the next message (or instantly with
`/hell reloadmessages`).

HOW TO EDIT SAFELY
------------------
1.  Only change the text **inside the quotes**.
2.  Keep every `{placeholder}` that is already in a line — they are filled in
    by the bot (each block lists the placeholders it supports).  You may delete
    a placeholder you do not want, or reuse one several times, but never invent
    a new name: an unknown `{name}` makes that message fail to render.
3.  A literal curly brace must be doubled: `{{like this}}`.
4.  Discord markdown works: `**bold**`, `*italics*`, `` `code` ``, `<#channel>`,
    `<@user>`, `<@&role>`.
5.  After editing, run `/hell reloadmessages` (needs `@gamenight host`) or just
    restart the bot.  If this file has a syntax error the bot keeps using the
    previously loaded text and tells you what is wrong — it will not crash.

Tip: `python tools/simulate.py` prints every message offline, so you can proof
read your changes without starting the bot.
================================================================================
"""

# =============================================================================
#  0. LOOK AND FEEL — branding, artwork and the shapes used in every message
# =============================================================================
#  Images are OFF by default: the messages are clean text, and nothing is
#  uploaded with every post. To use artwork, set a value below to either an
#  https:// URL (nothing is uploaded, recommended) or a file inside the project
#  such as "assets/hellbot.png" (uploaded with the message). Leave "" for none.

BRAND_NAME = "WELCOME TO HELL"
BRAND_TAGLINE = "160 hours · one voice channel · no gaps"
BRAND_ICON = ""                            # small icon beside the brand line
PROGRESS_THUMBNAIL = ""                    # beside the live progress card
MILESTONE_IMAGE = ""                       # big image under a milestone post
COMPLETION_IMAGE = ""                      # big image under the 160h post

# The progress bar is split into one segment per milestone, e.g.
#   ▰▰▰▰┃▰▰▱▱┃▱▱▱▱┃▱▱▱▱┃▱▱▱▱
BAR_FULL = "▰"
BAR_EMPTY = "▱"
BAR_SEPARATOR = "┃"
BAR_CELLS_PER_MILESTONE = 4

# Milestone tally shown on announcements, e.g. 🔥🔥🔥◦◦
DOT_REACHED = "🔥"
DOT_PENDING = "◦"

DIVIDER = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"


# =============================================================================
#  1. MILESTONES — the five checkpoints and their rewards
# =============================================================================
#  hours        : the mark on the 0 → 160h event timeline (do not change unless
#                 you really mean to change the event structure)
#  title        : headline of the milestone announcement
#  blurb        : one or two sentences under the headline
#  flavour      : italic closing line (optional, use "" for none)
#  reward       : full reward text shown in the announcement
#  short_reward : compact version used in lists and summaries
#  role_token   : the exact text to swap for a real @role mention (optional)
#  role_env     : which role ID in .env that mention comes from (optional)

VERITIES_URL = "https://www.roblox.com/games/138268356635577/Find-the-Verities"

MILESTONES = (
    {
        "hours": 32,
        "title": "🔥 32 HOURS SURVIVED",
        "blurb": "Welcome to Hell has reached the first milestone.",
        "flavour": "The first gate is behind you. 128 hours to go — the easy part is over.",
        "reward": "@hell (limited)",
        "short_reward": "@hell",
        "role_token": "@hell",
        "role_env": "HELL_ROLE_ID",
    },
    {
        "hours": 64,
        "title": "🔥🔥 64 HOURS — THE FIRE SPREADS",
        "blurb": "Two full days and change without the VC ever going quiet. "
                 "The second milestone belongs to you.",
        "flavour": "Two gates down. This VC has not been silent for a single second.",
        "reward": f"Limited-time Verity in **Find the Verities** — {VERITIES_URL}",
        "short_reward": "a limited Verity in Find the Verities",
    },
    {
        "hours": 96,
        "title": "🔥🔥🔥 96 HOURS — HALFWAY IS BEHIND YOU",
        "blurb": "Four straight days in Hell. The third milestone is complete and there is "
                 "no turning back now.",
        "flavour": "Three gates cleared. Quitting now would be a tragedy for everyone involved.",
        "reward": "@hell-ist (limited)",
        "short_reward": "@hell-ist",
        "role_token": "@hell-ist",
        "role_env": "HELLIST_ROLE_ID",
    },
    {
        "hours": 128,
        "title": "🔥🔥🔥🔥 128 HOURS — THE FINAL STRETCH",
        "blurb": "The fourth milestone has fallen. Only 32 hours stand between this VC and "
                 "immortality.",
        "flavour": "Four gates cleared. Thirty-two hours from history.",
        "reward": "Music permissions for everyone, provided they are not abused.",
        "short_reward": "music permissions",
    },
    {
        "hours": 160,
        "title": "🏆🔥 160 HOURS — WELCOME TO HELL",
        "blurb": "The full 160 consecutive hours have been survived. The final milestone is "
                 "complete.",
        "flavour": "There is nothing left to survive. Hell has been conquered.",
        "reward": "@hell master (limited)",
        "short_reward": "@hell master",
        "role_token": "@hell master",
        "role_env": "HELL_MASTER_ROLE_ID",
    },
)

# Extra reward for the final Top 3 of a completed run.
TOP3_BONUS_ROLE = "@cool people :D"


# =============================================================================
#  2. MILESTONE ANNOUNCEMENT  (posted with @everyone)
# =============================================================================
#  {reward} {members} {member_count} {reached_at} {reached_relative}
#  {hours} {remaining_hours}

MILESTONE_REWARD_FIELD = "🎁 Reward"
MILESTONE_CLAIM_FIELD = "⚠️ How to claim"
MILESTONE_CLAIM_TEXT = (
    "**Only the users listed below — the ones in the VC at this exact moment — "
    "can claim this reward.** The list is recorded and timestamped; joining afterwards "
    "does not count."
)
MILESTONE_ELIGIBLE_FIELD = "👥 Eligible ({member_count})"
MILESTONE_REACHED_FIELD = "🕛 Reached at"
MILESTONE_REACHED_TEXT = "{reached_at} ({reached_relative})"
MILESTONE_LATE_FIELD = "ℹ️ Note"
MILESTONE_LATE_TEXT = (
    "The bot was offline at the exact milestone second; this list is the first "
    "verified snapshot taken afterwards."
)
MILESTONE_TALLY = "{dots}  ·  milestone **{index} of {count}**"
MILESTONE_LEADERS_FIELD = "🏆 Most time in Hell so far"
MILESTONE_FOOTER = "{hours}h of 160h cleared · {remaining_hours}h to go"
MILESTONE_FOOTER_FINAL = "160h of 160h cleared · FINAL MILESTONE"
MILESTONE_NOBODY = "*nobody — the VC was empty*"


# =============================================================================
#  3. EVENT START  (posted with @everyone)
# =============================================================================
#  {host} {vc} {clanker_role} {started_at} {started_relative} {ends_at}
#  {ends_relative} {participant_count} {participants} {total_hours}

START_TITLE = "🔥 WELCOME TO HELL HAS STARTED 🔥"
START_DESCRIPTION = (
    "The gates are open. Starting **now**, {vc} must keep "
    "**at least one real human inside, continuously, for {total_hours} hours**.\n\n"
    "Started by {host} • {started_at} ({started_relative})"
)
START_RULES_FIELD = "📜 The rules"
START_RULES = (
    "• Bots never count.\n"
    "• {clanker_role} users are removed on sight and earn no time.\n"
    "• AFK still counts — you just have to *be there*.\n"
    "• **Random alive checks**: every 1–6 hours everyone in the VC gets pinged and has "
    "5 minutes to reply `Yes`. Miss it and you are disconnected — your leaderboard time "
    "stays and you can rejoin instantly.\n"
    "• If the VC empties, a {grace_seconds}s countdown starts. Nobody back in time = "
    "**FAILED**, forever."
)
START_MILESTONES_FIELD = "🏁 Milestones"
START_MILESTONE_LINE = "`{hours:>4}h`  {reward}"
START_PARTICIPANTS_FIELD = "👥 In Hell right now ({participant_count})"
START_NOBODY = "*nobody yet*"
START_FINISH_FIELD = "🕛 Finish line"
START_FINISH_TEXT = "{ends_at}\n({ends_relative})"
START_FOOTER = "Good luck. You are going to need it. · /hell status · /hell leaderboard"


# =============================================================================
#  4. LIVE PROGRESS MESSAGE  (edited on a timer, never pings)
# =============================================================================
#  {status} {emoji} {bar} {elapsed} {total} {percent} {participants}
#  {remaining} {current_milestone} {next_milestone} {time_to_next}
#  {next_relative} {started_at} {started_relative} {unverified}
#  {grace_left} {vc}

PROGRESS_TITLE = "{emoji} THE 160 HOUR CHALLENGE"
PROGRESS_DESCRIPTION = (
    "`{bar}`\n"
    "**{elapsed}** of {total}  ·  **{percent}**  ·  {dots}"
)
PROGRESS_STATUS_FIELD = "Status"
PROGRESS_STATUS_VALUE = "`{status}`"
PROGRESS_STATUS_VALUE_EMPTY_VC = "`{status}` ⚠️ **VC EMPTY**"
PROGRESS_STATUS_VALUE_PAUSED = "`{status}` ⏸️ **TIMER FROZEN** — nothing counts right now"
PROGRESS_PEOPLE_FIELD = "👥 Currently in Hell"
PROGRESS_PEOPLE_VALUE = "**{participants}**"
PROGRESS_REMAINING_FIELD = "⏳ Time remaining"
PROGRESS_REMAINING_VALUE = "**{remaining}**"
PROGRESS_CURRENT_FIELD = "✅ Current milestone"
PROGRESS_CURRENT_VALUE = "**{current_milestone}h** cleared"
PROGRESS_CURRENT_NONE = "*none yet*"
PROGRESS_NEXT_FIELD = "🔥 Next milestone"
PROGRESS_NEXT_VALUE = "**{next_milestone}h**\nin {time_to_next}"
PROGRESS_NEXT_VALUE_RUNNING = "**{next_milestone}h**\nin {time_to_next}\n({next_relative})"
PROGRESS_NEXT_NONE = "*all milestones cleared*"
PROGRESS_STARTED_FIELD = "🕛 Started"
PROGRESS_STARTED_VALUE = "{started_at}\n{started_relative}"
PROGRESS_FOOTER_LIVE = "Live · updates every {interval}s · /hell status · /hell leaderboard"
PROGRESS_FOOTER_FINAL = "Final state · this message is no longer updating"

PROGRESS_REMAINING_BLIND = "👁️ *[HIDDEN BY BLINDNESS]*"
PROGRESS_NEXT_BLIND = "👁️ *[HIDDEN BY BLINDNESS]*"
PROGRESS_TITLE_FINAL_HOUR = "👹 THE FINAL HOUR"
PROGRESS_TITLE_COUNTDOWN = "👹 FINAL COUNTDOWN"

PROGRESS_FAILED_FIELD = "💀 FAILED"
PROGRESS_FAILED_DEFAULT = "The VC became empty of valid participants."
PROGRESS_COMPLETED_FIELD = "🏆 COMPLETED"
PROGRESS_COMPLETED_TEXT = "160 consecutive hours survived. Welcome to Hell has been completed."
PROGRESS_CANCELLED_FIELD = "🛑 CANCELLED"
PROGRESS_CANCELLED_DEFAULT = "Manually stopped by a host."
PROGRESS_GRACE_FIELD = "⚠️ THE VC IS EMPTY"
PROGRESS_GRACE_TEXT = "**{grace_left}s** left to get somebody back in {vc} or the run is over."
PROGRESS_UNVERIFIED_FIELD = "⚠️ Unobserved window"
PROGRESS_UNVERIFIED_TEXT = (
    "{unverified} of this run could not be watched (bot offline). "
    "The timer kept running; nobody was credited for that window."
)


# =============================================================================
#  5. EMPTY-VC GRACE PERIOD  (warning + recovery, both posted WITHOUT pings)
# =============================================================================
#  {vc} {seconds} {deadline_at} {deadline_relative} {elapsed}
#  {empty_for} {participants} {participant_count}

GRACE_WARNING_TITLE = "⚠️ THE VC IS EMPTY — THE RUN IS ABOUT TO DIE"
GRACE_WARNING_DESCRIPTION = (
    "{vc} has **no valid humans** in it.\n"
    "Somebody has **{seconds} seconds** to join or **Welcome to Hell fails permanently**."
)
GRACE_WARNING_DEADLINE_FIELD = "⏳ Deadline"
GRACE_WARNING_DEADLINE_TEXT = "{deadline_at} ({deadline_relative})"
GRACE_WARNING_CLOCK_FIELD = "⏱️ On the clock"
GRACE_WARNING_CLOCK_TEXT = "**{elapsed}** / 160h"
GRACE_WARNING_FOOTER = "No pings on purpose — if you are reading this, get in the VC."

GRACE_RECOVERED_TITLE = "✅ SAVED — THE RUN CONTINUES"
GRACE_RECOVERED_DESCRIPTION = (
    "The VC was empty for **{empty_for}s** and somebody made it back in time. "
    "The 160h clock never stopped."
)
GRACE_RECOVERED_FIELD = "👥 Back in Hell ({participant_count})"
GRACE_RECOVERED_NOBODY = "*nobody*"
GRACE_RECOVERED_FOOTER = "That was close."


# =============================================================================
#  6. FAILURE  (posted with @everyone)
# =============================================================================
#  {vc} {survived} {percent} {failed_at} {milestones}

FAILURE_TITLE = "💀 WELCOME TO HELL — CHALLENGE FAILED"
FAILURE_DESCRIPTION = (
    "{vc} was **completely empty of valid participants** for the entire grace period, "
    "so nobody came back in time.\n"
    "The timer has stopped. A host can resume this run with `/hell resume` (operator approval required)."
)
FAILURE_SURVIVED_FIELD = "⏱️ Survived"
FAILURE_SURVIVED_TEXT = "**{survived}** of 160h"
FAILURE_PROGRESS_FIELD = "📉 Progress"
FAILURE_PROGRESS_TEXT = "**{percent}**"
FAILURE_WHEN_FIELD = "🕛 Failed at"
FAILURE_MILESTONES_FIELD = "🏁 Milestones secured"
FAILURE_MILESTONES_NONE = "**none**"
FAILURE_NEAR_MISS_FIELD = "😤 So close"
FAILURE_NEAR_MISS = "You were **{time_to_next}** away from the **{next_milestone}h** milestone."
FAILURE_TOP_FIELD = "🥇 Longest in Hell"
FAILURE_FOOTER = "Rewards already earned at reached milestones still stand. Reset with /hell reset."
FAILURE_LEADERBOARD_TITLE = "🏆 FINAL LEADERBOARD (frozen)"


# =============================================================================
#  7. CANCELLED  (manual stop by a host — no ping)
# =============================================================================
#  {who} {elapsed} {cancelled_at}

CANCELLED_TITLE = "🛑 WELCOME TO HELL — CANCELLED"
CANCELLED_DESCRIPTION = (
    "The event was manually stopped by {who}.\n"
    "This is a **cancellation, not a failure** — the VC never emptied."
)
CANCELLED_CLOCK_FIELD = "⏱️ Time on the clock"
CANCELLED_CLOCK_TEXT = "**{elapsed}** of 160h"
CANCELLED_WHEN_FIELD = "🕛 Stopped at"
CANCELLED_FOOTER = "A host can begin a fresh run with /hell reset followed by /hell start."
CANCELLED_LEADERBOARD_TITLE = "🏆 LEADERBOARD (frozen)"


# =============================================================================
#  8. COMPLETION — 160 hours  (posted with @everyone)
# =============================================================================
#  {vc} {completed_at} {final_reward} {all_rewards} {bonus_role} {podium}

COMPLETION_TITLE = "🏆🔥 WELCOME TO HELL HAS BEEN COMPLETED 🔥🏆"
COMPLETION_DESCRIPTION = (
    "**160 consecutive hours.**\n"
    "{vc} never emptied — not for one single second.\n\n"
    "Completed {completed_at}."
)
COMPLETION_REWARD_FIELD = "🎁 160h reward"
COMPLETION_REWARD_TEXT = "{final_reward}\n*for everyone who was in the VC at the 160h mark*"
COMPLETION_TOP3_FIELD = "🥇 Special Top 3 reward"
COMPLETION_TOP3_TEXT = (
    "The final Top 3 receive **every milestone reward** — {all_rewards} — "
    "**plus {bonus_role}**."
)
COMPLETION_PODIUM_FIELD = "🏅 The Top 3"
COMPLETION_PODIUM_NONE = "*No ranked participants.*"
COMPLETION_FOOTER = "The leaderboard below is final and frozen. Well done, all of you."
COMPLETION_LEADERBOARD_TITLE = "🏆 FINAL RANKINGS (frozen)"


# =============================================================================
#  9. LEADERBOARD
# =============================================================================
#  {total} {title} — entry lines use {medal} {who} {time}

LEADERBOARD_TITLE = "🏆 WELCOME TO HELL — LEADERBOARD"
LEADERBOARD_TITLE_FINAL = "🏆 WELCOME TO HELL — FINAL LEADERBOARD"
LEADERBOARD_EMPTY = "*Nobody has spent time in Hell yet.*"
LEADERBOARD_NO_PODIUM = "*No podium yet.*"
LEADERBOARD_REST_TITLE = "Everyone else"
LEADERBOARD_REST_TITLE_CONT = "Everyone else (cont.)"
LEADERBOARD_FOOTER = "{total} participant(s) · {tracked} tracked in total · time counts only while the event runs"
LEADERBOARD_FROZEN_FOOTER = "These rankings are frozen; the event is over."
LEADERBOARD_MORE = "and {hidden} more participant(s)"
LEADERBOARD_ENTRY = "{medal}  {who}  ·  `{time}`"
LEADERBOARD_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}
LEADERBOARD_RANK = "`#{rank}`"        # used from 4th place down


# =============================================================================
# 10. ALIVE CHECKS ("roll call")
# =============================================================================
#  {minutes} {answered} {total} {kicked} {left_early} {reason}
#
#  ALIVE_CHECK_TEXT is the exact headline that gets pinged — keep it short.

ALIVE_CHECK_TEXT = "🚨 ARE YOU ALIVE? Say: Yes"
ALIVE_CHECK_INSTRUCTIONS = (
    "*Reply with* `Yes` *(or `yeah`, `yep`, `yup`, `si`, `sure`, `okay`, "
    "`i'm alive`, …)* *in this channel within {minutes} minutes or you will be "
    "disconnected from the VC. You keep all your leaderboard time and can rejoin "
    "immediately.*\n\n"
    "*If you answer* `No`, *you will be kicked from the voice channel.*"
)
ALIVE_CHECK_RESULT_TITLE = "🚨 **Alive check finished**"
ALIVE_CHECK_RESULT_ANSWERED = "✅ Answered: **{answered}**"
ALIVE_CHECK_RESULT_KICKED = "❌ Disconnected for not answering: {kicked}"
ALIVE_CHECK_RESULT_KICKED_NOTE = (
    "*Their leaderboard time is untouched — rejoin whenever you like and tracking resumes.*"
)
ALIVE_CHECK_RESULT_NOBODY_KICKED = "❌ Disconnected: **nobody** — everyone answered in time."
ALIVE_CHECK_RESULT_LEFT_EARLY = "↩️ Already out of the VC: {left_early}"
ALIVE_CHECK_NO_REPLY = "Alright then"
ALIVE_CHECK_REJOIN_DM = "Psssst, you don't have to do the Alive Check."
ALIVE_CHECK_CANCELLED = (
    "🚨 **Alive check cancelled** — {reason}. Nobody was disconnected."
)
ALIVE_CHECK_CANCELLED_DEFAULT_REASON = "the bot was restarted while it was running"
ALIVE_CHECK_STATUS_RUNNING = "🚨 Alive check running — **{answered}/{total}** answered, {left}s left"
ALIVE_CHECK_STATUS_IDLE = (
    "🚨 Alive checks: random, every **{min_hours:g}–{max_hours:g}h** — reply `Yes` "
    "(or `yeah`, `yep`, `si`, …) within {minutes} min"
)

# --- Dead checks (introduced in Difficulty 2+) ------------------------------
DEAD_CHECK_TEXT = "💀 ARE YOU DEAD? DO NOT REPLY!"
DEAD_CHECK_INSTRUCTIONS = (
    "*This is a* **DEAD CHECK**! *DO NOT reply with* `Yes` *or anything else. "
    "Anyone who replies will be muted from the server for {mute_duration}.*"
)
DEAD_CHECK_RESULT_TITLE = "💀 **Dead check finished**"
DEAD_CHECK_RESULT_TRAPPED = "🔇 Muted for replying: {trapped} ({mute_duration})"
DEAD_CHECK_RESULT_NOBODY_TRAPPED = "✅ **Nobody fell for the dead check.** Everyone stayed silent."
DEAD_CHECK_STATUS_RUNNING = "💀 Dead check running — {left}s left (DO NOT REPLY)"


# =============================================================================
# 11. END-OF-EVENT STAT CARD  (DM'd to every contestant)
# =============================================================================
#  {survived} {rank} {participants} {reward_count} {reward_plural}
#  {rewards} {outcome} {event_clock}

CARD_EMBED_TITLE = "🔥 WELCOME TO HELL"
CARD_EMBED_FOOTER = "Thanks for surviving with us. See you in the next one."
CARD_EMBED_FOOTER_RUNNING = "Event in progress · Keep surviving to climb the ranks."
CARD_BODY = (
    "**WELCOME TO HELL**\n"
    "\n"
    "**{survived} SURVIVED**\n"
    "\n"
    "**YOU WERE... TOP {rank}**\n"
    "*out of {participants} contestant(s)*\n"
    "\n"
    "**YOU WON {reward_count} REWARD{reward_plural}**"
)
CARD_BODY_RUNNING = (
    "**WELCOME TO HELL**\n"
    "\n"
    "**{survived} SURVIVED SO FAR**\n"
    "\n"
    "**CURRENT RANK: TOP {rank}**\n"
    "*out of {participants} contestant(s)*\n"
    "\n"
    "**YOU HAVE CLAIMED {reward_count} REWARD{reward_plural} SO FAR**"
)
CARD_REWARD_LINE = "• {reward}"
CARD_REWARD_TOP3_NOTE = "*Top 3: every milestone reward is yours.*"
CARD_REWARD_NOTE = "*Claimable because you were in the VC when the milestone hit.*"
CARD_REWARD_NOTE_RUNNING = "*Milestone reward(s) secured by being in the VC when milestones were reached.*"
CARD_NO_REWARDS = "*You were not in the VC at any milestone moment — no rewards this time.*"
CARD_NO_REWARDS_RUNNING = "*No milestone rewards unlocked yet — be in the VC when the next milestone hits!*"
CARD_TOP3_BONUS_LINE = "Top 3 bonus — {bonus_role}"
CARD_MILESTONE_LINE = "{hours}h — {reward}"
CARD_OUTCOME = {
    "RUNNING": "The challenge is **IN PROGRESS** — keep surviving in the VC!",
    "COMPLETED": "The challenge was **COMPLETED** — 160 consecutive hours.",
    "FAILED": "The challenge **FAILED** — the VC emptied before 160 hours.",
    "CANCELLED": "The challenge was **CANCELLED** by a host.",
}
CARD_OUTCOME_DEFAULT = "The event has ended."
CARD_EVENT_CLOCK = "Event clock: **{event_clock}** of 160:00:00."

DM_SUMMARY_TITLE = "📬 Stat cards delivered"
DM_SUMMARY_TEXT = "Sent **{sent}** personal summaries to the contestants of this run."
DM_SUMMARY_BLOCKED_FIELD = "DMs closed"
DM_SUMMARY_BLOCKED_TEXT = (
    "**{blocked}** contestant(s) could not be messaged. They can run `/hell mystats` "
    "to see their card."
)
DM_SUMMARY_FAILED_FIELD = "Failed"
DM_SUMMARY_FAILED_TEXT = "**{failed}** (see the logs)"


# =============================================================================
#  11b. LIVE LOG ALERT PING  (DM'd to the operator when something goes wrong)
# =============================================================================
#  {mention} {title} {alerts}
#  Sent when a log line is at LOG_DM_PING_LEVEL or worse, or Discord reports a
#  rate limit. The {mention} is the reason the ping works — keep it in the body.

LOG_ALERT_TITLE = "⚠️ Welcome to Hell — something went wrong"
LOG_ALERT_BODY = (
    "{mention}\n\n"
    "**{title}**\n"
    "```ansi\n{alerts}\n```\n"
    "*The full stream continues below — this ping is just the alarm bell.*"
)


# =============================================================================
# 12. COMMAND REPLIES  (only the person running the command sees these)
# =============================================================================
#  {vc} {announce_channel} {host_role} {elapsed} {total} {status} {started_at}
#  {minutes} {check_channel} {previous_status}

# `/hell status` and `/hell leaderboard` (and their `!` twins) reply with these
# links when they are used in the VC text chat: Discord renders the linked
# message as a preview, so the pinned live cards are shown without dumping a
# second copy into the VC.  Point them at the pinned messages of your server.
CMD_STATUS_VC_LINK = (
    "https://discord.com/channels/1539201707026939934/1539784904630468699/1541386927113117751"
)
CMD_LEADERBOARD_VC_LINK = (
    "https://discord.com/channels/1539201707026939934/1539784904630468699/1541807282155954247"
)

CMD_ALREADY_RUNNING = (
    "❌ **Welcome to Hell is already RUNNING** — {elapsed} on the clock. "
    "Use `/hell status`, or `/hell stop` to cancel it first."
)
CMD_VC_UNREACHABLE = (
    "❌ I cannot see the target voice channel ({vc}). "
    "Check the ID and my permissions, then try again."
)
CMD_VC_EMPTY_ON_START = (
    "❌ {vc} has **no valid humans** in it. "
    "The event would fail on its very first check — get someone in there first."
)
CMD_STARTED = (
    "🔥 **Welcome to Hell has started.** Timer running since {started_at}; "
    "target: {total} of continuous presence in {vc}. "
    "Announcements go to {announce_channel}."
)
CMD_IDLE_TITLE = "💤 Welcome to Hell is not running"
CMD_IDLE_TEXT = (
    "No event has been started yet.\n"
    "A {host_role} can start one with `/hell start`.\n\n"
    "Target VC: {vc} • Duration: **160 hours**"
)
CMD_PAUSE_DONE = (
    "⏸️ **Event paused.** The 160h timer and every contestant's clock are frozen — "
    "no milestones can fire and nothing can fail while paused. Use `/hell resume` to continue; "
    "the paused time is never counted."
)
CMD_RESUME_DONE = "▶️ **Event resumed.** Timers are running again from exactly where they stopped."
CMD_STOP_NOTHING = "❌ Nothing to stop — current status is `{status}`."
CMD_STOP_DONE = "🛑 Event cancelled and leaderboard frozen."
CMD_RESET_DONE = (
    "♻️ **Event data reset.** Previous status was `{previous_status}`. "
    "All timers, leaderboard entries and milestones are gone — `/hell start` begins a fresh run."
)
CMD_NOT_A_HOST = "⛔ You are not a `@gamenight host`."
CMD_NOT_ALLOWED = "⛔ You cannot use this command. ({host_role} only)"
CMD_ERROR = "💥 Something went wrong running that command. The event state is untouched."
CMD_DM_ONLY = "⛔ This command can only be used in a DM to the bot, not in a server channel."
CMD_OPERATOR_ONLY = "⛔ Only the bot operator can run this command."

# --- approval codes for dangerous commands ------------------------------
DANGER_CODE_TITLE = "🔐 Welcome to Hell — approval code"
DANGER_CODE_BODY = (
    "**{action}** needs your approval.\n\n"
    "**Code:** `{code}`\n\n"
    "Enter it with `/hell approve`. The code expires in **{expires} minutes** and works once.\n\n"
    "Requested by **{requester}**."
)
DANGER_ACTION_STOP = "Stopping the event"
DANGER_ACTION_RESET = "Resetting all event data"
DANGER_ACTION_RESUME = "Resuming the failed event"
CMD_APPROVAL_REQUESTED = (
    "⏳ **Approval required.** A one-time code was sent to the operator's DMs. "
    "Run `/hell approve` and enter the code to proceed."
)
CMD_APPROVE_NOTHING_PENDING = "✅ Nothing is waiting for approval."
CMD_APPROVE_EXPIRED = "⌛ The approval code for **{action}** has expired. Run the command again to get a fresh code."
CMD_APPROVE_STALE_EVENT = (
    "🔄 The event changed since this code was issued — it would act on a different event. "
    "The code was invalidated. Run the command again to get a fresh one."
)
CMD_APPROVE_FAILED = "❌ Approval failed — {error}"
CMD_APPROVAL_UNAVAILABLE = "❌ Could not send the approval code to the operator (`{error}`). Nothing was changed — try again."

CMD_ALIVECHECK_NO_EVENT = "❌ No event is running (status `{status}`)."
CMD_ALIVECHECK_DISABLED = "❌ Alive checks are disabled (`ALIVE_CHECK_ENABLED=false`)."
CMD_ALIVECHECK_ALREADY = "❌ An alive check is already running."
CMD_ALIVECHECK_PAUSED = "⏸️ Cannot start an alive check while the event is paused — resume it first."
CMD_ALIVECHECK_FAILED = (
    "❌ Could not start it — the VC is empty or the check channel is unreachable."
)
CMD_ALIVECHECK_STARTED = (
    "🚨 Alive check posted in {check_channel}. "
    "Everyone in the VC has {minutes} minutes to reply `Yes`."
)
CMD_DEADCHECK_STARTED = (
    "💀 Dead check posted in {check_channel}. "
    "Anyone who replies within {minutes} minutes will be muted for {mute_duration}."
)

CMD_DIFFICULTY_TITLE = "⚡ WELCOME TO HELL — DIFFICULTIES"
CMD_DIFFICULTY_DESCRIPTION = (
    "Difficulties make the challenge progressively harder at each milestone reached."
)
CMD_DIFFICULTY_CURRENT = "⚡ **Current Difficulty:** Level {level} ({name})\n• {description}"
DIFFICULTY_ANNOUNCE_TITLE = "⚡ DIFFICULTY UPDATE — LEVEL {level} ({name})"
CMD_SETDIFFICULTY_DONE = "⚡ Difficulty set to **Level {level} ({name})**.\n• {description}"
CMD_SETDIFFICULTY_AUTO = "⚡ Difficulty override cleared — difficulty is now managed **automatically** based on event progress (currently **Level {level}: {name}**)."
CMD_ANNOUNCE_DIFFICULTY_DONE = "📢 Difficulty announcement posted to {channel}."

# Hell Events
HELL_EVENT_TITLE = "⚡ HELL EVENT — {name}"
HELL_EVENT_DOUBLE_TIME_START = (
    "🔥 **HELL EVENT — DOUBLE TIME**\n\n"
    "For the next **{duration} minutes**, your personal leaderboard time is being multiplied by **{multiplier}x**."
)
HELL_EVENT_DOUBLE_TIME_END = "🔥 **DOUBLE TIME HAS ENDED**\n\nHell is no longer feeling generous."
HELL_EVENT_OVERDRIVE_START = (
    "⚡ **HELL EVENT — OVERDRIVE**\n\n"
    "For the next **{duration} minutes**, your personal leaderboard time is being multiplied by **{multiplier}x**."
)
HELL_EVENT_OVERDRIVE_END = "⚡ **OVERDRIVE HAS ENDED**\n\nThe surge fades — time flows normally again."
HELL_EVENT_BLOOD_PACT_START = (
    "🩸 **HELL EVENT — BLOOD PACT**\n\n"
    "Everyone currently in Hell ({count} participants) has been granted **+{bonus}** of personal survival time."
)
HELL_EVENT_INFERNO_START = (
    "🔥 **HELL EVENT — INFERNO**\n\n"
    "Hell is getting hotter.\n"
    "Alive and Dead Checks will occur more frequently for the next **{duration} minutes**."
)
HELL_EVENT_INFERNO_END = "🔥 **INFERNO HAS SUBSIDED**\n\nThe heat recedes. Check frequency has returned to normal."
HELL_EVENT_EMBER_RAIN_START = (
    "🌧️ **HELL EVENT — EMBER RAIN**\n\n"
    "Burning embers drift down from above.\n"
    "Roll calls will fall every **8–15 minutes** for the next **{duration} minutes**."
)
HELL_EVENT_EMBER_RAIN_END = (
    "🌧️ **EMBER RAIN HAS PASSED**\n\nThe embers die out. Roll call frequency has returned to normal."
)
HELL_EVENT_BLINDNESS_START = (
    "👁️ **HELL EVENT — BLINDNESS**\n\n"
    "For the next **{duration} minutes**, Hell will hide your remaining time."
)
HELL_EVENT_BLINDNESS_END = "👁️ **BLINDNESS HAS ENDED**\n\nYou can see the remaining time again."
HELL_EVENT_JACKPOT_START = (
    "🎰 **HELL EVENT — JACKPOT**\n\n"
    "For the next **{duration} minutes**, gambling rewards are increased."
)
HELL_EVENT_JACKPOT_END = "🎰 **JACKPOT HAS ENDED**\n\nGambling rewards have returned to normal."
HELL_EVENT_FORTUNES_WHEEL_START = (
    "🎡 **HELL EVENT — FORTUNE'S WHEEL**\n\n"
    "The wheel is spinning in your favour. For the next **{duration} minutes**, gambling rewards are increased."
)
HELL_EVENT_FORTUNES_WHEEL_END = "🎡 **FORTUNE'S WHEEL HAS STOPPED**\n\nGambling rewards have returned to normal."
HELL_EVENT_TIME_VORTEX_START = (
    "🌀 **HELL EVENT — TIME VORTEX**\n\n"
    "For the next **{duration} minutes**, your personal leaderboard time is running at **{multiplier}x speed**.\n"
    "Every second in Hell now counts for less."
)
HELL_EVENT_TIME_VORTEX_END = "🌀 **TIME VORTEX HAS CLOSED**\n\nTime flows normally again. Every second counts once more."
HELL_EVENT_GOLDEN_HOUR_START = (
    "😇 **HELL EVENT — GOLDEN HOUR**\n\n"
    "Hell looks away for a moment. Your next roll call has been postponed by **{delay}**.\n"
    "Breathe. You have earned it."
)
HELL_EVENT_SOUL_CACHE_START = (
    "💎 **HELL EVENT — SOUL CACHE**\n\n"
    "A hidden cache of stolen time has surfaced — and **{who}** found it first.\n"
    "**+{bonus}** of personal survival time, on the house."
)
HELL_EVENT_BLOOD_DEBT_START = (
    "📉 **HELL EVENT — BLOOD DEBT**\n\n"
    "The tax collectors of Hell have come knocking. Everyone currently in Hell ({count} participants) "
    "has been charged **-{penalty}** of personal survival time."
)
HELL_EVENT_CULLING_START = (
    "⚔️ **HELL EVENT — THE CULLING**\n\n"
    "Hell demands proof of life **right now**.\n"
    "An immediate roll call has been triggered: reply **Yes** in time or be disconnected from the VC."
)
HELL_EVENT_SECRET_TITLE = "🕯️ SECRET HELL EVENT — ???"
HELL_EVENT_SECRET_FOOTER = "Its nature stays hidden until it ends"
HELL_EVENT_SECRET_START = (
    "🕯️ **A SECRET HELL EVENT HAS BEGUN**\n\n"
    "Something has changed deep within Hell…\n"
    "What exactly? **Nobody knows — yet.**\n\n"
    "The veil lifts when the event ends. Stay alert."
)
HELL_EVENT_SECRET_END_TITLE = "🕯️ SECRET HELL EVENT REVEALED — {name}"
HELL_EVENT_SECRET_REVEAL = (
    "🕯️ **THE SECRET EVENT IS REVEALED: {name}**\n\n"
    "The veil lifts — all along, it was **{name}**.\n\n"
    "{description}"
)
CMD_HELLEVENTS_TITLE = "⚡ HELL EVENTS"
CMD_HELLEVENTS_STATUS_NONE = "*No Hell Event is currently active.*"
CMD_HELLEVENTS_ACTIVE_SECRET = "🔮 ACTIVE: ??? (Secret Event)"
CMD_HELLEVENTS_TRIGGERED = "⚡ Triggered Hell Event: **{name}**."
CMD_HELLEVENTS_TRIGGERED_SECRET = (
    "🕯️ Triggered a **secret** Hell Event — its nature stays hidden until it ends."
)
CMD_HELLEVENTS_LOCKED = (
    "🔒 **{name}** is locked — it unlocks at **Difficulty {level} ({tier})**, "
    "{hours}h into the run. Current difficulty: **{current_level} ({current_name})**."
)

# Finale System
FINALE_FINAL_HOUR_TITLE = "👹 THE FINAL HOUR"
FINALE_FINAL_HOUR_ANNOUNCE = (
    "👹 **THE FINAL HOUR HAS BEGUN**\n\n"
    "**1 HOUR REMAINING**\n"
    "DO NOT LET HELL GO EMPTY."
)
FINALE_30M_TITLE = "⚠️ 30 MINUTES REMAIN"
FINALE_30M_ANNOUNCE = "⚠️ **30 MINUTES REMAIN**\n\nHell is crumbling. Keep the voice channel alive!"
FINALE_10M_TITLE = "🚨 10 MINUTES REMAIN"
FINALE_10M_ANNOUNCE = "🚨 **10 MINUTES REMAIN**\n\n**HELL IS ALMOST CONQUERED.**"
FINALE_5M_TITLE = "🔥 5 MINUTES REMAIN"
FINALE_5M_ANNOUNCE = "🔥 **5 MINUTES REMAIN**\n\nThe end of Hell is in sight. Stay in the VC!"
COMPLETION_FINALE_TITLE = "👹 WELCOME TO HELL HAS BEEN COMPLETED"
COMPLETION_FINALE_DESCRIPTION = (
    "**160:00:00 SURVIVED**\n\n"
    "**HELL HAS BEEN CONQUERED.**\n\n"
    "{vc} never emptied — not for one single second.\n\n"
    "Completed {completed_at}."
)

CMD_GAMBLE_LOCKED = (
    "❌ **Gambling is locked.** Gambling unlocks at **Difficulty 3** (96h milestone). "
    "Current difficulty: **Level {level} ({name})**."
)
CMD_GAMBLE_NOT_RUNNING = "❌ No event is currently running (status `{status}`)."
CMD_GAMBLE_PAUSED = "⏸️ Cannot gamble while the event is paused."
CMD_GAMBLE_GRACE = "⚠️ Cannot gamble while the voice channel is empty — get someone back in first."
CMD_GAMBLE_NOT_IN_VC = "❌ You must be **in the Hell voice channel** to gamble."
CMD_GAMBLE_NO_TIME = "❌ You have **{user_time}** recorded time, but you need at least **{min_time}** to place this bet."
CMD_GAMBLE_INVALID_BET = "❌ Bet amount must be positive and at most **{max_hours}h** for Difficulty {level}."
CMD_GAMBLE_HOURLY_LIMIT = (
    "❌ **Hourly gambling limit reached.** You can only gamble **{limit} time(s) per hour**. "
    "Next gamble available in **{time_left}**."
)
CMD_GAMBLE_OVERFLOW = (
    "⏳ **Fast bets used** ({limit} this hour). You can still gamble — extra bets use a "
    "**separate timer**. Wait **{cooldown}**."
)
CMD_GAMBLE_COOLDOWN = "⏳ You must wait **{cooldown}** before gambling again."
CMD_GAMBLE_WIN = (
    "🎰 **GAMBLE WON!** 🎲 {who} rolled a WIN ({win_chance}% odds on this bet) on Difficulty {level}!\n\n"
    "**+{reward_time}** has been added to your personal leaderboard timer! 🔥\n"
    "*Bet:* `{bet_time}` · *Net gain:* `+{net_gain}` · *New leaderboard time:* `{new_time}`\n"
    "*Bigger bets pay the same multiplier but with worse odds.*"
)
CMD_GAMBLE_JACKPOT = (
    "💎 **JACKPOT!** 🎲 {who} hit a **{multiplier}x** jackpot ({win_chance}% win band) on Difficulty {level}!\n\n"
    "**+{reward_time}** slammed onto your leaderboard timer! 🔥\n"
    "*Bet:* `{bet_time}` · *Net gain:* `+{net_gain}` · *New leaderboard time:* `{new_time}`"
)
CMD_GAMBLE_LOSE = (
    "💀 **GAMBLE LOST!** 🎲 {who} rolled a LOSS ({win_chance}% win odds on this bet) on Difficulty {level}!\n\n"
    "You lost **-{penalty_time}** from your leaderboard timer and have been muted for **{mute_duration}** from the server. 🔇\n"
    "*Bet:* `{bet_time}` · *New leaderboard time:* `{new_time}`"
)
CMD_GAMBLE_LOSE_WALLET = (
    "💀 **GAMBLE LOST!** 🎲 {who} rolled a LOSS ({win_chance}% win odds) on Difficulty {level}!\n\n"
    "You lost **-{penalty_time}** from Gamble Time (heavier than the stake — **no mute**).\n"
    "*Bet:* `{bet_time}` · *New Gamble Time:* `{new_time}`"
)

CMD_MYSTATS_NONE = (
    "You have no recorded time in this event yet — join {vc} to start your clock."
)

CMD_MILESTONES_TITLE = "🏁 WELCOME TO HELL — MILESTONES"
CMD_MILESTONES_DESCRIPTION = (
    "Milestones follow the **global event timer**, not individual user time."
)
CMD_MILESTONES_REACHED = "✅ reached {reached_at} — {member_count} eligible"
CMD_MILESTONES_PENDING = "⏳ in {time_to_go}"
CMD_MILESTONES_IDLE = "—"
CMD_MILESTONES_FIELD = "{hours}h — {short_reward}"

CMD_HELP_TITLE = "🔥 WELCOME TO HELL — COMMANDS"
CMD_HELP_DESCRIPTION = (
    "The event: keep at least one real human in {vc} for **160 hours straight**.\n"
    "If it empties, a **{grace_seconds}s** countdown starts — nobody back in time and the run "
    "is over, permanently."
)
CMD_HELP_EVERYONE_FIELD = "Anyone can use"
CMD_HELP_HOST_FIELD = "{host_role} only"
CMD_HELP_RULES_FIELD = "Good to know"
CMD_HELP_RULES = (
    "• Bots and {clanker_role} never count, and {clanker_role} is disconnected on sight.\n"
    "• AFK counts — you just have to be there.\n"
    "• Random alive checks: reply `Yes` within {alive_minutes} minutes or you are disconnected "
    "(your time is kept, rejoin whenever).\n"
    "• Milestone rewards go to whoever is in the VC at that exact second."
)
CMD_HELP_FOOTER = "Times are tracked per person; the 160h clock is shared."

CMD_USER_TITLE = "📊 {name} in Hell"
CMD_USER_NO_TIME = "{who} has no recorded time in this event yet."
CMD_USER_TIME_FIELD = "⏱️ Time in the VC"
CMD_USER_RANK_FIELD = "🏅 Rank"
CMD_USER_RANK_VALUE = "**#{rank}** of {total}"
CMD_USER_SHARE_FIELD = "📈 Share of the event"
CMD_USER_MILESTONES_FIELD = "🎁 Milestones claimed ({count})"
CMD_USER_MILESTONES_NONE = "*none yet — be in the VC when the next one lands*"
CMD_USER_PRESENT = "🟢 In the VC right now"
CMD_USER_ABSENT = "⚫ Not in the VC"

CMD_EXPORT_TITLE = "📄 Leaderboard export"
CMD_EXPORT_DESCRIPTION = (
    "`{filename}` — {rows} participant(s), for handing out rewards outside Discord."
)
CMD_EXPORT_EMPTY = "❌ There is nothing to export yet — no participant has recorded time."

CMD_LOGS_TAIL_EMPTY = "📭 Nothing buffered right now."
CMD_LOGS_UNAVAILABLE = "❌ The live log stream is not available."
CMD_LOGS_ON = "📡 Live log stream **enabled**."
CMD_LOGS_OFF = "📴 Live log stream **disabled**."
CMD_LOGS_TEST = "✅ Test line sent to the operator's DMs."
CMD_LOGS_FLUSHED = "📨 Flushed **{sent}** message(s)."
CMD_LOGS_STATUS = "📡 Live log stream: **{status}**"

CMD_RESTART_DONE = (
    "♻️ **Bot restarting** — the process will exit now and the process manager "
    "should bring it back automatically. The event timer is untouched; the bot "
    "will resume where it left off."
)
CMD_RESTART_NOT_OPERATOR = "⛔ Only the bot operator can restart the bot."
CMD_RESTART_DM_ONLY = "⛔ This command can only be used in a DM to the bot."

CMD_SECURITY_TITLE = "🛡️ WELCOME TO HELL — SECURITY REPORT"
CMD_SECURITY_DESCRIPTION = (
    "This report shows what the bot's anti-cheat system has detected. "
    "Anything suspicious triggers an automatic alert to the operator's DMs."
)

CMD_MESSAGES_RELOADED = (
    "✅ **Announcements.py reloaded** — {count} message(s) in memory, {milestones} milestone(s). "
    "New wording applies from the next message."
)
CMD_MESSAGES_FAILED = (
    "❌ **Announcements.py could not be loaded**, so the bot kept the previous text:\n"
    "```\n{error}\n```"
)


# =============================================================================
# 12b. /hell broadcast  (host colored embed)
# =============================================================================
#  {level} {emoji}

BROADCAST_TARGET_ANNOUNCE = "the announcement channel"
BROADCAST_TARGET_VC = "the VC text chat"
BROADCAST_TITLE_INFO = "{emoji} INFO"
BROADCAST_TITLE_SUCCESS = "{emoji} SUCCESS"
BROADCAST_TITLE_WARNING = "{emoji} WARNING"
BROADCAST_TITLE_ERROR = "{emoji} ERROR"
BROADCAST_TITLE_DEBUG = "{emoji} DEBUG"
BROADCAST_TITLE_MILESTONE = "{emoji} MILESTONE"
BROADCAST_TITLE_IDLE = "{emoji} IDLE"
BROADCAST_TITLE_GRACE = "{emoji} GRACE"
BROADCAST_TITLE_COMPLETED = "{emoji} COMPLETED"

CMD_BROADCAST_DONE = (
    "📢 Broadcast sent to **{target}** as a **{level}** embed."
)
CMD_BROADCAST_FAILED = (
    "❌ Could not post the broadcast to **{target}** — check the bot's channel permissions."
)
CMD_SETDIFFICULTY_LEVEL_REQUIRED = "❌ Please choose a difficulty level (`0`, `1`, `2`, `3`, `4`, or `auto`)."
CMD_BROADCAST_NEED_MESSAGE = "❌ Please include a message to broadcast."

CMD_CONTINUATION_NOT_ALLOWED = (
    "❌ **Hell 2 cannot resume yet.** The 160h *keep on Hell?* vote must have closed with a **Yes** majority after 10 minutes."
)
CMD_CONTINUATION_DONE = (
    "🔥 **HELL 2 HAS STARTED.** The same run now continues to **320 hours** — no milestones after 160h, only a final and secret reward."
)


# =============================================================================
# 12c. 160h continuation vote & Hell 2
# =============================================================================
#  {answer} {seconds} {yes} {no}

CONTINUATION_TITLE = "📬 Something has been sent to your DM"
CONTINUATION_DESCRIPTION = (
    "**Will you like to keep on Hell or not?**\n\n"
    "Your personal stat card was sent to your DMs. "
    "If most votes are **Yes**, the hosts can use `/hell resume` to keep the same run going to **320h**.\n\n"
    "The vote closes in **{seconds} minutes**."
)
CONTINUATION_VOTE_RECORDED = "✅ Vote recorded: **{answer}**."
CONTINUATION_VOTE_CLOSED = "❌ This vote has already closed."

CONTINUATION_RESULT_TITLE = "🗳️ The 160h vote has closed"
CONTINUATION_RESULT_DESCRIPTION = "**Yes**: {yes} vote(s)  ·  **No**: {no} vote(s)"
CONTINUATION_RESULT_YES_FIELD = "🔥 The run can continue"
CONTINUATION_RESULT_YES_TEXT = (
    "Most people voted **Yes**. A host can now run `/hell resume` to keep the same run going to **320h**. "
    "After 160h there are no milestones — only one final and secret reward."
)
CONTINUATION_RESULT_NO_FIELD = "🛑 Hell has ended"
CONTINUATION_RESULT_NO_TEXT = "The majority said **No** — Hell stays conquered."

CONTINUATION_RESUME_TITLE = "🔥 HELL 2 — THE KEEP ON HELL CHALLENGE"
CONTINUATION_RESUME_DESCRIPTION = (
    "Hell is not over. The same run now continues to **320 hours**. "
    "There are no more milestones after 160h — only one final and secret reward."
)
CONTINUATION_RESUME_RULE_FIELD = "📜 The rule"
CONTINUATION_RESUME_RULE = (
    "• Keep at least one real human in {vc} until **320h**.\n"
    "• No milestones will be announced after 160h.\n"
    "• At **320h**, a final and secret reward awaits the survivors."
)
CONTINUATION_RESUME_FOOTER = "320h · no milestones · secret reward"

CONTINUATION_COMPLETION_TITLE = "🏆🔥 HELL 2 — 320H SURVIVED"
CONTINUATION_COMPLETION_DESCRIPTION = (
    "The full **320H** Hell 2 challenge has been survived. "
    "The final, secret reward is now yours."
)
CONTINUATION_SECRET_REWARD = "🔒 *A final and secret reward*"
CONTINUATION_COMPLETION_REWARD_FIELD = "🎁 Final secret reward"
CONTINUATION_COMPLETION_REWARD_TEXT = (
    "{final_reward}\n"
    "*Only those who survived to 320h.*"
)
CONTINUATION_COMPLETION_FOOTER = "320h · no milestones · final secret reward"

PROGRESS_TITLE_CONTINUATION = "🔥 HELL 2 — KEEP ON HELL"
PROGRESS_CURRENT_CONTINUATION = "🔒 No milestones — only the final 320h reward"
PROGRESS_NEXT_CONTINUATION = "🔒 Final secret reward at 320h"
PROGRESS_COMPLETED_CONTINUATION_TEXT = "320H Hell 2 survived. The final secret reward is theirs."


# =============================================================================
# 13. COLOURS  (hex, as used by the embeds)
# =============================================================================

COLOR_RUNNING = 0xE25822     # ember orange
COLOR_MILESTONE = 0xFF4500
COLOR_FAILED = 0x8B0000
COLOR_COMPLETED = 0xFFD700
COLOR_CANCELLED = 0x607D8B
COLOR_IDLE = 0x2F3136
COLOR_GRACE = 0xFFA500
COLOR_CARD = 0xE25822
COLOR_INFO = 0x3498DB
COLOR_SUCCESS = 0x2ECC71
COLOR_WARNING = 0xF1C40F
COLOR_ERROR = 0xE74C3C
COLOR_DEBUG = 0x9B59B6
COLOR_CONTINUATION = 0xFF4500

# Emoji shown next to the event status on the progress message.
STATUS_EMOJI = {
    "IDLE": "💤",
    "RUNNING": "🔥",
    "FAILED": "💀",
    "COMPLETED": "🏆",
    "CANCELLED": "🛑",
}
