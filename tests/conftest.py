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


@pytest.fixture(autouse=True)
def _create_the_admin_early(request):
    """Entry fixtures run before `hass_client` creates its user, but the person must be exactly that user."""
    if "hass" in request.fixturenames:
        request.getfixturevalue("hass_admin_user")


def person(user, **kw):
    """A person (config subentry) for a Home Assistant user: every user of the cards needs one."""
    from homeassistant.config_entries import ConfigSubentryData
    return ConfigSubentryData(subentry_type="user", title=user.name, unique_id=user.id, data={
        "ha_user": user.id, "komga_api_key": "", "abs_token": "", "notify_service": "", "tolino": False, "tolino_account": "",
        "auto_send": False, "sync_progress": False, "sync_progress_write": False, **kw})


async def admin_person(hass, **kw):
    """A person for the test administrator (the user behind `hass_client`; created when no such user exists yet)."""
    users = [u for u in await hass.auth.async_get_users() if u.is_active and not u.system_generated and u.is_admin]
    user = users[0] if users else await hass.auth.async_create_user("Tester", group_ids=["system-admin"])
    return person(user, **kw)


@pytest.fixture
def entry(hass_admin_user) -> MockConfigEntry:
    """The test client (`hass_client`) is the administrator: it is a person too."""
    return MockConfigEntry(domain=DOMAIN, data=dict(ENTRY_DATA), title="Books", subentries_data=[person(hass_admin_user)])


@pytest.fixture
async def setup_entry(hass, entry):
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry
