"""Tests for the removed-feature cleanup (legacy_cleanup.py).

Sentinel no longer ships rules posts, reaction-role menus or tickets. This
covers the migration that deals with what they left behind, and the parts worth
pinning are the ones that are easy to get subtly wrong:

* the config shapes are *historical*, so a config written by an older build
  (the rules section as a single object rather than a list) still has to be
  read — otherwise its posts are orphaned in the server forever;
* every step must be idempotent, because ``on_ready`` fires again on every
  reconnect and the pass runs against a config that may already be clean;
* a failure to delete one message must not stop the rest, nor stop the config
  from being pruned — otherwise the bot retries the same unauthorised delete
  on every reconnect, forever;
* the input config must never be mutated in place, because the bot holds a live
  reference to it.

Run with either:
    python -m pytest tests/test_legacy_cleanup.py
    python tests/test_legacy_cleanup.py
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import discord  # noqa: E402

import legacy_cleanup  # noqa: E402


GUILD_ID = 1234567890123456789
OTHER_GUILD_ID = 987654321098765432


def _legacy_config() -> dict:
    """A config as an older build would have left it on disk.

    One guild with a rule set, a reaction-role post and a ticket panel; a
    second guild with only a ticket panel; plus the sections that must survive.
    """
    return {
        "bot_token": "token",
        "server_id": GUILD_ID,
        "rules": {
            str(GUILD_ID): [
                {"ruleset_id": "r1", "channel_id": 11, "message_id": 12},
                {"ruleset_id": "r2", "channel_id": 13, "message_id": 14},
            ],
        },
        "reaction_roles": {
            str(GUILD_ID): [
                {"post_id": "p1", "channel_id": 21, "message_id": 22},
            ],
        },
        "tickets": {
            str(GUILD_ID): {
                "panel": {"channel_id": 31, "message_id": 32},
                "categories": [{"category_id": "c1", "label": "General"}],
            },
            str(OTHER_GUILD_ID): {"panel": {"channel_id": 41, "message_id": 42}},
        },
        "applications": {str(GUILD_ID): [{"form_id": "f1"}]},
        "guilds": {},
    }


class _FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.id = message_id
        self.deleted = False

    async def delete(self) -> None:
        self.deleted = True


class _FakeChannel:
    """A channel holding exactly the messages it is told about."""

    def __init__(self, channel_id: int, messages: dict[int, _FakeMessage]) -> None:
        self.id = channel_id
        self.messages = messages

    async def fetch_message(self, message_id: int) -> _FakeMessage:
        message = self.messages.get(int(message_id))
        if message is None:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Message")
        return message


class _FakeGuild:
    def __init__(self, guild_id: int, channels: dict[int, _FakeChannel]) -> None:
        self.id = guild_id
        self.channels = channels

    def get_channel(self, channel_id: int):
        return self.channels.get(int(channel_id))

    async def fetch_channel(self, channel_id: int):
        raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Channel")


class _FakeBot:
    """Only what the cleanup touches: ``config`` and ``get_guild``."""

    def __init__(self, config: dict, guilds: dict[int, _FakeGuild] | None = None) -> None:
        self.config = config
        self.guilds = dict(guilds or {})

    def get_guild(self, guild_id: int):
        return self.guilds.get(int(guild_id))


def _bot_with_every_message(config: dict) -> tuple[_FakeBot, list[_FakeMessage]]:
    """A bot whose guilds actually hold every message the config names."""
    messages: list[_FakeMessage] = []
    guilds: dict[int, dict[int, _FakeChannel]] = {}
    for record in legacy_cleanup.legacy_messages(config):
        channels = guilds.setdefault(record["guild_id"], {})
        message = _FakeMessage(record["message_id"])
        messages.append(message)
        channel = channels.setdefault(
            record["channel_id"], _FakeChannel(record["channel_id"], {})
        )
        channel.messages[record["message_id"]] = message
    return _FakeBot(config, {gid: _FakeGuild(gid, ch) for gid, ch in guilds.items()}), messages


class LegacyMessageDiscoveryTests(unittest.TestCase):
    """Finding what to delete, from config shapes this module did not create."""

    def test_finds_rules_posts_reaction_posts_and_ticket_panels(self) -> None:
        found = legacy_cleanup.legacy_messages(_legacy_config())
        identities = {(r["guild_id"], r["channel_id"], r["message_id"]) for r in found}
        self.assertEqual(
            identities,
            {
                (GUILD_ID, 11, 12),
                (GUILD_ID, 13, 14),
                (GUILD_ID, 21, 22),
                (GUILD_ID, 31, 32),
                (OTHER_GUILD_ID, 41, 42),
            },
        )

    def test_each_record_is_labelled_with_the_feature_it_came_from(self) -> None:
        """The label is what an administrator sees in the log line."""
        kinds = {
            (r["channel_id"], r["message_id"]): r["kind"]
            for r in legacy_cleanup.legacy_messages(_legacy_config())
        }
        self.assertEqual(kinds[(11, 12)], "rules post")
        self.assertEqual(kinds[(21, 22)], "reaction role post")
        self.assertEqual(kinds[(31, 32)], "ticket panel")

    def test_reads_the_legacy_single_object_rules_shape(self) -> None:
        """Older builds stored one rule-set object, not a list of them."""
        config = {"rules": {str(GUILD_ID): {"channel_id": 51, "message_id": 52}}}
        found = legacy_cleanup.legacy_messages(config)
        self.assertEqual(
            [(r["guild_id"], r["channel_id"], r["message_id"]) for r in found],
            [(GUILD_ID, 51, 52)],
        )

    def test_unpublished_and_malformed_entries_are_skipped(self) -> None:
        """Nothing to delete must not become a bogus delete attempt."""
        config = {
            "rules": {
                str(GUILD_ID): [
                    {"channel_id": 11},                  # never posted
                    {"message_id": 12},                  # no channel
                    {"channel_id": "nope", "message_id": "nope"},
                    "not a dict",
                ],
            },
            "reaction_roles": {str(GUILD_ID): {"not": "a list"}},
            "tickets": {str(GUILD_ID): {"categories": []}},   # panel never set
            "guilds": "not a dict",
        }
        self.assertEqual(legacy_cleanup.legacy_messages(config), [])

    def test_a_message_recorded_twice_is_only_listed_once(self) -> None:
        config = {
            "rules": {str(GUILD_ID): [{"channel_id": 11, "message_id": 12}]},
            "reaction_roles": {str(GUILD_ID): [{"channel_id": 11, "message_id": 12}]},
        }
        self.assertEqual(len(legacy_cleanup.legacy_messages(config)), 1)

    def test_a_clean_config_yields_nothing(self) -> None:
        self.assertEqual(legacy_cleanup.legacy_messages({"bot_token": "x"}), [])
        self.assertEqual(legacy_cleanup.legacy_messages({}), [])

    def test_a_non_numeric_guild_key_is_ignored(self) -> None:
        config = {"rules": {"not-a-guild": [{"channel_id": 1, "message_id": 2}]}}
        self.assertEqual(legacy_cleanup.legacy_messages(config), [])


class PruneConfigTests(unittest.TestCase):
    def test_removes_exactly_the_removed_features_sections(self) -> None:
        config = _legacy_config()
        updated, dropped = legacy_cleanup.prune_config(config)
        self.assertEqual(dropped, ["rules", "reaction_roles", "tickets"])
        self.assertNotIn("rules", updated)
        self.assertNotIn("reaction_roles", updated)
        self.assertNotIn("tickets", updated)

    def test_keeps_every_other_section(self) -> None:
        """Applications and the moderation config are not part of this."""
        config = _legacy_config()
        updated, _ = legacy_cleanup.prune_config(config)
        self.assertEqual(updated["applications"], config["applications"])
        self.assertEqual(updated["bot_token"], "token")
        self.assertEqual(updated["server_id"], GUILD_ID)
        self.assertIn("guilds", updated)

    def test_never_mutates_the_config_it_was_given(self) -> None:
        """The bot holds a live reference; pruning in place would race it."""
        config = _legacy_config()
        before = {key: config[key] for key in ("rules", "reaction_roles", "tickets")}
        updated, _ = legacy_cleanup.prune_config(config)
        self.assertEqual(config["rules"], before["rules"])
        self.assertEqual(config["reaction_roles"], before["reaction_roles"])
        self.assertEqual(config["tickets"], before["tickets"])
        self.assertIsNot(updated, config)

    def test_an_already_clean_config_reports_nothing_dropped(self) -> None:
        """What makes a second run (a reconnect) a no-op rather than a rewrite."""
        config = {"bot_token": "x", "applications": {}}
        updated, dropped = legacy_cleanup.prune_config(config)
        self.assertEqual(dropped, [])
        self.assertEqual(updated, config)

    def test_an_empty_section_still_counts_as_dropped(self) -> None:
        """DEFAULT_CONFIG shipped these as `{}`, so most installs have them."""
        updated, dropped = legacy_cleanup.prune_config({"rules": {}, "tickets": {}})
        self.assertEqual(dropped, ["rules", "tickets"])
        self.assertEqual(updated, {})


class DropTicketsTableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = tempfile.TemporaryDirectory(prefix="pm-cleanup-test-")
        self.addCleanup(self.home.cleanup)
        self.db_file = str(Path(self.home.name) / "sentinel.db")

    def _make_db(self, *, with_rows: bool = False) -> None:
        # ``closing()``, not a bare ``with sqlite3.connect(...)``: the
        # connection's own context manager only commits, it does not close, and
        # a leaked handle makes TemporaryDirectory.cleanup() fail on Windows
        # (WinError 32) while unlinking happily on Linux and macOS.
        with closing(sqlite3.connect(self.db_file)) as conn:
            conn.executescript(
                """
                CREATE TABLE tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    number INTEGER NOT NULL
                );
                CREATE INDEX idx_tickets_guild ON tickets (guild_id);
                CREATE TABLE punishments (id INTEGER PRIMARY KEY AUTOINCREMENT);
                """
            )
            if with_rows:
                conn.execute(
                    "INSERT INTO tickets (guild_id, number) VALUES (?, ?)",
                    (GUILD_ID, 1),
                )
            conn.commit()

    def _names(self, kind: str) -> set[str]:
        with closing(sqlite3.connect(self.db_file)) as conn:
            return {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = ?", (kind,)
                )
            }

    def test_drops_the_table_and_reports_that_it_did(self) -> None:
        self._make_db(with_rows=True)
        self.assertTrue(legacy_cleanup.drop_tickets_table(self.db_file))
        self.assertNotIn("tickets", self._names("table"))
        self.assertNotIn("idx_tickets_guild", self._names("index"))

    def test_leaves_the_other_tables_alone(self) -> None:
        """Migration scope is one table; punishments must survive it."""
        self._make_db(with_rows=True)
        legacy_cleanup.drop_tickets_table(self.db_file)
        self.assertIn("punishments", self._names("table"))

    def test_is_idempotent(self) -> None:
        """init_db() no longer creates the table, so run two reports nothing."""
        self._make_db()
        self.assertTrue(legacy_cleanup.drop_tickets_table(self.db_file))
        self.assertFalse(legacy_cleanup.drop_tickets_table(self.db_file))

    def test_a_database_that_never_had_tickets_reports_false(self) -> None:
        with closing(sqlite3.connect(self.db_file)) as conn:
            conn.execute("CREATE TABLE punishments (id INTEGER PRIMARY KEY)")
            conn.commit()
        self.assertFalse(legacy_cleanup.drop_tickets_table(self.db_file))

    def test_a_missing_database_file_is_not_fatal(self) -> None:
        """sqlite3 creates an empty file; there is simply nothing to drop."""
        self.assertFalse(
            legacy_cleanup.drop_tickets_table(str(Path(self.home.name) / "absent.db"))
        )


class DeleteLegacyMessagesTests(unittest.IsolatedAsyncioTestCase):
    async def test_deletes_every_message_it_can_reach(self) -> None:
        config = _legacy_config()
        bot, messages = _bot_with_every_message(config)
        tally = await legacy_cleanup.delete_legacy_messages(
            bot, legacy_cleanup.legacy_messages(config)
        )
        self.assertEqual(tally, {"deleted": len(messages), "missing": 0, "failed": 0})
        self.assertTrue(all(message.deleted for message in messages))

    async def test_a_guild_the_bot_left_counts_as_missing_not_failed(self) -> None:
        """The posts went with the server; nothing is left to clean up."""
        records = [{"guild_id": OTHER_GUILD_ID, "channel_id": 41, "message_id": 42,
                    "kind": "ticket panel"}]
        tally = await legacy_cleanup.delete_legacy_messages(_FakeBot({}), records)
        self.assertEqual(tally, {"deleted": 0, "missing": 1, "failed": 0})

    async def test_an_already_deleted_message_counts_as_missing(self) -> None:
        guild = _FakeGuild(GUILD_ID, {})   # no channels at all
        bot = _FakeBot({}, {GUILD_ID: guild})
        records = [{"guild_id": GUILD_ID, "channel_id": 11, "message_id": 12,
                    "kind": "rules post"}]
        tally = await legacy_cleanup.delete_legacy_messages(bot, records)
        self.assertEqual(tally, {"deleted": 0, "missing": 1, "failed": 0})

    async def test_a_refused_delete_does_not_stop_the_others(self) -> None:
        """One channel without Manage Messages must not abort the pass."""
        config = _legacy_config()
        bot, messages = _bot_with_every_message(config)

        async def refuse(_self=None) -> None:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")

        # Fail the first message only; the rest must still be deleted.
        messages[0].delete = refuse  # type: ignore[method-assign]
        tally = await legacy_cleanup.delete_legacy_messages(
            bot, legacy_cleanup.legacy_messages(config)
        )
        self.assertEqual(tally["failed"], 1)
        self.assertEqual(tally["deleted"], len(messages) - 1)
        self.assertTrue(all(message.deleted for message in messages[1:]))

    async def test_an_empty_list_is_a_no_op(self) -> None:
        self.assertEqual(
            await legacy_cleanup.delete_legacy_messages(_FakeBot({}), []),
            {"deleted": 0, "missing": 0, "failed": 0},
        )


class RunCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_deletes_the_posts_and_prunes_the_config(self) -> None:
        config = _legacy_config()
        bot, messages = _bot_with_every_message(config)
        saved: list[dict] = []
        summary = await legacy_cleanup.run_cleanup(
            bot, save_config=saved.append
        )
        self.assertTrue(all(message.deleted for message in messages))
        self.assertEqual(
            summary["dropped"], ["rules", "reaction_roles", "tickets"]
        )
        self.assertEqual(summary["tally"]["deleted"], len(messages))
        # The config was written once, without the removed sections ...
        self.assertEqual(len(saved), 1)
        self.assertNotIn("rules", saved[0])
        self.assertNotIn("tickets", saved[0])
        # ... and the bot's live config is the pruned one.
        self.assertIs(bot.config, saved[0])
        self.assertEqual(bot.config["applications"], config["applications"])

    async def test_a_second_run_does_nothing_at_all(self) -> None:
        """``on_ready`` fires again on every reconnect; this must stay quiet."""
        config = _legacy_config()
        bot, _ = _bot_with_every_message(config)
        saved: list[dict] = []
        await legacy_cleanup.run_cleanup(bot, save_config=saved.append)
        second = await legacy_cleanup.run_cleanup(bot, save_config=saved.append)
        self.assertEqual(second["messages"], [])
        self.assertEqual(second["dropped"], [])
        self.assertEqual(len(saved), 1, "a clean config must not be rewritten")

    async def test_prunes_even_when_every_delete_was_refused(self) -> None:
        """Keeping the keys would retry the same failure on every reconnect."""
        config = _legacy_config()
        bot, messages = _bot_with_every_message(config)

        async def refuse(_self=None) -> None:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")

        for message in messages:
            message.delete = refuse  # type: ignore[method-assign]
        saved: list[dict] = []
        summary = await legacy_cleanup.run_cleanup(bot, save_config=saved.append)
        self.assertEqual(summary["tally"]["failed"], len(messages))
        self.assertEqual(summary["dropped"], ["rules", "reaction_roles", "tickets"])
        self.assertEqual(len(saved), 1)

    async def test_an_unwritable_config_keeps_the_bot_running(self) -> None:
        def refuse(_config: dict) -> None:
            raise OSError("read-only file system")

        bot = _FakeBot(_legacy_config())
        summary = await legacy_cleanup.run_cleanup(bot, save_config=refuse)
        self.assertFalse(summary["saved"])
        self.assertEqual(summary["dropped"], [], "nothing was persisted")
        # The in-memory config is untouched, so a retry next start is possible.
        self.assertIn("tickets", bot.config)

    async def test_a_clean_install_needs_no_write(self) -> None:
        """The common case after this migration has already run once."""
        bot = _FakeBot({"bot_token": "x", "applications": {}})
        saved: list[dict] = []
        summary = await legacy_cleanup.run_cleanup(bot, save_config=saved.append)
        self.assertEqual(summary["messages"], [])
        self.assertEqual(summary["dropped"], [])
        self.assertEqual(saved, [])

    async def test_a_bot_without_a_config_dict_is_survivable(self) -> None:
        """on_ready wraps this in a try, but it must not raise either."""
        summary = await legacy_cleanup.run_cleanup(SimpleNamespace(config=None))
        self.assertEqual(summary["dropped"], [])


class ModuleIsolationTests(unittest.TestCase):
    """The cleanup has to outlive the modules it cleans up after."""

    def test_imports_no_feature_module(self) -> None:
        """rules/reaction_roles/tickets are deleted; importing one is a crash.

        This is what keeps the migration working on an install whose bundle
        contains only the current code.
        """
        source = (REPO_ROOT / "legacy_cleanup.py").read_text(encoding="utf-8")
        for removed in ("import rules", "import tickets", "import reaction_roles",
                        "import discord_markdown", "import bot"):
            with self.subTest(removed=removed):
                self.assertNotIn(removed, source)

    def test_the_removed_modules_really_are_gone(self) -> None:
        for name in ("rules.py", "tickets.py", "reaction_roles.py",
                     "discord_markdown.py"):
            with self.subTest(module=name):
                self.assertFalse((REPO_ROOT / name).exists())

    def test_nothing_left_in_the_repo_imports_them(self) -> None:
        """A stale import anywhere else is a bot that will not start."""
        offenders = []
        for path in sorted(REPO_ROOT.glob("*.py")):
            if path.name == "legacy_cleanup.py":
                continue
            text = path.read_text(encoding="utf-8")
            for module in ("rules", "tickets", "reaction_roles", "discord_markdown"):
                for line in text.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("#") or stripped.startswith('"""'):
                        continue
                    if stripped.startswith(f"import {module}") or stripped.startswith(
                        f"from {module} "
                    ):
                        offenders.append(f"{path.name}: {stripped}")
        self.assertEqual(offenders, [])


def main() -> int:
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
    sys.exit(main())
