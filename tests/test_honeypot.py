"""Offline tests for the honeypot feature (honeypot.py).

A honeypot is a trap channel: whoever posts in it is acted on automatically,
so the tests care most about the two ways it can go wrong:

* **It must fire.** A non-privileged member who posts in an armed channel is
  acted on with the configured action (punish role via the real
  ``start_punishment`` flow, kick, or ban), the message is deleted, the
  trigger is recorded in the database, and staff get an embed.
* **It must not fire on the wrong people.** DMs, webhooks, bots, other
  channels, and everyone :func:`settings.is_protected_member` protects —
  administrators, moderation-permission holders, the staff role, members at
  or above the bot's top role — are ignored, so staff can walk through a trap
  safely and nobody is punished by accident.

The command surface is covered too: add/remove/clear/action are
administrator-only, ``list`` is a staff query, and every write persists
through the injected ``_save_config``.

Run with either:
    python -m pytest tests/test_honeypot.py
    python tests/test_honeypot.py
"""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import discord  # noqa: E402

import bot as bot_module  # noqa: E402
import honeypot  # noqa: E402
import store  # noqa: E402
from honeypot import HoneypotMixin  # noqa: E402


GUILD_ID = 123456789012345678
TRAP_CHANNEL_ID = 555000000000000001
OTHER_CHANNEL_ID = 555000000000000002
STAFF_CHANNEL_ID = 555000000000000003
PUNISH_ROLE_ID = 444000000000000001
POST_ROLE_ID = 444000000000000002
STAFF_ROLE_ID = 444000000000000003
VICTIM_ID = 111000000000000001
RAIDER2_ID = 111000000000000002
ADMIN_ID = 222000000000000001
STAFF_ID = 333000000000000001
OUTSIDER_ID = 121000000000000001
BOT_ID = 999000000000000001


def _permissions(**flags) -> SimpleNamespace:
    names = (
        "administrator",
        "manage_guild",
        "moderate_members",
        "kick_members",
        "ban_members",
    )
    return SimpleNamespace(**{name: bool(flags.get(name, False)) for name in names})


class _FakeRole:
    def __init__(self, role_id: int, position: int = 1) -> None:
        self.id = role_id
        self.position = position
        self.mention = f"<@&{role_id}>"

    def __hash__(self) -> int:
        return hash(self.id)

    def __ge__(self, other): return self.position >= other.position
    def __gt__(self, other): return self.position > other.position
    def __lt__(self, other): return self.position < other.position
    def __le__(self, other): return self.position <= other.position


class _FakeMember:
    def __init__(
        self,
        member_id: int,
        *,
        name: str = "Member",
        bot_user: bool = False,
        roles=(),
        top_role: _FakeRole | None = None,
        **permissions,
    ) -> None:
        self.id = member_id
        self.name = name
        self.display_name = name
        self.bot = bot_user
        self.mention = f"<@{member_id}>"
        self.roles = list(roles)
        self.top_role = top_role or _FakeRole(1, 1)
        self.guild_permissions = _permissions(**permissions)
        self.display_avatar = None
        self.dms: list[object] = []
        self.role_changes: list[tuple[str, int]] = []
        self.kicked: list[str] = []
        self.banned: list[str] = []

    async def send(self, *, embed=None, **_kwargs) -> None:
        self.dms.append(embed)

    async def add_roles(self, role, reason=None) -> None:
        self.role_changes.append(("add", role.id))

    async def remove_roles(self, role, reason=None) -> None:
        self.role_changes.append(("remove", role.id))

    async def kick(self, reason=None) -> None:
        self.kicked.append(reason or "")

    async def ban(self, reason=None, **_kwargs) -> None:
        self.banned.append(reason or "")


class _FakeMessage:
    def __init__(self, channel, author, *, content: str = "gotcha", message_id: int = 700) -> None:
        self.id = message_id
        self.channel = channel
        self.guild = channel.guild if channel is not None else None
        self.author = author
        self.content = content
        self.webhook_id = None
        self.deleted = False
        self.delete_calls = 0

    async def delete(self) -> None:
        self.delete_calls += 1
        self.deleted = True


