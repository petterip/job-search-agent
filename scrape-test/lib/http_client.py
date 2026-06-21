"""Minimal HTTP helpers — stdlib only, shared across regression scripts."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

DEFAULT_UA = "JobResearch/1.0 (+https://github.com/job-search-agent)"

_DEFAULT_HEADERS = {
    "User-Agent": DEFAULT_UA,
    "Accept": "application/json",
}


def _merge_headers(extra: dict[str, str] | None) -> dict[str, str]:
    headers = dict(_DEFAULT_HEADERS)
    if extra:
        headers.update(extra)
    return headers


def fetch_json(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 20,
) -> tuple[Any, int, dict[str, str]]:
    data = json.dumps(body).encode() if body is not None else None
    req_headers = _merge_headers(headers)
    if data is not None:
        req_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode()
            response_headers = {k.lower(): v for k, v in response.headers.items()}
            return json.loads(raw), response.status, response_headers
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            payload: Any = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"_body": raw[:500]}
        return payload, exc.code, {}


def post_json(url: str, body: dict[str, Any], **kwargs: Any) -> tuple[Any, int, dict[str, str]]:
    return fetch_json(url, method="POST", body=body, **kwargs)


def fetch_text(url: str, *, headers: dict[str, str] | None = None, timeout: float = 60) -> tuple[str, int]:
    req = urllib.request.Request(url, headers=_merge_headers(headers))
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read().decode(errors="replace"), response.status
