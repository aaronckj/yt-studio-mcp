"""Where this server keeps its local state.

Everything on disk -- credentials (file backend), the banned-word list and scan
watermark, giveaway snapshots, question-comment tracking, archived thumbnails,
scan history and live status -- lives under ONE directory:

    YT_MCP_CONFIG_DIR   default ~/.config/yt-studio-mcp

Point each instance at its own directory to run one server per channel side by
side; nothing is shared between them. YT_MCP_CREDENTIALS_FILE overrides just the
credentials file (default <config dir>/credentials.json).

Paths are resolved at call time, not import time, so the environment of the
running process is always what counts.
"""

from __future__ import annotations

import os
from pathlib import Path

CONFIG_DIR_ENV = "YT_MCP_CONFIG_DIR"
CREDENTIALS_FILE_ENV = "YT_MCP_CREDENTIALS_FILE"
DEFAULT_CONFIG_DIR = Path.home() / ".config" / "yt-studio-mcp"


def config_dir() -> Path:
    value = os.environ.get(CONFIG_DIR_ENV, "").strip()
    return Path(value).expanduser() if value else DEFAULT_CONFIG_DIR


def credentials_path() -> Path:
    value = os.environ.get(CREDENTIALS_FILE_ENV, "").strip()
    return Path(value).expanduser() if value else config_dir() / "credentials.json"


def wordlist_path() -> Path:
    return config_dir() / "banned_words.json"


def scan_state_path() -> Path:
    return config_dir() / "scan_state.json"


def scan_history_path() -> Path:
    return config_dir() / "scan_history.jsonl"


def live_status_path() -> Path:
    return config_dir() / "live_status.json"


def giveaway_dir() -> Path:
    return config_dir() / "giveaways"


def question_state_path() -> Path:
    return config_dir() / "question_comments.json"


def thumbnails_dir() -> Path:
    return config_dir() / "thumbnails"
