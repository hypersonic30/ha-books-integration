"""Constants for the Books integration."""

DOMAIN = "books"

CONF_CHAPTARR_URL = "chaptarr_url"
CONF_CHAPTARR_API_KEY = "chaptarr_api_key"
CONF_ABS_URL = "abs_url"
CONF_ABS_TOKEN = "abs_token"
CONF_KOMGA_URL = "komga_url"
CONF_KOMGA_API_KEY = "komga_api_key"
CONF_MYLAR_URL = "mylar_url"
CONF_MYLAR_API_KEY = "mylar_api_key"

# Per-person accounts (config subentries): which Home Assistant user reads with which Komga / Audiobookshelf account.
SUBENTRY_USER = "user"
SUBENTRY_TOLINO_ACCOUNT = "tolino_account"      # not a stored entry: the flow that manages the bridge's Thalia accounts
CONF_HA_USER = "ha_user"
CONF_USER_TOLINO = "tolino"          # this person uses the Tolino bridge
CONF_TOLINO_ACCOUNT = "tolino_account"   # which Thalia account of the bridge (empty = the default account)
DEFAULT_TOLINO_ACCOUNT = "default"
CONF_KOMGA_NAME = "komga_name"       # who the key belongs to, shown as a check when saving
CONF_ABS_NAME = "abs_name"
WISH_MAX_AGE_SECONDS = 30 * 24 * 3600
WISH_INTERVAL_SECONDS = 5 * 60
CONF_TOLINO_URL = "tolino_url"
CONF_TOLINO_TOKEN = "tolino_token"
CONF_VERIFY_SSL = "verify_ssl"
CONF_RESCUE_IMPORTS = "rescue_imports"
CONF_NOTIFY_SERVICE = "notify_service"
CONF_DEBUG_LOGGING = "debug_logging"
CONF_AUTO_SEND = "auto_send"
CONF_IMPORT_TOLINO = "import_tolino"
CONF_IMPORT_TOLINO_AUDIOBOOKS = "import_tolino_audiobooks"
CONF_IMPORT_TOLINO_RADIOPLAYS = "import_tolino_radioplays"
CONF_RESTRICT_BOOKS = "restrict_books"
CONF_SYNC_PROGRESS = "sync_progress"
CONF_SYNC_PROGRESS_WRITE = "sync_progress_write"

# Second-stage search: when Chaptarr's own search for a wanted book stays empty (e.g. a German release
# transliterates ü/ö/ä/ß differently than Chaptarr's metadata-derived query), ask Prowlarr directly and, on a
# strict match, hand the NZB to NZBGet under Chaptarr's own watched category so Chaptarr imports it itself.
CONF_PROWLARR_URL = "prowlarr_url"
CONF_PROWLARR_API_KEY = "prowlarr_api_key"
CONF_NZBGET_URL = "nzbget_url"
CONF_NZBGET_USERNAME = "nzbget_username"
CONF_NZBGET_PASSWORD = "nzbget_password"
CONF_NZBGET_CATEGORY = "nzbget_category"
CONF_FALLBACK_SEARCH = "fallback_search"

DEFAULT_VERIFY_SSL = True
DEFAULT_RESCUE_IMPORTS = True
DEFAULT_DEBUG_LOGGING = False
DEFAULT_AUTO_SEND = False
DEFAULT_SYNC_PROGRESS = False
DEFAULT_SYNC_PROGRESS_WRITE = False
DEFAULT_NZBGET_CATEGORY = "Chaptarr"
DEFAULT_FALLBACK_SEARCH = False

REQUEST_TIMEOUT = 10
# Chaptarr's /release and /search endpoints query every indexer synchronously.
SLOW_REQUEST_TIMEOUT = 120

# Tolino Cloud only accepts these; larger files are refused by the bridge anyway.
TOLINO_FORMATS = frozenset({"epub", "pdf"})
# Kindle-style formats the bridge converts to EPUB with Calibre (if it is installed there).
TOLINO_CONVERTIBLE = frozenset({"mobi", "azw", "azw3", "prc", "fb2", "lit"})
TOLINO_MAX_BYTES = 100 * 1024 * 1024
TOLINO_UPLOAD_TIMEOUT = 180
# The bridge may convert first (Calibre can take a while on big books).
TOLINO_BRIDGE_TIMEOUT = 660
TOLINO_MAX_COVER_BYTES = 10 * 1024 * 1024

SIGNAL_SYNC_UPDATED = "books_progress_sync_updated"
SIGNAL_AUTOSEND_UPDATED = "books_autosend_updated"
SIGNAL_FALLBACK_UPDATED = "books_fallback_search_updated"
# Bus events for automations (payloads documented in the README)
EVENT_TOLINO_SENT = "books_tolino_sent"
EVENT_PROGRESS_SYNCED = "books_tolino_progress_synced"

RESCUE_INTERVAL_SECONDS = 120
SYNC_INTERVAL_SECONDS = 600
AUTO_SEND_INTERVAL_SECONDS = 600
IMPORT_INTERVAL_SECONDS = 600

# Chaptarr fallback search (second stage via Prowlarr + NZBGet, see chaptarr_fallback.py)
FALLBACK_INTERVAL_SECONDS = 3600
FALLBACK_SCAN_LIMIT = 60             # wanted/missing records looked at per tick, newest first - not the whole backlog
FALLBACK_MAX_PER_RUN = 3             # Prowlarr queries per tick - indexers have hourly rate limits
FALLBACK_MIN_AGE_SECONDS = 24 * 3600     # give Chaptarr's own search/RSS cycle a fair chance first
FALLBACK_RETRY_SECONDS = 24 * 3600       # cooldown between our own attempts for the same book
FALLBACK_MAX_ATTEMPTS = 10            # then give up and notify once, instead of retrying forever
# Newznab categories a fallback match may come from: Audio (3xxx), Books/Other (7xxx) - excludes TV/Movies/etc.,
# which is what the literal "S01E25"-style false positives in this indexer's library turned out to be.
NEWZNAB_ALLOWED_RANGES = (range(3000, 4000), range(7000, 8000))

# Chaptarr API areas the card never needs. They hold indexer/download-client
# credentials or can reconfigure/shut down Chaptarr, so the proxy refuses them
# for every method — settings stay in Chaptarr's own UI.
# Commands the card may trigger through POST /command.
CHAPTARR_ALLOWED_COMMANDS = frozenset({
    "BookSearch", "AuthorSearch", "MissingBookSearch", "RefreshAuthor",
    "RefreshBook", "RssSync", "RefreshMonitoredDownloads",
})

# Headers passed through to/from upstream on streamed responses (audio, EPUB, covers).
PASSTHROUGH_REQUEST_HEADERS = ("Range", "If-Range", "If-None-Match", "If-Modified-Since", "Accept")
PASSTHROUGH_RESPONSE_HEADERS = (
    "Content-Type", "Content-Length", "Content-Range", "Accept-Ranges",
    "Cache-Control", "ETag", "Last-Modified", "Content-Disposition",
)
