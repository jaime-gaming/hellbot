"""Smoke tests for the Discord wiring (no gateway connection is made)."""

from __future__ import annotations

import discord
import pytest
from discord.ext import commands

from hell.announcer import Announcer
from hell.cog import RESET_PHRASE, HellCommands
from hell.monitor import VoiceMonitor


@pytest.fixture
def bot(config):
    intents = discord.Intents.default()
    intents.members = True
    intents.voice_states = True
    b = commands.Bot(command_prefix="!", intents=intents)
    b.config = config  # type: ignore[attr-defined]
    return b


def test_command_group_exposes_the_expected_subcommands(bot, config, engine):
    announcer = Announcer(bot, config, engine)
    monitor = VoiceMonitor(bot, config, engine, announcer)
    cog = HellCommands(bot, config, engine, monitor)

    names = {cmd.name for cmd in cog.app_command.commands}  # type: ignore[union-attr]
    assert names == {"start", "status", "leaderboard", "milestones", "stop", "reset"}
    assert cog.app_command.name == "hell"  # type: ignore[union-attr]


def test_restricted_commands_carry_a_check(bot, config, engine):
    announcer = Announcer(bot, config, engine)
    monitor = VoiceMonitor(bot, config, engine, announcer)
    cog = HellCommands(bot, config, engine, monitor)
    by_name = {c.name: c for c in cog.app_command.commands}  # type: ignore[union-attr]
    for restricted in ("start", "stop", "reset"):
        assert by_name[restricted].checks, f"/hell {restricted} must be host-restricted"
    for public in ("status", "leaderboard", "milestones"):
        assert not by_name[public].checks


def test_reset_phrase_is_strong():
    assert RESET_PHRASE == "RESET WELCOME TO HELL"


def test_monitor_intervals_follow_config(bot, config, engine):
    monitor = VoiceMonitor(bot, config, engine, Announcer(bot, config, engine))
    assert monitor._monitor_loop.seconds == 1.0
    assert monitor._progress_loop.seconds == 10.0
