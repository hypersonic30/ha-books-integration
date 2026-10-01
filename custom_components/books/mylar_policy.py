"""What the manga card may ask Mylar3. Mylar's API is `GET /api?cmd=<command>&apikey=...` - everything, including
deleting series, changing indexers/providers, reading the log or shutting the server down, goes through that one URL.
So the allow-list is per command and per parameter; anything else is refused before Mylar sees it."""
from __future__ import annotations

import re

_ID = re.compile(r"^[A-Za-z0-9-]{1,24}$")
_NAME = re.compile(r"^[^\x00-\x1f\x7f]{1,100}$")

# command -> (HTTP method the card must use, {parameter: validator}, run in the background)
_COMMANDS: dict[str, tuple[str, dict[str, re.Pattern], bool]] = {
    "findComic": ("GET", {"name": _NAME}, False),
    "getIndex": ("GET", {}, False),
    "getComic": ("GET", {"id": _ID}, False),
    "getWanted": ("GET", {}, False),
    "getHistory": ("GET", {}, False),
    "addComic": ("POST", {"id": _ID}, False),
    # Both search every indexer and Mylar answers only when done (minutes) - the proxy must not wait for them.
    "queueIssue": ("POST", {"id": _ID}, True),
    "forceSearch": ("POST", {}, True),
    "unqueueIssue": ("POST", {"id": _ID}, False),
    "pauseComic": ("POST", {"id": _ID}, False),
    "resumeComic": ("POST", {"id": _ID}, False),
}


def mylar_request(method: str, cmd: str, query) -> tuple[dict[str, str], bool] | None:
    """Validated upstream parameters (without the key) and whether to run in the background; None = not allowed."""
    spec = _COMMANDS.get(cmd)
    if spec is None or spec[0] != method.upper():
        return None
    params = {k: v for k, v in query.items() if k != "authSig"}  # authSig is Home Assistant's own
    if set(params) != set(spec[1]) or not all(rx.match(params[k]) for k, rx in spec[1].items()):
        return None
    return {"cmd": cmd, **params}, spec[2]
