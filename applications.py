"""Applications for Sentinel: a form members fill in, a review staff decide on.

An administrator creates a form with ``/manage applications form`` (or the
dashboard) and publishes an *application panel* with ``/manage applications
panel``. Members press **Apply** — or run ``/apply`` — answer a short modal,
and the submission is posted to the form's review channel with **Approve** and
**Deny** buttons.

Staff can work from the queue commands or directly by id. ``review`` opens one
ephemeral queue; picking a submission shows its answers and the same
Approve/Deny buttons as the staff-channel card. ``view`` opens one submission
directly, and ``accept`` approves it without requiring a button. ``decide`` can
approve or deny by id. The applicant is DM'd the outcome and, when the form
names one, gets its accept role.

Normal members can *submit* an application and nothing else. There is no
command, button or panel that lets them list, read, edit or withdraw somebody
else's — or even their own — submission: every read path (``/manage
applications form``, ``review``, ``view``, ``accept``, ``decide``, the
dashboard pages) sits behind the staff check in :func:`is_application_staff`,
and the dashboard itself requires the dashboard token. `/apply` is the only
member-facing command, and all it does is open the modal.

Forms are stored per guild in ``config["applications"]``::

    "applications": {
        "123456789012345678": {
            "forms": [
                {
                    "form_id": "ab12cd34",
                    "name": "Staff application",
                    "description": "Apply to join the staff team.",
                    "review_channel_id": 444,
                    "panel_channel_id": 222,
                    "panel_message_id": 333,
                    "panel_title": null,
                    "panel_description": null,
                    "accept_role_id": 555,
                    "remove_role_id": null,
                    "allow_multiple": false,
                    "questions": [
                        {"label": "Why do you want to join?",
                         "style": "paragraph", "required": true,
                         "placeholder": null}
                    ]
                }
            ]
        }
    }

Submissions live in SQLite (``applications``) with the questions and answers
copied in, so editing a form later never rewrites what somebody was asked.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Callable, Optional

import discord
from discord import app_commands
from discord.ext import commands

import store
from command_tree import is_administrator, manage_group
from settings import (
    get_staff_channel_id,
    get_staff_role_id,
    save_config,
    should_dm_user,
)

logger = logging.getLogger("sentinel.applications")

APPLICATIONS_KEY = "applications"

#: A modal holds at most five inputs, and its title at most 45 characters.
#: The review buttons are the first row of the review message.
MAX_QUESTIONS = 5
MAX_ANSWER_LENGTH = 1000
MAX_QUESTION_LABEL_LENGTH = 45
MAX_PLACEHOLDER_LENGTH = 100
MAX_FORM_NAME_LENGTH = 80
MAX_FORMS_PER_GUILD = 25
MAX_PANEL_TITLE_LENGTH = 256
MAX_PANEL_DESCRIPTION_LENGTH = 4096
MAX_DECISION_NOTE_LENGTH = 400
MAX_LIST_ROWS = 25

STYLE_SHORT = "short"
STYLE_PARAGRAPH = "paragraph"
STYLES = (STYLE_SHORT, STYLE_PARAGRAPH)

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_DENIED = "denied"
STATUSES = (STATUS_PENDING, STATUS_APPROVED, STATUS_DENIED)

DECISION_APPROVE = "approve"
DECISION_DENY = "deny"
DECISIONS = (DECISION_APPROVE, DECISION_DENY)

DEFAULT_QUESTION = "Why do you want to join?"
DEFAULT_PANEL_TITLE = "Applications"
DEFAULT_PANEL_DESCRIPTION = "Press the button below to fill in the application form."

# --------------------------------------------------------------------------- #
# Component routing
# --------------------------------------------------------------------------- #
CUSTOM_ID_PREFIX = "sentinel:ap:"

ACTION_APPLY = "apply"        # sentinel:ap:apply:<form_id>        (panel button)
ACTION_MODAL = "modal"        # sentinel:ap:modal:<form_id>        (modal submit)
ACTION_CHOOSE = "choose"      # sentinel:ap:choose                 (form picker)
ACTION_DECIDE = "decide"      # sentinel:ap:decide:<id>:<approve|deny>  (button)
ACTION_DECIDED = "decided"    # sentinel:ap:decided:<id>:<approve|deny>  (modal submit)
ACTION_REVIEW = "review"      # sentinel:ap:review                   (/manage applications review select)

#: How many submissions the staff review queue offers (Discord allows 25).
REVIEW_LIMIT = 25


class ApplicationError(Exception):
    """An application action that cannot proceed, with a member-facing reason."""


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def new_form_id() -> str:
    """Return a short unique id for an application form."""
    return secrets.token_hex(4)


def _as_int(value: object) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _truthy(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: Optional[datetime]) -> Optional[str]:
    return moment.isoformat() if moment is not None else None


def _format_timestamp(value: object) -> str:
    if not value:
        return "—"
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return f"<t:{int(moment.timestamp())}:R>"


def actor_id(actor: object) -> Optional[int]:
    return _as_int(getattr(actor, "id", None))


def actor_name(actor: object) -> str:
    for attribute in ("display_name", "global_name", "name"):
        value = getattr(actor, attribute, None)
        if isinstance(value, str) and value.strip():
            return value
    return f"<@{actor_id(actor)}>" if actor_id(actor) else "unknown"


def actor_mention(actor: object) -> str:
    mention = getattr(actor, "mention", None)
    if isinstance(mention, str) and mention:
        return mention
    return f"<@{actor_id(actor)}>" if actor_id(actor) else actor_name(actor)


def question_custom_id(index: int) -> str:
    """The custom_id of the ``index``-th question in a form modal.

    Numbered rather than slugged from the label: labels are free text, get
    edited, and two of them may be identical, while the answer order is what
    the modal submit payload preserves.
    """
    return f"q{index}"


def custom_id(action: str, *parts: object) -> str:
    suffix = "".join(f":{part}" for part in parts if part is not None)
    return f"{CUSTOM_ID_PREFIX}{action}{suffix}"


def parse_custom_id(value: object) -> tuple[Optional[str], list[str]]:
    """Split a custom_id into ``(action, parts)``, or ``(None, [])``."""
    if not isinstance(value, str) or not value.startswith(CUSTOM_ID_PREFIX):
        return None, []
    remainder = value[len(CUSTOM_ID_PREFIX):]
    action, _, rest = remainder.partition(":")
    parts = [part for part in rest.split(":") if part] if rest else []
    return action or None, parts


def normalize_style(value: object) -> str:
    """Text-input style for a question (``short`` or ``paragraph``)."""
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned in STYLES:
            return cleaned
    return STYLE_PARAGRAPH


def normalize_question(raw: object, index: int = 0) -> Optional[dict]:
    """A stored question in its canonical shape (``None`` if unusable)."""
    if isinstance(raw, str):
        raw = {"label": raw}
    if not isinstance(raw, dict):
        return None
    label = str(raw.get("label") or "").strip()
    if not label:
        return None
    placeholder = raw.get("placeholder")
    placeholder = str(placeholder).strip() if placeholder else None
    return {
        "label": label[:MAX_QUESTION_LABEL_LENGTH],
        "style": normalize_style(raw.get("style")),
        "required": _truthy(raw.get("required"), True),
        "placeholder": placeholder[:MAX_PLACEHOLDER_LENGTH] if placeholder else None,
    }


def normalize_questions(raw: object) -> list[dict]:
    """Up to :const:`MAX_QUESTIONS` usable questions, keeping their order."""
    if not isinstance(raw, (list, tuple)):
        return []
    questions = []
    for index, item in enumerate(raw):
        question = normalize_question(item, index)
        if question is not None:
            questions.append(question)
        if len(questions) >= MAX_QUESTIONS:
            break
    return questions


def normalize_form(raw: object) -> Optional[dict]:
    """A stored form in its canonical shape (``None`` if unusable)."""
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()
    form_id = str(raw.get("form_id") or "").strip()
    if not name or not form_id:
        return None
    return {
        "form_id": form_id,
        "name": name[:MAX_FORM_NAME_LENGTH],
        "description": (str(raw.get("description")).strip() or None)
        if raw.get("description")
        else None,
        "review_channel_id": _as_int(raw.get("review_channel_id")),
        "panel_channel_id": _as_int(raw.get("panel_channel_id")),
        "panel_message_id": _as_int(raw.get("panel_message_id")),
        "panel_title": (str(raw.get("panel_title")).strip() or None)
        if raw.get("panel_title")
        else None,
        "panel_description": (str(raw.get("panel_description")).strip() or None)
        if raw.get("panel_description")
        else None,
        "accept_role_id": _as_int(raw.get("accept_role_id")),
        "remove_role_id": _as_int(raw.get("remove_role_id")),
        "allow_multiple": _truthy(raw.get("allow_multiple"), False),
        "questions": normalize_questions(raw.get("questions")),
    }


# --------------------------------------------------------------------------- #
# Reading and writing the per-guild configuration
# --------------------------------------------------------------------------- #
def _guild_settings(config: dict, guild_id: int) -> dict:
    stored = config.get(APPLICATIONS_KEY, {})
    if isinstance(stored, dict):
        entry = stored.get(str(int(guild_id)))
        if isinstance(entry, dict):
            return entry
    return {}


def get_guild_forms(config: dict, guild_id: int) -> list[dict]:
    """Every usable form for a guild, in the order they were added."""
    entry = _guild_settings(config, guild_id)
    return [
        form
        for form in (normalize_form(raw) for raw in entry.get("forms", []))
        if form is not None
    ]


def write_guild_forms(config: dict, guild_id: int, forms: list[dict]) -> None:
    """Persist a guild's forms and write the config to disk."""
    stored = config.get(APPLICATIONS_KEY, {})
    updated = dict(stored) if isinstance(stored, dict) else {}
    entry = dict(_guild_settings(config, guild_id))
    entry["forms"] = [
        form
        for form in (normalize_form(raw) for raw in forms)
        if form is not None
    ]
    updated[str(int(guild_id))] = entry
    config[APPLICATIONS_KEY] = updated
    save_config(config)


