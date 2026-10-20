"""
Sentinel - a discord.py server management bot.

Everything is reached through one slash-command group, ``/manage``:

    /manage punish      temporarily swap a member's role
    /manage pardon      end that early
    /manage warn        record a warning without changing roles
    /manage warnings    list or clear a member's warnings
    /manage status      server config, active punishments, member history
    /manage setup       configure roles, staff channel and DMs (admins)
    /manage fixcommands clean up duplicated slash commands (admins)
    /manage applications form    list / create / edit / delete a form (admins)
    /manage applications panel   publish an Apply panel (admins)
    /manage applications review  read submissions and decide (staff)
    /apply              fill in an application form (every member)

Members submit applications through the buttons on panels staff published, or
with that member command; they can never list, view or edit anybody's
application. Moderation itself is staff-role-only: punish, pardon, warn,
warnings and status all require the server's configured staff role
(administrators also qualify), re-checked on every call. Its authenticated
server-side web dashboard manages connected servers, moderation, warnings,
applications, configuration and history.

Rules posts, reaction-role menus and tickets used to live here too and have
been removed. :mod:`legacy_cleanup` deletes the messages those features left
in a server and prunes their config sections on first start after upgrading.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks


import paths
import command_tree
import legacy_cleanup
import store
import duckdns
from dashboard import DashboardServer, ensure_dashboard_token
from settings import (
    config_path,
    get_guild_config,
    get_staff_channel_id,
    get_staff_role_id,
    is_protected_member,
    moderation_denial,
    save_config,
    should_dm_user,
)
from applications import (
    ApplicationsCog,
    ApplicationsMixin,
    route_application_interaction,
)


# --------------------------------------------------------------------------- #
# Paths / constants
# --------------------------------------------------------------------------- #
# All writable locations are resolved by paths.py. Never write next to
# `__file__`: in a packaged build that is the read-only app tree
# (/opt/sentinel/_internal, C:\Program Files\...), and creating
# `data/` there raises PermissionError before the bot can even log anything.
BASE_DIR = paths.APP_DIR          # read-only in packaged builds
DATA_DIR = paths.DATA_DIR         # writable: db + log
DB_PATH = paths.DB_PATH
LOG_PATH = paths.LOG_PATH


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
logger = logging.getLogger("sentinel")
logger.setLevel(logging.INFO)
formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)
try:
    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
except OSError as exc:
    # A broken log file must not take the bot down; stdout/journal still work.
    logger.warning("File logging disabled (%s: %s)", LOG_PATH, exc)


def _report_path_notes() -> None:
    """Log any location fallbacks paths.py had to take (each note once)."""
    for note in paths.new_startup_notes():
        logger.info(note)


def _log_path_notes() -> None:
    """Report where this run keeps its files, plus late-resolved fallbacks."""
    logger.info("Sentinel %s", paths.app_version())
    _report_path_notes()
    logger.info("Data directory: %s", DATA_DIR)


# Paths are resolved before the logger exists; report them as the first thing
# in the log so a misconfigured install is obvious from `bot.log` alone.
_report_path_notes()


# --------------------------------------------------------------------------- #
# Persistence (SQLite)
# --------------------------------------------------------------------------- #
# Note: `normal_role_id` is kept as a nullable column for back-compat with
# older databases. New code no longer uses it - the bot no longer manages a
# "normal" role. Users who were being punished simply have the punish role
# added on top of whatever roles they already have; when pardoned, the
# punish / post role is removed and they go back to whatever they had.
SCHEMA = """
CREATE TABLE IF NOT EXISTS punishments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    moderator_id    INTEGER NOT NULL,
    reason          TEXT,
    punish_role_id  INTEGER NOT NULL,
    normal_role_id  INTEGER,  -- legacy, unused by new code
    post_role_id    INTEGER NOT NULL,
    started_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    stage           TEXT NOT NULL DEFAULT 'punished'  -- 'punished' or 'post'
);
CREATE INDEX IF NOT EXISTS idx_punishments_expiry
    ON punishments (expires_at);
CREATE INDEX IF NOT EXISTS idx_punishments_user
    ON punishments (guild_id, user_id);

-- Permanent record of every punishment that's ever ended. Used by
-- /manage status <member> to show a member's history.
CREATE TABLE IF NOT EXISTS punishment_history (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id         INTEGER NOT NULL,
    user_id          INTEGER NOT NULL,
    moderator_id     INTEGER NOT NULL,
    reason           TEXT,
    started_at       TEXT NOT NULL,
    duration_seconds INTEGER NOT NULL,
    ended_at         TEXT NOT NULL,
    ended_reason     TEXT
);
CREATE INDEX IF NOT EXISTS idx_punishment_history_user
    ON punishment_history (guild_id, user_id, started_at DESC);

-- Warnings are lighter than punishments: nothing is timed and no role is
-- swapped. They are kept until a moderator clears them, so /manage warnings
-- <user> can show the full record (with the escalating totals) later.
CREATE TABLE IF NOT EXISTS warnings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    reason       TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_warnings_user
    ON warnings (guild_id, user_id, created_at DESC);

-- Applications (see applications.py). `answers` is a JSON list of
-- {"question": str, "answer": str} in the order the questions were asked, so a
-- submission stays readable after the form it answered has been edited or
-- deleted.
CREATE TABLE IF NOT EXISTS applications (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id          INTEGER NOT NULL,
    form_id           TEXT NOT NULL,
    form_name         TEXT NOT NULL,
    user_id           INTEGER NOT NULL,
    answers           TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending', -- pending|approved|denied
    review_channel_id INTEGER,
    review_message_id INTEGER,
    submitted_at      TEXT NOT NULL,
    decided_at        TEXT,
    decided_by        INTEGER,
    decision_note     TEXT
);
CREATE INDEX IF NOT EXISTS idx_applications_guild
    ON applications (guild_id, status, submitted_at DESC);
CREATE INDEX IF NOT EXISTS idx_applications_user
    ON applications (guild_id, user_id, form_id);
"""


def init_db() -> None:
    """Create the SQLite database and tables if they don't exist.

    Also performs a runtime migration on older databases that may have
    the `normal_role_id` column marked NOT NULL. New code does not use
    that column, but writes NULL to it, so we need it nullable.
    """
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.executescript(SCHEMA)
        # Best-effort: drop NOT NULL on normal_role_id if it exists.
        # SQLite has no ALTER COLUMN, so we have to rebuild the table.
        try:
            cols = conn.execute("PRAGMA table_info(punishments)").fetchall()
            for col in cols:
                if col[1] == "normal_role_id" and col[3] == 1:  # notnull=1
                    logger.info("Migrating punishments table: making normal_role_id nullable.")
                    conn.executescript("""
                        CREATE TABLE punishments_new (
                            id              INTEGER PRIMARY KEY AUTOINCREMENT,
                            guild_id        INTEGER NOT NULL,
                            user_id         INTEGER NOT NULL,
                            moderator_id    INTEGER NOT NULL,
                            reason          TEXT,
                            punish_role_id  INTEGER NOT NULL,
                            normal_role_id  INTEGER,
                            post_role_id    INTEGER NOT NULL,
                            started_at      TEXT NOT NULL,
                            expires_at      TEXT NOT NULL,
                            stage           TEXT NOT NULL DEFAULT 'punished'
                        );
                        INSERT INTO punishments_new
                            SELECT id, guild_id, user_id, moderator_id, reason,
                                   punish_role_id, normal_role_id, post_role_id,
                                   started_at, expires_at, stage
                            FROM punishments;
                        DROP TABLE punishments;
                        ALTER TABLE punishments_new RENAME TO punishments;
                        CREATE INDEX IF NOT EXISTS idx_punishments_expiry
                            ON punishments (expires_at);
                        CREATE INDEX IF NOT EXISTS idx_punishments_user
                            ON punishments (guild_id, user_id);
                    """)
                    break
        except sqlite3.Error as exc:
            logger.warning("DB migration step failed (continuing): %s", exc)
        conn.commit()
    logger.info("Database initialised at %s", DB_PATH)


# The three helpers below now live in store.py so that applications.py can use
# them without importing this module (it is imported *by* this one). They are
# re-exported here because the dashboard is built with these callables and
# tests reach for bot.db_execute.
db_execute = store.db_execute
db_fetchall = store.db_fetchall
db_fetchone = store.db_fetchone


def archive_punishment(record: dict, ended_reason: str) -> None:
    """Move a finished punishment into the history table. The original
    record dict is the row from the `punishments` table."""
    started = datetime.fromisoformat(record["started_at"])
    expires = datetime.fromisoformat(record["expires_at"])
    duration = max(0, int((expires - started).total_seconds()))
    try:
        db_execute(
            """
            INSERT INTO punishment_history
                (guild_id, user_id, moderator_id, reason,
                 started_at, duration_seconds, ended_at, ended_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record["guild_id"],
                record["user_id"],
                record["moderator_id"],
                record.get("reason"),
                record["started_at"],
                duration,
                datetime.now(timezone.utc).isoformat(),
                ended_reason,
            ),
        )
    except sqlite3.Error as exc:
        logger.warning("Failed to archive punishment %s: %s", record.get("id"), exc)


# --------------------------------------------------------------------------- #
# Warnings
# --------------------------------------------------------------------------- #
# A warning is a permanent-on-record note attached to a member: no role is
# swapped, nothing expires, and moderators can list or clear them. Every read
# tolerates a missing table so a database created by an older build keeps
# working before the next ``init_db()`` migration adds it.
MAX_WARNING_REASON_LENGTH = 900


