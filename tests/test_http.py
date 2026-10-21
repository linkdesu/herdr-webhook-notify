"""http.send reports failures instead of raising and never follows redirects."""

import contextlib
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from herdr_webhook_notify import http


def test_send_returns_failure_for_invalid_header_value():
    # urllib raises ValueError for a header value with CR/LF before it connects.
    response = http.send(
        "http://127.0.0.1:1/hook",
        b"{}",
        {"X-Bad": "v\r\nX-Evil: 1"},
        timeout=0.1,
        retries=0,
    )
    assert response.ok is False
    assert "header value" in response.error.lower()


def test_bad_url_scheme_does_not_raise():
    response = http.send("notaurl", b"{}", {}, timeout=0.1, retries=0)
    assert response.ok is False
    assert "notaurl" in response.error


class _Redirector(BaseHTTPRequestHandler):
    location = ""

    def do_POST(self):  # noqa: N802 - stdlib naming
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(302)
        self.send_header("Location", self.location)
        self.end_headers()

    def log_message(self, *args):
        return


class _Sink(BaseHTTPRequestHandler):
    hits = []

    def _record(self):
        self.hits.append(
            {"method": self.command, "path": self.path, "auth": self.headers.get("Authorization")}
        )
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.end_headers()

    do_GET = _record  # noqa: N815 - stdlib naming
    do_POST = _record  # noqa: N815 - stdlib naming

    def log_message(self, *args):
        return


@contextlib.contextmanager
def local_server(handler, **attributes):
    for name, value in attributes.items():
        setattr(handler, name, value)
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def test_redirect_is_not_followed():
    _Sink.hits = []
    with local_server(_Sink) as sink:
        sink_url = f"http://127.0.0.1:{sink.server_port}/collect"
        with local_server(_Redirector, location=sink_url) as redirector:
            response = http.send(
                f"http://127.0.0.1:{redirector.server_port}/webhook",
                b'{"text":"hi"}',
                {"Content-Type": "application/json", "Authorization": "Bearer SECRET"},
                timeout=2,
                retries=1,
            )

        assert _Sink.hits == []  # the Authorization header never left the target
        assert response.ok is False
        assert response.status == 302
        assert "302" in response.error
        assert "not followed" in response.error
        assert str(sink.server_port) in response.error
