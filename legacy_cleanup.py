"""One-time cleanup for features Sentinel no longer ships.

Sentinel used to publish three things into a server that are now gone from the
codebase: rule-set posts with a ✅ acceptance reaction, self-service
reaction-role menus, and ticket panels. Their modules (``rules.py``,
``reaction_roles.py``, ``tickets.py``) have been deleted, so nothing else can
read the configuration they left behind — which means this module must be
self-contained. It deliberately imports *only* :mod:`paths` and
:mod:`settings`, never a feature module, so deleting those files cannot break
it and it keeps working against config written by older builds.

It does three jobs, all idempotent:

* :func:`drop_tickets_table` removes the ``tickets`` SQLite table (and its
  indexes) from the database. Called from ``main()`` next to ``init_db()``, so
  it happens even if the Discord login later fails.
* :func:`delete_legacy_messages` deletes the messages those features posted —
  rule-set posts, reaction-role menus and ticket panels — so a server is not
  left with buttons and reactions that no longer do anything.
* :func:`prune_config` drops the ``rules``, ``reaction_roles`` and ``tickets``
  keys from ``config.json``.

Running the whole pass is :func:`run_cleanup`. Because the last step removes
the keys it reads, a second run finds nothing to do: that is what makes it
safe to call from ``on_ready``, which Discord may fire more than once per
process (reconnects).

What is *not* cleaned up, on purpose: the private ticket threads and channels
members were talking in. They hold member conversations rather than
bot-published furniture, and deleting them is unrecoverable, so they are left
for an administrator to remove. Only the panel message that opened them goes.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from typing import Callable, Iterable, Optional

import discord

import paths
from settings import save_config as _default_save_config


logger = logging.getLogger("sentinel.cleanup")

#: Config sections owned by the removed features.
REMOVED_CONFIG_KEYS = ("rules", "reaction_roles", "tickets")

#: The table the removed ticket system kept its rows in.
TICKETS_TABLE = "tickets"

#: Indexes that went with it. Dropping the table drops these too; they are
#: listed so a database that somehow kept them is still left tidy.
TICKETS_INDEXES = (
    "idx_tickets_guild",
    "idx_tickets_user",
    "idx_tickets_thread",
)

#: Human-readable label per section, used in log lines.
_KINDS = {
    "rules": "rules post",
    "reaction_roles": "reaction role post",
    "tickets": "ticket panel",
}

__all__ = [
    "REMOVED_CONFIG_KEYS",
    "TICKETS_TABLE",
    "delete_legacy_messages",
    "drop_tickets_table",
    "legacy_messages",
    "prune_config",
    "run_cleanup",
]


def _as_int(value: object) -> Optional[int]:
    """A positive int from a stored config value, or ``None``."""
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _guild_entries(section: object) -> Iterable[tuple[int, object]]:
    """Yield ``(guild_id, entry)`` for each guild in one config section.

    Both historical shapes are accepted, because config written by older
    builds has to be readable here: a section is normally a dict keyed by
    guild id, and the rules section could also store a *single* object rather
    than a list of rule sets.
    """
    if not isinstance(section, dict):
        return
    for guild_key, entry in section.items():
        guild_id = _as_int(guild_key)
        if guild_id is None:
            continue
        yield guild_id, entry


def _rule_messages(entry: object) -> Iterable[tuple[Optional[int], Optional[int]]]:
    """Channel/message pairs for a guild's rules section.

    The rules section is a list of rule sets, or — in the oldest shape — one
    bare rule-set object.
    """
    entries = entry if isinstance(entry, list) else [entry]
    for ruleset in entries:
        if isinstance(ruleset, dict):
            yield _as_int(ruleset.get("channel_id")), _as_int(ruleset.get("message_id"))


def _post_messages(entry: object) -> Iterable[tuple[Optional[int], Optional[int]]]:
    """Channel/message pairs for a guild's reaction-role post list."""
    if not isinstance(entry, list):
        return
    for post in entry:
        if isinstance(post, dict):
            yield _as_int(post.get("channel_id")), _as_int(post.get("message_id"))


def _panel_messages(entry: object) -> Iterable[tuple[Optional[int], Optional[int]]]:
    """The channel/message pair of a guild's ticket panel, if published."""
    if not isinstance(entry, dict):
        return
    panel = entry.get("panel")
    if isinstance(panel, dict):
        yield _as_int(panel.get("channel_id")), _as_int(panel.get("message_id"))


#: Per-section readers. Each turns one guild's entry into channel/message pairs.
_READERS: dict[str, Callable[[object], Iterable[tuple[Optional[int], Optional[int]]]]] = {
    "rules": _rule_messages,
    "reaction_roles": _post_messages,
    "tickets": _panel_messages,
}


def legacy_messages(config: dict) -> list[dict]:
    """Every message the removed features published, from ``config``.

    Returns a list of ``{"guild_id", "channel_id", "message_id", "kind"}``
    dicts. Entries with no usable channel or message id are skipped: an
    unpublishable half-record is not something the cleanup can act on, and
    pruning still removes it.

    The result is de-duplicated, so a message recorded under two sections is
    only fetched (and only reported) once.
    """
    messages: list[dict] = []
    seen: set[tuple[int, int, int]] = set()
    for key in REMOVED_CONFIG_KEYS:
        reader = _READERS[key]
        kind = _KINDS[key]
        for guild_id, entry in _guild_entries(config.get(key)):
            for channel_id, message_id in reader(entry):
                if channel_id is None or message_id is None:
                    continue
                identity = (guild_id, channel_id, message_id)
                if identity in seen:
                    continue
                seen.add(identity)
                messages.append(
                    {
                        "guild_id": guild_id,
                        "channel_id": channel_id,
                        "message_id": message_id,
                        "kind": kind,
                    }
                )
    return messages