def add_warning(
    guild_id: int,
    user_id: int,
    moderator_id: int,
    reason: str,
) -> Optional[int]:
    """Record a warning. Returns its row id, or None if the write failed."""
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            cursor = conn.execute(
                """
                INSERT INTO warnings
                    (guild_id, user_id, moderator_id, reason, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    int(guild_id),
                    int(user_id),
                    int(moderator_id),
                    reason,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)
    except sqlite3.Error as exc:
        logger.warning("Failed to record warning for user %s: %s", user_id, exc)
        return None


def count_warnings(guild_id: int, user_id: int) -> int:
    """How many warnings a member currently has on record."""
    try:
        row = db_fetchone(
            "SELECT COUNT(*) AS count FROM warnings "
            "WHERE guild_id = ? AND user_id = ?",
            (int(guild_id), int(user_id)),
        )
    except sqlite3.OperationalError:
        # Older database without the warnings table yet.
        return 0
    return int(row["count"]) if row is not None else 0


def list_warnings(
    guild_id: int,
    user_id: Optional[int] = None,
    *,
    limit: int = 10,
) -> list[sqlite3.Row]:
    """Warnings for one member (or the whole guild), newest first."""
    limit = max(1, int(limit))
    try:
        if user_id is None:
            return db_fetchall(
                "SELECT * FROM warnings WHERE guild_id = ? "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (int(guild_id), limit),
            )
        return db_fetchall(
            "SELECT * FROM warnings WHERE guild_id = ? AND user_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (int(guild_id), int(user_id), limit),
        )
    except sqlite3.OperationalError:
        return []


def clear_warnings(guild_id: int, user_id: int) -> int:
    """Delete every warning for a member. Returns how many were removed.

    Raises ``sqlite3.Error`` if the delete fails; callers distinguish "nothing
    to clear" (0) from "the database refused the delete" instead of silently
    reporting success.
    """
    removed = count_warnings(guild_id, user_id)
    if removed == 0:
        return 0
    db_execute(
        "DELETE FROM warnings WHERE guild_id = ? AND user_id = ?",
        (int(guild_id), int(user_id)),
    )
    return removed


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
# We support two config shapes for backwards compatibility:
#   - NEW (single-server) shape: top-level bot_token, server_id, role ids,
#     staff_channel_id, dm_user. Filled in by the interactive installer.
#   - OLD (multi-server) shape: token + guilds{} map. Used by the in-Discord
#     /manage setup command. Old setups keep working unchanged.
#
# A config is considered "legacy" if it has the `token` key but no
# `bot_token` key. The migration step rewrites it in place.
DEFAULT_CONFIG: dict = {
    # --- NEW shape (preferred) ---
    "bot_token": "",
    "server_id": None,
    "punish_role_id": None,
    "post_role_id": None,
    "staff_role_id": None,    # protected from punishment
    "staff_channel_id": None,
    "dm_user": True,
    # --- legacy / shared ---
    "token": "",
    "guilds": {},
    "applications": {},       # per-guild application forms and their panels
    "dashboard_enabled": True,
    "dashboard_host": "127.0.0.1",
    "dashboard_port": 8765,
    "dashboard_secure_cookie": False,
    "dashboard_allowed_hosts": [],   # public names allowed in the Host header
    "dashboard_token": "",
    # --- remote access (see docs/REMOTE_ACCESS.md) ---
    "dashboard_public_url": "",       # e.g. "https://yourname.duckdns.org"
    "dashboard_trusted_proxies": [],  # proxy IPs/CIDRs whose X-Forwarded-For counts
    "dashboard_tls_cert": "",         # serve HTTPS here instead of via a proxy
    "dashboard_tls_key": "",
    # --- DuckDNS (keeps that public name pointed at this machine) ---
    "duckdns_enabled": True,
    "duckdns_domain": "",             # "yourname" or "yourname.duckdns.org"
    "duckdns_token": "",              # DuckDNS account token (NOT the dashboard key)
    "duckdns_interval_minutes": 5,
    "default_duration_minutes": 30,
    "log_channel_id": None,
}


# config_path() and save_config() live in settings.py too, for the same reason
# as the lookups above: the feature modules save the config they own
# (application forms) and must not import bot.py to do it. They are imported at
# the top of this file, so `bot.save_config` still resolves.
# load_config stays here: it falls back to DEFAULT_CONFIG, which this module
# owns.


def load_config() -> dict:
    """Read config.json, creating a default one in the data dir if missing.

    In a packaged install the config may live in a read-only system location
    (/etc/sentinel/config.json on Linux); the default/updated copy
    is written to the writable data dir instead. See paths.py.

    A missing, unreadable or corrupt config is reported and treated as "not
    configured yet" - the bot then runs the installer rather than dying with a
    traceback at import time (this module is imported before main() runs).
    """
    cfg_file = paths.config_path()
    if not cfg_file.is_file():
        try:
            written = paths.write_config(dict(DEFAULT_CONFIG))
            logger.warning(
                "Created default config at %s - run the installer to fill it in.",
                written,
            )
        except OSError as exc:
            logger.warning(
                "No config file at %s and none could be created (%s); "
                "set DISCORD_TOKEN or run the installer.",
                cfg_file,
                exc,
            )
            return dict(DEFAULT_CONFIG)
    try:
        cfg = paths.load_config_dict(dict(DEFAULT_CONFIG))
    except (OSError, RuntimeError, ValueError) as exc:
        logger.error(
            "Ignoring unreadable config file %s (%s). Fix or delete it and "
            "re-run the installer.",
            cfg_file,
            exc,
        )
        cfg = {}
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    return cfg


# get_guild_config / get_staff_role_id / get_staff_channel_id / should_dm_user /
# is_protected_member live in settings.py: the applications module needs them
# too, and importing them from here would be circular. They are imported at the
# top of this file, so `bot.get_guild_config(...)` and every call site below
# are unchanged.


def resolve_token(cfg: dict) -> Optional[str]:
    """Token resolution order: env var, then new key, then legacy key."""
    return (
        os.environ.get("DISCORD_TOKEN")
        or cfg.get("bot_token")
        or cfg.get("token")
        or None
    )


# --------------------------------------------------------------------------- #
# Duration parsing
# --------------------------------------------------------------------------- #
DURATION_UNITS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "week": 604800, "weeks": 604800,
}


def parse_duration(text: str) -> Optional[int]:
    """
    Parse a duration like '30m', '2h', '1d12h', '90' (treated as minutes).
    Returns the duration in seconds, or None if invalid.
    """
    if not text:
        return None
    text = text.strip().lower().replace(" ", "")
    if not text:
        return None
    if text.isdigit():
        return int(text) * 60
    total = 0
    i = 0
    n = len(text)
    while i < n:
        j = i
        while j < n and (text[j].isdigit() or text[j] == "."):
            j += 1
        if j == i:
            return None
        try:
            value = float(text[i:j])
        except ValueError:
            return None
        k = j
        while k < n and text[k].isalpha():
            k += 1
        unit = text[j:k] if k > j else "m"
        multiplier = DURATION_UNITS.get(unit)
        if multiplier is None:
            return None
        total += int(value * multiplier)
        i = k
    return total if total > 0 else None


def format_duration(seconds: int) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    parts = []
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs and not (days or hours):
        parts.append(f"{secs}s")
    return " ".join(parts) or "0m"


# --------------------------------------------------------------------------- #
# Bot
# --------------------------------------------------------------------------- #
intents = discord.Intents.default()
intents.members = True       # required to read/modify members
intents.guilds = True
# No reactions intent: the rules acceptance gate and the reaction-role menus
# were the only consumers, and both features have been removed.


class SentinelBot(commands.Bot):
    def __init__(self, config: dict) -> None:
        super().__init__(
            command_prefix="!",  # not used; we only expose slash commands
            intents=intents,
            help_command=None,
        )
        self.config = config
        self.dashboard = DashboardServer(
            self,
            save_config=save_config,
            db_fetchall=db_fetchall,
            db_fetchone=db_fetchone,
            db_execute=db_execute,
            archive_punishment=archive_punishment,
            get_guild_config=get_guild_config,
            get_staff_channel_id=get_staff_channel_id,
            should_dm_user=should_dm_user,
            is_protected_member=is_protected_member,
            parse_duration=parse_duration,
            format_duration=format_duration,
        )
        self.scheduler_task: Optional[asyncio.Task] = None
        # lambda, not the dict: the interactive installer path replaces
        # bot.config after this object is built (see __main__).
        self.duckdns = duckdns.DuckDNSUpdater(lambda: self.config)
        self._guild_sync_lock = asyncio.Lock()
        self._guild_commands_copied: set[int] = set()
        self._guild_commands_synced: set[int] = set()
        # Set once the duplicate-prone *global* command registry has been
        # emptied. Retried on the next READY if that request failed.
        self._global_registry_cleared = False

    async def close(self) -> None:
        await self.duckdns.stop()
        await self.dashboard.close()
        await super().close()

    async def _log_local_commands(self) -> None:
        """Log every slash command that's about to be registered. Useful
        for confirming at startup that the code on disk is what we
        expect (e.g. nothing got dropped by a botched merge)."""
        local = sorted(c.qualified_name for c in self.tree.walk_commands())
        logger.info("Local command tree (%d): %s", len(local), ", ".join(local) or "(none)")

    async def _sync_guild_commands(self, guild_id: int, *, force: bool = False) -> bool:
        """Publish this bot's slash commands to one guild, in the guild scope.

        ``CommandTree.sync(guild=...)`` only uploads commands scoped to that
        guild. Copying the global tree first is what makes those commands
        available instantly in the guild instead of waiting for global
        propagation.

        Commands are registered in the *guild* scope on purpose: Discord keeps
        the global and the per-guild command registries separate, and a command
        that lives in both is listed twice by the Discord client. See
        :meth:`_sync_commands`.

        ``force`` re-uploads a guild that was already synced (``/fixcommands``
        uses it to refresh the command list on demand).
        """
        guild_id = int(guild_id)
        async with self._guild_sync_lock:
            if not force and guild_id in self._guild_commands_synced:
                return True

            guild_obj = discord.Object(id=guild_id)
            try:
                if guild_id not in self._guild_commands_copied:
                    self.tree.copy_global_to(guild=guild_obj)
                    self._guild_commands_copied.add(guild_id)

                synced = await self.tree.sync(guild=guild_obj)
            except discord.HTTPException as exc:
                logger.warning(
                    "Guild command sync to %s failed (%s); will retry when "
                    "the guild is available.",
                    guild_id, exc,
                )
                return False
            except Exception:
                logger.exception(
                    "Unexpected error syncing commands to guild %s.", guild_id
                )
                return False

            self._guild_commands_synced.add(guild_id)

        logger.info(
            "Synced %d command(s) to guild %s (instant).",
            len(synced), guild_id,
        )
        return True

    async def _fetch_global_command_count(self) -> Optional[int]:
        """How many commands Discord has in the *global* command registry.

        ``None`` means the count could not be read (no application id yet, or
        Discord refused the request), so callers can report "unknown" instead of
        wrongly claiming there was nothing to remove.
        """
        if self.application_id is None:
            return None
        try:
            return len(await self.http.get_global_commands(self.application_id))
        except discord.HTTPException as exc:
            logger.warning("Could not read the global command registry: %s", exc)
            return None

    async def _clear_global_commands(self, *, reason: str) -> Optional[int]:
        """Empty Discord's *global* command registry (removes duplicate commands).

        Discord keeps global commands and per-guild commands in two separate
        registries, and a command that lives in both is shown twice by the
        Discord client - that is where doubled ``/manage`` entries
        came from. This bot registers commands per guild, so anything still in
        the global registry is a duplicate.

        The upload replaces the complete registry, so one empty request deletes
        every global command. Only the *registry* is emptied: the local tree is
        restored right after, which keeps :meth:`copy_global_to` working for
        guilds that join later.

        Returns how many commands were registered globally before the request
        (``None`` if that could not be determined). Raises
        :class:`discord.HTTPException` if the upload fails.
        """
        # Reading first makes the log line (and /fixcommands) truthful: stale
        # global commands are invisible to the local tree.
        before = await self._fetch_global_command_count()

        # Serialize with the per-guild syncs: they copy the local global tree,
        # so it must never be seen half-emptied.
        async with self._guild_sync_lock:
            commands = list(self.tree.get_commands(guild=None))
            self.tree.clear_commands(guild=None)
            try:
                # An empty payload replaces the whole global registry, so this
                # one request deletes every globally registered command.
                await self.tree.sync()
            finally:
                for command in commands:
                    self.tree.add_command(command)
        self._global_registry_cleared = True

        if before is None:
            logger.info("Cleared the global command registry (%s).", reason)
        elif before == 0:
            logger.info("Global command registry was already empty (%s).", reason)
        else:
            logger.info(
                "Removed %d duplicate global command(s) (%s): %s",
                before,
                reason,
                ", ".join(c.qualified_name for c in commands) or "(none)",
            )
        return before

    async def _retry_global_cleanup(self) -> None:
        """Empty the global registry unless that already succeeded this run."""
        if self._global_registry_cleared:
            return
        try:
            await self._clear_global_commands(
                reason="startup; commands are registered per guild"
            )
        except Exception:
            logger.exception(
                "Failed to clear the global command registry; will retry when "
                "the gateway is ready."
            )

    async def _sync_connected_guilds(self) -> None:
        """Immediately sync commands to every guild this bot is connected to."""
        # Retry the duplicate cleanup if setup could not reach Discord. The
        # per-guild syncs below are the only registrations that should remain.
        await self._retry_global_cleanup()

        guilds = list(self.guilds)
        if not guilds:
            logger.info("No connected guilds found for immediate command sync.")
            return

        for guild in guilds:
            await self._sync_guild_commands(guild.id)

    async def _sync_commands(self) -> None:
        """Register the slash commands exactly once: per guild, never globally.

        Discord keeps two independent command registries per application: the
        *global* commands and the *per-guild* commands. Registering the same
        command in both is allowed, and the Discord client then lists every
        command twice - once from each registry. That is what earlier releases
        did (a global sync *and* ``copy_global_to()`` for every connected
        guild), so every command showed up doubled.

        This bot registers commands in the guild scope only. It is the scope
        that appears instantly, it covers every server the bot is in (the
        configured ``server_id`` here, every connected guild on READY, and new
        guilds when they become available), and it cannot double up with a
        global copy.

        Leftover global commands from an older version are deleted here, which
        is what makes existing duplicates disappear after updating.
        """
        await self._log_local_commands()

        # Remove the duplicate half first, so the guild syncs below leave
        # exactly one registration per command.
        await self._retry_global_cleanup()

        # Keep the configured server fast even before the gateway cache is
        # ready. The connected-guild pass (on_ready) also covers every server.
        server_id = self.config.get("server_id")
        if server_id:
            try:
                await self._sync_guild_commands(int(server_id))
            except (TypeError, ValueError):
                logger.warning("Ignoring invalid configured server_id: %r", server_id)
        else:
            logger.info(
                "No server_id configured; commands will be synced instantly "
                "to every connected guild after login."
            )

    async def setup_hook(self) -> None:
        await self._sync_commands()

        if self.scheduler_task is None or self.scheduler_task.done():
            self.scheduler_task = self.loop.create_task(self.scheduler_loop())
            logger.info("Scheduler loop started.")

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (id=%s)", self.user, self.user.id)
        logger.info("Connected to %d guild(s).", len(self.guilds))
        await self._sync_connected_guilds()
        # One-time tidy-up of what the removed rules / reaction-role / ticket
        # features left behind. Runs here rather than in setup_hook because it
        # needs the guilds (and their channels) to be in cache to delete the
        # messages it was told about. Idempotent: on_ready can fire again on a
        # reconnect, and by then the config sections it reads are gone.
        try:
            await legacy_cleanup.run_cleanup(self)
        except Exception:
            logger.exception("Legacy cleanup failed; the bot will keep running.")
        await self.process_due_punishments()

    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Make commands available immediately when the bot joins a server."""
        await self._sync_guild_commands(guild.id)

    async def on_guild_available(self, guild: discord.Guild) -> None:
        """Retry a guild sync when Discord makes a server available."""
        await self._sync_guild_commands(guild.id)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        """Allow a later re-join to trigger another guild sync."""
        async with self._guild_sync_lock:
            self._guild_commands_synced.discard(guild.id)

    # ------------------------------------------------------------------ #
    # Scheduler
    # ------------------------------------------------------------------ #
    async def scheduler_loop(self) -> None:
        try:
            while not self.is_closed():
                await self.process_due_punishments()
                await asyncio.sleep(10)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Scheduler loop crashed.")
            raise

    async def process_due_punishments(self) -> None:
        now = datetime.now(timezone.utc)
        rows = db_fetchall(
            "SELECT * FROM punishments WHERE expires_at <= ?",
            (now.isoformat(),),
        )
        for row in rows:
            await self._advance_punishment(dict(row))

    async def _advance_punishment(self, record: dict) -> None:
        guild = self.get_guild(record["guild_id"])
        if guild is None:
            logger.warning(
                "Guild %s not found; deleting punishment record.",
                record["guild_id"],
            )
            db_execute("DELETE FROM punishments WHERE id = ?", (record["id"],))
            return

        try:
            member = await guild.fetch_member(record["user_id"])
        except (discord.NotFound, discord.HTTPException):
            logger.info(
                "User %s no longer in guild %s; deleting record.",
                record["user_id"],
                guild.id,
            )
            db_execute("DELETE FROM punishments WHERE id = ?", (record["id"],))
            return

        stage = record["stage"]
        punish_role = guild.get_role(record["punish_role_id"])
        post_role = guild.get_role(record["post_role_id"])

        try:
            if stage == "punished":
                if punish_role and punish_role in member.roles:
                    await member.remove_roles(
                        punish_role, reason="Punishment timer expired."
                    )
                if post_role and post_role not in member.roles:
                    await member.add_roles(
                        post_role, reason="Punishment timer expired."
                    )
                archive_punishment(record, ended_reason="timer_expired")
                db_execute("DELETE FROM punishments WHERE id = ?", (record["id"],))
                await self._log_event(
                    guild,
                    f"[timer] {member.mention} moved from **punished** -> "
                    f"**post-punishment**.",
                )
                staff_embed = self._build_timer_staff_embed(
                    member=member,
                    started_at=datetime.fromisoformat(record["started_at"]),
                )
                await self._send_staff_embed(guild, staff_embed)
                logger.info(
                    "Advanced punishment %s for user %s to post stage.",
                    record["id"],
                    record["user_id"],
                )
            else:
                db_execute("DELETE FROM punishments WHERE id = ?", (record["id"],))
        except discord.Forbidden:
            logger.error(
                "Missing permissions in guild %s for punishment %s.",
                guild.id, record["id"],
            )
        except discord.HTTPException:
            logger.exception(
                "Discord error while processing punishment %s.", record["id"]
            )

    async def _log_event(self, guild: discord.Guild, message: str) -> None:
        channel_id = get_staff_channel_id(self.config, guild.id)
        if not channel_id:
            return
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(channel_id)
            except discord.HTTPException:
                return
        if isinstance(channel, discord.abc.Messageable):
            try:
                await channel.send(message)
            except discord.HTTPException:
                pass

    # ------------------------------------------------------------------ #
    # Embed helpers - staff channel + user DM
    # ------------------------------------------------------------------ #
    def _build_punish_staff_embed(
        self,
        *,
        member: discord.Member,
        moderator: discord.Member,
        duration_seconds: int,
        reason: str,
        started_at: datetime,
        expires_at: datetime,
    ) -> discord.Embed:
        """Embed posted in the staff channel when a punishment is applied."""
        e = discord.Embed(
            title="Member punished",
            color=discord.Color.orange(),
            timestamp=started_at,
        )
        e.add_field(name="User", value=f"{member.mention} (`{member.id}`)", inline=True)
        e.add_field(name="Moderator", value=f"{moderator.mention}", inline=True)
        e.add_field(
            name="Duration",
            value=format_duration(duration_seconds),
            inline=True,
        )
        e.add_field(name="Reason", value=reason, inline=False)
        e.add_field(
            name="Started",
            value=f"<t:{int(started_at.timestamp())}:F>",
            inline=True,
        )
        e.add_field(
            name="Ends",
            value=f"<t:{int(expires_at.timestamp())}:F> "
                  f"(<t:{int(expires_at.timestamp())}:R>)",
            inline=True,
        )
        if member.display_avatar:
            e.set_thumbnail(url=member.display_avatar.url)
        e.set_footer(text=f"User ID: {member.id}")
        return e

    def _build_punish_dm_embed(
        self,
        *,
        guild_name: str,
        moderator_name: str,
        duration_seconds: int,
        reason: str,
        started_at: datetime,
        expires_at: datetime,
    ) -> discord.Embed:
        """Embed DMed to the user who was just punished."""
        e = discord.Embed(
            title=f"You've been punished in {guild_name}",
            description=(
                "Your access has been temporarily restricted in the server. "
                "Details are below. If you believe this was a mistake, "
                "please reach out to a moderator."
            ),
            color=discord.Color.red(),
            timestamp=started_at,
        )
        e.add_field(
            name="Duration",
            value=format_duration(duration_seconds),
            inline=False,
        )
        e.add_field(name="Reason", value=reason, inline=False)
        e.add_field(
            name="Started",
            value=f"<t:{int(started_at.timestamp())}:F>",
            inline=True,
        )
        e.add_field(
            name="Ends",
            value=f"<t:{int(expires_at.timestamp())}:F> "
                  f"(<t:{int(expires_at.timestamp())}:R>)",
            inline=True,
        )
        e.add_field(
            name="Issued by",
            value=moderator_name,
            inline=False,
        )
        e.set_footer(text="Sentinel")
        return e

    def _build_warn_staff_embed(
        self,
        *,
        member: discord.Member,
        moderator: discord.Member,
        reason: str,
        created_at: datetime,
        total: int,
    ) -> discord.Embed:
        """Embed posted in the staff channel when a member is warned."""
        e = discord.Embed(
            title="Member warned",
            color=discord.Color.gold(),
            timestamp=created_at,
        )
        e.add_field(name="User", value=f"{member.mention} (`{member.id}`)", inline=True)
        e.add_field(name="Moderator", value=f"{moderator.mention}", inline=True)
        e.add_field(name="Total warnings", value=str(int(total)), inline=True)
        e.add_field(name="Reason", value=reason, inline=False)
        e.add_field(
            name="Time",
            value=f"<t:{int(created_at.timestamp())}:F>",
            inline=True,
        )
        if member.display_avatar:
            e.set_thumbnail(url=member.display_avatar.url)
        e.set_footer(text=f"User ID: {member.id}")
        return e

    def _build_warn_dm_embed(
        self,
        *,
        guild_name: str,
        moderator_name: str,
        reason: str,
        total: int,
        created_at: datetime,
    ) -> discord.Embed:
        """Embed DMed to the user who was just warned."""
        e = discord.Embed(
            title=f"You've been warned in {guild_name}",
            description=(
                "A moderator has recorded a warning against your account. "
                "No roles were changed, but repeated warnings may lead to "
                "further moderation. If you believe this was a mistake, "
                "please reach out to a moderator."
            ),
            color=discord.Color.gold(),
            timestamp=created_at,
        )
        e.add_field(name="Reason", value=reason, inline=False)
        e.add_field(name="Total warnings", value=str(int(total)), inline=True)
        e.add_field(
            name="Time",
            value=f"<t:{int(created_at.timestamp())}:F>",
            inline=True,
        )
        e.add_field(name="Issued by", value=moderator_name, inline=False)
        e.set_footer(text="Sentinel")
        return e

    def _build_warn_clear_staff_embed(
        self,
        *,
        member: discord.Member,
        moderator: discord.Member,
        removed: int,
    ) -> discord.Embed:
        """Embed posted when a member's warnings are cleared."""
        e = discord.Embed(
            title="Warnings cleared",
            color=discord.Color.dark_grey(),
            timestamp=datetime.now(timezone.utc),
        )
        e.add_field(name="User", value=f"{member.mention} (`{member.id}`)", inline=True)
        e.add_field(name="Moderator", value=f"{moderator.mention}", inline=True)
        e.add_field(name="Warnings removed", value=str(int(removed)), inline=True)
        e.set_footer(text=f"User ID: {member.id}")
        return e

    def _build_pardon_staff_embed(
        self,
        *,
        member: discord.Member,
        moderator: discord.Member,
        reason: str,
        was_active: bool,
    ) -> discord.Embed:
        e = discord.Embed(
            title="Member pardoned",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        e.add_field(name="User", value=f"{member.mention} (`{member.id}`)", inline=True)
        e.add_field(name="Moderator", value=f"{moderator.mention}", inline=True)
        e.add_field(
            name="Outcome",
            value="Punishment ended early" if was_active else "No active punishment found; roles reset just in case",
            inline=False,
        )
        e.set_footer(text=f"User ID: {member.id}")
        return e

    def _build_pardon_dm_embed(
        self,
        *,
        guild_name: str,
        moderator_name: str,
    ) -> discord.Embed:
        e = discord.Embed(
            title=f"Your punishment in {guild_name} has been lifted",
            description=(
                "A moderator has ended your punishment early. The punish "
                "role has been removed and your access is restored."
            ),
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        e.add_field(name="Issued by", value=moderator_name, inline=False)
        e.set_footer(text="Sentinel")
        return e

    def _build_timer_staff_embed(
        self,
        *,
        member: discord.Member,
        started_at: datetime,
    ) -> discord.Embed:
        e = discord.Embed(
            title="Punishment timer expired",
            description=(
                f"{member.mention} was moved from the punish role to the "
                "post-punishment role."
            ),
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc),
        )
        e.add_field(
            name="Started",
            value=f"<t:{int(started_at.timestamp())}:R>",
            inline=True,
        )
        e.set_footer(text=f"User ID: {member.id}")
        return e

    async def _send_staff_embed(
        self,
        guild: discord.Guild,
        embed: discord.Embed,
        *,
        content: Optional[str] = None,
    ) -> None:
        """Post an embed in the configured staff channel. Never raises."""
        channel_id = get_staff_channel_id(self.config, guild.id)
        if not channel_id:
            return
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(channel_id)
            except discord.HTTPException:
                return
        if not isinstance(channel, discord.abc.Messageable):
            return
        try:
            await channel.send(content=content, embed=embed)
        except discord.HTTPException as exc:
            logger.warning("Failed to send staff embed: %s", exc)

    async def _dm_embed(
        self,
        user: discord.abc.User,
        embed: discord.Embed,
        *,
        content: Optional[str] = None,
    ) -> bool:
        """DM an embed to a user. Returns True on success, False on any
        failure (DMs closed, etc.). Never raises.
        """
        try:
            await user.send(content=content, embed=embed)
            return True
        except discord.Forbidden:
            logger.info(
                "Cannot DM user %s (DMs closed or bot blocked).", user.id
            )
            return False
        except discord.HTTPException as exc:
            logger.warning("Failed to DM user %s: %s", user.id, exc)
            return False

    # ------------------------------------------------------------------ #
    # Core punishment flow
    # ------------------------------------------------------------------ #
    async def start_punishment(
        self,
        guild: discord.Guild,
        member: discord.Member,
        moderator: discord.Member,
        duration_seconds: int,
        reason: str,
        cfg: dict,
    ) -> Optional[dict]:
        # No more "normal role" - the bot only manages the punish and
        # post-punish roles. The user keeps whatever roles they had.
        punish_role = guild.get_role(cfg["punish_role_id"])
        post_role = guild.get_role(cfg["post_role_id"])

        if not (punish_role and post_role):
            return None

        if punish_role >= guild.me.top_role:
            return {"error": "The punish role is higher than (or equal to) my top role."}
        if post_role >= guild.me.top_role:
            return {"error": "The post-punish role is higher than (or equal to) my top role."}

        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=duration_seconds)

        try:
            db_execute(
                """
                INSERT INTO punishments
                    (guild_id, user_id, moderator_id, reason,
                     punish_role_id, normal_role_id, post_role_id,
                     started_at, expires_at, stage)
                VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, 'punished')
                """,
                (
                    guild.id,
                    member.id,
                    moderator.id,
                    reason,
                    punish_role.id,
                    post_role.id,
                    now.isoformat(),
                    expires.isoformat(),
                ),
            )
        except sqlite3.Error as exc:
            logger.exception("Failed to record punishment: %s", exc)
            return {"error": "Database error; punishment not recorded."}

        try:
            if punish_role not in member.roles:
                await member.add_roles(punish_role, reason=f"Punished: {reason}")
        except discord.Forbidden:
            db_execute(
                "DELETE FROM punishments WHERE user_id = ? AND guild_id = ? AND stage='punished'",
                (member.id, guild.id),
            )
            return {"error": "I lack permission to change that user's roles."}
        except discord.HTTPException as exc:
            db_execute(
                "DELETE FROM punishments WHERE user_id = ? AND guild_id = ? AND stage='punished'",
                (member.id, guild.id),
            )
            return {"error": f"Discord error: {exc}"}

        return {
            "expires_at": expires,
            "started_at": now,
            "duration_seconds": duration_seconds,
        }


# --------------------------------------------------------------------------- #
# Slash command setup
# --------------------------------------------------------------------------- #
# NOTE: import-time matters. The `bot` instance is created at module import
# so that `@bot.tree.command(...)` decorators below can register onto it.
# We also build the Cog and add it to the bot explicitly so that the
# command discovery happens in one well-defined place instead of being
# scattered across the module. This is the most reliable way to avoid
# the "app_commands breaks at sync time" footgun in discord.py 2.x.
# --------------------------------------------------------------------------- #

bot = SentinelBot(load_config())


# ---- A single shared Cog holds all slash commands. --------------------- #
class SentinelCog(ApplicationsMixin, commands.Cog):
    """Every slash command, under the shared ``/manage`` group.

    It also mixes in :class:`applications.ApplicationsMixin`, because those
    commands are children of the same group: a nested group is registered - and
    its callbacks bound - by the cog that owns the top-level command, so they
    cannot be cogs of their own. The mixin's helpers expect ``bot`` and
    ``_save_config`` (it is normally constructed with them); this cog is the one
    Discord sees.

    ``/apply`` is the exception: it is a *top-level* command, so it lives in
    :class:`applications.ApplicationsCog`.
    """

    def __init__(self, bot_: SentinelBot) -> None:
        self.bot = bot_
        self._save_config = save_config

    async def cog_load(self) -> None:
        """Set default_permissions on the command group.

        ``app_commands.command()`` does not accept this kwarg, so we assign it
        here, after the decorator machinery has run.

        Discord applies ``default_member_permissions`` to the *top-level*
        command only, so the whole group shares one setting. It is left unset
        (``None``), the same as ``/apply``: showing ``/manage`` to members with
        *Moderate Members* would hide it from the very people the server wants
        moderating — members who hold the staff role but no moderation
        permission — and Discord has no way to express "has role X". So
        visibility is wide and every command decides for itself when it runs:

        * moderation (punish, pardon, warn, warnings, status) requires the
          configured staff role or Administrator
          (:meth:`_moderation_denial`);
        * ``setup`` and ``fixcommands`` require Administrator
          (:func:`command_tree.is_administrator`);
        * the application commands re-check their own staff rules.

        A server that would rather keep the group hidden from members can
        restrict it (or individual commands) in Server Settings → Integrations;
        the runtime checks are what actually gate the work.
        """
        # ``self.manage_group`` is the copy Cog.__new__ made for this cog
        # (see ``manage_group`` below); assigning to the shared object from
        # command_tree would miss the copy Discord actually receives.
        self.manage_group.default_permissions = None
        self.manage_group.guild_only = True

    # ---- Buttons, selects and modals ----------------------------------- #
    # Panels and review messages are published as components with
    # self-describing custom ids ("sentinel:ap:...") and handled here instead
    # of through registered views. That is what keeps a button working after a
    # restart, or after the server changed its forms: nothing has to be
    # re-registered, and an old panel resolves against the current
    # configuration (or explains that it is stale).
    #
    # discord.py dispatches "on_interaction" for every interaction after its
    # own routing, so this listener is additive: slash commands still reach the
    # tree. The router answers only for its own prefix.
    @commands.Cog.listener("on_interaction")
    async def on_component_interaction(
        self, interaction: discord.Interaction
    ) -> None:
        await route_application_interaction(self.bot, interaction)

    # ---- Every command hangs off the shared /manage group -------------- #
    # The group lives in its own module (command_tree.py) so that a feature
    # module can attach a sub-group to it without importing this one. Naming it
    # here is what makes this cog the one that registers /manage with Discord -
    # and therefore the cog every callback below binds to.
    manage_group = command_tree.manage_group

    # NOTE: Discord rejects the *entire* command list (HTTP 400, code 50035)
    # if any command or option description is over 100 characters.
    # tests/test_commands.py checks every description against that limit.

    @manage_group.command(
        name="punish",
        description="Add the punish role to a member for a duration.",
    )
    @app_commands.describe(
        user="The member to punish.",
        duration="How long. Examples: 30m, 2h, 1d, 1d12h. Bare numbers = minutes.",
        reason="Optional reason shown in logs and stored in the database.",
    )
    async def manage_punish(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        duration: str,
        reason: Optional[str] = None,
    ) -> None:
        await self._handle_punish(interaction, user, duration, reason)

    @manage_group.command(
        name="pardon",
        description="End an active punishment early and restore the member.",
    )
    @app_commands.describe(user="The member to pardon.")
    async def manage_pardon(
        self, interaction: discord.Interaction, user: discord.Member
    ) -> None:
        await self._handle_pardon(interaction, user)

    @manage_group.command(
        name="warn",
        description="Record a warning against a member without changing their roles.",
    )
    @app_commands.describe(
        user="The member to warn.",
        reason="Why they are being warned. Shown in the staff log and the member's DM.",
    )
    async def manage_warn(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reason: str,
    ) -> None:
        await self._handle_warn(interaction, user, reason)

    @manage_group.command(
        name="warnings",
        description="List a member's warnings, or clear them with clear:true.",
    )
    @app_commands.describe(
        user="The member whose warnings to show.",
        clear="Set to true to delete all of this member's warnings.",
    )
    async def manage_warnings(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        clear: Optional[bool] = False,
    ) -> None:
        await self._handle_warnings(interaction, user, bool(clear))

    @manage_group.command(
        name="status",
        description="Show this server's config and active punishments. "
                    "Pass a member for their history.",
    )
    @app_commands.describe(
        user="Optional: show this member's history instead of all active punishments.",
    )
    async def manage_status(
        self,
        interaction: discord.Interaction,
        user: Optional[discord.Member] = None,
    ) -> None:
        await self._handle_status(interaction, user)

    # ---- /manage setup: administrators only ---------------------------- #
    @manage_group.command(
        name="setup",
        description="Configure this server's punish / post-punish / staff roles, "
                    "staff channel, and DMs.",
    )
    @app_commands.describe(
        punish_role="The role given during punishment.",
        post_role="The role given after the timer expires.",
        staff_role="Optional role that marks a member as staff (protected from punishment).",
        staff_channel="Optional channel where staff get embed notifications.",
        dm_user="Whether to DM the punished member an embed about their punishment.",
    )
    async def setup_cmd(
        self,
        interaction: discord.Interaction,
        punish_role: discord.Role,
        post_role: discord.Role,
        staff_role: Optional[discord.Role] = None,
        staff_channel: Optional[discord.TextChannel] = None,
        dm_user: Optional[bool] = None,
    ) -> None:
        await self._handle_setup(
            interaction,
            punish_role,
            post_role,
            staff_role,
            staff_channel,
            dm_user,
        )

    # ---- /manage fixcommands: clean up duplicated slash commands -------- #
    @manage_group.command(
        name="fixcommands",
        description="Remove duplicated slash commands and re-sync this server.",
    )
    async def fixcommands_cmd(self, interaction: discord.Interaction) -> None:
        await self._handle_fixcommands(interaction)

    # ------------------------------------------------------------------ #
    # Command implementations
    # ------------------------------------------------------------------ #
    async def _handle_punish(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        duration: str,
        reason: Optional[str],
    ) -> None:
        # All responses are ephemeral. Defer first so we have time to act.
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
        except discord.InteractionResponded:
            pass
        except discord.HTTPException:
            return

        if not await self._require_moderator(interaction):
            return

        cfg = get_guild_config(self.bot.config, interaction.guild_id)
        if not cfg:
            await self._safe_followup(
                interaction,
                "This server is not configured yet. An admin should run "
                "`/manage setup` or edit `config.json`.",
            )
            return

        if user.id == interaction.user.id:
            await self._safe_followup(interaction, "You can't punish yourself.")
            return

        # Refuse to punish admins, mods, and anyone with the staff role.
        protected_reason = is_protected_member(
            user, self.bot.config, guild=interaction.guild
        )
        if protected_reason is not None:
            await self._safe_followup(interaction, protected_reason)
            return

        seconds = parse_duration(duration)
        if seconds is None or seconds < 5:
            await self._safe_followup(
                interaction,
                "Invalid duration. Try e.g. `30m`, `2h`, `1d12h`.",
            )
            return
        if seconds > 60 * 60 * 24 * 30:  # 30 days cap
            await self._safe_followup(
                interaction, "Duration too long (max 30 days)."
            )
            return

        result = await self.bot.start_punishment(
            guild=interaction.guild,
            member=user,
            moderator=interaction.user,  # type: ignore[arg-type]
            duration_seconds=seconds,
            reason=reason or "No reason provided.",
            cfg=cfg,
        )

        if result is None:
            await self._safe_followup(
                interaction,
                "Could not start punishment: one or more configured roles are missing.",
            )
            return
        if "error" in result:
            await self._safe_followup(interaction, result["error"])
            return

        pretty = format_duration(seconds)
        reason_text = reason or "No reason provided."
        started_at: datetime = result["started_at"]
        expires_at: datetime = result["expires_at"]

        await self._safe_followup(
            interaction,
            f"Punished {user.mention} for **{pretty}**.\nReason: {reason_text}",
        )

        # Staff channel embed.
        staff_embed = self.bot._build_punish_staff_embed(
            member=user,
            moderator=interaction.user,  # type: ignore[arg-type]
            duration_seconds=seconds,
            reason=reason_text,
            started_at=started_at,
            expires_at=expires_at,
        )
        await self.bot._send_staff_embed(interaction.guild, staff_embed)

        # DM the punished user.
        if should_dm_user(self.bot.config, interaction.guild_id):
            dm_embed = self.bot._build_punish_dm_embed(
                guild_name=interaction.guild.name,
                moderator_name=str(interaction.user),
                duration_seconds=seconds,
                reason=reason_text,
                started_at=started_at,
                expires_at=expires_at,
            )
            sent = await self.bot._dm_embed(user, dm_embed)
            if not sent:
                logger.info(
                    "Skipped DM to %s: DMs unavailable.", user.id
                )

    def _moderation_denial(self, interaction: discord.Interaction) -> Optional[str]:
        """Why the caller may not run a moderation command, or ``None``.

        One rule for every moderation command (punish, pardon, warn, warnings,
        status): the caller must hold the configured staff role, or be an
        administrator. See :func:`settings.moderation_denial` for why Discord's
        *Moderate Members* permission is not enough on its own.
        """
        return moderation_denial(
            interaction.user,
            self.bot.config,
            guild=getattr(interaction, "guild", None),
            guild_id=getattr(interaction, "guild_id", None),
        )

    async def _require_moderator(self, interaction: discord.Interaction) -> bool:
        """Send the refusal and return ``False`` when the caller is not staff.

        Every moderation handler starts here, so a member who guesses the
        command name — or a server that relaxed the command's visibility in
        Discord's integration settings — still cannot punish anyone.
        """
        denial = self._moderation_denial(interaction)
        if denial is None:
            return True
        await self._safe_followup(interaction, denial)
        return False

    async def _handle_warn(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reason: str,
    ) -> None:
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
        except discord.InteractionResponded:
            pass
        except discord.HTTPException:
            return

        if interaction.guild is None or interaction.guild_id is None:
            await self._safe_followup(
                interaction, "Warnings only work inside a server."
            )
            return

        if not await self._require_moderator(interaction):
            return

        if user.id == interaction.user.id:
            await self._safe_followup(interaction, "You can't warn yourself.")
            return

        protected_reason = is_protected_member(
            user, self.bot.config, guild=interaction.guild
        )
        if protected_reason is not None:
            await self._safe_followup(interaction, protected_reason)
            return

        cleaned = (reason or "").strip()
        if not cleaned:
            await self._safe_followup(
                interaction, "Please provide a reason for the warning."
            )
            return
        if len(cleaned) > MAX_WARNING_REASON_LENGTH:
            await self._safe_followup(
                interaction,
                f"Keep the reason under {MAX_WARNING_REASON_LENGTH} characters.",
            )
            return

        warning_id = add_warning(
            interaction.guild_id, user.id, interaction.user.id, cleaned
        )
        if warning_id is None:
            await self._safe_followup(
                interaction, "Could not save the warning (database error)."
            )
            return

        total = count_warnings(interaction.guild_id, user.id)
        created_at = datetime.now(timezone.utc)
        await self._safe_followup(
            interaction,
            f"Warned {user.mention} - warning **#{total}** on record.\n"
            f"Reason: {cleaned}",
        )

        staff_embed = self.bot._build_warn_staff_embed(
            member=user,
            moderator=interaction.user,  # type: ignore[arg-type]
            reason=cleaned,
            created_at=created_at,
            total=total,
        )
        await self.bot._send_staff_embed(interaction.guild, staff_embed)

        if should_dm_user(self.bot.config, interaction.guild_id):
            dm_embed = self.bot._build_warn_dm_embed(
                guild_name=interaction.guild.name,
                moderator_name=str(interaction.user),
                reason=cleaned,
                total=total,
                created_at=created_at,
            )
            sent = await self.bot._dm_embed(user, dm_embed)
            if not sent:
                logger.info("Skipped warning DM to %s: DMs unavailable.", user.id)

    async def _handle_warnings(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        clear: bool = False,
    ) -> None:
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
        except discord.InteractionResponded:
            pass
        except discord.HTTPException:
            return

        if interaction.guild is None or interaction.guild_id is None:
            await self._safe_followup(
                interaction, "Warnings only work inside a server."
            )
            return

        if not await self._require_moderator(interaction):
            return

        if clear:
            try:
                removed = clear_warnings(interaction.guild_id, user.id)
            except sqlite3.Error as exc:
                logger.warning("Failed to clear warnings for %s: %s", user.id, exc)
                await self._safe_followup(
                    interaction, "Could not clear the warnings (database error)."
                )
                return
            if removed == 0:
                await self._safe_followup(
                    interaction, f"{user.mention} has no warnings to clear."
                )
                return
            await self._safe_followup(
                interaction,
                f"Cleared **{removed}** warning(s) for {user.mention}.",
            )
            staff_embed = self.bot._build_warn_clear_staff_embed(
                member=user,
                moderator=interaction.user,  # type: ignore[arg-type]
                removed=removed,
            )
            await self.bot._send_staff_embed(interaction.guild, staff_embed)
            return

        total = count_warnings(interaction.guild_id, user.id)
        rows = list_warnings(interaction.guild_id, user.id, limit=15)
        lines = [f"**Warnings for {user.mention}:** {total}", ""]
        if rows:
            for row in rows:
                try:
                    created = datetime.fromisoformat(row["created_at"])
                    stamp = f"<t:{int(created.timestamp())}:d>"
                except (TypeError, ValueError):
                    stamp = str(row["created_at"])
                lines.append(
                    f"- {stamp} by <@{row['moderator_id']}> - {row['reason']}"
                )
            if total > len(rows):
                lines.append(f"... and {total - len(rows)} older warning(s).")
        else:
            lines.append("- (none recorded)")
        await self._safe_followup(interaction, "\n".join(lines))

    async def _handle_pardon(
        self, interaction: discord.Interaction, user: discord.Member
    ) -> None:
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
        except discord.InteractionResponded:
            pass
        except discord.HTTPException:
            return

        if not await self._require_moderator(interaction):
            return

        cfg = get_guild_config(self.bot.config, interaction.guild_id)
        if not cfg:
            await self._safe_followup(
                interaction,
                "This server is not configured yet. An admin should run "
                "`/manage setup` or edit `config.json`.",
            )
            return

        record = db_fetchone(
            "SELECT * FROM punishments WHERE guild_id = ? AND user_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (interaction.guild_id, user.id),
        )
        if not record:
            await self._safe_followup(
                interaction, "That user has no active punishment on record."
            )
            return

        punish_role = interaction.guild.get_role(record["punish_role_id"])
        post_role = interaction.guild.get_role(record["post_role_id"])

        try:
            if punish_role and punish_role in user.roles:
                await user.remove_roles(
                    punish_role, reason=f"Pardoned by {interaction.user}"
                )
            if post_role and post_role in user.roles:
                await user.remove_roles(
                    post_role, reason=f"Pardoned by {interaction.user}"
                )
        except discord.Forbidden:
            await self._safe_followup(
                interaction, "I lack permission to change that user's roles."
            )
            return

        db_execute("DELETE FROM punishments WHERE id = ?", (record["id"],))
        archive_punishment(dict(record), ended_reason="pardoned")
        await self._safe_followup(
            interaction,
            f"Pardoned {user.mention}. The punish role has been removed.",
        )
        # Staff embed.
        staff_embed = self.bot._build_pardon_staff_embed(
            member=user,
            moderator=interaction.user,  # type: ignore[arg-type]
            reason="Pardoned by moderator",
            was_active=True,
        )
        await self.bot._send_staff_embed(interaction.guild, staff_embed)
        # DM the user.
        if should_dm_user(self.bot.config, interaction.guild_id):
            dm_embed = self.bot._build_pardon_dm_embed(
                guild_name=interaction.guild.name,
                moderator_name=str(interaction.user),
            )
            await self.bot._dm_embed(user, dm_embed)

    async def _handle_setup(
        self,
        interaction: discord.Interaction,
        punish_role: discord.Role,
        post_role: discord.Role,
        staff_role: Optional[discord.Role],
        staff_channel: Optional[discord.TextChannel],
        dm_user: Optional[bool],
    ) -> None:
        if not command_tree.is_administrator(interaction):
            await interaction.response.send_message(
                "Only server administrators can change this server's "
                "configuration.",
                ephemeral=True,
            )
            return
        if staff_role is not None:
            role_ids = {punish_role.id, post_role.id, staff_role.id}
        else:
            role_ids = {punish_role.id, post_role.id}
        # All roles must be distinct.
        if len(role_ids) != (3 if staff_role is not None else 2):
            await interaction.response.send_message(
                "All configured roles must be different from each other.",
                ephemeral=True,
            )
            return

        # Store complete per-guild settings and mirror to the installer shape
        # for the configured primary server. Per-guild overrides are preferred
        # by the helpers above, so one server can be configured independently.
        guilds = self.bot.config.get("guilds", {})
        if not isinstance(guilds, dict):
            guilds = {}
            self.bot.config["guilds"] = guilds
        guild_key = str(interaction.guild_id)
        guild_cfg = guilds.get(guild_key, {})
        guild_cfg = dict(guild_cfg) if isinstance(guild_cfg, dict) else {}
        guild_cfg.update({
            "punish_role_id": punish_role.id,
            "post_role_id": post_role.id,
        })
        if staff_role is not None:
            guild_cfg["staff_role_id"] = staff_role.id
        if staff_channel is not None:
            guild_cfg["staff_channel_id"] = staff_channel.id
        if dm_user is not None:
            guild_cfg["dm_user"] = bool(dm_user)
        guilds[guild_key] = guild_cfg

        try:
            is_primary = bool(self.bot.config.get("server_id")) and int(
                self.bot.config["server_id"]
            ) == int(interaction.guild_id)
        except (TypeError, ValueError):
            is_primary = False
        if is_primary:
            self.bot.config["punish_role_id"] = punish_role.id
            self.bot.config["post_role_id"] = post_role.id
            if staff_role is not None:
                self.bot.config["staff_role_id"] = staff_role.id
        if staff_channel is not None:
            self.bot.config["staff_channel_id"] = staff_channel.id
            self.bot.config["log_channel_id"] = staff_channel.id  # legacy key
        if dm_user is not None:
            self.bot.config["dm_user"] = bool(dm_user)

        save_config(self.bot.config)
        lines = [
            "Saved configuration for this server:",
            f"- Punish role: {punish_role.mention}",
            f"- Post-punish role: {post_role.mention}",
        ]
        if staff_role is not None:
            lines.append(f"- Staff role (protected): {staff_role.mention}")
        if staff_channel is not None:
            lines.append(f"- Staff channel: {staff_channel.mention}")
        if dm_user is not None:
            lines.append(f"- DM the user: {bool(dm_user)}")
        await interaction.response.send_message(
            "\n".join(lines), ephemeral=True
        )

    async def _handle_fixcommands(self, interaction: discord.Interaction) -> None:
        """Delete the duplicate (global) half of this bot's slash commands.

        Discord's global and per-guild command registries are independent, so a
        command registered in both is listed twice. Startup empties the global
        registry automatically; this command does the same on demand and
        re-uploads this server's copy so what remains is fresh.
        """
        if not command_tree.is_administrator(interaction):
            await self._safe_followup(
                interaction, "Only server administrators can re-sync commands."
            )
            return
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
        except (discord.InteractionResponded, discord.HTTPException):
            pass

        try:
            removed = await self.bot._clear_global_commands(
                reason=f"requested with /fixcommands in guild {interaction.guild_id}"
            )
            resynced = await self.bot._sync_guild_commands(
                interaction.guild_id, force=True
            )
            remaining = await self.bot._fetch_global_command_count()
        except discord.DiscordException as exc:
            logger.warning("Could not remove duplicate commands: %s", exc)
            await self._safe_followup(
                interaction,
                "Could not remove the duplicate commands "
                f"(Discord said: {exc}). Please try again in a minute.",
            )
            return

        if removed is None:
            lines = ["Cleared Discord's global command registry."]
        elif removed == 0:
            lines = [
                "No duplicated commands found: each command is registered once."
            ]
        else:
            lines = [f"Removed {removed} duplicated command(s)."]
        lines.append(
            "Re-synced this server's commands."
            if resynced
            else "Could not re-sync this server; it will be retried on the next connect."
        )
        if remaining:
            lines.append(
                f"{remaining} global command(s) came back - is another copy of "
                "the bot still running with the same token?"
            )
        lines.append(
            "Discord can take a few minutes to refresh the command list; "
            "reopening Discord shows the result right away."
        )
        await self._safe_followup(interaction, "\n".join(lines))

    async def _handle_status(
        self,
        interaction: discord.Interaction,
        user: Optional[discord.Member] = None,
    ) -> None:
        if not await self._require_moderator(interaction):
            return

        cfg = get_guild_config(self.bot.config, interaction.guild_id)
        if not cfg:
            await interaction.response.send_message(
                "This server is not configured yet. An admin should run "
                "`/manage setup` or edit `config.json`.",
                ephemeral=True,
            )
            return
        punish = interaction.guild.get_role(cfg["punish_role_id"])
        post = interaction.guild.get_role(cfg["post_role_id"])
        staff_role_id = get_staff_role_id(self.bot.config, interaction.guild_id)
        staff_role = (
            interaction.guild.get_role(staff_role_id) if staff_role_id else None
        )

        if user is not None:
            await self._status_for_user(interaction, cfg, user)
            return

        # Server-wide summary.
        active = db_fetchall(
            "SELECT user_id, expires_at FROM punishments WHERE guild_id = ? "
            "ORDER BY expires_at",
            (interaction.guild_id,),
        )
        lines = [
            "**Server configuration**",
            f"- Punish role: {punish.mention if punish else '(missing)'}",
            f"- Post-punish role: {post.mention if post else '(missing)'}",
            f"- Staff role (protected): {staff_role.mention if staff_role else '(not set)'}",
            "",
            f"**Active punishments:** {len(active)}",
        ]
        for row in active[:10]:
            member = interaction.guild.get_member(row["user_id"])
            exp = datetime.fromisoformat(row["expires_at"])
            delta = exp - datetime.now(timezone.utc)
            who = member.mention if member else f"<@{row['user_id']}>"
            lines.append(
                f"- {who} - ends in "
                f"{format_duration(max(0, int(delta.total_seconds())))}"
            )
        if len(active) > 10:
            lines.append(f"... and {len(active) - 10} more.")
        await interaction.response.send_message(
            "\n".join(lines), ephemeral=True
        )

    async def _status_for_user(
        self,
        interaction: discord.Interaction,
        cfg: dict,
        user: discord.Member,
    ) -> None:
        """Show a single user's active + historical punishment state."""
        # Currently active?
        active = db_fetchone(
            "SELECT * FROM punishments WHERE guild_id = ? AND user_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (interaction.guild_id, user.id),
        )
        # Past punishments: try the history table first; if it doesn't exist
        # (old DBs / pre-migration), gracefully fall back to nothing.
        history_rows: list[sqlite3.Row] = []
        try:
            history_rows = db_fetchall(
                "SELECT reason, started_at, duration_seconds, moderator_id, "
                "       ended_reason "
                "FROM punishment_history "
                "WHERE guild_id = ? AND user_id = ? "
                "ORDER BY started_at DESC LIMIT 10",
                (interaction.guild_id, user.id),
            )
        except sqlite3.OperationalError:
            # Table doesn't exist yet. That's fine; just show no history.
            history_rows = []

        lines = [f"**Punishment status for {user.mention}**", ""]

        if active is not None:
            exp = datetime.fromisoformat(active["expires_at"])
            now = datetime.now(timezone.utc)
            remaining = int((exp - now).total_seconds())
            remaining_text = (
                format_duration(max(0, remaining)) if remaining > 0
                else "ending now"
            )
            mod_id = active["moderator_id"]
            lines.extend([
                "**Currently active:**",
                f"- Stage: `{active['stage']}`",
                f"- Reason: {active['reason'] or 'No reason provided.'}",
                f"- Started: <t:{int(datetime.fromisoformat(active['started_at']).timestamp())}:f>",
                f"- Ends in: {remaining_text}",
                f"- Moderator: <@{mod_id}>",
            ])
        else:
            lines.append("No active punishment.")

        lines.append("")
        lines.append(f"**Recent history:** {len(history_rows)}")
        if history_rows:
            for row in history_rows:
                started = datetime.fromisoformat(row["started_at"])
                duration_s = int(row["duration_seconds"])
                mod_id = row["moderator_id"]
                ended = row["ended_reason"] or "completed"
                lines.append(
                    f"- <t:{int(started.timestamp())}:d> "
                    f"for {format_duration(duration_s)} "
                    f"by <@{mod_id}> - ended: {ended} - "
                    f"reason: {row['reason'] or 'n/a'}"
                )
        else:
            lines.append("- (none recorded)")

        # Warnings are tracked separately from punishments and never expire,
        # so a member's status shows the running total plus the latest few.
        warning_total = count_warnings(interaction.guild_id, user.id)
        recent_warnings = list_warnings(interaction.guild_id, user.id, limit=3)
        lines.append("")
        lines.append(f"**Warnings:** {warning_total}")
        if recent_warnings:
            for row in recent_warnings:
                try:
                    created = datetime.fromisoformat(row["created_at"])
                    stamp = f"<t:{int(created.timestamp())}:d>"
                except (TypeError, ValueError):
                    stamp = str(row["created_at"])
                lines.append(
                    f"- {stamp} by <@{row['moderator_id']}> - {row['reason']}"
                )
            if warning_total > len(recent_warnings):
                lines.append(
                    f"... and {warning_total - len(recent_warnings)} more "
                    "(use `/manage warnings`)."
                )
        else:
            lines.append("- (none recorded)")

        await interaction.response.send_message(
            "\n".join(lines), ephemeral=True
        )

    # ------------------------------------------------------------------ #
    # Robustly send a followup message; never raise.
    # ------------------------------------------------------------------ #
    async def _safe_followup(
        self, interaction: discord.Interaction, content: str
    ) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(content, ephemeral=True)
            else:
                await interaction.response.send_message(content, ephemeral=True)
        except (discord.InteractionResponded, discord.HTTPException) as exc:
            logger.warning("Failed to send interaction response: %s", exc)


# Add the cog to the bot. We do this BEFORE setup_hook runs (so the
# tree has the commands by the time it syncs).
async def _register_cog() -> None:
    if bot.get_cog("SentinelCog") is None:
        # One cog owns /manage: the application sub-group from
        # ApplicationsMixin, plus the component routing it shares with the
        # moderation commands defined below it.
        await bot.add_cog(SentinelCog(bot))
    if bot.get_cog("ApplicationsCog") is None:
        # /apply: the member-facing command, published as a top-level command
        # of its own.
        await bot.add_cog(ApplicationsCog(bot))


# --------------------------------------------------------------------------- #
# Slash command error handling - the part that almost always "breaks".
# --------------------------------------------------------------------------- #
# Two things matter here:
#   1) Every error path must produce SOME user-visible feedback, even if
#      interaction.response is already done. We use followup as fallback.
#   2) We must not double-respond (which raises InteractionResponded).
# --------------------------------------------------------------------------- #
@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    # Check failures: the predicate should have already sent the user-facing
    # message. If it didn't (e.g. it crashed), send a generic one.
    if isinstance(error, app_commands.CheckFailure):
        msg = "You don't have permission to run that command."
    elif isinstance(error, app_commands.CommandNotFound):
        return  # silently ignore
    elif isinstance(error, app_commands.CommandOnCooldown):
        msg = f"That command is on cooldown. Try again in {error.retry_after:.1f}s."
    elif isinstance(error, app_commands.MissingPermissions):
        msg = f"You're missing permissions: {', '.join(error.missing_permissions)}."
    elif isinstance(error, app_commands.BotMissingPermissions):
        msg = (
            f"I'm missing permissions: {', '.join(error.missing_permissions)}. "
            "Check the bot's role position and channel overrides."
        )
    elif isinstance(error, app_commands.TransformerError):
        msg = f"Invalid value for `{error.param.name}`."
    elif isinstance(error, app_commands.NoPrivateMessage):
        msg = "This command can only be used in a server."
    else:
        logger.exception("Unhandled slash command error: %s", error)
        msg = "Something went wrong while running that command."

    # Send via followup if already responded, otherwise via response.
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except (discord.InteractionResponded, discord.HTTPException) as exc:
        logger.warning("Failed to deliver error message: %s", exc)


# --------------------------------------------------------------------------- #
# Override setup_hook to register the cog before syncing.
# --------------------------------------------------------------------------- #
_original_setup_hook = SentinelBot.setup_hook


async def setup_hook(self: SentinelBot) -> None:
    await _register_cog()
    await _original_setup_hook(self)
    try:
        await self.dashboard.start()
    except Exception:
        logger.exception("Could not start the server dashboard; the Discord bot will keep running.")
    _log_remote_access_notes(self.config)
    try:
        await self.duckdns.start()
    except Exception:
        logger.exception("Could not start the DuckDNS updater; the Discord bot will keep running.")


def _log_remote_access_notes(config: dict) -> None:
    """Say, once at startup, what a remote visitor (e.g. over DuckDNS) would hit.

    Everything here is advisory: the dashboard starts either way. It exists
    because each of these misconfigurations shows up in the browser as a
    connection or login failure that says nothing about the fix.
    """
    if duckdns.configured(config):
        logger.info("%s", duckdns.describe_status(config))
    for warning in duckdns.dashboard_warnings(config):
        logger.warning("Remote access: %s", warning)


SentinelBot.setup_hook = setup_hook  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main() -> int:
    _log_path_notes()
    init_db()
    # The ticket system is gone, so its table is dropped here rather than in
    # init_db(): SCHEMA no longer creates it, and this is a one-way migration
    # that belongs with the other removed-feature cleanup. It runs before login
    # so the database is tidy even if the token turns out to be bad.
    if legacy_cleanup.drop_tickets_table():
        logger.info("Dropped the %s table left by the removed ticket system.",
                    legacy_cleanup.TICKETS_TABLE)
    token = resolve_token(bot.config)
    if not token:
        logger.error(
            "No Discord token found. Set the DISCORD_TOKEN environment variable, "
            "or put it in config.json under 'bot_token' (or the legacy 'token')."
        )
        return 1
    try:
        bot.run(token, log_handler=None)
    except discord.LoginFailure:
        logger.error("Login failed - is the token valid?")
        return 1
    return 0


def _print_help() -> None:
    sys.stdout.write(
        "Usage: sentinel [options]\n"
        "\n"
        "Options:\n"
        "  (no args)         Start the bot. If no token is configured,\n"
        "                    run the interactive installer first.\n"
        "  --install         Force the interactive installer to run.\n"
        "  --reinstall       Same as --install; overwrites existing config.\n"
        "  --install-service Install a background service for the bot\n"
        "                    (systemd on Linux, launchd on macOS, NSSM\n"
        "                    on Windows). Requires admin / sudo.\n"
        "  --uninstall-service\n"
        "                    Remove the background service.\n"
        "  --paths           Print where config, database and logs live.\n"
        "  --dashboard-token Print the web dashboard login key (creating one\n"
        "                    on first run). Also stored as \"dashboard_token\"\n"
        "                    in config.json.\n"
        "  --dashboard       Show how the dashboard is reachable: the URLs, the\n"
        "                    TLS and proxy settings, and what to fix for remote\n"
        "                    access (e.g. over DuckDNS).\n"
        "  --duckdns         Send one DuckDNS update now and report the result,\n"
        "                    then exit. The bot does this on a timer anyway;\n"
        "                    this is for testing the setup.\n"
        "  --version         Print the version of this build.\n"
        "  --help, -h        Show this message.\n"
        "\n"
        "Files: the app directory is read-only in packaged installs, so\n"
        "state lives in a per-user/per-system location. Override with\n"
        "  SENTINEL_HOME      (data + config base directory)\n"
        "  SENTINEL_DATA      (db + log directory)\n"
        "  SENTINEL_CONFIG    (config.json path)\n"
        "Run --paths to see the resolved locations.\n"
        "\n"
    )


def _describe_dashboard() -> int:
    """Print the effective dashboard settings and how to reach them.

    The address a remote user needs is not obvious from config.json: it depends
    on the bind address, the scheme, whether a proxy is in front, and whether
    the public name is in the Host allowlist. This prints the conclusion.
    """
    config = bot.config
    sys.stdout.write(f"Sentinel {paths.app_version()}\n")
    if not config.get("dashboard_enabled", True):
        sys.stdout.write("Dashboard: disabled ('dashboard_enabled': false).\n")
        return 0

    server = bot.dashboard
    try:
        server._apply_settings(config)
    except ValueError as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 1

    sys.stdout.write(f"Listening on:  {server.scheme}://{server._host}:{server._port}/\n")
    if server._public_url:
        sys.stdout.write(
            f"Public URL:    {server._public_url.rstrip('/')}/   <- open this one\n"
        )
    sys.stdout.write(f"Browser sees:  {server.browser_scheme} (cookie rules)\n")
    allowed = config.get("dashboard_allowed_hosts", [])
    if isinstance(allowed, str):
        allowed = [allowed]
    sys.stdout.write(
        "Allowed Host:  " + (", ".join(str(item) for item in allowed) or "(none configured)")
        + "\n"
    )
    proxies = config.get("dashboard_trusted_proxies", [])
    sys.stdout.write(
        "Trusted proxy: " + (", ".join(str(item) for item in proxies) or "(none)")
        + "\n"
    )
    sys.stdout.write(
        "TLS here:      " + ("yes (dashboard_tls_cert)" if server._tls_context else "no")
        + "\n"
    )
    sys.stdout.write(f"Secure cookie: {'yes' if server._secure_cookie else 'no'}\n")
    sys.stdout.write(duckdns.describe_status(config) + "\n")
    last_result = bot.duckdns.last_result
    if last_result is not None:  # pragma: no cover - only after an update ran
        sys.stdout.write(f"Last update:   {last_result.describe()}\n")

    warnings = list(server.remote_access_warnings()) + duckdns.dashboard_warnings(config)
    if warnings:
        sys.stdout.write("\nTo fix for remote access:\n")
        for warning in warnings:
            sys.stdout.write(f"  - {warning}\n")
    else:
        sys.stdout.write("\nNo remote-access problems found.\n")
    sys.stdout.write(
        "\nLogin key: run 'sentinel --dashboard-token' on this machine.\n"
    )
    return 0


def _duckdns_update_once() -> int:
    """Run a single DuckDNS update and print the outcome (for testing)."""
    config = bot.config
    if not duckdns.configured(config):
        sys.stderr.write(
            "ERROR: DuckDNS is not configured. Set 'duckdns_domain' and "
            "'duckdns_token' in config.json (or SENTINEL_DUCKDNS_DOMAIN / "
            "SENTINEL_DUCKDNS_TOKEN).\n"
        )
        return 1

    async def run() -> duckdns.UpdateResult:
        updater = duckdns.DuckDNSUpdater(config)
        try:
            result = await updater.update_now()
        finally:
            await updater.stop()
        assert result is not None
        return result

    result = asyncio.run(run())
    sys.stdout.write(result.describe() + "\n")
    if result.ok:
        for warning in duckdns.dashboard_warnings(config):
            sys.stdout.write(f"NOTE: {warning}\n")
        return 0
    return 1


def _service_install() -> int:
    """Install the bot as a background service for this OS."""
    if sys.platform.startswith("linux"):
        return _service_install_linux()
    if sys.platform == "darwin":
        return _service_install_macos()
    if sys.platform.startswith("win"):
        return _service_install_windows()
    sys.stderr.write(f"Service install not supported on {sys.platform}\n")
    return 1


def _service_uninstall() -> int:
    """Remove the background service for this OS."""
    if sys.platform.startswith("linux"):
        return _service_uninstall_linux()
    if sys.platform == "darwin":
        return _service_uninstall_macos()
    if sys.platform.startswith("win"):
        return _service_uninstall_windows()
    sys.stderr.write(f"Service uninstall not supported on {sys.platform}\n")
    return 1


# Fallback unit used when the shipped build/linux/sentinel.service
# isn't reachable (e.g. running the PyInstaller binary, where the source tree
# isn't installed). Paths are filled in from this process so the unit matches
# wherever the executable actually lives.
_LINUX_UNIT_TEMPLATE = """[Unit]
Description=Sentinel Discord bot
Documentation=https://github.com/mob5824m-wq/Sentinel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=sentinel
Group=sentinel
WorkingDirectory={app_dir}
ExecStart={exe}
Restart=on-failure
RestartSec=10

# Sandboxing - tighten if your distro supports it.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/sentinel /etc/sentinel

# Logging.
StandardOutput=journal
StandardError=journal
SyslogIdentifier=sentinel

[Install]
WantedBy=multi-user.target
"""


def _systemd_unit_text() -> str:
    """Body for the systemd unit: shipped file if present, else generated.

    The ExecStart/WorkingDirectory lines are rewritten to match wherever this
    process is actually running from, so `--install-service` works for a binary
    unpacked outside /opt and for a source checkout (via the venv's python).
    """
    if paths.is_frozen():
        # `sys.executable` *is* the app in a PyInstaller build. Note that
        # resolve() is deliberately not used: for a source install it would
        # follow a venv symlink to the base interpreter and drop site-packages.
        exe = str(Path(sys.executable))
        work_dir = str(Path(sys.executable).parent)
    else:
        script = paths.APP_DIR / "bot.py"
        exe = f"{Path(sys.executable)} {script}"
        work_dir = str(paths.APP_DIR)

    shipped = paths.resource_path("build", "linux", "sentinel.service")
    if shipped is not None:
        text = shipped.read_text(encoding="utf-8")
    else:
        text = _LINUX_UNIT_TEMPLATE.format(app_dir=work_dir, exe=exe)

    if not text.endswith("\n"):
        text += "\n"
    lines = []
    for line in text.splitlines():
        if line.startswith("ExecStart="):
            line = f"ExecStart={exe}"
        elif line.startswith("WorkingDirectory="):
            line = f"WorkingDirectory={work_dir}"
        lines.append(line)
    return "\n".join(lines) + "\n"


def _service_install_linux() -> int:
    """systemd: install + enable (but don't auto-start) the service."""
    if os.geteuid() != 0:
        sys.stderr.write("Re-run with sudo to install the service.\n")
        return 1
    unit_dst = Path("/lib/systemd/system/sentinel.service")
    unit_text = _systemd_unit_text()
    unit_dst.parent.mkdir(parents=True, exist_ok=True)
    unit_dst.write_text(unit_text, encoding="utf-8")
    # Idempotent user / group / state dirs.
    subprocess.run(["groupadd", "-rf", "sentinel"],
                   check=False, capture_output=True)
    subprocess.run(
        [
            "useradd", "-r", "-g", "sentinel",
            "-d", "/var/lib/sentinel",
            "-s", "/usr/sbin/nologin",
            "-c", "Sentinel",
            "sentinel",
        ],
        check=False, capture_output=True,
    )
    for d in ("/var/lib/sentinel", "/etc/sentinel"):
        Path(d).mkdir(parents=True, exist_ok=True)
    _chown_state_dirs()
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "sentinel.service"], check=True)
    sys.stdout.write(
        "Installed systemd unit. Start with: "
        "sudo systemctl start sentinel\n"
    )
    return 0


