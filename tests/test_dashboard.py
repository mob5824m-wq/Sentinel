"""Authentication and session tests for the server-side web dashboard."""

from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import discord  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from dashboard import (  # noqa: E402
    DashboardServer,
    _json_safe_ids,
    _snowflake,
    ensure_dashboard_token,
)


# A realistic Discord snowflake: larger than 2**53, so a JavaScript client
# rounds it if it arrives as a JSON number (…789 comes back as …800).
GUILD_ID = 1234567890123456789
OWNER_ID = 222222222222222222


class FakeBot:
    config = {}
    guilds = []
    user = None

    def is_ready(self):
        return False


class FakeGuild:
    id = GUILD_ID
    name = "Test Guild"
    icon = None
    member_count = 7
    owner_id = 222222222222222222
    roles = []
    text_channels = []
    # Discord *category* channels: they are containers rather than posting
    # destinations, so the guild payload lists them separately.
    categories = []


class GuildFakeBot(FakeBot):
    guilds = [FakeGuild()]

    def get_guild(self, guild_id):
        return FakeGuild() if int(guild_id) == GUILD_ID else None


def _self_signed_certificate(case: unittest.TestCase) -> tuple[Path, Path]:
    """Write a throwaway self-signed TLS certificate for a test.

    Generated rather than committed: a checked-in private key is a bad habit
    even for localhost, and openssl is present wherever this suite runs.
    """
    import shutil
    import subprocess
    import tempfile

    if shutil.which("openssl") is None:  # pragma: no cover - CI images have it
        case.skipTest("openssl is not available to generate a test certificate")
    tmp = Path(tempfile.mkdtemp(prefix="sentinel-tls-"))
    case.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
    cert, key = tmp / "cert.pem", tmp / "key.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(cert),
            "-days", "1", "-subj", "/CN=myhome.duckdns.org",
        ],
        check=True, capture_output=True,
    )
    return cert, key


class SnowflakeSerialisationTests(unittest.TestCase):
    """Discord ids must cross the JSON boundary as strings.

    Regression test: sending snowflakes as JSON numbers made the browser round
    them, so selecting a server produced "The bot is not connected to that
    server." even when the bot was connected.
    """

    def test_snowflake_helper(self) -> None:
        self.assertEqual(_snowflake(GUILD_ID), str(GUILD_ID))
        self.assertIsNone(_snowflake(None))
        self.assertIsNone(_snowflake(""))

    def test_row_ids_are_stringified(self) -> None:
        row = _json_safe_ids(
            {
                "user_id": 333333333333333333,
                "moderator_id": 444444444444444444,
                "guild_id": 555555555555555555,
                "id": 3,
                "reason": "no reason",
                "count": 2,
            }
        )
        self.assertEqual(row["user_id"], "333333333333333333")
        self.assertEqual(row["moderator_id"], "444444444444444444")
        self.assertEqual(row["guild_id"], "555555555555555555")
        self.assertEqual(row["id"], 3)  # database row key, not a snowflake
        self.assertEqual(row["count"], 2)
        self.assertEqual(row["reason"], "no reason")


class DashboardTokenTests(unittest.TestCase):
    def test_token_is_generated_once_and_saved(self) -> None:
        config = {"dashboard_token": ""}
        writes = []
        token = ensure_dashboard_token(config, writes.append)
        self.assertGreaterEqual(len(token), 32)
        self.assertEqual(config["dashboard_token"], token)
        self.assertEqual(writes[0]["dashboard_token"], token)
        self.assertEqual(ensure_dashboard_token(config, writes.append), token)
        self.assertEqual(len(writes), 1)

    def test_environment_token_overrides_stored_value(self) -> None:
        config = {"dashboard_token": "S" * 40}
        with patch.dict(os.environ, {"SENTINEL_DASHBOARD_TOKEN": "E" * 40}):
            token = ensure_dashboard_token(
                config,
                lambda _cfg: self.fail("environment token should not be saved"),
            )
        self.assertEqual(token, "E" * 40)

    def test_weak_configured_token_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ensure_dashboard_token({"dashboard_token": "short"}, lambda _cfg: None)


class DashboardSessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.saved = []
        self.bot = FakeBot()
        self.server = DashboardServer(
            self.bot,
            save_config=self.saved.append,
            db_fetchall=lambda *_args: [],
            db_fetchone=lambda *_args: None,
            db_execute=lambda *_args: None,
            archive_punishment=lambda *_args, **_kwargs: None,
            get_guild_config=lambda *_args: None,
            get_staff_channel_id=lambda *_args: None,
            should_dm_user=lambda *_args: True,
            is_protected_member=lambda *_args, **_kwargs: None,
            parse_duration=lambda _value: None,
            format_duration=lambda value: str(value),
        )
        self.server._token = "T" * 48

    async def test_login_cookie_csrf_and_logout_flow(self) -> None:
        async with TestClient(TestServer(self.server._build_app())) as client:
            page = await client.get("/")
            self.assertEqual(page.status, 200)
            self.assertIn("Server Console", await page.text())
            hostile_host = await client.get("/", headers={"Host": "attacker.example"})
            self.assertEqual(hostile_host.status, 400)

            denied = await client.get("/api/overview")
            self.assertEqual(denied.status, 401)

            bad_login = await client.post("/api/login", json={"token": "not-the-key"})
            self.assertEqual(bad_login.status, 401)

            login = await client.post("/api/login", json={"token": self.server._token})
            self.assertEqual(login.status, 200)
            body = await login.json()
            csrf = body["csrfToken"]
            self.assertTrue(login.cookies.get("sentinel_dashboard_session").value)
            self.assertTrue(login.cookies.get("sentinel_dashboard_session")["httponly"])

            overview = await client.get("/api/overview")
            self.assertEqual(overview.status, 200)
            self.assertEqual((await overview.json())["bot"]["guildCount"], 0)

            csrf_denied = await client.post("/api/logout", json={})
            self.assertEqual(csrf_denied.status, 403)

            logout = await client.post(
                "/api/logout",
                json={},
                headers={"X-CSRF-Token": csrf},
            )
            self.assertEqual(logout.status, 200)
            denied_again = await client.get("/api/overview")
            self.assertEqual(denied_again.status, 401)

    async def _login(self, client) -> str:
        login = await client.post("/api/login", json={"token": self.server._token})
        self.assertEqual(login.status, 200)
        return (await login.json())["csrfToken"]

    async def test_guild_ids_cross_the_wire_as_strings(self) -> None:
        self.server.bot = GuildFakeBot()
        async with TestClient(TestServer(self.server._build_app())) as client:
            await self._login(client)

            overview = await client.get("/api/overview")
            self.assertEqual(overview.status, 200)
            raw = await overview.text()
            body = await overview.json()
            self.assertIsInstance(body["guilds"][0]["id"], str)
            self.assertEqual(body["guilds"][0]["id"], str(GUILD_ID))
            # A JSON number here is what the browser rounds.
            self.assertIn(f'"id": "{GUILD_ID}"', raw)

            detail = await client.get(f"/api/guilds/{GUILD_ID}")
            self.assertEqual(detail.status, 200)
            self.assertEqual((await detail.json())["guild"]["ownerId"], "222222222222222222")

    async def test_browser_rounded_id_is_rejected(self) -> None:
        """Documents the failure the string encoding prevents."""
        rounded = int(float(GUILD_ID))  # what JavaScript does to the number
        self.assertNotEqual(rounded, GUILD_ID)
        self.server.bot = GuildFakeBot()
        async with TestClient(TestServer(self.server._build_app())) as client:
            await self._login(client)
            response = await client.get(f"/api/guilds/{rounded}")
            self.assertEqual(response.status, 404)


