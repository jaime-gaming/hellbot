"""Tests for `!` prefix commands in DMs and direct message channels."""

from __future__ import annotations

from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from discord.ext import commands

from hell import RESTART_EXIT_CODE
from hell.announcer import Announcer
from hell.cog import HellCommands
from hell.models import EventStatus
from hell.monitor import VoiceMonitor
from hell.texts import TEXT
from hell.timeutil import now_ts
from tests.conftest import T0, obs, start
from tests.test_command_flows import (
    FakeAuthor,
    FakeOperator,
    _operator_code,
    run,
)
from tests.test_integration import FakeTextChannel
from tests.test_monitor import FakeMember, FakeVoiceChannel


class FakeContext:
    """A fake `discord.ext.commands.Context` standing in for a DM interaction."""

    def __init__(self, bot: commands.Bot, author: FakeAuthor, guild_id: Optional[int] = None,
                 channel_id: int = 999999):
        self.bot = bot
        self.author = author
        self.guild = type("G", (), {"id": guild_id})() if guild_id else None
        self.channel = type("C", (), {"id": channel_id})()
        self.sent: list[dict[str, Any]] = []
        self.invoked_subcommand = None
        self.command = MagicMock()
        self.command.name = "test"

    async def send(self, content: Optional[str] = None, **kwargs: Any) -> Optional[discord.Message]:
        self.sent.append({"content": content, **kwargs})
        return None

    def text(self) -> str:
        parts: list[str] = []
        for message in self.sent:
            if message.get("content"):
                parts.append(str(message["content"]))
            for key in ("embed", "embeds"):
                value = message.get(key)
                if value is None:
                    continue
                from hell.announcer import embed_to_text

                for embed in value if isinstance(value, list) else [value]:
                    parts.append(embed_to_text(embed))
        return "\n".join(parts)


@pytest.fixture
def wired(config, engine):
    text = FakeTextChannel(config.announce_channel_id)
    voice = FakeVoiceChannel([FakeMember(1, "Alice"), FakeMember(2, "Bob")])

    class Bot(commands.Bot):
        def __init__(self):
            intents = discord.Intents.default()
            intents.members = True
            super().__init__(command_prefix=commands.when_mentioned_or("!"), intents=intents, help_command=None)
            self.config = config
            self.log_stream: Any = None
            self.operator = FakeOperator(config.log_dm_user_id)

        def is_ready(self):
            return True

        def is_closed(self):
            return False

        def get_channel(self, cid):
            return voice if cid == voice.id else text

        def get_guild(self, _gid):
            return None

        def get_user(self, uid):
            return self.operator if uid == self.operator.id else None

        async def fetch_user(self, uid):
            return self.operator if uid == self.operator.id else None

    bot = Bot()
    announcer = Announcer(bot, config, engine)
    monitor = VoiceMonitor(bot, config, engine, announcer)
    monitor.voice_channel = lambda: voice  # type: ignore[method-assign]
    monitor._ready_at = now_ts() - 1000
    cog = HellCommands(bot, config, engine, monitor)
    return cog, bot, text, voice


def call(cog: HellCommands, name: str, ctx: FakeContext, *args: Any, **kwargs: Any) -> Any:
    """Invoke a prefix command callback."""
    command = getattr(cog, name)
    return run(command.callback(cog, ctx, *args, **kwargs))


# ------------------------------------------------------------- public DM cmds


def test_dm_status_idle_and_running(wired, engine):
    cog, bot, _text, _voice = wired
    ctx = FakeContext(bot, FakeAuthor(uid=100, name="Contestant"))

    # When idle
    call(cog, "prefix_status", ctx)
    assert "not running" in ctx.text().lower()

    # When running
    start(engine, now_ts() - 3600, 1, 2)
    ctx_running = FakeContext(bot, FakeAuthor(uid=100, name="Contestant"))
    call(cog, "prefix_status", ctx_running)
    assert "RUNNING" in ctx_running.text()
    assert "1h 00m" in ctx_running.text()