def _chown_state_dirs() -> None:
    """Give the service user write access to its state + config dirs.

    Mirrors build/linux/postinst. Without this, a service running as
    `sentinel` gets PermissionError writing its db/log because
    `mkdir` above (run as root) leaves the dirs root-owned.
    """
    import pwd  # noqa: PLC0415 - posix-only, imported lazily
    import grp  # noqa: PLC0415

    try:
        pw = pwd.getpwnam("sentinel")
    except KeyError:
        return
    try:
        uid, gid = pw.pw_uid, pw.pw_gid
        state = Path("/var/lib/sentinel")
        os.chown(str(state), uid, gid)
        os.chmod(str(state), 0o750)
        # Anything already sitting there (e.g. a db or config written by a
        # `sudo sentinel` run) has to belong to the service too.
        for path in sorted(state.rglob("*")):
            try:
                os.chown(str(path), uid, gid)
            except OSError:
                pass
        # Config dir: root-owned, group-readable by the service user.
        os.chown("/etc/sentinel", 0, gid)
        os.chmod("/etc/sentinel", 0o750)
        cfg = Path("/etc/sentinel/config.json")
        if cfg.exists():
            os.chown(str(cfg), 0, gid)
            os.chmod(str(cfg), 0o640)
    except (OSError, grp.error) as exc:
        sys.stderr.write(f"warning: could not set ownership on state dirs: {exc}\n")


