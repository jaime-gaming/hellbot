"""The `/hell` commands, driven through their real callbacks.

Command bodies were the largest untested surface: everything a host or a
player actually touches lives here.  The Discord objects are fakes, but the
code path is the real one, checks included.
"""

from __future__ import annotations

import asyncio
from typing import Any

import discord
import pytest
from discord.ext import commands

from hell import RESTART_EXIT_CODE
from hell.announcer import Announcer
from hell.cog import HellCommands
from hell.milestones import TOTAL_SECONDS
from hell.models import EventStatus
from hell.monitor import VoiceMonitor
from hell.timeutil import now_ts
from hell.ui import DMsClosed, NotAHost, NotOperator
from tests.conftest import GRACE, T0, obs, start
from tests.test_integration import FakeTextChannel
from tests.test_monitor import FakeMember, FakeVoiceChannel

# --------------------------------------------------------------- fake Discord


class FakeRole:
    def __init__(self, rid: int):
        self.id = rid


class FakeAuthor:
    def __init__(self, uid=42, roles=(), name="Host"):
        self.id = uid
        self.roles = [FakeRole(r) for r in roles]
        self.display_name = name
        self.mention = f"<@{uid}>"
        self.bot = False

    def __str__(self):
        return self.display_name


class FakeResponse:
    def __init__(self):
        self.deferred = False
        self.sent: list[dict] = []
        self.modal = None
        self._done = False

    async def defer(self, **kwargs):
        self.deferred = True
        self._done = True

    async def send_message(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})
        self._done = True

    async def send_modal(self, modal):
        self.modal = modal
        self._done = True

    async def edit_message(self, **kwargs):
        self.sent.append({"edit": True, **kwargs})

    def is_done(self):
        return self._done


class FakeFollowup:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})
        return None


class FakeInteraction:
    def __init__(self, client, user, guild_id=1):
        self.client = client
        self.user = user
        self.guild = type("G", (), {"id": guild_id})()
        self.response = FakeResponse()
        self.followup = FakeFollowup()

    # convenience -----------------------------------------------------
    @property
    def messages(self) -> list[dict]:
        return self.response.sent + self.followup.sent

    def text(self) -> str:
        parts: list[str] = []
        for message in self.messages:
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


class Choice:
    def __init__(self, value):
        self.value = value


class FakeOperator:
    """The account that receives approval codes — records what it was sent."""

    def __init__(self, uid: int):
        self.id = uid
        self.sent_text = ""

    async def send(self, content=None, **kwargs):
        from hell.announcer import embed_to_text

        embed = kwargs.get("embed")
        self.sent_text = embed_to_text(embed) if embed is not None else (content or "")


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------- fixtures


@pytest.fixture
def wired(config, engine):
    text = FakeTextChannel(config.announce_channel_id)
    voice = FakeVoiceChannel([FakeMember(1, "Alice"), FakeMember(2, "Bob")])

    class Bot(commands.Bot):
        def __init__(self):
            intents = discord.Intents.default()
            intents.members = True
            super().__init__(command_prefix="!", intents=intents)
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


@pytest.fixture
def host(config):
    return FakeAuthor(roles=[config.gamenight_host_role_id])


def call(cog, name: str, interaction, **kwargs):
    """Invoke a command's real callback (bypassing Discord's dispatcher)."""
    command = getattr(cog, name)
    return run(command.callback(cog, interaction, **kwargs))


# -------------------------------------------------------------------- checks


def test_host_check_accepts_hosts_and_rejects_everyone_else(wired, config, host):
    cog, bot, _text, _voice = wired
    predicate = cog.start.checks[0]      # the real check attached to /hell start

    allowed = FakeInteraction(bot, host)
    allowed.user = _member(host)
    assert run(predicate(allowed)) is True

    outsider = FakeInteraction(bot, _member(FakeAuthor(uid=7, roles=[999])))
    with pytest.raises(NotAHost):
        run(predicate(outsider))


