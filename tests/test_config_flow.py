"""Config flow: validation of both services, per-field errors, reconfigure."""
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from custom_components.books.const import DOMAIN

from .conftest import ABS, CHAPTARR, ENTRY_DATA

USER_INPUT = {**ENTRY_DATA, "chaptarr_url": CHAPTARR + "/", "notify_service": " notify.mobile_app_iphone "}


def _ok(aioclient_mock):
    aioclient_mock.get(f"{CHAPTARR}/api/v1/system/status", json={"appName": "Chaptarr", "version": "1.0.0"})
    aioclient_mock.get(f"{ABS}/api/me", json={"username": "homeassistant"})


async def _start(hass):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})


async def test_user_flow_success(hass, aioclient_mock):
    _ok(aioclient_mock)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["chaptarr_url"] == CHAPTARR  # trailing slash stripped
    assert result["data"]["notify_service"] == "notify.mobile_app_iphone"
    headers = {str(c[1]): c[3] for c in aioclient_mock.mock_calls}
    assert headers[f"{CHAPTARR}/api/v1/system/status"]["X-Api-Key"] == "chaptarr-key"
    assert headers[f"{ABS}/api/me"]["Authorization"] == "Bearer abs-token"


async def test_user_flow_bad_chaptarr_key(hass, aioclient_mock):
    aioclient_mock.get(f"{CHAPTARR}/api/v1/system/status", status=401)
    aioclient_mock.get(f"{ABS}/api/me", json={})
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"chaptarr_api_key": "chaptarr_invalid_auth"}


async def test_user_flow_wrong_app_and_bad_abs_token(hass, aioclient_mock):
    aioclient_mock.get(f"{CHAPTARR}/api/v1/system/status", json={"appName": "Readarr"})
    aioclient_mock.get(f"{ABS}/api/me", status=401)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["errors"] == {"chaptarr_url": "not_chaptarr", "abs_token": "abs_invalid_auth"}


async def test_user_flow_invalid_url(hass, aioclient_mock):
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, "abs_url": "abs.example"}
    )
    assert result["errors"] == {"abs_url": "invalid_url"}
    assert aioclient_mock.call_count == 0


async def test_user_flow_cannot_connect(hass, aioclient_mock):
    import aiohttp
    aioclient_mock.get(f"{CHAPTARR}/api/v1/system/status", exc=aiohttp.ClientConnectionError())
    aioclient_mock.get(f"{ABS}/api/me", exc=TimeoutError())
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert set(result["errors"].values()) <= {"chaptarr_cannot_connect", "abs_cannot_connect", "unknown"}
    assert result["errors"]["abs_url"] == "abs_cannot_connect"


async def test_reconfigure(hass, setup_entry, aioclient_mock):
    _ok(aioclient_mock)
    result = await setup_entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**ENTRY_DATA, "rescue_imports": False}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert setup_entry.data["rescue_imports"] is False
    assert hass.data[DOMAIN]["config"]["rescue_imports"] is False
