"""Error codes for Welcome to Hell — known, documented failures.

Every error or warning the bot emits that is not a simple log message carries
a code like HEL-042.  These codes are stable across versions so operators
can look them up here or in the dashboard.

Code format: HEL-NNN where NNN is a three-digit number.

Range allocation
----------------
001-099  Configuration / startup
100-199  Runtime / engine
200-299  Discord API / rate limits
300-399  Security / cheating
400-499  Storage / data
500-599  Crash / recovery
"""

from __future__ import annotations


class ErrorCode:
    """A documented error code with a human-readable explanation."""

    def __init__(self, code: str, title: str, description: str, solution: str = ""):
        self.code = code
        self.title = title
        self.description = description
        self.solution = solution

    def __str__(self) -> str:
        return f"[{self.code}] {self.title}"

    def format(self) -> str:
        parts = [f"**{self.code}**: {self.title}", f"_{self.description}_"]
        if self.solution:
            parts.append(f"\u2192 {self.solution}")
        return "\n".join(parts)

    def short(self) -> str:
        return f"{self.code} {self.title}"


# ---------------------------------------------------------------------------
#  001-099  Configuration / startup
# ---------------------------------------------------------------------------

CONFIG_MISSING_TOKEN = ErrorCode(
    "HEL-001",
    "Missing Discord token",
    "DISCORD_TOKEN is not set in the environment or .env file.",
    "Add DISCORD_TOKEN=your_token to .env or export it in the shell.",
)

CONFIG_MISSING_GUILD = ErrorCode(
    "HEL-002",
    "Missing guild ID",
    "GUILD_ID is required but was not set.",
    "Add GUILD_ID=your_server_id to .env.",
)

CONFIG_MISSING_CHANNEL = ErrorCode(
    "HEL-003",
    "Missing announcement channel",
    "ANNOUNCE_CHANNEL_ID is required but was not set.",
    "Add ANNOUNCE_CHANNEL_ID=your_channel_id to .env.",
)

CONFIG_MISSING_HOST_ROLE = ErrorCode(
    "HEL-004",
    "Missing host role",
    "GAMENIGHT_HOST_ROLE_ID is required but was not set.",
    "Add GAMENIGHT_HOST_ROLE_ID=your_role_id to .env.",
)

CONFIG_BAD_INTENT = ErrorCode(
    "HEL-010",
    "Privileged intent not enabled",
    "The bot needs the Message Content, Server Members, and Voice States intents "
    "enabled in the Discord Developer Portal.",
    "Go to Developer Portal -> Bot -> Privileged Gateway Intents.",
)

CONFIG_UNKNOWN_ROLE = ErrorCode(
    "HEL-011",
    "Unknown role ID in configuration",
    "A role ID set in the .env file does not match any existing role in the server. "
    "Reward mentions will be shown as plain text instead.",
    "Check that the role ID is correct and the bot can see the role.",
)

# ---------------------------------------------------------------------------
#  100-199  Runtime / engine
# ---------------------------------------------------------------------------

EVENT_VC_EMPTY_FAIL = ErrorCode(
    "HEL-100",
    "Event failed \u2014 VC stayed empty",
    "The voice channel was empty of valid humans for the entire grace period. "
    "The event has been terminated and cannot be resumed.",
    "Start a new run with /hell reset followed by /hell start.",
)

EVENT_GRACE_OPEN = ErrorCode(
    "HEL-101",
    "VC empty \u2014 grace window open",
    "All valid humans left the voice channel. The grace countdown is running.",
    "Somebody needs to join the VC before the window expires.",
)

EVENT_DODGE_DETECTED = ErrorCode(
    "HEL-110",
    "Alive-check dodging detected",
    "A user left the VC right before or during a roll call, suggesting they "
    "are avoiding the check.",
    "The operator may want to warn the user: dodging may result in disqualification.",
)

EVENT_FLAP_DETECTED = ErrorCode(
    "HEL-111",
    "VC flapping detected",
    "A user joined and left the VC repeatedly in a short window, which may "
    "indicate attempts to pause their personal timer.",
    "The operator may want to investigate the user's behaviour.",
)

EVENT_RATE_LIMIT_SPIKE = ErrorCode(
    "HEL-120",
    "Rate-limit spike detected",
    "The bot received an unusually high number of Discord 429 responses in a "
    "short period.  This may cause commands to be delayed.",
    "The bot will automatically back off.  Check if external tools are calling "
    "the API, or if a misconfigured command is looping.",
)

EVENT_MONITOR_STALE = ErrorCode(
    "HEL-130",
    "Monitor loop stalled",
    "The voice channel has not been successfully observed for over 5 minutes. "
    "The event timer keeps running but nobody's presence is being verified.",
    "Check the bot's gateway connection and the VC channel permissions.",
)

# ---------------------------------------------------------------------------
#  200-299  Discord API / rate limits
# ---------------------------------------------------------------------------

API_LOGIN_FAILED = ErrorCode(
    "HEL-200",
    "Discord login failed",
    "The bot's token was rejected by Discord.  This usually means the token "
    "is invalid or has been reset.",
    "Check DISCORD_TOKEN in your .env file.  Regenerate it in the Developer Portal if needed.",
)

