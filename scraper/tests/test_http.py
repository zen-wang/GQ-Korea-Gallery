"""Tests for the polite HTTP client.

Three other modules build on PoliteClient, so what is pinned here is the
*contract*: spacing, what gets retried, what deliberately does not, and the
single exception type callers have to catch.

No real network. Every test drives an httpx.MockTransport, which exercises the
genuine request path — headers, form encoding, redirects, streaming — instead of
stubbing out our own internals, and a FakeClock stands in for time so the
spacing assertions are exact rather than "roughly a second".
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from email.utils import format_datetime

import httpx
import pytest

from gallery_scraper.core import http as http_module
from gallery_scraper.core.http import USER_AGENT, HttpError, PoliteClient

URL = "https://example.test/2026/07/12/summer-black/"
AJAX_URL = "https://example.test/wp-admin/admin-ajax.php"


# --------------------------------------------------------------------------
# Doubles
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Reply:
    """One scripted HTTP response. Frozen so a script cannot be edited mid-run."""

    status: int = 200
    body: bytes = b""
    headers: tuple[tuple[str, str], ...] = ()

    def build(self) -> httpx.Response:
        # A fresh Response per attempt: httpx consumes the byte stream on read,
        # so replaying one instance twice would fail on the second attempt.
        return httpx.Response(self.status, content=self.body, headers=list(self.headers))


@dataclass(frozen=True)
class Chunked:
    """A response that declares no length, the way a CDN streams a large file.

    httpx sets Content-Length for us whenever it is handed bytes, so a scripted
    `Reply` can only ever exercise the declared-length half of the size guard.
    This shape — chunked, length unknown until the last byte — is the case the
    running total exists for, and the one a server that under-declares produces.

    `produced` records what the generator was actually asked for, so a test can
    tell "refused after reading everything" from "refused mid-download". It is a
    recorder, not state the script reads, which is why freezing still holds.
    """

    chunks: tuple[bytes, ...]
    status: int = 200
    headers: tuple[tuple[str, str], ...] = ()
    produced: list[bytes] = field(default_factory=list)

    def build(self) -> httpx.Response:
        return httpx.Response(self.status, content=self._stream(), headers=list(self.headers))

    def _stream(self) -> Iterator[bytes]:
        for chunk in self.chunks:
            self.produced.append(chunk)
            yield chunk


class StubTransport(httpx.MockTransport):
    """Replays a script of Reply/Exception steps and records what was sent.

    The last step repeats, so a one-step script models a server that keeps
    answering the same way however many attempts we make.
    """

    def __init__(self, *script: Reply | Chunked | Exception) -> None:
        if not script:
            raise ValueError("a script needs at least one step")
        self.requests: list[httpx.Request] = []
        self._script = script
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        step = self._script[min(len(self.requests) - 1, len(self._script) - 1)]
        if isinstance(step, Exception):
            raise step
        return step.build()


@dataclass
class FakeClock:
    """Injected monotonic/sleep pair. Sleeping advances the clock, as it would.

    `overshoot` models what time.sleep actually does — it guarantees a floor,
    not an exact wake-up — so the pacing can be tested against a clock that
    lands late rather than one that is conveniently perfect.
    """

    now: float = 1_000.0
    slept: list[float] = field(default_factory=list)
    overshoot: float = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds + self.overshoot


def make_client(
    *script: Reply | Chunked | Exception, **kwargs
) -> tuple[PoliteClient, StubTransport, FakeClock]:
    """A client wired to a scripted transport and a fake clock.

    min_interval defaults to 0 so that `clock.slept` records backoff only;
    the spacing tests set it explicitly.
    """
    transport = StubTransport(*script)
    clock = FakeClock()
    kwargs.setdefault("min_interval", 0.0)
    client = PoliteClient(
        transport=transport, sleep=clock.sleep, monotonic=clock.monotonic, **kwargs
    )
    return client, transport, clock


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


def test_user_agent_names_the_project_and_carries_a_contact_url():
    assert "http" in USER_AGENT and "://" in USER_AGENT
    assert "gallery" in USER_AGENT.lower() or "gq" in USER_AGENT.lower()


def test_user_agent_impersonates_neither_a_browser_nor_a_disallowed_crawler():
    # robots.txt disallows named crawlers and AI-training crawlers; we are
    # neither, and claiming to be a browser would be the other kind of lie.
    lowered = USER_AGENT.lower()
    for forbidden in ("mozilla", "chrome", "safari", "webkit", "scrapy", "claudebot", "gptbot"):
        assert forbidden not in lowered


def test_every_request_carries_the_user_agent():
    client, transport, _ = make_client(Reply(200, b"ok"))
    with client:
        client.get_bytes(URL)
    assert transport.requests[0].headers["user-agent"] == USER_AGENT


def test_reading_a_page_or_an_image_is_a_GET():
    # Only the admin-ajax endpoint takes a POST. Sending one anywhere else reads
    # as a write to the site, and misses every cache in front of it.
    client, transport, _ = make_client(Reply(200, b"ok"))
    with client:
        client.get_bytes(URL)
        client.get_text(URL)
    assert [request.method for request in transport.requests] == ["GET", "GET"]


def test_every_request_asks_for_korean():
    # The site publishes in Korean only. Sending the locale a reader's browser
    # would send keeps us off any negotiated translation or redirect path.
    client, transport, _ = make_client(Reply(200, b"ok"))
    with client:
        client.get_bytes(URL)
    assert transport.requests[0].headers["accept-language"].startswith("ko")


# --------------------------------------------------------------------------
# Construction
#
# Every one of these settings is a politeness knob. A caller that passes
# nonsense has to hear about it at construction, not by quietly hammering the
# site at zero interval for a whole run.
# --------------------------------------------------------------------------


def test_a_negative_interval_is_refused():
    with pytest.raises(ValueError, match="min_interval"):
        PoliteClient(min_interval=-1.0)


def test_a_non_positive_timeout_is_refused():
    with pytest.raises(ValueError, match="timeout"):
        PoliteClient(timeout=0.0)


def test_fewer_than_one_attempt_is_refused():
    # max_attempts=0 would make every fetch fail without ever asking, which is
    # the kind of bug that looks like a site outage.
    with pytest.raises(ValueError, match="max_attempts"):
        PoliteClient(max_attempts=0)


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------


def test_the_first_request_does_not_wait():
    client, _, clock = make_client(Reply(200, b"ok"), min_interval=2.0)
    with client:
        client.get_bytes(URL)
    assert clock.slept == []


def test_consecutive_requests_are_spaced_by_min_interval():
    client, transport, clock = make_client(Reply(200, b"ok"), min_interval=2.0)
    with client:
        client.get_bytes(URL)
        client.get_bytes(URL)
        client.get_bytes(URL)
    assert clock.slept == [2.0, 2.0]
    assert len(transport.requests) == 3


def test_time_already_spent_counts_towards_the_interval():
    # Parsing and image encoding happen between fetches; that work is part of
    # the gap, so the client must not sleep the full interval on top of it.
    client, _, clock = make_client(Reply(200, b"ok"), min_interval=2.0)
    with client:
        client.get_bytes(URL)
        clock.now += 1.5
        client.get_bytes(URL)
    assert clock.slept == [0.5]


def test_a_slow_response_removes_the_wait_entirely():
    client, _, clock = make_client(Reply(200, b"ok"), min_interval=2.0)
    with client:
        client.get_bytes(URL)
        clock.now += 30.0
        client.get_bytes(URL)
    assert clock.slept == []


def test_the_interval_is_measured_from_when_we_actually_woke_up():
    # time.sleep promises a floor, not a wake-up time. If the client assumed it
    # resumed exactly when it meant to, every overshoot would be credited
    # against the *next* gap and the pace would drift faster than min_interval.
    client, _, clock = make_client(Reply(200, b"ok"), min_interval=2.0)
    clock.overshoot = 5.0
    with client:
        client.get_bytes(URL)
        client.get_bytes(URL)
        client.get_bytes(URL)
    assert clock.slept == [2.0, 2.0]


# --------------------------------------------------------------------------
# Retries
# --------------------------------------------------------------------------


def test_a_server_error_is_retried_and_the_result_is_returned():
    client, transport, clock = make_client(Reply(503), Reply(200, b"ok"))
    with client:
        assert client.get_bytes(URL) == b"ok"
    assert len(transport.requests) == 2
    assert clock.slept == [http_module.BACKOFF_BASE_SECONDS]


def test_backoff_grows_exponentially_between_attempts():
    client, _, clock = make_client(Reply(500), max_attempts=4)
    with client, pytest.raises(HttpError):
        client.get_bytes(URL)
    base = http_module.BACKOFF_BASE_SECONDS
    factor = http_module.BACKOFF_FACTOR
    assert clock.slept == [base, base * factor, base * factor**2]


def test_transport_failures_are_retried():
    client, transport, _ = make_client(httpx.ConnectError("connection refused"), Reply(200, b"ok"))
    with client:
        assert client.get_bytes(URL) == b"ok"
    assert len(transport.requests) == 2


def test_timeouts_are_retried():
    client, transport, _ = make_client(httpx.ReadTimeout("too slow"), Reply(200, b"ok"))
    with client:
        assert client.get_bytes(URL) == b"ok"
    assert len(transport.requests) == 2


def test_exhausted_retries_raise_httperror_naming_url_status_and_attempts():
    client, transport, _ = make_client(Reply(502), max_attempts=3)
    with client, pytest.raises(HttpError) as excinfo:
        client.get_bytes(URL)
    message = str(excinfo.value)
    assert URL in message
    assert "502" in message
    assert "3" in message
    assert len(transport.requests) == 3


def test_exhausted_transport_failures_raise_httperror_naming_the_cause():
    client, _, _ = make_client(httpx.ConnectError("connection refused"), max_attempts=2)
    with client, pytest.raises(HttpError) as excinfo:
        client.get_bytes(URL)
    assert "ConnectError" in str(excinfo.value)


def test_a_404_is_not_retried():
    # A 4xx that is not 429 means we asked for the wrong thing. Repeating the
    # same wrong request three times only adds load.
    client, transport, clock = make_client(Reply(404))
    with client, pytest.raises(HttpError) as excinfo:
        client.get_bytes(URL)
    assert "404" in str(excinfo.value)
    assert len(transport.requests) == 1
    assert clock.slept == []


def test_a_401_is_not_retried():
    # /wp-json/ answers 401; PoliteClient must surface that immediately.
    client, transport, _ = make_client(Reply(401))
    with client, pytest.raises(HttpError):
        client.get_bytes(URL)
    assert len(transport.requests) == 1


def test_a_429_is_retried():
    client, transport, _ = make_client(Reply(429), Reply(200, b"ok"))
    with client:
        assert client.get_bytes(URL) == b"ok"
    assert len(transport.requests) == 2


def test_retry_after_header_overrides_the_backoff():
    client, _, clock = make_client(
        Reply(429, headers=(("Retry-After", "7"),)),
        Reply(200, b"ok"),
    )
    with client:
        client.get_bytes(URL)
    assert clock.slept == [7.0]


def test_an_absurd_retry_after_is_capped():
    # An hour-long Retry-After should not hang a nightly run indefinitely.
    client, _, clock = make_client(
        Reply(503, headers=(("Retry-After", "86400"),)),
        Reply(200, b"ok"),
    )
    with client:
        client.get_bytes(URL)
    assert clock.slept == [http_module.MAX_RETRY_AFTER_SECONDS]


def test_backoff_stops_growing_at_the_ceiling():
    # Doubling forever would park a nightly run for hours on a site that is
    # simply down; the ceiling is what bounds the whole retry budget.
    client, _, clock = make_client(Reply(500), max_attempts=8)
    with client, pytest.raises(HttpError):
        client.get_bytes(URL)
    assert max(clock.slept) == http_module.MAX_BACKOFF_SECONDS
    assert clock.slept[-1] == http_module.MAX_BACKOFF_SECONDS
    assert clock.slept == sorted(clock.slept)  # monotone: capped, never reset


def test_a_retry_after_http_date_is_honoured():
    # RFC 9110 allows either form. WordPress sends the integer, but a CDN in
    # front of it may send the date, and reading it as "malformed" would throw
    # away the one instruction the server actually gave us.
    when = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=30)
    client, _, clock = make_client(
        Reply(503, headers=(("Retry-After", format_datetime(when, usegmt=True)),)),
        Reply(200, b"ok"),
    )
    with client:
        client.get_bytes(URL)
    assert clock.slept == [pytest.approx(30.0, abs=2.0)]


def test_a_retry_after_date_already_in_the_past_never_sleeps_backwards():
    # Clock skew between us and the origin is normal, and a negative sleep is a
    # ValueError from time.sleep in production.
    when = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    client, _, clock = make_client(
        Reply(429, headers=(("Retry-After", format_datetime(when, usegmt=True)),)),
        Reply(200, b"ok"),
    )
    with client:
        client.get_bytes(URL)
    assert clock.slept == [0.0]


def test_an_absurd_retry_after_date_is_capped_like_the_integer_form():
    # Both forms have to be capped, not just the one WordPress happens to send:
    # a nightly run that parks until tomorrow has already failed.
    when = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
    client, _, clock = make_client(
        Reply(503, headers=(("Retry-After", format_datetime(when, usegmt=True)),)),
        Reply(200, b"ok"),
    )
    with client:
        client.get_bytes(URL)
    assert clock.slept == [http_module.MAX_RETRY_AFTER_SECONDS]


def test_a_retry_after_date_without_a_timezone_is_read_as_utc():
    # RFC 9110 dates carrying "-0000" parse to a naive datetime, and subtracting
    # one of those from an aware "now" is a TypeError — which would escape as
    # neither HttpError nor a retry, taking the run down over a header.
    when = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=45)
    client, _, clock = make_client(
        Reply(429, headers=(("Retry-After", format_datetime(when.replace(tzinfo=None))),)),
        Reply(200, b"ok"),
    )
    with client:
        assert client.get_bytes(URL) == b"ok"
    assert clock.slept == [pytest.approx(45.0, abs=2.0)]


def test_a_malformed_retry_after_falls_back_to_the_normal_backoff():
    # A header we cannot parse is not worth failing the fetch over; it just
    # means the server told us nothing useful.
    client, _, clock = make_client(
        Reply(503, headers=(("Retry-After", "when we feel like it"),)),
        Reply(200, b"ok"),
    )
    with client:
        assert client.get_bytes(URL) == b"ok"
    assert clock.slept == [http_module.BACKOFF_BASE_SECONDS]


def test_a_connection_dropped_mid_response_is_retried():
    # The realistic image-download failure: the CDN closes the socket partway
    # through the body. That is their hiccup, so it earns another attempt.
    client, transport, _ = make_client(
        httpx.RemoteProtocolError("server disconnected without sending a response"),
        Reply(200, b"ok"),
    )
    with client:
        assert client.get_bytes(URL) == b"ok"
    assert len(transport.requests) == 2


def test_a_deterministic_transport_failure_is_not_retried():
    # An unusable URL fails identically however many times we send it, so
    # retrying only repeats our own bug more slowly. It still has to arrive as
    # HttpError, because that is the single type callers catch.
    client, transport, clock = make_client(httpx.UnsupportedProtocol("unknown scheme"))
    with client, pytest.raises(HttpError) as excinfo:
        client.get_bytes(URL)
    assert "UnsupportedProtocol" in str(excinfo.value)
    assert len(transport.requests) == 1
    assert clock.slept == []


# --------------------------------------------------------------------------
# Redirects
#
# The site redirects constantly — /sitemap.xml to /wp-sitemap.xml,
# /category/style/ to /style, permalinks to their trailing-slash form — so
# following them is not a nicety, and a 3xx that reaches us is a failure
# carrying an empty stub rather than a page.
# --------------------------------------------------------------------------


def test_a_redirect_is_followed_to_the_page_it_names():
    page = "<html><body>스타일</body></html>"
    client, transport, _ = make_client(
        Reply(301, headers=(("Location", "/style"),)),
        Reply(200, page.encode("utf-8")),
    )
    with client:
        assert client.get_text("https://example.test/category/style/") == page
    assert str(transport.requests[-1].url) == "https://example.test/style"
    assert transport.requests[-1].headers["user-agent"] == USER_AGENT


def test_an_unfollowable_redirect_is_a_failure_not_an_empty_page():
    # A 3xx only survives to us when httpx declined to follow it — here because
    # there is no Location to follow. Its body is the empty redirect stub, and
    # handing that to the parser would look like an article with no HTML.
    client, _, clock = make_client(Reply(302))
    with client, pytest.raises(HttpError) as excinfo:
        client.get_bytes(URL)
    message = str(excinfo.value)
    assert "302" in message
    assert "no Location" in message
    assert clock.slept == []  # not a hiccup, so not retried


def test_a_redirect_loop_fails_without_retrying():
    # A permalink that redirects to itself is a site-side bug we cannot fix by
    # asking again; httpx gives up, and we must not restart the whole chain.
    client, _, clock = make_client(Reply(302, headers=(("Location", "/loop"),)))
    with client, pytest.raises(HttpError) as excinfo:
        client.get_bytes(URL)
    assert "TooManyRedirects" in str(excinfo.value)
    assert clock.slept == []


# --------------------------------------------------------------------------
# Decoding
# --------------------------------------------------------------------------


def test_get_text_decodes_korean_as_utf8_whatever_the_headers_claim():
    korean = "여름의 끝에서, 검정"
    client, _, _ = make_client(
        Reply(
            200,
            korean.encode("utf-8"),
            headers=(("Content-Type", "text/html; charset=iso-8859-1"),),
        )
    )
    with client:
        assert client.get_text(URL) == korean


def test_get_text_rejects_bytes_that_are_not_utf8():
    client, _, _ = make_client(Reply(200, b"\xff\xfe not utf-8"))
    with client, pytest.raises(HttpError) as excinfo:
        client.get_text(URL)
    assert "UTF-8" in str(excinfo.value)


# --------------------------------------------------------------------------
# post_json
# --------------------------------------------------------------------------


def test_post_json_sends_a_form_encoded_body_and_parses_the_reply():
    payload = {"posts": [{"url": URL}]}
    client, transport, _ = make_client(Reply(200, json.dumps(payload).encode("utf-8")))
    with client:
        assert client.post_json(AJAX_URL, {"action": "load_more", "page": "2"}) == payload

    request = transport.requests[0]
    assert request.method == "POST"
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert request.content == b"action=load_more&page=2"


def test_post_json_raises_httperror_with_a_snippet_when_html_comes_back():
    # The realistic failure: the endpoint answers 200 with an error page.
    html = b"<!DOCTYPE html><html><head><title>Error</title></head><body>nope</body></html>"
    client, _, _ = make_client(Reply(200, html))
    with client, pytest.raises(HttpError) as excinfo:
        client.post_json(AJAX_URL, {"action": "load_more"})
    message = str(excinfo.value)
    assert AJAX_URL in message
    assert "JSON" in message
    assert "<!DOCTYPE html" in message


def test_post_json_snippet_is_truncated():
    client, _, _ = make_client(Reply(200, b"x" * 10_000))
    with client, pytest.raises(HttpError) as excinfo:
        client.post_json(AJAX_URL, {"action": "load_more"})
    assert len(str(excinfo.value)) < 1_000


# --------------------------------------------------------------------------
# Response size guard
# --------------------------------------------------------------------------


def test_a_streamed_body_over_the_cap_is_refused_by_the_running_total(monkeypatch):
    # The guard that matters: nothing declared a length, so only the bytes as
    # they arrive can say stop. Chunk size and cap are chosen so the refusal
    # has to happen partway through — a check that only ran after the loop
    # would have the whole file in memory before objecting, which is the exact
    # outcome the cap exists to prevent.
    monkeypatch.setattr(http_module, "MAX_RESPONSE_BYTES", 64)
    body = Chunked(tuple(b"x" * 32 for _ in range(10)))
    client, _, _ = make_client(body)
    with client, pytest.raises(HttpError) as excinfo:
        client.get_bytes(URL)
    message = str(excinfo.value)
    assert "too large" in message
    assert "declared" not in message  # the running total refused it, not the header
    assert len(body.produced) < len(body.chunks)


def test_a_server_that_under_declares_its_length_is_still_refused(monkeypatch):
    # Content-Length is the server's claim, not a fact. A small declared length
    # in front of a large body must not buy a free pass past the cap.
    monkeypatch.setattr(http_module, "MAX_RESPONSE_BYTES", 64)
    client, _, _ = make_client(
        Chunked(tuple(b"x" * 32 for _ in range(10)), headers=(("Content-Length", "10"),))
    )
    with client, pytest.raises(HttpError) as excinfo:
        client.get_bytes(URL)
    assert "too large" in str(excinfo.value)


def test_a_streamed_body_under_the_cap_is_reassembled_in_order():
    client, _, _ = make_client(Chunked((b"<html>", b"\xeb\xaa\xa9", b"</html>")))
    with client:
        assert client.get_bytes(URL) == b"<html>\xeb\xaa\xa9</html>"


def test_an_oversized_content_length_is_refused_before_the_body_is_read():
    # The body here is five bytes; only the declared length is absurd, so this
    # can only pass if the header is checked before anything is downloaded.
    client, _, _ = make_client(
        Reply(200, b"small", headers=(("Content-Length", str(10**10)),))
    )
    with client, pytest.raises(HttpError) as excinfo:
        client.get_bytes(URL)
    assert "too large" in str(excinfo.value)


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


def test_context_manager_yields_the_client_itself():
    client, transport, _ = make_client(Reply(200, b"ok"))
    with client as entered:
        assert entered is client
        client.get_bytes(URL)
        client.get_bytes(URL)
    assert len(transport.requests) == 2


def test_the_client_is_closed_on_exit():
    # Closing has to bite on the next call, which is also the proof that one
    # httpx client — one connection pool — is reused across requests rather
    # than rebuilt per call.
    client, _, _ = make_client(Reply(200, b"ok"))
    with client:
        client.get_bytes(URL)
    with pytest.raises(RuntimeError, match="closed"):
        client.get_bytes(URL)


def test_close_is_idempotent():
    client, _, _ = make_client(Reply(200, b"ok"))
    client.close()
    client.close()
