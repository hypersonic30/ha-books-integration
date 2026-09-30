"""Small async clients for Chaptarr and Audiobookshelf, shared by views and the import rescue."""
from __future__ import annotations

from json import loads as json_loads
from typing import Any

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_ABS_TOKEN,
    CONF_ABS_URL,
    CONF_CHAPTARR_API_KEY,
    CONF_CHAPTARR_URL,
    CONF_TOLINO_TOKEN,
    CONF_TOLINO_URL,
    CONF_VERIFY_SSL,
    DOMAIN,
    REQUEST_TIMEOUT,
)


class UpstreamError(Exception):
    """Raised when Chaptarr/Audiobookshelf answers with an error status."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


def get_config(hass: HomeAssistant) -> dict:
    """The live config entry data — updated on reconfigure without a restart."""
    return hass.data.get(DOMAIN, {}).get("config", {})


class _Client:
    def __init__(self, hass: HomeAssistant, base_url: str, headers: dict, verify_ssl: bool) -> None:
        self._hass = hass
        self.base_url = base_url.rstrip("/")
        self.headers = headers
        self._verify_ssl = verify_ssl

    @property
    def session(self) -> aiohttp.ClientSession:
        return async_get_clientsession(self._hass, verify_ssl=self._verify_ssl)

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        data: Any = None,
        params: dict | None = None,
        timeout: float = REQUEST_TIMEOUT,
    ) -> Any:
        async with self.session.request(
            method,
            f"{self.base_url}{path}",
            headers=self.headers,
            json=json,
            data=data,
            params=params,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            body = await resp.read()
            if resp.status >= 400:
                raise UpstreamError(resp.status, body[:500].decode(errors="replace"))
            if not body:
                return None
            text = body.decode(errors="replace")
            if "json" in (resp.content_type or "") or text.lstrip()[:1] in ("{", "["):
                try:
                    return json_loads(text)
                except ValueError:
                    pass
            return text

    async def fetch_bytes(self, path: str, *, max_bytes: int, timeout: float) -> bytes:
        """GET a binary body, refusing anything larger than `max_bytes`."""
        async with self.session.get(
            f"{self.base_url}{path}", headers=self.headers, timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status >= 400:
                raise UpstreamError(resp.status, (await resp.read())[:500].decode(errors="replace"))
            declared = resp.headers.get("Content-Length", "")
            if declared.isdigit() and int(declared) > max_bytes:
                raise UpstreamError(413, "file too large")
            body = bytearray()
            async for chunk in resp.content.iter_chunked(256 * 1024):
                body += chunk
                if len(body) > max_bytes:
                    raise UpstreamError(413, "file too large")
            return bytes(body)

    async def get(self, path: str, **kwargs: Any) -> Any:
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, json: Any = None, **kwargs: Any) -> Any:
        return await self.request("POST", path, json=json, **kwargs)


class ChaptarrClient(_Client):
    """Chaptarr /api/v1 with X-Api-Key auth."""

    def __init__(self, hass: HomeAssistant, cfg: dict) -> None:
        super().__init__(
            hass,
            f"{cfg.get(CONF_CHAPTARR_URL, '').rstrip('/')}/api/v1",
            {"X-Api-Key": cfg.get(CONF_CHAPTARR_API_KEY, "")},
            cfg.get(CONF_VERIFY_SSL, True),
        )


class AbsClient(_Client):
    """Audiobookshelf /api with bearer-token auth."""

    def __init__(self, hass: HomeAssistant, cfg: dict) -> None:
        super().__init__(
            hass,
            f"{cfg.get(CONF_ABS_URL, '').rstrip('/')}/api",
            {"Authorization": f"Bearer {cfg.get(CONF_ABS_TOKEN, '')}"},
            cfg.get(CONF_VERIFY_SSL, True),
        )


class TolinoBridgeClient(_Client):
    """tolino-bridge (Tolino Cloud upload service) with bearer-token auth."""

    def __init__(self, hass: HomeAssistant, cfg: dict) -> None:
        super().__init__(
            hass,
            (cfg.get(CONF_TOLINO_URL) or "").rstrip("/"),
            {"Authorization": f"Bearer {cfg.get(CONF_TOLINO_TOKEN, '')}"},
            cfg.get(CONF_VERIFY_SSL, True),
        )

    @property
    def configured(self) -> bool:
        return bool(self.base_url) and self.headers["Authorization"] != "Bearer "
