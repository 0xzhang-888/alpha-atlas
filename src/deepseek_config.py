"""Shared Messages endpoint for direct DeepSeek and compatible gateways."""
from __future__ import annotations

import os


def messages_url() -> str:
    explicit = os.environ.get("DEEPSEEK_MESSAGES_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    return os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/") + "/anthropic/v1/messages"
