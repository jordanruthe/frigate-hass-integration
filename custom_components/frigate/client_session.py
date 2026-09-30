"""The aiohttp session used for requests to Frigate."""

from __future__ import annotations

import aiohttp

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .const import DOMAIN

DATA_CLIENTSESSION = f"{DOMAIN}_clientsession"


@callback
def async_get_frigate_clientsession(hass: HomeAssistant) -> aiohttp.ClientSession:
    """Return the session for requests to Frigate.

    The session does not keep cookies. Home Assistant's shared session does, and a
    cookie in its jar overrides the same cookie in an explicit `Cookie` header. A
    forward-auth proxy sets a fresh unauthenticated session cookie when it
    redirects an expired session to login, which would then shadow the cookie from
    every subsequent login. All cookies Frigate needs are sent explicitly instead.
    TLS verification is chosen per request through the `ssl` argument.
    """
    session: aiohttp.ClientSession | None = hass.data.get(DATA_CLIENTSESSION)
    if session is None:
        session = async_create_clientsession(hass, cookie_jar=aiohttp.DummyCookieJar())
        hass.data[DATA_CLIENTSESSION] = session
    return session