def test_dm_operator_only_check_rules(wired, config):
    cog, bot, _text, _voice = wired
    predicate = cog.restart.checks[0]

    # In a guild -> DMsClosed
    guild_interaction = FakeInteraction(bot, FakeAuthor(uid=config.log_dm_user_id))
    with pytest.raises(DMsClosed):
        run(predicate(guild_interaction))

    # In DM, non-operator -> NotOperator
    dm_outsider = FakeInteraction(bot, FakeAuthor(uid=12345))
    dm_outsider.guild = None
    with pytest.raises(NotOperator):
        run(predicate(dm_outsider))

    # In DM, operator -> allowed
    dm_op = FakeInteraction(bot, FakeAuthor(uid=config.log_dm_user_id))
    dm_op.guild = None
    assert run(predicate(dm_op)) is True


def test_restart_command_exits_with_restart_code(wired, config):
    cog, bot, _text, _voice = wired
    dm_op = FakeInteraction(bot, FakeAuthor(uid=config.log_dm_user_id))
    dm_op.guild = None

    with pytest.raises(SystemExit) as exc_info:
        call(cog, "restart", dm_op)
    assert exc_info.value.code == RESTART_EXIT_CODE
    assert "restart" in dm_op.text().lower()


def test_cog_app_command_error_handling(wired, config):
    cog, bot, _text, _voice = wired
    interaction = FakeInteraction(bot, FakeAuthor())

    # NotAHost error
    run(cog.cog_app_command_error(interaction, NotAHost("not allowed")))
    assert "not allowed" in interaction.text().lower()

    # DMsClosed error
    interaction2 = FakeInteraction(bot, FakeAuthor())
    run(cog.cog_app_command_error(interaction2, DMsClosed()))
    assert "dm" in interaction2.text().lower()

    # NotOperator error
    interaction3 = FakeInteraction(bot, FakeAuthor())
    run(cog.cog_app_command_error(interaction3, NotOperator()))
    assert "operator" in interaction3.text().lower()


def _member(author):
    """A stand-in that passes `isinstance(user, discord.Member)`."""
    from unittest.mock import MagicMock

    member = MagicMock(spec=discord.Member)
    member.id = author.id
    member.roles = author.roles
    member.mention = author.mention
    member.display_name = author.display_name
    return member


# --------------------------------------------------------------- /hell start


def test_start_launches_the_event_and_announces_it(wired, host):
    cog, bot, text, _voice = wired
    interaction = FakeInteraction(bot, host)

    call(cog, "start", interaction)

    assert cog.engine.status is EventStatus.RUNNING
    assert "has started" in interaction.text()
    posted = "\n".join(str(m.content) for m in text.sent if m.content)
    assert "@everyone" in posted
    assert cog.monitor.alive_checks.next_check_ts() is not None  # roll call scheduled


def test_start_is_rejected_while_an_event_runs(wired, host, engine):
    cog, bot, _text, _voice = wired
    start(engine, now_ts(), 1)
    interaction = FakeInteraction(bot, host)

    call(cog, "start", interaction)

    assert "already RUNNING" in interaction.text()


def test_start_refuses_an_empty_vc(wired, host, voice_empty=None):
    cog, bot, _text, voice = wired
    voice.members = []
    interaction = FakeInteraction(bot, host)

    call(cog, "start", interaction)

    assert cog.engine.status is EventStatus.IDLE
    assert "no valid humans" in interaction.text()


def test_start_reports_an_invisible_channel(wired, host):
    cog, bot, _text, _voice = wired
    cog.monitor.voice_channel = lambda: None  # type: ignore[method-assign]
    interaction = FakeInteraction(bot, host)

    call(cog, "start", interaction)

    assert cog.engine.status is EventStatus.IDLE
    assert "cannot see the target voice channel" in interaction.text()


def test_start_kicks_clankers_before_counting(wired, host, config):
    cog, bot, _text, voice = wired
    clanker = FakeMember(9, "Clank3r", roles=[config.clanker_role_id])
    voice.members = [clanker]
    interaction = FakeInteraction(bot, host)

    call(cog, "start", interaction)

    assert clanker.moved_to == [None]
    assert cog.engine.status is EventStatus.IDLE       # a clanker is not a participant


# -------------------------------------------------------------- /hell status


