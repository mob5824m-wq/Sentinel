"""Tests for the application system (applications.py).

The permission model is what matters most here, because it is the one the
feature was specified with: a normal member can *submit* an application and
nothing else. They cannot list submissions, read their own or anyone else's
answers back, or decide anything — `/apply` only opens the modal.

The rest of the file covers what staff rely on: the modal is built from the
form's questions (capped at Discord's five-input limit), submissions are stored
with the questions that were asked, the review card carries working
Approve/Deny buttons, decisions are written once and DM the applicant, and the
approval role is granted only when the bot can actually grant it.

Run with either:
    python -m pytest tests/test_applications.py
    python tests/test_applications.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import discord  # noqa: E402

import applications  # noqa: E402
import bot as bot_module  # noqa: E402
import store  # noqa: E402
from applications import (  # noqa: E402
    ApplicationsCog,
    ApplicationsMixin,
    ApplicationError,
)


GUILD_ID = 123456789012345678
APPLICANT_ID = 111111111111111111
STAFF_ID = 222222222222222222
OUTSIDER_ID = 333333333333333333
ADMIN_ID = 121212121212121212
BOT_ID = 999999999999999999
STAFF_ROLE_ID = 444444444444444444
REVIEW_CHANNEL_ID = 555555555555555555
PANEL_CHANNEL_ID = 666666666666666666
ACCEPT_ROLE_ID = 777777777777777777


def _permissions(**flags) -> SimpleNamespace:
    names = ("administrator", "manage_guild", "moderate_members")
    return SimpleNamespace(**{name: bool(flags.get(name, False)) for name in names})


class _FakeRole:
    """Hashable enough for permission checks (roles are compared by id)."""

    def __init__(self, role_id: int, name: str = "role") -> None:
        self.id = role_id
        self.name = name
        self.mention = f"<@&{role_id}>"

    def __hash__(self) -> int:
        return hash(self.id)


class _FakeMember:
    def __init__(self, member_id: int, *, roles=(), **permissions) -> None:
        self.id = member_id
        self.bot = False
        self.guild_permissions = _permissions(**permissions)
        self.roles = [_FakeRole(role_id) for role_id in roles]
        self.mention = f"<@{member_id}>"
        self.display_name = f"member-{member_id}"
        self.dms: list[object] = []
        self.role_changes: list[tuple[str, int]] = []

    async def send(self, *, embed=None, allowed_mentions=None) -> None:
        self.dms.append(embed)

    async def add_roles(self, role, reason=None) -> None:
        self.role_changes.append(("add", role.id))

    async def remove_roles(self, role, reason=None) -> None:
        self.role_changes.append(("remove", role.id))


class _FakeMessage:
    def __init__(self, channel, message_id: int) -> None:
        self.id = message_id
        self.channel = channel
        self.author = SimpleNamespace(id=BOT_ID)
        self.edits: list[dict] = []
        self.deleted = False

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        return self

    async def delete(self) -> None:
        self.deleted = True


class _FakeTextChannel:
    def __init__(self, guild, channel_id: int, name: str = "staff") -> None:
        self.id = channel_id
        self.guild = guild
        self.name = name
        self.mention = f"<#{channel_id}>"
        self.sent: list[dict] = []
        self.messages: dict[int, _FakeMessage] = {}
        self._next_id = 700

    def permissions_for(self, _member):
        return SimpleNamespace(view_channel=True, send_messages=True, embed_links=True)

    async def send(self, *, content=None, embed=None, view=None, allowed_mentions=None):
        message = _FakeMessage(self, self._next_id)
        self._next_id += 1
        self.messages[message.id] = message
        self.sent.append({"content": content, "embed": embed, "view": view})
        return message

    async def fetch_message(self, message_id):
        message = self.messages.get(int(message_id))
        if message is None:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown")
        return message

    async def delete(self, **_kwargs) -> None:
        pass


class _FakeGuild:
    id = GUILD_ID
    name = "Test Guild"

    def __init__(self) -> None:
        self.me = _FakeMember(BOT_ID)
        self._channels = {
            REVIEW_CHANNEL_ID: _FakeTextChannel(self, REVIEW_CHANNEL_ID),
            PANEL_CHANNEL_ID: _FakeTextChannel(self, PANEL_CHANNEL_ID, name="apply"),
        }
        self._roles = {}
        self._members = {}

    @property
    def review_channel(self) -> _FakeTextChannel:
        return self._channels[REVIEW_CHANNEL_ID]

    def add_role(self, role_id: int, name: str) -> _FakeRole:
        role = _FakeRole(role_id, name)
        self._roles[role_id] = role
        return role

    def get_role(self, role_id):
        return self._roles.get(int(role_id))

    def get_channel(self, channel_id):
        return self._channels.get(int(channel_id))

    def set_members(self, members) -> None:
        self._members = {member.id: member for member in members}

    def get_member(self, member_id):
        return self._members.get(int(member_id))

    async def fetch_member(self, member_id):
        member = self.get_member(member_id)
        if member is None:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown")
        return member


class _FakeResponse:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.modals: list[object] = []
        self.edits: list[dict] = []
        self.deferred = False

    def is_done(self) -> bool:
        return self.deferred

    async def defer(self, *, ephemeral=False) -> None:
        self.deferred = True

    async def send_message(self, content=None, *, embed=None, view=None, ephemeral=False, allowed_mentions=None):
        self.messages.append(
            {"content": content, "embed": embed, "view": view, "ephemeral": ephemeral}
        )

    async def edit_message(self, *, embed=None, view=None, content=None) -> None:
        # The review queue rewrites its own ephemeral message on a pick.
        self.edits.append({"content": content, "embed": embed, "view": view})

    async def send_modal(self, modal) -> None:
        self.modals.append(modal)


class _FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.embeds: list[object] = []
        self.views: list[object] = []

    async def send(self, content=None, *, embed=None, view=None, ephemeral=False, allowed_mentions=None):
        if content is not None:
            self.messages.append({"content": content, "ephemeral": ephemeral})
        if embed is not None:
            self.embeds.append(embed)
        if view is not None:
            self.views.append(view)


class _FakeInteraction:
    def __init__(self, *, user, guild, data=None) -> None:
        self.user = user
        self.guild = guild
        self.guild_id = guild.id
        self.data = data or {}
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()

    def replies(self) -> list[str]:
        return [
            item["content"]
            for item in self.response.messages + self.followup.messages
            if item.get("content")
        ]


class _FakeBot:
    def __init__(self, config, guild) -> None:
        self.config = config
        self._guild = guild
        self.user = SimpleNamespace(id=BOT_ID)
        self._cogs: dict[str, object] = {}

    @property
    def cogs(self):
        return self._cogs

    def get_guild(self, guild_id):
        return self._guild if int(guild_id) == self._guild.id else None


def _modal_values(answers: list[str]) -> dict:
    """A modal submit payload: one action row per answer, in order."""
    return {
        "components": [
            {"components": [{"custom_id": f"q{index}", "value": value}]}
            for index, value in enumerate(answers)
        ]
    }


class _IsolatedStoreMixin:
    """Temporary database + stubbed config writer.

    Redirects ``SENTINEL_DATA`` before the modules read it, so no test can
    write into the developer's real config or database.
    """

    def _isolate(self) -> None:
        self.home = tempfile.TemporaryDirectory(prefix="pm-applications-test-")
        self.db_file = Path(self.home.name) / "sentinel.db"
        self.saved_configs: list[dict] = []
        for patcher in (
            patch.object(store, "db_path", lambda: str(self.db_file)),
            patch.object(bot_module, "DB_PATH", str(self.db_file)),
            patch.object(
                applications,
                "save_config",
                lambda config: self.saved_configs.append(config),
            ),
            # The module refuses to post a review card into anything that is not
            # a text channel (the check that stops a misconfigured form from
            # silently swallowing submissions). A fake cannot satisfy
            # isinstance() without this swap of the name it looks up.
            patch.object(discord, "TextChannel", _FakeTextChannel),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        bot_module.init_db()

    def _cleanup(self) -> None:
        self.home.cleanup()


class FormConfigTests(_IsolatedStoreMixin, unittest.TestCase):
    """Forms: questions, limits, and the slash command's question syntax."""

    def setUp(self) -> None:
        self._isolate()
        self.addCleanup(self._cleanup)

    def test_form_round_trip(self) -> None:
        config = {"applications": {}}
        stored = applications.upsert_form(
            config,
            GUILD_ID,
            {
                "form_id": "f1",
                "name": "Staff application",
                "review_channel_id": REVIEW_CHANNEL_ID,
                "questions": [
                    {"label": "Why you?", "style": "short"},
                    {"label": "Experience", "style": "paragraph", "required": False},
                ],
            },
        )
        self.assertEqual(len(stored["questions"]), 2)
        self.assertEqual(stored["questions"][0]["style"], "short")
        self.assertFalse(stored["questions"][1]["required"])

        reread = applications.get_guild_forms(config, GUILD_ID)
        self.assertEqual(reread[0]["name"], "Staff application")
        self.assertTrue(self.saved_configs, "the config must be persisted")

    def test_a_form_without_questions_is_refused(self) -> None:
        with self.assertRaises(ApplicationError):
            applications.upsert_form(
                {"applications": {}}, GUILD_ID, {"form_id": "f1", "name": "Empty", "questions": []}
            )

    def test_questions_are_capped_at_discords_modal_limit(self) -> None:
        questions = [{"label": f"Question {index}"} for index in range(9)]
        normalized = applications.normalize_questions(questions)
        self.assertEqual(len(normalized), applications.MAX_QUESTIONS)

    def test_finding_by_name_is_case_insensitive(self) -> None:
        config = {"applications": {}}
        applications.upsert_form(
            config,
            GUILD_ID,
            {"form_id": "f1", "name": "Helper", "questions": [{"label": "Why?"}]},
        )
        self.assertEqual(applications.find_form(config, GUILD_ID, "helper")["form_id"], "f1")
        self.assertIsNone(applications.find_form(config, GUILD_ID, "nope"))

    def test_question_list_syntax(self) -> None:
        parsed = applications.parse_question_list(
            "Why join? | short:Timezone | paragraph:Experience"
        )
        self.assertEqual([question["label"] for question in parsed], ["Why join?", "Timezone", "Experience"])
        self.assertEqual(parsed[1]["style"], "short")
        self.assertEqual(parsed[2]["style"], "paragraph")

    def test_removing_a_form(self) -> None:
        config = {"applications": {}}
        applications.upsert_form(
            config, GUILD_ID, {"form_id": "f1", "name": "Helper", "questions": [{"label": "Why?"}]}
        )
        removed = applications.remove_form(config, GUILD_ID, "Helper")
        self.assertEqual(removed["form_id"], "f1")
        self.assertEqual(applications.get_guild_forms(config, GUILD_ID), [])
        with self.assertRaises(ApplicationError):
            applications.remove_form(config, GUILD_ID, "Helper")