def _service_uninstall_linux() -> int:
    if os.geteuid() != 0:
        sys.stderr.write("Re-run with sudo to uninstall the service.\n")
        return 1
    subprocess.run(["systemctl", "disable", "--quiet",
                    "sentinel.service"], check=False)
    subprocess.run(["systemctl", "stop",    "--quiet",
                    "sentinel.service"], check=False)
    unit = Path("/lib/systemd/system/sentinel.service")
    if unit.exists():
        unit.unlink()
    subprocess.run(["systemctl", "daemon-reload"], check=False)
    sys.stdout.write("Removed systemd unit.\n")
    return 0


# Fallback launchd agent for packaged macOS builds, where the source tree
# (and therefore build/macos/*.plist) isn't installed.
_MACOS_PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.arena.sentinel</string>

    <key>ProgramArguments</key>
    <array>
        <string>{exe}</string>
    </array>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
        <key>Crashed</key>
        <true/>
    </dict>

    <key>ThrottleInterval</key>
    <integer>10</integer>

    <key>StandardOutPath</key>
    <string>{outlog}</string>

    <key>StandardErrorPath</key>
    <string>{errlog}</string>

    <key>WorkingDirectory</key>
    <string>{workdir}</string>
</dict>
</plist>
"""


def _service_install_macos() -> int:
    plist_dst = Path.home() / "Library" / "LaunchAgents" / "com.arena.sentinel.plist"
    plist_src = paths.resource_path("build", "macos", "com.arena.sentinel.plist")
    if plist_src is not None:
        text = plist_src.read_text(encoding="utf-8")
    else:
        # Packaged build (no source tree installed) or the shipped plist is
        # missing: generate one that points at the binary being run, and send
        # its output to the writable data dir rather than /tmp.
        exe = Path(sys.executable).resolve() if paths.is_frozen() else (
            Path(sys.argv[0]).resolve() if sys.argv[0] else Path(sys.executable).resolve()
        )
        text = _MACOS_PLIST_TEMPLATE.format(
            exe=exe,
            workdir=paths.DATA_DIR,
            outlog=paths.LOG_PATH.with_name("launchd.out.log"),
            errlog=paths.LOG_PATH.with_name("launchd.err.log"),
        )
    try:
        plist_dst.parent.mkdir(parents=True, exist_ok=True)
        plist_dst.write_text(text, encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(f"Cannot write launchd agent to {plist_dst}: {exc}\n")
        return 1
    subprocess.run(["launchctl", "load", "-w", str(plist_dst)], check=False)
    sys.stdout.write(
        f"Installed launchd agent at {plist_dst}.\n"
        "Start with: launchctl start com.arena.sentinel\n"
    )
    return 0


def _service_uninstall_macos() -> int:
    plist_dst = Path.home() / "Library" / "LaunchAgents" / "com.arena.sentinel.plist"
    if plist_dst.exists():
        subprocess.run(["launchctl", "unload", str(plist_dst)], check=False)
        plist_dst.unlink()
    sys.stdout.write("Removed launchd agent.\n")
    return 0


def _service_install_windows() -> int:
    """Windows service install via NSSM (if available) or schtasks fallback."""
    # Look for nssm.exe in PATH or alongside the binary.
    nssm = shutil.which("nssm") or shutil.which("nssm.exe")
    if nssm:
        exe = sys.executable if paths.is_frozen() else str(paths.APP_DIR / "bot.py")
        # Working directory must be writable (the install dir under
        # Program Files is not), and stdout/stderr belong in the data dir.
        subprocess.run([nssm, "install", "Sentinel", exe], check=True)
        subprocess.run([nssm, "set", "Sentinel",
                        "AppDirectory", str(paths.DATA_DIR)], check=True)
        for opt, value in (
            ("AppStdout", str(paths.LOG_PATH.with_name("service.out.log"))),
            ("AppStderr", str(paths.LOG_PATH.with_name("service.err.log"))),
            ("AppRotateFiles", "1"),
            ("AppRotateOnline", "1"),
        ):
            subprocess.run([nssm, "set", "Sentinel", opt, value],
                           check=False, capture_output=True)
        subprocess.run([nssm, "set", "Sentinel",
                        "DisplayName", "Sentinel"], check=True)
        subprocess.run([nssm, "set", "Sentinel",
                        "Description",
                        "Discord server management bot: moderation, "
                        "applications and a web dashboard."],
                       check=True)
        subprocess.run([nssm, "set", "Sentinel",
                        "Start", "SERVICE_AUTO_START"], check=True)
        sys.stdout.write(
            "Installed Windows service via NSSM. Start with:\n"
            "  sc start Sentinel\n"
        )
        return 0
    # Fallback: use the Task Scheduler so the bot starts on user login.
    sys.stdout.write(
        "NSSM not found; falling back to a Task Scheduler entry.\n"
        "Run once at logon: schtasks /create /tn Sentinel ...\n"
    )
    return 0


def _service_uninstall_windows() -> int:
    nssm = shutil.which("nssm") or shutil.which("nssm.exe")
    if nssm:
        subprocess.run([nssm, "stop", "Sentinel"],
                       check=False, capture_output=True)
        subprocess.run([nssm, "remove", "Sentinel", "confirm"],
                       check=False, capture_output=True)
    subprocess.run(
        ["schtasks", "/delete", "/tn", "Sentinel", "/f"],
        check=False, capture_output=True,
    )
    sys.stdout.write("Removed Windows service / task.\n")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]

    if args and args[0] in ("--help", "-h"):
        _print_help()
        sys.exit(0)

    if args and args[0] == "--version":
        sys.stdout.write(f"Sentinel {paths.app_version()}\n")
        sys.exit(0)

    if args and args[0] == "--paths":
        sys.stdout.write(f"Sentinel {paths.app_version()}\n")
        sys.stdout.write(paths.describe() + "\n")
        sys.exit(0)

    if args and args[0] == "--dashboard-token":
        try:
            sys.stdout.write(ensure_dashboard_token(bot.config, save_config) + "\n")
            sys.exit(0)
        except (OSError, ValueError) as exc:
            sys.stderr.write(f"ERROR: could not initialize dashboard key: {exc}\n")
            sys.exit(1)

    if args and args[0] == "--dashboard":
        sys.exit(_describe_dashboard())

    if args and args[0] == "--duckdns":
        sys.exit(_duckdns_update_once())

    if args and args[0] in ("--install", "--reinstall"):
        from installer import run_installer
        sys.exit(run_installer())

    if args and args[0] == "--install-service":
        sys.exit(_service_install())

    if args and args[0] == "--uninstall-service":
        sys.exit(_service_uninstall())

    # If the bot token is missing entirely, run the interactive installer
    # first so the user doesn't have to edit JSON by hand.
    try:
        has_token = bool(resolve_token(load_config()))
    except (OSError, RuntimeError) as exc:
        # Unusable config/state location: say so plainly instead of dying with
        # a traceback out of an import.
        sys.stderr.write(
            f"ERROR: cannot use the Sentinel files ({exc})\n"
            f"  config: {paths.config_path()}\n"
            f"  data:   {paths.DATA_DIR}\n"
            "Set SENTINEL_CONFIG / SENTINEL_DATA to writable\n"
            "paths, or run the installer with sudo (for a system service).\n"
        )
        sys.exit(1)
    if not has_token:
        from installer import run_installer
        if run_installer() == 0:
            # Re-read, or main() would still be looking at the empty config
            # loaded at import time and refuse to start after a successful
            # install.
            bot.config = load_config()
    sys.exit(main())
