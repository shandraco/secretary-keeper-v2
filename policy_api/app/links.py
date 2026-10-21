"""Signed, expiring download links, so a browser can fetch a file without the API key."""
from __future__ import annotations

import hashlib
import hmac
import re
import time

# <uuid>.<ext> for a run's files and generated documents; <uuid>-<chart>.png for a run's charts.
FILE_NAME = re.compile(r"^[0-9a-f-]{36}(?:-(status|matrix|types))?\.(xlsx|pdf|docx|png)$")


def _sig(key: str, name: str, exp: int) -> str:
    return hmac.new(key.encode(), f"{name}.{exp}".encode(), hashlib.sha256).hexdigest()


def file_url(base_url: str, key: str, name: str, ttl_hours: int) -> str:
    exp = int(time.time()) + ttl_hours * 3600
    return f"{base_url}/exports/{name}?exp={exp}&sig={_sig(key, name, exp)}"


def excel_url(base_url: str, key: str, run_id: str, ttl_hours: int) -> str:
    return file_url(base_url, key, f"{run_id}.xlsx", ttl_hours)


def verify(key: str, name: str, exp: int, sig: str) -> bool:
    return exp >= time.time() and hmac.compare_digest(_sig(key, name, exp), sig)