class ApplicationPermissionTests(unittest.TestCase):
    """Who may read or decide an application."""

    def setUp(self) -> None:
        self.config = {"server_id": GUILD_ID, "staff_role_id": STAFF_ROLE_ID}

    def test_staff_permissions_and_role(self) -> None:
        self.assertTrue(
            applications.is_application_staff(_FakeMember(1, administrator=True), self.config, GUILD_ID)
        )
        self.assertTrue(
            applications.is_application_staff(_FakeMember(1, moderate_members=True), self.config, GUILD_ID)
        )
        self.assertTrue(
            applications.is_application_staff(
                _FakeMember(1, roles=[STAFF_ROLE_ID]), self.config, GUILD_ID
            )
        )

    def test_a_reviewer_role_counts_for_its_own_form(self) -> None:
        form = {"accept_role_id": ACCEPT_ROLE_ID}
        self.assertTrue(
            applications.is_application_staff(
                _FakeMember(1, roles=[ACCEPT_ROLE_ID]), self.config, GUILD_ID, form
            )
        )

    def test_a_plain_member_is_not_staff(self) -> None:
        member = _FakeMember(1, roles=[4242])
        self.assertFalse(applications.is_application_staff(member, self.config, GUILD_ID))
        self.assertFalse(
            applications.is_application_staff(
                member, self.config, GUILD_ID, {"accept_role_id": ACCEPT_ROLE_ID}
            )
        )


