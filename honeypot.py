"""The honeypot: trap channels that act on whoever posts in them.

A honeypot is a channel an administrator designates as a trap. Nothing about
it looks special to a member, but the moment someone sends a message there,
Sentinel treats it as a raid/bot indicator and acts automatically:

* the offending message is deleted (best-effort — the trap stays empty);
* the member is acted on with the configured ``honeypot_action``:

  - ``punish`` (default) — the normal punish-role flow for the server's
    default duration, so a human who wandered in is held, not nuked;
  - ``kick`` — removed from the server;
  - ``ban`` — banned outright;

* the trigger is recorded in the ``honeypot_traps`` table (user, channel,
  message content, action) for ``/manage honeypot list`` and an embed is
  posted in the staff channel.

Members the bot would refuse to punish anyway are never acted on: bots,
administrators, moderation-permission holders, the configured staff role, and
anyone at or above the bot's top role (see
:func:`settings.is_protected_member`). That is what makes the trap safe to
leave armed: staff can walk through the channel to check on it.

The slash-command half (``/manage honeypot …``) hangs off the shared
``/manage`` group exactly the way :mod:`applications` does: the group is a
class attribute of :class:`HoneypotMixin`, which is mixed into the cog that
owns ``manage_group`` (see ``bot.SentinelCog``), so the commands register with
the same tree without this module importing ``bot`` — the import would be
circular, because ``bot`` imports this module.

Administrators arm and disarm the trap and choose the action; listing the
channels and recent triggers is a staff query, so it uses the same staff rule
as the other moderation commands (:func:`settings.moderation_denial`).
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import store
from command_tree import is_administrator, manage_group
from settings import (
    HONEYPOT_ACTIONS,
    get_guild_config,
    get_honeypot_action,
    get_honeypot_channels,
    is_protected_member,
    moderation_denial,
    should_dm_user,
)


logger = logging.getLogger("sentinel.honeypot")

#: Refuse to let a server arm more than this many traps at once. A honeypot is
#: a deliberately small, deliberate surface; dozens of them is misconfiguration.
MAX_CHANNELS = 10

#: The staff embed shows the trapped message's text, capped at Discord's
#: 1024-character field limit.
CONTENT_LIMIT = 1024


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
# The table itself is created by bot.init_db() from the shared SCHEMA (this
# module cannot import bot.py: bot imports this one). These helpers go through
# the same store functions everything else uses, so an isolated test can point
# them at a temporary database by patching store.db_path.
def record_trap(
    guild_id: int,
    user_id: int,
    channel_id: int,
    message_id: Optional[int],
    content: str,
    action: str,
) -> int:
    """Store one trigger and return its row id."""
    return store.db_insert(
        """
        INSERT INTO honeypot_traps
            (guild_id, user_id, channel_id, message_id, content, action, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(guild_id),
            int(user_id),
            int(channel_id),
            int(message_id) if message_id else None,
            content or "",
            action,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def recent_traps(guild_id: int, limit: int = 5) -> list[sqlite3.Row]:
    """The newest triggers for a guild, newest first."""
    return store.db_fetchall(
        """
        SELECT * FROM honeypot_traps
        WHERE guild_id = ?
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (int(guild_id), int(limit)),
    )


def count_traps(guild_id: int) -> int:
    """How many members have ever tripped this server's honeypots."""
    row = store.db_fetchone(
        "SELECT COUNT(*) AS n FROM honeypot_traps WHERE guild_id = ?",
        (int(guild_id),),
    )
    return int(row["n"]) if row is not None else 0


def user_trap_count(guild_id: int, user_id: int) -> int:
    """How many times one member has tripped this server's honeypots."""
    row = store.db_fetchone(
        "SELECT COUNT(*) AS n FROM honeypot_traps WHERE guild_id = ? AND user_id = ?",
        (int(guild_id), int(user_id)),
    )
    return int(row["n"]) if row is not None else 0


# --------------------------------------------------------------------------- #
# Configuration editing (config.json via the cog's _save_config)
# --------------------------------------------------------------------------- #
def _update_guild_entry(config: dict, guild_id: int, **fields) -> None:
    """Merge fields into a guild's config entry, mirroring the primary guild.

    Per-guild entries are what the lookups in :mod:`settings` prefer, so every
    server — including the configured primary one — gets its settings written
    there. For the primary server the top-level installer shape is mirrored
    too, the same way ``/manage setup`` mirrors the role ids, so tools that
    only read the top level still see the truth.
    """
    guilds = config.get("guilds")
    if not isinstance(guilds, dict):
        guilds = {}
        config["guilds"] = guilds
    key = str(int(guild_id))
    entry = guilds.get(key)
    entry = dict(entry) if isinstance(entry, dict) else {}
    entry.update(fields)
    guilds[key] = entry

    try:
        is_primary = bool(config.get("server_id")) and int(
            config["server_id"]
        ) == int(guild_id)
    except (TypeError, ValueError):
        is_primary = False
    if is_primary:
        config.update(fields)


def set_honeypot_channels(config: dict, guild_id: int, channels: list[int]) -> None:
    """Save a guild's trap-channel list (drops duplicates, keeps order)."""
    unique: list[int] = []
    for value in channels:
        number = int(value)
        if number not in unique:
            unique.append(number)
    _update_guild_entry(config, guild_id, honeypot_channel_ids=unique)


def set_honeypot_action(config: dict, guild_id: int, action: str) -> None:
    """Save what happens to a member who posts in a trap channel."""
    normalized = str(action).strip().lower()
    if normalized not in HONEYPOT_ACTIONS:
        raise ValueError(f"Unknown honeypot action: {action!r}")
    _update_guild_entry(config, guild_id, honeypot_action=normalized)


def _format_duration(seconds: Optional[int]) -> str:
    """``bot.format_duration``, resolved lazily.

    bot.py imports this module, so a module-level import back into bot would
    be circular; by the time a trap fires, bot is fully loaded and this is a
    plain attribute lookup.
    """
    if not seconds:
        return "?"
    from bot import format_duration  # local import: bot imports this module

    return format_duration(int(seconds))


# --------------------------------------------------------------------------- #
# The trap itself
# --------------------------------------------------------------------------- #
def trap_embed(
    *,
    member: discord.abc.User,
    channel: object,
    action: str,
    content: str,
    error: Optional[str] = None,
    duration_seconds: Optional[int] = None,
    already_punished: bool = False,
    trigger_count: int = 1,
) -> discord.Embed:
    """The staff-channel embed posted when a honeypot catches someone."""
    titles = {
        "punish": "Honeypot triggered — member punished",
        "kick": "Honeypot triggered — member kicked",
        "ban": "Honeypot triggered — member banned",
    }
    e = discord.Embed(
        title=titles.get(action, "Honeypot triggered"),
        color=discord.Color.red(),
        timestamp=discord.utils.utcnow(),
    )
    e.add_field(
        name="User",
        value=f"{getattr(member, 'mention', member.id)} (`{member.id}`)",
        inline=True,
    )
    e.add_field(
        name="Channel",
        value=getattr(channel, "mention", str(getattr(channel, "id", "?"))),
        inline=True,
    )
    if action == "punish" and duration_seconds:
        e.add_field(
            name="Duration",
            value=_format_duration(duration_seconds),
            inline=True,
        )
    note = error
    if note is None and already_punished:
        note = "Already serving a punishment; timer left alone."
    if note is not None:
        e.add_field(name="Note", value=note[:1000], inline=False)
    if trigger_count > 1:
        e.add_field(name="Previous triggers", value=str(trigger_count - 1), inline=True)
    text = (content or "").strip()
    if text:
        e.add_field(name="Message", value=text[:CONTENT_LIMIT], inline=False)
    else:
        e.add_field(name="Message", value="*(no text — attachments or embeds only)*", inline=False)
    e.set_footer(text=f"User ID: {member.id} • Channel ID: {getattr(channel, 'id', '?')}")
    return e


def dm_embed(*, guild_name: str, action: str, duration_seconds: Optional[int]) -> discord.Embed:
    """The embed DMed to a member a honeypot caught (punish and kick only)."""
    if action == "kick":
        description = (
            f"You were removed from **{guild_name}** for posting in a channel "
            "that is off-limits. Moderators can let you back in."
        )
        title = f"Removed from {guild_name}"
    else:
        length = _format_duration(duration_seconds) if duration_seconds else "the default"
        description = (
            f"You were given the punish role in **{guild_name}** for {length} for "
            "posting in a channel that is off-limits."
        )
        title = f"You've been punished in {guild_name}"
    e = discord.Embed(
        title=title,
        description=description,
        color=discord.Color.red(),
        timestamp=discord.utils.utcnow(),
    )
    return e


async def process_message(bot, message: discord.Message) -> Optional[dict]:
    """Check one message against the honeypot configuration and act.

    Returns a summary dict when the trap fired (handy for tests and logging),
    or ``None`` when the message was ignored — DMs, webhooks, bots, servers
    with no trap armed, channels that are not traps, and anyone
    :func:`settings.is_protected_member` refuses to punish.
    """
    if message.guild is None:
        return None
    author = message.author
    if getattr(author, "bot", False) or message.webhook_id:
        return None

    guild = message.guild
    cfg = get_guild_config(bot.config, guild.id)
    if cfg is None:
        return None
    channels = get_honeypot_channels(bot.config, guild.id)
    if not channels or message.channel.id not in channels:
        return None

    # Protected members are exactly the people the punish flow refuses:
    # bots, admins, moderation-permission holders, the staff role, and anyone
    # at/above the bot's top role. Staff must be able to inspect the trap.
    try:
        protection = is_protected_member(author, bot.config, guild=guild)
    except AttributeError:
        # Not a full member object (author left the guild, partial payload):
        # leave it alone rather than guess.
        return None
    if protection is not None:
        return None

    action = get_honeypot_action(bot.config, guild.id)
    content = (message.content or "").strip()
    channel = message.channel
    error: Optional[str] = None
    duration_seconds: Optional[int] = None
    already_punished = False

    # Clear the bait out of the trap first: whatever happens below, the
    # channel ends up empty. Never raises.
    deleted = False
    if not getattr(message, "deleted", False):
        try:
            await message.delete()
            deleted = True
        except discord.HTTPException as exc:
            logger.warning("Could not delete honeypot message: %s", exc)

    if action == "punish":
        try:
            minutes = int(bot.config.get("default_duration_minutes", 30) or 30)
        except (TypeError, ValueError):
            minutes = 30
        duration_seconds = max(1, minutes) * 60
        if not cfg.get("punish_role_id") or not cfg.get("post_role_id"):
            error = (
                "No punish/post-punish roles configured — run /manage setup, "
                "or switch the honeypot action with /manage honeypot action."
            )
        else:
            active = store.db_fetchone(
                """
                SELECT id FROM punishments
                WHERE guild_id = ? AND user_id = ? AND stage = 'punished'
                LIMIT 1
                """,
                (guild.id, author.id),
            )
            if active is not None:
                # Re-punishing would duplicate the row and restart the timer;
                # the member is already being held, so just log the trigger.
                already_punished = True
            else:
                result = await bot.start_punishment(
                    guild,
                    author,
                    guild.me,
                    duration_seconds,
                    "Honeypot trap",
                    cfg,
                )
                if result is None:
                    error = "The configured punish/post-punish roles could not be applied."
                elif isinstance(result, dict) and result.get("error"):
                    error = str(result["error"])
    elif action == "kick":
        try:
            await author.kick(reason="Sentinel honeypot")
        except discord.Forbidden:
            error = "I lack permission to kick that member."
        except discord.HTTPException as exc:
            error = f"Discord error: {exc}"
    elif action == "ban":
        try:
            await author.ban(reason="Sentinel honeypot")
        except discord.Forbidden:
            error = "I lack permission to ban that member."
        except discord.HTTPException as exc:
            error = f"Discord error: {exc}"

    trap_id = record_trap(
        guild.id, author.id, channel.id, message.id, content, action
    )
    trigger_count = 1 + user_trap_count(guild.id, author.id)

    embed = trap_embed(
        member=author,
        channel=channel,
        action=action,
        content=content,
        error=error,
        duration_seconds=duration_seconds,
        already_punished=already_punished,
        trigger_count=trigger_count,
    )
    await bot._send_staff_embed(guild, embed)

    # A banned member will never see the server again; punish and kick are
    # reversible, so honour the server's dm_user preference for those.
    dm_sent = False
    if action in ("punish", "kick") and error is None and should_dm_user(cfg, guild.id):
        dm_sent = await bot._dm_embed(
            author,
            dm_embed(
                guild_name=guild.name,
                action=action,
                duration_seconds=duration_seconds,
            ),
        )

    return {
        "guild_id": guild.id,
        "user_id": author.id,
        "channel_id": channel.id,
        "message_id": message.id,
        "action": action,
        "error": error,
        "already_punished": already_punished,
        "deleted": deleted,
        "trap_id": trap_id,
        "trigger_count": trigger_count,
        "duration_seconds": duration_seconds,
        "dm_sent": dm_sent,
    }


# --------------------------------------------------------------------------- #
# Slash commands
# --------------------------------------------------------------------------- #
async def _reply(interaction: discord.Interaction, content: str) -> None:
    """Answer an interaction ephemerally, whether or not it was deferred."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True)
        else:
            await interaction.response.send_message(content, ephemeral=True)
    except (discord.InteractionResponded, discord.HTTPException) as exc:
        logger.warning("Failed to answer honeypot command: %s", exc)


def _action_label(action: str) -> str:
    return {
        "punish": "punish role for the default duration",
        "kick": "kick from the server",
        "ban": "ban from the server",
    }.get(action, action)


class HoneypotMixin:
    """``/manage honeypot`` — arm, disarm and watch the trap channels.

    Mixed into the cog that owns the shared ``/manage`` group (see
    :class:`applications.ApplicationsMixin` for why the group is a class
    attribute). Expects the host cog to provide ``self.bot`` and
    ``self._save_config``, which ``bot.SentinelCog`` already does for the
    applications mixin.
    """

    honeypot_group = app_commands.Group(
        name="honeypot",
        parent=manage_group,
        description="Trap channels that act on whoever posts in them.",
        guild_only=True,
    )

    # ---- The trap's own message listener ------------------------------- #
    @commands.Cog.listener("on_message")
    async def on_message(self, message: discord.Message) -> None:
        await process_message(self.bot, message)

    # ---- Arm / disarm --------------------------------------------------- #
    @honeypot_group.command(
        name="add",
        description="Turn a channel into a honeypot trap. (administrators)",
    )
    @app_commands.describe(channel="The channel members must not post in.")
    async def honeypot_add(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        if not is_administrator(interaction):
            await _reply(
                interaction,
                "Only server administrators can change the honeypot.",
            )
            return
        guild_id = interaction.guild_id
        channels = get_honeypot_channels(self.bot.config, guild_id)
        if channel.id in channels:
            await _reply(
                interaction,
                f"{channel.mention} is already a honeypot. "
                f"{len(channels)} channel(s) are armed.",
            )
            return
        if len(channels) >= MAX_CHANNELS:
            await _reply(
                interaction,
                f"A server can arm at most {MAX_CHANNELS} honeypot channels. "
                "Remove one with `/manage honeypot remove` first.",
            )
            return
        set_honeypot_channels(self.bot.config, guild_id, channels + [channel.id])
        self._save_config(self.bot.config)

        action = get_honeypot_action(self.bot.config, guild_id)
        lines = [
            f"{channel.mention} is now a honeypot. Anyone who posts there "
            f"gets: {_action_label(action)}.",
            f"{len(channels) + 1} channel(s) armed.",
        ]
        cfg = get_guild_config(self.bot.config, guild_id)
        if (
            action == "punish"
            and cfg is not None
            and not (cfg.get("punish_role_id") and cfg.get("post_role_id"))
        ):
            lines.append(
                "Heads up: the punish/post-punish roles are not configured yet "
                "(see `/manage setup`), so traps only log triggers until they are."
            )
        await _reply(interaction, "\n".join(lines))

    @honeypot_group.command(
        name="remove",
        description="Stop treating a channel as a honeypot. (administrators)",
    )
    @app_commands.describe(channel="The channel to disarm.")
    async def honeypot_remove(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        if not is_administrator(interaction):
            await _reply(
                interaction,
                "Only server administrators can change the honeypot.",
            )
            return
        guild_id = interaction.guild_id
        channels = get_honeypot_channels(self.bot.config, guild_id)
        if channel.id not in channels:
            await _reply(interaction, f"{channel.mention} is not a honeypot.")
            return
        channels.remove(channel.id)
        set_honeypot_channels(self.bot.config, guild_id, channels)
        self._save_config(self.bot.config)
        if channels:
            await _reply(
                interaction,
                f"{channel.mention} is no longer a honeypot. "
                f"{len(channels)} channel(s) remain armed.",
            )
        else:
            await _reply(
                interaction,
                f"{channel.mention} is no longer a honeypot. No channels are armed.",
            )

    @honeypot_group.command(
        name="clear",
        description="Remove every honeypot channel in this server. (administrators)",
    )
    async def honeypot_clear(self, interaction: discord.Interaction) -> None:
        if not is_administrator(interaction):
            await _reply(
                interaction,
                "Only server administrators can change the honeypot.",
            )
            return
        guild_id = interaction.guild_id
        channels = get_honeypot_channels(self.bot.config, guild_id)
        set_honeypot_channels(self.bot.config, guild_id, [])
        self._save_config(self.bot.config)
        await _reply(
            interaction,
            f"Disarmed {len(channels)} honeypot channel(s). Nothing is trapped now.",
        )

    @honeypot_group.command(
        name="action",
        description="Choose what happens to members who post in a honeypot. (administrators)",
    )
    @app_commands.describe(
        action="What a honeypot does to whoever posts in it. Omit to show the current value.",
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(
                name="Punish role (reversible, recommended)",
                value="punish",
            ),
            app_commands.Choice(name="Kick from the server", value="kick"),
            app_commands.Choice(name="Ban from the server", value="ban"),
        ]
    )
    async def honeypot_action(
        self,
        interaction: discord.Interaction,
        action: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        if not is_administrator(interaction):
            await _reply(
                interaction,
                "Only server administrators can change the honeypot.",
            )
            return
        guild_id = interaction.guild_id
        current = get_honeypot_action(self.bot.config, guild_id)
        if action is None:
            channels = get_honeypot_channels(self.bot.config, guild_id)
            await _reply(
                interaction,
                "Honeypot action: "
                f"{_action_label(current)}. "
                f"{len(channels)} channel(s) armed.",
            )
            return
        set_honeypot_action(self.bot.config, guild_id, action.value)
        self._save_config(self.bot.config)
        if action.value == "punish":
            cfg = get_guild_config(self.bot.config, guild_id)
            if cfg is not None and not (
                cfg.get("punish_role_id") and cfg.get("post_role_id")
            ):
                await _reply(
                    interaction,
                    "Saved. Heads up: the punish/post-punish roles are not "
                    "configured yet (see `/manage setup`), so traps only log "
                    "triggers until they are.",
                )
                return
        await _reply(
            interaction, f"Saved. Honeypots now {_action_label(action.value)}."
        )

    # ---- Watch ----------------------------------------------------------- #
    @honeypot_group.command(
        name="list",
        description="Show honeypot channels and recent triggers. (staff)",
    )
    async def honeypot_list(self, interaction: discord.Interaction) -> None:
        denial = moderation_denial(
            interaction.user,
            self.bot.config,
            guild=getattr(interaction, "guild", None),
            guild_id=getattr(interaction, "guild_id", None),
        )
        if denial is not None:
            await _reply(interaction, denial)
            return
        guild_id = interaction.guild_id
        guild = interaction.guild
        channels = get_honeypot_channels(self.bot.config, guild_id)
        action = get_honeypot_action(self.bot.config, guild_id)
        lines = [
            f"Honeypot action: {_action_label(action)}.",
        ]
        if not channels:
            lines.append(
                "No channels are armed. An administrator can arm one with "
                "`/manage honeypot add channel:#channel`."
            )
        else:
            mentions = []
            for channel_id in channels:
                channel = guild.get_channel(channel_id) if guild else None
                mentions.append(channel.mention if channel else f"<#{channel_id}>")
            lines.append("Armed channels: " + ", ".join(mentions))
        traps = recent_traps(guild_id, limit=5)
        total = count_traps(guild_id)
        lines.append(f"Triggered {total} time(s) since the feature was added.")
        if traps:
            lines.append("Most recent:")
            for trap in traps:
                user = f"<@{trap['user_id']}>"
                try:
                    moment = datetime.fromisoformat(trap["created_at"])
                    when = f"<t:{int(moment.timestamp())}:R>"
                except (TypeError, ValueError):
                    when = str(trap["created_at"])
                lines.append(
                    f"• {user} in <#{trap['channel_id']}> — {_action_label(trap['action'])}"
                    f" ({when})"
                )
        await _reply(interaction, "\n".join(lines))
