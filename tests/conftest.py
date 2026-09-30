"""Shared fixtures for the Books integration tests."""
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.books.const import (
    CONF_ABS_TOKEN,
    CONF_ABS_URL,
    CONF_CHAPTARR_API_KEY,
    CONF_CHAPTARR_URL,
    CONF_DEBUG_LOGGING,
    CONF_NOTIFY_SERVICE,
    CONF_RESCUE_IMPORTS,
    CONF_VERIFY_SSL,
    DOMAIN,
)

CHAPTARR = "http://chaptarr.test"
ABS = "http://abs.test"

ENTRY_DATA = {
    CONF_CHAPTARR_URL: CHAPTARR,
    CONF_CHAPTARR_API_KEY: "chaptarr-key",
    CONF_ABS_URL: ABS,
    CONF_ABS_TOKEN: "abs-token",
    CONF_VERIFY_SSL: True,
    CONF_RESCUE_IMPORTS: True,
    CONF_NOTIFY_SERVICE: "",
    CONF_DEBUG_LOGGING: False,
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def entry() -> MockConfigEntry:
    return MockConfigEntry(domain=DOMAIN, data=dict(ENTRY_DATA), title="Books")


@pytest.fixture
async def setup_entry(hass, entry):
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry
