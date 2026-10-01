"""Every field of the setup/reconfigure form needs a label AND a description in every language (a missing one shows up
as the raw key in Home Assistant, e.g. 'sync_progress_write')."""
import json
from pathlib import Path

import pytest
from homeassistant.helpers.translation import async_get_translations

from custom_components.books.config_flow import _schema
from custom_components.books.const import DOMAIN

BASE = Path(__file__).parent.parent / "custom_components" / "books"
FILES = ["strings.json", "translations/en.json", "translations/de.json"]
FIELDS = [str(k) for k in _schema({}).schema]


@pytest.mark.parametrize("file", FILES)
@pytest.mark.parametrize("step", ["user", "reconfigure"])
def test_every_field_has_label_and_description(file, step):
    section = json.loads((BASE / file).read_text())["config"]["step"][step]
    for field in FIELDS:
        assert section["data"].get(field, "").strip(), f"{file} {step}: no label for {field!r}"
        assert section["data_description"].get(field, "").strip(), f"{file} {step}: no description for {field!r}"
    assert set(section["data"]) == set(FIELDS), f"{file} {step}: labels for fields that do not exist or are missing"


@pytest.mark.parametrize("file", FILES)
def test_every_error_key_used_by_the_flow_is_translated(file):
    src = (BASE / "config_flow.py").read_text()
    src = src[:src.index("class UserSubentryFlow")]            # the person form has its own errors (and test) below
    import re
    used = set(re.findall(r'"((?:chaptarr|abs|tolino|komga|mylar|sync_progress)_[a-z_]+|invalid_url|not_chaptarr|not_komga|not_mylar|not_tolino_bridge|ssl_error|unknown)"', src))
    errors = json.loads((BASE / file).read_text())["config"]["error"]
    missing = {k for k in used if k in {"invalid_url", "not_chaptarr", "not_komga", "not_mylar", "not_tolino_bridge", "ssl_error", "unknown"} or k.endswith(("_cannot_connect", "_invalid_auth", "_missing", "_required"))} - set(errors)
    assert not missing, f"{file}: untranslated errors {missing}"


@pytest.mark.parametrize("language", ["en", "de"])
async def test_home_assistant_loads_the_new_labels(hass, language):
    loaded = await async_get_translations(hass, language, "config", {DOMAIN})
    for step in ("user", "reconfigure"):
        for field in FIELDS:
            assert loaded.get(f"component.{DOMAIN}.config.step.{step}.data.{field}"), f"{language}: HA has no label for {field} ({step})"
            assert loaded.get(f"component.{DOMAIN}.config.step.{step}.data_description.{field}"), f"{language}: HA has no description for {field} ({step})"


# --- the "person" form (config subentry) ----------------------------------------------------------------

PERSON_FIELDS = ["ha_user", "komga_api_key", "abs_token", "notify_service", "tolino", "tolino_account", "auto_send", "sync_progress", "sync_progress_write", "notify_test"]


@pytest.mark.parametrize("file", FILES)
@pytest.mark.parametrize("step", ["user", "reconfigure"])
def test_person_form_has_label_and_description_for_every_field(file, step):
    sub = json.loads((BASE / file).read_text())["config_subentries"]["user"]
    section = sub["step"][step]
    fields = [f for f in PERSON_FIELDS if not (step == "reconfigure" and f == "ha_user")]
    for field in fields:
        assert section["data"].get(field, "").strip(), f"{file} {step}: no label for {field!r}"
        assert section["data_description"].get(field, "").strip(), f"{file} {step}: no description for {field!r}"
    assert set(section["data"]) == set(fields) == set(section["data_description"])
    assert sub["entry_type"] and sub["initiate_flow"]["user"], f"{file}: the integration page needs a name and an 'add' button text"


@pytest.mark.parametrize("file", FILES)
def test_every_error_of_the_person_form_is_translated(file):
    import re
    src = (BASE / "config_flow.py").read_text()
    flow = src[src.index("class UserSubentryFlow"):]
    used = set(re.findall(r'"((?:komga|abs)_[a-z_]+|not_komga|tolino_bridge_required|tolino_person_required|tolino_account_unknown|tolino_account_taken|tolino_account_invalid|sync_progress_required|notify_unknown)"', flow))
    used |= {"komga_cannot_connect", "abs_cannot_connect", "ssl_error"}                  # produced by the shared _error_key helper
    errors = json.loads((BASE / file).read_text())["config_subentries"]["user"]["error"]
    assert not used - set(errors), f"{file}: untranslated {used - set(errors)}"
    abort = json.loads((BASE / file).read_text())["config_subentries"]["user"]["abort"]
    assert {"no_users_left", "already_configured", "reconfigure_successful"} <= set(abort)


@pytest.mark.parametrize("language", ["en", "de"])
async def test_home_assistant_loads_the_person_form_texts(hass, language):
    loaded = await async_get_translations(hass, language, "config_subentries", {DOMAIN})
    for field in PERSON_FIELDS:
        assert loaded.get(f"component.{DOMAIN}.config_subentries.user.step.user.data.{field}"), f"{language}: no label for {field}"
        assert loaded.get(f"component.{DOMAIN}.config_subentries.user.step.user.data_description.{field}"), f"{language}: no description for {field}"
