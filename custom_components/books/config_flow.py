"""Config flow for the Books integration."""
from __future__ import annotations

import logging
import re

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult, ConfigSubentryFlow, SubentryFlowResult
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import TolinoBridgeClient, UpstreamError
from .notify_helper import async_push, unknown_targets
from .const import (
    CONF_TOLINO_ACCOUNT,
    DEFAULT_TOLINO_ACCOUNT,
    CONF_ABS_NAME,
    CONF_HA_USER,
    CONF_KOMGA_NAME,
    CONF_USER_TOLINO,
    SUBENTRY_USER,
    CONF_ABS_TOKEN,
    CONF_ABS_URL,
    CONF_AUTO_SEND,
    CONF_CHAPTARR_API_KEY,
    CONF_CHAPTARR_URL,
    CONF_DEBUG_LOGGING,
    CONF_KOMGA_API_KEY,
    CONF_KOMGA_URL,
    CONF_MYLAR_API_KEY,
    CONF_MYLAR_URL,
    CONF_NOTIFY_SERVICE,
    CONF_RESCUE_IMPORTS,
    CONF_SYNC_PROGRESS,
    CONF_SYNC_PROGRESS_WRITE,
    CONF_TOLINO_TOKEN,
    CONF_TOLINO_URL,
    CONF_VERIFY_SSL,
    DEFAULT_AUTO_SEND,
    DEFAULT_DEBUG_LOGGING,
    DEFAULT_RESCUE_IMPORTS,
    DEFAULT_SYNC_PROGRESS,
    DEFAULT_SYNC_PROGRESS_WRITE,
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
        vol.Optional(CONF_KOMGA_URL, description={"suggested_value": defaults.get(CONF_KOMGA_URL, "")}): _URL,
        vol.Optional(CONF_KOMGA_API_KEY, description={"suggested_value": defaults.get(CONF_KOMGA_API_KEY, "")}): _PASSWORD,
        vol.Optional(CONF_MYLAR_URL, description={"suggested_value": defaults.get(CONF_MYLAR_URL, "")}): _URL,
        vol.Optional(CONF_MYLAR_API_KEY, description={"suggested_value": defaults.get(CONF_MYLAR_API_KEY, "")}): _PASSWORD,
        vol.Optional(CONF_TOLINO_URL, description={"suggested_value": defaults.get(CONF_TOLINO_URL, "")}): _URL,
        vol.Optional(CONF_TOLINO_TOKEN, description={"suggested_value": defaults.get(CONF_TOLINO_TOKEN, "")}): _PASSWORD,
        vol.Required(CONF_AUTO_SEND, default=defaults.get(CONF_AUTO_SEND, DEFAULT_AUTO_SEND)): bool,
        vol.Required(CONF_SYNC_PROGRESS, default=defaults.get(CONF_SYNC_PROGRESS, DEFAULT_SYNC_PROGRESS)): bool,
        vol.Required(
            CONF_SYNC_PROGRESS_WRITE, default=defaults.get(CONF_SYNC_PROGRESS_WRITE, DEFAULT_SYNC_PROGRESS_WRITE)
        ): bool,
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


async def _abs_identity(hass: HomeAssistant, url: str, token: str, verify_ssl: bool) -> tuple[str | None, str]:
    """(error key or None, the Audiobookshelf user the token belongs to)."""
    session = async_get_clientsession(hass, verify_ssl=verify_ssl)
    try:
        async with session.get(
            f"{url}/api/me",
            headers={"Authorization": f"Bearer {token}"},
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
        ) as resp:
            if resp.status in (401, 403):
                return "abs_invalid_auth", ""
            if resp.status != 200:
                return "abs_cannot_connect", ""
            me = await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as err:
        return _error_key("abs", url, err), ""
    return None, str(me.get("username") or "") if isinstance(me, dict) else ""


async def _check_abs(hass: HomeAssistant, url: str, token: str, verify_ssl: bool) -> str | None:
    return (await _abs_identity(hass, url, token, verify_ssl))[0]


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


async def _komga_identity(hass: HomeAssistant, url: str, key: str, verify_ssl: bool) -> tuple[str | None, str]:
    """(error key or None, the Komga user the key belongs to)."""
    session = async_get_clientsession(hass, verify_ssl=verify_ssl)
    try:
        async with session.get(f"{url}/api/v2/users/me", headers={"X-API-Key": key},
                               timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as resp:
            if resp.status in (401, 403):
                return "komga_invalid_auth", ""
            if resp.status != 200:
                return "komga_cannot_connect", ""
            me = await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as err:
        return _error_key("komga", url, err), ""
    if not (isinstance(me, dict) and "roles" in me):
        return "not_komga", ""
    return None, str(me.get("email") or me.get("id") or "")


async def _check_komga(hass: HomeAssistant, url: str, key: str, verify_ssl: bool) -> str | None:
    return (await _komga_identity(hass, url, key, verify_ssl))[0]


async def _check_mylar(hass: HomeAssistant, url: str, key: str, verify_ssl: bool) -> str | None:
    """Mylar answers HTTP 200 even for errors: {"success": false, "error": {"code": 460, ...}}."""
    session = async_get_clientsession(hass, verify_ssl=verify_ssl)
    try:
        async with session.get(f"{url}/api", params={"cmd": "getVersion", "apikey": key},
                               timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as resp:
            if resp.status in (401, 403):
                return "mylar_invalid_auth"
            if resp.status != 200:
                return "mylar_cannot_connect"
            body = await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as err:
        return _error_key("mylar", url, err)
    if not isinstance(body, dict) or "success" not in body:
        return "not_mylar"
    if body["success"]:
        return None if isinstance(body.get("data"), dict) and "current_version" in body["data"] else "not_mylar"
    code = (body.get("error") or {}).get("code") if isinstance(body.get("error"), dict) else None
    return "mylar_invalid_auth" if code == 460 else "not_mylar"


async def _validate(hass: HomeAssistant, data: dict) -> tuple[dict, dict[str, str]]:
    data = {**data}
    errors: dict[str, str] = {}
    for key in (CONF_CHAPTARR_URL, CONF_ABS_URL):
        data[key] = data[key].strip().rstrip("/")
        if not data[key].startswith(("http://", "https://")):
            errors[key] = "invalid_url"
    data[CONF_NOTIFY_SERVICE] = (data.get(CONF_NOTIFY_SERVICE) or "").strip()
    data[CONF_KOMGA_URL] = (data.get(CONF_KOMGA_URL) or "").strip().rstrip("/")
    data[CONF_KOMGA_API_KEY] = (data.get(CONF_KOMGA_API_KEY) or "").strip()
    if data[CONF_KOMGA_URL] and not data[CONF_KOMGA_URL].startswith(("http://", "https://")):
        errors[CONF_KOMGA_URL] = "invalid_url"
    elif data[CONF_KOMGA_URL] and not data[CONF_KOMGA_API_KEY]:
        errors[CONF_KOMGA_API_KEY] = "komga_key_missing"
    elif data[CONF_KOMGA_API_KEY] and not data[CONF_KOMGA_URL]:
        errors[CONF_KOMGA_URL] = "komga_url_missing"
    data[CONF_MYLAR_URL] = (data.get(CONF_MYLAR_URL) or "").strip().rstrip("/")
    data[CONF_MYLAR_API_KEY] = (data.get(CONF_MYLAR_API_KEY) or "").strip()
    if data[CONF_MYLAR_URL] and not data[CONF_MYLAR_URL].startswith(("http://", "https://")):
        errors[CONF_MYLAR_URL] = "invalid_url"
    elif data[CONF_MYLAR_URL] and not data[CONF_MYLAR_API_KEY]:
        errors[CONF_MYLAR_API_KEY] = "mylar_key_missing"
    elif data[CONF_MYLAR_API_KEY] and not data[CONF_MYLAR_URL]:
        errors[CONF_MYLAR_URL] = "mylar_url_missing"
    data[CONF_TOLINO_URL] = (data.get(CONF_TOLINO_URL) or "").strip().rstrip("/")
    data[CONF_TOLINO_TOKEN] = (data.get(CONF_TOLINO_TOKEN) or "").strip()
    if data[CONF_TOLINO_URL] and not data[CONF_TOLINO_URL].startswith(("http://", "https://")):
        errors[CONF_TOLINO_URL] = "invalid_url"
    elif data[CONF_TOLINO_URL] and not data[CONF_TOLINO_TOKEN]:
        errors[CONF_TOLINO_TOKEN] = "tolino_token_missing"
    elif data[CONF_TOLINO_TOKEN] and not data[CONF_TOLINO_URL]:
        errors[CONF_TOLINO_URL] = "tolino_url_missing"
    # The tolino features silently do nothing without their prerequisites - say so in the form instead.
    wanted = [k for k in (CONF_AUTO_SEND, CONF_SYNC_PROGRESS, CONF_SYNC_PROGRESS_WRITE) if data.get(k)]
    if wanted and not data[CONF_TOLINO_URL]:
        for key in wanted:
            errors.setdefault(key, "tolino_bridge_required")
    if data.get(CONF_SYNC_PROGRESS_WRITE) and not data.get(CONF_SYNC_PROGRESS):
        errors.setdefault(CONF_SYNC_PROGRESS_WRITE, "sync_progress_required")
    if errors:
        return data, errors
    verify = data[CONF_VERIFY_SSL]
    if err := await _check_chaptarr(hass, data[CONF_CHAPTARR_URL], data[CONF_CHAPTARR_API_KEY].strip(), verify):
        errors[CONF_CHAPTARR_API_KEY if "auth" in err else CONF_CHAPTARR_URL] = err
    if err := await _check_abs(hass, data[CONF_ABS_URL], data[CONF_ABS_TOKEN].strip(), verify):
        errors[CONF_ABS_TOKEN if "auth" in err else CONF_ABS_URL] = err
    if data[CONF_KOMGA_URL]:
        if err := await _check_komga(hass, data[CONF_KOMGA_URL], data[CONF_KOMGA_API_KEY], verify):
            errors[CONF_KOMGA_API_KEY if "auth" in err else CONF_KOMGA_URL] = err
    if data[CONF_MYLAR_URL]:
        if err := await _check_mylar(hass, data[CONF_MYLAR_URL], data[CONF_MYLAR_API_KEY], verify):
            errors[CONF_MYLAR_API_KEY if "auth" in err else CONF_MYLAR_URL] = err
    if data[CONF_TOLINO_URL]:
        if err := await _check_tolino(hass, data[CONF_TOLINO_URL], data[CONF_TOLINO_TOKEN], verify):
            errors[CONF_TOLINO_TOKEN if "auth" in err else CONF_TOLINO_URL] = err
    return data, errors


class BooksConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Set up the Chaptarr + Audiobookshelf connection."""

    VERSION = 1

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_USER: UserSubentryFlow}

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


_ACCOUNT_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")        # what the bridge accepts as an account name


# ── One person: their Home Assistant user and their own accounts ───────────────────────────────────

class UserSubentryFlow(ConfigSubentryFlow):
    """"Person hinzufügen": a Home Assistant user, their Komga key, Audiobookshelf token, notify target and Tolino switch.

    Empty Komga key / token = this person keeps using the shared account from the main settings."""

    async def async_step_user(self, user_input: dict | None = None) -> SubentryFlowResult:
        return await self._form("user", user_input)

    async def async_step_reconfigure(self, user_input: dict | None = None) -> SubentryFlowResult:
        return await self._form("reconfigure", user_input)

    async def _people(self, keep: str | None) -> list[SelectOptionDict]:
        taken = {sub.data.get(CONF_HA_USER) for sub in self._get_entry().subentries.values()} - {keep}
        users = await self.hass.auth.async_get_users()
        return [SelectOptionDict(value=u.id, label=u.name or u.id)
                for u in users if u.is_active and not u.system_generated and u.id not in taken]

    async def _bridge_accounts(self, entry) -> list[str] | None:
        """The Thalia accounts the bridge knows (None: no bridge configured or it does not answer right now)."""
        if not entry.data.get(CONF_TOLINO_URL):
            return None
        try:
            status = await TolinoBridgeClient(self.hass, entry.data).get("/status")
        except (UpstreamError, aiohttp.ClientError, TimeoutError):
            return None
        names = status.get("accounts") if isinstance(status, dict) else None
        return [str(n) for n in names] if isinstance(names, list) else [DEFAULT_TOLINO_ACCOUNT]   # an older bridge: just the default

    async def _form(self, step: str, user_input: dict | None) -> SubentryFlowResult:
        entry = self._get_entry()
        sub = self._get_reconfigure_subentry() if step == "reconfigure" else None
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = await self._validate(entry, sub, user_input)
            if not errors:
                if data.pop("notify_test", False) and data.get(CONF_NOTIFY_SERVICE):
                    await async_push(self.hass, "Books", "Testnachricht: so kommen Benachrichtigungen bei dir an.", targets=data[CONF_NOTIFY_SERVICE])
                name = data.pop("_title")
                if sub is not None:
                    return self.async_update_and_abort(entry, sub, title=name, data=data)
                return self.async_create_entry(title=name, data=data, unique_id=data[CONF_HA_USER])
        shown = {**(sub.data if sub else {}), **(user_input or {})}
        schema: dict = {}
        accounts = await self._bridge_accounts(entry)
        if sub is None:
            options = await self._people(None)
            if not options:
                return self.async_abort(reason="no_users_left")
            schema[vol.Required(CONF_HA_USER, default=shown.get(CONF_HA_USER, vol.UNDEFINED))] = SelectSelector(
                SelectSelectorConfig(options=options, mode="dropdown"))
        schema.update({
            vol.Optional(CONF_KOMGA_API_KEY, description={"suggested_value": shown.get(CONF_KOMGA_API_KEY, "")}): _PASSWORD,
            vol.Optional(CONF_ABS_TOKEN, description={"suggested_value": shown.get(CONF_ABS_TOKEN, "")}): _PASSWORD,
            vol.Optional(CONF_NOTIFY_SERVICE, description={"suggested_value": shown.get(CONF_NOTIFY_SERVICE, "")}): str,
            vol.Required(CONF_USER_TOLINO, default=bool(shown.get(CONF_USER_TOLINO, False))): bool,
            vol.Optional(CONF_TOLINO_ACCOUNT, description={"suggested_value": shown.get(CONF_TOLINO_ACCOUNT, "")}): (
                SelectSelector(SelectSelectorConfig(options=[SelectOptionDict(value=n, label=n) for n in accounts],
                                                    custom_value=True, mode="dropdown")) if accounts else str),
            vol.Required(CONF_AUTO_SEND, default=bool(shown.get(CONF_AUTO_SEND, False))): bool,
            vol.Required(CONF_SYNC_PROGRESS, default=bool(shown.get(CONF_SYNC_PROGRESS, False))): bool,
            vol.Required(CONF_SYNC_PROGRESS_WRITE, default=bool(shown.get(CONF_SYNC_PROGRESS_WRITE, False))): bool,
            vol.Required("notify_test", default=False): bool,
        })
        placeholders = {}
        if sub is not None:
            people = {u.id: u.name for u in await self.hass.auth.async_get_users()}
            placeholders["person"] = people.get(sub.data.get(CONF_HA_USER), sub.title)
        return self.async_show_form(step_id=step, data_schema=vol.Schema(schema), errors=errors, description_placeholders=placeholders)

    async def _validate(self, entry, sub, user_input: dict) -> tuple[dict, dict[str, str]]:
        cfg = entry.data
        verify = cfg.get(CONF_VERIFY_SSL, True)
        data = {
            CONF_HA_USER: sub.data[CONF_HA_USER] if sub else user_input[CONF_HA_USER],
            CONF_KOMGA_API_KEY: (user_input.get(CONF_KOMGA_API_KEY) or "").strip(),
            CONF_ABS_TOKEN: (user_input.get(CONF_ABS_TOKEN) or "").strip(),
            CONF_NOTIFY_SERVICE: (user_input.get(CONF_NOTIFY_SERVICE) or "").strip(),
            CONF_USER_TOLINO: bool(user_input.get(CONF_USER_TOLINO)),
            CONF_TOLINO_ACCOUNT: (user_input.get(CONF_TOLINO_ACCOUNT) or "").strip(),
            CONF_AUTO_SEND: bool(user_input.get(CONF_AUTO_SEND)),
            CONF_SYNC_PROGRESS: bool(user_input.get(CONF_SYNC_PROGRESS)),
            CONF_SYNC_PROGRESS_WRITE: bool(user_input.get(CONF_SYNC_PROGRESS_WRITE)),
            "notify_test": bool(user_input.get("notify_test")),
            CONF_KOMGA_NAME: "", CONF_ABS_NAME: "",
        }
        errors: dict[str, str] = {}
        if data[CONF_KOMGA_API_KEY]:
            if not cfg.get(CONF_KOMGA_URL):
                errors[CONF_KOMGA_API_KEY] = "komga_not_configured"
            else:
                err, who = await _komga_identity(self.hass, cfg[CONF_KOMGA_URL], data[CONF_KOMGA_API_KEY], verify)
                if err:
                    errors[CONF_KOMGA_API_KEY] = err
                data[CONF_KOMGA_NAME] = who
        if data[CONF_ABS_TOKEN]:
            err, who = await _abs_identity(self.hass, cfg[CONF_ABS_URL], data[CONF_ABS_TOKEN], verify)
            if err:
                errors[CONF_ABS_TOKEN] = err
            data[CONF_ABS_NAME] = who
        if data[CONF_USER_TOLINO] and not cfg.get(CONF_TOLINO_URL):
            errors[CONF_USER_TOLINO] = "tolino_bridge_required"
        # Every person with a Tolino has their own Thalia account in the bridge (an empty name = the default account).
        mine = sub.subentry_id if sub else None
        account = data[CONF_TOLINO_ACCOUNT] or DEFAULT_TOLINO_ACCOUNT
        if data[CONF_USER_TOLINO]:
            if account != DEFAULT_TOLINO_ACCOUNT and not _ACCOUNT_NAME.match(account):
                errors[CONF_TOLINO_ACCOUNT] = "tolino_account_invalid"
            elif any(o.data.get(CONF_USER_TOLINO) and (o.data.get(CONF_TOLINO_ACCOUNT) or DEFAULT_TOLINO_ACCOUNT) == account
                     for sid, o in entry.subentries.items() if sid != mine):
                errors[CONF_TOLINO_ACCOUNT] = "tolino_account_taken"
            elif (known := await self._bridge_accounts(entry)) is not None and account not in known:
                errors[CONF_TOLINO_ACCOUNT] = "tolino_account_unknown"
        elif data[CONF_TOLINO_ACCOUNT]:
            errors[CONF_TOLINO_ACCOUNT] = "tolino_person_required"
        for key in (CONF_AUTO_SEND, CONF_SYNC_PROGRESS, CONF_SYNC_PROGRESS_WRITE):
            if data[key] and not data[CONF_USER_TOLINO]:
                errors[key] = "tolino_person_required"
        if data[CONF_SYNC_PROGRESS_WRITE] and not data[CONF_SYNC_PROGRESS]:
            errors.setdefault(CONF_SYNC_PROGRESS_WRITE, "sync_progress_required")
        if data[CONF_NOTIFY_SERVICE] and unknown_targets(self.hass, data[CONF_NOTIFY_SERVICE]):
            errors[CONF_NOTIFY_SERVICE] = "notify_unknown"
        people = {u.id: u.name for u in await self.hass.auth.async_get_users()}
        data["_title"] = people.get(data[CONF_HA_USER]) or data[CONF_HA_USER]
        return data, errors
