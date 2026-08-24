"""The one way this project talks to the network.

Discovery, article fetches and image downloads all share `PoliteClient`, which
means they also share one connection pool, one pace and one exception type.
That is the point: the politeness budget belongs to the run, not to whichever
module happens to be making a request.

Why the rules are what they are (scraper/README.md §Site reconnaissance):

**The UA tells the truth.** GQ Korea's robots.txt disallows named crawlers
(Scrapy, CCBot, …) and AI-training crawlers (GPTBot, ClaudeBot, …) outright. We
are neither — this is a personal archive fetching its own reading material — but
the site's position is clear enough that identifying honestly is the minimum
courtesy. So: project name, version, and a URL a sysadmin can open to see what
this is. Claiming to be Chrome would be the other kind of lie.

**Retries are for their hiccups, not our mistakes.** 429 and 5xx are worth
another attempt; a 404 or a 401 (which is what `/wp-json/` answers) means we
asked for the wrong thing, and repeating a wrong request three times only adds
load. Same reasoning splits transport failures: a timeout or a dropped
connection is worth retrying, a bad URL scheme or a redirect loop is not.

**Redirects are followed; an unfollowed one is a failure, not a body.** The
site redirects constantly — `/sitemap.xml` to `/wp-sitemap.xml`, `/category/style/`
to `/style`, article permalinks to their trailing-slash form — so following is
not optional. What follows from that is the less obvious half: if a 3xx still
reaches us, httpx declined to follow it, and the empty stub it carries would
otherwise be handed to the parser as a page with no HTML.

**UTF-8 is not negotiable.** The site is Korean and its headers have been wrong
about encoding before, so `get_text` decodes UTF-8 explicitly rather than
letting httpx guess from a `charset` it does not trust.

**Bodies are capped.** A mistyped image URL that lands on a multi-gigabyte file
should fail the article, not the machine.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from collections.abc import Callable, Mapping
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

USER_AGENT = (
    "gq-korea-gallery/0.1 (personal image archive; "
    "+https://github.com/zen-wang/GQ-Korea-Gallery)"
)

DEFAULT_MIN_INTERVAL = 1.0
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_ATTEMPTS = 3

# No jitter in the backoff: one client makes one request at a time, so there is
# no fleet to de-synchronize, and a deterministic delay is one we can test.
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_FACTOR = 2.0
MAX_BACKOFF_SECONDS = 30.0

# A server may ask for any wait it likes; we honour it up to a point, because a
# nightly run that parks for an hour has effectively failed anyway.
MAX_RETRY_AFTER_SECONDS = 120.0

# Comfortably above any editorial JPEG and far below anything that would hurt.
MAX_RESPONSE_BYTES = 32 * 1024 * 1024

# Enough of a non-JSON body to recognise an HTML error page at a glance.
SNIPPET_CHARS = 200

RETRY_STATUS = 429
SERVER_ERROR_STATUS = 500

# The boundaries of "this response carries the thing we asked for". Anything
# from 300 up does not, whatever else it may be.
REDIRECT_STATUS = 300
CLIENT_ERROR_STATUS = 400

# Failures that are plausibly transient. Everything else httpx can raise
# (UnsupportedProtocol, LocalProtocolError, TooManyRedirects) is deterministic:
# retrying it just repeats our own bug more slowly.
_RETRYABLE_ERRORS = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)


class HttpError(RuntimeError):
    """A request that could not be completed politely.

    Covers exhausted retries, a status we refuse to retry, an oversized body and
    an undecodable one — so callers have exactly one thing to catch.
    """


def _is_retryable_status(status: int) -> bool:
    return status == RETRY_STATUS or status >= SERVER_ERROR_STATUS


def _backoff(attempt: int) -> float:
    """Delay before the attempt after `attempt` (1-based)."""
    return min(BACKOFF_BASE_SECONDS * BACKOFF_FACTOR ** (attempt - 1), MAX_BACKOFF_SECONDS)


def _retry_after(response: httpx.Response) -> float | None:
    """The server's own requested delay, capped, or None if it did not ask.

    Both RFC 9110 forms are accepted; the integer one is what WordPress and
    Cloudflare actually send, the HTTP-date one costs four lines to support.
    """
    raw = (response.headers.get("retry-after") or "").strip()
    if not raw:
        return None

    if raw.isdigit():
        return min(float(raw), MAX_RETRY_AFTER_SECONDS)

    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None  # A malformed header is not worth failing over; back off normally.
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    seconds = (when - dt.datetime.now(dt.timezone.utc)).total_seconds()
    return max(0.0, min(seconds, MAX_RETRY_AFTER_SECONDS))


def _snippet(body: bytes) -> str:
    """A short, printable prefix of a body we could not parse."""
    return body[:SNIPPET_CHARS].decode("utf-8", errors="replace")


def _describe_failure(status: int | None, error: Exception | None) -> str:
    if status is not None:
        return f"HTTP {status}"
    if error is not None:
        return f"{type(error).__name__}: {error}"
    return "unknown failure"


class PoliteClient:
    """A rate-limited, retrying HTTP client over one reused connection pool.

    `sleep` and `monotonic` are injected so the pacing is testable: tests pass a
    fake clock and assert the exact spacing instead of waiting for real seconds.
    `transport` exists for the same reason — an httpx.MockTransport exercises the
    real request path with no network. Neither is used in production.
    """

    def __init__(
        self,
        *,
        user_agent: str = USER_AGENT,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        timeout: float = DEFAULT_TIMEOUT,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if min_interval < 0:
            raise ValueError(f"min_interval must not be negative: {min_interval}")
        if timeout <= 0:
            raise ValueError(f"timeout must be positive: {timeout}")
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be at least 1: {max_attempts}")

        self._min_interval = min_interval
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._monotonic = monotonic
        # None until the first request: nobody should wait to start.
        self._earliest_next: float | None = None

        self._client = httpx.Client(
            headers={
                "User-Agent": user_agent,
                # The site publishes in Korean only; saying so avoids any
                # locale negotiation and is what a reader's browser would send.
                "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
            },
            timeout=httpx.Timeout(timeout),
            # /sitemap.xml redirects to /wp-sitemap.xml, and article permalinks
            # normalize their trailing slash.
            follow_redirects=True,
            # One connection is all a sequential, rate-limited scraper needs.
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            transport=transport,
        )

    # ---- public API ------------------------------------------------------

    def get_bytes(self, url: str) -> bytes:
        """Fetch a URL as raw bytes (images, and anything hashed)."""
        return self._fetch("GET", url)

    def get_text(self, url: str) -> str:
        """Fetch a URL and decode it as UTF-8, whatever the headers claim."""
        body = self._fetch("GET", url)
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError as exc:
            # Falling back to a lossy decode would hand the parser silently
            # corrupted Korean, which is worse than a failed article.
            raise HttpError(f"GET {url}: body is not valid UTF-8 ({exc})") from exc

    def post_json(self, url: str, data: Mapping[str, str]) -> Any:
        """Form-encoded POST returning parsed JSON (the admin-ajax endpoint)."""
        body = self._fetch("POST", url, data=data)
        try:
            return json.loads(body)
        except ValueError as exc:  # JSONDecodeError, plus UnicodeDecodeError on odd bytes
            # The realistic failure is a 200 carrying an HTML error or login
            # page, so the message shows what actually came back.
            raise HttpError(
                f"POST {url}: expected JSON, got {len(body)} bytes starting "
                f"{_snippet(body)!r} ({exc})"
            ) from exc

    def close(self) -> None:
        """Release the connection pool. Safe to call more than once."""
        self._client.close()

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- internals -------------------------------------------------------

    def _fetch(self, method: str, url: str, *, data: Mapping[str, str] | None = None) -> bytes:
        last_status: int | None = None
        last_error: Exception | None = None

        for attempt in range(1, self._max_attempts + 1):
            self._wait_turn()
            try:
                # Streaming so the size guard can refuse a body before it is
                # all in memory; a normal response is still read in full here.
                with self._client.stream(method, url, data=data) as response:
                    if response.status_code < REDIRECT_STATUS:
                        return self._read_capped(response, url)
                    if response.status_code < CLIENT_ERROR_STATUS:
                        # follow_redirects is on, so a 3xx that survives to here
                        # is one httpx would not follow — typically a Location
                        # that is missing or unusable. Its body is the empty
                        # redirect stub; returning that as a success would reach
                        # the parser as an article containing no HTML.
                        raise HttpError(
                            f"{method} {url}: HTTP {response.status_code} "
                            f"(redirect that could not be followed, to "
                            f"{response.headers.get('location') or 'no Location'})"
                        )
                    if not _is_retryable_status(response.status_code):
                        raise HttpError(
                            f"{method} {url}: HTTP {response.status_code} "
                            f"(client error, not retried)"
                        )
                    last_status, last_error = response.status_code, None
                    delay = _retry_after(response)
            except _RETRYABLE_ERRORS as exc:
                last_status, last_error = None, exc
                delay = None
            except httpx.HTTPError as exc:
                raise HttpError(f"{method} {url}: {type(exc).__name__}: {exc}") from exc

            if attempt == self._max_attempts:
                break
            self._sleep(_backoff(attempt) if delay is None else delay)

        raise HttpError(
            f"{method} {url}: giving up after {self._max_attempts} attempts "
            f"(last failure: {_describe_failure(last_status, last_error)})"
        )

    def _wait_turn(self) -> None:
        """Block until this client is allowed to make its next request.

        Time already spent — parsing, encoding, the previous response itself —
        counts towards the interval, so the pace is one request per
        `min_interval`, not one per interval *plus* however long we took.
        """
        now = self._monotonic()
        if self._earliest_next is not None and now < self._earliest_next:
            self._sleep(self._earliest_next - now)
            now = self._monotonic()  # sleep may overshoot; re-read rather than assume
        self._earliest_next = now + self._min_interval

    @staticmethod
    def _read_capped(response: httpx.Response, url: str) -> bytes:
        """Read a streamed body, refusing anything past the cap.

        Content-Length is checked first because it costs nothing and usually
        catches the bad case before a byte is transferred; the running total
        catches servers that under-declare or send chunked.
        """
        declared = (response.headers.get("content-length") or "").strip()
        if declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
            raise HttpError(
                f"{url}: response too large ({declared} bytes declared, "
                f"cap is {MAX_RESPONSE_BYTES})"
            )

        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                # Raising inside the stream context closes the connection, which
                # is what actually stops the download.
                raise HttpError(f"{url}: response too large (over {MAX_RESPONSE_BYTES} bytes)")
            chunks.append(chunk)
        return b"".join(chunks)