def test_dm_leaderboard(wired, engine):
    cog, bot, _text, _voice = wired
    ctx = FakeContext(bot, FakeAuthor(uid=100, name="Contestant"))

    # When empty
    call(cog, "prefix_leaderboard", ctx)
    assert "Nobody has spent time in Hell yet" in ctx.text()

    # When populated
    start(engine, T0, 1, 2)
    for i in range(1, 61):
        engine.tick(obs(T0 + i, 1, 2))
    ctx2 = FakeContext(bot, FakeAuthor(uid=100, name="Contestant"))
    call(cog, "prefix_leaderboard", ctx2)
    assert "🥇" in ctx2.text()


def test_prefix_status_and_leaderboard_in_the_vc_chat_link_the_pinned_messages(wired, engine):
    """`!status` / `!lb` / `!hell status` in the VC text chat link the pinned cards."""
    cog, bot, _text, voice = wired
    start(engine, now_ts() - 3600, 1, 2)
    player = FakeAuthor(uid=100, name="Contestant")

    ctx_st = FakeContext(bot, player, channel_id=voice.id)
    call(cog, "prefix_status", ctx_st)
    assert ctx_st.sent == [{"content": TEXT.CMD_STATUS_VC_LINK}]

    ctx_lb = FakeContext(bot, player, channel_id=voice.id)
    call(cog, "prefix_leaderboard", ctx_lb)
    assert ctx_lb.sent == [{"content": TEXT.CMD_LEADERBOARD_VC_LINK}]

    # The !hell <subcommand> group routes the same way
    ctx_grp = FakeContext(bot, player, channel_id=voice.id)
    call(cog, "prefix_hell_group", ctx_grp, "status")
    assert ctx_grp.sent == [{"content": TEXT.CMD_STATUS_VC_LINK}]

    # …and outside the VC chat the normal embeds still appear
    ctx_dm = FakeContext(bot, player)
    call(cog, "prefix_status", ctx_dm)
    assert any("embed" in m for m in ctx_dm.sent)


def test_dm_milestones(wired, engine):
    cog, bot, _text, _voice = wired
    start(engine, T0, 1)
    engine.tick(obs(T0 + 32 * 3600, 1))

    ctx = FakeContext(bot, FakeAuthor(uid=100, name="Contestant"))
    call(cog, "prefix_milestones", ctx)
    text = ctx.text()
    for hours in (32, 64, 96, 128, 160):
        assert f"{hours}h" in text
    assert "✅ reached" in text


def test_dm_mystats(wired, engine):
    cog, bot, _text, _voice = wired
    ctx_none = FakeContext(bot, FakeAuthor(uid=999, name="Nobody"))
    call(cog, "prefix_mystats", ctx_none)
    assert "no recorded time" in ctx_none.text()

    start(engine, T0, 1)
    for i in range(1, 61):
        engine.tick(obs(T0 + i, 1))
    ctx_user = FakeContext(bot, FakeAuthor(uid=1, name="Alice"))
    call(cog, "prefix_mystats", ctx_user)
    assert "WELCOME TO HELL" in ctx_user.text()
    assert "SURVIVED" in ctx_user.text()


def test_dm_user_lookup(wired, engine):
    cog, bot, _text, _voice = wired
    start(engine, T0, 1, 2)
    for i in range(1, 61):
        engine.tick(obs(T0 + i, 1, 2))

    # Self lookup (no arg)
    ctx_self = FakeContext(bot, FakeAuthor(uid=1, name="User1"))
    call(cog, "prefix_user", ctx_self)
    assert "User1 in Hell" in ctx_self.text()

    # Mention lookup
    ctx_mention = FakeContext(bot, FakeAuthor(uid=100, name="Spectator"))
    call(cog, "prefix_user", ctx_mention, member="<@1>")
    assert "Rank" in ctx_mention.text()

    # User ID lookup
    ctx_id = FakeContext(bot, FakeAuthor(uid=100, name="Spectator"))
    call(cog, "prefix_user", ctx_id, member="2")
    assert "Rank" in ctx_id.text()

    # Name search in leaderboard
    ctx_name = FakeContext(bot, FakeAuthor(uid=100, name="Spectator"))
    call(cog, "prefix_user", ctx_name, member="User1")
    assert "User1 in Hell" in ctx_name.text()

    # Unknown user
    ctx_unknown = FakeContext(bot, FakeAuthor(uid=100, name="Spectator"))
    call(cog, "prefix_user", ctx_unknown, member="NonexistentUser")
    assert "no recorded time" in ctx_unknown.text()


