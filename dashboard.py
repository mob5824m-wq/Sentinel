"""Authenticated server-side web dashboard for Sentinel.

The dashboard is deliberately bound to loopback by default. Its token is a
high-privilege, bot-wide credential; remote deployments should put it behind
HTTPS and a firewall (or reach it through an SSH tunnel).

Reaching it from outside the house - typically under a dynamic-DNS name such as
`yourname.duckdns.org` - needs three things, and the config keys that provide
them:

  * ``dashboard_host`` / ``dashboard_port``  what to listen on. Keep the default
    loopback binding and let a reverse proxy on the same machine reach it (see
    docs/REMOTE_ACCESS.md), or bind 0.0.0.0 and forward the port.
  * ``dashboard_allowed_hosts``              the public name(s) the dashboard
    will answer for. The HTTP ``Host`` header is checked against this list to
    block DNS-rebinding attacks, so a name that is not listed is rejected with
    "400 Unrecognized Host header" - that is the allowlist doing its job, not a
    network failure. ``dashboard_public_url``'s hostname is allowed implicitly.
  * ``dashboard_tls_cert`` / ``dashboard_tls_key``  serve HTTPS directly, or
    terminate TLS in a proxy and set ``dashboard_secure_cookie`` plus
    ``dashboard_public_url``.

``dashboard_trusted_proxies`` is the bridge to a reverse proxy: the addresses
(IPs or CIDR ranges) whose ``X-Forwarded-For`` header may be believed. Without
it, a proxied dashboard sees every visitor as the proxy's address, which turns
the login throttle into one bucket per proxy instead of per attacker.
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import os
import secrets
import sqlite3
import ssl
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any, Callable, Optional

import discord
from aiohttp import web

import applications
import paths


logger = logging.getLogger("sentinel.dashboard")
SESSION_COOKIE = "sentinel_dashboard_session"
SESSION_TTL_SECONDS = 8 * 60 * 60
MAX_LOGIN_FAILURES = 5
LOGIN_WINDOW_SECONDS = 5 * 60
MAX_REASON_LENGTH = 900
#: Rows a list endpoint returns at once. The dashboard shows the newest slice;
#: anything older is still in SQLite and one filter away.
MAX_DASHBOARD_ROWS = 100

#: Permissions the punishment roles must never grant. Sentinel swaps these
#: roles onto real members on a timer, so a role carrying moderation or
#: server-management power would hand that power to whoever is punished — and
#: to whoever they are after the punishment ends. Ordinary access/verified/
#: member roles are fine.
#:
#: This lived in the rules module, which the self-service reaction features
#: shared it with; those are gone, and server settings are the only remaining
#: consumer, so the policy lives here next to the check that applies it.
PRIVILEGED_ROLE_PERMISSIONS = (
    "administrator",
    "manage_guild",
    "manage_roles",
    "manage_channels",
    "manage_messages",
    "manage_webhooks",
    "manage_threads",
    "view_audit_log",
    "kick_members",
    "ban_members",
    "moderate_members",
    "manage_nicknames",
    "manage_emojis_and_stickers",
    "manage_events",
)


def _is_loopback_host(host: str) -> bool:
    """True for 127.0.0.1 / ::1 / localhost / another loopback address."""
    name = str(host or "").strip().lower()
    if name in {"", "localhost"}:
        return True
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def _snowflake(value: Any) -> Optional[str]:
    """Serialise a Discord id as a string for JSON transport.

    Snowflakes are 64-bit values that exceed JavaScript's
    ``Number.MAX_SAFE_INTEGER`` (2**53 - 1), so sending one as a JSON number
    makes the browser silently round it — ``...789`` comes back as ``...800``.
    The rounded id is then sent back on the next request and matches no guild,
    member, role, or channel, which surfaced as "The bot is not connected to
    that server." even though it is. Strings survive the round trip intact.
    """
    if value is None or value == "":
        return None
    return str(value)


def ensure_dashboard_token(
    config: dict,
    save_config: Callable[[dict], None],
) -> str:
    """Return the dashboard credential, creating a private config value once.

    ``SENTINEL_DASHBOARD_TOKEN`` overrides the stored value. A
    generated token is written through the same private config writer as the
    Discord bot token, and is never printed by the dashboard server itself.
    """
    env_token = os.environ.get("SENTINEL_DASHBOARD_TOKEN", "").strip()
    configured = env_token or config.get("dashboard_token")
    if isinstance(configured, str) and configured:
        if len(configured) < 32:
            raise ValueError("dashboard_token must contain at least 32 characters")
        return configured

    token = secrets.token_urlsafe(36)
    updated = dict(config)
    updated["dashboard_token"] = token
    save_config(updated)
    config.clear()
    config.update(updated)
    return token


class _DashboardActor:
    """Audit identity used by dashboard actions where no Discord user logs in.

    The id is a real Discord user (the server owner, by convention), so audit
    rows stay resolvable, while ``mention`` can carry a plain-English label for
    the messages that render it — a notice that says it was sent by
    "the dashboard" is more useful than one that names the server owner.
    """

    def __init__(self, user_id: int, label: Optional[str] = None) -> None:
        self.id = int(user_id)
        self.label = label
        self.mention = label or f"<@{self.id}>"
        self.display_name = label or f"<@{self.id}>"


class DashboardServer:
    """aiohttp dashboard that shares the bot's event loop and domain logic."""

    def __init__(
        self,
        bot: Any,
        *,
        save_config: Callable[[dict], None],
        db_fetchall: Callable[..., list],
        db_fetchone: Callable[..., Any],
        db_execute: Callable[..., None],
        archive_punishment: Callable[..., None],
        get_guild_config: Callable[..., Optional[dict]],
        get_staff_channel_id: Callable[..., Optional[int]],
        should_dm_user: Callable[..., bool],
        is_protected_member: Callable[..., Optional[str]],
        parse_duration: Callable[[str], Optional[int]],
        format_duration: Callable[[int], str],
    ) -> None:
        self.bot = bot
        self._save_config = save_config
        self._db_fetchall = db_fetchall
        self._db_fetchone = db_fetchone
        self._db_execute = db_execute
        self._archive_punishment = archive_punishment
        self._get_guild_config = get_guild_config
        self._get_staff_channel_id = get_staff_channel_id
        self._should_dm_user = should_dm_user
        self._is_protected_member = is_protected_member
        self._parse_duration = parse_duration
        self._format_duration = format_duration
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._sessions: dict[str, dict[str, Any]] = {}
        self._login_failures: dict[str, tuple[int, float]] = {}
        self._token: Optional[str] = None
        self._host = "127.0.0.1"
        self._port = 8765
        self._secure_cookie = False
        self._tls_context: Optional[ssl.SSLContext] = None
        self._trusted_proxies: list[str] = []
        self._public_url = ""
        self._warnings: list[str] = []
        self._settings_applied = False

    @property
    def running(self) -> bool:
        return self._runner is not None

    def _apply_settings(self, config: dict) -> None:
        """Resolve the network settings from config + environment.

        Split out of :meth:`start` so the configuration can be validated - and
        tested - without binding a socket.
        """
        self._host = os.environ.get(
            "SENTINEL_DASHBOARD_HOST",
            str(config.get("dashboard_host") or "127.0.0.1"),
        )
        raw_port = os.environ.get(
            "SENTINEL_DASHBOARD_PORT",
            str(config.get("dashboard_port", 8765)),
        )
        try:
            self._port = int(raw_port)
        except (TypeError, ValueError) as exc:
            raise ValueError("dashboard_port must be an integer") from exc
        if not 0 < self._port < 65536:
            raise ValueError("dashboard_port must be between 1 and 65535")

        self._public_url = str(
            os.environ.get("SENTINEL_DASHBOARD_PUBLIC_URL")
            or config.get("dashboard_public_url")
            or ""
        ).strip()

        raw_proxies = config.get("dashboard_trusted_proxies", [])
        if isinstance(raw_proxies, str):
            raw_proxies = [raw_proxies]
        self._trusted_proxies = [
            str(item).strip() for item in raw_proxies if str(item).strip()
        ] if isinstance(raw_proxies, list) else []

        cert = str(
            os.environ.get("SENTINEL_DASHBOARD_TLS_CERT")
            or config.get("dashboard_tls_cert")
            or ""
        ).strip()
        key = str(
            os.environ.get("SENTINEL_DASHBOARD_TLS_KEY")
            or config.get("dashboard_tls_key")
            or ""
        ).strip()
        if bool(cert) != bool(key):
            raise ValueError(
                "dashboard_tls_cert and dashboard_tls_key must be set together "
                "(both the certificate and its private key)."
            )
        self._tls_context = self._load_tls_context(cert, key) if cert else None
        # A TLS listener always gets a secure session cookie; anything else
        # would send the dashboard key back over plain HTTP.
        self._secure_cookie = bool(self._tls_context) or bool(
            config.get("dashboard_secure_cookie", False)
        )
        self._warnings = self.remote_access_warnings()
        self._settings_applied = True

    def _load_tls_context(self, cert_path: str, key_path: str) -> ssl.SSLContext:
        """Load the HTTPS certificate, refusing to start on a bad one.

        A TLS listener that silently fails to load, or that loads one half of a
        key pair, is worse than no TLS: the dashboard would either not start or
        serve a certificate no browser trusts.
        """
        for path in (cert_path, key_path):
            if not Path(path).is_file():
                raise ValueError(
                    f"dashboard TLS file not found: {path} "
                    "(set dashboard_tls_cert/dashboard_tls_key to the fullchain "
                    "certificate and its private key)."
                )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        try:
            context.load_cert_chain(certfile=cert_path, keyfile=key_path)
        except (ssl.SSLError, OSError) as exc:
            raise ValueError(f"could not load the dashboard TLS certificate: {exc}") from exc
        try:
            context.minimum_version = ssl.TLSVersion.TLSv1_2
        except (AttributeError, ValueError):  # pragma: no cover - older OpenSSL
            pass
        return context

    @property
    def scheme(self) -> str:
        """What *this* listener speaks."""
        return "https" if self._tls_context is not None else "http"

    @property
    def browser_scheme(self) -> str:
        """What the browser will use, which is what cookie rules apply to.

        TLS may be terminated in a reverse proxy rather than here, in which
        case `dashboard_public_url` is the only thing that says so - the
        browser's connection (and therefore the session cookie) is HTTPS even
        though this listener answers plain HTTP on the loopback interface.
        """
        if self._tls_context is not None:
            return "https"
        if self._public_url.lower().startswith("https://"):
            return "https"
        return "http"

    def urls(self) -> list[str]:
        """Every URL this dashboard can be opened at, the public one first."""
        urls: list[str] = []
        if self._public_url:
            urls.append(self._public_url.rstrip("/") + "/")
        urls.append(f"{self.scheme}://{self._host}:{self._port}/")
        return urls

    def remote_access_warnings(self) -> list[str]:
        """The ways this listener would fail for a remote visitor.

        Each entry is the *fix*, not just the symptom, because the symptom
        (a connection that never arrives, a login that bounces, a 400 from the
        allowlist) is what a user sees and none of them say what to change.
        """
        warnings: list[str] = []
        exposed = not _is_loopback_host(self._host)
        browser_is_secure = self.browser_scheme == "https"

        if self._secure_cookie and not browser_is_secure:
            warnings.append(
                "dashboard_secure_cookie is on, but the browser reaches this "
                "dashboard over plain HTTP, so it will refuse to store the "
                "session cookie and every login will appear to succeed and then "
                "bounce back to the login screen. Serve HTTPS - terminate it in a "
                "reverse proxy and set dashboard_public_url to the https:// URL, "
                "or serve it here with dashboard_tls_cert + dashboard_tls_key - "
                "or set dashboard_secure_cookie to false."
            )
        if exposed and not browser_is_secure:
            warnings.append(
                f"The dashboard is listening on {self._host} and is reached over "
                "plain HTTP: the login key crosses the network in clear text. "
                "Put it behind HTTPS (a reverse proxy, or "
                "dashboard_tls_cert/dashboard_tls_key) and firewall the port."
            )
        if exposed and not self._allowed_hosts_configured():
            warnings.append(
                f"dashboard_host is {self._host} (reachable off this machine) but "
                "'dashboard_allowed_hosts' is empty, so any Host header that is "
                "not a bare IP is answered with 400 Unrecognized Host header. "
                "Add the public name, e.g. "
                '"dashboard_allowed_hosts": ["yourname.duckdns.org"].'
            )
        if self._public_url and not self._public_url.lower().startswith(("http://", "https://")):
            warnings.append(
                f"dashboard_public_url should be a full URL including the scheme; "
                f"got {self._public_url!r}."
            )
        return warnings

    def _allowed_hosts_configured(self) -> bool:
        configured = self.bot.config.get("dashboard_allowed_hosts", [])
        return bool(configured) if isinstance(configured, list) else bool(configured)

    async def start(self) -> bool:
        """Start the web server if enabled. Returns True if it is listening."""
        if self.running:
            return True
        config = self.bot.config
        if not config.get("dashboard_enabled", True):
            logger.info("Web dashboard disabled by config.")
            return False

        self._apply_settings(config)
        self._token = ensure_dashboard_token(config, self._save_config)

        app = self._build_app()
        runner = web.AppRunner(app, access_log=None, shutdown_timeout=5)
        await runner.setup()
        site = web.TCPSite(
            runner,
            host=self._host,
            port=self._port,
            ssl_context=self._tls_context,
        )
        try:
            await site.start()
        except Exception:
            await runner.cleanup()
            raise
        self._runner = runner
        self._site = site
        logger.info(
            "Admin dashboard listening at %s://%s:%d/%s.",
            self.scheme,
            self._host,
            self._port,
            " (TLS)" if self._tls_context else "",
        )
        if self._public_url:
            logger.info("Dashboard public URL: %s", self._public_url)
        if self._trusted_proxies:
            logger.info(
                "Trusting X-Forwarded-For from: %s",
                ", ".join(self._trusted_proxies),
            )
        for warning in self._warnings:
            logger.warning("Remote access: %s", warning)
        logger.info(
            "Dashboard login key: run 'sentinel --dashboard-token' on the bot host "
            "(from source: 'python3 bot.py --dashboard-token'); it is also saved as "
            "'dashboard_token' in the bot's config.json."
        )
        return True

    async def close(self) -> None:
        if self._runner is not None:
            runner, self._runner = self._runner, None
            self._site = None
            await runner.cleanup()
        self._sessions.clear()

    def _build_app(self) -> web.Application:
        server = self
        if not self._settings_applied:
            # Every path that serves requests goes through the same resolution,
            # so an entry point that forgets _apply_settings cannot silently
            # serve the defaults (plain HTTP, no allowlist) instead of config.
            self._apply_settings(self.bot.config)

        @web.middleware
        async def auth_and_security(request: web.Request, handler):
            if not server._allowed_host(request.host):
                response = web.json_response(
                    {"error": "Unrecognized Host header."}, status=400
                )
                return server._security_headers(response)
            if request.path.startswith("/api/") and request.path != "/api/login":
                session = server._session_for_request(request)
                if session is None:
                    response = web.json_response(
                        {"error": "Authentication required."}, status=401
                    )
                    return server._security_headers(response)
                if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
                    csrf = request.headers.get("X-CSRF-Token", "")
                    if not hmac.compare_digest(csrf, session["csrf"]):
                        response = web.json_response(
                            {"error": "Invalid CSRF token."}, status=403
                        )
                        return server._security_headers(response)

            try:
                response = await handler(request)
            except web.HTTPException as exc:
                if request.path.startswith("/api/"):
                    response = web.json_response(
                        {"error": exc.text or exc.reason}, status=exc.status
                    )
                else:
                    response = exc
            except Exception:
                logger.exception("Unhandled dashboard request %s %s", request.method, request.path)
                if request.path.startswith("/api/"):
                    response = web.json_response(
                        {"error": "The dashboard could not complete that request."},
                        status=500,
                    )
                else:
                    response = web.Response(text="Dashboard error", status=500)
            return server._security_headers(response)

        app = web.Application(
            middlewares=[auth_and_security],
            client_max_size=64 * 1024,
        )
        app.add_routes(
            [
                web.get("/", self.index),
                web.get("/api/session", self.session_status),
                web.post("/api/login", self.login),
                web.post("/api/logout", self.logout),
                web.get("/api/overview", self.overview),
                web.get("/api/guilds/{guild_id}", self.guild_detail),
                web.get("/api/guilds/{guild_id}/members", self.search_members),
                web.post("/api/guilds/{guild_id}/settings", self.save_guild_settings),
                web.get("/api/guilds/{guild_id}/punishments", self.list_punishments),
                web.post("/api/guilds/{guild_id}/punishments", self.apply_punishment),
                web.post(
                    "/api/guilds/{guild_id}/punishments/{user_id}/pardon",
                    self.pardon_member,
                ),
                web.get("/api/guilds/{guild_id}/warnings", self.list_warnings),
                web.post("/api/guilds/{guild_id}/warnings", self.warn_member),
                web.delete(
                    "/api/guilds/{guild_id}/warnings/{user_id}",
                    self.clear_warnings,
                ),
                web.get("/api/guilds/{guild_id}/history", self.history),
                web.get(
                    "/api/guilds/{guild_id}/applications", self.applications_status
                ),
                web.post(
                    "/api/guilds/{guild_id}/applications/forms",
                    self.save_application_form,
                ),
                web.delete(
                    "/api/guilds/{guild_id}/applications/forms/{form_id}",
                    self.delete_application_form,
                ),
                web.post(
                    "/api/guilds/{guild_id}/applications/forms/{form_id}/panel",
                    self.publish_application_panel,
                ),
                web.post(
                    "/api/guilds/{guild_id}/applications/{application_id}/decision",
                    self.decide_application,
                ),
                web.post("/api/guilds/{guild_id}/sync", self.sync_commands),
            ]
        )
        return app

    @staticmethod
    def _security_headers(response: web.StreamResponse) -> web.StreamResponse:
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; img-src 'self' data: https://cdn.discordapp.com; "
            "connect-src 'self'; base-uri 'none'"
        )
        if response.headers.get("Content-Type", "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def _allowed_host(self, raw_host: str) -> bool:
        """Reject DNS-rebinding Host headers; remote names need an explicit allowlist.

        A DNS-rebinding attack works by pointing an attacker-controlled name at
        this server's address, so the browser sends that name in the Host
        header with the victim's cookies attached. Checking Host against a list
        is what stops it - which is why a public name such as
        `yourname.duckdns.org` has to be listed in `dashboard_allowed_hosts`
        (or be the host of `dashboard_public_url`) before the dashboard will
        answer for it.
        """
        host = self._normalize_host(raw_host)

        # 'localhost' / 127.0.0.1 / ::1 are not reachable by an attacker's page
        # (a page cannot forge the Host header), so they are always allowed.
        if host in {"localhost", "127.0.0.1", "::1"}:
            return True

        allowed = set()
        configured = self.bot.config.get("dashboard_allowed_hosts", [])
        if isinstance(configured, str):
            configured = [configured]
        if isinstance(configured, (list, tuple, set)):
            allowed = {self._normalize_host(str(item)) for item in configured if str(item).strip()}
        if self._public_url:
            allowed.add(self._normalize_host(urlsplit(self._public_url).netloc))
        if allowed:
            return host in allowed

        if self._host not in {"0.0.0.0", "::", ""}:
            return host == self._normalize_host(self._host)
        return False

    def _client_ip(self, request: web.Request) -> str:
        """The visitor's address, believing X-Forwarded-For only when trusted.

        Behind a reverse proxy every request arrives from the proxy, so without
        this the login throttle would treat the whole internet as one client
        (and lock everyone out after five bad guesses), while the logs would
        name the proxy instead of the culprit. The header is attacker-supplied
        when the request does *not* come from a trusted proxy, so it is ignored
        there.
        """
        peer = request.remote or "unknown"
        if not self._trusted_proxies or not self._is_trusted_proxy(peer):
            return peer
        forwarded = request.headers.get("X-Forwarded-For", "")
        hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
        # Walk from the nearest hop outwards, skipping our own proxies: the
        # first address we did not add ourselves is the client.
        for hop in reversed(hops):
            if not self._is_trusted_proxy(hop):
                return hop
        return hops[0] if hops else peer

    def _is_trusted_proxy(self, value: str) -> bool:
        """Is this address allowed to tell us who the real client is?

        Entries may be addresses (`127.0.0.1`) or CIDR ranges (`172.18.0.0/16`
        for a Docker network), as ``ipaddress`` understands them.
        """
        candidate = self._normalize_host(value)
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            # Not an IP: fall back to an exact, case-insensitive name match.
            return candidate in {entry.strip().lower() for entry in self._trusted_proxies}
        for entry in self._trusted_proxies:
            try:
                network = ipaddress.ip_network(entry.strip(), strict=False)
            except ValueError:
                continue
            if address.version == network.version and address in network:
                return True
        return False

    @staticmethod
    def _normalize_host(raw_host: str) -> str:
        value = str(raw_host or "").strip().lower()
        if value.startswith("["):
            return (urlsplit("//" + value).hostname or "").rstrip(".")
        if value.count(":") > 1:
            return value.rstrip(".")  # bare IPv6 literal
        if ":" in value:
            value = value.split(":", 1)[0]
        return value.rstrip(".")

    def _session_for_request(self, request: web.Request) -> Optional[dict[str, Any]]:
        session_id = request.cookies.get(SESSION_COOKIE)
        if not session_id:
            return None
        session = self._sessions.get(session_id)
        if session is None:
            return None
        now = time.time()
        if session["expires_at"] <= now:
            self._sessions.pop(session_id, None)
            return None
        session["expires_at"] = now + SESSION_TTL_SECONDS
        return session

    async def index(self, _request: web.Request) -> web.Response:
        resource = paths.resource_path("dashboard.html")
        if resource is None:
            return web.Response(text="Dashboard UI is missing.", status=500)
        try:
            html = resource.read_text(encoding="utf-8")
        except OSError:
            logger.exception("Could not read dashboard.html from %s", resource)
            return web.Response(text="Dashboard UI is unavailable.", status=500)
        return web.Response(text=html, content_type="text/html", charset="utf-8")

    async def login(self, request: web.Request) -> web.Response:
        peer = self._client_ip(request)
        now = time.monotonic()
        count, reset_at = self._login_failures.get(peer, (0, now + LOGIN_WINDOW_SECONDS))
        if reset_at <= now:
            count, reset_at = 0, now + LOGIN_WINDOW_SECONDS
        if count >= MAX_LOGIN_FAILURES:
            return web.json_response(
                {"error": "Too many failed attempts. Wait a few minutes and try again."},
                status=429,
            )

        try:
            data = await request.json()
        except (ValueError, TypeError):
            data = {}
        submitted = data.get("token", "") if isinstance(data, dict) else ""
        expected = self._token or ""
        if not isinstance(submitted, str) or not expected or not hmac.compare_digest(submitted, expected):
            self._login_failures[peer] = (count + 1, reset_at)
            logger.warning("Rejected dashboard login from %s", peer)
            return web.json_response({"error": "Invalid dashboard key."}, status=401)

        self._login_failures.pop(peer, None)
        session_id = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        self._sessions[session_id] = {
            "csrf": csrf,
            "expires_at": time.time() + SESSION_TTL_SECONDS,
        }
        response = web.json_response({"authenticated": True, "csrfToken": csrf})
        response.set_cookie(
            SESSION_COOKIE,
            session_id,
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            secure=self._secure_cookie,
            samesite="Strict",
            path="/",
        )
        logger.info("Dashboard login accepted from %s", peer)
        return response

    async def session_status(self, request: web.Request) -> web.Response:
        session = self._session_for_request(request)
        if session is None:  # normally intercepted by the auth middleware
            raise web.HTTPUnauthorized(text="Authentication required.")
        return web.json_response({"authenticated": True, "csrfToken": session["csrf"]})

    async def logout(self, request: web.Request) -> web.Response:
        session_id = request.cookies.get(SESSION_COOKIE)
        if session_id:
            self._sessions.pop(session_id, None)
        response = web.json_response({"authenticated": False})
        response.del_cookie(SESSION_COOKIE, path="/")
        return response

    async def overview(self, _request: web.Request) -> web.Response:
        guilds = list(self.bot.guilds)
        counts = {
            int(row["guild_id"]): int(row["count"])
            for row in self._db_fetchall(
                "SELECT guild_id, COUNT(*) AS count FROM punishments GROUP BY guild_id"
            )
        }
        payload = [
            {
                "id": _snowflake(guild.id),
                "name": guild.name,
                "icon": guild.icon.url if guild.icon else None,
                "memberCount": guild.member_count,
                "activePunishments": counts.get(guild.id, 0),
            }
            for guild in guilds
        ]
        active_total = sum(counts.values())
        return web.json_response(
            {
                "bot": {
                    "ready": self.bot.is_ready(),
                    "name": str(self.bot.user) if self.bot.user else "Connecting",
                    "avatar": self.bot.user.display_avatar.url if self.bot.user else None,
                    "guildCount": len(guilds),
                    "activePunishments": active_total,
                },
                "guilds": payload,
            }
        )

    async def guild_detail(self, request: web.Request) -> web.Response:
        guild = self._guild_from_request(request)
        config = self._get_guild_config(self.bot.config, guild.id) or {}
        roles = [
            {
                "id": _snowflake(role.id),
                "name": role.name,
                "position": role.position,
                "color": role.color.value,
                "managed": role.managed,
            }
            for role in sorted(guild.roles, key=lambda item: item.position, reverse=True)
            if not role.is_default()
        ]
        channels = [
            {
                "id": _snowflake(channel.id),
                "name": channel.name,
                "category": channel.category.name if channel.category else None,
            }
            for channel in guild.text_channels
        ]
        # Discord's *category* channels. Kept separate from `channels`: a
        # category is a container, not a destination anything can be posted to.
        categories = [
            {"id": _snowflake(channel.id), "name": channel.name}
            for channel in guild.categories
        ]
        active_count = self._db_fetchone(
            "SELECT COUNT(*) AS count FROM punishments WHERE guild_id = ?",
            (guild.id,),
        )
        warning_count = self._db_fetchone(
            "SELECT COUNT(*) AS count FROM warnings WHERE guild_id = ?",
            (guild.id,),
        )
        return web.json_response(
            {
                "guild": {
                    "id": _snowflake(guild.id),
                    "name": guild.name,
                    "icon": guild.icon.url if guild.icon else None,
                    "ownerId": _snowflake(guild.owner_id),
                    "memberCount": guild.member_count,
                },
                "settings": {
                    "punishRoleId": _snowflake(config.get("punish_role_id")),
                    "postRoleId": _snowflake(config.get("post_role_id")),
                    "staffRoleId": _snowflake(config.get("staff_role_id")),
                    "staffChannelId": _snowflake(
                        self._get_staff_channel_id(self.bot.config, guild.id)
                    ),
                    "dmUser": self._should_dm_user(self.bot.config, guild.id),
                },
                "roles": roles,
                "channels": channels,
                "categories": categories,
                "activePunishments": int(active_count["count"]) if active_count else 0,
                "warningCount": int(warning_count["count"]) if warning_count else 0,
            }
        )

    async def search_members(self, request: web.Request) -> web.Response:
        guild = self._guild_from_request(request)
        query = request.query.get("q", "").strip()
        if len(query) < 2:
            raise web.HTTPBadRequest(text="Enter at least two characters to search members.")
        try:
            if query.isdigit():
                member = await self._resolve_member(guild, int(query))
                members = [member] if member is not None else []
            else:
                members = await guild.query_members(query, limit=25, cache=False)
        except discord.DiscordException as exc:
            logger.warning("Member search failed for guild %s: %s", guild.id, exc)
            members = [
                member
                for member in guild.members
                if query.casefold() in member.display_name.casefold()
                or query.casefold() in member.name.casefold()
            ][:25]
        return web.json_response(
            {
                "members": [
                    {
                        "id": _snowflake(member.id),
                        "name": member.display_name,
                        "username": str(member),
                        "avatar": member.display_avatar.url,
                    }
                    for member in members
                ]
            }
        )

    async def list_punishments(self, request: web.Request) -> web.Response:
        guild = self._guild_from_request(request)
        rows = self._db_fetchall(
            "SELECT id, guild_id, user_id, moderator_id, reason, started_at, expires_at, stage "
            "FROM punishments WHERE guild_id = ? ORDER BY expires_at",
            (guild.id,),
        )
        return web.json_response(
            {
                "punishments": [
                    _json_safe_ids(self._punishment_payload(guild, dict(row)))
                    for row in rows
                ]
            }
        )

    async def history(self, request: web.Request) -> web.Response:
        guild = self._guild_from_request(request)
        rows = self._db_fetchall(
            "SELECT id, guild_id, user_id, moderator_id, reason, started_at, "
            "duration_seconds, ended_at, ended_reason FROM punishment_history "
            "WHERE guild_id = ? ORDER BY started_at DESC LIMIT 100",
            (guild.id,),
        )
        history = []
        for row in rows:
            item = dict(row)
            member = guild.get_member(item["user_id"])
            item["memberName"] = member.display_name if member else f"User {item['user_id']}"
            item["durationLabel"] = self._format_duration(item["duration_seconds"])
            history.append(_json_safe_ids(item))
        return web.json_response({"history": history})

    async def save_guild_settings(self, request: web.Request) -> web.Response:
        guild = self._guild_from_request(request)
        data = await self._json_body(request)
        punish_role = self._role_from_body(guild, data.get("punishRoleId"), "punish role")
        post_role = self._role_from_body(guild, data.get("postRoleId"), "post-punishment role")
        staff_role = self._role_from_body(
            guild, data.get("staffRoleId"), "staff role", optional=True
        )
        if punish_role.id == post_role.id:
            raise web.HTTPBadRequest(text="Punish and post-punishment roles must be different.")
        if staff_role and staff_role.id in {punish_role.id, post_role.id}:
            raise web.HTTPBadRequest(text="The staff role must be different from punishment roles.")

        bot_member = guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_roles:
            raise web.HTTPForbidden(text="The bot needs Manage Roles in this server.")
        for role, label in ((punish_role, "Punish"), (post_role, "Post-punishment")):
            if role.managed or role >= bot_member.top_role:
                raise web.HTTPBadRequest(text=f"Move the bot role above the {label.lower()} role.")
            if any(getattr(role.permissions, name, False) for name in PRIVILEGED_ROLE_PERMISSIONS):
                raise web.HTTPBadRequest(
                    text=f"The {label.lower()} role cannot grant moderation or server-management permissions."
                )

        staff_channel_id = self._optional_int(data.get("staffChannelId"), "staff channel")
        staff_channel = guild.get_channel(staff_channel_id) if staff_channel_id else None
        if staff_channel_id and not isinstance(staff_channel, discord.TextChannel):
            raise web.HTTPBadRequest(text="Choose a text channel from this server.")
        if staff_channel is not None:
            permissions = staff_channel.permissions_for(bot_member)
            if not permissions.view_channel or not permissions.send_messages or not permissions.embed_links:
                raise web.HTTPBadRequest(
                    text="The bot needs View Channel, Send Messages, and Embed Links in the staff channel."
                )

        dm_user = data.get("dmUser")
        if not isinstance(dm_user, bool):
            raise web.HTTPBadRequest(text="dmUser must be true or false.")

        guilds = self.bot.config.get("guilds", {})
        guilds = dict(guilds) if isinstance(guilds, dict) else {}
        entry = guilds.get(str(guild.id), {})
        entry = {
            key: value
            for key, value in entry.items()
            if key != "normal_role_id"
        } if isinstance(entry, dict) else {}
        entry.update(
            {
                "punish_role_id": punish_role.id,
                "post_role_id": post_role.id,
                "staff_role_id": staff_role.id if staff_role else None,
                "staff_channel_id": staff_channel_id,
                "dm_user": dm_user,
            }
        )
        guilds[str(guild.id)] = entry
        updated = dict(self.bot.config)
        updated["guilds"] = guilds
        if self._is_configured_primary_guild(guild.id):
            # Keep installer-managed punish role fields in sync. Optional
            # staff/notification values stay per-guild so this form doesn't
            # unexpectedly alter another connected server.
            updated["punish_role_id"] = punish_role.id
            updated["post_role_id"] = post_role.id
        self._persist_config(updated)
        logger.info("Dashboard saved server settings for guild %s", guild.id)
        return web.json_response({"saved": True})

    async def apply_punishment(self, request: web.Request) -> web.Response:
        guild = self._guild_from_request(request)
        data = await self._json_body(request)
        user_id = self._required_int(data.get("userId"), "user ID")
        member = await self._resolve_member(guild, user_id)
        if member is None:
            raise web.HTTPNotFound(text="That user is not in this server.")
        reason = str(data.get("reason", "")).strip() or "No reason provided."
        if len(reason) > MAX_REASON_LENGTH:
            raise web.HTTPBadRequest(text=f"Reason must be {MAX_REASON_LENGTH} characters or fewer.")
        raw_duration = str(data.get("duration", "")).strip()
        seconds = self._parse_duration(raw_duration)
        if seconds is None or seconds < 5:
            raise web.HTTPBadRequest(text="Invalid duration. Use a value such as 30m, 2h, or 1d.")
        if seconds > 30 * 24 * 60 * 60:
            raise web.HTTPBadRequest(text="Punishment duration cannot exceed 30 days.")

        config = self._get_guild_config(self.bot.config, guild.id)
        if not config or not config.get("punish_role_id") or not config.get("post_role_id"):
            raise web.HTTPBadRequest(text="Configure this server's punishment roles first.")
        protected = self._is_protected_member(member, self.bot.config, guild=guild)
        if protected:
            raise web.HTTPForbidden(text=protected)
        duplicate = self._db_fetchone(
            "SELECT id FROM punishments WHERE guild_id = ? AND user_id = ? LIMIT 1",
            (guild.id, member.id),
        )
        if duplicate:
            raise web.HTTPConflict(text="That member already has an active punishment.")

        # A local dashboard credential is not a Discord identity. Attribute its
        # moderation record to the server owner and mark the reason as dashboard-originated.
        actor = _DashboardActor(guild.owner_id)
        full_reason = f"[Dashboard] {reason}"
        result = await self.bot.start_punishment(
            guild=guild,
            member=member,
            moderator=actor,
            duration_seconds=seconds,
            reason=full_reason,
            cfg=config,
        )
        if result is None:
            raise web.HTTPBadRequest(text="Configured punishment roles are missing from Discord.")
        if "error" in result:
            raise web.HTTPBadRequest(text=result["error"])

        started_at = result["started_at"]
        expires_at = result["expires_at"]
        staff_embed = self.bot._build_punish_staff_embed(
            member=member,
            moderator=actor,
            duration_seconds=seconds,
            reason=full_reason,
            started_at=started_at,
            expires_at=expires_at,
        )
        await self.bot._send_staff_embed(guild, staff_embed)
        if self._should_dm_user(self.bot.config, guild.id):
            dm = self.bot._build_punish_dm_embed(
                guild_name=guild.name,
                moderator_name="Sentinel dashboard",
                duration_seconds=seconds,
                reason=full_reason,
                started_at=started_at,
                expires_at=expires_at,
            )
            await self.bot._dm_embed(member, dm)
        logger.warning(
            "Dashboard applied punishment guild=%s user=%s peer=%s",
            guild.id,
            member.id,
            request.remote or "unknown",
        )
        return web.json_response(
            {
                "applied": True,
                "userId": _snowflake(member.id),
                "expiresAt": expires_at.isoformat(),
            },
            status=201,
        )

    async def pardon_member(self, request: web.Request) -> web.Response:
        guild = self._guild_from_request(request)
        user_id = self._required_int(request.match_info.get("user_id"), "user ID")
        member = await self._resolve_member(guild, user_id)
        if member is None:
            raise web.HTTPNotFound(text="That user is no longer in this server.")
        record = self._db_fetchone(
            "SELECT * FROM punishments WHERE guild_id = ? AND user_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (guild.id, member.id),
        )
        if record is None:
            raise web.HTTPNotFound(text="No active punishment was found for that member.")

        punish_role = guild.get_role(record["punish_role_id"])
        post_role = guild.get_role(record["post_role_id"])
        try:
            if punish_role and punish_role in member.roles:
                await member.remove_roles(punish_role, reason="Pardoned from the admin dashboard")
            if post_role and post_role in member.roles:
                await member.remove_roles(post_role, reason="Pardoned from the admin dashboard")
        except discord.Forbidden:
            raise web.HTTPForbidden(text="The bot cannot change that member's roles.")
        except discord.HTTPException as exc:
            logger.warning("Dashboard pardon failed in guild %s: %s", guild.id, exc)
            raise web.HTTPBadGateway(text="Discord could not update the member's roles.")

        actor = _DashboardActor(guild.owner_id)
        self._db_execute("DELETE FROM punishments WHERE id = ?", (record["id"],))
        self._archive_punishment(dict(record), ended_reason="pardoned via dashboard")
        staff_embed = self.bot._build_pardon_staff_embed(
            member=member,
            moderator=actor,
            reason="Pardoned via admin dashboard",
            was_active=True,
        )
        await self.bot._send_staff_embed(guild, staff_embed)
        if self._should_dm_user(self.bot.config, guild.id):
            dm = self.bot._build_pardon_dm_embed(
                guild_name=guild.name,
                moderator_name="Sentinel dashboard",
            )
            await self.bot._dm_embed(member, dm)
        logger.warning(
            "Dashboard pardoned member guild=%s user=%s peer=%s",
            guild.id,
            member.id,
            request.remote or "unknown",
        )
        return web.json_response({"pardoned": True, "userId": _snowflake(member.id)})

    # ---- Warnings ------------------------------------------------------- #
    # Warnings never change roles and never expire; these endpoints mirror
    # /manage warn and /manage warnings so both surfaces share one record.
    async def list_warnings(self, request: web.Request) -> web.Response:
        guild = self._guild_from_request(request)
        rows = self._db_fetchall(
            "SELECT id, guild_id, user_id, moderator_id, reason, created_at "
            "FROM warnings WHERE guild_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 100",
            (guild.id,),
        )
        totals: dict[int, int] = {}
        for row in self._db_fetchall(
            "SELECT user_id, COUNT(*) AS count FROM warnings "
            "WHERE guild_id = ? GROUP BY user_id",
            (guild.id,),
        ):
            totals[int(row["user_id"])] = int(row["count"])

        warnings = []
        for row in rows:
            item = dict(row)
            member = guild.get_member(item["user_id"])
            item["memberName"] = (
                member.display_name if member else f"User {item['user_id']}"
            )
            item["memberAvatar"] = member.display_avatar.url if member else None
            item["totalForMember"] = totals.get(int(item["user_id"]), 0)
            warnings.append(_json_safe_ids(item))
        return web.json_response(
            {"warnings": warnings, "total": sum(totals.values())}
        )

    async def warn_member(self, request: web.Request) -> web.Response:
        guild = self._guild_from_request(request)
        data = await self._json_body(request)
        user_id = self._required_int(data.get("userId"), "user ID")
        member = await self._resolve_member(guild, user_id)
        if member is None:
            raise web.HTTPNotFound(text="That user is not in this server.")
        reason = str(data.get("reason", "")).strip()
        if not reason:
            raise web.HTTPBadRequest(text="A reason is required for a warning.")
        if len(reason) > MAX_REASON_LENGTH:
            raise web.HTTPBadRequest(
                text=f"Reason must be {MAX_REASON_LENGTH} characters or fewer."
            )
        protected = self._is_protected_member(member, self.bot.config, guild=guild)
        if protected:
            raise web.HTTPForbidden(text=protected)

        # Same audit convention as the punishment endpoints: the dashboard has
        # no Discord identity, so the action is attributed to the owner and
        # marked as dashboard-originated in the stored reason.
        actor = _DashboardActor(guild.owner_id)
        created_at = datetime.now(timezone.utc)
        full_reason = f"[Dashboard] {reason}"
        try:
            self._db_execute(
                "INSERT INTO warnings "
                "(guild_id, user_id, moderator_id, reason, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (guild.id, member.id, actor.id, full_reason, created_at.isoformat()),
            )
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            logger.exception("Dashboard could not record a warning: %s", exc)
            raise web.HTTPInternalServerError(
                text="The warning could not be saved."
            ) from exc

        row = self._db_fetchone(
            "SELECT COUNT(*) AS count FROM warnings "
            "WHERE guild_id = ? AND user_id = ?",
            (guild.id, member.id),
        )
        total = int(row["count"]) if row is not None else 1

        staff_embed = self.bot._build_warn_staff_embed(
            member=member,
            moderator=actor,
            reason=full_reason,
            created_at=created_at,
            total=total,
        )
        await self.bot._send_staff_embed(guild, staff_embed)
        if self._should_dm_user(self.bot.config, guild.id):
            dm_embed = self.bot._build_warn_dm_embed(
                guild_name=guild.name,
                moderator_name="Sentinel dashboard",
                reason=full_reason,
                total=total,
                created_at=created_at,
            )
            await self.bot._dm_embed(member, dm_embed)
        logger.warning(
            "Dashboard warned member guild=%s user=%s peer=%s",
            guild.id,
            member.id,
            request.remote or "unknown",
        )
        return web.json_response(
            {
                "warned": True,
                "userId": _snowflake(member.id),
                "totalForMember": total,
            },
            status=201,
        )

    async def clear_warnings(self, request: web.Request) -> web.Response:
        guild = self._guild_from_request(request)
        user_id = self._required_int(request.match_info.get("user_id"), "user ID")
        member = await self._resolve_member(guild, user_id)
        if member is None:
            raise web.HTTPNotFound(text="That user is no longer in this server.")
        row = self._db_fetchone(
            "SELECT COUNT(*) AS count FROM warnings "
            "WHERE guild_id = ? AND user_id = ?",
            (guild.id, member.id),
        )
        removed = int(row["count"]) if row is not None else 0
        if removed == 0:
            raise web.HTTPNotFound(text="That member has no warnings on record.")
        self._db_execute(
            "DELETE FROM warnings WHERE guild_id = ? AND user_id = ?",
            (guild.id, member.id),
        )
        actor = _DashboardActor(guild.owner_id)
        staff_embed = self.bot._build_warn_clear_staff_embed(
            member=member,
            moderator=actor,
            removed=removed,
        )
        await self.bot._send_staff_embed(guild, staff_embed)
        logger.warning(
            "Dashboard cleared %s warning(s) guild=%s user=%s peer=%s",
            removed,
            guild.id,
            member.id,
            request.remote or "unknown",
        )
        return web.json_response(
            {"cleared": removed, "userId": _snowflake(member.id)}
        )

    # ---- Applications -------------------------------------------------- #
    async def applications_status(self, request: web.Request) -> web.Response:
        guild = self._guild_from_request(request)
        wanted = (request.query.get("status") or "pending").strip().lower()
        forms = applications.get_guild_forms(self.bot.config, guild.id)
        form_id = (request.query.get("formId") or "").strip() or None
        records = applications.list_applications(
            guild.id,
            status=wanted if wanted in applications.STATUSES else None,
            form_id=form_id,
            limit=MAX_DASHBOARD_ROWS,
        )
        return web.json_response(
            {
                "forms": [_application_form_payload(form, guild) for form in forms],
                "applications": [
                    _application_payload(record, guild) for record in records
                ],
                "counts": {
                    "pending": applications.count_pending(guild.id),
                    "shown": len(records),
                },
            }
        )

    async def save_application_form(self, request: web.Request) -> web.Response:
        """Create a form (no ``formId``) or update one (with ``formId``)."""
        guild = self._guild_from_request(request)
        data = await self._json_body(request)
        form_id = str(data.get("formId") or "").strip()
        existing = applications.find_form(self.bot.config, guild.id, form_id) if form_id else None
        if form_id and existing is None:
            raise web.HTTPNotFound(text="That application form no longer exists.")

        name = str(data.get("name") or "").strip()
        if not name:
            raise web.HTTPBadRequest(text="Give the form a name.")
        if len(name) > applications.MAX_FORM_NAME_LENGTH:
            raise web.HTTPBadRequest(
                text=f"Form names are limited to {applications.MAX_FORM_NAME_LENGTH} characters."
            )

        review_channel = self._postable_text_channel(
            guild,
            self._required_int(data.get("reviewChannelId"), "review channel"),
            "review channel",
        )
        questions = self._form_questions(data.get("questions"))
        accept_role_id = self._optional_int(data.get("acceptRoleId"), "accept role")
        remove_role_id = self._optional_int(data.get("removeRoleId"), "remove role")
        for role_id, label in ((accept_role_id, "accept"), (remove_role_id, "remove")):
            if role_id is not None and guild.get_role(role_id) is None:
                raise web.HTTPBadRequest(text=f"Choose a {label} role from this server.")

        form = {
            "form_id": existing["form_id"] if existing else applications.new_form_id(),
            "name": name,
            "description": str(data.get("description") or "").strip() or None,
            "review_channel_id": review_channel.id,
            "questions": questions,
            "accept_role_id": accept_role_id,
            "remove_role_id": remove_role_id,
            "allow_multiple": bool(data.get("allowMultiple", False)),
            "panel_channel_id": (existing or {}).get("panel_channel_id"),
            "panel_message_id": (existing or {}).get("panel_message_id"),
            "panel_title": (existing or {}).get("panel_title"),
            "panel_description": (existing or {}).get("panel_description"),
        }
        try:
            stored = applications.upsert_form(self.bot.config, guild.id, form)
        except applications.ApplicationError as exc:
            raise web.HTTPBadRequest(text=str(exc))
        logger.info("Dashboard saved application form %s for guild %s", stored["name"], guild.id)
        return web.json_response(
            {
                "saved": True,
                "form": _application_form_payload(stored, guild),
                "created": existing is None,
            },
            status=201,
        )

    async def delete_application_form(self, request: web.Request) -> web.Response:
        guild = self._guild_from_request(request)
        form_id = request.match_info.get("form_id", "")
        try:
            removed = applications.remove_form(self.bot.config, guild.id, form_id)
        except applications.ApplicationError as exc:
            raise web.HTTPNotFound(text=str(exc))
        logger.info("Dashboard removed application form %s for guild %s", form_id, guild.id)
        return web.json_response(
            {"removed": True, "form": _application_form_payload(removed, guild)}
        )

    async def publish_application_panel(self, request: web.Request) -> web.Response:
        guild = self._guild_from_request(request)
        data = await self._json_body(request)
        form = applications.find_form(
            self.bot.config, guild.id, request.match_info.get("form_id")
        )
        if form is None:
            raise web.HTTPNotFound(text="That application form no longer exists.")
        channel = self._postable_text_channel(
            guild,
            self._required_int(data.get("channelId"), "channel"),
            "panel channel",
        )
        try:
            published = await applications.publish_panel(
                self.bot,
                guild,
                form,
                channel,
                title=data.get("title"),
                description=data.get("description"),
            )
        except applications.ApplicationError as exc:
            raise web.HTTPBadRequest(text=str(exc))
        except discord.Forbidden as exc:
            raise web.HTTPForbidden(
                text="The bot needs View Channel, Send Messages and Embed Links there."
            ) from exc
        logger.info("Dashboard published an application panel for guild %s", guild.id)
        return web.json_response(
            {"published": True, "form": _application_form_payload(published, guild)},
            status=201,
        )

    async def decide_application(self, request: web.Request) -> web.Response:
        guild = self._guild_from_request(request)
        record = self._application_from_request(request, guild)
        data = await self._json_body(request)
        decision = str(data.get("decision") or "").strip().lower()
        if decision not in applications.DECISIONS:
            raise web.HTTPBadRequest(text="Decide with 'approve' or 'deny'.")
        note = str(data.get("note") or "").strip()
        if len(note) > applications.MAX_DECISION_NOTE_LENGTH:
            raise web.HTTPBadRequest(
                text=f"Keep the note to {applications.MAX_DECISION_NOTE_LENGTH} characters."
            )
        # Attribution follows the moderation endpoints: the dashboard has no
        # Discord identity, so the decision is recorded against the owner and
        # logged with the peer address.
        actor = _DashboardActor(guild.owner_id)
        try:
            decided = await applications.decide_application(
                self.bot, guild, record, actor, decision, note or None
            )
        except applications.ApplicationError as exc:
            raise web.HTTPBadRequest(text=str(exc))
        logger.warning(
            "Dashboard decided application guild=%s application=%s decision=%s peer=%s",
            guild.id,
            record["id"],
            decision,
            request.remote or "unknown",
        )
        return web.json_response(
            {"decided": True, "application": _application_payload(decided, guild)}
        )

    async def sync_commands(self, request: web.Request) -> web.Response:
        guild = self._guild_from_request(request)
        try:
            await self.bot._clear_global_commands(
                reason=f"requested from dashboard for guild {guild.id}"
            )
            synced = await self.bot._sync_guild_commands(guild.id, force=True)
        except discord.DiscordException as exc:
            logger.warning("Dashboard command sync failed for guild %s: %s", guild.id, exc)
            raise web.HTTPBadGateway(text="Discord could not refresh application commands.")
        return web.json_response({"synced": bool(synced)})

    def _postable_text_channel(
        self, guild: discord.Guild, channel_id: int, label: str
    ) -> discord.TextChannel:
        """Resolve a text channel and check the bot can post in it.

        Panel publishing and review posts are the parts of these features that
        fail *visibly* (a member presses nothing) when the bot lacks a
        permission, so the check happens here rather than after a failed post.

        Named for what it does rather than for one caller: it was the ticket
        system's helper first, and application panels and review posts use the
        same check now that tickets are gone.
        """
        channel = guild.get_channel(int(channel_id))
        if not isinstance(channel, discord.TextChannel):
            raise web.HTTPBadRequest(text=f"Choose a text channel from this server as the {label}.")
        bot_member = guild.me
        if bot_member is not None:
            permissions = channel.permissions_for(bot_member)
            missing = [
                name
                for name, attribute in (
                    ("View Channel", "view_channel"),
                    ("Send Messages", "send_messages"),
                    ("Embed Links", "embed_links"),
                )
                if not getattr(permissions, attribute, False)
            ]
            if missing:
                raise web.HTTPBadRequest(
                    text=f"The bot needs {', '.join(missing)} in that channel."
                )
        return channel

    @staticmethod
    def _form_questions(raw: object) -> list[dict]:
        """Validate the dashboard's question editor payload.

        A form is a list of up to :data:`applications.MAX_QUESTIONS` questions;
        the limits here are Discord's own modal limits, surfaced as a 400 so the
        administrator is told before a member ever sees a broken modal.
        """
        if raw is None:
            raise web.HTTPBadRequest(text="Add at least one question.")
        if not isinstance(raw, list):
            raise web.HTTPBadRequest(text="Questions must be a list.")
        if not raw:
            raise web.HTTPBadRequest(text="Add at least one question.")
        if len(raw) > applications.MAX_QUESTIONS:
            raise web.HTTPBadRequest(
                text=f"A form can ask at most {applications.MAX_QUESTIONS} questions "
                "(Discord's modal limit)."
            )
        questions = []
        for index, item in enumerate(raw, 1):
            if not isinstance(item, dict):
                raise web.HTTPBadRequest(text=f"Question {index} must be an object.")
            label = str(item.get("label") or "").strip()
            if not label:
                raise web.HTTPBadRequest(text=f"Question {index} needs a label.")
            if len(label) > applications.MAX_QUESTION_LABEL_LENGTH:
                raise web.HTTPBadRequest(
                    text=f"Question {index} is too long — labels are limited to "
                    f"{applications.MAX_QUESTION_LABEL_LENGTH} characters."
                )
            style = applications.normalize_style(item.get("style"))
            placeholder = str(item.get("placeholder") or "").strip() or None
            if placeholder and len(placeholder) > applications.MAX_PLACEHOLDER_LENGTH:
                raise web.HTTPBadRequest(
                    text=f"Question {index}'s placeholder is longer than "
                    f"{applications.MAX_PLACEHOLDER_LENGTH} characters."
                )
            questions.append(
                {
                    "label": label,
                    "style": style,
                    "required": bool(item.get("required", True)),
                    "placeholder": placeholder,
                }
            )
        return questions

    def _application_from_request(
        self, request: web.Request, guild: discord.Guild
    ) -> dict:
        """The application named in the URL path, or 404."""
        record = applications.get_application(
            request.match_info.get("application_id")
        )
        if record is None or int(record["guild_id"]) != guild.id:
            raise web.HTTPNotFound(text="That application no longer exists.")
        return record

    def _persist_config(self, updated: dict) -> None:
        try:
            self._save_config(updated)
        except OSError as exc:
            logger.exception("Dashboard could not save bot config")
            raise web.HTTPInternalServerError(text="Could not save config.json.") from exc
        self.bot.config = updated

    def _guild_from_request(self, request: web.Request) -> discord.Guild:
        guild_id = self._required_int(request.match_info.get("guild_id"), "server ID")
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            raise web.HTTPNotFound(text="The bot is not connected to that server.")
        return guild

    async def _resolve_member(
        self,
        guild: discord.Guild,
        user_id: int,
    ) -> Optional[discord.Member]:
        member = guild.get_member(user_id)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(user_id)
        except discord.NotFound:
            return None
        except discord.HTTPException as exc:
            logger.warning("Could not fetch member %s in guild %s: %s", user_id, guild.id, exc)
            raise web.HTTPBadGateway(text="Discord could not look up that member.")

    @staticmethod
    async def _json_body(request: web.Request) -> dict:
        try:
            data = await request.json()
        except (ValueError, TypeError) as exc:
            raise web.HTTPBadRequest(text="Send a valid JSON request body.") from exc
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(text="JSON body must be an object.")
        return data

    @staticmethod
    def _required_int(value: Any, label: str) -> int:
        parsed = DashboardServer._optional_int(value, label)
        if parsed is None:
            raise web.HTTPBadRequest(text=f"{label.capitalize()} is required.")
        return parsed

    @staticmethod
    def _optional_int(value: Any, label: str) -> Optional[int]:
        if value in (None, ""):
            return None
        if isinstance(value, bool):
            raise web.HTTPBadRequest(text=f"{label.capitalize()} must be a number.")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise web.HTTPBadRequest(text=f"{label.capitalize()} must be a number.") from exc
        if parsed <= 0:
            raise web.HTTPBadRequest(text=f"{label.capitalize()} must be positive.")
        return parsed

    @staticmethod
    def _role_from_body(
        guild: discord.Guild,
        raw_id: Any,
        label: str,
        *,
        optional: bool = False,
    ) -> Optional[discord.Role]:
        role_id = DashboardServer._optional_int(raw_id, label)
        if role_id is None and optional:
            return None
        if role_id is None:
            raise web.HTTPBadRequest(text=f"{label.capitalize()} is required.")
        role = guild.get_role(role_id)
        if role is None or role.is_default():
            raise web.HTTPBadRequest(text=f"Choose a valid {label} from this server.")
        return role

    def _is_configured_primary_guild(self, guild_id: int) -> bool:
        try:
            return int(self.bot.config.get("server_id") or 0) == int(guild_id)
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _punishment_payload(guild: discord.Guild, record: dict) -> dict:
        member = guild.get_member(record["user_id"])
        expires = datetime.fromisoformat(record["expires_at"])
        remaining = max(0, int((expires - datetime.now(timezone.utc)).total_seconds()))
        return {
            **record,
            "memberName": member.display_name if member else f"User {record['user_id']}",
            "memberAvatar": member.display_avatar.url if member else None,
            "remainingSeconds": remaining,
            "remainingLabel": _human_duration(remaining),
        }


_ID_FIELD_SUFFIXES = ("_id", "Id")
_ID_FIELD_EXCEPTIONS = {"id"}  # database row keys


def _application_form_payload(form: dict, guild: discord.Guild) -> dict:
    """One application form, including the panel and review destinations."""
    accept_role = guild.get_role(int(form["accept_role_id"])) if form.get("accept_role_id") else None
    remove_role = guild.get_role(int(form["remove_role_id"])) if form.get("remove_role_id") else None
    return {
        "formId": form.get("form_id"),
        "name": form.get("name"),
        "description": form.get("description"),
        "reviewChannelId": _snowflake(form.get("review_channel_id")),
        "acceptRoleId": _snowflake(form.get("accept_role_id")),
        "acceptRoleName": accept_role.name if accept_role else None,
        "removeRoleId": _snowflake(form.get("remove_role_id")),
        "removeRoleName": remove_role.name if remove_role else None,
        "allowMultiple": bool(form.get("allow_multiple")),
        "panelChannelId": _snowflake(form.get("panel_channel_id")),
        "panelMessageId": _snowflake(form.get("panel_message_id")),
        "panelTitle": form.get("panel_title"),
        "panelDescription": form.get("panel_description"),
        "questions": [
            {
                "label": question["label"],
                "style": question["style"],
                "required": bool(question.get("required", True)),
                "placeholder": question.get("placeholder"),
            }
            for question in form.get("questions", [])
        ],
    }


def _application_payload(application: dict, guild: discord.Guild) -> dict:
    """One submission, including the answers staff decide on."""
    return {
        "id": _snowflake(application.get("id")),
        "formId": application.get("form_id"),
        "formName": application.get("form_name"),
        "userId": _snowflake(application.get("user_id")),
        "status": application.get("status"),
        "submittedAt": application.get("submitted_at"),
        "decidedAt": application.get("decided_at"),
        "decidedBy": _snowflake(application.get("decided_by")),
        "decisionNote": application.get("decision_note"),
        "reviewChannelId": _snowflake(application.get("review_channel_id")),
        "reviewMessageId": _snowflake(application.get("review_message_id")),
        "answers": [
            {"question": pair.get("question"), "answer": pair.get("answer")}
            for pair in application.get("answers") or []
        ],
    }


def _json_safe_ids(payload: dict) -> dict:
    """Stringify every snowflake-looking field of a database row.

    Rows are echoed to the browser as-is, so ids such as ``user_id`` and
    ``moderator_id`` must be strings too — otherwise JavaScript rounds them
    and any id shown to a moderator (or sent back to the server) is wrong.
    """
    safe = {}
    for key, value in payload.items():
        if (
            key not in _ID_FIELD_EXCEPTIONS
            and key.endswith(_ID_FIELD_SUFFIXES)
            and isinstance(value, int)
            and not isinstance(value, bool)
        ):
            safe[key] = str(value)
        else:
            safe[key] = value
    return safe


def _human_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _seconds = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and not days:
        parts.append(f"{minutes}m")
    return " ".join(parts) or "under a minute"
