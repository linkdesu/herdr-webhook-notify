"""Per-owner routing: wake only the owner that started a session.

A session belongs to an owner when its Herdr agent name is
``<owner>--<rest>``, for example ``cto--hwn``. Sessions without the prefix, or
with a prefix that has no ``[owners.<owner>]`` table, are never sent here.
"""

from __future__ import annotations

import re
from datetime import datetime

from . import http
from .providers.base import Delivery, json_bytes, json_headers

AGENT_PATTERN = re.compile(r"^([a-z][a-z0-9]*)--(.+)$")
HEADER_PATTERN = re.compile(r"^([!#$%&'*+.^_`|~0-9A-Za-z-]+):[ \t]*(\S.*)$")
CONFIG_KEYS = ("url", "header")


def split_agent(agent_name: str):
    """Return ``(owner, rest)`` for an owner-scoped agent name, else ``None``."""
    match = AGENT_PATTERN.fullmatch((agent_name or "").strip())
    if not match:
        return None
    return match.group(1), match.group(2)


def parse_header(raw: str):
    """Parse one ``Name: value`` line into a header tuple, or ``None`` when empty."""
    text = (raw or "").strip()
    if not text:
        return None
    if "\n" in text or "\r" in text:
        raise ValueError("header must be a single line")
    match = HEADER_PATTERN.fullmatch(text)
    if not match:
        raise ValueError("header must be a full 'Name: value' line")
    return match.group(1), match.group(2).strip()


def payload(kind: str, status: str, fields: dict, agent_name: str, owner: str, now: float, herdr_event="") -> dict:
    return {
        "agent": agent_name,
        "owner": owner,
        "pane_id": fields.get("pane_id") or "",
        "workspace_id": fields.get("workspace_id") or "",
        "status": status,
        "event": kind,
        "herdr_event": herdr_event or "",
        "time": datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds"),
    }


def target(cfg, agent_name: str):
    """Return ``(owner, options)`` when this session should use an owner route."""
    parsed = split_agent(agent_name)
    if parsed is None:
        return None
    owner = parsed[0]
    options = cfg.owners.get(owner)
    if not options:
        return None
    return owner, options


def build(owner: str, options: dict, body: dict) -> Delivery:
    headers = json_headers()
    header = parse_header(options.get("header") or "")
    if header is not None:
        headers[header[0]] = header[1]
    return Delivery(
        f"owner:{owner}",
        options["url"],
        json_bytes(body),
        headers,
        f"owner wake payload for {owner}",
    )


def deliver(cfg, kind: str, status: str, fields: dict, agent_name: str, now: float, herdr_event="", only=None):
    """Send the owner wake webhook. Returns ``[(owner, ok, status_code, detail)]``."""
    match = target(cfg, agent_name)
    if match is None:
        return []
    owner, options = match
    if only and owner != only:
        return []
    if "${" in options["url"]:
        return [(owner, False, None, "url still contains an unset ${VAR}")]

    body = payload(kind, status, fields, agent_name, owner, now, herdr_event)
    try:
        delivery = build(owner, options, body)
    except ValueError as exc:
        return [(owner, False, None, f"invalid header: {exc}")]
    try:
        response = http.send(
            delivery.url,
            delivery.body,
            delivery.headers,
            timeout=float(cfg.http.get("timeout_seconds", 5)),
            retries=int(cfg.http.get("retries", 1)),
            method=delivery.method,
        )
    except Exception as exc:  # an owner route must not abort the other channels
        return [(owner, False, None, f"send failed: {exc}")]
    if response.ok:
        return [(owner, True, response.status, response.body[:200])]
    return [(owner, False, response.status, response.error or response.body)]