class SubmissionTests(_IsolatedStoreMixin, unittest.IsolatedAsyncioTestCase):
    """Submitting, reviewing and deciding."""

    def setUp(self) -> None:
        self._isolate()
        self.guild = _FakeGuild()
        self.guild.add_role(STAFF_ROLE_ID, "Staff")
        self.guild.add_role(ACCEPT_ROLE_ID, "Helpers")
        self.applicant = _FakeMember(APPLICANT_ID)
        self.staff = _FakeMember(STAFF_ID, roles=[STAFF_ROLE_ID])
        self.outsider = _FakeMember(OUTSIDER_ID)
        self.admin = _FakeMember(ADMIN_ID)
        self.admin.guild_permissions = SimpleNamespace(administrator=True)
        self.guild.set_members([self.applicant, self.staff, self.outsider, self.admin])
        self.config = {
            "server_id": GUILD_ID,
            "staff_role_id": STAFF_ROLE_ID,
            "applications": {
                str(GUILD_ID): {
                    "forms": [
                        {
                            "form_id": "staff",
                            "name": "Staff application",
                            "review_channel_id": REVIEW_CHANNEL_ID,
                            "accept_role_id": ACCEPT_ROLE_ID,
                            "questions": [
                                {"label": "Why do you want to join?", "style": "paragraph"},
                                {"label": "Timezone", "style": "short"},
                            ],
                        }
                    ]
                }
            },
        }
        self.bot = _FakeBot(self.config, self.guild)
        self.mixin = ApplicationsMixin(self.bot, lambda _config: None)
        self.bot.cogs["SentinelCog"] = self.mixin
        self.cog = ApplicationsCog(self.bot)

    def tearDown(self) -> None:
        self._cleanup()

    def _form(self) -> dict:
        return applications.find_form(self.bot.config, GUILD_ID, "staff")

    async def _submit(self, answers=("Because I like helping", "UTC+1")):
        return await applications.submit_application(
            self.bot,
            guild=self.guild,
            applicant=self.applicant,
            form=self._form(),
            values=dict(zip([f"q{index}" for index in range(len(answers))], answers)),
        )

    def test_answers_keep_the_questions_they_answered(self) -> None:
        encoded = applications.encode_answers(
            self._form()["questions"], {"q0": "Because", "q1": "UTC+1"}
        )
        self.assertEqual(
            applications.decode_answers(encoded),
            [
                {"question": "Why do you want to join?", "answer": "Because"},
                {"question": "Timezone", "answer": "UTC+1"},
            ],
        )

    async def test_submission_is_stored_posted_and_confirmed(self) -> None:
        application = await self._submit()
        self.assertEqual(application["status"], applications.STATUS_PENDING)
        self.assertEqual(self.applicant.dms and len(self.applicant.dms), 1)

        posted = self.guild.review_channel.sent[-1]
        self.assertIn("applied for", posted["content"])
        self.assertIn("Application #", posted["embed"].title)
        custom_ids = [child.custom_id for child in posted["view"].children]
        self.assertIn(f"sentinel:ap:decide:{application['id']}:approve", custom_ids)
        self.assertIn(f"sentinel:ap:decide:{application['id']}:deny", custom_ids)

    async def test_a_second_pending_submission_is_refused(self) -> None:
        await self._submit()
        with self.assertRaises(ApplicationError):
            await self._submit()

    async def test_approving_records_the_decision_grants_the_role_and_dms(self) -> None:
        application = await self._submit()
        decided = await applications.decide_application(
            self.bot, self.guild, application, self.staff, "approve", "Welcome!"
        )
        self.assertEqual(decided["status"], applications.STATUS_APPROVED)
        self.assertEqual(decided["decided_by"], STAFF_ID)
        self.assertEqual(decided["decision_note"], "Welcome!")
        self.assertIn(("add", ACCEPT_ROLE_ID), self.applicant.role_changes)
        # A DM on submission and one on the decision.
        self.assertEqual(len(self.applicant.dms), 2)
        # The review card lost its buttons and shows the outcome.
        review_message = self.guild.review_channel.messages[int(decided["review_message_id"])]
        self.assertTrue(review_message.edits)
        self.assertEqual(review_message.edits[-1]["view"].children, [])

    async def test_denying_applies_the_removal_role_and_never_the_accept_role(self) -> None:
        form = self._form()
        form["remove_role_id"] = STAFF_ROLE_ID
        applications.update_form(
            self.bot.config, GUILD_ID, "staff", remove_role_id=STAFF_ROLE_ID
        )
        application = await self._submit()
        decided = await applications.decide_application(
            self.bot, self.guild, application, self.staff, "deny", "Not this time"
        )
        self.assertEqual(decided["status"], applications.STATUS_DENIED)
        self.assertIn(("remove", STAFF_ROLE_ID), self.applicant.role_changes)
        self.assertNotIn(("add", ACCEPT_ROLE_ID), self.applicant.role_changes)

    async def test_a_decided_application_cannot_be_decided_twice(self) -> None:
        application = await self._submit()
        await applications.decide_application(
            self.bot, self.guild, application, self.staff, "approve"
        )
        with self.assertRaises(ApplicationError):
            await applications.decide_application(
                self.bot, self.guild, application, self.staff, "deny"
            )

    async def test_an_unknown_decision_is_refused(self) -> None:
        application = await self._submit()
        with self.assertRaises(ApplicationError):
            await applications.decide_application(
                self.bot, self.guild, application, self.staff, "maybe"
            )

    async def test_a_form_without_a_review_channel_submits_nothing(self) -> None:
        applications.update_form(
            self.bot.config, GUILD_ID, "staff", review_channel_id=None
        )
        self.bot.config.pop("staff_channel_id", None)
        with self.assertRaises(ApplicationError):
            await self._submit()
        self.assertEqual(applications.list_applications(GUILD_ID), [])