def test_status_when_idle_explains_how_to_start(wired, host):
    cog, bot, _text, _voice = wired
    interaction = FakeInteraction(bot, host)
    call(cog, "status", interaction)
    assert "not running" in interaction.text().lower()
    assert "/hell start" in interaction.text()


def test_status_shows_the_live_numbers(wired, host, engine):
    cog, bot, _text, _voice = wired
    start(engine, now_ts() - 3600, 1, 2)
    interaction = FakeInteraction(bot, host)

    call(cog, "status", interaction)

    text = interaction.text()
    assert "RUNNING" in text
    assert "1h 00m" in text and "160h 00m" in text
    assert "Alive checks" in text


# --------------------------------------------------------- /hell leaderboard


def test_leaderboard_empty_and_populated(wired, host, engine):
    cog, bot, _text, _voice = wired

    empty = FakeInteraction(bot, host)
    call(cog, "leaderboard", empty)
    assert "Nobody has spent time in Hell yet" in empty.text()

    start(engine, T0, 1, 2)
    for i in range(1, 61):
        engine.tick(obs(T0 + i, 1, 2))
    filled = FakeInteraction(bot, host)
    call(cog, "leaderboard", filled)
    assert "🥇" in filled.text()


def test_leaderboard_says_when_it_is_frozen(wired, host, engine):
    cog, bot, _text, _voice = wired
    start(engine, T0, 1)
    engine.tick(obs(T0 + 10, 1))
    engine.tick(obs(T0 + 11))
    engine.tick(obs(T0 + 11 + GRACE))                  # FAILED
    interaction = FakeInteraction(bot, host)

    call(cog, "leaderboard", interaction)

    assert "frozen" in interaction.text().lower()


# -------------------------------------------------------- /hell milestones


def test_milestones_lists_all_five_with_state(wired, host, engine):
    cog, bot, _text, _voice = wired
    start(engine, T0, 1)
    engine.tick(obs(T0 + 32 * 3600, 1))                # first milestone reached
    interaction = FakeInteraction(bot, host)

    call(cog, "milestones", interaction)

    text = interaction.text()
    for hours in (32, 64, 96, 128, 160):
        assert f"{hours}h" in text
    assert "✅ reached" in text
    assert "⏳ in" in text


# ------------------------------------------------------------ /hell mystats


def test_mystats_needs_recorded_time(wired, host):
    cog, bot, _text, _voice = wired
    interaction = FakeInteraction(bot, host)
    call(cog, "mystats", interaction)
    assert "no recorded time" in interaction.text()


def test_mystats_returns_the_card(wired, engine):
    cog, bot, _text, _voice = wired
    start(engine, T0, 1)
    for i in range(1, 61):
        engine.tick(obs(T0 + i, 1))
    interaction = FakeInteraction(bot, FakeAuthor(uid=1))

    call(cog, "mystats", interaction)

    text = interaction.text()
    assert "WELCOME TO HELL" in text and "SURVIVED" in text


# ---------------------------------------------------------- /hell alivecheck


def test_alivecheck_requires_a_running_event(wired, host):
    cog, bot, _text, _voice = wired
    interaction = FakeInteraction(bot, host)
    call(cog, "alivecheck", interaction)
    assert "No event is running" in interaction.text()


def test_alivecheck_posts_a_roll_call(wired, host, engine):
    cog, bot, _text, _voice = wired
    start(engine, now_ts(), 1, 2)
    interaction = FakeInteraction(bot, host)

    call(cog, "alivecheck", interaction)

    assert cog.monitor.alive_checks.pending is not None
    assert "Alive check posted" in interaction.text()


def test_alivecheck_refuses_a_second_one(wired, host, engine):
    cog, bot, _text, _voice = wired
    start(engine, now_ts(), 1)
    call(cog, "alivecheck", FakeInteraction(bot, host))
    second = FakeInteraction(bot, host)

    call(cog, "alivecheck", second)

    assert "already running" in second.text()


def test_alivecheck_respects_the_switch(wired, host, engine, config):
    cog, bot, _text, _voice = wired
    config.alive_check_enabled = False
    start(engine, now_ts(), 1)
    interaction = FakeInteraction(bot, host)

    call(cog, "alivecheck", interaction)

    assert "disabled" in interaction.text()


