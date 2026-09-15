"""Test client certificate and forward-auth (Authentik) support."""

from __future__ import annotations

from collections.abc import AsyncGenerator
import datetime
import ssl
from typing import Any
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs

import aiohttp
from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from hass_web_proxy_lib import ProxiedURL, ProxyView
import pytest

from custom_components.frigate.api import FrigateApiClient, FrigateApiClientError
from custom_components.frigate.config_flow import async_get_client_ssl_context
from custom_components.frigate.const import (
    CONF_CLIENT_CERTIFICATE,
    CONF_CLIENT_KEY,
    CONF_VALIDATE_SSL,
    DOMAIN,
)
from custom_components.frigate.forward_auth import (
    ClientCertificateError,
    ForwardAuthError,
    async_forward_auth_login,
    create_client_ssl_context,
)
from custom_components.frigate.views import FrigateProxyViewMixin
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant

from . import (
    TEST_URL,
    create_mock_frigate_client,
    create_mock_frigate_config_entry,
    start_frigate_server,
)

SESSION_COOKIE = "authentik_proxy_test"


def _make_certificate() -> tuple[str, str]:
    """Create a self-signed PEM certificate and key."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "homeassistant")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return (
        cert.public_bytes(serialization.Encoding.PEM).decode(),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
    )


CERTIFICATE, PRIVATE_KEY = _make_certificate()


@pytest.fixture
def ssl_context() -> ssl.SSLContext:
    """Return a client SSL context (unused by the plain-HTTP test servers)."""
    return ssl.create_default_context()


@pytest.fixture
async def aiohttp_session() -> AsyncGenerator[aiohttp.ClientSession]:
    """Test fixture for aiohttp.ClientSession."""
    async with aiohttp.ClientSession() as session:
        yield session


class FakeAuthentik:
    """A fake Frigate behind an Authentik-style forward-auth proxy."""

    def __init__(self, challenge: str = "xak-flow-redirect") -> None:
        """Initialize."""
        self.challenge = challenge
        self.valid_session = "session-1"
        self.logins = 0
        self.api_cookies: list[str | None] = []

    def _authorized(self, request: web.Request) -> bool:
        return request.cookies.get(SESSION_COOKIE) == self.valid_session

    async def version(self, request: web.Request) -> web.Response:
        """Frigate API endpoint behind forward-auth."""
        self.api_cookies.append(request.headers.get("Cookie"))
        if not self._authorized(request):
            return web.HTTPFound("/application/o/authorize/?client_id=frigate")
        return web.Response(text="0.16.0")

    async def authorize(self, request: web.Request) -> web.Response:
        """Authorization endpoint: requires the authentication flow first."""
        if request.cookies.get("authentik_session") != "logged-in":
            raise web.HTTPFound(
                "/if/flow/cert-login/?next=%2Fapplication%2Fo%2Fauthorize%2F"
            )
        # Implicit consent is itself a (stage-less) flow.
        raise web.HTTPFound("/if/flow/implicit-consent/?next=%2Fcallback")

    async def flow_page(self, request: web.Request) -> web.Response:
        """The flow interface (browser JavaScript app)."""
        return web.Response(text="<html>flow</html>", content_type="text/html")

    async def executor(self, request: web.Request) -> web.Response:
        """Flow executor API."""
        slug = request.match_info["slug"]
        if slug == "cert-login":
            assert parse_qs(request.query["query"]) == {
                "next": ["/application/o/authorize/"]
            }
            response = web.json_response(
                {"component": self.challenge, "to": "/application/o/authorize/"}
            )
            response.set_cookie("authentik_session", "logged-in")
            return response
        return web.json_response({"component": "xak-flow-redirect", "to": "/callback"})

    async def callback(self, request: web.Request) -> web.Response:
        """Outpost callback: sets the proxy session cookie."""
        self.logins += 1
        response = web.HTTPFound("/api/version")
        response.set_cookie(SESSION_COOKIE, self.valid_session)
        raise response

    def routes(self) -> list[web.RouteDef]:
        """Return the routes of the fake server."""
        return [
            web.get("/api/version", self.version),
            web.get("/application/o/authorize/", self.authorize),
            web.get("/if/flow/{slug}/", self.flow_page),
            web.get("/api/v3/flows/executor/{slug}/", self.executor),
            web.get("/callback", self.callback),
        ]


async def test_create_client_ssl_context() -> None:
    """Test building an SSL context from PEM strings."""
    context = create_client_ssl_context(CERTIFICATE, PRIVATE_KEY)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname

    context = create_client_ssl_context(CERTIFICATE, PRIVATE_KEY, verify_ssl=False)
    assert context.verify_mode == ssl.CERT_NONE
    assert not context.check_hostname

    with pytest.raises(ClientCertificateError):
        create_client_ssl_context(CERTIFICATE, "not a key")


async def test_login_through_authentik_flows(
    aiohttp_server: Any, ssl_context: ssl.SSLContext
) -> None:
    """Test a certificate login walking the authentication and consent flows."""
    authentik = FakeAuthentik()
    server = await start_frigate_server(aiohttp_server, authentik.routes())

    cookie = await async_forward_auth_login(str(server.make_url("/")), ssl_context)
    assert f"{SESSION_COOKIE}=session-1" in cookie
    assert authentik.logins == 1


async def test_login_not_required(
    aiohttp_server: Any, ssl_context: ssl.SSLContext
) -> None:
    """Test a proxy that only checks the certificate returns no cookies."""
    server = await start_frigate_server(
        aiohttp_server,
        [web.get("/api/version", lambda _: web.Response(text="1"))],
    )
    assert await async_forward_auth_login(str(server.make_url("/")), ssl_context) == ""


async def test_login_requires_interaction(
    aiohttp_server: Any, ssl_context: ssl.SSLContext
) -> None:
    """Test an unrecognized certificate leaves the flow at an interactive stage."""
    authentik = FakeAuthentik(challenge="ak-stage-identification")
    server = await start_frigate_server(aiohttp_server, authentik.routes())

    with pytest.raises(ForwardAuthError, match="ak-stage-identification"):
        await async_forward_auth_login(str(server.make_url("/")), ssl_context)


@pytest.mark.parametrize(
    "routes",
    [
        # Unauthorized without a login flow.
        [web.get("/api/version", lambda _: web.Response(status=401))],
        # Endless chain of flows.
        [
            web.get("/api/version", lambda _: web.HTTPFound("/if/flow/a/")),
            web.get("/if/flow/a/", lambda _: web.Response(text="a")),
            web.get(
                "/api/v3/flows/executor/a/",
                lambda _: web.json_response(
                    {"component": "xak-flow-redirect", "to": "/if/flow/a/"}
                ),
            ),
        ],
    ],
)
async def test_login_does_not_complete(
    aiohttp_server: Any, ssl_context: ssl.SSLContext, routes: list[web.RouteDef]
) -> None:
    """Test logins that never get back to Frigate."""
    server = await start_frigate_server(aiohttp_server, routes)
    with pytest.raises(ForwardAuthError, match="did not return"):
        await async_forward_auth_login(str(server.make_url("/")), ssl_context)


async def test_login_connection_error(ssl_context: ssl.SSLContext) -> None:
    """Test a login request failing."""
    with patch(
        "aiohttp.ClientSession.get", side_effect=aiohttp.ClientConnectionError
    ), pytest.raises(ForwardAuthError, match="request failed"):
        await async_forward_auth_login("https://frigate.example.com", ssl_context)


async def test_client_ssl_property(
    aiohttp_session: aiohttp.ClientSession, ssl_context: ssl.SSLContext
) -> None:
    """Test the ssl argument used for requests."""
    assert FrigateApiClient(TEST_URL, aiohttp_session).request_ssl is True
    assert (
        FrigateApiClient(TEST_URL, aiohttp_session, validate_ssl=False).request_ssl is False
    )
    client = FrigateApiClient(TEST_URL, aiohttp_session, ssl_context=ssl_context)
    assert client.request_ssl is ssl_context


async def test_client_logs_in_and_relogs_on_expiry(
    aiohttp_session: aiohttp.ClientSession,
    aiohttp_server: Any,
    ssl_context: ssl.SSLContext,
) -> None:
    """Test the API client logs in, sends the cookie and renews an expired session."""
    authentik = FakeAuthentik()
    server = await start_frigate_server(aiohttp_server, authentik.routes())
    client = FrigateApiClient(
        str(server.make_url("/")), aiohttp_session, ssl_context=ssl_context
    )

    assert await client.async_get_version() == "0.16.0"
    assert authentik.logins == 1
    assert f"{SESSION_COOKIE}=session-1" in (await client.get_auth_headers())["Cookie"]

    # The proxy session expires: the next request is redirected, so log in again.
    authentik.valid_session = "session-2"
    assert await client.async_get_version() == "0.16.0"
    assert authentik.logins == 2
    assert f"{SESSION_COOKIE}=session-2" in authentik.api_cookies[-1]


async def test_client_session_rejected_after_login(
    aiohttp_session: aiohttp.ClientSession,
    aiohttp_server: Any,
    ssl_context: ssl.SSLContext,
) -> None:
    """Test a proxy that keeps redirecting after a fresh login."""
    server = await start_frigate_server(
        aiohttp_server,
        [web.get("/api/version", lambda _: web.Response(status=401))],
    )
    client = FrigateApiClient(
        str(server.make_url("/")), aiohttp_session, ssl_context=ssl_context
    )

    with patch(
        "custom_components.frigate.api.async_forward_auth_login",
        AsyncMock(side_effect=["cookie=1", "cookie=2"]),
    ) as login, pytest.raises(FrigateApiClientError, match="rejected"):
        await client.async_get_version()
    assert login.call_count == 2


async def test_client_login_failure(
    aiohttp_session: aiohttp.ClientSession, ssl_context: ssl.SSLContext
) -> None:
    """Test a failing forward-auth login surfaces as a client error."""
    client = FrigateApiClient(TEST_URL, aiohttp_session, ssl_context=ssl_context)
    with patch(
        "custom_components.frigate.api.async_forward_auth_login",
        AsyncMock(side_effect=ForwardAuthError("nope")),
    ), pytest.raises(FrigateApiClientError, match="nope"):
        await client.get_auth_headers()


async def test_client_concurrent_login_is_shared(
    aiohttp_session: aiohttp.ClientSession, ssl_context: ssl.SSLContext
) -> None:
    """Test a login is skipped when another request already renewed the session."""
    client = FrigateApiClient(TEST_URL, aiohttp_session, ssl_context=ssl_context)
    with patch(
        "custom_components.frigate.api.async_forward_auth_login",
        AsyncMock(return_value="cookie=new"),
    ) as login:
        await client.get_auth_headers()
        await client._forward_auth_login("cookie=old")
    assert login.call_count == 1


async def test_client_frigate_login_behind_forward_auth(
    aiohttp_session: aiohttp.ClientSession,
    aiohttp_server: Any,
    ssl_context: ssl.SSLContext,
) -> None:
    """Test the Frigate login request carries the proxy session cookie too."""
    login_handler = AsyncMock(return_value=web.Response(text="ok"))
    server = await start_frigate_server(
        aiohttp_server, [web.post("/api/login", login_handler)]
    )
    client = FrigateApiClient(
        str(server.make_url("/")), aiohttp_session, "user", "pass", True, ssl_context
    )
    with patch(
        "custom_components.frigate.api.async_forward_auth_login",
        AsyncMock(return_value="cookie=1"),
    ):
        await client.api_wrapper(
            "post", str(server.make_url("/api/login")), is_login_request=True
        )
    assert login_handler.call_args[0][0].headers["Cookie"] == "cookie=1"


async def test_client_without_session_cookie(
    aiohttp_session: aiohttp.ClientSession, ssl_context: ssl.SSLContext
) -> None:
    """Test no Cookie header is sent when the proxy needed no login."""
    client = FrigateApiClient(TEST_URL, aiohttp_session, ssl_context=ssl_context)
    with patch(
        "custom_components.frigate.api.async_forward_auth_login",
        AsyncMock(return_value=""),
    ):
        assert await client.get_auth_headers() == {}


async def test_async_get_client_ssl_context(hass: HomeAssistant) -> None:
    """Test building the SSL context from config entry data."""
    assert await async_get_client_ssl_context(hass, {CONF_URL: TEST_URL}) is None

    context = await async_get_client_ssl_context(
        hass,
        {
            CONF_CLIENT_CERTIFICATE: CERTIFICATE,
            CONF_CLIENT_KEY: PRIVATE_KEY,
            CONF_VALIDATE_SSL: False,
        },
    )
    assert context is not None
    assert context.verify_mode == ssl.CERT_NONE

    with pytest.raises(ClientCertificateError):
        await async_get_client_ssl_context(hass, {CONF_CLIENT_CERTIFICATE: CERTIFICATE})


async def test_config_flow_with_client_certificate(hass: HomeAssistant) -> None:
    """Test the user flow with a client certificate."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    mock_client = create_mock_frigate_client()
    user_input = {
        CONF_URL: TEST_URL,
        CONF_CLIENT_CERTIFICATE: CERTIFICATE,
        CONF_CLIENT_KEY: PRIVATE_KEY,
    }

    with patch(
        "custom_components.frigate.config_flow.FrigateApiClient",
        return_value=mock_client,
    ) as client_class, patch(
        "custom_components.frigate.async_setup_entry", return_value=True
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input
        )
        await hass.async_block_till_done()

    assert result["type"] == "create_entry"
    assert result["data"][CONF_CLIENT_CERTIFICATE] == CERTIFICATE
    assert isinstance(client_class.call_args[0][5], ssl.SSLContext)