def find_form(config: dict, guild_id: int, wanted: object) -> Optional[dict]:
    """Find a form by id, or by name (case-insensitive)."""
    if wanted is None:
        return None
    text = str(wanted).strip()
    if not text:
        return None
    forms = get_guild_forms(config, guild_id)
    for form in forms:
        if form["form_id"] == text:
            return form
    lowered = text.lower()
    for form in forms:
        if form["name"].lower() == lowered:
            return form
    return None


def upsert_form(config: dict, guild_id: int, form: dict) -> dict:
    """Add or replace one application form."""
    normalized = normalize_form(form)
    if normalized is None:
        raise ApplicationError("A form needs a name and an id.")
    if not normalized["questions"]:
        raise ApplicationError(
            "Add at least one question — a form with no questions has nothing "
            "to ask (see the dashboard's Applications page)."
        )
    forms = get_guild_forms(config, guild_id)
    for index, existing in enumerate(forms):
        if existing["form_id"] == normalized["form_id"]:
            forms[index] = {**existing, **normalized}
            break
    else:
        if len(forms) >= MAX_FORMS_PER_GUILD:
            raise ApplicationError(
                f"This server already has {MAX_FORMS_PER_GUILD} forms. "
                "Remove one before adding another."
            )
        forms.append(normalized)
    write_guild_forms(config, guild_id, forms)
    return normalized


def remove_form(config: dict, guild_id: int, wanted: object) -> dict:
    """Remove a form, returning the one that was removed."""
    form = find_form(config, guild_id, wanted)
    if form is None:
        raise ApplicationError(
            "No form matches that. Use `/manage applications form` to list them."
        )
    remaining = [
        existing
        for existing in get_guild_forms(config, guild_id)
        if existing["form_id"] != form["form_id"]
    ]
    write_guild_forms(config, guild_id, remaining)
    return form


def update_form(config: dict, guild_id: int, form_id: str, **fields: object) -> dict:
    """Patch one stored field of a form (panel location, panel text, ...)."""
    forms = get_guild_forms(config, guild_id)
    for index, form in enumerate(forms):
        if form["form_id"] != form_id:
            continue
        updated = {**form, **fields}
        normalized = normalize_form(updated)
        if normalized is None:
            raise ApplicationError("That form could not be updated.")
        forms[index] = normalized
        write_guild_forms(config, guild_id, forms)
        return normalized
    raise ApplicationError("That form no longer exists.")


# --------------------------------------------------------------------------- #
# Permissions
# --------------------------------------------------------------------------- #
def is_application_staff(
    member: object, config: dict, guild_id: int, form: Optional[dict] = None
) -> bool:
    """Whether this member may read and decide applications.

    Staff means: administrator, *Manage Server* / *Moderate Members*, the
    configured staff role, or the role a form names as its reviewer. It is
    re-checked on every button, command and dashboard route rather than
    trusting the command's visibility.
    """
    if member is None or getattr(member, "bot", False):
        return False
    perms = getattr(member, "guild_permissions", None)
    if perms is not None and (
        getattr(perms, "administrator", False)
        or getattr(perms, "manage_guild", False)
        or getattr(perms, "moderate_members", False)
    ):
        return True
    role_ids = {
        _as_int(getattr(role, "id", None))
        for role in (getattr(member, "roles", None) or [])
    }
    role_ids.discard(None)
    staff_role = get_staff_role_id(config, guild_id)
    if staff_role is not None and staff_role in role_ids:
        return True
    if form is not None:
        accept_role = _as_int(form.get("accept_role_id"))
        if accept_role is not None and accept_role in role_ids:
            # Holding the applicant role marks a reviewer too: it is the role
            # the team hands out, so those members are part of the team.
            return True
    return False


# --------------------------------------------------------------------------- #
# Submissions (SQLite)
# --------------------------------------------------------------------------- #
APPLICATION_COLUMNS = (
    "guild_id",
    "form_id",
    "form_name",
    "user_id",
    "answers",
    "status",
    "review_channel_id",
    "review_message_id",
    "submitted_at",
    "decided_at",
    "decided_by",
    "decision_note",
)

_PUBLIC_FIELDS = (
    "id",
    "guild_id",
    "form_id",
    "form_name",
    "user_id",
    "answers",
    "status",
    "review_channel_id",
    "review_message_id",
    "submitted_at",
    "decided_at",
    "decided_by",
    "decision_note",
)


def _fresh(application: dict) -> dict:
    """Re-read a submission before deciding it.

    Two reviewers can press Approve within a second of each other, and the
    buttons on an old review card keep working after a restart. Reading the row
    back first means the second decision sees ``approved`` and is refused
    instead of quietly overwriting the first one.
    """
    fresh = (
        get_application(application.get("id")) if isinstance(application, dict) else None
    )
    return fresh or application


def _row_to_application(row: object) -> Optional[dict]:
    if row is None:
        return None
    record = {key: row[key] for key in _PUBLIC_FIELDS if key in row.keys()}
    record["answers"] = decode_answers(record.get("answers"))
    return record