class _FakeTextChannel:
    def __init__(self, guild, channel_id: int, name: str = "trap") -> None:
        self.id = channel_id
        self.guild = guild
        self.name = name
        self.mention = f"<#{channel_id}>"

    async def delete(self, **_kwargs) -> None:
        pass


class _FakeGuild:
    id = GUILD_ID
    name = "Test Guild"

    def __init__(self, me: _FakeMember) -> None:
        self.me = me
        self._roles: dict[int, _FakeRole] = {}
        self._channels: dict[int, _FakeTextChannel] = {}

    def add_role(self, role_id: int, position: int = 1) -> _FakeRole:
        role = _FakeRole(role_id, position)
        self._roles[role_id] = role
        return role

    def add_channel(self, channel_id: int, name: str = "trap") -> _FakeTextChannel:
        channel = _FakeTextChannel(self, channel_id, name)
        self._channels[channel_id] = channel
        return channel

    def get_role(self, role_id):
        return self._roles.get(int(role_id))

    def get_channel(self, channel_id):
        return self._channels.get(int(channel_id))


class _FakeBot:
    """Everything of ``SentinelBot`` the honeypot touches — and the real
    ``start_punishment``, bound to this fake, so the punish path exercises the
    actual database flow instead of a stub."""

    def __init__(self, config: dict, guild: _FakeGuild) -> None:
        self.config = config
        self._guild = guild
        self.staff_embeds: list[tuple[object, discord.Embed]] = []

    async def _send_staff_embed(self, guild, embed, *, content=None) -> None:
        self.staff_embeds.append((guild, embed))

    async def _dm_embed(self, user, embed, *, content=None) -> bool:
        user.dms.append(embed)
        return True

    # Bound to the real SentinelBot.start_punishment in setUp.
    async def start_punishment(self, guild, member, moderator, duration_seconds, reason, cfg):
        raise AssertionError("not bound")


def _bind_real_start_punishment(fake_bot: _FakeBot) -> None:
    fake_bot.start_punishment = types.MethodType(
        bot_module.SentinelBot.start_punishment, fake_bot
    )


class _FakeResponse:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.deferred = False

    def is_done(self) -> bool:
        return self.deferred

    async def send_message(self, content=None, *, ephemeral=False, **_kwargs) -> None:
        self.messages.append({"content": content, "ephemeral": ephemeral})


class _FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send(self, content=None, *, ephemeral=False, **_kwargs) -> None:
        self.messages.append({"content": content, "ephemeral": ephemeral})


class _FakeInteraction:
    def __init__(self, user, guild) -> None:
        self.user = user
        self.guild = guild
        self.guild_id = guild.id
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()

    def replies(self) -> list[str]:
        return [
            item["content"]
            for item in self.response.messages + self.followup.messages
            if item.get("content")
        ]


