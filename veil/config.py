import os
from pathlib import Path

DATA = Path(os.getenv("VEIL_DATA", ".veil")).resolve()
MAX_BYTES = int(os.getenv("VEIL_MAX_BYTES", 30 * 1024 * 1024))
MAX_EXPANDED = int(os.getenv("VEIL_MAX_EXPANDED", 150 * 1024 * 1024))
MAX_PARTS = int(os.getenv("VEIL_MAX_PARTS", 4000))
MAX_PAGES = int(os.getenv("VEIL_MAX_PAGES", 60))
TIMEOUT = int(os.getenv("VEIL_TIMEOUT", 600))
RETENTION = int(os.getenv("VEIL_RETENTION_HOURS", 24)) * 3600
MAX_QUEUE = int(os.getenv("VEIL_MAX_QUEUE", 32))
SUPPORTED = {".docx", ".pptx", ".xlsx", ".pdf"}
TERMINAL = {"completed", "review_required", "blocked", "failed"}
POLICY = "veil-preserve-content-v4"

MAX_BATCH = 50
MAX_BUNDLE_BYTES = int(os.getenv("VEIL_MAX_BUNDLE_BYTES", 2 * 1024**3))
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "testserver", "veil-api"} | {h.strip().lower() for h in os.getenv("VEIL_ALLOWED_HOSTS", "").split(",") if h.strip()}

PROVIDER_CHECK_TTL = max(30, min(3600, int(os.getenv('VEIL_PROVIDER_CHECK_TTL', '300'))))
PROVIDER_RETRY_SECONDS = max(1, min(60, int(os.getenv('VEIL_PROVIDER_RETRY_SECONDS', '5'))))
PROVIDER_WAIT_TIMEOUT = max(60, min(86400, int(os.getenv('VEIL_PROVIDER_WAIT_TIMEOUT', '900'))))
MAX_WORKSPACES = max(1, int(os.getenv('VEIL_MAX_WORKSPACES', '10000')))
WORKSPACE_CREATIONS_PER_HOUR = max(1, int(os.getenv('VEIL_WORKSPACE_CREATIONS_PER_HOUR', '60')))
