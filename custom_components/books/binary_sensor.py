"""Binary sensor platform: 'tolino-Bridge Problem'. The logic lives in tolino_watch."""
from .tolino_watch import async_setup_entry  # noqa: F401  (HA looks the platform entry point up here)
