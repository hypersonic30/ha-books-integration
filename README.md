# Books Integration for Home Assistant (Chaptarr, Audiobookshelf, Komga, Mylar, tolino)

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2025.4%2B-brightgreen.svg)](https://www.home-assistant.io)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A secure, server-side proxy between the Lovelace cards and your media servers:
[Chaptarr](https://github.com/Chaptarr/chaptarr) (ebook/audiobook manager, Readarr successor),
[Audiobookshelf](https://www.audiobookshelf.org/) (library, reader, player), optionally
[Komga](https://komga.org) (manga/comics reader) with [Mylar3](https://github.com/mylar3/mylar3) (searching and downloading manga),
and optionally the [tolino-bridge](https://github.com/hypersonic30/tolino-bridge) (send books to a tolino).

> [!IMPORTANT]
> Needs **Home Assistant 2025.4 or newer** (every person gets their own account through config subentries).
> The integration is the backend and installs first; the cards use it:
> - **[Books Card](https://github.com/hypersonic30/ha-books-card)** — ebooks and audiobooks (Chaptarr, Audiobookshelf, tolino)
> - **[Manga Card](https://github.com/hypersonic30/ha-manga-card)** — manga and comics (Komga, Mylar3); optional

```
books-card / manga-card  →  Home Assistant (your HA login)  →  this integration  →  Chaptarr, Audiobookshelf, Komga, Mylar3 (keys/tokens)
                                                                                 →  tolino-bridge  →  tolino Cloud
```

All API keys and tokens stay in Home Assistant; the cards only use your normal Home Assistant session. Each person can have
**their own accounts** (reading progress, notifications, tolino) — see *People* below. Without a tolino bridge the integration creates
no entities; with one it adds a few status entities (below).

## Features

- **Proxy for Chaptarr** — search, queue/wanted list, history, calendar, interactive release search, and a few harmless commands (book/author search,
  refresh, RSS sync). A strict **allow-list**: nothing that changes or deletes books, authors, files, queue entries or blocklists, and none of Chaptarr's
  settings (indexers, download clients, profiles, system). Chaptarr has one API key for everybody, so this list is what keeps an ordinary person from deleting
  the library. Adding a book goes through `POST /api/books/add`.
- **Proxy for Audiobookshelf** — libraries, items, covers, EPUB and audio files, playback sessions and each person's own reading progress, also an
  **allow-list** (no users, API keys, scans, edits or deletes, whatever the token would allow). Audio files and EPUBs are **streamed** with HTTP Range
  support (seeking in 30-hour M4Bs works), and `<audio>`/`<img>` tags authenticate via Home Assistant signed URLs.
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

- **Manga (optional)** — proxies for Komga (reading, progress) and Mylar3 (search, add, download) with strict allow-lists; see below.
- **People (optional)** — every Home Assistant user can have their own Komga/Audiobookshelf account, notify target and tolino account.
- **tolino (optional)** — send ebooks to the tolino Cloud, automatically if you like, and carry reading progress both ways; one Thalia
  account per person through the bridge.

## Security at a glance

- Every request needs a Home Assistant login **and a person**; each proxy has a strict allow-list (Chaptarr, Audiobookshelf, Komga, Mylar); paths with
  `..`, backslashes, NUL or percent signs are refused.
- **Administrator keys are refused** when you save: a Komga key whose user has the ADMIN role, an Audiobookshelf token of an `admin`/`root` user. The cards
  only need to read - use a dedicated restricted user. (Existing entries are not checked until you edit them.)
- **Repair hints** appear when a service refuses a key (Komga, Audiobookshelf, Chaptarr: HTTP 401; Mylar: "Missing API key"), per service and person, and
  disappear when it works again.
- **Diagnostics** (the integration's three-dots menu → Download diagnostics) have every key, token, notify target and user id blanked and no titles.
- Keys and tokens live in Home Assistant's config storage (and therefore in its backups): use encrypted backups. The tolino bridge speaks plain HTTP: keep
  its port off the internet (see its `docs/unraid.md`, "Härtung").

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
| `binary_sensor` *tolino-Bridge Problem* | on when the bridge is unreachable or an account is not logged in at Thalia; attribute `accounts` has the state of every account in use (alerts name the account: "tolino-Bridge Problem (anna)") |
| `sensor` *Last progress sync* | timestamp of the last reading-progress run; attributes `imported`, `exported`, `skipped` (that run of the default account), `enabled`, `write_enabled`, and `accounts` (the same per bridge account) |
| `sensor` *Last auto-sent book* | title of the book auto-sent last (survives restarts); attributes `sent_at`, `total_sent`, `given_up`, `enabled`, and `accounts` (per bridge account) |
| event `books_tolino_sent` | a book reached the tolino Cloud: `item_id`, `title`, `filename`, `deliverable_id`, `replaced`, `auto` (true = auto-send, false = the card's button) |
| event `books_wish_fulfilled` | a book or manga volume somebody asked for is in the library: `user`, `kind` (`book`/`manga`), `title` |
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

### People (required)

Every Home Assistant user who uses the cards needs a **person**: their own accounts, so nobody sees or overwrites somebody else's reading
progress. **Without a person there is no access** (the cards get `403 no_person` with a message that says what to do), and while nobody has been
added at all, Home Assistant shows a repair hint.

1. Create a Komga user (no admin rights) and an Audiobookshelf user for each person, and note their API key / token.
2. Settings → Devices & Services → Books → **Add person**. Pick the Home Assistant user, paste their Komga key and/or Audiobookshelf token
   (empty = that person uses the shared key/token of the main settings), optionally a notify target and whether they use the tolino bridge.
   The keys are checked when you save and the name of the account they belong to is stored with the person.
3. People can be added, edited and removed at any time, no restart.

What a person gets: their own reading progress, bookmarks and "Weiterlesen" in the Books and Manga cards; and, if marked as a **tolino user**,
"An tolino senden" with **their own Thalia account in the bridge** (the person's "Tolino account" = the name from `deploy.sh account add NAME`;
empty = the bridge's default account; the form offers the accounts the bridge knows and refuses one that is unknown or already used).
Everything runs per account: the list of sent books, auto-send, taking reading progress from tolino and sending it to tolino (the three
switches of the person), each with that person's Audiobookshelf account.

**Who is told what**
- The notify target of the main settings (the administrator) hears about everything: bridge/account problems (with the account named),
  auto-send giving a book up, failed import repairs - and as a persistent notification in Home Assistant.
- A person whose tolino account has a problem is told on **their own** notify target ("Dein tolino-Konto ist gerade nicht eingeloggt …",
  and again when it works) - not twice if that target is also the administrator's. A bridge that does not answer at all is **one** alert (to the
  administrator and each tolino person), not one per account.
- A push "Neu in der Bibliothek" goes to the person who asked for a book or manga volume when it arrives (also fired as the event
  `books_wish_fulfilled`). Books are recognised by title and author - a comparison, not a hard link - manga volumes exactly. Nobody is told
  what the others load.
- **One library, a tag per person**: when a book somebody asked for arrives in Audiobookshelf it gets the tag `für NAME` (the person's name),
  so the Books card can show *Alle / Für mich / each person* as chips. Books without such a tag (the existing stock, or loaded in Chaptarr
  directly) are for everybody. The tags are written by the integration with the shared Audiobookshelf user of the main settings - **that
  user needs the "update" permission** (not admin; without it the notification still comes, the tag is just missing and a warning is logged).
  The proxy still refuses every write to Audiobookshelf items.

- **Import from tolino (per person, off by default, needs bridge >= 0.8.0)**: with "Import ebooks from tolino" on, the person's tolino
  purchases and free ebooks (no audiobooks, not the books that came from Audiobookshelf in the first place) are downloaded through the bridge
  (the watermark file is dropped), uploaded to the "eBooks" library of Audiobookshelf and tagged `für NAME`. Switching it on takes the whole
  stock once (3 books every 10 minutes), later purchases follow by themselves; a book whose title is already in Audiobookshelf is skipped.
  Imported books are never sent back to tolino. They take part in the reading-progress sync like books sent from Audiobookshelf (switches
  "Take reading progress from tolino" / "Send reading progress to tolino" of the person; needs bridge >= 0.8.0): same file on both sides, so
  the position is exact. The shared Audiobookshelf user needs the **"upload" and "update"** permissions (not admin).

- **Audiobooks from tolino (two more switches per person, off by default, needs bridge >= 0.9.0)**: "Import audiobooks from tolino" and "Import radio
  plays from tolino" load the MP3 audiobooks of the person's tolino account track by track (the way the web reader does) into a temporary folder
  of the Home Assistant config directory, upload them in one go into the Audiobookshelf library named *Hörbücher* / *Hörspiele* and tag them
  `für NAME`. The shop does not say which is which: a title with three or more readers, or the word Hörspiel in its text, counts as a radio play,
  everything else as an audiobook - a title of a kind that is switched off stays in the cloud and comes when it is switched on. One audiobook per
  10-minute run; what is already in Audiobookshelf (same title) is skipped. Reading progress of audiobooks is not synced yet.
- **Child protection ("Lock other people's books", per person, off by default)**: a restricted person only sees the books released for them.
  The lock is Audiobookshelf's own tag limit: the person needs their **own Audiobookshelf user** (not admin) with *access to all tags* switched
  off and the allowed tags chosen (for example `für Lena` and `für alle`); Audiobookshelf then hides everything else - list, covers, files,
  search. Books without an allowed tag stay invisible, so the existing stock is closed for them until you tag a book (in Audiobookshelf,
  select several books and add the tag, or tap the person chips under "Für wen?" in the book detail of the Books card). Saving the person checks that the Audiobookshelf user really is limited, and the check is repeated
  every few minutes: if it stops being true (or Audiobookshelf cannot be asked for an hour) the library closes for that person and the
  administrator gets a notice - it never falls back to the shared token. **Search, requests, downloads and the import notes of Chaptarr and
  Mylar are closed for a restricted person** (the search shows every title with its blurb). Komga is **not** limited yet.

#### Thalia accounts from Home Assistant

Settings → Devices & Services → Books → **Manage Thalia accounts** (needs tolino-bridge ≥ 0.7.0). A menu offers what makes sense:

- **Create a new account** - name, Thalia e-mail and password. The password is sent **once**, in clear text over HTTP on your network, to the bridge and
  is not stored in Home Assistant (not in the config, not in a log, not in the logbook); the bridge stores it in its own account directory, logs in
  right away and removes the account again if the login fails (captcha, wrong password, no device yet), so a retry starts clean. An existing name is
  refused, nothing is ever overwritten. Afterwards enter the name as the person's *Tolino account*.
- **Rename the "default" account** - gives the bridge's original account the name of its person in one step: the bridge moves credentials, session and
  Chrome profile (the login is kept), the person using it is switched to the new name and the list of sent books follows.
- **Remove an account** - deletes credentials, session and profile in the bridge and the matching list of sent books here; refused while a person
  still uses the account. The Thalia account itself is untouched.

The terminal still works (`deploy.sh account add|list|remove|rename-default`), and the action **`books.move_tolino_account`** carries the list of sent books
over to a new name if you rename by hand.

## Setup

Settings → Devices & Services → Add Integration → **Books**:

| Field | Value |
|---|---|
| Chaptarr URL | e.g. `http://192.168.1.10:8789` |
| Chaptarr API key | Chaptarr → Settings → General → API Key |
| Audiobookshelf URL | e.g. `http://192.168.1.10:13378` |
| Audiobookshelf API token | token of a **dedicated, restricted** Audiobookshelf user (Settings → Users → create a user without upload/delete/update rights) — an admin token is refused |
| Komga URL / API key | optional, for the Manga Card: address of your Komga and the API key of a Komga user **without admin rights** (an admin key is refused). Checked when you save |
| Mylar3 URL / API key | optional, for searching and downloading manga in the Manga Card: address of Mylar3 and its API key (Mylar → Settings → Web Interface → API, enable it first). Checked when you save |
| Tolino bridge URL / token | optional: address of your tolino-bridge (e.g. `http://192.168.1.10:8199`) and its token (`deploy.sh token`). Leave empty to disable "send to Tolino" |
| Verify SSL | disable only for self-signed certificates |
| Automatically repair blocked imports | see above (default on) |
| Notification target | optional: a notify entity (e.g. `notify.iphone`, as used by `notify.send_message`) or a legacy notify service (e.g. `notify.mobile_app_iphone`); several separated by commas — told when a repair fails |

**People are required** (see *People* below): whoever uses the cards needs a person, and the tolino switches (auto-send, taking reading
progress from tolino, sending it to tolino) are settings of the person. The Komga key and the Audiobookshelf token here are the **shared**
ones, used by every person who does not enter their own (and by background jobs); the bridge URL/token and the notification target are the
administrator's.

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
| `/api/books/komga/{path}` | Komga `/api/{path}` — reading, progress, rescan only (strict allow-list; the person's own key) |
| `/api/books/mylar/{command}` | Mylar3 `/api?cmd={command}` — search, add, queue volumes (allow-list per command and parameter; slow searches answer `202` and run in the background) |
| `POST /api/books/tags` `{item_id, tag, tagged}` | release a book for a person (`für NAME`) or for everybody (`für alle`) or take it back; only these tags, never for locked people; written with the shared Audiobookshelf user (needs "update") |
| `GET /api/books/people` | the people with their tag (`name`, `tag`, `me`) and `restricted` (the asker is locked; then only themselves are listed) - what the card's person chips and tabs are built from |
| `GET /api/books/tolino` | status of the asking person's tolino account (`enabled` is false for people without a tolino; `reachable`, `logged_in`, `error`, `sent`) |
| `POST /api/books/tolino` `{abs_item_id, force?}` (409 `already_sent` if it is still in the cloud; `force` replaces the cloud copy) | sends that item's EPUB/PDF to the Tolino Cloud (errors carry a `code`: `bad_type`, `no_ebook`, `too_large`, `captcha`, `login_backoff`, `unreachable`, …) |
| `POST /api/books/tolino-autosend` | runs the auto-send job of the asking person's account now (409 `autosend_disabled` if it is switched off, 403 `no_tolino` without a tolino) |
| `POST /api/books/tolino-sync` | runs the reading-progress sync of the asking person's account now (409 `sync_disabled` if it is switched off) |
| `GET /api/books/rescue` | recent import-repair events |

All endpoints require a Home Assistant login.

## Development

```bash
uv venv --python 3.14 .venv && uv pip install --python .venv/bin/python pytest-homeassistant-custom-component
.venv/bin/python -m pytest
```
