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