class ComponentTests(_IsolatedStoreMixin, unittest.IsolatedAsyncioTestCase):
    """Buttons, selects and modal submits, routed by custom_id."""

    def setUp(self) -> None:
        self._isolate()
        self.guild = _FakeGuild()
        self.guild.add_role(STAFF_ROLE_ID, "Staff")
        self.guild.add_role(ACCEPT_ROLE_ID, "Helpers")
        self.applicant = _FakeMember(APPLICANT_ID)
        self.staff = _FakeMember(STAFF_ID, roles=[STAFF_ROLE_ID])
        self.outsider = _FakeMember(OUTSIDER_ID)
        self.admin = _FakeMember(ADMIN_ID)
        self.admin.guild_permissions = SimpleNamespace(administrator=True)
        self.guild.set_members([self.applicant, self.staff, self.outsider, self.admin])
        self.config = {
            "server_id": GUILD_ID,
            "staff_role_id": STAFF_ROLE_ID,
            "applications": {
                str(GUILD_ID): {
                    "forms": [
                        {
                            "form_id": "staff",
                            "name": "Staff application",
                            "review_channel_id": REVIEW_CHANNEL_ID,
                            "questions": [{"label": "Why do you want to join?"}],
                        }
                    ]
                }
            },
        }
        self.bot = _FakeBot(self.config, self.guild)
        self.mixin = ApplicationsMixin(self.bot, lambda _config: None)
        self.bot.cogs["SentinelCog"] = self.mixin
        self.cog = ApplicationsCog(self.bot)

    def tearDown(self) -> None:
        self._cleanup()

    async def test_apply_command_opens_the_modal_for_a_member(self) -> None:
        interaction = _FakeInteraction(user=self.applicant, guild=self.guild)
        await self.cog.apply.callback(self.cog, interaction)
        self.assertEqual(len(interaction.response.modals), 1)
        modal = interaction.response.modals[0]
        self.assertEqual(modal.custom_id, "sentinel:ap:modal:staff")
        # One text input per question, and nothing that could read a
        # submission back: /apply is submit-only by construction.
        self.assertEqual(len(modal.children), 1)

    async def test_apply_refuses_when_the_server_has_no_forms(self) -> None:
        self.config["applications"] = {}
        interaction = _FakeInteraction(user=self.applicant, guild=self.guild)
        await self.cog.apply.callback(self.cog, interaction)
        self.assertEqual(interaction.response.modals, [])
        self.assertIn("no open applications", " ".join(interaction.replies()).lower())

    async def test_panel_button_opens_the_modal(self) -> None:
        interaction = _FakeInteraction(
            user=self.applicant,
            guild=self.guild,
            data={"custom_id": "sentinel:ap:apply:staff"},
        )
        self.assertTrue(await applications.route_application_interaction(self.bot, interaction))
        self.assertEqual(interaction.response.modals[0].custom_id, "sentinel:ap:modal:staff")

    async def test_form_picker_opens_the_chosen_form(self) -> None:
        interaction = _FakeInteraction(
            user=self.applicant,
            guild=self.guild,
            data={"custom_id": "sentinel:ap:choose", "values": ["staff"]},
        )
        await applications.route_application_interaction(self.bot, interaction)
        self.assertEqual(interaction.response.modals[0].custom_id, "sentinel:ap:modal:staff")

    async def test_modal_submit_stores_the_application(self) -> None:
        interaction = _FakeInteraction(
            user=self.applicant,
            guild=self.guild,
            data={"custom_id": "sentinel:ap:modal:staff", **_modal_values(["I want to help"])},
        )
        await applications.route_application_interaction(self.bot, interaction)
        stored = applications.list_applications(GUILD_ID)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["answers"][0]["answer"], "I want to help")
        self.assertIn("#", " ".join(interaction.replies()))

    async def test_modal_submit_reports_a_missing_required_answer(self) -> None:
        interaction = _FakeInteraction(
            user=self.applicant,
            guild=self.guild,
            data={"custom_id": "sentinel:ap:modal:staff", **_modal_values([""])},
        )
        await applications.route_application_interaction(self.bot, interaction)
        self.assertEqual(applications.list_applications(GUILD_ID), [])
        self.assertIn("please answer", " ".join(interaction.replies()).lower())

    async def _submitted(self) -> dict:
        interaction = _FakeInteraction(
            user=self.applicant,
            guild=self.guild,
            data={"custom_id": "sentinel:ap:modal:staff", **_modal_values(["I want to help"])},
        )
        await applications.route_application_interaction(self.bot, interaction)
        return applications.list_applications(GUILD_ID)[0]

    async def test_a_member_cannot_press_approve(self) -> None:
        application = await self._submitted()
        interaction = _FakeInteraction(
            user=self.outsider,
            guild=self.guild,
            data={"custom_id": f"sentinel:ap:decide:{application['id']}:approve"},
        )
        await applications.route_application_interaction(self.bot, interaction)
        self.assertEqual(interaction.response.modals, [])
        self.assertIn("only staff", " ".join(interaction.replies()).lower())
        self.assertEqual(
            applications.get_application(application["id"])["status"],
            applications.STATUS_PENDING,
        )

    async def test_staff_press_approve_then_confirm_in_the_modal(self) -> None:
        application = await self._submitted()
        press = _FakeInteraction(
            user=self.staff,
            guild=self.guild,
            data={"custom_id": f"sentinel:ap:decide:{application['id']}:approve"},
        )
        await applications.route_application_interaction(self.bot, press)
        self.assertEqual(len(press.response.modals), 1)

        confirm = _FakeInteraction(
            user=self.staff,
            guild=self.guild,
            data={
                "custom_id": f"sentinel:ap:decided:{application['id']}:approve",
                # The decision modal's note input is named "note", not q0.
                "components": [
                    {"components": [{"custom_id": "note", "value": "Great answers"}]}
                ],
            },
        )
        await applications.route_application_interaction(self.bot, confirm)
        decided = applications.get_application(application["id"])
        self.assertEqual(decided["status"], applications.STATUS_APPROVED)
        self.assertEqual(decided["decision_note"], "Great answers")

    async def test_a_member_cannot_review_or_decide(self) -> None:
        """The permission split: submitting is the only thing a member gets.

        The review queue (which is where the answers live) and the by-id
        decision are both staff-only, and the check runs when the command is
        used, not when Discord drew it.
        """
        application = await self._submitted()
        for handler, kwargs in (
            (self.mixin.applications_review.callback, {}),
            (
                self.mixin.applications_view.callback,
                {"application": str(application["id"])},
            ),
            (
                self.mixin.applications_accept.callback,
                {"application": str(application["id"])},
            ),
            (
                self.mixin.applications_decide.callback,
                {
                    "application": str(application["id"]),
                    "decision": SimpleNamespace(value="approve"),
                },
            ),
            (self.mixin.applications_form.callback, {}),
        ):
            with self.subTest(handler=handler.__name__):
                interaction = _FakeInteraction(user=self.outsider, guild=self.guild)
                await handler(self.mixin, interaction, **kwargs)
                reply = "\n".join(interaction.replies()).lower()
                self.assertTrue(
                    "only staff" in reply or "only server administrators" in reply,
                    interaction.replies(),
                )

    async def test_staff_can_view_one_application_by_id(self) -> None:
        application = await self._submitted()
        interaction = _FakeInteraction(user=self.staff, guild=self.guild)
        await self.mixin.applications_view.callback(
            self.mixin, interaction, application=str(application["id"])
        )
        self.assertEqual(len(interaction.followup.embeds), 1)
        embed = interaction.followup.embeds[0]
        self.assertIn(f"Application #{application['id']:04d}", embed.title)
        self.assertEqual(embed.fields[0].value, "I want to help")
        self.assertEqual(
            [child.label for child in interaction.followup.views[0].children],
            ["Approve", "Deny"],
        )

    async def test_staff_can_accept_one_application_by_id(self) -> None:
        application = await self._submitted()
        interaction = _FakeInteraction(user=self.staff, guild=self.guild)
        await self.mixin.applications_accept.callback(
            self.mixin,
            interaction,
            application=str(application["id"]),
            note="Welcome aboard!",
        )
        accepted = applications.get_application(application["id"])
        self.assertEqual(accepted["status"], applications.STATUS_APPROVED)
        self.assertEqual(accepted["decision_note"], "Welcome aboard!")
        self.assertIn("approved", " ".join(interaction.replies()).lower())

    async def test_staff_review_queue_shows_answers_and_decides(self) -> None:
        application = await self._submitted()

        queue = _FakeInteraction(user=self.staff, guild=self.guild)
        await self.mixin.applications_review.callback(self.mixin, queue)
        self.assertEqual(len(queue.followup.embeds), 1)
        self.assertEqual(queue.followup.embeds[0].title, "Applications to review")
        options = queue.followup.views[0].children[0].options
        self.assertEqual(options[0].value, str(application["id"]))

        # Picking a submission rewrites the message into its review card ...
        pick = _FakeInteraction(
            user=self.staff,
            guild=self.guild,
            data={
                "custom_id": f"sentinel:ap:review:{application['id']}",
                "values": [str(application["id"])],
            },
        )
        await applications.route_application_interaction(self.bot, pick)
        card = pick.response.edits[0]
        self.assertIn("I want to help", card["embed"].fields[0].value)
        labels = [child.label for child in card["view"].children]
        self.assertEqual(labels, ["Approve", "Deny"])

        # ... and the buttons lead to the ordinary decision modal.
        approve = _FakeInteraction(
            user=self.staff,
            guild=self.guild,
            data={
                "custom_id": f"sentinel:ap:decide:{application['id']}:approve",
            },
        )
        await applications.route_application_interaction(self.bot, approve)
        self.assertEqual(len(approve.response.modals), 1)

    async def test_staff_can_decide_by_id(self) -> None:
        """The by-id path, for a submission that is not in the queue's select."""
        application = await self._submitted()
        decide = _FakeInteraction(user=self.staff, guild=self.guild)
        await self.mixin.applications_decide.callback(
            self.mixin,
            decide,
            application=str(application["id"]),
            decision=SimpleNamespace(value="deny"),
            note="Not this time",
        )
        stored = applications.get_application(application["id"])
        self.assertEqual(stored["status"], applications.STATUS_DENIED)
        self.assertEqual(stored["decision_note"], "Not this time")
        self.assertIsNotNone(stored["decided_at"])

    async def test_form_command_lists_creates_edits_and_removes(self) -> None:
        listed = _FakeInteraction(user=self.admin, guild=self.guild)
        await self.mixin.applications_form.callback(self.mixin, listed)
        reply = "\n".join(listed.replies())
        self.assertIn("Staff application", reply)
        self.assertIn("Why do you want to join?", reply)

        created = _FakeInteraction(user=self.admin, guild=self.guild)
        await self.mixin.applications_form.callback(
            self.mixin,
            created,
            name="Event team",
            review_channel=self.guild.get_channel(REVIEW_CHANNEL_ID),
            questions="Why events? | short:Timezone",
            accept_role=self.guild.get_role(ACCEPT_ROLE_ID),
        )
        stored = applications.find_form(self.bot.config, GUILD_ID, "Event team")
        self.assertIsNotNone(stored)
        self.assertEqual([q["label"] for q in stored["questions"]], ["Why events?", "Timezone"])

        # Editing only what changes keeps the questions and the review channel.
        edited = _FakeInteraction(user=self.admin, guild=self.guild)
        await self.mixin.applications_form.callback(
            self.mixin, edited, name="Event team", allow_multiple=True
        )
        stored = applications.find_form(self.bot.config, GUILD_ID, "Event team")
        self.assertTrue(stored["allow_multiple"])
        self.assertEqual([q["label"] for q in stored["questions"]], ["Why events?", "Timezone"])
        self.assertEqual(stored["review_channel_id"], REVIEW_CHANNEL_ID)

        removed = _FakeInteraction(user=self.admin, guild=self.guild)
        await self.mixin.applications_form.callback(
            self.mixin, removed, name="Event team", remove=True
        )
        self.assertIsNone(applications.find_form(self.bot.config, GUILD_ID, "Event team"))
        self.assertIsNotNone(applications.find_form(self.bot.config, GUILD_ID, "staff"))

    async def test_removing_a_form_leaves_old_submissions_readable(self) -> None:
        application = await self._submitted()
        applications.remove_form(self.bot.config, GUILD_ID, "staff")
        stored = applications.get_application(application["id"])
        self.assertEqual(stored["form_name"], "Staff application")
        self.assertEqual(stored["answers"][0]["question"], "Why do you want to join?")


