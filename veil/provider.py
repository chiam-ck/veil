"""Shared provider defaults; credentials are supplied at runtime."""
import os
from pathlib import Path

DEFAULT_ENDPOINT = 'https://api.example.com/v1'
DEFAULT_MODEL = 'your-model-id'


def api_key_from_env():
    """Allow a mounted secret file without exposing its contents in errors."""
    secret_file = os.getenv('VEIL_LLM_API_KEY_FILE', '')
    if secret_file:
        try:
            return Path(secret_file).read_text().strip()
        except (OSError, UnicodeError):
            raise ValueError('Unable to read the configured LLM API key file') from None
    return os.getenv('VEIL_LLM_API_KEY') or os.getenv('LLM_API_KEY', '')