# ---------------------------------------------------------------- /hell logs


class StubStream:
    def __init__(self):
        self.enabled = True
        self.level = "INFO"
        self.flushed = 0
        self.running = True

    def set_enabled(self, value):
        self.enabled = value

    def set_level(self, value):
        self.level = value

    async def flush(self):
        self.flushed += 1
        return 2

    async def start(self):
        self.running = True

    def status(self):
        return f"on ({self.level})"


def test_logs_command_controls_the_stream(wired, host):
    cog, bot, _text, _voice = wired
    stream = StubStream()
    bot.log_stream = stream

    status = FakeInteraction(bot, host)
    call(cog, "logs", status, action=Choice("status"))
    assert "Live log stream" in status.text()

    off = FakeInteraction(bot, host)
    call(cog, "logs", off, action=Choice("off"))
    assert stream.enabled is False

    on = FakeInteraction(bot, host)
    call(cog, "logs", on, action=Choice("on"), level=Choice("DEBUG"))
    assert stream.enabled is True and stream.level == "DEBUG"

    flush = FakeInteraction(bot, host)
    call(cog, "logs", flush, action=Choice("flush"))
    assert stream.flushed == 1 and "Flushed **2**" in flush.text()

    test = FakeInteraction(bot, host)
    call(cog, "logs", test, action=Choice("test"))
    assert stream.flushed == 2 and "Test line sent" in test.text()


def test_logs_command_without_a_stream(wired, host):
    cog, bot, _text, _voice = wired
    bot.log_stream = None
    interaction = FakeInteraction(bot, host)
    call(cog, "logs", interaction, action=Choice("status"))
    assert "not available" in interaction.text()


# -------------------------------------------------------------- /hell doctor


def test_doctor_reports_state_and_configuration(wired, host, engine):
    cog, bot, _text, _voice = wired
    cog.config.token = "super-secret-token-value"
    bot.log_stream = StubStream()
    start(engine, now_ts() - 600, 1, 2)
    interaction = FakeInteraction(bot, host)

    call(cog, "doctor", interaction)

    text = interaction.text()
    assert "Status:" in text and "RUNNING" in text
    assert "Background tasks" in text
    assert "Empty-VC grace" in text
    assert cog.config.token not in text          # never leaks the token


# ------------------------------------------------------- /hell reloadmessages


def test_reloadmessages_reports_success(wired, host):
    cog, bot, _text, _voice = wired
    interaction = FakeInteraction(bot, host)
    call(cog, "reloadmessages", interaction)
    assert "reloaded" in interaction.text().lower()


def test_reloadmessages_reports_a_broken_file(wired, host, tmp_path, monkeypatch):
    from hell import texts

    cog, bot, _text, _voice = wired
    broken = tmp_path / "Announcements.py"
    broken.write_text("MILESTONES = (oops\n", encoding="utf-8")
    monkeypatch.setattr(texts, "_candidates", lambda: [broken])
    interaction = FakeInteraction(bot, host)

    call(cog, "reloadmessages", interaction)

    assert "could not be loaded" in interaction.text()
    texts.load(force=True)


# ---------------------------------------------------------------- /hell stop


def test_stop_needs_a_running_event(wired, host):
    cog, bot, _text, _voice = wired
    interaction = FakeInteraction(bot, host)
    call(cog, "stop", interaction)
    assert "Nothing to stop" in interaction.text()


def _operator_code(bot) -> str:
    """The 6-character approval code from the operator's DM."""
    import re

    text = bot.operator.sent_text
    match = re.search(r"\*\*Code:\*\* `([A-Z2-9]{6})`", text)
    assert match, f"no code in operator DM: {text!r}"
    return match.group(1)


def test_stop_requires_the_operators_approval_code(wired, host, engine):
    cog, bot, _text, _voice = wired
    start(engine, now_ts() - 60, 1)
    interaction = FakeInteraction(bot, host)

    call(cog, "stop", interaction)

    assert cog.engine.status is EventStatus.RUNNING      # nothing happened yet
    assert "approval" in interaction.text().lower()      # the host is told
    assert bot.operator.sent_text                        # the code was DM'd
    assert len(_operator_code(bot)) == 6


