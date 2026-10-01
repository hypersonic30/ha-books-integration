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
- **Send to Tolino** — `POST /api/books/tolino` uploads an Audiobookshelf ebook (EPUB/PDF) to the
  Tolino Cloud through the optional [tolino-bridge](https://github.com/hypersonic30/tolino-bridge)
  service; the book then shows up in the tolino app on iOS and Android after a sync. Home Assistant
  fetches the file from Audiobookshelf itself, so neither the file nor the bridge token passes
  through the browser. Books that were already sent are remembered (a second tap asks before replacing the
  cloud copy — never a silent duplicate), and a **`binary_sensor.tolino_bridge_problem`** turns on when the
  bridge is unreachable or not logged in at Thalia; after two bad polls (10 min) you get a persistent
  notification and, if configured, a push — and one all-clear when it recovers.

### Sensors and events (with a tolino bridge)

| Entity / event | What it tells you |
|---|---|
| `binary_sensor` *tolino-Bridge Problem* | on when the bridge is unreachable or not logged in at Thalia |
| `sensor` *Last progress sync* | timestamp of the last reading-progress run; attributes `imported`, `exported`, `skipped` (that run), `enabled`, `write_enabled` |
| `sensor` *Last auto-sent book* | title of the book auto-sent last (survives restarts); attributes `sent_at`, `total_sent`, `given_up`, `enabled` |
| event `books_tolino_sent` | a book reached the tolino Cloud: `item_id`, `title`, `filename`, `deliverable_id`, `replaced`, `auto` (true = auto-send, false = the card's button) |
| event `books_tolino_progress_synced` | a reading state was carried over: `item_id`, `direction` (`tolino_to_abs` / `abs_to_tolino`), `finished` |

Use the events for automations, e.g. a push "New book is on your tolino – sync the app" when `books_tolino_sent` fires with `auto: true`.

### Manga (optional): Komga

With a [Komga](https://komga.org) URL and API key in the settings, `/api/books/komga/{path}` proxies Komga for the manga card.
It is a strict allow-list — reading (libraries, series, books, page images, thumbnails), reading progress, search and "rescan a
library" — and refuses everything else (users, API keys, library settings, original-file downloads). The API key never reaches
the browser; use a dedicated Komga user **without** admin rights. Page images work as `<img src>` through Home Assistant's signed paths.

### Manga downloads (optional): Mylar3

With a [Mylar3](https://github.com/mylar3/mylar3) URL and API key (Mylar → Settings → Web Interface → API, enable it first),
`/api/books/mylar/{command}` lets the manga card search ComicVine, add series and queue single volumes. Mylar's API is one URL
for everything (including deleting series, changing indexers, the log and shutdown), so the allow-list is per command **and** per
parameter: `findComic`, `getIndex`, `getComic`, `getWanted`, `getHistory` (GET) and `addComic`, `queueIssue`, `unqueueIssue`,
`pauseComic`, `resumeComic`, `forceSearch` (POST). Everything else is refused with 403. `queueIssue` and `forceSearch` only answer
when every indexer has been asked (over a minute was seen), so the proxy answers `202` at once and lets Mylar finish in the
background; the card follows progress through `getWanted`/`getHistory`.

### People: one account each (optional)

By default everybody who uses the cards reads with the **same** Komga/Audiobookshelf account - so one person sees what another reads and
reading the same book overwrites the other's progress. Give everybody their own accounts instead:

1. Create a Komga user (no admin rights) and an Audiobookshelf user for each person, and note their API key / token.
2. Settings → Devices & Services → Books → **Add person**. Pick the Home Assistant user, paste their Komga key and/or Audiobookshelf token
   (empty = that person keeps using the shared account), optionally a notify target and whether they use the Tolino bridge. The keys are
   checked when you save and the name of the account they belong to is stored with the person.
3. People can be added, edited and removed at any time, no restart. Everybody without an entry keeps the shared account.

What changes for a person with their own accounts: their own reading progress, bookmarks and "Weiterlesen" in the Books and Manga cards;
**"An tolino senden" only for people marked as Tolino users**, each with **their own Thalia account in the bridge** (the person's
"Tolino account" = the name from `deploy.sh account add <name>`; empty = the bridge's default account; the form offers the accounts the bridge
knows and refuses one that is unknown or already used). Everything runs per account: the list of sent books, auto-send, taking reading progress
from tolino and sending it to tolino (switches of the person; the matching switches in the main settings only count while nobody has been
added), the bridge alert ("tolino-Bridge Problem (anna)", to the notify target of the main settings) - each with that person's Audiobookshelf
account. Once anybody is added and nobody is marked, the bridge features are off; and a push to **their** notify target when a book or manga
volume **they** asked for is in the library ("Neu in der Bibliothek"; also fired as the event `books_wish_fulfilled`). Books are recognised by title
and author - a comparison, not a hard link - manga volumes exactly. Nobody is told what the others load.

## Setup

Settings → Devices & Services → Add Integration → **Books**:

| Field | Value |
|---|---|
| Chaptarr URL | e.g. `http://192.168.1.10:8789` |
| Chaptarr API key | Chaptarr → Settings → General → API Key |
| Audiobookshelf URL | e.g. `http://192.168.1.10:13378` |
| Audiobookshelf API token | token of a **dedicated, restricted** Audiobookshelf user (Settings → Users → create a user without upload/delete/update rights) — not the admin |
| Tolino bridge URL / token | optional: address of your tolino-bridge (e.g. `http://192.168.1.10:8199`) and its token (`deploy.sh token`). Leave empty to disable "send to Tolino" |
| Automatically send new ebooks to tolino | optional, **off by default**: every 10 minutes, ebooks that newly appear in Audiobookshelf (EPUB/PDF; MOBI/AZW3 are converted by the bridge) are sent to the tolino Cloud. Only books added **after** you switch it on — your existing library is never touched; at most 5 per run. Books that cannot be sent (unsupported, too large, conversion failed) are reported once (notification + push) and not retried; bridge/Thalia trouble is retried next time. Needs the tolino bridge |
| Sync reading progress from tolino | optional, **off by default**: every 10 minutes, the reading position and "finished" state of books you sent to tolino are imported into Audiobookshelf (and so the card resumes where you stopped on the reader). Only books sent through this integration; a newer Audiobookshelf state is never overwritten; needs the tolino bridge |
| Also write reading progress to tolino | optional, **off by default**, needs the option above: the other direction. What you read in the card (position, "finished") is written to the tolino Cloud so the reader continues there. The newer state wins; per book the last seen state on each side is remembered, so nothing ping-pongs |
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
| `GET /api/books/tolino` | tolino-bridge status (`enabled`, `reachable`, `logged_in`, `error`) |
| `POST /api/books/tolino` `{abs_item_id, force?}` (409 `already_sent` if it is still in the cloud; `force` replaces the cloud copy) | sends that item's EPUB/PDF to the Tolino Cloud (errors carry a `code`: `bad_type`, `no_ebook`, `too_large`, `captcha`, `login_backoff`, `unreachable`, …) |
| `POST /api/books/tolino-autosend` | runs the auto-send job now (409 `autosend_disabled` if it is switched off) |
| `POST /api/books/tolino-sync` | runs the reading-progress import now (409 `sync_disabled` if it is switched off) |
| `GET /api/books/rescue` | recent import-repair events |

All endpoints require a Home Assistant login.

## Development

```bash
uv venv --python 3.14 .venv && uv pip install --python .venv/bin/python pytest-homeassistant-custom-component
.venv/bin/python -m pytest
```
