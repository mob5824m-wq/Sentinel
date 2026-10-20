"""Per-guild configuration lookups, shared by every Sentinel module.

These helpers used to live in ``bot.py``. They moved here so the feature
modules (:mod:`applications`, ...) can resolve a guild's staff role,
notification channel and DM preference without importing ``bot`` — which
imports them, so that import would be circular.

They are pure functions over the config dict: nothing here touches Discord or
the database, which is also what makes them trivial to test. ``bot.py``
imports every name below, so ``bot.get_guild_config(...)`` and the callables
the dashboard is built with are unchanged.

Two config shapes have to keep working (see ``DEFAULT_CONFIG`` in bot.py):

* the new single-server shape, where the roles live at the top level and
  ``server_id`` names the guild;
* the older multi-server shape, where each guild has an entry in ``guilds``
  and top-level staff settings act as defaults.

:func:`get_guild_config` flattens both into one view; the other helpers build
on it so they cannot disagree about which guild is configured.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import discord

import paths


logger = logging.getLogger("sentinel.settings")


def config_path() -> Path:
    """Where config.json is read from (may be a read-only system location)."""
    return paths.config_path()


def save_config(cfg: dict) -> None:
    """Write config.json to the first writable location.

    Never writes into the application directory: on an installed build that
    is read-only (and world-readable, which would leak the token).
    """
    try:
        paths.write_config(cfg)
    except OSError as exc:
        logger.error("Could not save config to %s: %s", paths.config_write_path(), exc)
        raise


def get_guild_config(cfg: dict, guild_id: int) -> Optional[dict]:
    """Return effective settings for a guild, including legacy fallbacks.

    Punishment roles live in the per-guild ``guilds`` map for multi-server
    installs, or in the top-level fields for the configured primary guild.
    Staff channel and DM preferences support per-guild overrides while keeping
    older top-level values as defaults.
    """
    guild_id = int(guild_id)
    guilds = cfg.get("guilds", {})
    per_guild = guilds.get(str(guild_id)) if isinstance(guilds, dict) else None
    if not isinstance(per_guild, dict):
        per_guild = None

    try:
        is_primary = bool(cfg.get("server_id")) and int(cfg["server_id"]) == guild_id
    except (TypeError, ValueError):
        is_primary = False

    if not is_primary and per_guild is None:
        return None

    if is_primary:
        result = {
            "punish_role_id": cfg.get("punish_role_id"),
            "post_role_id": cfg.get("post_role_id"),
            "staff_role_id": cfg.get("staff_role_id"),
        }
    else:
        # Drop the legacy normal_role_id key from the in-memory view so
        # callers don't trip over it.
        result = {k: v for k, v in per_guild.items() if k != "normal_role_id"}

    # A guild entry wins over top-level defaults when that key was explicitly
    # saved (including an explicit null used to clear an optional setting).
    if per_guild is not None:
        for key in ("staff_role_id", "staff_channel_id", "dm_user"):
            if key in per_guild:
                result[key] = per_guild[key]

    result.setdefault("staff_role_id", cfg.get("staff_role_id"))
    result.setdefault(
        "staff_channel_id",
        cfg.get("staff_channel_id") or cfg.get("log_channel_id"),
    )
    result.setdefault("dm_user", cfg.get("dm_user", True))
    return result


def get_staff_role_id(cfg: dict, guild_id: Optional[int] = None) -> Optional[int]:
    """The staff role protected from punishment, optionally guild-specific."""
    guild_cfg = get_guild_config(cfg, guild_id) if guild_id is not None else None
    val = (
        guild_cfg.get("staff_role_id")
        if guild_cfg is not None
        else cfg.get("staff_role_id")
    )
    return int(val) if val else None


def get_staff_channel_id(cfg: dict, guild_id: Optional[int] = None) -> Optional[int]:
    """Resolve the staff/log channel with per-guild and legacy fallbacks."""
    guild_cfg = get_guild_config(cfg, guild_id) if guild_id is not None else None
    val = (
        guild_cfg.get("staff_channel_id")
        if guild_cfg is not None
        else cfg.get("staff_channel_id") or cfg.get("log_channel_id")
    )
    return int(val) if val else None


def should_dm_user(cfg: dict, guild_id: Optional[int] = None) -> bool:
    """Whether the bot should DM users, with an optional per-guild override."""
    guild_cfg = get_guild_config(cfg, guild_id) if guild_id is not None else None
    if guild_cfg is not None and "dm_user" in guild_cfg:
        return bool(guild_cfg["dm_user"])
    return bool(cfg.get("dm_user", True))


def moderation_denial(
    member: object,
    cfg: dict,
    *,
    guild: object = None,
    guild_id: Optional[int] = None,
) -> Optional[str]:
    """Why ``member`` may not use Sentinel's moderation commands, or ``None``.

    Moderation (punish, pardon, warn, warnings, status) is a *staff* job:
    holding the configured staff role — ``/manage setup staff_role:`` — is what
    qualifies. Discord's *Moderate Members* permission is deliberately not
    enough on its own, because a server can hand that permission to anyone,
    and those members should not be punishing people.

    Two deliberate exceptions:

    * **Administrators** always qualify. They choose the staff role in the
      first place, and an administrator can grant themselves the role in two
      clicks — refusing them adds friction, not safety.
    * A server that has **no staff role configured yet** keeps the historical
      permission rule (Moderate Members / Manage Server / Administrator), so
      moderation does not stop working on a first-run server. The refusal says
      how to switch to the staff-role rule.

    The return value is worded for the member who hit the wall, so callers can
    send it as-is.
    """
    perms = getattr(member, "guild_permissions", None)
    if perms is not None and getattr(perms, "administrator", False):
        return None

    resolved_id = guild_id if guild_id is not None else getattr(guild, "id", None)
    staff_role_id = get_staff_role_id(cfg, resolved_id)
    if staff_role_id is None:
        # Not configured: the old permission rule, so first-run servers keep
        # working. (Once a staff role is set, only staff and admins pass.)
        if perms is not None and (
            getattr(perms, "moderate_members", False)
            or getattr(perms, "manage_guild", False)
        ):
            return None
        return (
            "Only staff can use moderation commands. An administrator can set the "
            "staff role with `/manage setup staff_role:@Staff`."
        )

    roles = getattr(member, "roles", None) or []
    if any(getattr(role, "id", None) == staff_role_id for role in roles):
        return None
    role = None
    if guild is not None and hasattr(guild, "get_role"):
        role = guild.get_role(staff_role_id)
    mention = getattr(role, "mention", None) or f"<@&{staff_role_id}>"
    return f"Only members with the {mention} role can use moderation commands."


def is_protected_member(
    member: discord.Member, cfg: dict, *, guild: discord.Guild
) -> Optional[str]:
    """Return None if `member` can be punished, or a string reason if
    they cannot. Used to refuse `/manage punish` for admins, mods, and
    anyone holding the configured staff role.
    """
    if member.bot:
        return "Bots cannot be punished."
    if member.id == guild.me.id:
        return "I can't punish myself."
    # Administrator or any mod-like permission.
    perms = member.guild_permissions
    if perms.administrator:
        return "That user is a server administrator."
    if perms.moderate_members or perms.manage_guild or perms.kick_members or perms.ban_members:
        return "That user has moderation permissions and is protected."
    # Holding the configured staff role.
    staff_role_id = get_staff_role_id(cfg, guild.id)
    if staff_role_id is not None:
        staff_role = guild.get_role(staff_role_id)
        if staff_role and staff_role in member.roles:
            return f"That user has the {staff_role.mention} role and is protected."
    # Top-role hierarchy check.
    if member.top_role >= guild.me.top_role:
        return "That user has a role equal to or higher than mine."
    return None
