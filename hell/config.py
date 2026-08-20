"""Configuration loading (environment / .env based).

Roles and channels are configured by **ID** so renames can never break the bot.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .paths import env_path, resolve

try:  # optional dependency, only needed for local .env files
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - dotenv is in requirements but stay safe
    def load_dotenv(*_args, **_kwargs):  # type: ignore[misc]
        return False


DEFAULT_VOICE_CHANNEL_ID = 1539756705997652079
DEFAULT_LOG_DM_USER_ID = 984083829767675965  # Jaime Gaming — live log recipient


class ConfigError(RuntimeError):
    """Raised when the environment is missing something the bot cannot run without."""


def _int_env(name: str, default: int | None = None, required: bool = False) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        if required and default is None:
            raise ConfigError(f"Missing required environment variable: {name}")
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a numeric Discord ID, got {raw!r}") from exc


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


@dataclass
class Config:
    """All runtime knobs.  Only `token` and the IDs below are mandatory."""

    token: str
    guild_id: int
    voice_channel_id: int
    announce_channel_id: int

    gamenight_host_role_id: int
    clanker_role_id: int

    # Reward roles are announcement-only (the bot never assigns them), but the
    # IDs let the announcements render real mentions instead of plain text.
    hell_role_id: int | None = None
    hellist_role_id: int | None = None
    hell_master_role_id: int | None = None
    cool_people_role_id: int | None = None

    database_path: Path = field(default_factory=lambda: Path("data/hell.sqlite3"))

    monitor_interval: float = 1.0        # VC check cadence (seconds)
    progress_interval: float = 10.0      # progress message edit cadence (seconds)
    startup_grace: float = 15.0          # ignore VC observations right after boot
    empty_vc_grace_seconds: float = 15.0  # empty VC -> this long to repopulate or the run dies
    max_tick_credit: float = 5.0         # cap per-tick leaderboard credit (downtime guard)
    require_occupants_to_start: bool = True
    heartbeat_minutes: float = 15.0

    # --- alive checks ("roll call") ---
    alive_check_enabled: bool = True
    alive_check_min_hours: float = 1.0
    alive_check_max_hours: float = 6.0
    alive_check_timeout_minutes: float = 5.0
    alive_check_strict: bool = False          # True -> only the exact string "Yes"
    alive_check_channel_id: int | None = None  # default: the VC's own text chat

    # --- end-of-event stat cards ---
    send_final_dms: bool = True
    dm_delay_seconds: float = 1.0

    # --- live log stream (DM'd to the operator) ---
    log_dm_enabled: bool = True
    log_dm_user_id: int = DEFAULT_LOG_DM_USER_ID
    log_dm_level: str = "INFO"
    log_dm_flush_seconds: float = 3.0
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env_file: str | os.PathLike[str] | None = None) -> Config:
        """Load configuration from the environment (and `.env` next to the app)."""
        target = Path(env_file) if env_file is not None else env_path()
        if target and target.exists():
            load_dotenv(target, override=False)

        token = os.getenv("DISCORD_TOKEN", "").strip()
        if not token:
            raise ConfigError("Missing required environment variable: DISCORD_TOKEN")

        return cls(
            token=token,
            guild_id=_int_env("GUILD_ID", required=True),  # type: ignore[arg-type]
            voice_channel_id=_int_env("VOICE_CHANNEL_ID", default=DEFAULT_VOICE_CHANNEL_ID),  # type: ignore[arg-type]
            announce_channel_id=_int_env("ANNOUNCE_CHANNEL_ID", required=True),  # type: ignore[arg-type]
            gamenight_host_role_id=_int_env("GAMENIGHT_HOST_ROLE_ID", required=True),  # type: ignore[arg-type]
            clanker_role_id=_int_env("CLANKER_ROLE_ID", required=True),  # type: ignore[arg-type]
            hell_role_id=_int_env("HELL_ROLE_ID"),
            hellist_role_id=_int_env("HELLIST_ROLE_ID"),
            hell_master_role_id=_int_env("HELL_MASTER_ROLE_ID"),
            cool_people_role_id=_int_env("COOL_PEOPLE_ROLE_ID"),
            database_path=resolve(os.getenv("DATABASE_PATH", "").strip() or "data/hell.sqlite3"),
            monitor_interval=_float_env("MONITOR_INTERVAL", 1.0),
            progress_interval=_float_env("PROGRESS_INTERVAL", 10.0),
            startup_grace=_float_env("STARTUP_GRACE_SECONDS", 15.0),
            empty_vc_grace_seconds=_float_env("EMPTY_VC_GRACE_SECONDS", 15.0),
            max_tick_credit=_float_env("MAX_TICK_CREDIT_SECONDS", 5.0),
            require_occupants_to_start=_bool_env("REQUIRE_OCCUPANTS_TO_START", True),
            heartbeat_minutes=_float_env("HEARTBEAT_MINUTES", 15.0),
            alive_check_enabled=_bool_env("ALIVE_CHECK_ENABLED", True),
            alive_check_min_hours=_float_env("ALIVE_CHECK_MIN_HOURS", 1.0),
            alive_check_max_hours=_float_env("ALIVE_CHECK_MAX_HOURS", 6.0),
            alive_check_timeout_minutes=_float_env("ALIVE_CHECK_TIMEOUT_MINUTES", 5.0),
            alive_check_strict=_bool_env("ALIVE_CHECK_STRICT", False),
            alive_check_channel_id=_int_env("ALIVE_CHECK_CHANNEL_ID"),
            send_final_dms=_bool_env("SEND_FINAL_DMS", True),
            dm_delay_seconds=_float_env("DM_DELAY_SECONDS", 1.0),
            log_dm_enabled=_bool_env("LOG_DM_ENABLED", True),
            log_dm_user_id=_int_env("LOG_DM_USER_ID", default=DEFAULT_LOG_DM_USER_ID),  # type: ignore[arg-type]
            log_dm_level=os.getenv("LOG_DM_LEVEL", "INFO").strip().upper() or "INFO",
            log_dm_flush_seconds=_float_env("LOG_DM_FLUSH_SECONDS", 3.0),
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
        )

    def summary(self) -> list[tuple[str, str]]:
        """Human-readable settings for logs and `/hell doctor` (no secrets)."""
        return [
            ("Guild", str(self.guild_id)),
            ("Voice channel", str(self.voice_channel_id)),
            ("Announcements", str(self.announce_channel_id)),
            ("Host role", str(self.gamenight_host_role_id)),
            ("Clanker role", str(self.clanker_role_id)),
            ("Database", str(self.database_path)),
            ("Monitor / progress", f"{self.monitor_interval:g}s / {self.progress_interval:g}s"),
            ("Empty-VC grace", f"{self.empty_vc_grace_seconds:g}s"),
            (
                "Alive checks",
                (
                    f"every {self.alive_check_min_hours:g}-{self.alive_check_max_hours:g}h, "
                    f"{self.alive_check_timeout_minutes:g} min to answer"
                    if self.alive_check_enabled
                    else "disabled"
                ),
            ),
            ("Final DMs", "on" if self.send_final_dms else "off"),
            (
                "Live log stream",
                f"{self.log_dm_level} -> {self.log_dm_user_id}" if self.log_dm_enabled else "off",
            ),
        ]

    def role_mention(self, role_id: int | None, fallback: str) -> str:
        """Mention a reward role if its ID is configured, else show plain text."""
        return f"<@&{role_id}>" if role_id else fallback

    def reward_text(self, hours: int, default: str) -> str:
        """Swap the plain `@role` text of a milestone reward for a real mention."""
        mapping = {
            32: (self.hell_role_id, "@hell"),
            96: (self.hellist_role_id, "@hell-ist"),
            160: (self.hell_master_role_id, "@hell master"),
        }
        if hours not in mapping:
            return default
        role_id, plain = mapping[hours]
        if not role_id:
            return default
        return default.replace(plain, f"<@&{role_id}>")