class ReviewCardTests(_IsolatedStoreMixin, unittest.IsolatedAsyncioTestCase):
    """The review card renders every answer and the decision state."""

    def setUp(self) -> None:
        self._isolate()
        self.guild = _FakeGuild()

    def tearDown(self) -> None:
        self._cleanup()

    def test_review_embed_lists_every_answer(self) -> None:
        embed = applications.review_embed(
            self.guild,
            {
                "id": 4,
                "form_name": "Staff application",
                "user_id": APPLICANT_ID,
                "status": applications.STATUS_PENDING,
                "submitted_at": "2026-01-01T00:00:00+00:00",
                "answers": [
                    {"question": "Why?", "answer": "Because"},
                    {"question": "Timezone", "answer": ""},
                ],
            },
        )
        self.assertIn("Application #0004", embed.title)
        self.assertEqual(embed.fields[0].name, "1. Why?")
        self.assertEqual(embed.fields[0].value, "Because")
        self.assertEqual(embed.fields[1].value, "*left blank*")

    def test_decided_applications_have_no_buttons(self) -> None:
        pending = applications.review_view({"id": 1, "status": "pending"})
        self.assertEqual(len(pending.children), 2)
        decided = applications.review_view({"id": 1, "status": "approved"})
        self.assertEqual(decided.children, [])


def main() -> int:
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
    sys.exit(main())
