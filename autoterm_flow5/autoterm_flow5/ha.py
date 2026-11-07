"""Read Home Assistant states and post persistent notifications."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .faults import Notice


def supervisor_token() -> str:
    return os.environ.get("SUPERVISOR_TOKEN", "")


def read_temperature(entity_id: str, token: str, base_url: str, timeout: float = 5) -> float | None:
    url = f"{base_url.rstrip('/')}/states/{entity_id}"
    request = urllib.request.Request(url, headers=_headers(token))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError):
        return None
    state = payload.get("state")
    if state in (None, "unknown", "unavailable", ""):
        return None
    try:
        return float(state)
    except (TypeError, ValueError):
        return None


def deliver_notice(notice: Notice, token: str, base_url: str, timeout: float = 5) -> None:
    url = f"{base_url.rstrip('/')}/services/persistent_notification/{notice.action}"
    body: dict[str, str] = {"notification_id": notice.notification_id}
    if notice.action == "create":
        body["title"] = notice.title
        body["message"] = notice.message
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=_headers(token), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            return
    except (urllib.error.URLError, TimeoutError):
        return


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