def encode_answers(questions: list[dict], values: dict[str, str]) -> str:
    """Store question/answer pairs in the order they were asked."""
    pairs = [
        {
            "question": question["label"],
            "answer": (values.get(question_custom_id(index)) or "").strip(),
        }
        for index, question in enumerate(questions)
    ]
    return json.dumps(pairs, ensure_ascii=False)


def decode_answers(raw: object) -> list[dict]:
    """Read the stored answers back, tolerating hand-edited rows."""
    if isinstance(raw, list):
        pairs = raw
    else:
        try:
            pairs = json.loads(str(raw or "[]"))
        except (TypeError, ValueError):
            return []
    if not isinstance(pairs, list):
        return []
    return [
        {
            "question": str(pair.get("question") or ""),
            "answer": str(pair.get("answer") or ""),
        }
        for pair in pairs
        if isinstance(pair, dict)
    ]


def insert_application(**fields: object) -> dict:
    """Create a submission row and return it."""
    record = {key: fields.get(key) for key in APPLICATION_COLUMNS}
    placeholders = ", ".join("?" for _ in APPLICATION_COLUMNS)
    record_id = store.db_insert(
        "INSERT INTO applications ("
        + ", ".join(APPLICATION_COLUMNS)
        + f") VALUES ({placeholders})",
        tuple(record[key] for key in APPLICATION_COLUMNS),
    )
    application = get_application(record_id)
    if application is None:  # pragma: no cover - the row was just written
        raise ApplicationError("Could not store the application.")
    return application


def get_application(application_id: object) -> Optional[dict]:
    """One submission by its database id."""
    numeric = _as_int(application_id)
    if numeric is None:
        return None
    return _row_to_application(
        store.db_fetchone("SELECT * FROM applications WHERE id = ?", (numeric,))
    )


def list_applications(
    guild_id: int,
    *,
    status: Optional[str] = None,
    form_id: Optional[str] = None,
    user_id: Optional[int] = None,
    limit: int = MAX_LIST_ROWS,
) -> list[dict]:
    """Submissions for a guild, newest first (staff-only callers)."""
    clauses = ["guild_id = ?"]
    params: list[object] = [int(guild_id)]
    if status in STATUSES:
        clauses.append("status = ?")
        params.append(status)
    if form_id:
        clauses.append("form_id = ?")
        params.append(str(form_id))
    if user_id is not None:
        clauses.append("user_id = ?")
        params.append(int(user_id))
    params.append(max(1, min(int(limit), 100)))
    rows = store.db_fetchall(
        "SELECT * FROM applications WHERE "
        + " AND ".join(clauses)
        + " ORDER BY id DESC LIMIT ?",
        tuple(params),
    )
    return [
        application
        for application in (_row_to_application(row) for row in rows)
        if application is not None
    ]


def pending_application_for_user(
    guild_id: int, form_id: str, user_id: int
) -> Optional[dict]:
    """A submission from this member still waiting for a decision."""
    row = store.db_fetchone(
        "SELECT * FROM applications WHERE guild_id = ? AND form_id = ? "
        "AND user_id = ? AND status = ? ORDER BY id DESC LIMIT 1",
        (int(guild_id), str(form_id), int(user_id), STATUS_PENDING),
    )
    return _row_to_application(row)


def update_application(application_id: object, **fields: object) -> Optional[dict]:
    """Update the given columns on one submission and return the fresh row."""
    numeric = _as_int(application_id)
    if numeric is None:
        return None
    updates = {
        key: value for key, value in fields.items() if key in APPLICATION_COLUMNS
    }
    if updates:
        assignments = ", ".join(f"{key} = ?" for key in updates)
        store.db_execute(
            f"UPDATE applications SET {assignments} WHERE id = ?",
            tuple(updates.values()) + (numeric,),
        )
    return get_application(numeric)


def count_pending(guild_id: int) -> int:
    row = store.db_fetchone(
        "SELECT COUNT(*) AS count FROM applications WHERE guild_id = ? AND status = ?",
        (int(guild_id), STATUS_PENDING),
    )
    return int(row["count"]) if row else 0


# --------------------------------------------------------------------------- #
# Panel, modal and review message
# --------------------------------------------------------------------------- #
def panel_embed(form: dict, guild_name: str) -> discord.Embed:
    """The embed members see on an application panel."""
    embed = discord.Embed(
        title=form.get("panel_title") or form["name"] or DEFAULT_PANEL_TITLE,
        description=form.get("panel_description")
        or form.get("description")
        or DEFAULT_PANEL_DESCRIPTION,
        color=discord.Color.from_rgb(88, 165, 255),
    )
    questions = form.get("questions") or []
    if questions:
        embed.add_field(
            name="You'll be asked",
            value="\n".join(f"{index}. {question['label']}" for index, question in enumerate(questions, 1))[:1024],
            inline=False,
        )
    embed.set_footer(text=f"{guild_name} • one application at a time is enough")
    return embed


def panel_view(form: dict) -> discord.ui.View:
    """The panel's Apply button."""
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label=f"Apply — {form['name']}"[:80],
            emoji="📝",
            custom_id=custom_id(ACTION_APPLY, form["form_id"]),
            style=discord.ButtonStyle.primary,
        )
    )
    return view


def form_modal(form: dict) -> discord.ui.Modal:
    """Build the modal this form's questions describe.

    One :class:`discord.ui.TextInput` per question, numbered by position so the
    answers can be stored against the questions that were asked even if the
    form is edited between opening the modal and submitting it.
    """
    modal = discord.ui.Modal(
        title=str(form["name"])[:45],
        custom_id=custom_id(ACTION_MODAL, form["form_id"]),
    )
    for index, question in enumerate(form.get("questions") or []):
        modal.add_item(
            discord.ui.TextInput(
                label=question["label"][:MAX_QUESTION_LABEL_LENGTH],
                style=(
                    discord.TextStyle.paragraph
                    if question["style"] == STYLE_PARAGRAPH
                    else discord.TextStyle.short
                ),
                required=bool(question.get("required", True)),
                max_length=MAX_ANSWER_LENGTH,
                placeholder=question.get("placeholder") or None,
                custom_id=question_custom_id(index),
            )
        )
    return modal


def form_picker_view(forms: list[dict]) -> discord.ui.View:
    """A select of forms, for servers that run more than one."""
    view = discord.ui.View(timeout=180)
    options = [
        discord.SelectOption(label=form["name"][:100], value=form["form_id"])
        for form in forms[:25]
    ]
    select = discord.ui.Select(
        placeholder="Choose an application form",
        options=options,
        custom_id=custom_id(ACTION_CHOOSE),
    )
    view.add_item(select)
    return view


def review_embed(guild: discord.Guild, application: dict) -> discord.Embed:
    """The staff-facing submission card."""
    status = str(application.get("status") or STATUS_PENDING)
    color = {
        STATUS_PENDING: discord.Color.from_rgb(240, 166, 60),
        STATUS_APPROVED: discord.Color.from_rgb(88, 190, 120),
        STATUS_DENIED: discord.Color.from_rgb(200, 120, 120),
    }.get(status, discord.Color.from_rgb(240, 166, 60))
    embed = discord.Embed(
        title=f"Application #{int(application.get('id') or 0):04d} — {application.get('form_name')}",
        description=f"Submitted by <@{application.get('user_id')}> · "
        f"{_format_timestamp(application.get('submitted_at'))}",
        color=color,
    )
    for index, pair in enumerate(application.get("answers") or [], 1):
        answer = pair.get("answer") or "*left blank*"
        embed.add_field(
            name=f"{index}. {pair.get('question') or 'Question'}"[:256],
            value=answer[:1024],
            inline=False,
        )
    embed.add_field(name="Status", value=status.capitalize(), inline=True)
    if application.get("decided_by"):
        embed.add_field(
            name="Decided by",
            value=f"<@{application['decided_by']}> · "
            f"{_format_timestamp(application.get('decided_at'))}",
            inline=True,
        )
    if application.get("decision_note"):
        embed.add_field(
            name="Note",
            value=str(application["decision_note"])[:1024],
            inline=False,
        )
    embed.set_footer(text=f"Guild {guild.id} · application id {application.get('id')}")
    return embed