def test_stop_is_cancelled_only_with_the_right_code(wired, host, engine):
    cog, bot, _text, _voice = wired
    start(engine, now_ts() - 60, 1)
    call(cog, "stop", FakeInteraction(bot, host))
    code = _operator_code(bot)

    wrong = FakeInteraction(bot, host)
    call(cog, "approve", wrong, code="ZZZZZZ")
    assert cog.engine.status is EventStatus.RUNNING
    assert "not correct" in wrong.text()

    right = FakeInteraction(bot, host)
    call(cog, "approve", right, code=code.lower())       # case-insensitive
    assert cog.engine.status is EventStatus.CANCELLED
    assert "cancelled" in right.text().lower()

    again = FakeInteraction(bot, host)
    call(cog, "approve", again, code=code)               # single-use
    assert "nothing is waiting" in again.text().lower()


def test_stop_does_nothing_until_approved(wired, host, engine):
    cog, bot, _text, _voice = wired
    start(engine, now_ts() - 60, 1)
    interaction = FakeInteraction(bot, host)

    call(cog, "stop", interaction)                       # code sent, never entered

    assert cog.engine.status is EventStatus.RUNNING


def test_stale_approval_code_cannot_act_on_a_new_event(wired, host, engine):
    """An approval issued for event A must never cancel a NEW event B that is
    running by the time the code is entered."""
    cog, bot, _text, _voice = wired
    start(engine, now_ts() - 60, 1)                      # event A
    call(cog, "stop", FakeInteraction(bot, host))
    code = _operator_code(bot)

    engine.cancel()                                      # A ends...
    start(engine, now_ts() + 5, 2)                       # ...and B starts

    approve = FakeInteraction(bot, host)
    call(cog, "approve", approve, code=code)

    assert cog.engine.status is EventStatus.RUNNING      # B untouched
    assert "event changed" in approve.text().lower()
    assert cog.code_gate.pending_action is None          # stale code invalidated


def test_stat_cards_are_pinned_to_the_event_they_belong_to(wired, engine):
    """If a new event starts while the old one's stat cards are pending, the
    cards must still be built from the OLD event's leaderboard — never the
    new event's (empty) data."""
    cog, _bot, _text, _voice = wired
    start(engine, T0, 1, 2)                              # event A
    for i in range(1, 61):
        engine.tick(obs(T0 + i, 1, 2))
    engine.tick(obs(T0 + 61))                            # VC empties
    engine.tick(obs(T0 + 61 + GRACE))                    # A FAILED, board frozen
    uid_a = engine.event_uid
    assert engine.status.is_terminal
    assert {e.user_id for e in engine.leaderboard()} == {1, 2}

    start(engine, T0 + 1000, 3)                          # event B, different users
    assert engine.event_uid != uid_a

    dm = cog.monitor.reports
    reports_a = dm.reports(uid_a)                        # pinned to A
    assert {r.user_id for r in reports_a} == {1, 2}
    pending_a = dm.pending(uid_a)
    assert {r.user_id for r in pending_a} == {1, 2}      # nobody from B leaks in
    assert {r.user_id for r in dm.reports()} == {3}      # current event is B only


def test_approval_code_expires(wired, host, engine):
    import time

    cog, bot, _text, _voice = wired
    start(engine, now_ts() - 60, 1)
    call(cog, "stop", FakeInteraction(bot, host))
    cog.code_gate._pending.issued_at = time.time() - 10_000   # made stale

    approve = FakeInteraction(bot, host)
    call(cog, "approve", approve, code=_operator_code(bot))

    assert cog.engine.status is EventStatus.RUNNING
    assert "expired" in approve.text().lower()


class _Resp:
    status = 403
    reason = "Forbidden"


def test_stop_approval_dm_failure_aborts(wired, host, engine, monkeypatch):
    cog, bot, _text, _voice = wired
    start(engine, now_ts() - 60, 1)

    async def broken_send(*_a, **_k):
        raise discord.Forbidden(_Resp(), "DMs closed")

    monkeypatch.setattr(bot.operator, "send", broken_send)
    interaction = FakeInteraction(bot, host)

    call(cog, "stop", interaction)

    assert cog.engine.status is EventStatus.RUNNING
    assert "could not send the approval code" in interaction.text().lower()
    assert cog.code_gate.pending_action is None          # the code was invalidated


