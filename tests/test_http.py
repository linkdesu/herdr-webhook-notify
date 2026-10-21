"""http.send reports failures instead of raising, so one target cannot abort a run."""

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