class _IsolatedStoreMixin:
    """Temporary database + captured config writer, as in test_applications."""

    def _isolate(self) -> None:
        self.home = tempfile.TemporaryDirectory(prefix="pm-honeypot-test-")
        self.db_file = Path(self.home.name) / "sentinel.db"
        self.saved_configs: list[dict] = []
        for patcher in (
            patch.object(store, "db_path", lambda: str(self.db_file)),
            patch.object(bot_module, "DB_PATH", str(self.db_file)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        bot_module.init_db()

    def _cleanup(self) -> None:
        self.home.cleanup()

    def _rig(self, config: dict | None = None):
        """A fake bot + guild holding exactly the ``config`` dict passed in.

        No copy: the tests mutate the same dict the bot reads, and the command
        tests assert on the dict ``_save_config`` persisted.
        """
        config = config or {}
        config.setdefault("default_duration_minutes", 30)
        self.bot = _FakeBot(config, _FakeGuild(_FakeMember(BOT_ID, bot_user=True)))
        _bind_real_start_punishment(self.bot)
        self.guild = self.bot._guild
        self.guild.add_role(PUNISH_ROLE_ID, position=2)
        self.guild.add_role(POST_ROLE_ID, position=2)
        self.guild.add_role(STAFF_ROLE_ID, position=5)
        self.guild.add_channel(TRAP_CHANNEL_ID)
        self.guild.add_channel(OTHER_CHANNEL_ID, name="general")
        return self.bot

    def _member(self, member_id: int, **kwargs) -> _FakeMember:
        self.guild.me.top_role = _FakeRole(2, 10)
        kwargs.setdefault("top_role", _FakeRole(1, 1))
        return _FakeMember(member_id, **kwargs)

    def _trap_message(self, author, *, channel_id: int = TRAP_CHANNEL_ID, content: str = "gotcha", **kwargs):
        return _FakeMessage(self.guild._channels[channel_id], author, content=content, **kwargs)

    def _mixin(self) -> HoneypotMixin:
        mixin = HoneypotMixin()
        mixin.bot = self.bot
        mixin._save_config = lambda cfg: self.saved_configs.append(cfg)
        return mixin


class HoneypotConfigTests(unittest.TestCase):
    """The settings helpers: precedence, normalisation and defaults."""

    def test_defaults_when_nothing_configured(self) -> None:
        import settings

        self.assertEqual(settings.get_honeypot_channels({}), [])
        self.assertEqual(settings.get_honeypot_action({}), "punish")
        self.assertEqual(settings.get_honeypot_channels({"guilds": "junk"}), [])
        self.assertEqual(
            settings.get_honeypot_action({"honeypot_action": "explode"}), "punish"
        )

    def test_top_level_and_per_guild_precedence(self) -> None:
        import settings

        config = {
            "honeypot_channel_ids": [1, 2],
            "honeypot_action": "kick",
            "guilds": {
                str(GUILD_ID): {
                    "honeypot_channel_ids": [3],
                    "honeypot_action": "ban",
                },
                str(OTHER_CHANNEL_ID): {},
            },
        }
        self.assertEqual(settings.get_honeypot_channels(config, GUILD_ID), [3])
        self.assertEqual(settings.get_honeypot_action(config, GUILD_ID), "ban")
        # A guild entry without the keys falls back to the top level.
        self.assertEqual(settings.get_honeypot_channels(config, OTHER_CHANNEL_ID), [1, 2])
        self.assertEqual(settings.get_honeypot_action(config, OTHER_CHANNEL_ID), "kick")

    def test_explicit_empty_per_guild_list_beats_top_level(self) -> None:
        import settings

        config = {
            "honeypot_channel_ids": [1],
            "guilds": {str(GUILD_ID): {"honeypot_channel_ids": []}},
        }
        self.assertEqual(settings.get_honeypot_channels(config, GUILD_ID), [])

    def test_normalises_junk_and_duplicates(self) -> None:
        import settings

        config = {"honeypot_channel_ids": [7, "7", "x", None, 9]}
        self.assertEqual(settings.get_honeypot_channels(config), [7, 9])
        self.assertEqual(settings.get_honeypot_channels({"honeypot_channel_ids": "nope"}), [])

    def test_get_guild_config_carries_honeypot_keys(self) -> None:
        import settings

        # Primary server: the keys must survive the flattening.
        config = {
            "server_id": GUILD_ID,
            "punish_role_id": PUNISH_ROLE_ID,
            "post_role_id": POST_ROLE_ID,
            "honeypot_channel_ids": [1],
            "honeypot_action": "kick",
            "guilds": {str(GUILD_ID): {"honeypot_action": "ban"}},
        }
        flat = settings.get_guild_config(config, GUILD_ID)
        self.assertEqual(flat["honeypot_action"], "ban")   # per-guild wins
        self.assertEqual(flat["honeypot_channel_ids"], [1])  # top-level default

    def test_setters_round_trip(self) -> None:
        config = {}
        honeypot.set_honeypot_channels(config, GUILD_ID, [5, 5, "6", 6])
        self.assertEqual(config["guilds"][str(GUILD_ID)]["honeypot_channel_ids"], [5, 6])
        self.assertEqual(honeypot.get_honeypot_channels(config, GUILD_ID), [5, 6])
        honeypot.set_honeypot_action(config, GUILD_ID, " BAN ")
        self.assertEqual(config["guilds"][str(GUILD_ID)]["honeypot_action"], "ban")
        self.assertEqual(honeypot.get_honeypot_action(config, GUILD_ID), "ban")
        with self.assertRaises(ValueError):
            honeypot.set_honeypot_action(config, GUILD_ID, "nuke")

    def test_primary_guild_mirrors_to_top_level(self) -> None:
        config = {"server_id": GUILD_ID, "guilds": {}}
        honeypot.set_honeypot_channels(config, GUILD_ID, [9])
        honeypot.set_honeypot_action(config, GUILD_ID, "kick")
        self.assertEqual(config["honeypot_channel_ids"], [9])
        self.assertEqual(config["honeypot_action"], "kick")


class HoneypotTrapTests(_IsolatedStoreMixin, unittest.TestCase):
    """The trap: who fires it, who is spared, and what gets recorded."""

    def setUp(self) -> None:
        self._isolate()
        self.addCleanup(self._cleanup)
        self.config = {
            "guilds": {
                str(GUILD_ID): {
                    "punish_role_id": PUNISH_ROLE_ID,
                    "post_role_id": POST_ROLE_ID,
                    "staff_role_id": STAFF_ROLE_ID,
                    "staff_channel_id": STAFF_CHANNEL_ID,
                    "honeypot_channel_ids": [TRAP_CHANNEL_ID],
                }
            },
            "default_duration_minutes": 30,
        }
        self.bot = self._rig(self.config)

    def _punishment_rows(self):
        return store.db_fetchall(
            "SELECT * FROM punishments WHERE guild_id = ?", (GUILD_ID,)
        )

    def test_trap_fires_with_the_real_punish_flow(self) -> None:
        member = self._member(VICTIM_ID)
        message = self._trap_message(member)

        result = self._run(honeypot.process_message(self.bot, message))

        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "punish")
        self.assertIsNone(result["error"])
        self.assertTrue(result["deleted"])
        self.assertTrue(message.deleted)
        # The real start_punishment ran: role added and row recorded.
        self.assertIn(("add", PUNISH_ROLE_ID), member.role_changes)
        rows = self._punishment_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["user_id"], VICTIM_ID)
        self.assertEqual(rows[0]["moderator_id"], BOT_ID)
        self.assertEqual(result["duration_seconds"], 30 * 60)
        # Trigger + staff embed + DM (dm_user defaults to on).
        traps = honeypot.recent_traps(GUILD_ID)
        self.assertEqual(len(traps), 1)
        self.assertEqual(traps[0]["content"], "gotcha")
        self.assertEqual(len(self.bot.staff_embeds), 1)
        embed = self.bot.staff_embeds[0][1]
        self.assertIn("Honeypot", embed.title)
        self.assertEqual(len(member.dms), 1)
        self.assertTrue(result["dm_sent"])

    def test_ignores_other_channels_and_dms(self) -> None:
        member = self._member(VICTIM_ID)
        self.assertIsNone(
            self._run(honeypot.process_message(self.bot, self._trap_message(member, channel_id=OTHER_CHANNEL_ID)))
        )
        dm = _FakeMessage(None, member)  # guild=None -> DM
        self.assertIsNone(self._run(honeypot.process_message(self.bot, dm)))
        self.assertEqual(honeypot.recent_traps(GUILD_ID), [])
        self.assertEqual(self.bot.staff_embeds, [])

    def test_ignores_bots_and_webhooks(self) -> None:
        bot_member = self._member(BOT_ID, bot_user=True)
        self.assertIsNone(self._run(honeypot.process_message(self.bot, self._trap_message(bot_member))))
        human = self._member(VICTIM_ID)
        webhook_message = self._trap_message(human)
        webhook_message.webhook_id = 987
        self.assertIsNone(self._run(honeypot.process_message(self.bot, webhook_message)))
        self.assertFalse(webhook_message.deleted)
        self.assertEqual(honeypot.recent_traps(GUILD_ID), [])

    def test_ignores_protected_members(self) -> None:
        admin = self._member(ADMIN_ID, administrator=True)
        staff = self._member(STAFF_ID, roles=[self.guild.get_role(STAFF_ROLE_ID)])
        moderator = self._member(OUTSIDER_ID, kick_members=True)
        bot_top = self._member(RAIDER2_ID, top_role=_FakeRole(3, 99))
        for member in (admin, staff, moderator, bot_top):
            with self.subTest(member=member.id):
                self.assertIsNone(
                    self._run(honeypot.process_message(self.bot, self._trap_message(member)))
                )
        self.assertEqual(honeypot.recent_traps(GUILD_ID), [])
        self.assertEqual(self.bot.staff_embeds, [])

    def test_no_arming_no_fire(self) -> None:
        self.config["guilds"][str(GUILD_ID)]["honeypot_channel_ids"] = []
        member = self._member(VICTIM_ID)
        self.assertIsNone(self._run(honeypot.process_message(self.bot, self._trap_message(member))))

    def test_punish_action_without_roles_logs_but_does_not_crash(self) -> None:
        del self.config["guilds"][str(GUILD_ID)]["punish_role_id"]
        del self.config["guilds"][str(GUILD_ID)]["post_role_id"]
        member = self._member(VICTIM_ID)

        result = self._run(honeypot.process_message(self.bot, self._trap_message(member)))

        self.assertEqual(self._punishment_rows(), [])
        self.assertIn("manage setup", result["error"])
        self.assertEqual(len(honeypot.recent_traps(GUILD_ID)), 1)
        embed = self.bot.staff_embeds[0][1]
        note = [f for f in embed.fields if f.name == "Note"][0]
        self.assertIn("manage setup", note.value)
        # Nothing happened to the member, so no DM either.
        self.assertEqual(member.dms, [])

    def test_member_already_punished_is_not_re_punished(self) -> None:
        member = self._member(VICTIM_ID)
        self._run(honeypot.process_message(self.bot, self._trap_message(member)))
        self.assertEqual(len(self._punishment_rows()), 1)

        second = self._trap_message(member, message_id=701)
        result = self._run(honeypot.process_message(self.bot, second))

        self.assertTrue(result["already_punished"])
        self.assertEqual(len(self._punishment_rows()), 1)
        self.assertEqual(member.role_changes, [("add", PUNISH_ROLE_ID)])
        embed = self.bot.staff_embeds[-1][1]
        note = [f for f in embed.fields if f.name == "Note"][0]
        self.assertIn("Already serving", note.value)
        self.assertEqual(honeypot.count_traps(GUILD_ID), 2)

    def test_kick_action(self) -> None:
        self.config["guilds"][str(GUILD_ID)]["honeypot_action"] = "kick"
        member = self._member(VICTIM_ID)

        result = self._run(honeypot.process_message(self.bot, self._trap_message(member)))

        self.assertEqual(result["action"], "kick")
        self.assertIsNone(result["error"])
        self.assertEqual(len(member.kicked), 1)
        self.assertEqual(self._punishment_rows(), [])
        self.assertEqual(len(member.dms), 1)  # kick honours dm_user too

    def test_ban_action_never_dms(self) -> None:
        self.config["guilds"][str(GUILD_ID)]["honeypot_action"] = "ban"
        member = self._member(VICTIM_ID)

        result = self._run(honeypot.process_message(self.bot, self._trap_message(member)))

        self.assertEqual(result["action"], "ban")
        self.assertEqual(len(member.banned), 1)
        self.assertEqual(member.dms, [])

    def test_kick_failure_is_reported_to_staff(self) -> None:
        self.config["guilds"][str(GUILD_ID)]["honeypot_action"] = "kick"
        member = self._member(VICTIM_ID)

        def forbidden(_self, reason=None):
            raise discord.Forbidden(SimpleNamespace(status=403, reason="no"), "Forbidden")

        member.kick = types.MethodType(forbidden, member)
        result = self._run(honeypot.process_message(self.bot, self._trap_message(member)))

        self.assertIn("permission", result["error"])
        embed = self.bot.staff_embeds[0][1]
        note = [f for f in embed.fields if f.name == "Note"][0]
        self.assertIn("permission", note.value)

    def test_message_delete_failure_does_not_stop_the_action(self) -> None:
        member = self._member(VICTIM_ID)
        message = self._trap_message(member)

        async def broken_delete():
            raise discord.Forbidden(SimpleNamespace(status=403, reason="no"), "Forbidden")

        message.delete = broken_delete
        result = self._run(honeypot.process_message(self.bot, message))

        self.assertFalse(result["deleted"])
        self.assertIsNone(result["error"])
        self.assertEqual(len(self._punishment_rows()), 1)

    def test_dm_user_off_silences_the_dm(self) -> None:
        self.config["guilds"][str(GUILD_ID)]["dm_user"] = False
        member = self._member(VICTIM_ID)
        result = self._run(honeypot.process_message(self.bot, self._trap_message(member)))
        self.assertFalse(result["dm_sent"])
        self.assertEqual(member.dms, [])

    def _run(self, coro):
        import asyncio

        return asyncio.run(coro)