def test_dm_errors_lookup(wired):
    cog, bot, _text, _voice = wired

    # Missing arg
    ctx_none = FakeContext(bot, FakeAuthor(uid=100))
    call(cog, "prefix_errors", ctx_none, code=None)
    assert "specify an error code" in ctx_none.text()

    # Valid code
    ctx_valid = FakeContext(bot, FakeAuthor(uid=100))
    call(cog, "prefix_errors", ctx_valid, code="HEL-100")
    assert "Error code HEL-100" in ctx_valid.text()

    # Bare number
    ctx_num = FakeContext(bot, FakeAuthor(uid=100))
    call(cog, "prefix_errors", ctx_num, code="100")
    assert "Error code HEL-100" in ctx_num.text()

    # Unknown code
    ctx_unk = FakeContext(bot, FakeAuthor(uid=100))
    call(cog, "prefix_errors", ctx_unk, code="HEL-99999")
    assert "Unknown code" in ctx_unk.text()


def test_dm_help(wired):
    cog, bot, _text, _voice = wired
    ctx = FakeContext(bot, FakeAuthor(uid=100))
    call(cog, "prefix_help", ctx)
    text = ctx.text()
    assert "!status" in text
    assert "!leaderboard" in text
    assert "!mystats" in text
    assert "!doctor" in text
    assert "Event Rules" in text


def test_dm_difficulty_and_gamble(wired, engine):
    cog, bot, _text, _voice = wired
    now = now_ts()
    start(engine, now - 10 * 3600, 100)
    engine.tick(obs(now, 100))

    # Difficulty lookup
    ctx_diff = FakeContext(bot, FakeAuthor(uid=100))
    call(cog, "prefix_difficulty", ctx_diff)
    assert "DIFFICULTIES" in ctx_diff.text()

    # Gamble when locked (level 0)
    ctx_gamble = FakeContext(bot, FakeAuthor(uid=100))
    call(cog, "prefix_gamble", ctx_gamble)
    assert "locked" in ctx_gamble.text().lower()


def test_dm_odds_commands_show_the_current_card(wired, engine):
    cog, bot, _text, _voice = wired
    now = now_ts()
    start(engine, now - 100 * 3600, 100)   # 100h elapsed -> Difficulty 3 / Torment
    engine.tick(obs(now, 100))

    ctx = FakeContext(bot, FakeAuthor(uid=100))
    call(cog, "prefix_odds", ctx)
    text = ctx.text()
    assert "GAMBLE ODDS" in text
    assert "38%" in text and "1.5x" in text      # 15m chip at D3
    assert "2.0x" in text                          # max bet at D3

    # `!gamble odds` is a shortcut for the same card.
    ctx2 = FakeContext(bot, FakeAuthor(uid=100))
    call(cog, "prefix_gamble", ctx2, hours="odds")
    assert "GAMBLE ODDS" in ctx2.text()


def test_dm_odds_when_gambling_is_locked(wired, engine):
    cog, bot, _text, _voice = wired
    now = now_ts()
    start(engine, now - 10 * 3600, 100)   # 10h elapsed -> Difficulty 0
    engine.tick(obs(now, 100))

    ctx = FakeContext(bot, FakeAuthor(uid=100))
    call(cog, "prefix_odds", ctx)
    text = ctx.text().lower()
    assert "locked" in text
    assert "96h" in text


