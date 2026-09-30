# Books Integration for Home Assistant (Chaptarr + Audiobookshelf)

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2024.8%2B-brightgreen.svg)](https://www.home-assistant.io)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A secure, server-side proxy between the [Books Card](https://github.com/hypersonic30/ha-books-card),
[Chaptarr](https://github.com/Chaptarr/chaptarr) (ebook/audiobook manager, Readarr successor) and
[Audiobookshelf](https://www.audiobookshelf.org/) (library, reader, player).

> [!IMPORTANT]
> Two components, both required:
> - **Books Integration** (this repo) — backend proxy, install first
> - **[Books Card](https://github.com/hypersonic30/ha-books-card)** — the Lovelace card

```
books-card  →  Home Assistant (your HA login)  →  this integration  →  Chaptarr (API key)
                                                                   →  Audiobookshelf (token)
```

The Chaptarr API key and the Audiobookshelf token stay in Home Assistant; the card only
uses your normal Home Assistant session. The integration creates **no entities**.

## Features

- **Proxy for Chaptarr** — search, add, queue, history, calendar, interactive release search.
  Chaptarr's *settings* (indexers, download clients, profiles, system) are deliberately **not**
  reachable through Home Assistant; they stay in Chaptarr's own UI. Only harmless commands
  (book/author search, refresh, RSS sync) can be triggered.
- **Proxy for Audiobookshelf** — libraries, items, covers, playback sessions and progress.
  Audio files and EPUBs are **streamed** with HTTP Range support (seeking in 30-hour M4Bs works),
  and `<audio>`/`<img>` tags authenticate via Home Assistant signed URLs.
- **"Only this book" add** — `POST /api/books/add` adds a book as ebook and/or audiobook and
  monitors **only that book**, with no automatic monitoring of the author's future releases,
  explicitly for *each* media type. (Chaptarr's own dialog silently falls back to "All books"
  for the second media type when both are added at once.)
- **Automatic import repair** — German editions often name the series like book 1
  ("Die Chroniken von Alsea 02 - Der Sturm"), so Chaptarr matches book 2's file to
  book 1 and blocks the import with *"Completed download was grabbed for X, but import matched Y"*.
  Every 2 minutes the integration repairs exactly this case by importing the file into the book
  it was grabbed for — but only when unambiguous (same author, same media type, no existing file,
  an allowed file format, one book per grab). Anything else is left alone; each download gets one
  attempt; a failed attempt creates a Home Assistant notification and, optionally, a push
  notification to notify entities or services.

## Installation

### HACS (recommended)
1. HACS → ⋮ → Custom repositories → add this repo's URL, category **Integration**.
2. Install **Books (Chaptarr + Audiobookshelf)**, restart Home Assistant.

### Manual
Copy `custom_components/books/` into `config/custom_components/` and restart Home Assistant.

## Setup

Settings → Devices & Services → Add Integration → **Books**:

| Field | Value |
|---|---|
| Chaptarr URL | e.g. `http://192.168.1.10:8789` |
| Chaptarr API key | Chaptarr → Settings → General → API Key |
| Audiobookshelf URL | e.g. `http://192.168.1.10:13378` |
| Audiobookshelf API token | token of a **dedicated, restricted** Audiobookshelf user (Settings → Users → create a user without upload/delete/update rights) — not the admin |
| Verify SSL | disable only for self-signed certificates |
| Automatically repair blocked imports | see above (default on) |
| Notification target | optional: a notify entity (e.g. `notify.iphone`, as used by `notify.send_message`) or a legacy notify service (e.g. `notify.mobile_app_iphone`); several separated by commas — told when a repair fails |

Change anything later with the integration's **Reconfigure** action; it applies immediately.

### Recommended Chaptarr / Audiobookshelf setup
- In Chaptarr, add the **AudioBookShelf** connection (Settings → Connect) with an Audiobookshelf
  *admin* API key and map the root folders to libraries, so Audiobookshelf rescans after every
  import. On Unraid, filesystem change events often don't reach Audiobookshelf otherwise.
- Enable "write Audiobookshelf metadata.json / cover" on Chaptarr's root folders.
- Make sure both containers see the **same host folders** for the library.

## API (used by the card)

| Endpoint | Upstream |
|---|---|
| `/api/books/chaptarr/{path}` | Chaptarr `/api/v1/{path}` |
| `/api/books/chaptarr-media/{MediaCover…}` | Chaptarr cached covers |
| `/api/books/abs/{path}` | Audiobookshelf `/api/{path}` (streamed) |
| `POST /api/books/add` | adds a search result as ebook/audiobook, "only this book" |
| `GET /api/books/rescue` | recent import-repair events |

All endpoints require a Home Assistant login.

## Development

```bash
uv venv --python 3.14 .venv && uv pip install --python .venv/bin/python pytest-homeassistant-custom-component
.venv/bin/python -m pytest
```