async def test_config_flow_invalid_client_certificate(hass: HomeAssistant) -> None:
    """Test the user flow with an unusable client certificate."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_URL: TEST_URL, CONF_CLIENT_CERTIFICATE: CERTIFICATE},
    )
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_client_certificate"}


async def test_setup_entry_invalid_client_certificate(hass: HomeAssistant) -> None:
    """Test setup fails permanently with an unusable client certificate."""
    config_entry = create_mock_frigate_config_entry(
        hass, data={CONF_URL: TEST_URL, CONF_CLIENT_KEY: "garbage"}
    )
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state == ConfigEntryState.SETUP_ERROR


async def test_proxy_view_uses_client_certificate(
    ssl_context: ssl.SSLContext,
) -> None:
    """Test proxied requests present the client certificate."""

    class TestView(FrigateProxyViewMixin, ProxyView):
        def _get_proxied_url_impl(
            self, request: web.Request, **kwargs: Any
        ) -> ProxiedURL:
            return ProxiedURL(url="https://example.com", headers={}, query_params={})

    view = object.__new__(TestView)
    client = Mock(ssl_context=ssl_context)
    with patch.object(TestView, "_get_client_for_request", return_value=client):
        result = view._get_proxied_url(Mock())
    assert result.ssl_context is ssl_context