def test_dm_dump_sends_sql_and_recap_to_the_operator_only(wired, config):
    cog, bot, _text, _voice = wired

    # Outsiders get the operator gate, even in a DM.
    ctx_outsider = FakeContext(bot, FakeAuthor(uid=12345))
    call(cog, "prefix_dump", ctx_outsider)
    assert "operator" in ctx_outsider.text().lower()
    assert ctx_outsider.sent[0].get("files") is None

    # The operator gets the two attachments: restorable SQL + human recap.
    ctx_op = FakeContext(bot, FakeAuthor(uid=config.log_dm_user_id))
    call(cog, "prefix_dump", ctx_op)
    sent = ctx_op.sent
    assert len(sent) == 1
    files = sent[0].get("files")
    assert files is not None and len(files) == 2
    names = [f.filename for f in files]
    assert any(n.startswith("hellbot-database-") and n.endswith(".sql") for n in names)
    assert any(n.startswith("hellbot-recap-") and n.endswith(".txt") for n in names)
    assert "database dump ready" in sent[0]["content"].lower()


def test_dm_dump_refused_outside_a_dm(wired, config):
    cog, bot, _text, _voice = wired
    ctx_guild = FakeContext(
        bot, FakeAuthor(uid=config.log_dm_user_id), guild_id=1
    )
    call(cog, "prefix_dump", ctx_guild)
    assert "dm" in ctx_guild.text().lower()
    assert ctx_guild.sent[0].get("files") is None


