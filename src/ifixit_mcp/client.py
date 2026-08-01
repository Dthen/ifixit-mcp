"""Async HTTP client for the iFixit API 2.0 with caching and rate limiting."""

from __future__ import annotations

import asyncio
import copy
import html
import json
import math
import re
import time
from typing import Any

import httpx
from urllib.parse import quote

# Base URL for the iFixit API 2.0.
BASE_URL = "https://www.ifixit.com/api/2.0"

# Cache TTL presets (seconds). TTL_30MIN mirrors the iFixit CDN edge TTL
# (observed x-debug-ttl: 1800 on live responses).
TTL_MINUTE = 60
TTL_HOUR = 3600
TTL_30MIN = 1800

# Maximum cache entries before eviction.
MAX_CACHE_SIZE = 256

# Depth cap for recursive HTML/raw stripping (a real guide nests ~6
# levels; 50 is far beyond any legitimate payload).
MAX_HTML_CONVERT_DEPTH = 50

# QA Round 13 (R13-5): cap on string parameters. quote() can expand a
# character ~3x, and URL construction + cache-key json.dumps compound
# that — a 50MB title/query used to force multi-hundred-MB transient
# allocations. Real titles/queries are < 200 chars, so 100K is
# generous. (httpx separately hard-rejects URLs over 8192 bytes, but
# that check happens AFTER quote()/URL building — this cap bounds the
# pre-URL work and gives a clean ValueError.)
MAX_STRING_PARAM_LENGTH = 100_000

# Default User-Agent (identifies the client and points at the public
# repository).
DEFAULT_USER_AGENT = "ifixit-mcp/0.1.0 (https://github.com/Dthen/ifixit-mcp)"

# Rate limiting defaults.
DEFAULT_MIN_INTERVAL = 0.5  # seconds between requests
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE = 1.0  # seconds

# QA Round 15 (F15-4): cap on the Retry-After delay honored on 429s.
# The exponential-backoff path is bounded (≤8s at the default settings),
# but a large finite Retry-After used to be honored unbounded ('3600' ->
# 1-hour stall, '9e9' -> ~285 years), freezing every request for that
# key. iFixit's documented CDN TTL hints are far below this cap; a
# server asking for more is retried after the cap instead.
MAX_RETRY_AFTER = 8.0

# /guides pagination bounds (verified: limit clamps to 200 server-side).
GUIDES_MIN_LIMIT = 1
GUIDES_MAX_LIMIT = 200
GUIDES_DEFAULT_LIMIT = 20

# Valid /suggest doctypes (RESEARCH.md §3c). device/category alias topic,
# post aliases question; "all" = topic+guide. The API ignores unknown
# values (and 400s when nothing valid remains), so we reject them up front.
SEARCH_DOCTYPES = frozenset(
    {"guide", "item", "topic", "device", "category", "question", "post", "all"}
)

# Valid /media types (RESEARCH.md §3g). The API 404s on unknown types, so
# we reject them up front.
MEDIA_TYPES = frozenset({"images", "videos", "documents"})

# Valid /wikis namespaces (RESEARCH.md §2: `ns enum: WIKI,CATEGORY,INFO,
# ITEM,USER,TEAM`). Unknown values 404 server-side, so reject them up front.
VALID_NAMESPACES = frozenset({"WIKI", "CATEGORY", "INFO", "ITEM", "USER", "TEAM"})


class NotFoundError(ValueError):
    """Raised when the API responds 404.

    Subclasses ValueError so existing ``except ValueError`` handlers keep
    working; translation sites catch it specifically to produce
    per-resource messages (e.g. ``Guide not found: 1220``).
    """


class ForbiddenError(ValueError):
    """Raised when the API responds 403.

    Subclasses ValueError so existing ``except ValueError`` handlers keep
    working; callers that need to distinguish a permission failure from a
    generic 400 catch it specifically (e.g. get_media translates it into
    "Media not accessible").
    """


def _brief(value: Any) -> str:
    """A bounded repr for validator error messages.

    Huge values (e.g. a 5000-digit numeric string) must not blow up the
    error message itself (QA Round 5, R5-3b).
    """
    text = repr(value)
    if len(text) > 80:
        return text[:77] + "..."
    return text


def _validate_positive_int(field: str, value: int | str) -> int:
    """Validate a positive integer (int or numeric string); return the int.

    Rejects zero, negatives, floats, booleans, empty strings, and any
    non-numeric junk with a ValueError naming *field*.
    """
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive integer, got {value!r}")
    if isinstance(value, int):
        if value <= 0:
            raise ValueError(f"{field} must be a positive integer, got {value!r}")
        return value
    if isinstance(value, str):
        stripped = value.strip()
        # isdecimal() (not isdigit()) so Unicode superscripts like "²"
        # (isdigit True, int() rejects) get the clean family message.
        if stripped.isdecimal():
            try:
                parsed = int(stripped)
            except (ValueError, OverflowError):
                # Python 3.11+ caps int(str) at 4300 digits; an overlong
                # numeric string must get the clean family message, not
                # the raw "Exceeds the limit" ValueError (QA Round 5).
                raise ValueError(
                    f"{field} must be a positive integer, got {_brief(value)}"
                ) from None
            if parsed > 0:
                return parsed
    raise ValueError(f"{field} must be a positive integer, got {_brief(value)}")


def _validate_guideid(guideid: int | str) -> int:
    """Validate a guide id (positive int or numeric string); return the int."""
    return _validate_positive_int("guideid", guideid)


def _validate_string_length(field: str, value: Any) -> None:
    """Reject string params over MAX_STRING_PARAM_LENGTH (QA Round 13, R13-5).

    Applied to every user-supplied string before quote(), URL building
    or cache-key json.dumps so an absurdly long value fails with a clean
    ValueError naming the field instead of forcing multi-hundred-MB
    transient allocations. Non-string values pass through untouched
    (their own validators decide).
    """
    if isinstance(value, str) and len(value) > MAX_STRING_PARAM_LENGTH:
        raise ValueError(
            f"{field} exceeds maximum length of "
            f"{MAX_STRING_PARAM_LENGTH} characters"
        )


def _validate_title(title: str) -> str:
    """Validate a wiki/device title (non-empty string); return it stripped."""
    if not isinstance(title, str) or not title.strip():
        raise ValueError(f"title must be a non-empty string, got {title!r}")
    stripped = title.strip()
    _validate_string_length("title", stripped)
    return stripped


def _validate_namespace(namespace: str) -> str:
    """Validate a wiki namespace against the documented enum; return it.

    Accepts only the API's namespace enum (WIKI, CATEGORY, INFO, ITEM,
    USER, TEAM — RESEARCH.md §2) after stripping; anything else raises a
    ValueError naming the allowed set, before any network IO.
    """
    if not isinstance(namespace, str) or not namespace.strip():
        raise ValueError(
            f"namespace must be one of: {', '.join(sorted(VALID_NAMESPACES))}"
        )
    stripped = namespace.strip()
    if stripped not in VALID_NAMESPACES:
        raise ValueError(
            f"namespace must be one of: {', '.join(sorted(VALID_NAMESPACES))}"
        )
    return stripped


