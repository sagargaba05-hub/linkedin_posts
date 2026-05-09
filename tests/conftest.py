"""
conftest.py — pytest fixtures shared across tests.

We mock the third-party clients (gspread, anthropic, requests, slack_sdk) so
tests don't need real credentials. Tests focus on pure-logic functions.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

# Add project root to Python path so `import config` etc. works in tests
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Mock third-party deps before any project import — keeps tests fast and
# avoids needing the actual SDKs installed in CI's minimal environment.
for _mod in [
    "gspread",
    "gspread.utils",
    "google",
    "google.oauth2",
    "google.oauth2.service_account",
    "anthropic",
    "anthropic.errors",
    "requests",
    "requests.exceptions",
    "slack_sdk",
    "slack_sdk.errors",
    "tenacity",
    "pybreaker",
]:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

# Set required env vars to harmless values so config doesn't sys.exit on import
import os  # noqa: E402

os.environ.setdefault("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
os.environ.setdefault("SHEET_ID", "test-sheet")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("SLACK_BOT_TOKEN", "test-slack")
os.environ.setdefault("SLACK_CHANNEL_ID", "C-test")
os.environ.setdefault("LINKEDIN_TOKEN", "test-li")