API_INTENTS_REQUIRED = ErrorCode(
    "HEL-201",
    "Privileged intents not granted",
    "Discord refused the connection because required privileged intents are not "
    "enabled for this application.",
    "Enable Server Members Intent and Message Content Intent in the Developer Portal.",
)

API_DM_CLOSED = ErrorCode(
    "HEL-202",
    "Operator DMs closed",
    "The live log stream cannot deliver messages to the operator because they "
    "have closed DMs for this bot.",
    "The operator needs to open DMs: right-click the bot -> Message.  The stream "
    "will automatically resume.",
)

API_DM_FAILED = ErrorCode(
    "HEL-203",
    "DM delivery failed repeatedly",
    "The live log stream encountered multiple consecutive failures and was "
    "temporarily disabled.",
    "The bot will automatically retry.  If this persists, check that the "
    "operator's user ID is correct.",
)

API_RATE_LIMITED = ErrorCode(
    "HEL-204",
    "Discord rate limit hit",
    "The bot received a 429 response from Discord.  Operations will be delayed.",
    "The bot auto-backs off.  This is usually temporary.",
)

API_CANNOT_SEND = ErrorCode(
    "HEL-210",
    "Cannot send message",
    "The bot lacks permissions to send a message in the target channel.",
    "Check that the bot has View Channel, Send Messages, and Embed Links "
    "permissions in the announcement and alive-check channels.",
)

API_CANNOT_KICK = ErrorCode(
    "HEL-211",
    "Cannot disconnect user",
    "The bot lacks Move Members permission needed to disconnect users from the VC.",
    "Grant Move Members to the bot in the voice channel permissions.",
)

# ---------------------------------------------------------------------------
#  300-399  Security / cheating
# ---------------------------------------------------------------------------

SEC_DODGE_THRESHOLD = ErrorCode(
    "HEL-300",
    "Repeated alive-check dodging",
    "A user has dodged multiple roll calls by leaving the VC.  This is a pattern "
    "consistent with cheating.",
    "Consider talking to the user or checking the /hell security report.",
)

SEC_FLAP_THRESHOLD = ErrorCode(
    "HEL-301",
    "Excessive VC flapping",
    "A user has joined and left the VC many times, which may be an attempt to "
    "game the personal timer.",
    "Consider warning the user or reviewing the /hell security report.",
)

# ---------------------------------------------------------------------------
#  400-499  Storage / data
# ---------------------------------------------------------------------------

STORAGE_CORRUPT = ErrorCode(
    "HEL-401",
    "Database corrupt or unreadable",
    "The SQLite database file could not be read.  This may be due to a disk "
    "error or a corrupted file.",
    "Check that the database path is writable and the file is not corrupted. "
    "Restore from backup if available.",
)

STORAGE_MIGRATION = ErrorCode(
    "HEL-402",
    "Database migration needed",
    "The database is from an older version of the bot and needs schema changes.",
    "The bot will migrate automatically on startup.  If this fails, delete the "
    "database file and set up fresh.",
)

# ---------------------------------------------------------------------------
#  500-599  Crash / recovery
# ---------------------------------------------------------------------------

RECOVERY_RESTART = ErrorCode(
    "HEL-500",
    "Bot restart recovered",
    "The bot was restarted (or crashed) and has recovered the event state "
    "from the database.",
    "The event continues normally.  Any roll call in flight was cancelled and no-one "
    "was disconnected for it.",
)

RECOVERY_GRACE = ErrorCode(
    "HEL-501",
    "Grace window recovered after restart",
    "The bot was restarted while an empty-VC grace window was open.  The window "
    "has been resumed.",
    "Somebody needs to join the VC to keep the run alive.",
)

RECOVERY_MILESTONE = ErrorCode(
    "HEL-502",
    "Milestone re-announced after restart",
    "A milestone was claimed but never announced before a crash.  It is being "
    "re-posted now.",
    "No action needed; this automatically prevents the announcement from being lost.",
)

RECOVERY_STAT_CARDS = ErrorCode(
    "HEL-503",
    "Stat card delivery resumed",
    "The bot was restarted while end-of-event stat cards were being sent. "
    "Delivery is resuming from where it left off.",
    "No action needed.  Users with DMs open will receive their cards.",
)


# ---------------------------------------------------------------------------
#  Lookup by code string
# ---------------------------------------------------------------------------

_ALL: dict[str, ErrorCode] = {}

for _name in list(globals()):
    _val = globals()[_name]
    if isinstance(_val, ErrorCode):
        _ALL[_val.code] = _val


def lookup(code: str) -> ErrorCode | None:
    """Look up an error code by its string identifier."""
    return _ALL.get(code)


def format_short(code: str) -> str:
    """Short format for log lines: [HEL-001] Missing Discord token."""
    ec = lookup(code)
    return str(ec) if ec else f"[{code}] unknown code"


def format_long(code: str) -> str:
    """Full format with description and solution, for embeds."""
    ec = lookup(code)
    if ec:
        return ec.format()
    return f"**{code}**: unknown error code\n_No further information available._"