# --------------------------------------------------------------- /hell reset


def test_reset_requires_the_operators_approval_code(wired, host, engine):
    cog, bot, _text, _voice = wired
    start(engine, T0, 1)
    interaction = FakeInteraction(bot, host)

    call(cog, "reset", interaction)
    assert cog.engine.status is EventStatus.RUNNING      # not reset yet
    assert "approval" in interaction.text().lower()
    code = _operator_code(bot)

    wrong = FakeInteraction(bot, host)
    call(cog, "approve", wrong, code="WRONG1")
    assert cog.engine.status is EventStatus.RUNNING

    right = FakeInteraction(bot, host)
    call(cog, "approve", right, code=code)
    assert cog.engine.status is EventStatus.IDLE
    assert "reset" in right.text().lower()
    assert cog.monitor.alive_checks.pending is None


# --------------------------------------------------------------- error paths


def test_error_handler_explains_a_missing_role(wired, host):
    cog, bot, _text, _voice = wired
    interaction = FakeInteraction(bot, host)

    run(cog.cog_app_command_error(interaction, NotAHost("⛔ nope")))

    assert "nope" in interaction.text()


def test_error_handler_hides_internal_failures(wired, host, caplog):
    import logging

    cog, bot, _text, _voice = wired
    interaction = FakeInteraction(bot, host)

    with caplog.at_level(logging.ERROR, logger="hell.commands"):
        run(cog.cog_app_command_error(interaction, RuntimeError("boom")))

    assert "Something went wrong" in interaction.text()
    assert "boom" in caplog.text                       # the detail goes to the log
    assert "boom" not in interaction.text()            # not to the user


# ---------------------------------------------------------------- /hell help


def test_help_lists_every_command_split_by_permission(wired, host):
    cog, bot, _text, _voice = wired
    interaction = FakeInteraction(bot, host)

    call(cog, "help", interaction)

    text = interaction.text()
    for name in ("start", "status", "leaderboard", "user", "export", "doctor"):
        assert f"/hell {name}" in text
    assert "Anyone can use" in text
    assert "only" in text                      # the host-restricted section
    assert "alive checks" in text.lower()


# ------------------------------------------------------------- /hell broadcast


def test_broadcast_posts_a_colored_embed(wired, host):
    cog, bot, text, _voice = wired
    interaction = FakeInteraction(bot, host)

    call(cog, "broadcast", interaction, message="Heads up from the hosts", level=Choice("warning"), target=Choice("announcements"))

    assert "Broadcast sent" in interaction.text()
    assert "WARNING" in interaction.text()
    assert text.sent and len(text.sent) == 1
    from hell.announcer import embed_to_text

    rendered = embed_to_text(text.sent[0].embeds[0])
    assert "Heads up from the hosts" in rendered
    assert "WARNING" in rendered


def test_broadcast_requires_host_role(wired):
    cog, bot, _text, _voice = wired
    interaction = FakeInteraction(bot, FakeAuthor(uid=99, roles=[]))
    call(cog, "broadcast", interaction, message="spam")
    assert "cannot use this command" in interaction.text().lower()


# ----------------------------------------------------------- Hell 2 resume


def test_resume_completed_run_after_yes_vote_starts_hell_2(wired, host, engine):
    cog, bot, _text, _voice = wired
    start(engine, T0, 1, 2)
    engine.tick(obs(T0 + TOTAL_SECONDS, 1, 2))
    assert engine.status is EventStatus.COMPLETED

    uid = engine.event_uid
    engine.store.record_continuation_vote(uid, 1, "yes", T0)
    engine.store.record_continuation_vote(uid, 2, "yes", T0)
    engine.store.record_continuation_vote(uid, 3, "no", T0)
    engine.store.set_continuation_poll(uid, status="closed", result="yes")

    interaction = FakeInteraction(bot, host)
    call(cog, "resume", interaction)

    assert engine.status is EventStatus.RUNNING
    assert engine.is_continuation
    assert engine.state.total_seconds == 320 * 3600
    assert not engine.state.milestones_enabled
    assert "HELL 2" in interaction.text()