def review_view(application: dict) -> discord.ui.View:
    """Approve / Deny buttons, dropped once the application is decided."""
    view = discord.ui.View(timeout=None)
    if str(application.get("status")) != STATUS_PENDING:
        return view
    view.add_item(
        discord.ui.Button(
            label="Approve",
            emoji="✅",
            custom_id=custom_id(ACTION_DECIDE, application["id"], DECISION_APPROVE),
            style=discord.ButtonStyle.success,
        )
    )
    view.add_item(
        discord.ui.Button(
            label="Deny",
            emoji="⛔",
            custom_id=custom_id(ACTION_DECIDE, application["id"], DECISION_DENY),
            style=discord.ButtonStyle.danger,
        )
    )
    return view


def review_queue_embed(guild: discord.Guild, submissions: list[dict]) -> discord.Embed:
    """The summary above the staff review queue's submission select."""
    counts: dict[str, int] = {}
    for submission in submissions:
        status = str(submission.get("status") or STATUS_PENDING)
        counts[status] = counts.get(status, 0) + 1
    embed = discord.Embed(
        title="Applications to review",
        description=(
            "Pick a submission below to read the answers and approve or deny it. "
            "The applicant is DMed the outcome."
        ),
        color=discord.Color.from_rgb(88, 165, 255),
    )
    for status in (STATUS_PENDING, STATUS_APPROVED, STATUS_DENIED):
        if counts.get(status):
            embed.add_field(name=status.capitalize(), value=str(counts[status]), inline=True)
    embed.set_footer(text=f"{guild.name} • {len(submissions)} shown")
    return embed


def review_queue_view(submissions: list[dict]) -> discord.ui.View:
    """A select of submissions for the staff review queue."""
    view = discord.ui.View(timeout=300)
    options = []
    for submission in submissions[:REVIEW_LIMIT]:
        label = f"#{submission['id']} · {submission.get('form_name') or 'application'}"
        detail = " · ".join(
            part
            for part in (
                str(submission.get("status") or STATUS_PENDING),
                f"<@{submission.get('user_id')}>",
                _format_timestamp(submission.get("submitted_at")),
            )
            if part
        )
        options.append(
            discord.SelectOption(label=label[:100], value=str(submission["id"]), description=detail[:100] or None)
        )
    view.add_item(
        discord.ui.Select(
            placeholder="Choose a submission",
            options=options,
            custom_id=custom_id(ACTION_REVIEW),
        )
    )
    return view


def decision_modal(application: dict, decision: str) -> discord.ui.Modal:
    """Ask for an optional note before the decision is recorded."""
    verb = "Approve" if decision == DECISION_APPROVE else "Deny"
    modal = discord.ui.Modal(
        title=f"{verb} application #{int(application.get('id') or 0):04d}"[:45],
        custom_id=custom_id(ACTION_DECIDED, application["id"], decision),
    )
    modal.add_item(
        discord.ui.TextInput(
            label="Note for the applicant (optional)",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=MAX_DECISION_NOTE_LENGTH,
            placeholder="Anything they should know before they see the decision.",
            custom_id="note",
        )
    )
    return modal