class HoneypotCommandTests(_IsolatedStoreMixin, unittest.TestCase):
    """The /manage honeypot command surface."""

    def setUp(self) -> None:
        self._isolate()
        self.addCleanup(self._cleanup)
        self.config = {
            "server_id": GUILD_ID,
            "guilds": {
                str(GUILD_ID): {
                    "punish_role_id": PUNISH_ROLE_ID,
                    "post_role_id": POST_ROLE_ID,
                    "staff_role_id": STAFF_ROLE_ID,
                }
            },
        }
        self.bot = self._rig(self.config)
        self.mixin = self._mixin()
        self.admin = self._member(ADMIN_ID, administrator=True)
        self.staff = self._member(STAFF_ID, roles=[self.guild.get_role(STAFF_ROLE_ID)])
        self.outsider = self._member(OUTSIDER_ID)
        self.trap = self.guild._channels[TRAP_CHANNEL_ID]

    def _run(self, coro):
        import asyncio

        return asyncio.run(coro)

    def test_add_requires_administrator(self) -> None:
        interaction = _FakeInteraction(self.outsider, self.guild)
        self._run(self.mixin.honeypot_add.callback(self.mixin, interaction, self.trap))
        self.assertIn("administrators", interaction.replies()[0])
        self.assertEqual(self.config["guilds"][str(GUILD_ID)].get("honeypot_channel_ids"), None)
        self.assertEqual(self.saved_configs, [])

    def test_add_arms_channel_and_persists(self) -> None:
        interaction = _FakeInteraction(self.admin, self.guild)
        self._run(self.mixin.honeypot_add.callback(self.mixin, interaction, self.trap))
        reply = interaction.replies()[0]
        self.assertIn("now a honeypot", reply)
        self.assertEqual(honeypot.get_honeypot_channels(self.config, GUILD_ID), [TRAP_CHANNEL_ID])
        self.assertEqual(len(self.saved_configs), 1)
        # Primary-server mirroring keeps the installer shape truthful.
        self.assertEqual(self.config["honeypot_channel_ids"], [TRAP_CHANNEL_ID])

    def test_add_is_idempotent(self) -> None:
        self.config["guilds"][str(GUILD_ID)]["honeypot_channel_ids"] = [TRAP_CHANNEL_ID]
        interaction = _FakeInteraction(self.admin, self.guild)
        self._run(self.mixin.honeypot_add.callback(self.mixin, interaction, self.trap))
        self.assertIn("already", interaction.replies()[0])
        self.assertEqual(honeypot.get_honeypot_channels(self.config, GUILD_ID), [TRAP_CHANNEL_ID])
        self.assertEqual(self.saved_configs, [])

    def test_add_enforces_the_cap(self) -> None:
        self.config["guilds"][str(GUILD_ID)]["honeypot_channel_ids"] = list(range(100, 110))
        interaction = _FakeInteraction(self.admin, self.guild)
        self._run(self.mixin.honeypot_add.callback(self.mixin, interaction, self.trap))
        self.assertIn("at most", interaction.replies()[0])
        self.assertEqual(len(honeypot.get_honeypot_channels(self.config, GUILD_ID)), 10)

    def test_add_warns_when_punish_roles_missing(self) -> None:
        del self.config["guilds"][str(GUILD_ID)]["punish_role_id"]
        interaction = _FakeInteraction(self.admin, self.guild)
        self._run(self.mixin.honeypot_add.callback(self.mixin, interaction, self.trap))
        self.assertIn("manage setup", interaction.replies()[0])

    def test_remove_disarms_only_the_named_channel(self) -> None:
        self.config["guilds"][str(GUILD_ID)]["honeypot_channel_ids"] = [
            TRAP_CHANNEL_ID,
            OTHER_CHANNEL_ID,
        ]
        interaction = _FakeInteraction(self.admin, self.guild)
        self._run(self.mixin.honeypot_remove.callback(self.mixin, interaction, self.trap))
        self.assertEqual(
            honeypot.get_honeypot_channels(self.config, GUILD_ID), [OTHER_CHANNEL_ID]
        )

        stranger = _FakeTextChannel(self.guild, 42, name="nope")
        self.guild._channels[42] = stranger
        interaction = _FakeInteraction(self.admin, self.guild)
        self._run(self.mixin.honeypot_remove.callback(self.mixin, interaction, stranger))
        self.assertIn("is not a honeypot", interaction.replies()[0])

    def test_clear_disarms_everything(self) -> None:
        self.config["guilds"][str(GUILD_ID)]["honeypot_channel_ids"] = [TRAP_CHANNEL_ID]
        interaction = _FakeInteraction(self.admin, self.guild)
        self._run(self.mixin.honeypot_clear.callback(self.mixin, interaction))
        self.assertIn("Disarmed 1", interaction.replies()[0])
        self.assertEqual(honeypot.get_honeypot_channels(self.config, GUILD_ID), [])

    def test_action_shows_current_when_omitted(self) -> None:
        interaction = _FakeInteraction(self.admin, self.guild)
        self._run(self.mixin.honeypot_action.callback(self.mixin, interaction, None))
        self.assertIn("punish role", interaction.replies()[0])
        self.assertEqual(self.saved_configs, [])

    def test_action_saves_choice(self) -> None:
        choice = discord.app_commands.Choice(name="Kick from the server", value="kick")
        interaction = _FakeInteraction(self.admin, self.guild)
        self._run(self.mixin.honeypot_action.callback(self.mixin, interaction, choice))
        self.assertEqual(honeypot.get_honeypot_action(self.config, GUILD_ID), "kick")
        self.assertEqual(len(self.saved_configs), 1)

    def test_list_is_staff_only(self) -> None:
        interaction = _FakeInteraction(self.outsider, self.guild)
        self._run(self.mixin.honeypot_list.callback(self.mixin, interaction))
        self.assertIn("Only members with", interaction.replies()[0])

        interaction = _FakeInteraction(self.staff, self.guild)
        self._run(self.mixin.honeypot_list.callback(self.mixin, interaction))
        self.assertIn("No channels are armed", interaction.replies()[0])

        # Administrators count as staff, as everywhere else.
        interaction = _FakeInteraction(self.admin, self.guild)
        self._run(self.mixin.honeypot_list.callback(self.mixin, interaction))
        self.assertIn("No channels are armed", interaction.replies()[0])

    def test_list_shows_channels_and_recent_triggers(self) -> None:
        honeypot.set_honeypot_channels(self.config, GUILD_ID, [TRAP_CHANNEL_ID])
        honeypot.record_trap(GUILD_ID, VICTIM_ID, TRAP_CHANNEL_ID, 700, "gotcha", "punish")
        interaction = _FakeInteraction(self.staff, self.guild)
        self._run(self.mixin.honeypot_list.callback(self.mixin, interaction))
        reply = interaction.replies()[0]
        self.assertIn(str(TRAP_CHANNEL_ID), reply)  # <#id> mention
        self.assertIn("Triggered 1 time(s)", reply)
        self.assertIn("Most recent", reply)


if __name__ == "__main__":
    unittest.main()