def test_resume_completed_run_is_refused_without_yes_majority(wired, host, engine):
    cog, bot, _text, _voice = wired
    start(engine, T0, 1, 2)
    engine.tick(obs(T0 + TOTAL_SECONDS, 1, 2))
    engine.store.record_continuation_vote(engine.event_uid, 1, "no", T0)
    engine.store.record_continuation_vote(engine.event_uid, 2, "no", T0)
    engine.store.set_continuation_poll(engine.event_uid, status="closed", result="no")

    interaction = FakeInteraction(bot, host)
    call(cog, "resume", interaction)

    assert engine.status is EventStatus.COMPLETED
    assert "cannot resume" in interaction.text().lower()


# ---------------------------------------------------------------- /hell user


def test_user_reports_time_rank_and_milestones(wired, engine):
    cog, bot, _text, _voice = wired
    start(engine, T0, 1, 2)
    for i in range(1, 61):
        engine.tick(obs(T0 + i, 1, 2))
    engine.tick(obs(T0 + 32 * 3600, 1, 2))     # a milestone both were present for

    interaction = FakeInteraction(bot, FakeAuthor(uid=1, name="Alice"))
    call(cog, "user", interaction)

    text = interaction.text()
    assert "Alice in Hell" in text
    assert "Rank" in text and "#1" in text
    assert "32h" in text                        # the milestone they claimed
    assert "Share of the event" in text


def test_user_without_time_is_told_so(wired, host):
    cog, bot, _text, _voice = wired
    interaction = FakeInteraction(bot, host)
    call(cog, "user", interaction)
    assert "no recorded time" in interaction.text()


def test_user_can_look_up_somebody_else(wired, engine):
    cog, bot, _text, _voice = wired
    start(engine, T0, 1, 2)
    for i in range(1, 31):
        engine.tick(obs(T0 + i, 1, 2))

    other = FakeAuthor(uid=2, name="Bob")
    interaction = FakeInteraction(bot, FakeAuthor(uid=1, name="Alice"))
    call(cog, "user", interaction, member=other)

    assert "Bob in Hell" in interaction.text()


# -------------------------------------------------------------- /hell export


def test_export_produces_a_csv_of_the_leaderboard(wired, engine):
    cog, bot, _text, _voice = wired
    start(engine, T0, 1, 2)
    for i in range(1, 61):
        engine.tick(obs(T0 + i, 1, 2))
    engine.tick(obs(T0 + 32 * 3600, 1))

    interaction = FakeInteraction(bot, FakeAuthor(uid=9))
    call(cog, "export", interaction)

    payload = interaction.followup.sent[-1]
    attachment = payload["file"]
    assert attachment.filename.endswith(".csv")
    body = attachment.fp.read().decode("utf-8")
    assert body.splitlines()[0] == "rank,user_id,display_name,seconds,time,milestones"
    assert "32h" in body                        # milestone column filled in
    assert "User1" in body and "User2" in body


def test_export_says_when_there_is_nothing_to_export(wired, host):
    cog, bot, _text, _voice = wired
    interaction = FakeInteraction(bot, host)
    call(cog, "export", interaction)
    assert "nothing to export" in interaction.text()


# ------------------------------------------------------------ /hell logs tail


def test_logs_tail_shows_recent_lines_without_dms(wired, host):
    import logging

    cog, bot, _text, _voice = wired
    stream = StubStream()
    stream.lines = ["12:00:00 • [monitor] Alice joined the VC"]
    stream.tail = lambda limit=20: stream.lines  # type: ignore[assignment]
    bot.log_stream = stream

    interaction = FakeInteraction(bot, host)
    call(cog, "logs", interaction, action=Choice("tail"))

    assert "Alice joined the VC" in interaction.text()
    logging.getLogger("hell.test").debug("noop")


def test_logs_tail_when_nothing_has_happened(wired, host):
    cog, bot, _text, _voice = wired
    stream = StubStream()
    stream.tail = lambda limit=20: []  # type: ignore[assignment]
    bot.log_stream = stream

    interaction = FakeInteraction(bot, host)
    call(cog, "logs", interaction, action=Choice("tail"))

    assert "Nothing buffered" in interaction.text()


