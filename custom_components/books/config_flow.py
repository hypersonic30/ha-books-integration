"""Config flow for the Books integration."""
from __future__ import annotations

import logging

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_ABS_TOKEN,
    CONF_ABS_URL,
    CONF_CHAPTARR_API_KEY,
    CONF_CHAPTARR_URL,
    CONF_DEBUG_LOGGING,
    CONF_NOTIFY_SERVICE,
    CONF_RESCUE_IMPORTS,
    CONF_TOLINO_TOKEN,
    CONF_TOLINO_URL,
    CONF_VERIFY_SSL,
    DEFAULT_DEBUG_LOGGING,
    DEFAULT_RESCUE_IMPORTS,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    REQUEST_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)

_PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
_URL = TextSelector(TextSelectorConfig(type=TextSelectorType.URL))


def _schema(defaults: dict) -> vol.Schema:
    return vol.Schema({
        vol.Required(CONF_CHAPTARR_URL, default=defaults.get(CONF_CHAPTARR_URL, "")): _URL,
        vol.Required(CONF_CHAPTARR_API_KEY, default=defaults.get(CONF_CHAPTARR_API_KEY, "")): _PASSWORD,
        vol.Required(CONF_ABS_URL, default=defaults.get(CONF_ABS_URL, "")): _URL,
        vol.Required(CONF_ABS_TOKEN, default=defaults.get(CONF_ABS_TOKEN, "")): _PASSWORD,
        vol.Optional(CONF_TOLINO_URL, description={"suggested_value": defaults.get(CONF_TOLINO_URL, "")}): _URL,
        vol.Optional(CONF_TOLINO_TOKEN, description={"suggested_value": defaults.get(CONF_TOLINO_TOKEN, "")}): _PASSWORD,
        vol.Required(CONF_VERIFY_SSL, default=defaults.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)): bool,
        vol.Required(
            CONF_RESCUE_IMPORTS, default=defaults.get(CONF_RESCUE_IMPORTS, DEFAULT_RESCUE_IMPORTS)
        ): bool,
        vol.Optional(
            CONF_NOTIFY_SERVICE, description={"suggested_value": defaults.get(CONF_NOTIFY_SERVICE, "")}
        ): str,
        vol.Required(
            CONF_DEBUG_LOGGING, default=defaults.get(CONF_DEBUG_LOGGING, DEFAULT_DEBUG_LOGGING)
        ): bool,
    })


def _error_key(prefix: str, url: str, err: Exception) -> str:
    if isinstance(err, aiohttp.ClientSSLError):
        _LOGGER.warning("books: SSL error for %s — %s", url, err)
        return "ssl_error"
    if isinstance(err, aiohttp.ClientConnectorError):
        _LOGGER.warning(
            "books: cannot connect to %s — %s (a '.local' name usually doesn't resolve "
            "inside Home Assistant's container; use an IP or regular DNS name)", url, err,
        )
        return f"{prefix}_cannot_connect"
    if isinstance(err, TimeoutError):
        _LOGGER.warning("books: %s timed out after %ss", url, REQUEST_TIMEOUT)
        return f"{prefix}_cannot_connect"
    _LOGGER.error("books: unexpected error contacting %s — %s: %s", url, type(err).__name__, err)
    return "unknown"


async def _check_chaptarr(hass: HomeAssistant, url: str, api_key: str, verify_ssl: bool) -> str | None:
    session = async_get_clientsession(hass, verify_ssl=verify_ssl)
    try:
        async with session.get(
            f"{url}/api/v1/system/status",
            headers={"X-Api-Key": api_key},
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
        ) as resp:
            if resp.status == 401:
                return "chaptarr_invalid_auth"
            if resp.status != 200:
                return "chaptarr_cannot_connect"
            status = await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as err:
        return _error_key("chaptarr", url, err)
    if status.get("appName", "").lower() != "chaptarr":
        _LOGGER.warning("books: %s is %s, not Chaptarr", url, status.get("appName"))
        return "not_chaptarr"
    return None


