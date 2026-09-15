"""Client certificate + forward-auth (e.g. Authentik) support for the Frigate client.

When Frigate sits behind a reverse proxy that requires a client certificate,
optionally combined with a forward-auth SSO provider such as Authentik whose
login flow authenticates the user from that certificate, plain API requests get
redirected to an interactive login page. This module presents the certificate,
drives the SSO flow headlessly through Authentik's flow executor API and returns
the resulting session cookies so the API client can send them on every request.
"""

from __future__ import annotations

from http.cookies import SimpleCookie
import logging
import os
import re
import ssl
import tempfile

import aiohttp
import certifi
from yarl import URL

# ==============================================================================
# Please do not add HomeAssistant specific imports/functionality to this module,
# so that this library can be optionally moved to a different repo at a later
# date.
# ==============================================================================

_LOGGER: logging.Logger = logging.getLogger(__name__)

LOGIN_TIMEOUT = 30

# Maximum number of interactive flow pages (authentication, authorization, ...)
# to walk through before giving up.
MAX_FLOWS = 5

# Response statuses that indicate the forward-auth session is missing or expired.
AUTH_REQUIRED_STATUSES = frozenset({301, 302, 303, 307, 308, 401})

_FLOW_PATH_RE = re.compile(r"^/if/flow/(?P<slug>[^/]+)/?$")


class ClientCertificateError(Exception):
    """The client certificate or key could not be loaded."""


class ForwardAuthError(Exception):
    """Forward-auth login failed."""


def create_client_ssl_context(
    certificate: str, private_key: str, verify_ssl: bool = True
) -> ssl.SSLContext:
    """Build an SSL context presenting the given PEM certificate and key.

    This performs blocking file I/O and must be run in an executor.
    """
    context = ssl.create_default_context(cafile=certifi.where())
    if not verify_ssl:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    with tempfile.TemporaryDirectory() as tmpdir:
        cert_path = os.path.join(tmpdir, "client.crt")
        key_path = os.path.join(tmpdir, "client.key")
        for path, content in ((cert_path, certificate), (key_path, private_key)):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as file:
                file.write(content.strip() + "\n")
        try:
            context.load_cert_chain(cert_path, key_path)
        except (ssl.SSLError, ValueError) as exc:
            raise ClientCertificateError(str(exc)) from exc
    return context


def _same_origin(first: URL, second: URL) -> bool:
    return (first.scheme, first.host, first.port) == (
        second.scheme,
        second.host,
        second.port,
    )


async def async_forward_auth_login(host: str, ssl_context: ssl.SSLContext) -> str:
    """Log in through the forward-auth proxy in front of `host`.

    Returns the value for a `Cookie` header that authorizes requests to `host`
    (empty if the proxy let the request through without a login).
    """
    target = URL(host)
    probe_url = target / "api/version"

    try:
        async with aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(unsafe=True),
            timeout=aiohttp.ClientTimeout(total=LOGIN_TIMEOUT),
        ) as session:
            async with session.get(probe_url, ssl=ssl_context) as response:
                await response.read()
                current_url = response.url
                status = response.status

            for _ in range(MAX_FLOWS):
                match = _FLOW_PATH_RE.match(current_url.path)
                if match is None:
                    break

                executor_url = current_url.with_path(
                    f"/api/v3/flows/executor/{match['slug']}/"
                ).with_query({"query": current_url.raw_query_string})
                async with session.get(executor_url, ssl=ssl_context) as response:
                    response.raise_for_status()
                    challenge = await response.json()

                component = challenge.get("component")
                if component != "xak-flow-redirect":
                    raise ForwardAuthError(
                        f"login flow '{match['slug']}' requires interaction "
                        f"(stage '{component}'); is the client certificate "
                        "mapped to a user?"
                    )

                async with session.get(
                    current_url.join(URL(challenge["to"])), ssl=ssl_context
                ) as response:
                    await response.read()
                    current_url = response.url
                    status = response.status

            if (
                not _same_origin(current_url, target)
                or status in AUTH_REQUIRED_STATUSES
                or _FLOW_PATH_RE.match(current_url.path)
            ):
                raise ForwardAuthError(
                    f"login did not return to {target} (ended at {current_url} "
                    f"with status {status})"
                )

            cookies: SimpleCookie = session.cookie_jar.filter_cookies(target)
    except (aiohttp.ClientError, TimeoutError, KeyError, ValueError) as exc:
        raise ForwardAuthError(f"login request failed: {exc!r}") from exc

    _LOGGER.debug("Forward-auth login to %s succeeded", target)
    return "; ".join(f"{name}={morsel.value}" for name, morsel in cookies.items())
