"""HTTP delivery with bounded retries and secret-safe logging."""

from __future__ import annotations

import http.client
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Treat every 3xx as a failure: a webhook endpoint never redirects.

    urllib's default handler would re-send the request, including an
    ``Authorization`` header, to whatever address the response points at, and
    downgrade a POST to a GET on 301/302/303.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


@dataclass
class Response:
    ok: bool
    status: int = None
    body: str = ""
    error: str = ""


def redact(url: str) -> str:
    """Hide the secret part of a webhook URL before logging it."""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return "<invalid url>"
    path = parsed.path
    segments = [segment for segment in path.split("/") if segment]
    if segments:
        segments[-1] = "***"
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    hidden = "&".join(f"{key}=***" for key, _ in query)
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, "/" + "/".join(segments), hidden, "")
    )


def single_line(text) -> str:
    """Collapse line breaks so a value can travel as one HTTP header.

    Titles come from terminal titles and agent names, and a stray CR/LF makes
    urllib reject the whole request, so providers clean header values here.
    """
    return " ".join(part.strip() for part in str(text).splitlines() if part.strip())


SENSITIVE_HEADER_HINTS = ("authorization", "cookie", "token", "secret", "key", "password")


def redact_headers(headers: dict) -> dict:
    """Mask the values of headers that usually carry credentials."""
    masked = {}
    for key, value in (headers or {}).items():
        name = str(key).lower()
        if any(hint in name for hint in SENSITIVE_HEADER_HINTS):
            masked[str(key)] = "***"
        else:
            masked[str(key)] = str(value)
    return masked


def send(url: str, body: bytes, headers: dict, timeout: float = 5, retries: int = 1, method: str = "POST") -> Response:
    attempt = 0
    while True:
        try:
            request = urllib.request.Request(url, data=body, headers=dict(headers or {}), method=method)
            with _OPENER.open(request, timeout=timeout) as response:
                payload = response.read().decode("utf-8", "replace")
                return Response(True, response.status, payload, "")
        except (ValueError, http.client.HTTPException) as exc:
            # Invalid header values, invalid URLs and malformed responses: a
            # retry cannot help, and one bad target must not stop the others.
            return Response(False, None, "", str(exc))
        except urllib.error.HTTPError as exc:  # server answered with an error status
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:400]
            except Exception:  # pragma: no cover - body may be unreadable
                detail = ""
            if 300 <= exc.code < 400:
                location = (exc.headers.get("Location", "") if exc.headers else "") or ""
                where = redact(location) if "://" in location else "<relative location>"
                return Response(
                    False, exc.code, detail, f"HTTP {exc.code} redirect to {where} (not followed)"
                )
            if exc.code in RETRY_STATUSES and attempt < retries:
                attempt += 1
                time.sleep(0.5 * attempt)
                continue
            return Response(False, exc.code, detail, f"HTTP {exc.code}")
        except (urllib.error.URLError, OSError) as exc:
            if attempt < retries:
                attempt += 1
                time.sleep(0.5 * attempt)
                continue
            return Response(False, None, "", str(exc))
