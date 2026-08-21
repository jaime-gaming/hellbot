"""Startup preflight (`hell/health.py`) and the roll-call Discord I/O.

These are the modules that tell an operator *why* something is broken, so they
had better be right themselves.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import discord

from hell.aliveio import DiscordAliveCheckIO
from hell.health import preflight
from tests.test_monitor import FakeMember


def run(coro):
    return asyncio.run(coro)


def perms(**overrides) -> discord.Permissions:
    values = dict(
        view_channel=True,
        send_messages=True,
        embed_links=True,
        read_message_history=True,
        mention_everyone=True,
        manage_messages=True,
        move_members=True,
        add_reactions=True,
    )
    values.update(overrides)
    return discord.Permissions(**values)


def make_voice(config, *, permissions=None, members=()):
    vc = MagicMock(spec=discord.VoiceChannel)
    vc.id = config.voice_channel_id
    vc.name = "hell"
    vc.members = list(members)
    vc.permissions_for.return_value = permissions or perms()
    return vc


def make_text(config, *, permissions=None):
    chan = MagicMock(spec=discord.TextChannel)
    chan.id = config.announce_channel_id
    chan.name = "hell-progress"
    chan.permissions_for.return_value = permissions or perms()
    return chan


def make_guild(config, *, voice=None, text=None, roles=None):
    guild = MagicMock(spec=discord.Guild)
    guild.id = config.guild_id
    guild.name = "Test Server"
    guild.me = MagicMock(spec=discord.Member)

    channels = {}
    if voice is not None:
        channels[voice.id] = voice
    if text is not None:
        channels[text.id] = text
    guild.get_channel.side_effect = channels.get

    known = roles if roles is not None else {
        config.gamenight_host_role_id: "gamenight host",
        config.clanker_role_id: "clanker",
    }

    def get_role(rid):
        if rid in known:
            role = MagicMock(spec=discord.Role)
            role.name = known[rid]
            return role
        return None

    guild.get_role.side_effect = get_role
    return guild


class HealthBot:
    def __init__(self, guild=None, *, members=True, voice_states=True, message_content=True):
        self._guild = guild
        self.intents = discord.Intents.default()
        self.intents.members = members
        self.intents.voice_states = voice_states
        self.intents.message_content = message_content

    def get_guild(self, _gid):
        return self._guild


# ------------------------------------------------------------------ preflight


def test_a_healthy_server_passes(config):
    voice, text = make_voice(config, members=[FakeMember(1, "Alice")]), make_text(config)
    report = run(preflight(HealthBot(make_guild(config, voice=voice, text=text)), config))
    assert report.ok
    assert not report.errors
    assert any("Test Server" in line for line in report.info)
    assert any("1 member(s) inside" in line for line in report.info)


def test_missing_guild_is_the_first_thing_reported(config):
    report = run(preflight(HealthBot(None), config))
    assert not report.ok
    assert "GUILD_ID" in report.errors[0]


def test_missing_move_members_is_an_error(config):
    voice = make_voice(config, permissions=perms(move_members=False))
    report = run(preflight(HealthBot(make_guild(config, voice=voice, text=make_text(config))), config))
    assert not report.ok
    assert any("Move Members" in e for e in report.errors)


def test_missing_message_content_intent_is_an_error(config):
    """Without it the bot cannot read alive-check replies — every roll call
    would end by disconnecting everyone."""
    guild = make_guild(config, voice=make_voice(config), text=make_text(config))
    report = run(preflight(HealthBot(guild, message_content=False), config))
    assert not report.ok
    assert any("Message Content" in e for e in report.errors)


def test_missing_mention_everyone_is_only_a_warning(config):
    text = make_text(config, permissions=perms(mention_everyone=False))
    report = run(preflight(HealthBot(make_guild(config, voice=make_voice(config), text=text)), config))
    assert report.ok                                    # the event can still run
    assert any("Mention @everyone" in w for w in report.warnings)


def test_unreadable_announcement_channel_is_fatal(config):
    text = make_text(config, permissions=perms(send_messages=False, embed_links=False))
    report = run(preflight(HealthBot(make_guild(config, voice=make_voice(config), text=text)), config))
    assert not report.ok
    assert any("Send Messages" in e for e in report.errors)
    assert any("Embed Links" in e for e in report.errors)


def test_missing_channels_and_roles_are_reported(config):
    guild = make_guild(config, voice=None, text=None, roles={})
    report = run(preflight(HealthBot(guild), config))
    joined = " ".join(report.errors)
    assert "Voice channel" in joined
    assert "Announcement channel" in joined
    assert "GAMENIGHT_HOST_ROLE_ID" in joined
    assert "CLANKER_ROLE_ID" in joined


def test_disabled_intents_are_reported(config):
    guild = make_guild(config, voice=make_voice(config), text=make_text(config))
    report = run(preflight(HealthBot(guild, members=False, voice_states=False), config))
    assert any("Server Members intent" in e for e in report.errors)
    assert any("Voice States intent" in e for e in report.errors)


def test_optional_reward_roles_only_warn(config):
    config.hell_role_id = 4242
    guild = make_guild(config, voice=make_voice(config), text=make_text(config))
    report = run(preflight(HealthBot(guild), config))
    assert report.ok
    assert any("HELL_ROLE_ID" in w for w in report.warnings)


def test_alive_check_channel_is_checked_too(config):
    voice = make_voice(config, permissions=perms(send_messages=False, read_message_history=False))
    guild = make_guild(config, voice=voice, text=make_text(config))
    report = run(preflight(HealthBot(guild), config))
    assert any("alive-check channel" in e for e in report.errors)
    assert any("Read Message History" in w for w in report.warnings)


def test_report_renders_as_text(config):
    report = run(preflight(HealthBot(None), config))
    assert "ERROR" in report.as_text()
    report.log()  # must not raise


# ---------------------------------------------------------------- alive I/O


class _Resp:
    status = 403
    reason = "Forbidden"


class IOChannel(discord.abc.Messageable):
    """A channel that records sends and can replay history."""

    async def _get_channel(self):
        return self

    def __init__(self, cid=1539756705997652079, *, forbidden=False):
        self.id = cid
        self.sent: list[str] = []
        self.forbidden = forbidden
        self.past: list[tuple[int, str]] = []

    async def send(self, content=None, **kwargs):
        if self.forbidden:
            raise discord.Forbidden(_Resp(), "no")
        self.sent.append(content or "")
        message = MagicMock()
        message.id = 500 + len(self.sent)
        message.channel = self
        return message

    def history(self, **_kwargs):
        entries = list(self.past)

        class _Iter:
            def __aiter__(self_inner):
                self_inner._it = iter(entries)
                return self_inner

            async def __anext__(self_inner):
                try:
                    uid, content = next(self_inner._it)
                except StopIteration:
                    raise StopAsyncIteration from None
                message = MagicMock()
                message.author.id = uid
                message.content = content
                return message

        return _Iter()


class IOBot:
    def __init__(self, channel=None, guild=None, voice=None):
        self._channel = channel
        self._guild = guild
        self._voice = voice

    def get_channel(self, cid):
        if self._voice is not None and cid == self._voice.id:
            return self._voice
        return self._channel

    def get_guild(self, _gid):
        return self._guild

    async def fetch_channel(self, cid):
        raise discord.NotFound(MagicMock(status=404), f"no channel {cid}")


def test_roll_call_defaults_to_the_vc_text_chat(config):
    io = DiscordAliveCheckIO(IOBot(), config)
    assert io.channel_id() == config.voice_channel_id
    config.alive_check_channel_id = 777
    assert io.channel_id() == 777


def test_send_check_pings_everyone_listed(config):
    channel = IOChannel()
    io = DiscordAliveCheckIO(IOBot(channel), config)

    result = run(io.send_check("🚨 ARE YOU ALIVE? Say: Yes", [1, 2, 3]))

    assert result is not None
    assert "<@1> <@2> <@3>" in channel.sent[0]
    assert "ARE YOU ALIVE" in channel.sent[0]


def test_send_check_splits_a_huge_ping_list(config):
    channel = IOChannel()
    io = DiscordAliveCheckIO(IOBot(channel), config)

    run(io.send_check("text", list(range(1, 200))))

    assert len(channel.sent) > 1
    assert all(len(message) <= 1900 for message in channel.sent)


def test_send_check_survives_a_forbidden_channel(config, caplog):
    import logging

    io = DiscordAliveCheckIO(IOBot(IOChannel(forbidden=True)), config)
    with caplog.at_level(logging.ERROR, logger="hell.aliveio"):
        assert run(io.send_check("text", [1])) is None
    assert "Missing permission" in caplog.text


def test_kick_only_touches_people_still_in_the_vc(config):
    present = FakeMember(1, "Alice")
    voice = MagicMock(spec=discord.VoiceChannel)   # must pass the isinstance guard
    voice.id = config.voice_channel_id
    voice.members = [present]
    guild = MagicMock(spec=discord.Guild)
    io = DiscordAliveCheckIO(IOBot(voice=voice, guild=guild), config)

    removed = run(io.kick([1, 2], "no answer"))

    assert removed == [1]
    assert present.moved_to == [None]


def test_kick_reports_when_the_vc_is_invisible(config, caplog):
    import logging

    io = DiscordAliveCheckIO(IOBot(), config)
    with caplog.at_level(logging.ERROR, logger="hell.aliveio"):
        assert run(io.kick([1, 2], "no answer")) == []
    assert "nobody was removed" in caplog.text


def test_replies_since_recovers_answers_written_while_offline(config):
    channel = IOChannel()
    channel.past = [(1, "Yes"), (2, "nah"), (3, "YES!"), (4, "Yes")]
    io = DiscordAliveCheckIO(IOBot(channel), config)

    found = run(io.replies_since(channel.id, 100, [1, 2, 3]))

    assert found == {1, 3}          # user 4 was not asked, user 2 did not answer


def test_replies_since_handles_a_missing_channel(config):
    io = DiscordAliveCheckIO(IOBot(None), config)
    assert run(io.replies_since(1, 2, [1])) == set()