async def _check_abs(hass: HomeAssistant, url: str, token: str, verify_ssl: bool) -> str | None:
    session = async_get_clientsession(hass, verify_ssl=verify_ssl)
    try:
        async with session.get(
            f"{url}/api/me",
            headers={"Authorization": f"Bearer {token}"},
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
        ) as resp:
            if resp.status in (401, 403):
                return "abs_invalid_auth"
            if resp.status != 200:
                return "abs_cannot_connect"
            await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as err:
        return _error_key("abs", url, err)
    return None


async def _check_tolino(hass: HomeAssistant, url: str, token: str, verify_ssl: bool) -> str | None:
    """The bridge must answer and accept the token; being logged out at Thalia is not a config error."""
    session = async_get_clientsession(hass, verify_ssl=verify_ssl)
    try:
        async with session.get(
            f"{url}/status",
            headers={"Authorization": f"Bearer {token}"},
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
        ) as resp:
            if resp.status == 401:
                return "tolino_invalid_auth"
            if resp.status != 200:
                return "tolino_cannot_connect"
            status = await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as err:
        return _error_key("tolino", url, err)
    return None if isinstance(status, dict) and "logged_in" in status else "not_tolino_bridge"


async def _validate(hass: HomeAssistant, data: dict) -> tuple[dict, dict[str, str]]:
    data = {**data}
    errors: dict[str, str] = {}
    for key in (CONF_CHAPTARR_URL, CONF_ABS_URL):
        data[key] = data[key].strip().rstrip("/")
        if not data[key].startswith(("http://", "https://")):
            errors[key] = "invalid_url"
    data[CONF_NOTIFY_SERVICE] = (data.get(CONF_NOTIFY_SERVICE) or "").strip()
    data[CONF_TOLINO_URL] = (data.get(CONF_TOLINO_URL) or "").strip().rstrip("/")
    data[CONF_TOLINO_TOKEN] = (data.get(CONF_TOLINO_TOKEN) or "").strip()
    if data[CONF_TOLINO_URL] and not data[CONF_TOLINO_URL].startswith(("http://", "https://")):
        errors[CONF_TOLINO_URL] = "invalid_url"
    elif data[CONF_TOLINO_URL] and not data[CONF_TOLINO_TOKEN]:
        errors[CONF_TOLINO_TOKEN] = "tolino_token_missing"
    elif data[CONF_TOLINO_TOKEN] and not data[CONF_TOLINO_URL]:
        errors[CONF_TOLINO_URL] = "tolino_url_missing"
    if errors:
        return data, errors
    verify = data[CONF_VERIFY_SSL]
    if err := await _check_chaptarr(hass, data[CONF_CHAPTARR_URL], data[CONF_CHAPTARR_API_KEY].strip(), verify):
        errors[CONF_CHAPTARR_API_KEY if "auth" in err else CONF_CHAPTARR_URL] = err
    if err := await _check_abs(hass, data[CONF_ABS_URL], data[CONF_ABS_TOKEN].strip(), verify):
        errors[CONF_ABS_TOKEN if "auth" in err else CONF_ABS_URL] = err
    if data[CONF_TOLINO_URL]:
        if err := await _check_tolino(hass, data[CONF_TOLINO_URL], data[CONF_TOLINO_TOKEN], verify):
            errors[CONF_TOLINO_TOKEN if "auth" in err else CONF_TOLINO_URL] = err
    return data, errors


class BooksConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Set up the Chaptarr + Audiobookshelf connection."""

    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = await _validate(self.hass, user_input)
            if not errors:
                return self.async_create_entry(title="Books", data=data)
        return self.async_show_form(step_id="user", data_schema=_schema(user_input or {}), errors=errors)

    async def async_step_reconfigure(self, user_input: dict | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = await _validate(self.hass, user_input)
            if not errors:
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_schema({**entry.data, **(user_input or {})}),
            errors=errors,
        )
