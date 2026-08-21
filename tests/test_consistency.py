"""Consistency between the places that describe the same thing.

Configuration lives in three surfaces that must agree — `hell/config.py`
(what the bot reads), `.env.example` (what an operator copies) and
`launcher/envfile.py` (what the desktop UI shows).  Commands live in two —
the cog and the README.  Drift between them is invisible until a user follows
the docs and nothing happens, so it is checked here instead.
"""

from __future__ import annotations

import re
from pathlib import Path

import discord
import pytest

from hell.cog import HellCommands
from launcher import envfile

ROOT = Path(__file__).resolve().parents[1]
CONFIG_SOURCE = (ROOT / "hell" / "config.py").read_text(encoding="utf-8")
ENV_EXAMPLE = (ROOT / ".env.example").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")

# Read straight from the loader: every getenv/_int_env/_float_env/_bool_env key.
CONFIG_KEYS = set(re.findall(r'_(?:int|float|bool)_env\(\s*"([A-Z0-9_]+)"', CONFIG_SOURCE)) | set(
    re.findall(r'os\.getenv\(\s*"([A-Z0-9_]+)"', CONFIG_SOURCE)
)


def env_example_keys() -> set[str]:
    return {
        line.split("=", 1)[0].strip()
        for line in ENV_EXAMPLE.splitlines()
        if line.strip() and not line.strip().startswith("#") and "=" in line
    }


# ------------------------------------------------------------ configuration


def test_the_loader_reads_a_sane_set_of_keys():
    assert "DISCORD_TOKEN" in CONFIG_KEYS
    assert len(CONFIG_KEYS) > 15


def test_every_setting_is_documented_in_env_example():
    missing = CONFIG_KEYS - env_example_keys()
    assert not missing, f"settings the bot reads but .env.example never mentions: {sorted(missing)}"


def test_env_example_invents_nothing():
    unknown = env_example_keys() - CONFIG_KEYS
    assert not unknown, f".env.example lists settings the bot never reads: {sorted(unknown)}"


def test_every_setting_is_editable_in_the_launcher():
    missing = CONFIG_KEYS - set(envfile.BY_KEY)
    assert not missing, f"settings missing from the launcher's Settings tab: {sorted(missing)}"


def test_the_launcher_invents_nothing():
    unknown = set(envfile.BY_KEY) - CONFIG_KEYS
    assert not unknown, f"launcher fields the bot never reads: {sorted(unknown)}"


def test_required_settings_agree_with_the_loader():
    """A field marked optional in the UI must not be one the bot demands."""
    required_in_loader = set(re.findall(r'"([A-Z0-9_]+)",\s*required=True', CONFIG_SOURCE))
    required_in_loader.add("DISCORD_TOKEN")
    required_in_ui = {field.key for field in envfile.FIELDS if field.required}
    assert required_in_loader <= required_in_ui, (
        f"the bot refuses to start without {sorted(required_in_loader - required_in_ui)}, "
        "but the launcher does not mark them required"
    )


def test_defaults_match_between_the_launcher_and_env_example():
    example_values = {
        line.split("=", 1)[0].strip(): line.split("=", 1)[1].strip()
        for line in ENV_EXAMPLE.splitlines()
        if line.strip() and not line.strip().startswith("#") and "=" in line
    }
    mismatched = []
    for field in envfile.FIELDS:
        shipped = example_values.get(field.key, "")
        if field.default and shipped and field.default != shipped:
            mismatched.append(f"{field.key}: launcher={field.default!r} .env.example={shipped!r}")
    assert not mismatched, "defaults disagree:\n" + "\n".join(mismatched)


# ---------------------------------------------------------------- commands


@pytest.fixture
def command_names(config, engine):
    from discord.ext import commands as dpy

    from hell.announcer import Announcer
    from hell.monitor import VoiceMonitor

    intents = discord.Intents.default()
    bot = dpy.Bot(command_prefix="!", intents=intents)
    bot.config = config
    announcer = Announcer(bot, config, engine)
    monitor = VoiceMonitor(bot, config, engine, announcer)
    cog = HellCommands(bot, config, engine, monitor)
    return {command.name for command in cog.app_command.commands}  # type: ignore[union-attr]


def test_every_command_is_documented(command_names):
    undocumented = {name for name in command_names if f"/hell {name}" not in README}
    assert not undocumented, f"commands missing from the README: {sorted(undocumented)}"


def test_the_readme_invents_no_commands(command_names):
    documented = set(re.findall(r"`/hell (\w+)`", README))
    ghosts = documented - command_names
    assert not ghosts, f"the README documents commands that do not exist: {sorted(ghosts)}"


def test_every_command_has_a_description(command_names, config, engine):
    from discord.ext import commands as dpy

    from hell.announcer import Announcer
    from hell.monitor import VoiceMonitor

    bot = dpy.Bot(command_prefix="!", intents=discord.Intents.default())
    bot.config = config
    monitor = VoiceMonitor(bot, config, engine, Announcer(bot, config, engine))
    cog = HellCommands(bot, config, engine, monitor)
    for command in cog.app_command.commands:  # type: ignore[union-attr]
        assert command.description, f"/hell {command.name} has no description"
        assert len(command.description) <= 100, f"/hell {command.name} description is too long"


# ------------------------------------------------------------------ wording


def test_the_message_file_defines_every_text_the_code_asks_for():
    """`TEXT.SOMETHING` in the code must exist in Announcements.py."""
    from hell.texts import TEXT

    used: set[str] = set()
    for path in (ROOT / "hell").glob("*.py"):
        used |= set(re.findall(r"TEXT\.([A-Z][A-Z0-9_]+)", path.read_text(encoding="utf-8")))

    missing = [name for name in sorted(used) if not hasattr(TEXT, name)]
    assert not missing, f"Announcements.py is missing: {missing}"


def test_no_module_hardcodes_a_milestone_reward():
    """Rewards belong in Announcements.py, not scattered through the code."""
    offenders = []
    for path in (ROOT / "hell").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "@hell-ist" in text or "@hell master" in text:
            offenders.append(path.name)
    assert not offenders, f"reward text hardcoded in: {offenders}"


def test_boolean_settings_reject_typos_loudly(monkeypatch):
    """`ALIVE_CHECK_ENABLED=flase` or `LOG_DM_ENABLED=ture` must not
    silently flip a feature off — that is how events break invisibly."""
    from hell.config import ConfigError, _bool_env

    monkeypatch.setenv("HELL_TEST_BOOL", "ture")
    with pytest.raises(ConfigError):
        _bool_env("HELL_TEST_BOOL", True)
    monkeypatch.setenv("HELL_TEST_BOOL", "flase")
    with pytest.raises(ConfigError):
        _bool_env("HELL_TEST_BOOL", False)

    for raw, expected in (("true", True), ("1", True), ("on", True), ("yes", True),
                          ("false", False), ("0", False), ("off", False), ("no", False)):
        monkeypatch.setenv("HELL_TEST_BOOL", raw)
        assert _bool_env("HELL_TEST_BOOL", True) is expected

    monkeypatch.delenv("HELL_TEST_BOOL", raising=False)
    assert _bool_env("HELL_TEST_BOOL", True) is True      # empty -> default