def test_every_command_use_is_logged(wired, host, caplog):
    """An audit trail of who ran what, for after-the-fact questions."""
    import logging

    cog, bot, _text, _voice = wired
    interaction = FakeInteraction(bot, host)
    interaction.command = type("C", (), {"name": "status"})()

    with caplog.at_level(logging.INFO, logger="hell.commands"):
        run(cog.interaction_check(interaction))

    assert "/hell status by" in caplog.text
    assert str(host.id) in caplog.text


# -------------------------------------------------------------- /hell pause


def test_pause_freezes_and_resume_continues(wired, host, engine):
    cog, bot, _text, _voice = wired
    start(engine, now_ts() - 3600, 1)              # 1h in
    pause = FakeInteraction(bot, host)

    call(cog, "pause", pause)

    assert engine.is_paused
    assert "paused" in pause.text().lower()
    frozen = engine.elapsed()
    engine.tick(obs(now_ts(), 1))                  # inert while paused
    assert engine.elapsed() == frozen

    resume = FakeInteraction(bot, host)
    call(cog, "resume", resume)

    assert not engine.is_paused
    assert "resumed" in resume.text().lower()


def test_pause_is_refused_when_nothing_runs(wired, host):
    cog, bot, _text, _voice = wired
    interaction = FakeInteraction(bot, host)

    call(cog, "pause", interaction)

    assert "not running" in interaction.text().lower()
    assert not engine_is_paused(cog)


def engine_is_paused(cog):
    return cog.engine.is_paused


def test_resume_is_refused_when_not_paused(wired, host, engine):
    cog, bot, _text, _voice = wired
    start(engine, now_ts() - 60, 1)
    interaction = FakeInteraction(bot, host)

    call(cog, "resume", interaction)

    assert "not paused" in interaction.text().lower()
    assert not engine_is_paused(cog)


def test_resume_failed_run_requires_mfa(wired, host, engine):
    cog, bot, _text, _voice = wired
    start(engine, now_ts() - 60, 1)
    # Fail the event by emptying the VC
    engine.tick(obs(now_ts() - 30))
    engine.tick(obs(now_ts() - 30 + GRACE))
    assert cog.engine.status is EventStatus.FAILED

    # Host requests resume -> approval code sent to operator
    interaction = FakeInteraction(bot, host)
    call(cog, "resume", interaction)
    assert "approval" in interaction.text().lower()
    assert cog.engine.status is EventStatus.FAILED
    assert bot.operator.sent_text
    code = _operator_code(bot)

    # Wrong code rejected
    wrong = FakeInteraction(bot, host)
    call(cog, "approve", wrong, code="WRONG1")
    assert cog.engine.status is EventStatus.FAILED
    assert "not correct" in wrong.text()

    # Right code approves and resumes
    right = FakeInteraction(bot, host)
    call(cog, "approve", right, code=code)
    assert cog.engine.status is EventStatus.RUNNING
    assert "resumed" in right.text().lower()


def test_resume_is_refused_when_idle(wired, host):
    cog, bot, _text, _voice = wired
    interaction = FakeInteraction(bot, host)

    call(cog, "resume", interaction)

    assert "not running" in interaction.text().lower()


def test_pause_cancels_a_pending_alive_check(wired, host, engine):
    cog, bot, _text, _voice = wired
    start(engine, now_ts(), 1, 2)
    call(cog, "alivecheck", FakeInteraction(bot, host))
    assert cog.monitor.alive_checks.pending is not None

    call(cog, "pause", FakeInteraction(bot, host))

    assert cog.monitor.alive_checks.pending is None   # cancelled, never enforced


def test_alivecheck_refuses_while_paused(wired, host, engine):
    cog, bot, _text, _voice = wired
    start(engine, now_ts(), 1)
    call(cog, "pause", FakeInteraction(bot, host))

    interaction = FakeInteraction(bot, host)
    call(cog, "alivecheck", interaction)

    assert "paused" in interaction.text().lower()