# QA Round 11 (F11-1, HIGH): defense-in-depth cap for _html_to_text. The
# tag regex is linear after the loop-class fix (benchmarked: 400KB of
# hostile input in ~0.11s), so this cap is NOT what makes the sanitizer
# safe — it bounds worst-case per-call CPU (~2s at 10MB) in case a future
# edit reintroduces superlinearity, and rejects absurd inputs early.
# Realistic iFixit rendered fields are KBs, so 10MB is unreachable for
# legitimate content.
_MAX_HTML_TO_TEXT_INPUT = 10 * 1024 * 1024


def _html_to_text(value: str) -> str:
    """Strip HTML tags and unescape entities, producing plain text.

    Block-level tags become newlines so paragraphs stay readable; runs of
    horizontal whitespace collapse to a single space. Only tag-like
    constructs (``<`` followed by a letter or ``/``) are stripped, so a
    bare ``<`` in plain text (e.g. "5 < 10 and 10 > 5") survives.

    QA Round 11 (F11-1, HIGH): the generic tag regex used to be
    vulnerable to ReDoS — its unquoted loop class consumed ``<``, so
    repeated unclosed tag openings (``"<a"*n``) forced O(n) backtracking
    per ``<`` start position -> O(n²) (40KB took ~47s, freezing the
    single-threaded server for every client). The loop class now excludes
    ``<`` ([^"'<>]), making the sanitizer linear (benchmarked: 40KB 12ms,
    200KB 54ms, 400KB 113ms). As defense-in-depth, inputs over
    _MAX_HTML_TO_TEXT_INPUT (10MB) raise a clean ValueError instead of
    being processed — unreachable for realistic iFixit rendered fields
    (KBs), it bounds worst-case per-call CPU even if a future edit
    reintroduces superlinearity.

    QA Round 12 (F12-1, HIGH): the script/style/doctype OPENING-TAG
    classes had the same flaw — ``[^>]*`` scans to end-of-string when no
    ``>`` follows, and re.sub retries at every ``<`` start -> O(n²)
    (``"<script "*40000`` = 320KB took ~12.8s). Those classes now exclude
    ``<`` too ([^<>]*), so each match attempt is bounded by the next
    ``<`` and the sanitizer is linear end to end. The closing-tag
    patterns (``</script\\s*>`` etc.) are unchanged.

    QA Round 6 (F2): comments, doctype/CDATA declarations, and
    script/style BODIES are removed BEFORE the generic tag strip — the
    tag regex cannot match ``<!--`` (next char is ``!``) or
    ``<![CDATA[``, so those used to leak verbatim into the plain text
    (live: user 3's about_text began with a Font Awesome license
    comment), and script/style bodies would have lost their tags but
    kept their JavaScript/CSS content.

    QA Round 7 (F8): malformed / truncated HTML must not leak either.
    Unclosed comments, unclosed script/style bodies, quoted attribute
    values containing ``>``, and ``</ p>`` (stray whitespace inside a
    close tag) used to survive as fragments:

    - comments/script/style strip through end-of-string when the closing
      delimiter is missing (``.*?(?:...</...>|$)``);
    - the generic tag regex skips quoted attribute values, so a ``>``
      inside ``title="a > b"`` cannot terminate the tag early;
    - close tags tolerate whitespace after the slash (``</ p>``).
    """
    if len(value) > _MAX_HTML_TO_TEXT_INPUT:
        raise ValueError(
            f"Input too large for HTML conversion "
            f"({len(value)} > {_MAX_HTML_TO_TEXT_INPUT} characters)"
        )
    text = re.sub(r"(?i)<br\s*/?>", "\n", value)
    # Non-tag constructs: comments, doctype, CDATA, script/style bodies.
    # Each tolerates truncation (no closing delimiter) via the |$ branch.
    # The script/style/doctype opening-tag classes mirror the generic
    # regex's quoted branches ("[^\"]*" / '[^']*') so a "<" inside a
    # quoted attribute value cannot defeat the body strip, while each
    # branch stays bounded by the next quote (or "<") — excluding BOTH
    # "<" and ">" from the unquoted class ([^<>]) keeps every match
    # attempt bounded and the sanitizer linear (QA Round 12, F12-1,
    # HIGH; QA Round 13, R13-1).
    #
    # QA Round 14 (F14-1, HIGH): the R13-1 class (?:[^<>]|"[^"]*"|'[^']*')*
    # was AMBIGUOUS — [^<>] also matches quotes, so every quote could be
    # consumed singly OR as a pair, and a quote run with no closing ">"
    # made the engine explore exponentially many partitions ('<script' +
    # '"'*40 took >30s, freezing the server for every client). Excluding
    # quotes from the plain branch ([^<>"']) gives every character
    # position EXACTLY ONE matching branch, so the match is linear. The
    # optional ['"]? before ">" preserves the pre-fix strip of a lone
    # UNPAIRED trailing quote (malformed odd-quote opening tags) without
    # reintroducing ambiguity: the loop is still deterministic, and the
    # optional quote adds at most one extra parse per backtrack state.
    #
    # QA Round 15 (F15-3): the CLOSING tags now use the same deterministic
    # quoted-branch class as the openings. `</script\s*>` didn't match
    # attributed closing tags (`</script data-x="a>b">`), so
    # `.*?(?:</script\s*>|$)` ran to end-of-string and deleted content
    # AFTER the malformed close (KEEP was lost). Attributed closes are
    # still closes: a quoted ">" inside the closing tag's attributes is
    # skipped the same way as in an opening tag, and the branch classes
    # stay mutually exclusive, so the match remains linear.
    text = re.sub(r"(?s)<!--.*?(?:-->|$)", "", text)
    text = re.sub(
        r"""(?is)<!doctype\b(?:[^<>"']|"[^"]*"|'[^']*')*['"]?>""", "", text
    )
    text = re.sub(r"(?is)<!\[cdata\[.*?(?:\]\]>|$)", "", text)
    text = re.sub(
        r"""(?is)<script\b(?:[^<>"']|"[^"]*"|'[^']*')*['"]?>"""
        r""".*?(?:</script\b(?:[^<>"']|"[^"]*"|'[^']*')*['"]?>|$)""",
        "",
        text,
    )
    text = re.sub(
        r"""(?is)<style\b(?:[^<>"']|"[^"]*"|'[^']*')*['"]?>"""
        r""".*?(?:</style\b(?:[^<>"']|"[^"]*"|'[^']*')*['"]?>|$)""",
        "",
        text,
    )
    text = re.sub(r"(?i)</\s*(p|div|li|h[1-6]|tr)\s*>", "\n", text)
    # Generic tag strip: quoted attribute values may contain ">" (and
    # "<"), so a tag is "<" + optional "/" + optional whitespace + a
    # letter + any mix of quoted strings and non-quote/non-">" characters
    # + ">". "<" is EXCLUDED from the unquoted loop class ([^"'<>]) —
    # QA Round 11 (F11-1, HIGH): the old [^"'>] also consumed "<", so
    # repeated unclosed tag openings ("<a"*n) forced O(n) backtracking
    # per "<" start -> O(n²) (40KB took ~47s, freezing the single-threaded
    # server for every client). With "<" excluded, each match attempt is
    # bounded by the next unquoted "<" and the sanitizer is linear.
    text = re.sub(
        r"<(?:\s*/?\s*[a-zA-Z](?:\"[^\"]*\"|'[^']*'|[^\"'<>])*)>",
        "",
        text,
    )
    text = html.unescape(text)
    return re.sub(r"[ \t\r\f\v\u00a0]+", " ", text).strip()


