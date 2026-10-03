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

DEFAULT_VERIFY_SSL = True
DEFAULT_RESCUE_IMPORTS = True
DEFAULT_DEBUG_LOGGING = False
DEFAULT_AUTO_SEND = False
DEFAULT_SYNC_PROGRESS = False
DEFAULT_SYNC_PROGRESS_WRITE = False

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
# Bus events for automations (payloads documented in the README)
EVENT_TOLINO_SENT = "books_tolino_sent"
EVENT_PROGRESS_SYNCED = "books_tolino_progress_synced"

RESCUE_INTERVAL_SECONDS = 120
SYNC_INTERVAL_SECONDS = 600
AUTO_SEND_INTERVAL_SECONDS = 600
IMPORT_INTERVAL_SECONDS = 600

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