class RemoteAccessTests(unittest.IsolatedAsyncioTestCase):
    """Reaching the dashboard under a public name such as a DuckDNS subdomain.

    A remote visitor is allowed on purpose here, so the failure modes are
    configuration ones, and every one of them looks like a different kind of
    "the dashboard is broken" in the browser:

    * a 400 ``Unrecognized Host header`` from the anti-DNS-rebinding check,
    * a login that succeeds and immediately bounces (a cookie the browser
      refuses to store because it was not marked secure, or one sent over
      plain HTTP and dropped),
    * a connection that never arrives (loopback-only listener, no proxy).

    These tests pin the behaviour and the guidance that turns each symptom into
    the setting to change.
    """

    def make_server(self, **config):
        """A dashboard server over the given config; no socket is bound."""
        bot = FakeBot()
        bot.config = {"dashboard_enabled": True, "dashboard_port": 8765, **config}
        return DashboardServer(
            bot,
            save_config=lambda _cfg: None,
            db_fetchall=lambda *_args: [],
            db_fetchone=lambda *_args: None,
            db_execute=lambda *_args: None,
            archive_punishment=lambda *_args, **_kwargs: None,
            get_guild_config=lambda *_args: None,
            get_staff_channel_id=lambda *_args: None,
            should_dm_user=lambda *_args: True,
            is_protected_member=lambda *_args, **_kwargs: None,
            parse_duration=lambda _value: None,
            format_duration=lambda value: str(value),
        )

    async def test_the_public_name_must_be_allowlisted_and_then_works(self) -> None:
        server = self.make_server()
        server._token = "T" * 48
        async with TestClient(TestServer(server._build_app())) as client:
            blocked = await client.get("/", headers={"Host": "myhome.duckdns.org"})
            self.assertEqual(blocked.status, 400)

        server = self.make_server(dashboard_allowed_hosts=["myhome.duckdns.org"])
        server._token = "T" * 48
        async with TestClient(TestServer(server._build_app())) as client:
            allowed = await client.get("/", headers={"Host": "myhome.duckdns.org"})
            self.assertEqual(allowed.status, 200)

    async def test_the_public_url_is_allowlisted_implicitly(self) -> None:
        # One setting instead of two: the host of dashboard_public_url is the
        # host users actually type, so it is allowed without repeating it.
        server = self.make_server(dashboard_public_url="https://myhome.duckdns.org")
        server._token = "T" * 48
        async with TestClient(TestServer(server._build_app())) as client:
            response = await client.get("/", headers={"Host": "myhome.duckdns.org"})
            self.assertEqual(response.status, 200)

    async def test_a_configured_allowlist_replaces_rather_than_extends_localhost(self) -> None:
        server = self.make_server(dashboard_allowed_hosts=["myhome.duckdns.org"])
        server._token = "T" * 48
        async with TestClient(TestServer(server._build_app())) as client:
            # localhost still works: it cannot be forged by a hostile page.
            self.assertEqual((await client.get("/")).status, 200)
            self.assertEqual(
                (await client.get("/", headers={"Host": "attacker.example"})).status, 400
            )

    async def test_urls_put_the_public_address_first(self) -> None:
        server = self.make_server(dashboard_public_url="https://myhome.duckdns.org")
        server._apply_settings(server.bot.config)
        self.assertEqual(server.urls()[0], "https://myhome.duckdns.org/")
        self.assertIn("http://127.0.0.1:8765/", server.urls())
        self.assertEqual(server.scheme, "http")
        self.assertEqual(server.browser_scheme, "https")

    def test_tls_settings_are_validated_before_binding(self) -> None:
        server = self.make_server(dashboard_tls_cert="/tmp/only-cert.pem")
        with self.assertRaises(ValueError) as ctx:
            server._apply_settings(server.bot.config)
        self.assertIn("dashboard_tls_key", str(ctx.exception))

        server = self.make_server(
            dashboard_tls_cert="/tmp/only-cert.pem", dashboard_tls_key="/tmp/only-key.pem"
        )
        with self.assertRaises(ValueError) as ctx:
            server._apply_settings(server.bot.config)
        message = str(ctx.exception)
        self.assertIn("dashboard_tls_cert", message)

    def test_a_bad_certificate_fails_loudly(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            cert = Path(tmp) / "cert.pem"
            key = Path(tmp) / "key.pem"
            cert.write_text("not a certificate", encoding="utf-8")
            key.write_text("not a key", encoding="utf-8")
            server = self.make_server(dashboard_tls_cert=str(cert), dashboard_tls_key=str(key))
            # Fail at startup with the file name, rather than serving a
            # certificate no browser will trust.
            with self.assertRaises(ValueError) as ctx:
                server._apply_settings(server.bot.config)
            self.assertIn("cert", str(ctx.exception).lower())

    async def test_a_tls_listener_serves_https_and_forces_a_secure_cookie(self) -> None:
        cert, key = _self_signed_certificate(self)
        server = self.make_server(dashboard_tls_cert=str(cert), dashboard_tls_key=str(key))
        server._token = "T" * 48
        async with TestClient(TestServer(server._build_app())) as client:
            response = await client.post("/api/login", json={"token": server._token})
            self.assertEqual(response.status, 200)
            cookie = response.cookies.get("sentinel_dashboard_session")
            self.assertTrue(cookie["secure"], "the session cookie must not travel over HTTP")
        self.assertEqual(server.scheme, "https")
        self.assertEqual(server.browser_scheme, "https")

    async def test_forwarded_for_is_ignored_from_an_untrusted_peer(self) -> None:
        server = self.make_server(dashboard_trusted_proxies=["10.0.0.1"])
        server._apply_settings(server.bot.config)
        request = SimpleNamespace(remote="127.0.0.1", headers={"X-Forwarded-For": "198.51.100.9"})
        self.assertEqual(server._client_ip(request), "127.0.0.1")

    async def test_forwarded_for_is_believed_from_a_trusted_proxy(self) -> None:
        server = self.make_server(dashboard_trusted_proxies=["127.0.0.1"])
        server._apply_settings(server.bot.config)
        request = SimpleNamespace(remote="127.0.0.1", headers={"X-Forwarded-For": "198.51.100.9"})
        self.assertEqual(server._client_ip(request), "198.51.100.9")

        # Behind two trusted hops, walk past our own proxies to the client.
        server = self.make_server(dashboard_trusted_proxies=["127.0.0.1", "10.0.0.0/8"])
        server._apply_settings(server.bot.config)
        request = SimpleNamespace(
            remote="127.0.0.1", headers={"X-Forwarded-For": "198.51.100.9, 10.1.2.3"}
        )
        self.assertEqual(server._client_ip(request), "198.51.100.9")

    async def test_the_login_throttle_sees_the_client_not_the_proxy(self) -> None:
        # Without trusting the proxy every request looks like it came from the
        # proxy's address, so five bad guesses by a stranger would lock the
        # whole internet out of the dashboard.
        server = self.make_server(dashboard_trusted_proxies=["127.0.0.1"])
        server._token = "T" * 48
        async with TestClient(TestServer(server._build_app())) as client:
            for _ in range(6):
                response = await client.post(
                    "/api/login",
                    json={"token": "wrong"},
                    headers={"X-Forwarded-For": "198.51.100.9"},
                )
            self.assertEqual(response.status, 429, "the abusive client is throttled")

            other = await client.post(
                "/api/login",
                json={"token": server._token},
                headers={"X-Forwarded-For": "198.51.100.10"},
            )
            self.assertEqual(other.status, 200, "an innocent client must not be locked out")

    async def test_an_attacker_cannot_invent_a_client_address(self) -> None:
        # With no trusted proxy configured, X-Forwarded-For is just a header a
        # stranger can write, so it must not let them dodge the throttle.
        server = self.make_server()
        server._token = "T" * 48
        async with TestClient(TestServer(server._build_app())) as client:
            for index in range(6):
                response = await client.post(
                    "/api/login",
                    json={"token": "wrong"},
                    headers={"X-Forwarded-For": f"203.0.113.{index}"},
                )
            self.assertEqual(response.status, 429)

    def test_remote_access_warnings_name_the_fix(self) -> None:
        # The dashboard knows the listener's shape; the DuckDNS module knows the
        # public name. `bot.py --dashboard` prints both lists, and between them
        # a user has to be told each of the three things that must change.
        import duckdns

        config = {"duckdns_domain": "myhome", "duckdns_token": "t"}
        server = self.make_server(**config)
        server._apply_settings(server.bot.config)
        text = "\n".join(list(server.remote_access_warnings()) + duckdns.dashboard_warnings(server.bot.config))
        self.assertIn("dashboard_allowed_hosts", text)
        self.assertIn("loopback", text)
        self.assertIn("dashboard_public_url", text)

    def test_an_exposed_listener_without_an_allowlist_is_called_out(self) -> None:
        server = self.make_server(dashboard_host="0.0.0.0")
        server._apply_settings(server.bot.config)
        text = "\n".join(server.remote_access_warnings())
        self.assertIn("dashboard_allowed_hosts", text)
        self.assertIn("clear text", text)


    def test_no_warnings_once_the_remote_setup_is_complete(self) -> None:
        # The proxy-terminated shape: plain HTTP on loopback (only the proxy can
        # reach it), HTTPS in the URL users open, and a secure cookie.
        server = self.make_server(
            dashboard_public_url="https://myhome.duckdns.org",
            dashboard_secure_cookie=True,
        )
        server._apply_settings(server.bot.config)
        self.assertEqual(server.remote_access_warnings(), [])
        self.assertEqual(server.browser_scheme, "https")
        self.assertTrue(server._secure_cookie)


class DashboardMarkupTests(unittest.TestCase):
    """Static checks on the served UI, which no test can click through.

    The page script looks its elements up by id; a typo there fails silently in
    the browser (a null dereference on the first interaction), so every
    ``$("id")`` the script uses must exist in the markup.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (REPO_ROOT / "dashboard.html").read_text(encoding="utf-8")
        cls.script = cls.html.rsplit("<script>", 1)[1]

    def test_every_id_the_script_looks_up_exists(self) -> None:
        markup_ids = set(re.findall(r'id="([^"]+)"', self.html))
        used_ids = set(re.findall(r'\$\("([^"]+)"\)', self.script))
        self.assertTrue(used_ids, "no element lookups found - did the script move?")
        self.assertEqual(used_ids - markup_ids, set())

    def test_ids_are_unique_across_the_page(self) -> None:
        """One id must mean one element.

        Regression: the applications publish form carried ``id="application-panel-form"``
        on both the <form> and its <select>. ``document.getElementById`` returns
        the first match, so ``fillSelect("application-panel-form", …)`` replaced
        the *form's* innerHTML with <option> elements and destroyed the panel
        editor — a break no server-side endpoint test could see.
        """
        ids = re.findall(r'id="([^"]+)"', self.html)
        duplicates = sorted({value for value in ids if ids.count(value) > 1})
        self.assertEqual(duplicates, [], f"duplicate element ids in dashboard.html: {duplicates}")

    def test_every_select_filled_by_the_script_exists(self) -> None:
        markup_ids = set(re.findall(r'id="([^"]+)"', self.html))
        filled = set(re.findall(r'fillSelect\("([^"]+)"', self.script))
        self.assertTrue(filled, "no fillSelect calls found - did the script move?")
        self.assertEqual(filled - markup_ids, set())

    def test_applications_page_is_wired_up(self) -> None:
        for element in (
            'id="page-applications"',
            'data-page="applications"',
            'id="application-form"',
            'id="application-questions"',
            'id="application-panel-publish"',
            'id="application-panel-form"',
            'id="application-panel-channel"',
            'id="application-status-filter"',
            'id="application-table"',
            'id="application-detail"',
        ):
            with self.subTest(element=element):
                self.assertIn(element, self.html)
        # The publish form and the <select> inside it must stay separate
        # elements, and the script must submit the form, not the select.
        self.assertIn('$("application-panel-publish").addEventListener("submit"', self.script)
        self.assertIn('const formId = $("application-panel-form").value;', self.script)

    def test_removed_features_left_no_trace_in_the_ui(self) -> None:
        """Rules, reaction-role menus and tickets are gone from the product.

        A leftover nav button or page section would render a dead tab — the
        script that filled it was deleted with the feature, so clicking it
        shows an empty page with no error anywhere.
        """
        lowered = self.html.lower()
        for absent in (
            'data-page="rules"',
            'data-page="tickets"',
            'id="page-rules"',
            'id="page-tickets"',
            "reaction-roles",
            "/tickets",
        ):
            with self.subTest(absent=absent):
                self.assertNotIn(absent, lowered)
        # Every page in the nav has a matching section, and vice versa.
        nav_pages = set(re.findall(r'data-page="([^"]+)"', self.html))
        sections = set(re.findall(r'<section id="page-([^"]+)"', self.html))
        self.assertEqual(nav_pages, sections)
        # ...and each has an entry in the script's `titles` map, which goTo()
        # refuses to navigate to without one.
        declared = set(
            re.findall(r"^\s{8}(\w+): \[", self.script.split("const titles = {", 1)[1]
                       .split("\n      };", 1)[0], re.M)
        )
        self.assertEqual(nav_pages, declared)


def main() -> int:
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
