"""The shared slash-command tree for Sentinel.

Everything the bot can do hangs off one top-level group, ``/manage``::

    /manage punish      apply a timed punishment role
    /manage pardon      end one early
    /manage warn        record a warning (no role change)
    /manage warnings    list / clear a member's warnings
    /manage status      server config, active punishments, member history
    /manage setup       configure this server (administrators)
    /manage fixcommands clean up duplicated slash commands (administrators)
    /manage applications form    list / create / edit / delete a form (admins)
    /manage applications panel   publish an Apply panel (admins)
    /manage applications review  read submissions and approve or deny (staff)
    /manage applications view    view one submission's answers (staff)
    /manage applications accept  approve one submission directly (staff)
    /manage applications decide  approve or deny one by id (staff)
    /apply              fill in an application form (every member)

The group object lives in its own module because two places need it:

* :mod:`bot` owns the cog that Discord sees, so it owns registration of the
  group (only a command whose ``parent`` is ``None`` is registered by
  ``Cog``);
* a feature module can attach its own sub-group to it with
  ``parent=manage_group``, so its commands are defined next to the helpers they
  use instead of being moved into bot.py. :mod:`applications` does exactly
  that.

Sub-command callbacks are bound to whichever cog owns the group, so the
application commands are mixed into that same cog (see
:class:`applications.ApplicationsMixin`).

The application commands support both queue workflows and direct lookups:
``/manage applications review`` or ``view`` to read submissions, and ``accept``
or ``decide`` to record an outcome. Queue messages are ephemeral and their
selects and buttons call the same permission-checked handlers as the direct
commands.

``/apply`` is deliberately *not* a child of this group: it is the command a
normal member must be able to see, and all it does is open a modal. Reading or
deciding anything always goes through the group (and through a permission
re-check), which is what keeps "members can apply but not view or edit" true.

Discord only honours ``default_member_permissions`` on the *top-level*
command, and Sentinel leaves it unset (the group is visible to everyone in the
server). A role is not expressible as a permission, and hiding ``/manage``
behind *Moderate Members* would hide it from staff-role holders who lack that
permission — so each command decides for itself when it runs: moderation needs
the configured staff role (or Administrator, see
:func:`settings.moderation_denial`), ``setup`` and ``fixcommands`` need
Administrator (:func:`is_administrator`), and the application commands
re-check their own staff rules.
"""

from __future__ import annotations

import discord
from discord import app_commands

__all__ = ["MANAGE_GROUP_NAME", "is_administrator", "manage_group"]


MANAGE_GROUP_NAME = "manage"

manage_group = app_commands.Group(
    name=MANAGE_GROUP_NAME,
    description="Server management: moderation, setup and applications.",
    # Every command targets a specific server, so never offer them in DMs.
    guild_only=True,
)


def is_administrator(interaction: discord.Interaction) -> bool:
    """Whether the caller has *Administrator* in this server.

    Sub-commands share their group's ``default_member_permissions``, and a
    server can relax that per integration, so the administrative commands
    re-check here instead of trusting what the client was told to hide.
    """
    perms = getattr(interaction.user, "guild_permissions", None)
    if perms is None:
        return False
    return bool(getattr(perms, "administrator", False))