def test_dm_setdifficulty_and_announcedifficulty(wired, config, engine):
    cog, bot, _text, _voice = wired
    now = now_ts()
    start(engine, now, 100)

    # Outsider attempt
    ctx_outsider = FakeContext(bot, FakeAuthor(uid=12345))
    call(cog, "prefix_setdifficulty", ctx_outsider, level="3")
    assert "cannot use this command" in ctx_outsider.text().lower()

    # Host set to level 3
    ctx_host = FakeContext(bot, FakeAuthor(uid=config.log_dm_user_id))
    call(cog, "prefix_setdifficulty", ctx_host, level="3")
    assert "Level 3" in ctx_host.text()
    assert engine.difficulty_override == 3

    # Host broadcast announcement
    with patch.object(cog.announcer, "send", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = MagicMock()
        ctx_ann = FakeContext(bot, FakeAuthor(uid=config.log_dm_user_id))
        call(cog, "prefix_announcedifficulty", ctx_ann)
        assert "posted to" in ctx_ann.text()


# ----------------------------------------------------------- operator / host cmds


def test_dm_restart_operator_vs_outsider(wired, config):
    cog, bot, _text, _voice = wired

    # Non-operator attempt
    ctx_outsider = FakeContext(bot, FakeAuthor(uid=12345))
    call(cog, "prefix_restart", ctx_outsider)
    assert "operator" in ctx_outsider.text().lower()

    # Operator attempt
    ctx_op = FakeContext(bot, FakeAuthor(uid=config.log_dm_user_id))
    with pytest.raises(SystemExit) as exc:
        call(cog, "prefix_restart", ctx_op)
    assert exc.value.code == RESTART_EXIT_CODE
    assert "restart" in ctx_op.text().lower()


def test_dm_doctor_host_vs_outsider(wired, config, engine):
    cog, bot, _text, _voice = wired
    start(engine, now_ts() - 600, 1, 2)

    # Outsider
    ctx_outsider = FakeContext(bot, FakeAuthor(uid=12345))
    call(cog, "prefix_doctor", ctx_outsider)
    assert "cannot use this command" in ctx_outsider.text().lower()

    # Operator
    ctx_op = FakeContext(bot, FakeAuthor(uid=config.log_dm_user_id))
    call(cog, "prefix_doctor", ctx_op)
    assert "State" in ctx_op.text()
    assert "RUNNING" in ctx_op.text()

    # Host role
    ctx_host = FakeContext(bot, FakeAuthor(uid=777, roles=[config.gamenight_host_role_id]))
    call(cog, "prefix_doctor", ctx_host)
    assert "State" in ctx_host.text()


def test_dm_logs_control(wired, config):
    from tests.test_command_flows import StubStream

    cog, bot, _text, _voice = wired
    stream = StubStream()
    bot.log_stream = stream

    ctx_host = FakeContext(bot, FakeAuthor(uid=config.log_dm_user_id))
    call(cog, "prefix_logs", ctx_host, action="status")
    assert "Live log stream" in ctx_host.text()

    call(cog, "prefix_logs", ctx_host, action="off")
    assert stream.enabled is False

    call(cog, "prefix_logs", ctx_host, action="on", level="DEBUG")
    assert stream.enabled is True and stream.level == "DEBUG"

    call(cog, "prefix_logs", ctx_host, action="flush")
    assert stream.flushed == 1

    call(cog, "prefix_logs", ctx_host, action="test")
    assert stream.flushed == 2


def test_dm_security_report(wired, config):
    cog, bot, _text, _voice = wired
    ctx_host = FakeContext(bot, FakeAuthor(uid=config.log_dm_user_id))
    call(cog, "prefix_security", ctx_host)
    assert "Security Report" in ctx_host.text()


def test_dm_export(wired, config, engine):
    cog, bot, _text, _voice = wired
    ctx_empty = FakeContext(bot, FakeAuthor(uid=config.log_dm_user_id))
    call(cog, "prefix_export", ctx_empty)
    assert "nothing to export" in ctx_empty.text()

    start(engine, T0, 1, 2)
    for i in range(1, 61):
        engine.tick(obs(T0 + i, 1, 2))
    ctx_data = FakeContext(bot, FakeAuthor(uid=config.log_dm_user_id))
    call(cog, "prefix_export", ctx_data)
    payload = ctx_data.sent[-1]["file"]
    assert payload.filename.endswith(".csv")


def test_dm_reloadmessages(wired, config):
    cog, bot, _text, _voice = wired
    ctx_host = FakeContext(bot, FakeAuthor(uid=config.log_dm_user_id))
    call(cog, "prefix_reloadmessages", ctx_host)
    assert "reloaded" in ctx_host.text().lower()


def test_dm_alivecheck(wired, config, engine):
    cog, bot, _text, _voice = wired
    ctx_host = FakeContext(bot, FakeAuthor(uid=config.log_dm_user_id))

    # Not running
    call(cog, "prefix_alivecheck", ctx_host)
    assert "No event is running" in ctx_host.text()

    # Running
    start(engine, now_ts(), 1, 2)
    ctx_running = FakeContext(bot, FakeAuthor(uid=config.log_dm_user_id))
    call(cog, "prefix_alivecheck", ctx_running)
    assert cog.monitor.alive_checks.pending is not None
    assert "Alive check posted" in ctx_running.text()


def test_prefix_alivecheck_refused_in_guild_channels(wired, config, engine):
    """The alive check lever is DM-only — a server channel must be refused."""
    cog, bot, _text, _voice = wired
    start(engine, now_ts(), 1, 2)

    ctx_guild = FakeContext(
        bot, FakeAuthor(uid=config.log_dm_user_id), guild_id=123
    )
    call(cog, "prefix_alivecheck", ctx_guild)

    assert TEXT.CMD_DM_ONLY in ctx_guild.text()
    assert cog.monitor.alive_checks.pending is None


def test_dm_pause_and_resume(wired, config, engine):
    cog, bot, _text, _voice = wired
    start(engine, now_ts() - 3600, 1)

    ctx_host = FakeContext(bot, FakeAuthor(uid=config.log_dm_user_id))
    call(cog, "prefix_pause", ctx_host)
    assert engine.is_paused
    assert "paused" in ctx_host.text().lower()

    ctx_res = FakeContext(bot, FakeAuthor(uid=config.log_dm_user_id))
    call(cog, "prefix_resume", ctx_res)
    assert not engine.is_paused
    assert "resumed" in ctx_res.text().lower()


def test_dm_stop_reset_approve_flow(wired, config, engine):
    cog, bot, _text, _voice = wired
    start(engine, now_ts() - 60, 1)

    # Request stop
    ctx_stop = FakeContext(bot, FakeAuthor(uid=config.log_dm_user_id))
    call(cog, "prefix_stop", ctx_stop)
    assert cog.engine.status is EventStatus.RUNNING
    assert "approval" in ctx_stop.text().lower()
    code = _operator_code(bot)

    # Approve with code
    ctx_approve = FakeContext(bot, FakeAuthor(uid=config.log_dm_user_id))
    call(cog, "prefix_approve", ctx_approve, code=code)
    assert cog.engine.status is EventStatus.CANCELLED
    assert "cancelled" in ctx_approve.text().lower()


def test_dm_start(wired, config):
    cog, bot, _text, _voice = wired
    ctx_host = FakeContext(bot, FakeAuthor(uid=config.log_dm_user_id))
    call(cog, "prefix_start", ctx_host)
    assert cog.engine.status is EventStatus.RUNNING
    assert "has started" in ctx_host.text()


# ------------------------------------------------------------- !hell group routing


def test_hell_group_subcommand_dispatching(wired, config, engine):
    cog, bot, _text, _voice = wired

    # !hell (alone) -> status
    ctx_root = FakeContext(bot, FakeAuthor(uid=100))
    call(cog, "prefix_hell_group", ctx_root)
    assert "not running" in ctx_root.text().lower()

    # !hell lb
    ctx_lb = FakeContext(bot, FakeAuthor(uid=100))
    call(cog, "prefix_hell_group", ctx_lb, subcommand="lb")
    assert "Nobody has spent time in Hell yet" in ctx_lb.text()

    # !hell ms
    ctx_ms = FakeContext(bot, FakeAuthor(uid=100))
    call(cog, "prefix_hell_group", ctx_ms, subcommand="ms")
    assert "32h" in ctx_ms.text()

    # !hell help
    ctx_h = FakeContext(bot, FakeAuthor(uid=100))
    call(cog, "prefix_hell_group", ctx_h, subcommand="help")
    assert "!status" in ctx_h.text()

    # !hell mycard
    ctx_card = FakeContext(bot, FakeAuthor(uid=100))
    call(cog, "prefix_hell_group", ctx_card, subcommand="mycard")
    assert "no recorded time" in ctx_card.text().lower()

    # !hell setdiff
    ctx_sd = FakeContext(bot, FakeAuthor(uid=config.log_dm_user_id))
    call(cog, "prefix_hell_group", ctx_sd, subcommand="setdiff", rest="2")
    assert "Level 2" in ctx_sd.text()

    # !hell unknown
    ctx_unk = FakeContext(bot, FakeAuthor(uid=100))
    call(cog, "prefix_hell_group", ctx_unk, subcommand="foobar")
    assert "Unknown subcommand `foobar`" in ctx_unk.text()


# ----------------------------------------------------- prefix command routing


def test_cog_check_allows_dms_and_guild_channels(wired):
    """`!` commands must work in the server too, not only in DMs."""
    cog, bot, _text, _voice = wired
    dm_ctx = FakeContext(bot, FakeAuthor(uid=100), guild_id=None)
    guild_ctx = FakeContext(bot, FakeAuthor(uid=100), guild_id=123)

    assert run(cog.cog_check(dm_ctx)) is True
    assert run(cog.cog_check(guild_ctx)) is True


def test_bot_on_message_routes_prefix_commands_everywhere(wired, config):
    from hell.bot import build_bot

    bot = build_bot(config)
    calls: list[str] = []

    async def fake_process(msg):
        calls.append(msg.content)

    bot.process_commands = fake_process  # type: ignore[assignment]

    # DM message from user -> processed
    dm = MagicMock(spec=discord.Message)
    dm.guild = None
    dm.author.bot = False
    dm.content = "!status"
    run(bot.on_message(dm))
    assert calls == ["!status"]

    # Server/guild message -> processed for prefix commands too (this used to
    # be the bug: `!status` in the server silently did nothing)
    guild_msg = MagicMock(spec=discord.Message)
    guild_msg.guild = MagicMock()
    guild_msg.guild.id = 123
    guild_msg.author.bot = False
    guild_msg.content = "!status"
    run(bot.on_message(guild_msg))
    assert calls == ["!status", "!status"]

    # Bot message -> ignored
    bot_msg = MagicMock(spec=discord.Message)
    bot_msg.guild = None
    bot_msg.author.bot = True
    bot_msg.content = "!status"
    run(bot.on_message(bot_msg))
    assert len(calls) == 2

    bot.store.close()


def test_prefix_commands_actually_run_in_guild_channels(wired, config, monkeypatch):
    """The real discord.py pipeline: `!status` typed in a server must answer.

    This is the regression test for the bug where `!` commands only ever ran
    in DMs and silently did nothing in the server.
    """
    from discord.ext import commands as dpy

    from hell.bot import build_bot

    bot = build_bot(config)
    bot._connection.user = MagicMock(id=999)  # pretend we are logged in

    sent: list[dict] = []

    async def fake_ctx_send(self, content=None, **kwargs):
        sent.append({"content": content, **kwargs})
        return MagicMock()

    monkeypatch.setattr(dpy.Context, "send", fake_ctx_send)

    from hell.cog import HellCommands

    async def add_cog():
        await bot.add_cog(HellCommands(bot, config, bot.engine, bot.monitor))

    run(add_cog())

    member = MagicMock(spec=discord.Member)
    member.id = 42
    member.bot = False
    member.roles = []
    member.display_name = "Host"

    def guild_message(content):
        msg = MagicMock(spec=discord.Message)
        msg.guild = MagicMock()
        msg.guild.id = 1
        msg.author = member
        msg.channel = MagicMock()
        msg.channel.id = 777
        msg.content = content
        return msg

    async def drive(content):
        msg = guild_message(content)
        ctx = await bot.get_context(msg)
        assert ctx.valid, f"`{content}` did not resolve to a command in a guild"
        await bot.invoke(ctx)

    run(drive("!status"))
    assert len(sent) == 1 and sent[0].get("embed") is not None

    sent.clear()
    run(drive("!hell status"))
    assert len(sent) == 1 and sent[0].get("embed") is not None

    sent.clear()
    run(drive("!help"))
    assert len(sent) == 1 and sent[0].get("embed") is not None

    bot.store.close()


def test_bot_on_command_error_friendly_feedback_everywhere(wired, config):
    from hell.bot import build_bot

    bot = build_bot(config)

    # Unknown command in DM
    ctx_dm = FakeContext(bot, FakeAuthor(uid=100))
    ctx_dm.guild = None
    run(bot.on_command_error(ctx_dm, commands.CommandNotFound("unknown")))
    assert "Unknown command" in ctx_dm.text()

    # Unknown command in guild -> also answered (commands work there now)
    ctx_guild = FakeContext(bot, FakeAuthor(uid=100), guild_id=123)
    run(bot.on_command_error(ctx_guild, commands.CommandNotFound("unknown")))
    assert "Unknown command" in ctx_guild.text()

    # Missing arg
    ctx_arg = FakeContext(bot, FakeAuthor(uid=100))
    param = MagicMock()
    param.name = "code"
    run(bot.on_command_error(ctx_arg, commands.MissingRequiredArgument(param)))
    assert "Missing required argument `code`" in ctx_arg.text()

    # Check failure
    ctx_chk = FakeContext(bot, FakeAuthor(uid=100))
    run(bot.on_command_error(ctx_chk, commands.CheckFailure("nope")))
    assert "host" in ctx_chk.text().lower() or "allowed" in ctx_chk.text().lower()

    bot.store.close()