def _rendered_to_text_key(key: str) -> str:
    """Map a ``*_rendered`` key to its plain-text key name.

    ``introduction_rendered`` -> ``introduction_text``,
    ``conclusion_rendered`` -> ``conclusion_text``, and the step-line
    ``text_rendered`` -> ``text`` (so it does not become the awkward
    ``text_text``).
    """
    stem = key[: -len("_rendered")]
    return stem if stem == "text" else f"{stem}_text"


def _consume_task_exception(task: asyncio.Task) -> None:
    """Retrieve a finished fetch task's exception, if any (QA Round 4).

    Done-callback registered on every in-flight fetch task so its
    exception is always consumed once it finishes: when a creator is
    cancelled mid-flight and the shielded fetch then fails with no
    waiters left, nobody awaits the task — without this, asyncio logs
    "Task exception was never retrieved" at task destruction. Retrieving
    via ``exception()`` suppresses that warning while leaving the
    exception retrievable for any waiter that does await the task (they
    still see the real exception).
    """
    if task.cancelled():
        return
    try:
        task.exception()
    except Exception:  # pragma: no cover - defensive; see add_done_callback
        pass


class IfixitClient:
    """Async client for the iFixit API 2.0.

    Features:
    - Descriptive User-Agent on every request.
    - Bounded in-memory TTL cache (deep-copied on read and write).
    - Simple rate limiter (minimum interval between requests).
    - Automatic retry with exponential backoff on 429 / Retry-After.
    - Error mapping: 400/401/403 -> ValueError; 404 -> NotFoundError (a
      ValueError subclass) with a clear message.
    """

    def __init__(
        self,
        user_agent: str = DEFAULT_USER_AGENT,
        min_request_interval: float = DEFAULT_MIN_INTERVAL,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": user_agent},
            follow_redirects=True,
            timeout=30.0,
        )
        self._owns_client = client is None
        self._min_interval = min_request_interval
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._last_request_time: float = 0.0
        # Serializes the rate-limit check/sleep so concurrent callers cannot
        # all pass the check and fire together (single event loop, but the
        # check spans an await, so it needs an explicit lock).
        self._rate_lock = asyncio.Lock()
        self._cache: dict[str, tuple[float, Any]] = {}
        # Per-key in-flight requests: while a fetch for a key is running,
        # concurrent callers await the same task instead of stampeding the
        # upstream API. Entries are removed by the task when it finishes
        # (success or failure), so a failed fetch can be retried.
        self._in_flight: dict[str, asyncio.Task] = {}

    async def __aenter__(self) -> "IfixitClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def clear_cache(self) -> None:
        self._cache.clear()

    # -- Cache helpers -------------------------------------------------------

    def _get_cached(self, key: str, ttl: int) -> Any | None:
        """Return a deep copy of cached data if fresh, else None."""
        if ttl <= 0:
            return None
        entry = self._cache.get(key)
        if entry is None:
            return None
        ts, data = entry
        if time.monotonic() - ts >= ttl:
            return None
        try:
            return copy.deepcopy(data)
        except RecursionError as exc:
            # Pathologically nested cached data cannot be copied; surface a
            # clean error instead of leaking a RecursionError to callers.
            raise ValueError("Unexpected API response format") from exc

    def _get_cached_ref(self, key: str, ttl: int) -> Any | None:
        """Return the cached value itself (no deep copy) if fresh, else None.

        Internal use only — the caller must NOT mutate the returned object.
        Used by get_categories navigation, which only reads the tree, so
        each browse call avoids deep-copying the ~1.5MB tree.
        """
        if ttl <= 0:
            return None
        entry = self._cache.get(key)
        if entry is None:
            return None
        ts, data = entry
        if time.monotonic() - ts >= ttl:
            return None
        return data

    def _set_cached(self, key: str, data: Any) -> None:
        # Deep copy on write so the caller can't mutate the cache by
        # mutating an object passed in (mirrors the deep copy on read).
        self._cache[key] = (time.monotonic(), copy.deepcopy(data))
        self._evict_if_needed()

    def _evict_if_needed(self) -> None:
        if len(self._cache) <= MAX_CACHE_SIZE:
            return
        ordered = sorted(self._cache, key=lambda k: self._cache[k][0])
        excess = len(self._cache) - MAX_CACHE_SIZE
        for key in ordered[:excess]:
            del self._cache[key]

    # -- Rate limiting -------------------------------------------------------

    async def _wait_for_rate_limit(self) -> None:
        # The lock is held across the sleep so concurrent callers serialize:
        # each one observes the previous caller's timestamp and waits its
        # turn, instead of all passing the check and firing together.
        async with self._rate_lock:
            now = time.monotonic()
            elapsed = now - self._last_request_time
            if elapsed < self._min_interval:
                await asyncio.sleep(self._min_interval - elapsed)
            self._last_request_time = time.monotonic()

    # -- Core request with retry ---------------------------------------------

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | list[tuple[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Make an HTTP request with rate limiting and 429 retry.

        On 429, retries with exponential backoff (honoring Retry-After header).
        Raises httpx.HTTPStatusError after max_retries exhausted.
        """
        for attempt in range(self._max_retries + 1):
            await self._wait_for_rate_limit()
            response = await self._client.request(
                method, url, params=params, headers=headers
            )
            if response.status_code == 429:
                if attempt == self._max_retries:
                    raise httpx.HTTPStatusError(
                        f"Rate limited after {self._max_retries + 1} attempts",
                        request=response.request,
                        response=response,
                    )
                retry_after = response.headers.get("Retry-After")
                delay = None
                if retry_after:
                    try:
                        parsed = float(retry_after)
                    except ValueError:
                        parsed = float("nan")
                    # Reject non-finite/negative Retry-After values (e.g.
                    # "nan", "-1") — asyncio.sleep would raise on them.
                    if math.isfinite(parsed) and parsed >= 0:
                        delay = parsed
                if delay is None:
                    delay = self._backoff_base * (2**attempt)
                # QA Round 15 (F15-4): a huge finite Retry-After used to
                # be honored unbounded ('3600' -> 1-hour stall, '9e9' ->
                # ~285 years), freezing every request for this key. Cap
                # the honored delay at MAX_RETRY_AFTER, the backoff
                # path's ceiling; a server asking for more is retried
                # after the cap. Invalid/non-finite headers still fall
                # back to the exponential backoff above.
                await asyncio.sleep(min(delay, MAX_RETRY_AFTER))
                continue
            return response

        raise RuntimeError("Exhausted retries")  # pragma: no cover

    # -- Response handling -----------------------------------------------------

    def _handle_response(self, response: httpx.Response) -> httpx.Response:
        """Map HTTP errors to ValueError; 429 is retried in _request.

        400 -> ValueError("bad request")
        401 -> ValueError("auth required")
        403 -> ValueError("forbidden")
        404 -> NotFoundError("not found")  # a ValueError subclass
        Any other non-2xx -> httpx.HTTPStatusError via raise_for_status.
        """
        if response.status_code == 429:
            # Handled by the retry loop in _request; never raise here.
            return response
        if response.status_code == 400:
            raise ValueError("bad request")
        if response.status_code == 401:
            raise ValueError("auth required")
        if response.status_code == 403:
            raise ForbiddenError("forbidden")
        if response.status_code == 404:
            raise NotFoundError("not found")
        response.raise_for_status()
        return response

    # -- Core GET wrapper -------------------------------------------------------

    async def get(
        self,
        path: str,
        params: dict[str, Any] | list[tuple[str, Any]] | None = None,
        cache_ttl: int = TTL_30MIN,
    ) -> Any:
        """GET a JSON resource from the iFixit API, cached for *cache_ttl* seconds.

        Returns the parsed JSON (deep copy so callers can't corrupt the cache).
        Raises ValueError for 400/401/403, NotFoundError (a ValueError
        subclass) for 404, and httpx.HTTPStatusError for other non-2xx
        responses. A pathologically nested response body raises a clean
        ValueError("Unexpected API response format"), never a RecursionError.

        Concurrent callers for the same key share one in-flight request
        (cache stampede dedup): while a fetch is running, later callers
        await the same task instead of firing their own upstream request.
        """
        if not path or not path.startswith("/"):
            raise ValueError("path must start with '/'")

        cache_key = self._cache_key(path, params)
        cached = self._get_cached(cache_key, cache_ttl)
        if cached is not None:
            return cached

        in_flight = self._in_flight.get(cache_key)
        if in_flight is not None:
            # Another caller is already fetching this resource — share the
            # in-flight request. The task's result object is shared, so hand
            # each waiter its own deep copy (matches the cache-hit contract:
            # callers own the returned object exclusively).
            #
            # The shared task is awaited through asyncio.shield so a
            # waiter's own cancellation never cancels the fetch for the
            # other callers (QA Round 3: one cancelled waiter used to kill
            # the shared request — and every other waiter — with
            # CancelledError).
            try:
                result = await asyncio.shield(in_flight)
            except asyncio.CancelledError:
                # This waiter was cancelled; the shielded fetch keeps
                # running for the other waiters. Re-raise.
                raise
            try:
                return copy.deepcopy(result)
            except RecursionError as exc:
                # The shared result is pathologically nested — the JSON
                # parser accepted it but deepcopy overflows. Surface the
                # same clean error as the other guarded copy paths instead
                # of leaking a RecursionError to waiters.
                raise ValueError("Unexpected API response format") from exc

        task = asyncio.create_task(
            self._run_fetch_and_cache(cache_key, path, params, cache_ttl)
        )
        # No await between the marker check and this assignment, so two
        # concurrent callers cannot both create a task for the same key.
        self._in_flight[cache_key] = task
        # Always consume the fetch task's exception once it finishes:
        # when a creator is cancelled mid-flight and the shielded fetch
        # then fails with no waiters left, nobody awaits the task and
        # asyncio would log "Task exception was never retrieved" at task
        # destruction. Retrieving via exception() suppresses that warning
        # while leaving the exception retrievable for anyone who does
        # await the task (QA Round 4).
        task.add_done_callback(_consume_task_exception)
        try:
            # Shielded for the same reason as the waiter path: if this
            # creator is cancelled, the fetch must keep running for the
            # waiters (the wrapper below pops the marker when it finishes).
            return await asyncio.shield(task)
        finally:
            # Creator-side marker guard: a task cancelled before its first
            # step never executes its own body (finally included), so the
            # marker would otherwise stay poisoned with a done+cancelled
            # task and every later get() on this key would raise
            # CancelledError forever. Only pop when this task is still the
            # registered marker AND finished; a mid-flight fetch keeps its
            # marker so subsequent callers join it instead of stampeding.
            if self._in_flight.get(cache_key) is task and task.done():
                self._in_flight.pop(cache_key, None)

    async def _run_fetch_and_cache(
        self,
        cache_key: str,
        path: str,
        params: dict[str, Any] | list[tuple[str, Any]] | None,
        cache_ttl: int,
    ) -> Any:
        """Run one fetch while owning its in-flight marker.

        Pops the marker on success, failure, or cancellation — the cleanup
        that runs whenever the task body actually executes (the one case
        it cannot cover — a task cancelled before its first step — is
        handled by the creator-side guard in get()).
        """
        try:
            return await self._fetch_and_cache(cache_key, path, params, cache_ttl)
        finally:
            self._in_flight.pop(cache_key, None)

    async def _fetch_and_cache(
        self,
        cache_key: str,
        path: str,
        params: dict[str, Any] | list[tuple[str, Any]] | None,
        cache_ttl: int,
    ) -> Any:
        """Perform the actual GET and store the result.

        The in-flight marker is owned by the caller (_run_fetch_and_cache),
        which pops it on success, failure, or cancellation.
        """
        resp = await self._request("GET", f"{BASE_URL}{path}", params=params)
        self._handle_response(resp)
        try:
            data = resp.json()
        except (json.JSONDecodeError, RecursionError) as exc:
            # A 200 with a non-JSON body (HTML error page, empty body)
            # raises json.JSONDecodeError; deeply nested JSON overflows
            # the parser's recursion limit. Both surface as a clean
            # error instead of leaking the raw exception (QA Round 12,
            # F12-6; the RecursionError guard predates it).
            raise ValueError("Unexpected API response format") from exc
        # Guard against unexpected payload shapes (e.g. error pages or
        # scalars) BEFORE anything enters the cache. Top-level lists are
        # legitimate: /users/{id}/guides and /teams return arrays.
        if not isinstance(data, (dict, list)):
            raise ValueError("Unexpected API response format")
        if cache_ttl > 0:
            try:
                self._set_cached(cache_key, data)
            except RecursionError as exc:
                # _set_cached deep-copies on write; a pathological payload
                # that the parser accepted (or a pre-existing deep entry)
                # must not leak RecursionError to callers.
                raise ValueError("Unexpected API response format") from exc
        # Safe to return the local object directly: _set_cached deep-copies
        # on write, so the creator of this task owns this instance
        # exclusively (waiters deep-copy in get()).
        return data

    def _cache_key(
        self,
        path: str,
        params: dict[str, Any] | list[tuple[str, Any]] | None,
    ) -> str:
        """Deterministic cache key from path + params (dict sorted by key).

        Params serialize as a JSON array of [key, value] pairs so distinct
        param lists can never collide: [("a","x,b=y")] and [("a","x"),
        ("b","y")] both used to key to "a=x,b=y" (QA Round 3). The params
        segment is ALWAYS present (``[]`` when empty): a bare
        ``get:{path}`` key collided with ``get:{path}:{encoded}`` when a
        path literally ended in the serialized params string (QA Round 4).
        """
        if isinstance(params, dict):
            items = sorted(params.items())
        else:
            items = list(params) if params else []
        encoded = json.dumps(items, sort_keys=True, separators=(",", ":"))
        return f"get:{path}:{encoded}"

    # -- Guide methods ---------------------------------------------------------

    async def get_guide(
        self,
        guideid: int | str,
        detail: str = "summary",
        lang: str | None = None,
        max_steps: int | str | None = None,
    ) -> dict:
        """Fetch a repair guide by id (cached, TTL_30MIN).

        *detail* controls compaction of the response copy:
        - "summary" (default): metadata + parts/tools lists + step count and
          step titles only. Strips ``*_raw`` fields, ``revisionid``,
          ``patrol_threshold``, ``can_edit``, ``comments`` and ``flags``;
          rendered HTML (``*_rendered``) is converted to plain text and
          renamed to ``*_text``. Steps with an empty title fall back to the
          first line's plain text (truncated to 80 chars).
        - "full": everything, but ``*_raw`` fields are stripped and rendered
          HTML (``*_rendered``) is converted to plain text and renamed to
          ``*_text`` (step-line ``text_rendered`` becomes ``text``).
          *max_steps* (optional) truncates the steps list.

        *max_steps* accepts an int or a numeric string (the tool layer
        str-types the parameter; QA Round 6 F1).

        Raises ValueError("Guide not found: {id}") on 404.
        """
        guide_id = _validate_guideid(guideid)
        if detail not in ("summary", "full"):
            raise ValueError("detail must be 'summary' or 'full'")
        if max_steps is not None:
            max_steps = _validate_positive_int("max_steps", max_steps)

        params: dict[str, Any] = {}
        if lang:
            # QA Round 14 (F14-2): lang was the one uncapped string param
            # — "L"*100001 hit httpx's InvalidURL ("URL component 'query'
            # too long") after unbounded pre-URL work, and the bare
            # InvalidURL escaped the tool except-family (it is not an
            # httpx.HTTPError). Cap it like search's lang (R13-5).
            _validate_string_length("lang", lang)
            params["langid"] = lang

        try:
            data = await self.get(f"/guides/{guide_id}", params=params)
        except NotFoundError as exc:
            # Translate the generic 404 mapping into a per-guide message.
            raise ValueError(f"Guide not found: {guide_id}") from exc

        # QA Round 5 (R5-1): every other public method guards the top-level
        # shape after get(); get_guide was missing it — a 200 with a
        # top-level LIST used to raise a bare AttributeError ("'list' object
        # has no attribute 'items'") in summary mode and silently return the
        # list in full mode (wrong-shape success).
        if not isinstance(data, dict):
            raise ValueError("Unexpected API response format")

        if detail == "summary":
            return self._summarize_guide(data)
        return self._full_guide(data, max_steps)

    async def list_guides(
        self,
        filter_type: str | None = None,
        offset: int = 0,
        limit: int = GUIDES_DEFAULT_LIMIT,
        modified_since: str | None = None,
    ) -> list[dict]:
        """List guide summaries (paginated), optionally filtered by guide type.

        *filter_type* maps to the ``filter`` query param (e.g. "replacement",
        "repair", "disassembly"). *modified_since* maps to ``modifiedSince``.
        *limit* must be 1-200 (server clamps at 200), *offset* >= 0.

        Never cached: the endpoint is paginated and mutable.
        """
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ValueError("limit must be an integer between 1 and 200")
        if limit < GUIDES_MIN_LIMIT or limit > GUIDES_MAX_LIMIT:
            raise ValueError("limit must be an integer between 1 and 200")
        if not isinstance(offset, int) or isinstance(offset, bool):
            raise ValueError("offset must be a non-negative integer")
        if offset < 0:
            raise ValueError("offset must be a non-negative integer")

        params: list[tuple[str, Any]] = [("offset", offset), ("limit", limit)]
        if filter_type:
            # QA Round 14 (F14-3): same string-param cap as every other
            # user-supplied string (R13-5 posture) — an absurd filter_type
            # used to reach URL building and raise a bare InvalidURL.
            _validate_string_length("filter_type", filter_type)
            params.append(("filter", filter_type))
        if modified_since:
            _validate_string_length("modified_since", modified_since)
            params.append(("modifiedSince", modified_since))

        data = await self.get("/guides", params=params, cache_ttl=0)
        if not isinstance(data, list):
            raise ValueError("Unexpected API response format")
        return data

    # -- Search ----------------------------------------------------------------

    async def search(
        self,
        query: str,
        doctypes: str = "guide",
        device: str | None = None,
        lang: str | None = None,
    ) -> dict:
        """Search the iFixit catalog via GET /suggest/{query}.

        Returns ``{"query": ..., "results": [...]}`` — results are the API's
        own compact result objects (≤10, discriminator ``dataType``).
        Never cached: suggestions are volatile.

        *doctypes* is a comma-separated list drawn from the documented set
        (guide, item, topic, device, category, question, post, all; aliases
        accepted as documented). *device* scopes results via the
        ``guideDevice`` param, *lang* via ``langid``.
        """
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        query = query.strip()
        _validate_string_length("query", query)

        if not isinstance(doctypes, str):
            raise ValueError(
                "doctypes must be a comma-separated list of valid types"
            )
        # Length cap before the split/join so an absurd doctypes value
        # fails cleanly (QA Round 13, R13-5).
        _validate_string_length("doctypes", doctypes)
        requested = [d.strip() for d in doctypes.split(",")]
        if any(d not in SEARCH_DOCTYPES for d in requested):
            raise ValueError(
                "doctypes must be one or more of: "
                + ", ".join(sorted(SEARCH_DOCTYPES))
            )

        params: dict[str, Any] = {"doctypes": ",".join(requested)}
        if device:
            _validate_string_length("device", device)
            params["guideDevice"] = device
        if lang:
            _validate_string_length("lang", lang)
            params["langid"] = lang

        data = await self.get(
            f"/suggest/{quote(query, safe='')}", params=params, cache_ttl=0
        )
        if not isinstance(data, dict):
            raise ValueError("Unexpected API response format")
        results = data.get("results")
        if not isinstance(results, list):
            raise ValueError("Unexpected API response format")
        return {"query": query, "results": results}

    # -- Categories --------------------------------------------------------------

    async def get_categories(self, path: str | None = None) -> list[str]:
        """List category names from the /categories tree (cached, TTL_30MIN).

        With no *path* (or an empty string), returns the top-level category
        names (up to ~16), sorted. With *path* (e.g. "Mac" or
        "Mac/Mac Laptop"), navigates the nested tree by splitting on ``/``
        and returns that subtree's children names, sorted. A leaf or null
        subtree yields ``[]``; an unknown branch raises ValueError. Segments
        are whitespace-stripped; empty segments (consecutive slashes) are
        rejected — all path validation happens BEFORE any network IO.

        Only projected name lists are ever returned — the raw ~1.5MB tree
        never leaves the client. Navigation reads the cached tree directly
        (no deep copy per call); the tree is only copied when it is first
        stored.
        """
        if path is not None:
            if not isinstance(path, str):
                raise ValueError(
                    "path must be a string like 'Mac' or 'Mac/Mac Laptop'"
                )
            # Length cap before any tree navigation or IO (QA Round 13,
            # R13-5).
            _validate_string_length("path", path)
            # Empty/whitespace-only path means "top level".
            if not path.strip():
                path = None
        if path is not None:
            segments = [segment.strip() for segment in path.split("/")]
            if any(not segment for segment in segments):
                raise ValueError(
                    "path must not contain empty segments (e.g. 'Mac//MacBook')"
                )
        else:
            segments = []

        # Read the cached tree WITHOUT deep-copying it: navigation below is
        # strictly read-only (dict lookups + sorted keys), and copying the
        # ~1.5MB tree on every browse call would be wasteful.
        cache_key = self._cache_key("/categories", None)
        data = self._get_cached_ref(cache_key, TTL_30MIN)
        if data is None:
            data = await self.get("/categories")
        if not isinstance(data, dict):
            raise ValueError("Unexpected API response format")

        node: Any = data
        for segment in segments:
            if not isinstance(node, dict) or segment not in node:
                raise ValueError(f"Category not found: {path}")
            node = node[segment]
            # A subtree value that is neither a nested dict nor null is a
            # malformed tree — reject it instead of returning [].
            if node is not None and not isinstance(node, dict):
                raise ValueError("Unexpected API response format")

        if not isinstance(node, dict):
            return []
        keys = list(node.keys())
        # JSON keys are always strings, but guard the sort so a mixed-type
        # key set raises a clean ValueError, not a TypeError.
        if not all(isinstance(k, str) for k in keys):
            raise ValueError("Unexpected API response format")
        return sorted(keys)

    # -- Device / wiki methods ------------------------------------------------

    async def get_device(
        self, title: str, namespace: str = "CATEGORY"
    ) -> dict:
        """Fetch a device wiki page, projected to a compact summary (cached).

        GET /wikis/{namespace}/{title} (cache TTL_30MIN). The raw wiki
        response (~238KB for a busy device, 39 keys per RESEARCH.md §3e) is
        projected to: ``title``, ``display_title``, ``repairability_score``
        (omitted when unset or null — iFixit only publishes scores for some
        devices), ``summary`` (the wiki ``description`` truncated to 500
        chars), ``featured_guides`` (title/guideid/url only), ``children``
        (names only), ``parts_count`` / ``tools_count`` (from the parts/tools
        lists; parts may be an object ``{url, categories: [{tag, count}]}``,
        in which case the count is the sum of the category counts; 0 when
        absent), and ``ancestors`` (breadcrumb names only).

        Strips contents_raw/contents_rendered/contents_json, linked_wikis,
        related_wikis, flags, revisionid and source_revisionid.

        Raises ValueError("Device not found: {title}") on 404.
        """
        title = _validate_title(title)
        namespace = _validate_namespace(namespace)
        try:
            data = await self.get(
                f"/wikis/{quote(namespace, safe='')}/{quote(title, safe='')}"
            )
        except NotFoundError as exc:
            # Translate the generic 404 mapping into a per-device message.
            raise ValueError(f"Device not found: {title}") from exc
        if not isinstance(data, dict):
            raise ValueError("Unexpected API response format")
        return self._project_device(data)

    async def list_device_guides(
        self, title: str, namespace: str = "CATEGORY"
    ) -> list[dict]:
        """List a device's guides, compacted (cached, TTL_30MIN).

        GET /wikis/{namespace}/{title} and merge the wiki's ``guides`` and
        ``featured_guides`` lists — guides first — deduplicated by guideid.
        Each item is compacted to {guideid, title, url, difficulty,
        time_required_max, image_thumbnail}; optional keys that are absent
        (or null) are omitted rather than fabricated.

        Raises ValueError("Device not found: {title}") on 404.
        """
        title = _validate_title(title)
        namespace = _validate_namespace(namespace)
        try:
            data = await self.get(
                f"/wikis/{quote(namespace, safe='')}/{quote(title, safe='')}"
            )
        except NotFoundError as exc:
            raise ValueError(f"Device not found: {title}") from exc
        if not isinstance(data, dict):
            raise ValueError("Unexpected API response format")

        merged: list[dict] = []
        seen: set[Any] = set()
        for source in (data.get("guides"), data.get("featured_guides")):
            if not isinstance(source, list):
                continue
            for item in source:
                if not isinstance(item, dict):
                    continue
                guideid = item.get("guideid")
                if guideid is None:
                    continue
                # QA Round 5 (R5-2): the dedup set requires a hashable
                # scalar — a list/dict guideid used to raise a bare
                # TypeError("unhashable type: 'list'"), and a bool would
                # silently alias guideid 1 (True == 1 in a set). Reject
                # non-scalar ids with the clean family message.
                if not isinstance(guideid, (str, int)) or isinstance(
                    guideid, bool
                ):
                    raise ValueError("Unexpected API response format")
                if guideid in seen:
                    continue
                seen.add(guideid)
                merged.append(self._compact_guide_item(item))
        return merged

    async def get_maintenance_schedule(
        self, title: str, namespace: str = "CATEGORY"
    ) -> dict:
        """Fetch a device's maintenance schedule (cached, TTL_30MIN).

        GET /wikis/maintenance/{namespace}/{title}. Returns
        {"schedules": [...]} where each schedule carries task/status/triggers
        (schedules are stable, so the response is cached). When the API
        reports an inherited schedule, the dict also carries
        ``inherited_from`` (the name of the parent device the schedules were
        inherited from). A device with no maintenance schedule still returns
        200 with ``{"schedules": []}`` (verified live) — that is the normal
        "no schedule" signal, not an error.

        Raises ValueError("No maintenance schedule for: {title}") on 404 —
        which the live API returns only for unknown/nonexistent titles.
        """
        title = _validate_title(title)
        namespace = _validate_namespace(namespace)
        try:
            data = await self.get(
                f"/wikis/maintenance/{quote(namespace, safe='')}/"
                f"{quote(title, safe='')}"
            )
        except NotFoundError as exc:
            raise ValueError(
                f"No maintenance schedule for: {title}"
            ) from exc
        if not isinstance(data, dict):
            raise ValueError("Unexpected API response format")
        schedules = data.get("schedules")
        if not isinstance(schedules, list):
            return {"schedules": []}
        result: dict[str, Any] = {"schedules": schedules}
        inherited = data.get("inheritedFrom")
        if inherited is not None:
            result["inherited_from"] = inherited
        return result

    # -- Media / user methods --------------------------------------------------

    async def get_media(
        self, media_id: int | str, media_type: str = "images"
    ) -> dict:
        """Fetch a media object's CDN URLs by id (cached, TTL_HOUR).

        GET /media/{media_type}/{media_id}. *media_type* must be one of
        ``images``, ``videos``, ``documents`` (validated before any network
        IO). Returns the API's own compact image object
        (``{image: {id, guid, mini, thumbnail, ..., original}}``,
        RESEARCH.md §3g) passed through as-is; ungenerated sizes are simply
        absent.

        Raises ValueError("Media not accessible") on 403 and
        ValueError("Media not found: {id}") on 404.
        """
        media_id_int = _validate_positive_int("media_id", media_id)
        if not isinstance(media_type, str) or media_type not in MEDIA_TYPES:
            raise ValueError(
                "media_type must be one of: images, videos, documents"
            )

        try:
            data = await self.get(
                f"/media/{media_type}/{media_id_int}", cache_ttl=TTL_HOUR
            )
        except NotFoundError as exc:
            raise ValueError(f"Media not found: {media_id_int}") from exc
        except ForbiddenError as exc:
            # Permission-checked endpoint: translate the 403 sentinel into a
            # per-media message (assets not referenced by viewable content).
            raise ValueError("Media not accessible") from exc
        if not isinstance(data, dict):
            raise ValueError("Unexpected API response format")
        return data

    async def get_user(self, user_id: int | str) -> dict:
        """Fetch a user profile by id (cached, TTL_30MIN).

        GET /users/{user_id}. Returns the API's compact profile dict
        (~13 keys per RESEARCH.md §3f: userid, username, unique_username,
        image, teams, reputation, join_date, certification_count,
        badge_counts, summary, ...) passed through as-is except for the
        about section, which is sanitized: ``about_rendered`` (raw HTML,
        e.g. imageBox divs with onclick imgs) is converted to plain text
        and renamed ``about_text`` (omitted when absent or non-string),
        and the wiki-markup ``about_raw`` is dropped entirely — raw HTML
        or wiki markup never leaves the server (same posture as guide
        summary output). All other fields pass through untouched.

        Raises ValueError("User not found: {id}") on 404.
        """
        user_id_int = _validate_positive_int("user_id", user_id)
        try:
            data = await self.get(f"/users/{user_id_int}")
        except NotFoundError as exc:
            # Translate the generic 404 mapping into a per-user message.
            raise ValueError(f"User not found: {user_id_int}") from exc
        if not isinstance(data, dict):
            raise ValueError("Unexpected API response format")
        profile = dict(data)
        # HTML hygiene on the about section (get() already hands back an
        # exclusive copy, so the shallow copy here is just defensive).
        profile.pop("about_raw", None)
        about_rendered = profile.pop("about_rendered", None)
        if isinstance(about_rendered, str):
            profile["about_text"] = _html_to_text(about_rendered)
        # A missing or non-string about_rendered simply yields no
        # about_text key — nothing fabricated, nothing leaked.
        return profile

    async def list_user_guides(
        self, user_id: int | str, offset: int = 0, limit: int | str = 20
    ) -> list[dict]:
        """List a user's guide summaries (paginated).

        GET /users/{user_id}/guides. *limit* must be 1-200 (server clamps
        at 200), *offset* >= 0; *limit* accepts an int or a numeric string
        (the get_user tool str-types the parameter; QA Round 6 F1).
        Returns the API's GuideSummary array passed through as-is (the
        server tool layer projects further).

        Never cached: the endpoint is paginated and mutable.

        Raises ValueError("User not found: {id}") on 404.
        """
        user_id_int = _validate_positive_int("user_id", user_id)
        limit = _validate_positive_int("limit", limit)
        if limit > GUIDES_MAX_LIMIT:
            raise ValueError("limit must be an integer between 1 and 200")
        if not isinstance(offset, int) or isinstance(offset, bool):
            raise ValueError("offset must be a non-negative integer")
        if offset < 0:
            raise ValueError("offset must be a non-negative integer")

        try:
            data = await self.get(
                f"/users/{user_id_int}/guides",
                params=[("offset", offset), ("limit", limit)],
                cache_ttl=0,
            )
        except NotFoundError as exc:
            # Translate the generic 404 mapping into a per-user message.
            raise ValueError(f"User not found: {user_id_int}") from exc
        if not isinstance(data, list):
            raise ValueError("Unexpected API response format")
        return data

    # -- Guide response compaction ----------------------------------------------

    def _summarize_guide(self, data: dict) -> dict:
        """Compact a full guide response to metadata + parts/tools + step titles.

        ``*_raw`` fields, revision metadata and comment/flags noise are
        dropped; string ``*_rendered`` HTML fields (introduction/
        conclusion) are converted to plain text and renamed to ``*_text``
        so summary-mode output never leaks raw HTML. A non-string
        ``*_rendered`` value (null, number, object) has no text to
        convert and is OMITTED entirely — it is never passed through
        still named ``*_rendered`` (a key that promises HTML) and never
        renamed to ``*_text`` (QA Round 13, R13-6). Step entries carry
        ``stepid`` and ``title``; a step with an empty title falls back
        to the first non-empty line's plain text (truncated to 80 chars)
        — live teardown guides often leave titles blank.
        """
        summary: dict[str, Any] = {}
        for key, value in data.items():
            if key.endswith("_raw") or key in (
                "revisionid",
                "patrol_threshold",
                "can_edit",
                "comments",
                "flags",
            ):
                continue
            if key.endswith("_rendered"):
                if isinstance(value, str):
                    summary[_rendered_to_text_key(key)] = _html_to_text(value)
                # Non-string *_rendered: omit (R13-6) — see docstring.
                continue
            summary[key] = value
        steps = data.get("steps")
        if steps is None:
            steps = []
        elif not isinstance(steps, list):
            # A non-list "steps" — including FALSY malformed shapes like
            # 0, "", {} and False, which "or []" used to swallow as an
            # empty step list — is a malformed payload. Surface the clean
            # ValueError instead of a bare TypeError or silently empty
            # steps (QA Round 3/4).
            raise ValueError("Unexpected API response format")
        summarized_steps: list[dict[str, Any]] = []
        for step in steps:
            if not isinstance(step, dict):
                # Malformed step entries must raise rather than be silently
                # filtered out (QA Round 3).
                raise ValueError("Unexpected API response format")
            title = step.get("title")
            if title is not None and not isinstance(title, str):
                # A non-string title would blow up .strip() with a bare
                # AttributeError (QA Round 3). Missing/None titles remain
                # valid and take the line-text fallback below.
                raise ValueError("Unexpected API response format")
            summarized_steps.append(
                {
                    "stepid": step.get("stepid"),
                    "title": (title or "").strip()
                    or self._step_title_fallback(step),
                }
            )
        summary["steps"] = summarized_steps
        return summary

    def _step_title_fallback(self, step: dict) -> str:
        """Derive a step title from the first non-empty line's plain text.

        Used when a step's ``title`` is empty (common on teardown guides).
        Truncates to 80 characters; returns "" when there is no usable text.
        """
        lines = step.get("lines")
        if not isinstance(lines, list):
            return ""
        for line in lines:
            if not isinstance(line, dict):
                continue
            rendered = line.get("text_rendered")
            if isinstance(rendered, str) and rendered.strip():
                text = _html_to_text(rendered)
                if text:
                    return text[:80]
        return ""

    # -- Device wiki projection ------------------------------------------------

    def _project_device(self, data: dict) -> dict:
        """Project a raw wiki response to the compact get_device summary."""
        summary: dict[str, Any] = {}
        for key in ("title", "display_title"):
            if key in data:
                summary[key] = data[key]
        ancestors = data.get("ancestors")
        if ancestors is not None:
            summary["ancestors"] = self._ancestor_names(ancestors)
        score = data.get("repairability_score")
        if score is not None:
            summary["repairability_score"] = score
        description = data.get("description")
        if isinstance(description, str) and description.strip():
            summary["summary"] = description[:500]
        summary["featured_guides"] = self._compact_featured_guides(
            data.get("featured_guides")
        )
        summary["children"] = self._child_names(data.get("children"))
        parts = data.get("parts")
        summary["parts_count"] = self._parts_count(parts)
        tools = data.get("tools")
        summary["tools_count"] = len(tools) if isinstance(tools, list) else 0
        return summary

    def _parts_count(self, parts: Any) -> int:
        """Count a wiki page's parts.

        ``parts`` is a list on some namespaces and an object
        ``{url, categories: [{tag, count, url}]}`` on CATEGORY wikis
        (verified live); in the object shape the count is the sum of the
        category counts. Absent or malformed values yield 0.
        """
        if isinstance(parts, list):
            return len(parts)
        if isinstance(parts, dict):
            categories = parts.get("categories")
            if isinstance(categories, list):
                return sum(
                    item.get("count", 0)
                    for item in categories
                    if isinstance(item, dict)
                    and isinstance(item.get("count"), int)
                )
        return 0

    def _compact_featured_guides(self, guides: Any) -> list[dict]:
        """Project featured_guides entries to {title, guideid, url} only."""
        if not isinstance(guides, list):
            return []
        compact: list[dict] = []
        for guide in guides:
            if not isinstance(guide, dict):
                continue
            item: dict[str, Any] = {}
            for key in ("title", "guideid", "url"):
                value = guide.get(key)
                if value is not None:
                    item[key] = value
            compact.append(item)
        return compact

    def _child_names(self, children: Any) -> list[str]:
        """Project children entries to display names only."""
        if not isinstance(children, list):
            return []
        names: list[str] = []
        for child in children:
            if not isinstance(child, dict):
                continue
            name = child.get("display_title") or child.get("title")
            if name is not None:
                names.append(name)
        return names

    def _ancestor_names(self, ancestors: Any) -> list[str]:
        """Project ancestor wiki entries to display names only.

        The live API embeds full wiki objects in ``ancestors`` (including
        ``image`` dicts, ``summary``, ``url``); the projection keeps just the
        breadcrumb names.
        """
        if not isinstance(ancestors, list):
            return []
        names: list[str] = []
        for ancestor in ancestors:
            if not isinstance(ancestor, dict):
                continue
            name = ancestor.get("display_title") or ancestor.get("title")
            if name is not None:
                names.append(name)
        return names

    def _compact_guide_item(self, item: dict) -> dict:
        """Compact a wiki guide entry to the list_device_guides shape.

        Optional keys (difficulty, time_required_max, image_thumbnail) are
        omitted when absent or null — never fabricated.
        """
        compact: dict[str, Any] = {}
        for key in ("guideid", "title", "url", "difficulty", "time_required_max"):
            value = item.get(key)
            if value is not None:
                compact[key] = value
        image = item.get("image")
        if isinstance(image, dict):
            thumbnail = image.get("thumbnail")
            if thumbnail is not None:
                compact["image_thumbnail"] = thumbnail
        return compact

    def _full_guide(self, data: dict, max_steps: int | None) -> dict:
        """Full guide with *_raw stripped and rendered HTML converted to text.

        *data* is deep-copied BEFORE any mutation: the creator of an
        in-flight request receives the shared fetch task's result BY
        REFERENCE, and concurrent waiters deep-copy that same object when
        they resume — an in-place projection would race them, corrupting
        their copies (QA Round 4: summary waiters lost their title
        fallback text; unrestricted full waiters got the truncated step
        list). RecursionError from the copy or the strip pass
        (pathologically nested payload) is surfaced as a clean ValueError.
        """
        try:
            data = copy.deepcopy(data)
            self._strip_raw_and_convert_html(data)
        except RecursionError as exc:
            raise ValueError("Unexpected API response format") from exc
        if max_steps is not None:
            steps = data.get("steps")
            if isinstance(steps, list):
                data["steps"] = steps[:max_steps]
        return data

    def _strip_raw_and_convert_html(self, node: Any, _depth: int = 0) -> None:
        """Recursively drop *_raw keys and convert string *_rendered HTML to text.

        String ``*_rendered`` fields are renamed to ``*_text`` (step-line
        ``text_rendered`` becomes ``text``) so no key ever promises
        rendered HTML that has been replaced by plain text. A non-string
        ``*_rendered`` value (null, number, object) is OMITTED — it has
        no text to convert, and keeping the HTML-promising key name would
        lie about the value (QA Round 13, R13-6). Recursion is
        depth-capped (max 50 levels) so a pathologically nested response
        raises a clean ValueError instead of a bare RecursionError.
        """
        if _depth > MAX_HTML_CONVERT_DEPTH:
            raise ValueError(
                f"Response nesting exceeds {MAX_HTML_CONVERT_DEPTH} levels"
            )
        if isinstance(node, dict):
            for key in list(node.keys()):
                if key.endswith("_raw"):
                    del node[key]
                elif key.endswith("_rendered"):
                    if isinstance(node[key], str):
                        text = _html_to_text(node[key])
                        del node[key]
                        node[_rendered_to_text_key(key)] = text
                    else:
                        # Non-string *_rendered: omit (R13-6).
                        del node[key]
                else:
                    self._strip_raw_and_convert_html(node[key], _depth + 1)
        elif isinstance(node, list):
            for item in node:
                self._strip_raw_and_convert_html(item, _depth + 1)