# --------------------------------------------------------------------------- #
# Publishing a panel
# --------------------------------------------------------------------------- #
async def publish_panel(
    bot: commands.Bot,
    guild: discord.Guild,
    form: dict,
    channel: discord.TextChannel,
    *,
    title: Optional[str] = None,
    description: Optional[str] = None,
) -> dict:
    """Post (or refresh) one form's panel and remember where it is."""
    if title is not None and len(title) > MAX_PANEL_TITLE_LENGTH:
        raise ApplicationError(
            f"Keep the panel title to {MAX_PANEL_TITLE_LENGTH} characters."
        )
    if description is not None and len(description) > MAX_PANEL_DESCRIPTION_LENGTH:
        raise ApplicationError(
            f"Keep the panel description to {MAX_PANEL_DESCRIPTION_LENGTH} characters."
        )

    fields: dict = {"panel_channel_id": channel.id}
    if title is not None:
        fields["panel_title"] = title.strip() or None
    if description is not None:
        fields["panel_description"] = description.strip() or None
    form = update_form(bot.config, guild.id, form["form_id"], **fields)

    embed = panel_embed(form, guild.name)
    view = panel_view(form)

    message = None
    if form.get("panel_message_id") and form.get("panel_channel_id"):
        old_channel = guild.get_channel(int(form["panel_channel_id"]))
        if old_channel is not None and hasattr(old_channel, "fetch_message"):
            try:
                message = await old_channel.fetch_message(int(form["panel_message_id"]))
            except discord.HTTPException:
                message = None

    if message is not None and message.channel.id == channel.id:
        await message.edit(embed=embed, view=view)
        form = update_form(bot.config, guild.id, form["form_id"], panel_message_id=message.id)
    else:
        sent = await channel.send(
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        form = update_form(
            bot.config, guild.id, form["form_id"], panel_message_id=sent.id
        )
        if message is not None and bot.user is not None and message.author.id == bot.user.id:
            try:
                await message.delete()
            except discord.HTTPException:
                logger.info("Could not delete the replaced application panel %s", message.id)
    return form


# --------------------------------------------------------------------------- #
# Submitting and deciding
# --------------------------------------------------------------------------- #
def _review_channel(
    bot: commands.Bot, guild: discord.Guild, form: dict
) -> Optional[discord.TextChannel]:
    channel_id = _as_int(form.get("review_channel_id")) or get_staff_channel_id(
        bot.config, guild.id
    )
    if not channel_id:
        return None
    channel = guild.get_channel(int(channel_id))
    return channel if isinstance(channel, discord.TextChannel) else None


async def submit_application(
    bot: commands.Bot,
    *,
    guild: discord.Guild,
    applicant: discord.Member,
    form: dict,
    values: dict[str, str],
) -> dict:
    """Store a submission, post it for staff, and confirm to the applicant.

    Raises :class:`ApplicationError` when the member already has this form
    pending (unless the form allows repeats) or the form has no review channel.
    """
    if not form.get("allow_multiple"):
        pending = pending_application_for_user(guild.id, form["form_id"], applicant.id)
        if pending is not None:
            raise ApplicationError(
                f"You already have **{form['name']}** waiting for a decision. "
                "Staff will get back to you."
            )

    review_channel = _review_channel(bot, guild, form)
    if review_channel is None:
        raise ApplicationError(
            "This application form has no review channel yet, so your answers "
            "would go nowhere. Please tell an administrator."
        )

    application = insert_application(
        guild_id=guild.id,
        form_id=form["form_id"],
        form_name=form["name"],
        user_id=applicant.id,
        answers=encode_answers(form.get("questions") or [], values),
        status=STATUS_PENDING,
        review_channel_id=review_channel.id,
        submitted_at=_stamp(utc_now()),
    )

    try:
        message = await review_channel.send(
            content=f"<@{applicant.id}> applied for **{form['name']}**.",
            embed=review_embed(guild, application),
            view=review_view(application),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        application = update_application(
            application["id"], review_message_id=message.id
        ) or application
    except discord.HTTPException as exc:
        # The answers are stored either way: staff can still decide it from the
        # dashboard, so a failed post must not lose the submission.
        logger.warning(
            "Could not post application %s for review: %s", application["id"], exc
        )

    if should_dm_user(bot.config, guild.id):
        await _dm(
            applicant,
            discord.Embed(
                title=f"Application received — {form['name']}",
                description=(
                    f"Thanks! Your answers went to the {guild.name} staff team. "
                    "You'll get a DM once they decide."
                ),
                color=discord.Color.from_rgb(88, 165, 255),
                timestamp=utc_now(),
            ),
        )
    return application


async def decide_application(
    bot: commands.Bot,
    guild: discord.Guild,
    application: dict,
    actor: object,
    decision: str,
    note: Optional[str] = None,
) -> dict:
    """Approve or deny a submission, DM the applicant and grant roles.

    Everything here happens once: a decided application can only be re-decided
    from the dashboard, and the role grant is best-effort — a missing role or an
    insufficient bot hierarchy is logged and reported, never silently ignored
    (the decision itself is still recorded).
    """
    application = _fresh(application)
    if decision not in DECISIONS:
        raise ApplicationError("Decide with `approve` or `deny`.")
    if str(application.get("status")) != STATUS_PENDING:
        raise ApplicationError(
            f"That application was already {application.get('status')}."
        )
    form = find_form(bot.config, guild.id, application.get("form_id"))
    cleaned_note = (note or "").strip()[:MAX_DECISION_NOTE_LENGTH] or None

    decided = update_application(
        application["id"],
        status=STATUS_APPROVED if decision == DECISION_APPROVE else STATUS_DENIED,
        decided_at=_stamp(utc_now()),
        decided_by=actor_id(actor),
        decision_note=cleaned_note,
    ) or application

    role_note = ""
    applicant = guild.get_member(int(decided["user_id"]))
    if applicant is None:
        try:
            applicant = await guild.fetch_member(int(decided["user_id"]))
        except discord.HTTPException:
            applicant = None

    if applicant is not None and form is not None:
        role_id = (
            _as_int(form.get("accept_role_id"))
            if decision == DECISION_APPROVE
            else _as_int(form.get("remove_role_id"))
        )
        if role_id is not None:
            role = guild.get_role(role_id)
            if role is None:
                role_note = f"\n(I couldn't find the role for this form — `{role_id}`.)"
            else:
                try:
                    if decision == DECISION_APPROVE:
                        await applicant.add_roles(role, reason=f"Application #{decided['id']} approved")
                    else:
                        await applicant.remove_roles(role, reason=f"Application #{decided['id']} denied")
                except discord.Forbidden:
                    role_note = f"\n(I couldn't change {role.mention} — check my role position.)"
                except discord.HTTPException as exc:
                    logger.info("Could not change roles for application %s: %s", decided["id"], exc)
                    role_note = "\n(I couldn't update their roles.)"

    # The review message loses its buttons and shows the outcome.
    if decided.get("review_message_id") and decided.get("review_channel_id"):
        channel = guild.get_channel(int(decided["review_channel_id"]))
        if channel is not None:
            try:
                message = await channel.fetch_message(int(decided["review_message_id"]))
                await message.edit(
                    embed=review_embed(guild, decided),
                    view=review_view(decided),
                )
            except discord.HTTPException as exc:
                logger.info("Could not update review message for %s: %s", decided["id"], exc)

    if applicant is not None and should_dm_user(bot.config, guild.id):
        approved = decision == DECISION_APPROVE
        await _dm(
            applicant,
            discord.Embed(
                title=(
                    f"Application {'approved' if approved else 'denied'} — "
                    f"{decided['form_name']}"
                ),
                description=(cleaned_note or (
                    "Welcome aboard! Staff will follow up with next steps."
                    if approved
                    else "Thanks for applying. You can apply again later."
                )),
                color=discord.Color.from_rgb(
                    88, 190, 120
                ) if approved else discord.Color.from_rgb(200, 120, 120),
                timestamp=utc_now(),
            ),
        )
    logger.info(
        "Application %s %s by %s%s",
        decided["id"], decided["status"], actor_name(actor), role_note,
    )
    return decided


async def _dm(member: discord.Member, embed: discord.Embed) -> bool:
    try:
        await member.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        return True
    except (discord.Forbidden, discord.HTTPException) as exc:
        logger.info("Could not DM %s: %s", member.id, exc)
        return False


# --------------------------------------------------------------------------- #
# Interaction routing
# --------------------------------------------------------------------------- #
def _modal_values(interaction: discord.Interaction) -> dict[str, str]:
    """Flatten a modal submit payload into ``{custom_id: value}``."""
    values: dict[str, str] = {}
    data = interaction.data or {}
    for row in data.get("components", []) or []:
        for component in row.get("components", []) or []:
            identifier = component.get("custom_id")
            if identifier:
                values[str(identifier)] = str(component.get("value") or "")
    return values


async def _respond(
    interaction: discord.Interaction, content: str, *, ephemeral: bool = True
) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(
                content,
                ephemeral=ephemeral,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await interaction.response.send_message(
                content,
                ephemeral=ephemeral,
                allowed_mentions=discord.AllowedMentions.none(),
            )
    except (discord.InteractionResponded, discord.HTTPException) as exc:
        logger.warning("Could not answer an application interaction: %s", exc)


async def _send_modal(interaction: discord.Interaction, modal: discord.ui.Modal) -> None:
    """Show a modal, or explain why it could not be shown.

    A modal can only answer an interaction that has not been answered yet, and
    it fails outright if the interaction is older than Discord's window — so
    the failure is reported to the member instead of surfacing as a button that
    silently does nothing.
    """
    try:
        await interaction.response.send_modal(modal)
    except (discord.InteractionResponded, discord.HTTPException) as exc:
        logger.warning("Could not open application modal %s: %s", modal.custom_id, exc)
        await _respond(interaction, "I couldn't open that form — give it another try.")


def _find_mixin(bot: commands.Bot) -> Optional["ApplicationsMixin"]:
    """The cog that owns ``/manage applications`` (mixed into the main cog)."""
    for cog in getattr(bot, "cogs", {}).values():
        if isinstance(cog, ApplicationsMixin):
            return cog
    return None


async def route_application_interaction(
    bot: commands.Bot, interaction: discord.Interaction
) -> bool:
    """Handle one of this module's buttons, selects or modals.

    Returns ``True`` when the interaction was ours. Buttons on review messages
    published months ago still resolve: the custom_id carries the application
    and the decision, and the staff check happens here.
    """
    action, parts = parse_custom_id((interaction.data or {}).get("custom_id"))
    if action is None:
        return False

    mixin = _find_mixin(bot)
    if mixin is None:  # pragma: no cover - the cog is always loaded
        logger.warning("Application interaction arrived with no mixin loaded.")
        return True
    try:
        await mixin.handle_application_component(interaction, action, parts)
    except ApplicationError as exc:
        await _respond(interaction, str(exc))
    except Exception:
        logger.exception("Unhandled error handling application component %s", action)
        await _respond(
            interaction, "Something went wrong handling that application action."
        )
    return True


# --------------------------------------------------------------------------- #
# The cog: /manage applications ... and the member-facing /apply
# --------------------------------------------------------------------------- #
class ApplicationsMixin:
    """``/manage applications`` — the staff half of the application system.

    Mixed into the cog that owns the shared ``/manage`` group, because a nested
    group is registered — and its callbacks bound — by the cog that owns the
    top-level command. ``/apply`` itself is registered by
    :class:`ApplicationsCog` (see below), because a top-level command belongs
    to a cog of its own.
    """

    def __init__(self, bot: commands.Bot, save_config_fn: Callable[[dict], None]) -> None:
        self.bot = bot
        self._save_config = save_config_fn

    applications_group = app_commands.Group(
        name="applications",
        parent=manage_group,
        description="Application forms, panels and decisions.",
    )

    # ---- Forms: list, create, edit and delete in one command ----------- #
    @applications_group.command(
        name="form",
        description="List, create, edit or delete an application form. (administrators)",
    )
    @app_commands.describe(
        name="The form's name. Omit it to list the forms instead.",
        review_channel="Staff channel where submissions are posted.",
        questions="Questions separated by ' | ' (up to 5), e.g. 'Why us? | short:Timezone'.",
        description="Optional text shown on the panel and the review card.",
        accept_role="Role to give an approved applicant.",
        remove_role="Role to take away from an approved applicant.",
        allow_multiple="Allow a new application while one is pending (default: no).",
        remove="Set to true to delete this form (old submissions stay readable).",
    )
    async def applications_form(
        self,
        interaction: discord.Interaction,
        name: Optional[str] = None,
        review_channel: Optional[discord.TextChannel] = None,
        questions: Optional[str] = None,
        description: Optional[str] = None,
        accept_role: Optional[discord.Role] = None,
        remove_role: Optional[discord.Role] = None,
        allow_multiple: Optional[bool] = None,
        remove: Optional[bool] = None,
    ) -> None:
        """Without ``name`` this is the old ``forms`` list command.

        Naming an existing form edits it and keeps every option that was left
        out, so ``/manage applications form name:"Staff" allow_multiple:true``
        does not wipe the questions or the review channel.
        """
        await self._defer(interaction)
        if not is_administrator(interaction):
            await self._respond(
                interaction, "Only server administrators can change application forms."
            )
            return
        guild = interaction.guild
        forms = get_guild_forms(self.bot.config, guild.id)

        if name is None:
            if not forms:
                await self._respond(
                    interaction,
                    "No application forms yet — create one with "
                    "`/manage applications form name:Staff application "
                    "review_channel:#staff-apply`.",
                )
                return
            lines = [f"**Application forms** ({len(forms)}) — edit one by naming it."]
            for form in forms:
                review = (
                    f"<#{form['review_channel_id']}>"
                    if form.get("review_channel_id")
                    else "the staff channel"
                )
                panel = (
                    f"<#{form['panel_channel_id']}>"
                    if form.get("panel_channel_id")
                    else "no panel yet"
                )
                lines.append(
                    f"- **{form['name']}** · reviews in {review} · panel: {panel}\n"
                    f"  asks: "
                    + ", ".join(question["label"] for question in form["questions"])
                )
            await self._respond(interaction, "\n".join(lines))
            return

        cleaned = (name or "").strip()
        if not cleaned:
            await self._respond(interaction, "Give the form a name.")
            return
        if len(cleaned) > MAX_FORM_NAME_LENGTH:
            await self._respond(
                interaction, f"Keep the name to {MAX_FORM_NAME_LENGTH} characters or fewer."
            )
            return
        existing = find_form(self.bot.config, guild.id, cleaned)

        if remove:
            if existing is None:
                await self._respond(interaction, f"No application form named **{cleaned}**.")
                return
            try:
                removed = remove_form(self.bot.config, guild.id, cleaned)
            except ApplicationError as exc:
                await self._respond(interaction, str(exc))
                return
            await self._respond(
                interaction,
                f"Deleted **{removed['name']}**. Submissions already received stay in the "
                "dashboard's history.",
            )
            return

        for option, value in (
            ("review channel", review_channel),
            ("accept role", accept_role),
            ("remove role", remove_role),
        ):
            if value is not None and getattr(value, "guild", guild).id != guild.id:
                await self._respond(interaction, f"Choose a {option} from this server.")
                return

        if existing is None:
            if review_channel is None:
                await self._respond(
                    interaction,
                    "New forms need a review channel: "
                    "`/manage applications form name:Staff application "
                    "review_channel:#staff-apply`.",
                )
                return
            parsed = parse_question_list(questions) if questions else []
            form = {
                "form_id": new_form_id(),
                "name": cleaned,
                "description": description,
                "review_channel_id": review_channel.id,
                "accept_role_id": accept_role.id if accept_role is not None else None,
                "remove_role_id": remove_role.id if remove_role is not None else None,
                "allow_multiple": bool(allow_multiple),
                "questions": parsed or [{"label": DEFAULT_QUESTION}],
            }
            try:
                stored = upsert_form(self.bot.config, guild.id, form)
            except ApplicationError as exc:
                await self._respond(interaction, str(exc))
                return
            await self._respond(
                interaction,
                f"Form **{stored['name']}** created with {len(stored['questions'])} "
                f"question(s) and reviews in {review_channel.mention}. Publish it with "
                "`/manage applications panel`.",
            )
            return

        fields: dict = {"name": cleaned}
        if review_channel is not None:
            fields["review_channel_id"] = review_channel.id
        if questions is not None:
            parsed = parse_question_list(questions)
            if not parsed:
                await self._respond(
                    interaction, "Give at least one question, separated by ` | `."
                )
                return
            fields["questions"] = parsed
        if description is not None:
            fields["description"] = description
        if accept_role is not None:
            fields["accept_role_id"] = accept_role.id
        if remove_role is not None:
            fields["remove_role_id"] = remove_role.id
        if allow_multiple is not None:
            fields["allow_multiple"] = bool(allow_multiple)
        try:
            stored = update_form(self.bot.config, guild.id, existing["form_id"], **fields)
        except ApplicationError as exc:
            await self._respond(interaction, str(exc))
            return
        changed = ", ".join(
            key for key in fields if key != "name"
        ) or "nothing else"
        await self._respond(
            interaction,
            f"Updated **{stored['name']}** ({changed}).",
        )

    # ---- Panel --------------------------------------------------------- #
    @applications_group.command(
        name="panel",
        description="Post or refresh an application panel in a channel. (administrators)",
    )
    @app_commands.describe(
        form="The form's name or id.",
        channel="Channel where members press Apply.",
        title="Optional panel title.",
        description="Optional panel text.",
    )
    async def applications_panel(
        self,
        interaction: discord.Interaction,
        form: str,
        channel: discord.TextChannel,
        title: Optional[str] = None,
        description: Optional[str] = None,
    ) -> None:
        await self._defer(interaction)
        if not is_administrator(interaction):
            await self._respond(interaction, "Only server administrators can publish panels.")
            return
        guild = interaction.guild
        record = find_form(self.bot.config, guild.id, form)
        if record is None:
            await self._respond(interaction, "No form matches that name or id.")
            return
        if channel.guild.id != guild.id:
            await self._respond(interaction, "Choose a channel from this server.")
            return
        try:
            published = await publish_panel(
                self.bot, guild, record, channel, title=title, description=description
            )
        except ApplicationError as exc:
            await self._respond(interaction, str(exc))
            return
        except discord.Forbidden:
            await self._respond(
                interaction,
                "I need *View Channel*, *Send Messages* and *Embed Links* in that channel.",
            )
            return
        await self._respond(
            interaction,
            f"Panel for **{published['name']}** published in <#{published['panel_channel_id']}>.",
        )

    # ---- Review: the queue, the answers and the decision --------------- #
    @applications_group.command(
        name="review",
        description="Review submissions: pick one to read the answers and approve or deny it. (staff)",
    )
    @app_commands.describe(
        status="Which submissions to offer (default: the pending ones).",
        form="Only submissions for this form.",
    )
    @app_commands.choices(
        status=[
            app_commands.Choice(name="Pending", value=STATUS_PENDING),
            app_commands.Choice(name="Approved", value=STATUS_APPROVED),
            app_commands.Choice(name="Denied", value=STATUS_DENIED),
            app_commands.Choice(name="All", value="all"),
        ]
    )
    async def applications_review(
        self,
        interaction: discord.Interaction,
        status: Optional[app_commands.Choice[str]] = None,
        form: Optional[str] = None,
    ) -> None:
        """One ephemeral message that is the review queue.

        Picking a submission replaces the summary with the applicant's answers
        and the same Approve/Deny buttons the review card in the staff channel
        carries, so there is one decision path no matter where staff start.
        """
        await self._defer(interaction)
        if not await self._require_application_staff(interaction):
            return
        wanted = status.value if status is not None else STATUS_PENDING
        record = find_form(self.bot.config, interaction.guild_id, form) if form else None
        submissions = list_applications(
            interaction.guild_id,
            status=None if wanted == "all" else wanted,
            form_id=record["form_id"] if record is not None else None,
            limit=REVIEW_LIMIT,
        )
        if not submissions:
            await self._respond(
                interaction,
                "No submissions match that filter. Members apply with `/apply` or a "
                "panel button.",
            )
            return
        await interaction.followup.send(
            embed=review_queue_embed(interaction.guild, submissions),
            view=review_queue_view(submissions),
            ephemeral=True,
        )

    @applications_group.command(
        name="view",
        description="View one application's answers and review controls. (staff)",
    )
    @app_commands.describe(
        application="Submission id shown in /manage applications review.",
    )
    async def applications_view(
        self, interaction: discord.Interaction, application: str
    ) -> None:
        """Show one submission's complete review card in a private reply."""
        await self._defer(interaction)
        record = get_application(application)
        if record is None or int(record["guild_id"]) != interaction.guild_id:
            if not is_application_staff(
                interaction.user, self.bot.config, interaction.guild_id
            ):
                await self._respond(interaction, "Only staff can view applications.")
            else:
                await self._respond(interaction, "No application matches that id.")
            return
        form = find_form(self.bot.config, interaction.guild_id, record.get("form_id"))
        if not is_application_staff(
            interaction.user, self.bot.config, interaction.guild_id, form
        ):
            await self._respond(interaction, "Only staff can view applications.")
            return
        await interaction.followup.send(
            embed=review_embed(interaction.guild, record),
            view=review_view(record),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @applications_group.command(
        name="accept",
        description="Approve one pending application directly by id. (staff)",
    )
    @app_commands.describe(
        application="Submission id shown in /manage applications review.",
        note="Optional note sent to the applicant and stored with the decision.",
    )
    async def applications_accept(
        self,
        interaction: discord.Interaction,
        application: str,
        note: Optional[str] = None,
    ) -> None:
        """Approve a pending submission directly from a slash command."""
        await self._defer(interaction)
        record = get_application(application)
        if record is None or int(record["guild_id"]) != interaction.guild_id:
            if not is_application_staff(
                interaction.user, self.bot.config, interaction.guild_id
            ):
                await self._respond(interaction, "Only staff can accept applications.")
            else:
                await self._respond(interaction, "No application matches that id.")
            return
        form = find_form(self.bot.config, interaction.guild_id, record.get("form_id"))
        if not is_application_staff(
            interaction.user, self.bot.config, interaction.guild_id, form
        ):
            await self._respond(interaction, "Only staff can accept applications.")
            return
        try:
            decided = await decide_application(
                self.bot,
                interaction.guild,
                record,
                interaction.user,
                DECISION_APPROVE,
                note,
            )
        except ApplicationError as exc:
            await self._respond(interaction, str(exc))
            return
        await self._respond(
            interaction,
            f"Application `#{decided['id']}` was approved.",
        )

    @applications_group.command(
        name="decide",
        description="Approve or deny one submission by id, without opening the queue. (staff)",
    )
    @app_commands.describe(
        application="The submission id shown in /manage applications review.",
        decision="Approve or deny it.",
        note="Optional note shown to the applicant and stored with the decision.",
    )
    @app_commands.choices(
        decision=[
            app_commands.Choice(name="Approve", value=DECISION_APPROVE),
            app_commands.Choice(name="Deny", value=DECISION_DENY),
        ]
    )
    async def applications_decide(
        self,
        interaction: discord.Interaction,
        application: str,
        decision: app_commands.Choice[str],
        note: Optional[str] = None,
    ) -> None:
        await self._defer(interaction)
        if not await self._require_application_staff(interaction):
            return
        record = get_application(application)
        if record is None or int(record["guild_id"]) != interaction.guild_id:
            await self._respond(interaction, "No application matches that id.")
            return
        try:
            decided = await decide_application(
                self.bot,
                interaction.guild,
                record,
                interaction.user,
                decision.value,
                note,
            )
        except ApplicationError as exc:
            await self._respond(interaction, str(exc))
            return
        await self._respond(
            interaction,
            f"Application `#{decided['id']}` is now **{decided['status']}**. "
            "The applicant has been told.",
        )


    # ---- Component handling -------------------------------------------- #
    async def handle_application_component(
        self,
        interaction: discord.Interaction,
        action: str,
        parts: list[str],
    ) -> None:
        """Dispatch one of this module's buttons, selects or modal submits.

        Named after the module so that it cannot collide with a handler of the
        same name on another mixin of the shared cog.
        """
        if interaction.guild is None:
            await _respond(interaction, "Applications only work inside a server.")
            return
        if action == ACTION_APPLY:
            await self._handle_apply(interaction, parts[0] if parts else None)
        elif action == ACTION_CHOOSE:
            await self._handle_choose(interaction)
        elif action == ACTION_MODAL:
            await self._handle_modal(interaction, parts[0] if parts else None)
        elif action == ACTION_DECIDE:
            await self._handle_decide_button(
                interaction,
                parts[0] if parts else None,
                parts[1] if len(parts) > 1 else None,
            )
        elif action == ACTION_DECIDED:
            await self._handle_decision_modal(
                interaction,
                parts[0] if parts else None,
                parts[1] if len(parts) > 1 else None,
            )
        elif action == ACTION_REVIEW:
            await self._handle_review_pick(
                interaction, parts[0] if parts else None
            )
        else:
            logger.info("Ignoring unknown application component action %r", action)

    async def _handle_apply(
        self, interaction: discord.Interaction, form_id: Optional[str]
    ) -> None:
        form = find_form(self.bot.config, interaction.guild_id, form_id)
        if form is None:
            raise ApplicationError(
                "That application form is no longer open — an administrator can "
                "republish the panel."
            )
        await _send_modal(interaction, form_modal(form))

    async def _handle_choose(self, interaction: discord.Interaction) -> None:
        """A form was picked from the /apply select: open its modal."""
        values = (interaction.data or {}).get("values") or []
        form_id = str(values[0]) if values else None
        form = find_form(self.bot.config, interaction.guild_id, form_id)
        if form is None:
            raise ApplicationError("That form is no longer available.")
        await _send_modal(interaction, form_modal(form))

    async def _handle_modal(
        self, interaction: discord.Interaction, form_id: Optional[str]
    ) -> None:
        form = find_form(self.bot.config, interaction.guild_id, form_id)
        if form is None:
            raise ApplicationError(
                "That application form is no longer open. Please ask staff to "
                "republish it."
            )
        values = _modal_values(interaction)
        missing = [
            question["label"]
            for index, question in enumerate(form["questions"])
            if question.get("required", True)
            and not (values.get(question_custom_id(index)) or "").strip()
        ]
        if missing:
            await _respond(
                interaction,
                "Please answer: " + ", ".join(f"**{label}**" for label in missing),
            )
            return
        # Acknowledge first: posting the review card and the DM happen after.
        await _respond(interaction, "Submitting your application…")
        try:
            application = await submit_application(
                self.bot,
                guild=interaction.guild,
                applicant=interaction.user,  # type: ignore[arg-type]
                form=form,
                values=values,
            )
        except ApplicationError as exc:
            await _respond(interaction, str(exc))
            return
        except discord.HTTPException:
            logger.exception("Discord refused the application review post.")
            await _respond(
                interaction,
                "Your answers were saved, but I couldn't post them to staff. "
                "Please tell a staff member.",
            )
            return
        await _respond(
            interaction,
            f"Thanks! Application `#{application['id']}` for **{form['name']}** "
            "is with the staff team. You'll get a DM when they decide.",
        )

    async def _handle_review_pick(
        self, interaction: discord.Interaction, application_id: Optional[str]
    ) -> None:
        """A submission was chosen from ``/manage applications review``.

        The queue message is *edited* into the review card: the same embed the
        staff channel shows, and the same Approve/Deny buttons, so both routes
        end in :func:`decide_application`.
        """
        record = get_application(application_id)
        if record is None or int(record["guild_id"]) != interaction.guild_id:
            await _respond(interaction, "That application no longer exists.")
            return
        if not is_application_staff(
            interaction.user, self.bot.config, interaction.guild_id
        ):
            await _respond(interaction, "Only staff can review applications.")
            return
        try:
            await interaction.response.edit_message(
                embed=review_embed(interaction.guild, record),
                view=review_view(record),
            )
        except (discord.InteractionResponded, discord.HTTPException) as exc:
            logger.info(
                "Could not show application %s in the review queue: %s",
                record["id"], exc,
            )
            await _respond(interaction, "I couldn't open that submission — try again.")

    async def _handle_decide_button(
        self,
        interaction: discord.Interaction,
        application_id: Optional[str],
        decision: Optional[str],
    ) -> None:
        record = get_application(application_id)
        if record is None or int(record["guild_id"]) != interaction.guild_id:
            await _respond(interaction, "That application no longer exists.")
            return
        form = find_form(self.bot.config, interaction.guild_id, record["form_id"])
        if not is_application_staff(
            interaction.user, self.bot.config, interaction.guild_id, form
        ):
            await _respond(interaction, "Only staff can decide applications.")
            return
        if decision not in DECISIONS:
            await _respond(interaction, "Decide with Approve or Deny.")
            return
        if str(record.get("status")) != STATUS_PENDING:
            await _respond(
                interaction, f"That application was already {record['status']}."
            )
            return
        await _send_modal(interaction, decision_modal(record, decision))

    async def _handle_decision_modal(
        self,
        interaction: discord.Interaction,
        application_id: Optional[str],
        decision: Optional[str],
    ) -> None:
        record = get_application(application_id)
        if record is None or int(record["guild_id"]) != interaction.guild_id:
            await _respond(interaction, "That application no longer exists.")
            return
        form = find_form(self.bot.config, interaction.guild_id, record["form_id"])
        if not is_application_staff(
            interaction.user, self.bot.config, interaction.guild_id, form
        ):
            await _respond(interaction, "Only staff can decide applications.")
            return
        note = (_modal_values(interaction).get("note") or "").strip() or None
        await _respond(interaction, "Recording the decision…")
        try:
            decided = await decide_application(
                self.bot,
                interaction.guild,
                record,
                interaction.user,
                decision or "",
                note,
            )
        except ApplicationError as exc:
            await _respond(interaction, str(exc))
            return
        await _respond(
            interaction,
            f"Application `#{decided['id']}` is now **{decided['status']}**.",
        )

    # ---- Shared helpers ------------------------------------------------- #
    async def _require_application_staff(
        self, interaction: discord.Interaction
    ) -> bool:
        """Refuse non-staff callers of the read/decide commands.

        Members can *submit* an application and nothing else; this is the check
        that keeps ``list`` and ``view`` (and therefore everyone else's
        answers) away from them.
        """
        if is_application_staff(
            interaction.user, self.bot.config, interaction.guild_id
        ):
            return True
        await _respond(interaction, "Only staff can view or decide applications.")
        return False

    @staticmethod
    async def _defer(interaction: discord.Interaction) -> None:
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
        except (discord.InteractionResponded, discord.HTTPException):
            pass

    @staticmethod
    async def _respond(interaction: discord.Interaction, content: str) -> None:
        await _respond(interaction, content)


class ApplicationsCog(commands.Cog):
    """The one member-facing command: ``/apply``.

    It is deliberately its own, top-level command (not a ``/manage`` child):
    members must be able to see it, while everything that can *read* an
    application lives under ``/manage applications``. It only opens the modal —
    there is no option, button or follow-up here that shows a submission back.
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="apply",
        description="Apply to the server's open applications (staff, events, and so on).",
    )
    @app_commands.guild_only()
    @app_commands.describe(form="Optional: which form, if the server runs several.")
    async def apply(
        self, interaction: discord.Interaction, form: Optional[str] = None
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await _respond(interaction, "Applications only work inside a server.")
            return
        mixin = _find_mixin(self.bot)
        if mixin is None:  # pragma: no cover - the cog is always loaded
            await _respond(interaction, "Applications are not available right now.")
            return
        forms = get_guild_forms(self.bot.config, guild.id)
        if not forms:
            await _respond(
                interaction, "This server has no open applications right now."
            )
            return
        if form:
            record = find_form(self.bot.config, guild.id, form)
            if record is None:
                await _respond(interaction, "No open application matches that name.")
                return
            await _send_modal(interaction, form_modal(record))
            return
        if len(forms) == 1:
            await _send_modal(interaction, form_modal(forms[0]))
            return
        await interaction.response.send_message(
            "Which application would you like to fill in?",
            view=form_picker_view(forms),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


def parse_question_list(text: Optional[str]) -> list[dict]:
    """Parse the ``a | short:b | c`` form used by the slash commands.

    A leading ``short:`` asks for a one-line box instead of the default
    paragraph, because the slash command has no room for a second option per
    question and most questions are one or the other.
    """
    if not text:
        return []
    questions = []
    for chunk in str(text).split("|"):
        label = chunk.strip()
        if not label:
            continue
        style = STYLE_PARAGRAPH
        lowered = label.lower()
        if lowered.startswith("short:"):
            style = STYLE_SHORT
            label = label[len("short:"):].strip()
        elif lowered.startswith("paragraph:"):
            label = label[len("paragraph:"):].strip()
        if not label:
            continue
        questions.append({"label": label, "style": style})
        if len(questions) >= MAX_QUESTIONS:
            break
    return questions


__all__ = [
    "ApplicationError",
    "ApplicationsCog",
    "review_queue_embed",
    "review_queue_view",
    "ApplicationsMixin",
    "DECISION_APPROVE",
    "DECISION_DENY",
    "STATUS_APPROVED",
    "STATUS_DENIED",
    "STATUS_PENDING",
    "decide_application",
    "find_form",
    "get_application",
    "get_guild_forms",
    "is_application_staff",
    "list_applications",
    "parse_question_list",
    "publish_panel",
    "remove_form",
    "route_application_interaction",
    "submit_application",
    "update_form",
    "upsert_form",
    "write_guild_forms",
]
