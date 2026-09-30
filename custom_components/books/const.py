"""Constants for the Books integration."""

DOMAIN = "books"

CONF_CHAPTARR_URL = "chaptarr_url"
CONF_CHAPTARR_API_KEY = "chaptarr_api_key"
CONF_ABS_URL = "abs_url"
CONF_ABS_TOKEN = "abs_token"
CONF_TOLINO_URL = "tolino_url"
CONF_TOLINO_TOKEN = "tolino_token"
CONF_VERIFY_SSL = "verify_ssl"
CONF_RESCUE_IMPORTS = "rescue_imports"
CONF_NOTIFY_SERVICE = "notify_service"
CONF_DEBUG_LOGGING = "debug_logging"
CONF_SYNC_PROGRESS = "sync_progress"

DEFAULT_VERIFY_SSL = True
DEFAULT_RESCUE_IMPORTS = True
DEFAULT_DEBUG_LOGGING = False
DEFAULT_SYNC_PROGRESS = False

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

RESCUE_INTERVAL_SECONDS = 120
SYNC_INTERVAL_SECONDS = 600

# Chaptarr API areas the card never needs. They hold indexer/download-client
# credentials or can reconfigure/shut down Chaptarr, so the proxy refuses them
# for every method — settings stay in Chaptarr's own UI.
CHAPTARR_BLOCKED_SEGMENTS = frozenset({
    "system", "config", "indexer", "downloadclient", "notification",
    "rootfolder", "qualityprofile", "metadataprofile", "customformat",
    "delayprofile", "releaseprofile", "importlist", "importlistexclusion",
    "remotepathmapping", "backup", "update", "log", "filesystem", "tag",
    "health", "indexerflag", "autotagging", "user", "apikey",
})

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