def prune_config(config: dict) -> tuple[dict, list[str]]:
    """A copy of ``config`` without the removed features' keys.

    Returns the new config and the list of keys actually dropped, so the
    caller can log what changed and skip writing the file when nothing did.
    The input is never mutated: other code holds a reference to it.
    """
    updated = dict(config)
    dropped = [key for key in REMOVED_CONFIG_KEYS if key in updated]
    for key in dropped:
        del updated[key]
    return updated, dropped


def drop_tickets_table(db_file: Optional[str] = None) -> bool:
    """Drop the ``tickets`` table (and its indexes) if it is there.

    Returns whether a table was actually dropped, which is what lets the
    caller log the migration once instead of on every start. Safe to call
    repeatedly: a database that never had the table reports ``False``.
    """
    path = db_file or paths.DB_PATH
    try:
        with closing(sqlite3.connect(path)) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
                (TICKETS_TABLE,),
            ).fetchone()
            if row is None:
                return False
            conn.execute(f"DROP TABLE {TICKETS_TABLE}")
            for index in TICKETS_INDEXES:
                conn.execute(f"DROP INDEX IF EXISTS {index}")
            conn.commit()
        return True
    except sqlite3.Error as exc:
        # A cleanup step must never stop the bot from starting: the table is
        # simply unused, so failing to drop it costs nothing but a log line.
        logger.warning("Could not drop the %s table (%s): %s", TICKETS_TABLE, path, exc)
        return False


async def delete_legacy_messages(
    bot: discord.Client, messages: Iterable[dict]
) -> dict[str, int]:
    """Delete each recorded message from Discord.

    Returns a tally of ``{"deleted", "missing", "failed"}``:

    * ``deleted`` — the message was there and is now gone;
    * ``missing`` — the server, channel or message no longer exists, which
      counts as success: the goal is that nothing is left behind;
    * ``failed`` — Discord refused (usually a missing *Manage Messages*
      permission in that channel). These are logged, never raised.

    Every failure is contained: one undeletable message in one server must not
    stop the rest of the cleanup, or the config pruning that follows it.
    """
    tally = {"deleted": 0, "missing": 0, "failed": 0}
    for record in messages:
        guild_id = record["guild_id"]
        channel_id = record["channel_id"]
        message_id = record["message_id"]
        kind = record.get("kind") or "post"
        guild = bot.get_guild(guild_id)
        if guild is None:
            # The bot is no longer in that server, so its posts went with it.
            tally["missing"] += 1
            continue
        try:
            channel = guild.get_channel(channel_id) or await guild.fetch_channel(channel_id)
            message = await channel.fetch_message(message_id)
            await message.delete()
        except discord.NotFound:
            tally["missing"] += 1
            continue
        except (discord.Forbidden, discord.HTTPException) as exc:
            tally["failed"] += 1
            logger.warning(
                "Could not delete the %s in guild %s (message %s): %s",
                kind, guild_id, message_id, exc,
            )
            continue
        tally["deleted"] += 1
        logger.info("Deleted a leftover %s (message %s) in guild %s.", kind, message_id, guild_id)
    return tally


async def run_cleanup(
    bot: object,
    *,
    save_config: Optional[Callable[[dict], None]] = None,
) -> dict:
    """Delete the removed features' posts and prune their config keys.

    Called from ``on_ready``. A no-op once the keys are gone, which is what
    makes repeated ``on_ready`` events (reconnects) harmless.

    The config is written *after* the deletion attempt, and even when every
    deletion failed: keeping the keys would leave the bot retrying the same
    unauthorised deletes on every reconnect, forever, and the records are
    useless without the code that reads them. Failures are logged above so an
    administrator can remove those messages by hand.

    Returns a summary dict (also logged), mainly so tests can assert on it.
    """
    config = getattr(bot, "config", None)
    if not isinstance(config, dict):
        return {"messages": [], "tally": {}, "dropped": []}

    messages = legacy_messages(config)
    writer = save_config or _default_save_config

    tally = await delete_legacy_messages(bot, messages) if messages else {}
    updated, dropped = prune_config(config)

    if dropped:
        try:
            writer(updated)
        except OSError as exc:
            # The keys stay on disk, so the next start retries. Nothing else
            # reads them, so a stale config is cosmetic — but say so loudly.
            logger.error("Could not save the pruned config: %s", exc)
            return {"messages": messages, "tally": tally, "dropped": [], "saved": False}
        bot.config = updated  # type: ignore[attr-defined]
        logger.info(
            "Removed the %s config section(s) left by features Sentinel no longer ships.",
            ", ".join(dropped),
        )

    summary = {"messages": messages, "tally": tally, "dropped": dropped, "saved": bool(dropped)}
    if messages or dropped:
        logger.info(
            "Legacy cleanup: %s message(s) handled (%s), %s config section(s) dropped.",
            len(messages),
            ", ".join(f"{count} {label}" for label, count in sorted(tally.items())) or "none",
            len(dropped),
        )
    return summary
