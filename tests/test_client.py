"""Tests for the IfixitClient HTTP layer."""

from __future__ import annotations

import asyncio
import gc
import time
from typing import Any

import httpx
import pytest

from ifixit_mcp.client import (
    BASE_URL,
    DEFAULT_USER_AGENT,
    MAX_CACHE_SIZE,
    MAX_RETRY_AFTER,
    ForbiddenError,
    NotFoundError,
    TTL_30MIN,
    TTL_HOUR,
    TTL_MINUTE,
    IfixitClient,
    _MAX_HTML_TO_TEXT_INPUT,
    _html_to_text,
    _rendered_to_text_key,
    _validate_string_length,
    _validate_title,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok_client(**kwargs) -> IfixitClient:
    """IfixitClient backed by a MockTransport that always returns 200."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    return IfixitClient(client=http, min_request_interval=0, **kwargs)


def _client_with(handler) -> IfixitClient:
    """IfixitClient backed by a MockTransport with the given handler."""
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    return IfixitClient(client=http_client, min_request_interval=0, backoff_base=0.01)


# ---------------------------------------------------------------------------
# Constants / User-Agent tests
# ---------------------------------------------------------------------------


def test_module_constants() -> None:
    assert BASE_URL == "https://www.ifixit.com/api/2.0"
    assert DEFAULT_USER_AGENT == "ifixit-mcp/0.1.0 (https://github.com/Dthen/ifixit-mcp)"
    assert MAX_CACHE_SIZE == 256
    assert TTL_MINUTE == 60
    assert TTL_HOUR == 3600
    assert TTL_30MIN == 1800  # mirrors iFixit CDN edge TTL
    assert MAX_RETRY_AFTER == 8.0  # F15-4: cap on honored Retry-After


def test_default_user_agent_contains_marker() -> None:
    assert "ifixit-mcp" in DEFAULT_USER_AGENT


def test_default_user_agent_has_repo_url() -> None:
    # The UA identifies the client and points at the public repository.
    assert "https://github.com/Dthen/ifixit-mcp" in DEFAULT_USER_AGENT


async def test_default_user_agent_sent() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers.get("user-agent", "")
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    ac = IfixitClient(min_request_interval=0)
    ac._client = httpx.AsyncClient(
        transport=transport, headers={"User-Agent": DEFAULT_USER_AGENT}
    )
    ac._owns_client = True
    await ac._request("GET", f"{BASE_URL}/x")
    assert "ifixit-mcp" in seen["ua"]
    await ac.aclose()


async def test_custom_user_agent_respected() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers.get("user-agent", "")
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    custom = "my-custom-agent/9.9"
    ac = IfixitClient(user_agent=custom, min_request_interval=0)
    ac._client = httpx.AsyncClient(transport=transport, headers={"User-Agent": custom})
    ac._owns_client = True
    await ac._request("GET", f"{BASE_URL}/x")
    assert seen["ua"] == custom
    await ac.aclose()


# ---------------------------------------------------------------------------
# Cache tests
# ---------------------------------------------------------------------------


def test_cache_miss_returns_none() -> None:
    ac = IfixitClient(min_request_interval=0)
    assert ac._get_cached("nope", ttl=60) is None


def test_cache_hit_returns_deep_copy() -> None:
    ac = IfixitClient(min_request_interval=0)
    data = {"items": [1, 2, 3], "nested": {"a": 1}}
    ac._set_cached("k", data)

    first = ac._get_cached("k", ttl=60)
    assert first == data
    # Mutate the returned copy.
    first["items"].append(999)
    first["nested"]["a"] = 42

    # Cache must be unaffected.
    second = ac._get_cached("k", ttl=60)
    assert second == data
    assert second["items"] == [1, 2, 3]
    assert second["nested"]["a"] == 1


def test_set_cached_stores_deep_copy_write_side_isolation() -> None:
    ac = IfixitClient(min_request_interval=0)
    data = {"items": [1, 2, 3], "nested": {"a": 1}}
    ac._set_cached("k", data)

    # Mutate the original object AFTER it was cached; the cache must be
    # unaffected (deep copy on write, not just on read).
    data["items"].append(999)
    data["nested"]["a"] = 42

    cached = ac._get_cached("k", ttl=60)
    assert cached == {"items": [1, 2, 3], "nested": {"a": 1}}
    assert cached["items"] == [1, 2, 3]
    assert cached["nested"]["a"] == 1


def test_expired_cache_entry_returns_none() -> None:
    ac = IfixitClient(min_request_interval=0)
    ac._set_cached("k", {"v": 1})
    # ttl=0 or negative bypasses / expires immediately.
    assert ac._get_cached("k", ttl=0) is None
    assert ac._get_cached("k", ttl=-1) is None


def test_cache_entry_expires_after_ttl() -> None:
    ac = IfixitClient(min_request_interval=0)
    ac._set_cached("k", {"v": 1})
    assert ac._get_cached("k", ttl=0.05) == {"v": 1}
    time.sleep(0.06)
    assert ac._get_cached("k", ttl=0.05) is None


def test_cache_key_params_are_unambiguous() -> None:
    # QA Round 3 (LOW): [("a","x,b=y")] and [("a","x"),("b","y")] both
    # keyed to "get:/p:a=x,b=y". Different param lists must never share
    # a cache key.
    ac = IfixitClient(min_request_interval=0)
    k1 = ac._cache_key("/p", [("a", "x,b=y")])
    k2 = ac._cache_key("/p", [("a", "x"), ("b", "y")])
    assert k1 != k2
    # Same logical params as dict vs list still key identically.
    assert ac._cache_key("/p", {"a": "x", "b": "y"}) == k2
    # No-params keys carry an explicit empty params segment (QA Round 4:
    # a bare "get:{path}" collided with a path ending in serialized
    # params).
    assert ac._cache_key("/p", None) == "get:/p:[]"
    assert ac._cache_key("/p", {}) == "get:/p:[]"


def test_cache_key_path_cannot_collide_with_serialized_params() -> None:
    # QA Round 4 (LOW): "get:{path}" (no params) collided with
    # "get:{path}:{encoded}" when a path literally ended in a serialized
    # params string — _cache_key("/p", [("a","x")]) and
    # _cache_key('/p:[["a","x"]]', None) used to produce the SAME key.
    ac = IfixitClient(min_request_interval=0)
    k1 = ac._cache_key("/p", [("a", "x")])
    k2 = ac._cache_key('/p:[["a","x"]]', None)
    assert k1 != k2
    # Empty params get an explicit segment so no path can masquerade as a
    # params-less key.
    assert ac._cache_key("/p", None) == "get:/p:[]"
    assert ac._cache_key("/p", {}) == "get:/p:[]"
    assert ac._cache_key("/p", []) == "get:/p:[]"


async def test_get_distinguishes_colliding_param_shapes() -> None:
    # QA Round 3 (LOW) regression: params [("a","x,b=y")] and
    # [("a","x"),("b","y")] used to collide on one cache entry, so the
    # second request was served the first's cached response.
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    ac = _client_with(handler)
    await ac.get("/p", params=[("a", "x,b=y")])
    await ac.get("/p", params=[("a", "x"), ("b", "y")])
    assert len(seen) == 2  # two distinct upstream requests
    assert len(ac._cache) == 2  # two distinct cache entries


def test_eviction_at_max_cache_size() -> None:
    ac = IfixitClient(min_request_interval=0)
    for i in range(MAX_CACHE_SIZE + 10):
        ac._set_cached(f"key-{i}", {"i": i})
    assert len(ac._cache) <= MAX_CACHE_SIZE


def test_clear_cache_empties_everything() -> None:
    ac = IfixitClient(min_request_interval=0)
    for i in range(5):
        ac._set_cached(f"key-{i}", i)
    assert len(ac._cache) == 5
    ac.clear_cache()
    assert len(ac._cache) == 0
    assert ac._get_cached("key-0", ttl=60) is None


# ---------------------------------------------------------------------------
# Rate limiter tests
# ---------------------------------------------------------------------------


async def test_first_call_is_immediate() -> None:
    ac = IfixitClient(min_request_interval=0.5)
    start = time.monotonic()
    await ac._wait_for_rate_limit()
    elapsed = time.monotonic() - start
    assert elapsed < 0.1


async def test_second_call_within_interval_is_delayed() -> None:
    ac = IfixitClient(min_request_interval=0.2)
    await ac._wait_for_rate_limit()  # first, immediate
    start = time.monotonic()
    await ac._wait_for_rate_limit()  # second, must wait ~0.2s
    elapsed = time.monotonic() - start
    assert elapsed >= 0.15


async def test_concurrent_requests_are_serialized_by_rate_limiter() -> None:
    # QA finding: the check→sleep→set sequence had no lock, so N concurrent
    # callers all passed the check before anyone slept and fired together.
    # With the asyncio.Lock held across the sleep, upstream calls must be
    # spaced by the minimum interval.
    interval = 0.5
    timestamps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        timestamps.append(time.monotonic())
        return httpx.Response(200, json={"ok": True})

    ac = IfixitClient(min_request_interval=interval, backoff_base=0.01)
    ac._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ac._owns_client = True

    await asyncio.gather(*[ac._request("GET", f"{BASE_URL}/x") for _ in range(3)])
    await ac.aclose()

    assert len(timestamps) == 3
    gaps = [b - a for a, b in zip(timestamps, timestamps[1:])]
    # Allow modest scheduling slop below the nominal interval.
    assert all(gap >= interval * 0.8 for gap in gaps), f"gaps too small: {gaps}"


# ---------------------------------------------------------------------------
# Cache stampede (in-flight dedup) tests
# ---------------------------------------------------------------------------


async def test_concurrent_identical_gets_share_one_upstream_request() -> None:
    # QA finding: 6 concurrent identical get_guide(1220) calls on a fresh
    # client produced 6 upstream requests. In-flight dedup must collapse
    # them into exactly one.
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={"guideid": 1220, "n": call_count})

    ac = _client_with(handler)
    results = await asyncio.gather(*[ac.get_guide(1220) for _ in range(6)])

    assert call_count == 1
    assert all(r == results[0] for r in results)
    assert results[0]["guideid"] == 1220
    # After the stampede, the result is cached: a later call is served from
    # the cache without any further upstream request.
    await ac.get_guide(1220)
    assert call_count == 1


async def test_concurrent_uncached_gets_share_one_upstream_request() -> None:
    # Even with cache_ttl=0 the in-flight dedup applies while the requests
    # are concurrent (volatile endpoints like /suggest).
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={"results": []})

    ac = _client_with(handler)
    await asyncio.gather(*[ac.get("/suggest/x", cache_ttl=0) for _ in range(4)])
    assert call_count == 1


async def test_failed_in_flight_fetch_can_be_retried() -> None:
    # A failed fetch must clear its in-flight marker so a subsequent call
    # retries instead of awaiting a dead task forever.
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"ok": True})

    ac = _client_with(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await ac.get("/guides/1220")
    assert len(ac._in_flight) == 0  # marker cleared on failure

    data = await ac.get("/guides/1220")
    assert data == {"ok": True}
    assert call_count == 2


async def test_concurrent_failure_propagates_to_all_waiters() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(404, json={"message": "nope"})

    ac = _client_with(handler)
    results = await asyncio.gather(
        *[ac.get("/guides/999999") for _ in range(4)],
        return_exceptions=True,
    )
    assert call_count == 1
    assert all(isinstance(r, NotFoundError) for r in results)


# ---------------------------------------------------------------------------
# Cancellation-safety tests (QA Round 3)
# ---------------------------------------------------------------------------


async def test_creator_cancelled_clears_in_flight_marker() -> None:
    # QA Round 3 (HIGH): a creator cancelled before/while its fetch runs
    # used to leave a done+cancelled task in _in_flight — every subsequent
    # get() on that key then raised CancelledError forever (a task
    # cancelled before its first step never executes its own finally).
    call_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.05)  # keep the fetch mid-flight
        return httpx.Response(200, json={"ok": True})

    ac = _client_with(handler)
    t = asyncio.create_task(ac.get("/k", cache_ttl=0))
    await asyncio.sleep(0)  # creator starts and registers the marker
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t

    # The marker must not be poisoned: a subsequent call on the same key
    # works (and reuses the shielded fetch the cancelled creator left
    # running — never awaits a dead task).
    data = await ac.get("/k", cache_ttl=0)
    assert data == {"ok": True}
    assert call_count == 1  # the cancelled creator's fetch was reused
    assert len(ac._in_flight) == 0  # marker cleaned by the time get returns


async def test_waiter_cancellation_does_not_cancel_shared_fetch() -> None:
    # QA Round 3 (HIGH): cancelling ONE waiter must not propagate into the
    # shared in-flight task — previously every other waiter (and the fetch
    # itself) died with CancelledError.
    started = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"ok": True})

    ac = _client_with(handler)
    w1 = asyncio.create_task(ac.get("/k", cache_ttl=0))
    await started.wait()  # fetch is now mid-flight
    w2 = asyncio.create_task(ac.get("/k", cache_ttl=0))
    w3 = asyncio.create_task(ac.get("/k", cache_ttl=0))
    await asyncio.sleep(0)  # w2/w3 attach as waiters
    w2.cancel()
    with pytest.raises(asyncio.CancelledError):
        await w2

    # The other waiters still get the result of the one shared fetch.
    r1 = await w1
    r3 = await w3
    assert r1 == {"ok": True}
    assert r3 == {"ok": True}
    assert len(ac._in_flight) == 0


async def test_cancelled_callers_in_stampede_do_not_poison_marker() -> None:
    # QA Round 3 (HIGH) stress: 30 concurrent gets on one key with every
    # 3rd caller cancelled mid-flight — no CancelledError may leak to the
    # uncancelled callers and the marker must never get stuck.
    gate = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        await gate.wait()  # hold the single fetch open until released
        return httpx.Response(200, json={"ok": True})

    ac = _client_with(handler)
    tasks = [asyncio.create_task(ac.get("/k", cache_ttl=0)) for _ in range(30)]
    await asyncio.sleep(0)  # all callers attach to the one in-flight fetch
    doomed = {t for i, t in enumerate(tasks) if i % 3 == 0}
    for t in doomed:
        t.cancel()
    for t in doomed:
        with pytest.raises(asyncio.CancelledError):
            await t

    gate.set()
    results = await asyncio.gather(*[t for t in tasks if t not in doomed])
    assert results == [{"ok": True}] * 20
    assert len(ac._in_flight) == 0  # marker never stuck

    # The key remains fully usable afterwards.
    assert await ac.get("/k", cache_ttl=0) == {"ok": True}
    assert len(ac._in_flight) == 0


async def test_orphaned_fetch_failure_exception_is_retrieved() -> None:
    # QA Round 4 (LOW): when a creator is cancelled mid-flight and the
    # shielded fetch then fails with NO waiters left, nobody ever awaits
    # the fetch task — asyncio used to log "Task exception was never
    # retrieved" at task destruction. The fetch task's exception must be
    # retrieved regardless (waiters that do await it still see the real
    # exception).
    started = asyncio.Event()
    released = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        started.set()
        await released.wait()
        return httpx.Response(500, text="boom")

    ac = _client_with(handler)
    loop = asyncio.get_running_loop()
    recorded: list[dict] = []
    original_handler = loop.get_exception_handler()

    def spy(context: dict) -> None:
        recorded.append(context)

    loop.set_exception_handler(spy)
    try:
        creator = asyncio.create_task(ac.get("/k", cache_ttl=0))
        await started.wait()  # fetch is mid-flight
        creator.cancel()
        with pytest.raises(asyncio.CancelledError):
            await creator
        # The shielded fetch keeps running; let it fail with no waiters.
        released.set()
        await asyncio.sleep(0.05)
        gc.collect()  # task destruction is where the warning would fire
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(original_handler)

    assert len(ac._in_flight) == 0  # marker cleaned up
    assert not any(
        "Task exception was never retrieved" in str(c.get("message", ""))
        for c in recorded
    ), f"orphaned exception warning emitted: {recorded}"


# ---------------------------------------------------------------------------
# Retry tests (httpx.MockTransport)
# ---------------------------------------------------------------------------


async def test_success_on_first_try_no_retry() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={"ok": True})

    ac = _client_with(handler)

    resp = await ac._request("GET", f"{BASE_URL}/x")
    assert resp.status_code == 200
    assert call_count == 1


async def test_429_with_retry_after_succeeds_on_second_attempt() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(429, headers={"Retry-After": "0.01"})
        return httpx.Response(200, json={"ok": True})

    ac = _client_with(handler)

    resp = await ac._request("GET", f"{BASE_URL}/x")
    assert resp.status_code == 200
    assert call_count == 2


async def test_429_without_retry_after_uses_exponential_backoff() -> None:
    call_count = 0
    timestamps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        timestamps.append(time.monotonic())
        if call_count <= 2:
            return httpx.Response(429)  # no Retry-After
        return httpx.Response(200, json={"ok": True})

    ac = _client_with(handler)

    resp = await ac._request("GET", f"{BASE_URL}/x")
    assert resp.status_code == 200
    assert call_count == 3
    # First backoff ~0.01 (0.01*2**0), second ~0.02 (0.01*2**1).
    gap1 = timestamps[1] - timestamps[0]
    gap2 = timestamps[2] - timestamps[1]
    assert gap1 >= 0.008
    assert gap2 >= 0.016
    assert gap2 > gap1  # exponential growth


async def test_429_every_attempt_raises_after_max_retries() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(429, headers={"Retry-After": "0.01"})

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    max_retries = 3
    ac = IfixitClient(
        client=http,
        min_request_interval=0,
        max_retries=max_retries,
        backoff_base=0.01,
    )

    with pytest.raises(httpx.HTTPStatusError):
        await ac._request("GET", f"{BASE_URL}/x")
    assert call_count == max_retries + 1


async def test_non_429_error_not_retried() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(500, json={"error": "boom"})

    ac = _client_with(handler)

    resp = await ac._request("GET", f"{BASE_URL}/x")
    assert resp.status_code == 500
    assert call_count == 1  # no retry on 500


# ---------------------------------------------------------------------------
# get() wrapper tests
# ---------------------------------------------------------------------------


async def test_get_returns_parsed_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"guideid": 1220, "title": "Test Guide"})

    ac = _client_with(handler)
    data = await ac.get("/guides/1220")
    assert data == {"guideid": 1220, "title": "Test Guide"}


async def test_get_builds_url_and_sends_params() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"ok": True})

    ac = _client_with(handler)
    await ac.get("/guides", params={"limit": 20, "offset": 0})
    assert seen["url"].startswith(f"{BASE_URL}/guides")
    assert "limit=20" in seen["url"]
    assert "offset=0" in seen["url"]


async def test_get_caches_by_path_and_params() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={"ok": True, "n": call_count})

    ac = _client_with(handler)
    first = await ac.get("/guides", params={"limit": 20})
    second = await ac.get("/guides", params={"limit": 20})
    assert first == second == {"ok": True, "n": 1}
    # Different params -> different cache entry -> network hit.
    third = await ac.get("/guides", params={"limit": 50})
    assert third == {"ok": True, "n": 2}
    assert call_count == 2


async def test_get_returns_deep_copy() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [1, 2], "nested": {"a": 1}})

    ac = _client_with(handler)
    first = await ac.get("/guides/1220")
    first["items"].append(999)
    first["nested"]["a"] = 42

    second = await ac.get("/guides/1220")  # served from cache
    assert second == {"items": [1, 2], "nested": {"a": 1}}


async def test_get_ttl_zero_bypasses_cache() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={"ok": True})

    ac = _client_with(handler)
    await ac.get("/guides/1220", cache_ttl=0)
    await ac.get("/guides/1220", cache_ttl=0)
    assert call_count == 2


async def test_get_rejects_path_without_leading_slash() -> None:
    ac = _client_with(lambda r: httpx.Response(200, json={}))
    with pytest.raises(ValueError, match="must start with '/'"):
        await ac.get("guides/1220")
    with pytest.raises(ValueError, match="must start with '/'"):
        await ac.get("")


async def test_get_ttl_zero_does_not_write_cache() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={"ok": True, "n": call_count})

    ac = _client_with(handler)
    await ac.get("/guides/1220", cache_ttl=0)
    # The no-cache call must not have polluted the cache.
    assert len(ac._cache) == 0

    # A default-TTL call must therefore hit the network again.
    data = await ac.get("/guides/1220")
    assert data == {"ok": True, "n": 2}
    assert call_count == 2


async def test_ttl_zero_call_does_not_evict_or_consume_cache_slots() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    ac = _client_with(handler)
    ac._set_cached("seed", {"v": 1})
    await ac.get("/other/path", cache_ttl=0)
    # Only the pre-existing entry may remain.
    assert len(ac._cache) == 1
    assert ac._get_cached("seed", ttl=60) == {"v": 1}


@pytest.mark.parametrize(
    "bad_body",
    [b'"just a string"', b"42", b"3.14", b"true", b"null"],
)
async def test_get_rejects_non_dict_non_list_json(bad_body) -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, content=bad_body)

    ac = _client_with(handler)
    with pytest.raises(ValueError, match="Unexpected API response format"):
        await ac.get("/weird")
    # The bad payload must never be cached.
    assert len(ac._cache) == 0
    with pytest.raises(ValueError, match="Unexpected API response format"):
        await ac.get("/weird")
    assert call_count == 2  # both calls hit the network


async def test_get_accepts_top_level_list_json() -> None:
    # /users/{id}/guides and /teams return top-level arrays.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"guideid": 1}, {"guideid": 2}])

    ac = _client_with(handler)
    data = await ac.get("/user/1/guides")
    assert data == [{"guideid": 1}, {"guideid": 2}]
    assert len(ac._cache) == 1


def _deep_json_body(depth: int = 1500) -> bytes:
    """A JSON body nested *depth* levels deep (valid, pathologically nested)."""
    return b'{"a":' * depth + b"1" + b"}" * depth


async def test_get_deep_nested_json_raises_clean_value_error_uncached() -> None:
    # QA finding: a 2000-deep payload made resp.json() raise RecursionError,
    # which escaped the no-stack-trace contract. It must surface as a clean
    # ValueError, and nothing may enter the cache.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_deep_json_body())

    ac = _client_with(handler)
    with pytest.raises(ValueError, match="Unexpected API response format"):
        await ac.get("/guides/1220", cache_ttl=0)
    assert len(ac._cache) == 0
    assert len(ac._in_flight) == 0  # marker cleared


async def test_get_200_with_non_json_body_raises_clean_value_error() -> None:
    # QA Round 12 (F12-6): a 200 with a non-JSON body (e.g. an HTML error
    # page or an empty body) used to surface the raw JSONDecodeError as
    # "Guide lookup failed: Expecting value: line 1 column 1 (char 0)".
    # resp.json() failures must map to the clean "Unexpected API response
    # format" ValueError, and nothing may enter the cache.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"<html><body>gateway error</body></html>"
        )

    ac = _client_with(handler)
    with pytest.raises(ValueError, match="Unexpected API response format"):
        await ac.get("/guides/1220")
    assert len(ac._cache) == 0


async def test_get_deep_nested_json_raises_clean_value_error_cached() -> None:
    # Same payload with default TTL: the parse failure happens before any
    # cache write, so the error is clean there too.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_deep_json_body())

    ac = _client_with(handler)
    with pytest.raises(ValueError, match="Unexpected API response format"):
        await ac.get("/guides/1220")
    assert len(ac._cache) == 0


async def test_get_deep_cached_entry_raises_clean_value_error() -> None:
    # If pathologically deep data somehow reaches the cache, the deep copy
    # on read must raise a clean ValueError, not a RecursionError.
    ac = IfixitClient(min_request_interval=0)
    deep: dict = {}
    node = deep
    for _ in range(1500):
        node["a"] = {}
        node = node["a"]
    ac._cache["get:/deep"] = (time.monotonic(), deep)

    with pytest.raises(ValueError, match="Unexpected API response format"):
        ac._get_cached("get:/deep", ttl=60)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    ac2 = _client_with(handler)
    ac2._cache["get:/deep:[]"] = (time.monotonic(), deep)
    with pytest.raises(ValueError, match="Unexpected API response format"):
        await ac2.get("/deep")


async def test_get_guide_full_deep_payload_raises_clean_value_error() -> None:
    # The full-guide path (deepcopy + recursive strip) must also stay inside
    # the no-RecursionError contract.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_deep_json_body())

    ac = _client_with(handler)
    with pytest.raises(ValueError, match="Unexpected API response format"):
        await ac.get_guide(1220, detail="full")


async def test_get_stampede_waiters_deepcopy_raises_clean_value_error() -> None:
    # QA finding: with cache_ttl=0 the creator returns the parsed payload
    # directly (no write-side copy), but every stampede waiter deep-copies
    # the shared task result in get() — an unguarded copy that raised a bare
    # RecursionError for payloads deep enough to overflow deepcopy but not
    # the JSON parser (depth ~500-900 on this interpreter; 600 is used
    # here). Waiters must get the same clean ValueError as the other
    # guarded copy paths, and no caller may ever see a RecursionError.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_deep_json_body(depth=600))

    ac = _client_with(handler)
    results = await asyncio.gather(
        *[ac.get("/suggest/x", cache_ttl=0) for _ in range(3)],
        return_exceptions=True,
    )

    assert not any(isinstance(r, RecursionError) for r in results)
    # The creator never copies (cache_ttl=0) and succeeds; the two waiters
    # fail on the shared result's deep copy — with the clean ValueError.
    errors = [r for r in results if isinstance(r, Exception)]
    assert len(errors) == 2
    assert all(
        isinstance(r, ValueError) and str(r) == "Unexpected API response format"
        for r in errors
    )
    ok = [r for r in results if not isinstance(r, Exception)]
    assert len(ok) == 1
    assert isinstance(ok[0], dict)
    assert len(ac._in_flight) == 0  # marker cleared


@pytest.mark.parametrize("bad_value", ["nan", "-1", "inf"])
async def test_429_with_invalid_retry_after_falls_back_to_backoff(
    bad_value, monkeypatch
) -> None:
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(429, headers={"Retry-After": bad_value})
        return httpx.Response(200, json={"ok": True})

    ac = _client_with(handler)  # backoff_base=0.01
    resp = await ac._request("GET", f"{BASE_URL}/x")
    assert resp.status_code == 200
    assert call_count == 2
    # A non-finite/negative Retry-After must be rejected and replaced by
    # the exponential backoff for the first attempt (0.01 * 2**0).
    assert delays == [0.01]


@pytest.mark.parametrize(
    ("retry_after", "expected_delay"),
    [
        ("2", 2.0),    # below the cap: honored as-is
        ("0.5", 0.5),  # sub-second: honored as-is
        ("3600", 8.0),  # F15-4: a 1-hour stall used to freeze every request
        ("9e9", 8.0),   # F15-4: ~285 years, capped to the same 8s
    ],
)
async def test_429_retry_after_honored_but_capped(
    retry_after, expected_delay, monkeypatch
) -> None:
    # F15-4: a large finite Retry-After used to be honored unbounded
    # ('3600' -> 1-hour stall, '9e9' -> ~285 years), freezing every
    # request for that key while the exponential backoff path is capped
    # at MAX_RETRY_AFTER. The header path now caps identically.
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(429, headers={"Retry-After": retry_after})
        return httpx.Response(200, json={"ok": True})

    ac = _client_with(handler)
    resp = await ac._request("GET", f"{BASE_URL}/x")
    assert resp.status_code == 200
    assert call_count == 2
    assert delays == [expected_delay]


# ---------------------------------------------------------------------------
# Error mapping tests
# ---------------------------------------------------------------------------


async def test_get_400_raises_value_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": True, "message": "bad params"})

    ac = _client_with(handler)
    with pytest.raises(ValueError, match="bad request"):
        await ac.get("/guides/0")


async def test_get_401_raises_auth_required() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": True, "message": "Invalid login"})

    ac = _client_with(handler)
    with pytest.raises(ValueError, match="auth required"):
        await ac.get("/user")


async def test_get_403_raises_forbidden() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": True, "message": "forbidden"})

    ac = _client_with(handler)
    with pytest.raises(ValueError, match="forbidden"):
        await ac.get("/media/images/1")


async def test_get_403_raises_forbidden_error_sentinel() -> None:
    # QA finding: get_media detected 403 by string-comparing the message
    # ("forbidden"). A dedicated sentinel (mirroring NotFoundError) makes
    # the mapping explicit and robust.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": True, "message": "forbidden"})

    ac = _client_with(handler)
    with pytest.raises(ForbiddenError, match="forbidden"):
        await ac.get("/media/images/1")


def test_forbidden_error_is_value_error_subclass() -> None:
    assert issubclass(ForbiddenError, ValueError)


async def test_get_404_raises_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Endpoint not found"})

    ac = _client_with(handler)
    with pytest.raises(ValueError, match="not found"):
        await ac.get("/guides/999999")


def test_not_found_error_is_value_error_subclass() -> None:
    # NotFoundError subclasses ValueError so existing except ValueError
    # handlers (including every server tool) keep working unchanged.
    assert issubclass(NotFoundError, ValueError)


async def test_get_404_raises_not_found_error_sentinel() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Endpoint not found"})

    ac = _client_with(handler)
    with pytest.raises(NotFoundError, match="not found"):
        await ac.get("/guides/999999")


async def test_get_500_raises_http_status_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": True, "message": "boom"})

    ac = _client_with(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await ac.get("/guides/1220")


# ---------------------------------------------------------------------------
# Context manager tests
# ---------------------------------------------------------------------------


async def test_aenter_returns_self() -> None:
    ac = _ok_client()
    async with ac as entered:
        assert entered is ac


async def test_aexit_closes_owned_client() -> None:
    ac = IfixitClient(min_request_interval=0)  # owns its client
    assert ac._owns_client is True
    inner = ac._client
    async with ac:
        pass
    assert inner.is_closed


async def test_external_client_not_closed_on_aexit() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"ok": True})
    )
    external = httpx.AsyncClient(transport=transport)
    ac = IfixitClient(client=external, min_request_interval=0)
    assert ac._owns_client is False
    async with ac:
        pass
    assert not external.is_closed
    await external.aclose()  # cleanup


# ---------------------------------------------------------------------------
# Guide fixtures (shapes from RESEARCH.md §3a / §3b)
# ---------------------------------------------------------------------------


def _full_guide_payload() -> dict:
    """A realistic full-guide response (verified shape, RESEARCH.md §3a).

    Mirrors live guide 1220: step ``title`` fields are EMPTY strings and the
    real text lives in the step lines' ``text_rendered``. Use
    ``_titled_guide_payload()`` when a test needs explicit step titles.
    """
    return {
        "guideid": 1220,
        "title": "iPhone 5c Battery Replacement",
        "summary": "Use this guide to replace the battery.",
        "type": "replacement",
        "category": "iPhone",
        "subject": "Battery",
        "difficulty": "Moderate",
        "time_required": "30 minutes",
        "time_required_min": 1800,
        "time_required_max": 2400,
        "public": True,
        "locale": "en",
        "langid": "en",
        "url": "https://www.ifixit.com/Guide/iPhone+5c+Battery+Replacement/1220",
        "created_date": 1357197076,
        "modified_date": 1477099265,
        "revisionid": 654321,
        "patrol_threshold": 25,
        "can_edit": True,
        "available_langids": ["en", "de"],
        "flags": ["GUIDE_USER_CONTRIBUTED"],
        "comments": [{"commentid": 1, "body": "nice guide"}],
        "author": {
            "userid": 1,
            "username": "kaykay",
            "unique_username": "kaykay",
            "join_date": 1234567890,
            "image": None,
            "reputation": 100,
            "url": "https://www.ifixit.com/User/1/kaykay",
            "teams": [],
        },
        "image": {
            "id": 14056,
            "guid": "s.ABC",
            "original": "https://guide-images.cdn.ifixit.com/igi/s.ABC.original",
            "large": "https://guide-images.cdn.ifixit.com/igi/s.ABC.large",
        },
        "introduction_raw": "== Introduction ==\nThis is the intro.",
        "introduction_rendered": "<h2>Introduction</h2><p>This is the intro.</p>",
        "conclusion_raw": "== Conclusion ==\nDone!",
        "conclusion_rendered": "<h2>Conclusion</h2><p>Done!</p>",
        "steps": [
            {
                "stepid": 12345,
                "guideid": 1220,
                "orderby": 1,
                "revisionid": 111,
                "title": "",
                "lines": [
                    {
                        "lineid": 1,
                        "text_raw": "**Hold** the power button.",
                        "text_rendered": "<p><strong>Hold</strong> the power button.</p>",
                        "bullet": "black",
                        "level": 0,
                    }
                ],
                "media": {
                    "type": "image",
                    "data": [{"id": 1, "url": "https://guide-images.cdn.ifixit.com/igi/x.original"}],
                },
                "comments": [{"commentid": 9, "body": "worked!"}],
                "comment_count": 1,
            },
            {
                "stepid": 12346,
                "guideid": 1220,
                "orderby": 2,
                "revisionid": 222,
                "title": "",
                "lines": [
                    {
                        "lineid": 2,
                        "text_raw": "Use a **Phillips** screwdriver.",
                        "text_rendered": "<p>Use a <em>Phillips</em> screwdriver.</p>",
                        "bullet": "black",
                        "level": 0,
                    }
                ],
                "media": {"type": "image", "data": []},
                "comments": [],
                "comment_count": 0,
            },
            {
                "stepid": 12347,
                "guideid": 1220,
                "orderby": 3,
                "revisionid": 333,
                "title": "",
                "lines": [
                    {
                        "lineid": 3,
                        "text_raw": "Lift it.",
                        "text_rendered": "<p>Lift it.</p>",
                        "bullet": "icon_note",
                        "level": 0,
                    }
                ],
                "media": None,
                "comments": [],
                "comment_count": 0,
            },
        ],
        "parts": [
            {
                "type": "parts",
                "quantity": 1,
                "text": "iPhone 5c Battery",
                "notes": "A new battery.",
                "url": "https://www.ifixit.com/Store/Parts/iphone-5c-battery",
                "thumbnail": "https://cart-products.cdn.ifixit.com/thumb.png",
                "isoptional": False,
                "featured": False,
            }
        ],
        "tools": [
            {
                "type": "tools",
                "quantity": 1,
                "text": "Phillips #00 Screwdriver",
                "url": "https://www.ifixit.com/Store/Tools/phillips-00-screwdriver",
                "thumbnail": "https://cart-products.cdn.ifixit.com/screw.png",
                "isoptional": False,
                "featured": True,
            }
        ],
        "prerequisites": [],
        "documents": [],
        "featured_documentid": None,
        "featured_document_embed_url": None,
        "featured_document_thumbnail_url": None,
        "intro_video": None,
        "intro_video_url": None,
        "favorited": False,
        "completed": False,
        "prereq_modified_date": None,
        "published_date": 1357197076,
    }


def _titled_guide_payload() -> dict:
    """A guide payload with explicit (non-empty) step titles.

    Some guides do carry real step titles (e.g. guide 41080); this variant
    keeps the same shape as ``_full_guide_payload()`` with titles filled in.
    """
    payload = _full_guide_payload()
    for step, title in zip(
        payload["steps"],
        ["Power off the phone", "Remove the screws", "Lift the battery"],
    ):
        step["title"] = title
    return payload


def _guide_summary(guideid: int, title: str = "Test Guide") -> dict:
    """A realistic guide-summary object (verified shape, RESEARCH.md §3b)."""
    return {
        "dataType": "guide",
        "guideid": guideid,
        "locale": "en",
        "revisionid": 1000 + guideid,
        "modified_date": 1477099265,
        "prereq_modified_date": None,
        "url": f"https://www.ifixit.com/Guide/Test+Guide/{guideid}",
        "type": "replacement",
        "category": "iPhone",
        "subject": "Battery",
        "title": title,
        "summary": "A summary.",
        "difficulty": "Moderate",
        "time_required_max": 2400,
        "public": True,
        "userid": 1,
        "username": "kaykay",
        "flags": [],
        "image": {"id": 14056, "guid": "s.ABC", "original": "https://guide-images.cdn.ifixit.com/igi/s.ABC.original"},
    }


# ---------------------------------------------------------------------------
# _html_to_text() unit tests
# ---------------------------------------------------------------------------


def test_html_to_text_unescapes_entities() -> None:
    assert _html_to_text("Tom &amp; Jerry") == "Tom & Jerry"
    assert _html_to_text("it&#39;s") == "it's"
    assert _html_to_text("a&nbsp;b") == "a b"  # nbsp collapses to a space
    assert _html_to_text("&lt;tag&gt;") == "<tag>"


def test_html_to_text_plain_text_passthrough() -> None:
    assert _html_to_text("Hello world") == "Hello world"
    assert _html_to_text("  multiple   spaces  ") == "multiple spaces"


def test_html_to_text_lone_lt_without_gt_preserved() -> None:
    # A "<" with no matching ">" is not a tag and must survive stripping.
    assert _html_to_text("a < b") == "a < b"
    assert _html_to_text("5 < 10 and 10 > 5") == "5 < 10 and 10 > 5"


# QA Round 6 (F2): HTML comments, doctype/CDATA declarations, and
# script/style BODIES must never leak into plain text. The generic tag
# regex only matches "<" followed by a letter or "/", so "<!--" and
# "<![CDATA[" passed through it untouched (live: user 3's about_text
# began with a Font Awesome license comment), and script/style bodies
# would lose their tags but keep their JavaScript/CSS content.


def test_html_to_text_strips_html_comments() -> None:
    text = _html_to_text("before<!--! Font Awesome Pro 6.5.1 license -->after")
    assert text == "beforeafter"
    assert "<!--" not in text


def test_html_to_text_strips_multiline_comments() -> None:
    text = _html_to_text("a<!-- multi\nline\ncomment -->b")
    assert text == "ab"


def test_html_to_text_strips_doctype_and_cdata() -> None:
    text = _html_to_text(
        "<!DOCTYPE html><p>Hi</p><![CDATA[<raw> & stuff]]><p>there</p>"
    )
    assert text == "Hi\nthere"  # </p> is a block tag -> newline
    assert "<!DOCTYPE" not in text and "<![CDATA[" not in text


def test_html_to_text_strips_script_body() -> None:
    text = _html_to_text(
        "<p>intro</p><script type=\"text/javascript\">"
        "var x = 5 < 3 ? '<b>a</b>' : 'b';\n</script><p>outro</p>"
    )
    assert text == "intro\noutro"  # </p> is a block tag -> newline
    assert "var x" not in text


def test_html_to_text_strips_style_body() -> None:
    text = _html_to_text(
        "<style>p { color: red; } /* <b>not html</b> */</style><p>body</p>"
    )
    assert text == "body"
    assert "color" not in text


def test_html_to_text_strips_script_style_case_insensitively() -> None:
    text = _html_to_text(
        "<SCRIPT>alert('<b>hi</b>')</SCRIPT><STYLE>a{}</STYLE><p>ok</p>"
    )
    assert text == "ok"


# QA Round 7 (F8): malformed / truncated HTML must not leak verbatim.
# Unclosed comments, unclosed script/style bodies, quoted attribute values
# containing ">", and "</ p>" (space inside a close tag) used to survive
# the sanitizer as fragments.


def test_html_to_text_unclosed_comment_stripped_to_end() -> None:
    text = _html_to_text("before <!-- Font Awesome license, never closed")
    assert text == "before"
    assert "<!--" not in text


def test_html_to_text_unclosed_script_body_stripped() -> None:
    text = _html_to_text(
        "intro <script>var x = 1; if (x < 2) { alert('<b>hi</b>'); }"
    )
    assert text == "intro"
    assert "var x" not in text


def test_html_to_text_unclosed_style_body_stripped() -> None:
    text = _html_to_text("intro <style>p { color: red; }")
    assert text == "intro"
    assert "color" not in text


def test_html_to_text_quoted_attribute_gt_does_not_fragment() -> None:
    # The ">" inside the quoted title value must not terminate the tag,
    # leaving a 'b">' fragment behind.
    text = _html_to_text('<div title="a > b">text</div>')
    assert text == "text"
    assert 'b">' not in text


def test_html_to_text_single_quoted_attribute_gt_does_not_fragment() -> None:
    text = _html_to_text("<p title='a > b'>ok</p>")
    assert text == "ok"


def test_html_to_text_close_tag_with_space_stripped() -> None:
    # "</ p>" is a block-level close tag with stray whitespace: newline.
    assert _html_to_text("a</ p>b") == "a\nb"
    # Non-block close tag with stray whitespace: stripped flat.
    assert _html_to_text("x</ span>y") == "xy"


# QA Round 11 (F11-1, HIGH): ReDoS in the generic tag regex. The loop
# class [^"'>] ALSO consumed "<", so repeated unclosed tag openings
# ("<a"*n) forced O(n) backtracking per "<" start position -> O(n²):
# 10KB took ~2.5s, 40KB ~47s, 100KB >120s — freezing the
# single-threaded asyncio loop for EVERY client. "<" must be excluded
# from the loop class ([^"'<>]) so each match attempt is bounded by the
# next unquoted "<" and the sanitizer is linear; the quoted-attribute
# branches ("[^"]*" / '[^']*') must still permit "<" inside quotes.


def test_html_to_text_quoted_attribute_lt_does_not_fragment() -> None:
    # A "<" inside a quoted attribute value must not stop the tag strip:
    # the quoted branches still allow "<" after the F11-1 loop-class fix.
    assert _html_to_text('<div title="a < b">text</div>') == "text"
    assert _html_to_text("<p title='x < y'>ok</p>") == "ok"
    assert _html_to_text('<a href="x<y">z</a>') == "z"


# QA Round 13 (R13-1, MEDIUM): the F12-1 fix changed the
# script/style/doctype opening-tag classes to [^<>]*, which ALSO
# excludes "<" inside quoted attribute values — so an opening tag like
# <script type="text/javascript" data-x="a<b"> no longer matched the
# body-stripping regex: the generic tag regex stripped the tags but left
# the JavaScript/CSS body behind (pre-fix: 'var x = 1;' leaked), and a
# quoted "<" in a doctype made the whole declaration survive verbatim.
# The opening-tag classes must mirror the generic regex's quoted
# branches ("[^"]*" / '[^']*'), which permit "<" inside quotes while
# staying linear (each branch is bounded by the next quote).


def test_html_to_text_quoted_lt_in_script_style_doctype_open_tags() -> None:
    # A "<" inside a quoted attribute of a script/style opening tag must
    # not defeat the body strip.
    text = _html_to_text(
        '<script type="text/javascript" data-x="a<b">var x = 1;</script>'
    )
    assert text == ""
    assert "var x" not in text

    text = _html_to_text("<style data-x='a<b'>p { color: red; }</style>")
    assert text == ""
    assert "color" not in text

    text = _html_to_text('<script src="https://x.test/a<b">var x = 1;</script>')
    assert text == ""
    assert "var x" not in text

    # A doctype declaration with a quoted "<" is stripped verbatim.
    text = _html_to_text('<!doctype html public "a<b">')
    assert text == ""
    assert "doctype" not in text.lower()


def test_html_to_text_quoted_lt_in_special_open_tags_strips_unclosed_body() -> None:
    # Same quoted-< handling with NO closing tag: the body strips through
    # end-of-string, exactly like the unquoted F12-1 case.
    text = _html_to_text(
        'intro <script type="text/javascript" data-x="a<b">var x = 1;'
    )
    assert text == "intro"
    assert "var x" not in text

    text = _html_to_text('intro <style data-x="a<b">p { color: red; }')
    assert text == "intro"
    assert "color" not in text


def test_html_to_text_repeated_quoted_lt_special_tags_are_linear() -> None:
    # R13-1: the quoted branches ("[^"]*" / '[^']*') are bounded by the
    # next quote, so '<script data-x="a<b" '*N stays linear like the
    # unquoted F12-1 case. No ">" anywhere, so nothing is stripped and
    # the payload survives unchanged.
    payload = '<script data-x="a<b" ' * 20000  # 380KB
    start = time.monotonic()
    result = _html_to_text(payload)
    elapsed = time.monotonic() - start
    assert elapsed < 3.0, (
        f"linearity regression: quoted-< special tags ({len(payload) // 1024}KB) "
        f"took {elapsed:.2f}s"
    )
    assert result == payload.rstrip()


# QA Round 14 (F14-1, HIGH): the R13-1 quoted-branch rewrite introduced a
# NEW exponential ReDoS. The opening-tag class (?:[^<>]|"[^"]*"|'[^']*')*
# is AMBIGUOUS: [^<>] also matches " and ', so every quote can be
# consumed singly (branch 1) OR as a pair (branch 2/3) — with no closing
# ">" the engine explores exponentially many partitions ('<script'+'"'*40
# took >30s, freezing the server for every client; *28 ~0.24s, *32 ~1.6s).
# Excluding quotes from the plain branch ([^<>"']) gives every character
# position EXACTLY ONE matching branch (non-quote/non-angle -> branch 1;
# " -> branch 2 through the next "; ' -> branch 3): linear.


_QUOTE_RUN_TAGS = ["<script", "<style", "<!doctype"]


@pytest.mark.parametrize("tag", _QUOTE_RUN_TAGS)
@pytest.mark.parametrize("quote", ['"', "'"])
@pytest.mark.parametrize("n", [20, 40, 80])
def test_html_to_text_quote_runs_are_linear(tag: str, quote: str, n: int) -> None:
    # F14-1: a quote run with no closing ">" used to force exponential
    # partition exploration (N=40 was >30s pre-fix). No ">" anywhere, so
    # nothing is stripped and the payload survives unchanged. Generous
    # <1s bound (pre-fix even N=28 took ~0.24s and growth was 4-6x per
    # +4 quotes).
    payload = tag + quote * n
    start = time.monotonic()
    result = _html_to_text(payload)
    elapsed = time.monotonic() - start
    assert elapsed < 1.0, (
        f"quote-run regression: {tag} + {quote!r}*{n} took {elapsed:.2f}s"
    )
    assert result == payload


@pytest.mark.parametrize("tag", _QUOTE_RUN_TAGS)
@pytest.mark.parametrize("n", [20, 40, 80])
def test_html_to_text_mixed_quote_runs_are_linear(tag: str, n: int) -> None:
    # Alternating single/double quotes: each position still has exactly
    # one matching branch, so the run must stay linear.
    payload = tag + "'\"'" * n
    start = time.monotonic()
    result = _html_to_text(payload)
    elapsed = time.monotonic() - start
    assert elapsed < 1.0, (
        f"mixed quote-run regression: {tag} + mixed*{n} took {elapsed:.2f}s"
    )
    assert result == payload


def test_html_to_text_pre_fix_exponential_quote_run_is_fast() -> None:
    # The exact empirical F14-1 case: '<script'+'"'*40 took >30s before
    # the fix. Must complete in well under a second now.
    payload = "<script" + '"' * 40
    start = time.monotonic()
    result = _html_to_text(payload)
    elapsed = time.monotonic() - start
    assert elapsed < 1.0, (
        f"F14-1 regression: '<script'+'\"'*40 took {elapsed:.2f}s"
    )
    assert result == payload


@pytest.mark.parametrize("tag", _QUOTE_RUN_TAGS)
@pytest.mark.parametrize("n", [40, 41])
def test_html_to_text_quote_runs_with_closing_gt_are_fast(tag: str, n: int) -> None:
    # Same quote runs terminated by ">": the opening tag consumes the
    # quoted run as attribute values and is stripped (even and odd quote
    # counts). Must stay linear and strip cleanly.
    payload = tag + '"' * n + ">"
    start = time.monotonic()
    result = _html_to_text(payload)
    elapsed = time.monotonic() - start
    assert elapsed < 1.0, (
        f"quote-run+'>' regression: {tag} + '\"'*{n} + '>' took {elapsed:.2f}s"
    )
    assert result == ""


def test_html_to_text_script_body_stripped_with_plain_open_tag() -> None:
    # F14-1 correctness: a plain script opening tag still strips the body.
    text = _html_to_text('<script type="text/javascript">evil();</script>')
    assert text == ""


def test_html_to_text_script_body_stripped_with_quoted_lt() -> None:
    # F14-1 correctness: a quoted "<" inside the opening tag still cannot
    # defeat the body strip (R13-1 behavior preserved).
    text = _html_to_text('<script data-x="a<b">var x=1;</script>')
    assert text == ""
    assert "var x" not in text


def test_html_to_text_attributed_closing_tag_terminates_script_body() -> None:
    # F15-3: `</script data-x="a>b">` must terminate the body strip like
    # a plain `</script>`. The old closing class `</script\s*>` didn't
    # match attributed closing tags, so `.*?(?:</script\s*>|$)` ran to
    # end-of-string and deleted content AFTER the malformed close (KEEP
    # was lost). Attributed closes are still closes.
    text = _html_to_text('<script>evil</script data-x="a>b">KEEP')
    assert text == "KEEP"
    assert "evil" not in text


def test_html_to_text_attributed_closing_tag_terminates_style_body() -> None:
    # F15-3: same fix for <style> bodies.
    text = _html_to_text('<style>p { color: red; }</style foo="x">KEEP')
    assert text == "KEEP"
    assert "color" not in text


def test_html_to_text_plain_closing_tag_still_terminates_body() -> None:
    # F15-3: the plain `</script>` close keeps working.
    text = _html_to_text('<script>evil</script>KEEP')
    assert text == "KEEP"
    assert "evil" not in text


def test_html_to_text_unclosed_body_fallback_intact() -> None:
    # F15-3: the |$ fallback for a truly unclosed body is unchanged.
    assert _html_to_text("<script>evil") == ""
    assert _html_to_text("<style>p { color: red; }") == ""


@pytest.mark.parametrize("n", [20000, 100000])
def test_html_to_text_repeated_tag_openings_are_linear(n: int) -> None:
    # F11-1 regression: "<a"*20000 (40KB) used to take ~47s (O(n²)
    # backtracking), freezing the server. Must now complete in well under
    # 2s. Unclosed tag openings are not tags (no ">") and must survive as
    # literal text, unchanged.
    payload = "<a" * n
    start = time.monotonic()
    result = _html_to_text(payload)
    elapsed = time.monotonic() - start
    assert elapsed < 2.0, (
        f"linearity regression: {n} unclosed tag openings took {elapsed:.2f}s"
    )
    assert result == payload


@pytest.mark.parametrize("tag", ["<script ", "<style ", "<!doctype "])
@pytest.mark.parametrize("n", [10000, 40000])
def test_html_to_text_repeated_unclosed_special_tags_are_linear(
    tag: str, n: int
) -> None:
    # QA Round 12 (F12-1, HIGH): the script/style/doctype opening-tag
    # classes used [^>]*, which scans to end-of-string when no ">" follows
    # — re.sub retries at every "<" start -> O(n²). "<script "*40000
    # (320KB) took ~12.8s (and "<style "/"<!doctype " similarly). With "<"
    # excluded from those classes too ([^<>]*), each attempt is bounded by
    # the next "<" and the sanitizer is linear. No ">" anywhere in the
    # payload, so nothing is stripped and it survives unchanged.
    payload = tag * n
    start = time.monotonic()
    result = _html_to_text(payload)
    elapsed = time.monotonic() - start
    assert elapsed < 3.0, (
        f"linearity regression: {tag!r}*{n} ({len(payload) // 1024}KB) "
        f"took {elapsed:.2f}s"
    )
    # No ">" anywhere, so nothing is stripped; the sanitizer's final
    # .strip() only removes the payload's trailing space.
    assert result == payload.rstrip()


def test_html_to_text_script_payload_scaling_is_roughly_linear() -> None:
    # F12-1: 4x the input must not cost ~16x. Generous bound (8x + 0.5s)
    # keeps the assertion noise-proof while still failing a quadratic
    # sanitizer (pre-fix: 0.70s -> 10.79s, ratio ~15x).
    small = "<script " * 10000
    large = "<script " * 40000
    start = time.monotonic()
    _html_to_text(small)
    small_elapsed = time.monotonic() - start
    start = time.monotonic()
    _html_to_text(large)
    large_elapsed = time.monotonic() - start
    assert large_elapsed < small_elapsed * 8 + 0.5, (
        f"superlinear scaling: {small_elapsed:.2f}s -> {large_elapsed:.2f}s "
        "for 4x input"
    )


def test_html_to_text_oversized_input_raises_clean_value_error() -> None:
    # F11-1 defense-in-depth: inputs over the 10MB cap are rejected with
    # a clean ValueError before any processing — bounds worst-case
    # per-call CPU even if a future edit reintroduces superlinearity.
    with pytest.raises(ValueError, match="Input too large for HTML conversion"):
        _html_to_text("x" * (_MAX_HTML_TO_TEXT_INPUT + 1))


def test_html_to_text_size_cap_boundary_is_exact(monkeypatch) -> None:
    # QA Round 12 (F12-5): the old "at the cap" assertion used a 12-byte
    # input, never touching the real boundary. Monkeypatch the cap to a
    # small value so the boundary logic is exercised without the 10MB
    # cost: exactly MAX characters processes normally; MAX+1 raises the
    # clean ValueError. The message reports CHARACTERS, not bytes — the
    # len() check counts code points, and an honest message must not
    # overstate the byte accounting (F12-3).
    monkeypatch.setattr("ifixit_mcp.client._MAX_HTML_TO_TEXT_INPUT", 1000)
    assert _html_to_text("x" * 1000) == "x" * 1000
    with pytest.raises(
        ValueError,
        match=r"Input too large for HTML conversion \(1001 > 1000 characters\)",
    ):
        _html_to_text("x" * 1001)


def test_rendered_to_text_key_mapping() -> None:
    assert _rendered_to_text_key("introduction_rendered") == "introduction_text"
    assert _rendered_to_text_key("conclusion_rendered") == "conclusion_text"
    # The step-line field becomes plain "text", not the awkward "text_text".
    assert _rendered_to_text_key("text_rendered") == "text"


# ---------------------------------------------------------------------------
# get_guide() tests
# ---------------------------------------------------------------------------


async def test_get_guide_summary_keeps_metadata_and_step_titles() -> None:
    ac = _client_with(lambda r: httpx.Response(200, json=_titled_guide_payload()))

    data = await ac.get_guide(1220)  # detail defaults to "summary"

    assert data["title"] == "iPhone 5c Battery Replacement"
    assert data["difficulty"] == "Moderate"
    assert data["type"] == "replacement"
    assert data["category"] == "iPhone"
    assert data["time_required"] == "30 minutes"
    assert len(data["parts"]) == 1
    assert len(data["tools"]) == 1
    # Step count + step titles only.
    assert len(data["steps"]) == 3
    assert [s["title"] for s in data["steps"]] == [
        "Power off the phone",
        "Remove the screws",
        "Lift the battery",
    ]
    assert data["steps"][0]["stepid"] == 12345
    # No step bodies in summary mode.
    assert "lines" not in data["steps"][0]


async def test_get_guide_summary_empty_step_titles_fall_back_to_line_text() -> None:
    # QA finding: live teardown guides (e.g. 1220) carry EMPTY step titles.
    # Summary mode must fall back to the first non-empty line's plain text.
    ac = _client_with(lambda r: httpx.Response(200, json=_full_guide_payload()))

    data = await ac.get_guide(1220)

    assert [s["title"] for s in data["steps"]] == [
        "Hold the power button.",
        "Use a Phillips screwdriver.",
        "Lift it.",
    ]
    assert data["steps"][0]["stepid"] == 12345


async def test_get_guide_summary_title_fallback_truncates_to_80_chars() -> None:
    payload = _full_guide_payload()
    long_text = "<p>" + "word " * 40 + "</p>"  # ~200 chars of text
    payload["steps"][0]["lines"] = [
        {"lineid": 1, "text_rendered": long_text, "bullet": None, "level": 0}
    ]
    ac = _client_with(lambda r: httpx.Response(200, json=payload))

    data = await ac.get_guide(1220)

    assert len(data["steps"][0]["title"]) == 80
    assert data["steps"][0]["title"] == ("word " * 40).strip()[:80]


async def test_get_guide_summary_title_fallback_without_lines_keeps_empty() -> None:
    # A step with an empty title AND no usable line text keeps title "" —
    # nothing is fabricated.
    payload = _full_guide_payload()
    payload["steps"].append({"stepid": 99999, "title": "", "lines": []})
    ac = _client_with(lambda r: httpx.Response(200, json=payload))

    data = await ac.get_guide(1220)

    assert data["steps"][-1] == {"stepid": 99999, "title": ""}


@pytest.mark.parametrize("steps", [7, 3.5, {"a": 1}, "notalist"])
async def test_get_guide_summary_non_list_steps_raise_clean_value_error(steps) -> None:
    # QA Round 3 (LOW): a truthy non-list "steps" value used to leak a bare
    # TypeError (int/float) or silently yield [] (dict/str iterate but every
    # item is filtered out). The client's clean-ValueError contract must hold.
    payload = _titled_guide_payload()
    payload["steps"] = steps
    ac = _client_with(lambda r: httpx.Response(200, json=payload))
    with pytest.raises(ValueError, match="Unexpected API response format"):
        await ac.get_guide(1220)


@pytest.mark.parametrize("steps", [{}, 0, "", False])
async def test_get_guide_summary_falsy_non_list_steps_raise_clean_value_error(
    steps,
) -> None:
    # QA Round 4 (LOW): "data.get('steps') or []" silently swallowed
    # FALSY malformed shapes ({}, 0, "", False) as an empty step list
    # while truthy malformed shapes raised. Every non-list shape must
    # raise the clean ValueError.
    payload = _titled_guide_payload()
    payload["steps"] = steps
    ac = _client_with(lambda r: httpx.Response(200, json=payload))
    with pytest.raises(ValueError, match="Unexpected API response format"):
        await ac.get_guide(1220)


@pytest.mark.parametrize("step", ["notadict", 5, ["nested"]])
async def test_get_guide_summary_non_dict_step_raises_clean_value_error(step) -> None:
    # QA Round 3 (LOW): non-dict step entries used to be silently filtered
    # out; malformed entries must raise instead of silently dropping data.
    payload = _titled_guide_payload()
    payload["steps"] = [{"stepid": 1, "title": "Good"}, step]
    ac = _client_with(lambda r: httpx.Response(200, json=payload))
    with pytest.raises(ValueError, match="Unexpected API response format"):
        await ac.get_guide(1220)


@pytest.mark.parametrize("title", [123, 3.5, ["x"], {"t": 1}])
async def test_get_guide_summary_non_string_step_title_raises_clean_value_error(
    title,
) -> None:
    # QA Round 3 (LOW): a non-string step title used to leak a bare
    # AttributeError from .strip().
    payload = _titled_guide_payload()
    payload["steps"] = [{"stepid": 1, "title": title}]
    ac = _client_with(lambda r: httpx.Response(200, json=payload))
    with pytest.raises(ValueError, match="Unexpected API response format"):
        await ac.get_guide(1220)


async def test_get_guide_summary_missing_or_null_step_title_still_ok() -> None:
    # Missing/None titles remain valid (they take the line-text fallback) —
    # only non-string, non-None titles are malformed.
    payload = _titled_guide_payload()
    payload["steps"] = [
        {"stepid": 1, "title": None, "lines": [{"text_rendered": "<p>hi</p>"}]},
        {"stepid": 2},
    ]
    ac = _client_with(lambda r: httpx.Response(200, json=payload))
    data = await ac.get_guide(1220)
    assert [s["title"] for s in data["steps"]] == ["hi", ""]


async def test_get_guide_summary_strips_raw_comment_flags_fields() -> None:
    ac = _client_with(lambda r: httpx.Response(200, json=_full_guide_payload()))

    data = await ac.get_guide(1220)

    assert "introduction_raw" not in data
    assert "conclusion_raw" not in data
    assert "revisionid" not in data
    assert "patrol_threshold" not in data
    assert "can_edit" not in data
    assert "comments" not in data
    assert "flags" not in data
    # Rendered metadata is converted to plain text and renamed *_text — no
    # raw HTML ever leaks out of summary mode.
    assert "introduction_rendered" not in data
    assert data["introduction_text"] == "Introduction\nThis is the intro."
    assert data["conclusion_text"] == "Conclusion\nDone!"


async def test_get_guide_summary_non_string_rendered_keys_omitted() -> None:
    # QA Round 13 (R13-6): a null (or any non-string) *_rendered value
    # has no text to convert — it must be OMITTED entirely, never passed
    # through still named *_rendered (a key that promises HTML), and
    # never renamed to *_text (there is no text).
    payload = _full_guide_payload()
    payload["introduction_rendered"] = None
    payload["conclusion_rendered"] = 123
    ac = _client_with(lambda r: httpx.Response(200, json=payload))

    data = await ac.get_guide(1220)

    assert "introduction_rendered" not in data
    assert "conclusion_rendered" not in data
    assert "introduction_text" not in data
    assert "conclusion_text" not in data


async def test_get_guide_summary_large_rendered_field_completes_quickly() -> None:
    # QA Round 11 (F11-1, HIGH): a 100KB rendered field of unclosed tag
    # openings used to take >120s in _html_to_text (O(n²) backtracking),
    # freezing the whole server for every client. End-to-end via
    # MockTransport, the sanitizer must stay linear: the wall-clock bound
    # is generous (<5s) so slow CI boxes don't flake, while a quadratic
    # regression would take minutes and fail loudly.
    payload = _full_guide_payload()
    payload["introduction_rendered"] = "<a" * 50000  # 100KB
    ac = _client_with(lambda r: httpx.Response(200, json=payload))

    start = time.monotonic()
    data = await ac.get_guide(1220)
    elapsed = time.monotonic() - start

    assert elapsed < 5.0, (
        f"get_guide with 100KB rendered field took {elapsed:.2f}s — "
        "linearity regression"
    )
    # Unclosed tag openings are not tags: they survive as literal text.
    assert data["introduction_text"] == "<a" * 50000


async def test_get_guide_full_keeps_steps_with_text() -> None:
    ac = _client_with(lambda r: httpx.Response(200, json=_titled_guide_payload()))

    data = await ac.get_guide(1220, detail="full")

    assert len(data["steps"]) == 3
    step = data["steps"][0]
    assert step["title"] == "Power off the phone"
    assert step["lines"][0]["text"] == "Hold the power button."
    assert step["media"]["type"] == "image"
    # Full detail keeps fields that summary strips.
    assert data["revisionid"] == 654321
    assert data["can_edit"] is True
    assert data["comments"] == [{"commentid": 1, "body": "nice guide"}]
    assert data["flags"] == ["GUIDE_USER_CONTRIBUTED"]


async def test_get_guide_full_strips_raw_and_converts_html_to_text() -> None:
    ac = _client_with(lambda r: httpx.Response(200, json=_full_guide_payload()))

    data = await ac.get_guide(1220, detail="full")

    # *_raw fields are stripped everywhere (top level and step lines).
    assert "introduction_raw" not in data
    assert "conclusion_raw" not in data
    assert "text_raw" not in data["steps"][0]["lines"][0]
    assert "text_raw" not in data["steps"][1]["lines"][0]
    # *_rendered HTML is converted to plain text and renamed *_text (the
    # step-line field becomes plain "text") — honest key names.
    assert "introduction_rendered" not in data
    assert "conclusion_rendered" not in data
    assert data["introduction_text"] == "Introduction\nThis is the intro."
    assert data["conclusion_text"] == "Conclusion\nDone!"
    assert data["steps"][0]["lines"][0]["text"] == "Hold the power button."
    assert data["steps"][1]["lines"][0]["text"] == "Use a Phillips screwdriver."
    assert "text_rendered" not in data["steps"][0]["lines"][0]


async def test_get_guide_full_non_string_rendered_keys_omitted() -> None:
    # QA Round 13 (R13-6): non-string *_rendered values (null, numbers,
    # objects) in full mode are omitted entirely — including nested step
    # lines — never kept under the HTML-promising *_rendered name.
    payload = _full_guide_payload()
    payload["introduction_rendered"] = None
    payload["steps"][0]["lines"][0]["text_rendered"] = 123
    ac = _client_with(lambda r: httpx.Response(200, json=payload))

    data = await ac.get_guide(1220, detail="full")

    assert "introduction_rendered" not in data
    assert "introduction_text" not in data
    line = data["steps"][0]["lines"][0]
    assert "text_rendered" not in line
    assert "text" not in line


async def test_get_guide_full_max_steps_truncates() -> None:
    ac = _client_with(lambda r: httpx.Response(200, json=_full_guide_payload()))

    data = await ac.get_guide(1220, detail="full", max_steps=2)

    assert [s["stepid"] for s in data["steps"]] == [12345, 12346]

    # max_steps larger than the step count is harmless.
    data = await ac.get_guide(1220, detail="full", max_steps=99)
    assert len(data["steps"]) == 3


async def test_full_creator_does_not_corrupt_concurrent_summary_waiter() -> None:
    # QA Round 4 (MEDIUM): the creator of an in-flight get_guide(full)
    # mutates the shared task result in place; a concurrent summary waiter
    # deep-copies that SAME object when it resumes — after the *_rendered
    # keys were renamed to *_text — so its title fallback found no
    # text_rendered and every title came back empty. The full projection
    # must copy before mutating.
    call_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.05)  # keep the fetch in-flight
        return httpx.Response(200, json=_full_guide_payload())

    ac = _client_with(handler)  # cache_ttl defaults to TTL_30MIN
    full_task = asyncio.create_task(ac.get_guide(1220, detail="full"))
    await asyncio.sleep(0)  # creator starts the shared fetch
    summary_task = asyncio.create_task(ac.get_guide(1220, detail="summary"))
    full, summary = await asyncio.gather(full_task, summary_task)

    assert call_count == 1  # the summary call shared the in-flight fetch
    assert full["guideid"] == 1220
    # Empty step titles fall back to the first line's plain text — the
    # waiter must see the raw payload, not the creator's mutated copy.
    assert [s["title"] for s in summary["steps"]] == [
        "Hold the power button.",
        "Use a Phillips screwdriver.",
        "Lift it.",
    ]


async def test_full_max_steps_creator_does_not_truncate_waiter() -> None:
    # QA Round 4 (MEDIUM): a max_steps-truncating full creator used to
    # truncate the SHARED task result, so a concurrent unrestricted full
    # waiter silently received the truncated step list.
    call_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.05)
        return httpx.Response(200, json=_full_guide_payload())

    ac = _client_with(handler)
    t1 = asyncio.create_task(ac.get_guide(1220, detail="full", max_steps=2))
    await asyncio.sleep(0)
    t2 = asyncio.create_task(ac.get_guide(1220, detail="full"))
    truncated, waiter = await asyncio.gather(t1, t2)

    assert call_count == 1  # one shared fetch for both projections
    assert [s["stepid"] for s in truncated["steps"]] == [12345, 12346]
    # The unrestricted waiter must still see all steps.
    assert [s["stepid"] for s in waiter["steps"]] == [12345, 12346, 12347]


async def test_full_projection_does_not_poison_cached_payload() -> None:
    # QA Round 4 regression guard: after get_guide(detail="full") on a
    # cached key, the cache entry must still hold the RAW unmutated
    # payload — a fresh summary get_guide returns correct data.
    ac = _client_with(lambda r: httpx.Response(200, json=_full_guide_payload()))

    full = await ac.get_guide(1220, detail="full")
    assert full["steps"][0]["lines"][0]["text"] == "Hold the power button."

    summary = await ac.get_guide(1220, detail="summary")
    assert [s["title"] for s in summary["steps"]] == [
        "Hold the power button.",
        "Use a Phillips screwdriver.",
        "Lift it.",
    ]
    # The cached entry itself is untouched by either projection.
    key = ac._cache_key("/guides/1220", None)
    cached = ac._get_cached_ref(key, TTL_30MIN)
    assert cached["steps"][0]["lines"][0]["text_rendered"] == (
        "<p><strong>Hold</strong> the power button.</p>"
    )
    assert len(cached["steps"]) == 3


def test_strip_raw_and_convert_html_depth_cap_raises_clean_value_error() -> None:
    # A pathologically nested response must raise a clean ValueError from
    # the depth cap, not a bare RecursionError.
    ac = IfixitClient(min_request_interval=0)
    node: dict = {}
    root = node
    for _ in range(2000):
        node["child"] = {}
        node = node["child"]
    node["text_rendered"] = "<p>deep</p>"

    with pytest.raises(ValueError, match="nesting"):
        ac._strip_raw_and_convert_html(root)


@pytest.mark.parametrize("bad", [0, -1, 1.5, True])
async def test_get_guide_rejects_invalid_max_steps(bad) -> None:
    ac = _client_with(lambda r: httpx.Response(200, json=_full_guide_payload()))
    with pytest.raises(ValueError, match="max_steps"):
        await ac.get_guide(1220, detail="full", max_steps=bad)


async def test_get_guide_accepts_numeric_string_max_steps() -> None:
    # QA Round 6 (F1): the tool layer str-types max_steps, so the client
    # must accept numeric strings ("2") and convert them like ints.
    ac = _client_with(lambda r: httpx.Response(200, json=_full_guide_payload()))
    data = await ac.get_guide(1220, detail="full", max_steps="2")
    assert [s["stepid"] for s in data["steps"]] == [12345, 12346]


async def test_get_guide_404_raises_specific_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Guide not found"})

    ac = _client_with(handler)
    with pytest.raises(ValueError, match="Guide not found: 999999"):
        await ac.get_guide(999999)


async def test_get_guide_400_passes_through_unchanged() -> None:
    # Only 404s are translated into the per-guide message; other
    # ValueErrors (e.g. 400 "bad request") must pass through untouched.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": True, "message": "bad params"})

    ac = _client_with(handler)
    with pytest.raises(ValueError, match="bad request"):
        await ac.get_guide(1220)


@pytest.mark.parametrize(
    "bad", [0, -5, 1.5, "abc", "12x", "-3", "", "  ", None, True, "²", "³"]
)
async def test_get_guide_rejects_invalid_guideid(bad) -> None:
    ac = _client_with(lambda r: httpx.Response(200, json=_full_guide_payload()))
    with pytest.raises(ValueError, match="guideid"):
        await ac.get_guide(bad)


async def test_get_guide_accepts_numeric_string_guideid() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=_full_guide_payload())

    ac = _client_with(handler)
    data = await ac.get_guide(" 1220 ")
    assert data["guideid"] == 1220
    assert seen["url"].endswith("/guides/1220")


async def test_get_guide_langid_param_passthrough() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = request.url.params
        return httpx.Response(200, json=_full_guide_payload())

    ac = _client_with(handler)
    await ac.get_guide(1220, lang="de")
    assert seen["params"]["langid"] == "de"

    # No lang -> no langid param at all.
    await ac.get_guide(1220)
    assert "langid" not in seen["params"]


async def test_get_guide_cached_second_call_no_network() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json=_full_guide_payload())

    ac = _client_with(handler)
    first = await ac.get_guide(1220)
    second = await ac.get_guide(1220)
    assert first == second
    assert call_count == 1  # TTL_30MIN cache served the second call

    # Summary and full share the same raw cache entry.
    await ac.get_guide(1220, detail="full")
    assert call_count == 1


async def test_get_guide_rejects_invalid_detail() -> None:
    ac = _client_with(lambda r: httpx.Response(200, json=_full_guide_payload()))
    with pytest.raises(ValueError, match="detail"):
        await ac.get_guide(1220, detail="everything")


async def test_get_guide_rejects_top_level_list_in_summary_mode() -> None:
    # QA Round 5 (R5-1): a 200 with a top-level LIST used to raise a bare
    # AttributeError ("'list' object has no attribute 'items'") in summary
    # mode. It must surface as the clean family ValueError.
    ac = _client_with(lambda r: httpx.Response(200, json=[{"guideid": 1}]))
    with pytest.raises(ValueError, match="Unexpected API response format"):
        await ac.get_guide(1220)


async def test_get_guide_rejects_top_level_list_in_full_mode() -> None:
    # Same malformed payload in full mode: previously the list was silently
    # returned as a "successful" guide (wrong-shape success). Must be the
    # clean family ValueError too.
    ac = _client_with(lambda r: httpx.Response(200, json=[{"guideid": 1}]))
    with pytest.raises(ValueError, match="Unexpected API response format"):
        await ac.get_guide(1220, detail="full")


async def test_get_guide_rejects_overlong_numeric_string_cleanly() -> None:
    # QA Round 5 (R5-3b): int() on a 5000-digit numeric string raises
    # Python 3.11's ValueError("Exceeds the limit (4300 digits)..."); that
    # raw error must be translated into the clean family message.
    ac = _client_with(lambda r: httpx.Response(200, json=_full_guide_payload()))
    with pytest.raises(ValueError) as exc_info:
        await ac.get_guide("5" * 5000)
    message = str(exc_info.value)
    assert "guideid must be a positive integer" in message
    assert "Exceeds the limit" not in message
    assert "Traceback" not in message


async def test_get_guide_rejects_overlong_lang() -> None:
    # QA Round 14 (F14-2): get_guide's lang was the one uncapped string
    # param — "L"*100001 hit httpx's InvalidURL ("URL component 'query'
    # too long") AFTER unbounded pre-URL work, and the bare InvalidURL
    # escaped the tool except-family (it is not an httpx.HTTPError). The
    # cap must fire first: clean ValueError naming lang, no network IO.
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no network call for an overlong lang")

    ac = _client_with(handler)
    with pytest.raises(
        ValueError, match="lang exceeds maximum length of 100000 characters"
    ):
        await ac.get_guide(1220, lang="L" * 100_001)


async def test_get_user_rejects_overlong_numeric_string_cleanly() -> None:
    # Same digit-limit hardening on the other id validator (R5-3b).
    ac = _client_with(lambda r: httpx.Response(200, json={"userid": 1}))
    with pytest.raises(ValueError) as exc_info:
        await ac.get_user("9" * 5000)
    message = str(exc_info.value)
    assert "user_id must be a positive integer" in message
    assert "Exceeds the limit" not in message


# ---------------------------------------------------------------------------
# list_guides() tests
# ---------------------------------------------------------------------------


async def test_list_guides_returns_list_of_summaries() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_guide_summary(1), _guide_summary(2)])

    ac = _client_with(handler)
    data = await ac.list_guides()

    assert isinstance(data, list)
    assert len(data) == 2
    assert data[0]["guideid"] == 1
    assert data[0]["title"] == "Test Guide"
    assert data[0]["dataType"] == "guide"
    assert "revisionid" in data[0]  # summaries are returned as-is


async def test_list_guides_passes_filter_offset_limit_params() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = request.url.params
        return httpx.Response(200, json=[])

    ac = _client_with(handler)
    await ac.list_guides(filter_type="repair", offset=40, limit=50)

    assert seen["params"]["filter"] == "repair"
    assert seen["params"]["offset"] == "40"
    assert seen["params"]["limit"] == "50"


async def test_list_guides_defaults_offset_and_limit() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = request.url.params
        return httpx.Response(200, json=[])

    ac = _client_with(handler)
    await ac.list_guides()
    assert seen["params"]["offset"] == "0"
    assert seen["params"]["limit"] == "20"
    assert "filter" not in seen["params"]


async def test_list_guides_modified_since_param_passthrough() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = request.url.params
        return httpx.Response(200, json=[])

    ac = _client_with(handler)
    await ac.list_guides(modified_since="2024-01-01T00:00:00Z")
    assert seen["params"]["modifiedSince"] == "2024-01-01T00:00:00Z"


@pytest.mark.parametrize(
    "field,kwargs",
    [
        ("filter_type", {"filter_type": "x" * 100_001}),
        ("modified_since", {"modified_since": "x" * 100_001}),
    ],
)
async def test_list_guides_rejects_overlong_params(field, kwargs) -> None:
    # QA Round 14 (F14-3): filter_type/modified_since were uncapped — the
    # same InvalidURL escape as get_guide's lang. Both must fail cleanly
    # with the cap message naming the field, before any network IO.
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no network call for an overlong param")

    ac = _client_with(handler)
    with pytest.raises(
        ValueError,
        match=rf"{field} exceeds maximum length of 100000 characters",
    ):
        await ac.list_guides(**kwargs)


@pytest.mark.parametrize("bad", [0, -1, 201, 20.5, "20", True, None])
async def test_list_guides_rejects_invalid_limit(bad) -> None:
    ac = _client_with(lambda r: httpx.Response(200, json=[]))
    with pytest.raises(ValueError, match="limit"):
        await ac.list_guides(limit=bad)


@pytest.mark.parametrize("bad", [-1, -100, 1.5, "0", True, None])
async def test_list_guides_rejects_invalid_offset(bad) -> None:
    ac = _client_with(lambda r: httpx.Response(200, json=[]))
    with pytest.raises(ValueError, match="offset"):
        await ac.list_guides(offset=bad)


async def test_list_guides_not_cached() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json=[_guide_summary(call_count)])

    ac = _client_with(handler)
    await ac.list_guides()
    await ac.list_guides()
    assert call_count == 2  # paginated list endpoint is never cached
    assert len(ac._cache) == 0


async def test_list_guides_rejects_non_list_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"guides": []})

    ac = _client_with(handler)
    with pytest.raises(ValueError, match="Unexpected API response format"):
        await ac.list_guides()


# ---------------------------------------------------------------------------
# Search + categories fixtures (shapes from RESEARCH.md §3c / §3d)
# ---------------------------------------------------------------------------


def _suggest_payload(query: str = "iphone") -> dict:
    """A realistic /suggest response: {query, results[]} (RESEARCH.md §3c)."""
    return {
        "query": query,
        "results": [
            {
                "dataType": "guide",
                "guideid": 1220,
                "title": "iPhone 5c Battery Replacement",
                "url": "https://www.ifixit.com/Guide/iPhone+5c+Battery+Replacement/1220",
                "category": "iPhone",
                "subject": "Battery",
                "difficulty": "Moderate",
                "time_required_max": 2400,
                "public": True,
                "userid": 1,
                "username": "kaykay",
                "flags": [],
                "image": {
                    "id": 14056,
                    "guid": "s.ABC",
                    "original": "https://guide-images.cdn.ifixit.com/igi/s.ABC.original",
                },
            },
            {
                "dataType": "wiki",
                "wikiid": 99,
                "namespace": "CATEGORY",
                "langid": "en",
                "title": "iPhone",
                "display_title": "iPhone",
                "summary": "Apple's flagship phone.",
                "url": "https://www.ifixit.com/Device/iPhone",
                "modified_date": 1477099265,
                "author": {"userid": 1, "username": "kaykay"},
                "image": None,
            },
        ],
    }


# The 16 real top-level categories (RESEARCH.md §3d).
TOP_LEVEL_CATEGORIES = [
    "Mac",
    "Game Console",
    "Phone",
    "Camera",
    "Vehicle",
    "Household",
    "Electronics",
    "Tablet",
    "Appliance",
    "Tool",
    "PC",
    "Computer Hardware",
    "Skills",
    "Car and Truck",
    "Apparel",
    "Medical Device",
]


def _categories_payload() -> dict:
    """A small realistic /categories tree: the 16 real top-level names with
    shallow subtrees (RESEARCH.md §3d). Not the real 1.5MB tree."""
    return {
        "Mac": {
            "iPod": None,
            "MacBook": {"MacBook Air": None, "MacBook Pro": None},
            "iMac": None,
        },
        "Phone": {
            "iPhone": {"iPhone 5c": None, "iPhone 13": None},
            "Samsung Galaxy": None,
        },
        "Game Console": None,
        "Camera": None,
        "Vehicle": None,
        "Household": None,
        "Electronics": None,
        "Tablet": None,
        "Appliance": None,
        "Tool": None,
        "PC": None,
        "Computer Hardware": None,
        "Skills": None,
        "Car and Truck": None,
        "Apparel": None,
        "Medical Device": None,
    }


# ---------------------------------------------------------------------------
# search() tests
# ---------------------------------------------------------------------------


async def test_search_returns_query_and_results() -> None:
    ac = _client_with(lambda r: httpx.Response(200, json=_suggest_payload()))

    data = await ac.search("iphone")

    assert data["query"] == "iphone"
    assert len(data["results"]) == 2
    # Results pass through as-is (already compact, ≤10 from the API).
    assert data["results"][0]["dataType"] == "guide"
    assert data["results"][0]["title"] == "iPhone 5c Battery Replacement"
    assert data["results"][1]["dataType"] == "wiki"


@pytest.mark.parametrize("bad", ["", "   ", "\t\n", None, 42])
async def test_search_rejects_empty_or_non_string_query(bad) -> None:
    ac = _client_with(lambda r: httpx.Response(200, json=_suggest_payload()))
    with pytest.raises(ValueError, match="query"):
        await ac.search(bad)


# QA Round 13 (R13-5): unbounded string params. quote() can expand a
# character ~3x, and URL building + cache-key json.dumps compound that —
# a 50MB query used to force multi-hundred-MB transient allocations. All
# string params are capped at MAX_STRING_PARAM_LENGTH (100_000 chars)
# with a clean ValueError naming the field.


async def test_search_rejects_overlong_query() -> None:
    ac = _client_with(lambda r: httpx.Response(200, json=_suggest_payload()))
    with pytest.raises(
        ValueError, match="query exceeds maximum length of 100000 characters"
    ):
        await ac.search("x" * 100_001)


def test_string_length_cap_boundary_is_exact() -> None:
    # The cap boundary, exercised at the validation layer: exactly
    # MAX_STRING_PARAM_LENGTH is accepted; MAX+1 raises the clean
    # ValueError. (An end-to-end URL at 100K chars is impossible —
    # httpx hard-rejects URLs over 8192 bytes with InvalidURL — and the
    # cap exists precisely to bound the pre-URL transient allocations:
    # quote() expansion, URL construction and cache-key json.dumps.)
    assert _validate_string_length("query", "x" * 100_000) is None
    assert _validate_title("x" * 100_000) == "x" * 100_000
    with pytest.raises(
        ValueError, match="query exceeds maximum length of 100000 characters"
    ):
        _validate_string_length("query", "x" * 100_001)
    with pytest.raises(
        ValueError, match="title exceeds maximum length of 100000 characters"
    ):
        _validate_title("x" * 100_001)


@pytest.mark.parametrize(
    "field,kwargs",
    [
        ("doctypes", {"doctypes": "x" * 100_001}),
        ("device", {"device": "x" * 100_001}),
        ("lang", {"lang": "x" * 100_001}),
    ],
)
async def test_search_rejects_overlong_optional_params(field, kwargs) -> None:
    ac = _client_with(lambda r: httpx.Response(200, json=_suggest_payload()))
    with pytest.raises(
        ValueError,
        match=rf"{field} exceeds maximum length of 100000 characters",
    ):
        await ac.search("iphone", **kwargs)


@pytest.mark.parametrize(
    "doctype",
    ["guide", "item", "topic", "device", "category", "question", "post", "all"],
)
async def test_search_accepts_all_documented_doctypes(doctype) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = request.url.params
        return httpx.Response(200, json=_suggest_payload())

    ac = _client_with(handler)
    await ac.search("iphone", doctypes=doctype)
    assert seen["params"]["doctypes"] == doctype


@pytest.mark.parametrize(
    "bad",
    ["guides", "foo", "", "  ", "guide,foo", "guide,", "GUIDE", "Guide", None],
)
async def test_search_rejects_invalid_doctypes(bad) -> None:
    ac = _client_with(lambda r: httpx.Response(200, json=_suggest_payload()))
    with pytest.raises(ValueError, match="doctypes"):
        await ac.search("iphone", doctypes=bad)


async def test_search_accepts_doctypes_csv() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = request.url.params
        return httpx.Response(200, json=_suggest_payload())

    ac = _client_with(handler)
    await ac.search("iphone", doctypes="guide, device")
    assert seen["params"]["doctypes"] == "guide,device"


async def test_search_device_param_passthrough() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = request.url.params
        return httpx.Response(200, json=_suggest_payload())

    ac = _client_with(handler)
    await ac.search("battery", device="iPhone 13")
    assert seen["params"]["guideDevice"] == "iPhone 13"

    # No device -> no guideDevice param at all.
    await ac.search("battery")
    assert "guideDevice" not in seen["params"]


async def test_search_lang_param_passthrough() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = request.url.params
        return httpx.Response(200, json=_suggest_payload())

    ac = _client_with(handler)
    await ac.search("battery", lang="de")
    assert seen["params"]["langid"] == "de"

    await ac.search("battery")
    assert "langid" not in seen["params"]


async def test_search_query_goes_into_path_stripped_and_encoded() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=_suggest_payload())

    ac = _client_with(handler)
    await ac.search("  iPhone 13  ")
    assert "/suggest/iPhone%2013" in seen["url"]


async def test_search_not_cached() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json=_suggest_payload())

    ac = _client_with(handler)
    await ac.search("iphone")
    await ac.search("iphone")
    assert call_count == 2  # volatile suggestions are never cached
    assert len(ac._cache) == 0


async def test_search_rejects_non_dict_response() -> None:
    ac = _client_with(lambda r: httpx.Response(200, json=[1, 2]))
    with pytest.raises(ValueError, match="Unexpected API response format"):
        await ac.search("iphone")


async def test_search_missing_results_key_raises() -> None:
    ac = _client_with(lambda r: httpx.Response(200, json={"query": "iphone"}))
    with pytest.raises(ValueError, match="Unexpected API response format"):
        await ac.search("iphone")


async def test_search_non_list_results_raises() -> None:
    ac = _client_with(
        lambda r: httpx.Response(200, json={"query": "iphone", "results": "nope"})
    )
    with pytest.raises(ValueError, match="Unexpected API response format"):
        await ac.search("iphone")


# ---------------------------------------------------------------------------
# get_categories() tests
# ---------------------------------------------------------------------------


async def test_get_categories_top_level_returns_16_sorted_names() -> None:
    ac = _client_with(lambda r: httpx.Response(200, json=_categories_payload()))

    names = await ac.get_categories()

    assert len(names) == 16
    assert names == sorted(TOP_LEVEL_CATEGORIES)
    # Only projected name lists are returned — never the raw nested tree.
    assert all(isinstance(n, str) for n in names)


async def test_get_categories_subtree_navigation() -> None:
    ac = _client_with(lambda r: httpx.Response(200, json=_categories_payload()))

    assert await ac.get_categories("Mac") == ["MacBook", "iMac", "iPod"]
    assert await ac.get_categories("Mac/MacBook") == ["MacBook Air", "MacBook Pro"]
    assert await ac.get_categories("Phone/iPhone") == ["iPhone 13", "iPhone 5c"]


async def test_get_categories_leaf_returns_empty_list() -> None:
    ac = _client_with(lambda r: httpx.Response(200, json=_categories_payload()))

    assert await ac.get_categories("Mac/iPod") == []
    assert await ac.get_categories("Game Console") == []


@pytest.mark.parametrize(
    "bad",
    ["Nintendo", "Mac/Nintendo", "Mac/iPod/Deep", "Phone/iPhone/iPhone 5c/X"],
)
async def test_get_categories_invalid_path_raises(bad) -> None:
    ac = _client_with(lambda r: httpx.Response(200, json=_categories_payload()))
    with pytest.raises(ValueError, match="Category not found"):
        await ac.get_categories(bad)


async def test_get_categories_path_normalization() -> None:
    ac = _client_with(lambda r: httpx.Response(200, json=_categories_payload()))

    # Whitespace around segments is stripped.
    assert await ac.get_categories("  Mac / MacBook  ") == [
        "MacBook Air",
        "MacBook Pro",
    ]
    # Empty segments (consecutive/leading/trailing slashes) are rejected.
    for bad in ["/", "Mac//MacBook", "/Mac", "Mac/", "Mac/ /MacBook"]:
        with pytest.raises(ValueError):
            await ac.get_categories(bad)


async def test_get_categories_empty_path_means_top_level() -> None:
    # QA finding: browse_categories(path="") errored instead of returning
    # the top level. An empty/whitespace-only path is now treated as None.
    ac = _client_with(lambda r: httpx.Response(200, json=_categories_payload()))

    assert await ac.get_categories("") == sorted(TOP_LEVEL_CATEGORIES)
    assert await ac.get_categories("   ") == sorted(TOP_LEVEL_CATEGORIES)


async def test_get_categories_navigation_does_not_deep_copy_tree(
    monkeypatch,
) -> None:
    # QA finding: every browse_categories call deep-copied the whole ~1.5MB
    # tree. Navigation must read the cached tree directly; the only deep
    # copy happens when the tree is first stored.
    import copy as copy_module

    original_deepcopy = copy_module.deepcopy  # capture BEFORE patching
    deepcopy_calls: list = []

    def spy_deepcopy(obj, *args, **kwargs):
        deepcopy_calls.append(obj)
        return original_deepcopy(obj, *args, **kwargs)

    monkeypatch.setattr(copy_module, "deepcopy", spy_deepcopy)

    ac = _client_with(lambda r: httpx.Response(200, json=_categories_payload()))
    await ac.get_categories("Mac")  # populate cache (one deep copy on write)
    assert len(deepcopy_calls) == 1

    await ac.get_categories("Mac/MacBook")
    await ac.get_categories()
    assert len(deepcopy_calls) == 1  # navigation never copies the tree


async def test_get_categories_cached_ref_is_same_object_across_calls() -> None:
    # The cache entry object itself is served (identity preserved) — proof
    # that navigation doesn't produce per-call copies.
    ac = _client_with(lambda r: httpx.Response(200, json=_categories_payload()))
    await ac.get_categories()

    key = ac._cache_key("/categories", None)
    ref1 = ac._get_cached_ref(key, TTL_30MIN)
    await ac.get_categories("Mac")
    ref2 = ac._get_cached_ref(key, TTL_30MIN)

    assert ref1 is ref2
    assert ref1 == _categories_payload()
    # The public get() still returns deep copies.
    public = await ac.get("/categories")
    assert public is not ref1


async def test_get_categories_raw_tree_fetched_once_then_projected() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json=_categories_payload())

    ac = _client_with(handler)
    top = await ac.get_categories()
    subtree = await ac.get_categories("Mac")
    deep = await ac.get_categories("Mac/MacBook")

    assert call_count == 1  # the 1.5MB tree is fetched once, cached (TTL_30MIN)
    assert top == sorted(TOP_LEVEL_CATEGORIES)
    assert subtree == ["MacBook", "iMac", "iPod"]
    assert deep == ["MacBook Air", "MacBook Pro"]


async def test_get_categories_returned_lists_are_deep_copies() -> None:
    ac = _client_with(lambda r: httpx.Response(200, json=_categories_payload()))

    first = await ac.get_categories()
    first.clear()
    first.append("Hacked")

    # Mutating the returned list must not corrupt the cache.
    second = await ac.get_categories()
    assert second == sorted(TOP_LEVEL_CATEGORIES)
    assert "Hacked" not in second

    subtree = await ac.get_categories("Mac")
    subtree[:] = []
    assert await ac.get_categories("Mac") == ["MacBook", "iMac", "iPod"]


async def test_get_categories_rejects_non_dict_response() -> None:
    ac = _client_with(lambda r: httpx.Response(200, json=[1, 2]))
    with pytest.raises(ValueError, match="Unexpected API response format"):
        await ac.get_categories()


async def test_get_categories_rejects_non_string_path() -> None:
    ac = _client_with(lambda r: httpx.Response(200, json=_categories_payload()))
    with pytest.raises(ValueError, match="path"):
        await ac.get_categories(42)


async def test_get_categories_rejects_overlong_path() -> None:
    # QA Round 13 (R13-5): the path is capped like every other string
    # param — a 100K-char path must fail cleanly, before any IO.
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json=_categories_payload())

    ac = _client_with(handler)
    with pytest.raises(
        ValueError, match="path exceeds maximum length of 100000 characters"
    ):
        await ac.get_categories("Mac/" + "x" * 100_000)
    assert call_count == 0


@pytest.mark.parametrize(
    "tree,path",
    [
        ({"Mac": "junk"}, "Mac"),
        ({"Mac": ["a"]}, "Mac"),
        ({"Mac": 42}, "Mac"),
        ({"Mac": {"iPod": "junk"}}, "Mac/iPod"),
    ],
)
async def test_get_categories_malformed_tree_raises_value_error(
    tree, path
) -> None:
    # A subtree value that is neither a nested dict nor None is a
    # malformed tree — reject it instead of silently returning [].
    ac = _client_with(lambda r: httpx.Response(200, json=tree))
    with pytest.raises(ValueError, match="Unexpected API response format"):
        await ac.get_categories(path)


async def test_get_categories_malformed_tree_mid_path_raises() -> None:
    ac = _client_with(lambda r: httpx.Response(200, json={"Mac": "junk"}))
    with pytest.raises(ValueError):
        await ac.get_categories("Mac/MacBook")


async def test_get_categories_bad_path_rejected_before_network_io() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json=_categories_payload())

    ac = _client_with(handler)
    for bad in ["Mac//iPod", "/Mac", "Mac/", "Mac/ /MacBook", 42]:
        with pytest.raises(ValueError):
            await ac.get_categories(bad)
    assert call_count == 0  # path validation happens before any network IO


async def test_get_categories_mixed_type_keys_raise_clean_value_error(
    monkeypatch,
) -> None:
    ac = IfixitClient(min_request_interval=0)

    async def fake_get(path, params=None, cache_ttl=TTL_30MIN):
        return {"Mac": {"iPod": None, 42: None}}

    monkeypatch.setattr(ac, "get", fake_get)
    # sorted() over mixed-type keys would raise TypeError; the guard must
    # surface a clean ValueError instead.
    with pytest.raises(ValueError, match="Unexpected API response format"):
        await ac.get_categories("Mac")


async def test_get_categories_top_level_mixed_type_keys_raise(monkeypatch) -> None:
    ac = IfixitClient(min_request_interval=0)

    async def fake_get(path, params=None, cache_ttl=TTL_30MIN):
        return {"Mac": None, 42: None}

    monkeypatch.setattr(ac, "get", fake_get)
    with pytest.raises(ValueError, match="Unexpected API response format"):
        await ac.get_categories()


# ---------------------------------------------------------------------------
# Device wiki fixtures (shapes from RESEARCH.md §3e + openapi Wiki schema)
# ---------------------------------------------------------------------------


def _wiki_guide_item(
    guideid: int, title: str = "Test Guide", *, with_optional: bool = True
) -> dict:
    """A lightweight guide object as embedded in a wiki page's guides[] /
    featured_guides[] lists (openapi Wiki schema)."""
    item = {
        "dataType": "guide",
        "guideid": guideid,
        "locale": "en",
        "revisionid": 1000 + guideid,
        "url": f"https://www.ifixit.com/Guide/{title.replace(' ', '+')}/{guideid}",
        "type": "replacement",
        "category": "iPhone",
        "subject": "Battery",
        "title": title,
        "summary": "A summary.",
        "public": True,
        "userid": 1,
        "username": "kaykay",
        "flags": [],
    }
    if with_optional:
        item["difficulty"] = "Moderate"
        item["time_required_max"] = 2400
        item["image"] = {
            "id": 14056,
            "guid": "s.ABC",
            "thumbnail": "https://guide-images.cdn.ifixit.com/igi/s.ABC.thumbnail",
        }
    return item


def _device_wiki_payload(
    *, with_score: bool = True, long_description: bool = False
) -> dict:
    """A realistic /wikis/CATEGORY/{title} response (RESEARCH.md §3e, 39 keys)."""
    payload = {
        "wikiid": 42,
        "namespace": "CATEGORY",
        "title": "iPhone 13",
        "display_title": "iPhone 13",
        "revisionid": 777,
        "source_revisionid": 778,
        "modified_date": 1477099265,
        "langid": "en",
        "url": "https://www.ifixit.com/Device/iPhone+13",
        "description": "Apple's flagship smartphone released in 2021.",
        "contents_raw": "== Intro ==\nLine one.\n",
        "contents_rendered": "<h2>Intro</h2><p>Line one.</p>",
        "contents_json": {"type": "doc", "content": []},
        "linked_wikis": [{"wikiid": 1, "title": "Battery"}],
        "related_wikis": [{"wikiid": 2, "title": "iPhone 13 Pro"}],
        "flags": {"1": {"flagid": 1, "title": "WIKI_USER_CONTRIBUTED"}},
        "ancestors": [
            {
                "dataType": "wiki",
                "wikiid": 10,
                "namespace": "CATEGORY",
                "langid": "en",
                "title": "Phone",
                "display_title": "Phone",
                "summary": "Repair guides for phones.",
                "url": "https://www.ifixit.com/Device/Phone",
                # The live API embeds full wiki objects (incl. image dicts);
                # the projection must reduce these to names only.
                "image": {
                    "id": 715911,
                    "guid": "vPdIkfeZ6PyHM32H",
                    "thumbnail": "https://guide-images.cdn.ifixit.com/igi/t.thumbnail",
                },
            }
        ],
        "children": [
            {
                "wikiid": 50,
                "namespace": "CATEGORY",
                "title": "iPhone_13_Battery",
                "display_title": "iPhone 13 Battery",
            },
            {
                "wikiid": 51,
                "namespace": "CATEGORY",
                "title": "iPhone_13_Screen",
                "display_title": "iPhone 13 Screen",
            },
        ],
        "guides": [
            _wiki_guide_item(1001, "iPhone 13 Battery Replacement"),
            _wiki_guide_item(1002, "iPhone 13 Screen Replacement"),
        ],
        "featured_guides": [
            # Duplicate of guides[0] — exercises merge dedupe.
            _wiki_guide_item(1001, "iPhone 13 Battery Replacement"),
            _wiki_guide_item(1003, "iPhone 13 Teardown"),
        ],
        "parts": [
            {
                "type": "parts",
                "quantity": 1,
                "text": "iPhone 13 Battery",
                "url": "https://www.ifixit.com/Store/Parts/iphone-13-battery",
            },
            {
                "type": "parts",
                "quantity": 1,
                "text": "iPhone 13 Screen",
                "url": "https://www.ifixit.com/Store/Parts/iphone-13-screen",
            },
        ],
        "tools": [
            {
                "title": "Phillips #00 Screwdriver",
                "target_url": "https://www.ifixit.com/Store/Tools/phillips-00-screwdriver",
                "image_url": "https://cart-products.cdn.ifixit.com/screw.png",
                "itemcode": "IF145-307-1",
            }
        ],
        "repairability_score": 6,
        "maintenance_schedule": {"schedules": []},
        "tags": ["smartphone"],
        "category_info": {"category_type": "device"},
        "category_lists": {},
        "is_troubleshooting": False,
        "can_edit": False,
        "available_langids": ["en", "de"],
        "page_title": "iPhone 13 - iFixit",
        "solutions_url": "https://www.ifixit.com/Answers/Device/iPhone+13",
        "author": 1,
        "authors": [1],
        "created_date": 1234567890,
        "diagrams": [],
        "documents": [],
        "info": [],
        "parent": "Phone",
    }
    if not with_score:
        del payload["repairability_score"]
    if long_description:
        payload["description"] = "x" * 600
    return payload


def _maintenance_payload(*, inherited: str | None = "Nintendo Switch Family") -> dict:
    """A realistic /wikis/maintenance/CATEGORY/{title} response.

    Live responses carry ``schedules`` plus ``inheritedFrom`` (the parent
    device name, or null when the schedule is the device's own).
    """
    payload: dict[str, Any] = {
        "schedules": [
            {
                "wiki_maintenance_scheduleid": 1,
                "task": "Replace the battery",
                "status": "active",
                "triggers": [
                    {
                        "trigger_type": "condition",
                        "metric": "battery_health_percent",
                        "unit": "%",
                        "first_interval": "80",
                    },
                    {
                        "trigger_type": "time",
                        "metric": "elapsed_time",
                        "unit": "months",
                        "repeat_interval": "24",
                    },
                ],
            }
        ],
        "langid": "en",
        "revisionid": 1242742,
    }
    if inherited is not None:
        payload["inheritedFrom"] = inherited
    else:
        payload["inheritedFrom"] = None
    return payload


# ---------------------------------------------------------------------------
# get_device() tests
# ---------------------------------------------------------------------------


async def test_get_device_projects_summary_only() -> None:
    payload = _device_wiki_payload()
    ac = _client_with(lambda r: httpx.Response(200, json=payload))

    data = await ac.get_device("iPhone 13")

    # Only the projected keys are present.
    assert set(data.keys()) == {
        "title",
        "display_title",
        "repairability_score",
        "summary",
        "featured_guides",
        "children",
        "parts_count",
        "tools_count",
        "ancestors",
    }
    assert data["title"] == "iPhone 13"
    assert data["display_title"] == "iPhone 13"
    # 'summary' is projected from the wiki 'description'.
    assert data["summary"] == payload["description"]
    # featured_guides are compacted to title/guideid/url only.
    assert data["featured_guides"] == [
        {
            "title": "iPhone 13 Battery Replacement",
            "guideid": 1001,
            "url": "https://www.ifixit.com/Guide/iPhone+13+Battery+Replacement/1001",
        },
        {
            "title": "iPhone 13 Teardown",
            "guideid": 1003,
            "url": "https://www.ifixit.com/Guide/iPhone+13+Teardown/1003",
        },
    ]
    # children are names only.
    assert data["children"] == ["iPhone 13 Battery", "iPhone 13 Screen"]
    # counts are derived from the parts/tools lists.
    assert data["parts_count"] == 2
    assert data["tools_count"] == 1
    # ancestors are projected to breadcrumb names only — the live API
    # embeds full wiki objects (image dicts, summaries, urls) there.
    assert data["ancestors"] == ["Phone"]
    # Raw content, link graphs, flags and revision metadata are stripped.
    for stripped in (
        "contents_raw",
        "contents_rendered",
        "contents_json",
        "linked_wikis",
        "related_wikis",
        "flags",
        "revisionid",
        "source_revisionid",
        "wikiid",
        "tags",
        "category_info",
    ):
        assert stripped not in data


async def test_get_device_summary_truncated_to_500_chars() -> None:
    payload = _device_wiki_payload(long_description=True)
    ac = _client_with(lambda r: httpx.Response(200, json=payload))

    data = await ac.get_device("iPhone 13")

    assert data["summary"] == payload["description"][:500]
    assert len(data["summary"]) == 500


@pytest.mark.parametrize("missing", ["absent", "null"])
async def test_get_device_repairability_score_omitted_when_unset(missing) -> None:
    payload = _device_wiki_payload()
    if missing == "absent":
        del payload["repairability_score"]
    else:
        payload["repairability_score"] = None
    ac = _client_with(lambda r: httpx.Response(200, json=payload))

    data = await ac.get_device("iPhone 13")

    assert "repairability_score" not in data


async def test_get_device_counts_default_to_zero_when_keys_missing() -> None:
    payload = _device_wiki_payload()
    del payload["parts"]
    del payload["tools"]
    ac = _client_with(lambda r: httpx.Response(200, json=payload))

    data = await ac.get_device("iPhone 13")

    assert data["parts_count"] == 0
    assert data["tools_count"] == 0


async def test_get_device_parts_object_counts_summed_categories() -> None:
    # QA finding: the live API returns `parts` as an OBJECT
    # ({url, categories: [{tag, count, url}]}) for CATEGORY wikis — the
    # code counted only lists, so parts_count was permanently 0 (verified
    # live: iPhone has 17 categories summing to 583). The object shape
    # counts as the sum of the category counts.
    payload = _device_wiki_payload()
    payload["parts"] = {
        "url": "https://www.ifixit.com/Store/Parts/iPhone-13",
        "categories": [
            {"tag": "Batteries", "count": 44, "url": "https://.../Batteries"},
            {"tag": "Screens", "count": 12, "url": "https://.../Screens"},
            # Non-dict / non-int entries are ignored, not fatal.
            {"tag": "Junk", "count": "many"},
            "not-a-dict",
        ],
    }
    ac = _client_with(lambda r: httpx.Response(200, json=payload))

    data = await ac.get_device("iPhone 13")

    assert data["parts_count"] == 56
    assert data["tools_count"] == 1


async def test_get_device_parts_empty_object_counts_zero() -> None:
    payload = _device_wiki_payload()
    payload["parts"] = {"url": "https://www.ifixit.com/Store/Parts/iPhone-13", "categories": []}
    ac = _client_with(lambda r: httpx.Response(200, json=payload))

    data = await ac.get_device("iPhone 13")

    assert data["parts_count"] == 0


async def test_get_device_404_raises_device_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Endpoint not found"})

    ac = _client_with(handler)
    with pytest.raises(ValueError, match="Device not found: iPhone 13"):
        await ac.get_device("iPhone 13")


@pytest.mark.parametrize("bad", ["", "   ", "\t\n", None, 42])
async def test_get_device_rejects_invalid_title(bad) -> None:
    ac = _client_with(lambda r: httpx.Response(200, json=_device_wiki_payload()))
    with pytest.raises(ValueError, match="title"):
        await ac.get_device(bad)


async def test_get_device_rejects_overlong_title() -> None:
    # QA Round 13 (R13-5): titles are capped at 100_000 chars with a
    # clean ValueError before any network IO or quote() expansion.
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json=_device_wiki_payload())

    ac = _client_with(handler)
    with pytest.raises(
        ValueError, match="title exceeds maximum length of 100000 characters"
    ):
        await ac.get_device("x" * 100_001)
    assert call_count == 0


async def test_get_device_title_encoded_in_path() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=_device_wiki_payload())

    ac = _client_with(handler)
    await ac.get_device("  iPhone 13  ")
    assert "/wikis/CATEGORY/iPhone%2013" in seen["url"]


@pytest.mark.parametrize("ns", ["WIKI", "CATEGORY", "INFO", "ITEM", "USER", "TEAM"])
async def test_get_device_accepts_all_documented_namespaces(ns) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=_device_wiki_payload())

    ac = _client_with(handler)
    await ac.get_device("iPhone 13", namespace=ns)
    assert f"/wikis/{ns}/iPhone%2013" in seen["url"]


@pytest.mark.parametrize(
    "bad", ["", "   ", None, 42, "wiki", "category", "WIKIS", "GUIDE", "User"]
)
async def test_get_device_rejects_invalid_namespace_before_io(bad) -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json=_device_wiki_payload())

    ac = _client_with(handler)
    with pytest.raises(ValueError, match="namespace"):
        await ac.get_device("iPhone 13", namespace=bad)
    assert call_count == 0  # rejected before any network IO


async def test_get_device_cached_second_call_no_network() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json=_device_wiki_payload())

    ac = _client_with(handler)
    first = await ac.get_device("iPhone 13")
    second = await ac.get_device("iPhone 13")
    assert first == second
    assert call_count == 1  # TTL_30MIN cache served the second call


# ---------------------------------------------------------------------------
# list_device_guides() tests
# ---------------------------------------------------------------------------


async def test_list_device_guides_merges_guides_then_featured_deduped() -> None:
    ac = _client_with(lambda r: httpx.Response(200, json=_device_wiki_payload()))

    data = await ac.list_device_guides("iPhone 13")

    # guides[] first, then featured_guides[]; guide 1001 appears in both
    # lists but must be returned once.
    assert [g["guideid"] for g in data] == [1001, 1002, 1003]


async def test_list_device_guides_compact_projection() -> None:
    ac = _client_with(lambda r: httpx.Response(200, json=_device_wiki_payload()))

    data = await ac.list_device_guides("iPhone 13")

    assert len(data) == 3
    item = data[0]
    # Only the compact keys are present — no dataType/revisionid/flags/image.
    assert set(item.keys()) == {
        "guideid",
        "title",
        "url",
        "difficulty",
        "time_required_max",
        "image_thumbnail",
    }
    assert item["guideid"] == 1001
    assert item["title"] == "iPhone 13 Battery Replacement"
    assert item["difficulty"] == "Moderate"
    assert item["time_required_max"] == 2400
    assert item["image_thumbnail"] == (
        "https://guide-images.cdn.ifixit.com/igi/s.ABC.thumbnail"
    )


async def test_list_device_guides_missing_optional_keys_omitted() -> None:
    payload = _device_wiki_payload()
    payload["guides"] = [_wiki_guide_item(2001, "Minimal Guide", with_optional=False)]
    payload["featured_guides"] = []
    ac = _client_with(lambda r: httpx.Response(200, json=payload))

    data = await ac.list_device_guides("iPhone 13")

    assert len(data) == 1
    # difficulty/time_required_max/image_thumbnail are not fabricated.
    assert set(data[0].keys()) == {"guideid", "title", "url"}
    assert data[0]["guideid"] == 2001


async def test_list_device_guides_missing_lists_yield_empty() -> None:
    payload = _device_wiki_payload()
    del payload["guides"]
    del payload["featured_guides"]
    ac = _client_with(lambda r: httpx.Response(200, json=payload))

    assert await ac.list_device_guides("iPhone 13") == []


async def test_list_device_guides_404_raises_device_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Endpoint not found"})

    ac = _client_with(handler)
    with pytest.raises(ValueError, match="Device not found: iPhone 13"):
        await ac.list_device_guides("iPhone 13")


@pytest.mark.parametrize("bad", ["", "   ", None, 42])
async def test_list_device_guides_rejects_invalid_title(bad) -> None:
    ac = _client_with(lambda r: httpx.Response(200, json=_device_wiki_payload()))
    with pytest.raises(ValueError, match="title"):
        await ac.list_device_guides(bad)


@pytest.mark.parametrize("bad_guideid", [[1, 2], {"id": 1}, True])
async def test_list_device_guides_non_scalar_guideid_raises_clean_value_error(
    bad_guideid,
) -> None:
    # QA Round 5 (R5-2): an unhashable guideid (list/dict) used to raise a
    # bare TypeError("unhashable type: 'list'") from the dedup set; a bool
    # guideid would silently alias guideid 1 (True == 1 in a set). All must
    # surface as the clean family ValueError.
    payload = _device_wiki_payload()
    payload["guides"] = [
        _wiki_guide_item(3001, "Fine Guide"),
        {"guideid": bad_guideid, "title": "Malformed"},
    ]
    payload["featured_guides"] = []
    ac = _client_with(lambda r: httpx.Response(200, json=payload))
    with pytest.raises(ValueError, match="Unexpected API response format"):
        await ac.list_device_guides("iPhone 13")


# ---------------------------------------------------------------------------
# get_maintenance_schedule() tests
# ---------------------------------------------------------------------------


async def test_get_maintenance_schedule_returns_schedules_with_triggers() -> None:
    payload = _maintenance_payload()
    ac = _client_with(lambda r: httpx.Response(200, json=payload))

    data = await ac.get_maintenance_schedule("iPhone 13")

    assert data == {
        "schedules": payload["schedules"],
        # QA finding: inheritedFrom was silently dropped. Live (verified):
        # Nintendo Switch returns schedules + inheritedFrom "Nintendo Switch
        # Family". The projection keeps it under an honest snake_case key.
        "inherited_from": "Nintendo Switch Family",
    }
    schedule = data["schedules"][0]
    assert schedule["task"] == "Replace the battery"
    assert schedule["status"] == "active"
    assert schedule["triggers"][0]["metric"] == "battery_health_percent"
    assert schedule["triggers"][1]["trigger_type"] == "time"


async def test_get_maintenance_schedule_null_inherited_from_omitted() -> None:
    # inheritedFrom: null (the device's own schedule) must not produce an
    # inherited_from key — the projection never fabricates fields.
    payload = _maintenance_payload(inherited=None)
    ac = _client_with(lambda r: httpx.Response(200, json=payload))

    data = await ac.get_maintenance_schedule("iPhone 13")

    assert data == {"schedules": payload["schedules"]}
    assert "inherited_from" not in data


async def test_get_maintenance_schedule_missing_schedules_key_returns_empty() -> None:
    ac = _client_with(lambda r: httpx.Response(200, json={"foo": "bar"}))

    assert await ac.get_maintenance_schedule("iPhone 13") == {"schedules": []}


async def test_get_maintenance_schedule_path_uses_maintenance_endpoint() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=_maintenance_payload())

    ac = _client_with(handler)
    await ac.get_maintenance_schedule("iPhone 13")
    assert "/wikis/maintenance/CATEGORY/iPhone%2013" in seen["url"]


async def test_get_maintenance_schedule_404_raises_specific_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Endpoint not found"})

    ac = _client_with(handler)
    with pytest.raises(ValueError, match="No maintenance schedule for: iPhone 13"):
        await ac.get_maintenance_schedule("iPhone 13")


@pytest.mark.parametrize("bad", ["", "   ", "\t", None, 42])
async def test_get_maintenance_schedule_rejects_invalid_title(bad) -> None:
    ac = _client_with(lambda r: httpx.Response(200, json=_maintenance_payload()))
    with pytest.raises(ValueError, match="title"):
        await ac.get_maintenance_schedule(bad)


# ---------------------------------------------------------------------------
# Media fixtures (shape from RESEARCH.md §3g)
# ---------------------------------------------------------------------------


def _media_payload(media_id: int = 14056) -> dict:
    """A realistic /media/images/{id} response (RESEARCH.md §3g)."""
    return {
        "image": {
            "id": media_id,
            "guid": "s.ABC",
            "mini": "https://guide-images.cdn.ifixit.com/igi/s.ABC.mini",
            "thumbnail": "https://guide-images.cdn.ifixit.com/igi/s.ABC.thumbnail",
            "140x105": "https://guide-images.cdn.ifixit.com/igi/s.ABC.140x105",
            "200x150": "https://guide-images.cdn.ifixit.com/igi/s.ABC.200x150",
            "standard": "https://guide-images.cdn.ifixit.com/igi/s.ABC.standard",
            "440x330": "https://guide-images.cdn.ifixit.com/igi/s.ABC.440x330",
            "medium": "https://guide-images.cdn.ifixit.com/igi/s.ABC.medium",
            "large": "https://guide-images.cdn.ifixit.com/igi/s.ABC.large",
            "huge": "https://guide-images.cdn.ifixit.com/igi/s.ABC.huge",
            "original": "https://guide-images.cdn.ifixit.com/igi/s.ABC.original",
        }
    }


# ---------------------------------------------------------------------------
# User fixtures (shape from RESEARCH.md §3f)
# ---------------------------------------------------------------------------


def _user_profile_payload(user_id: int = 1) -> dict:
    """A realistic /users/{id} response (RESEARCH.md §3f).

    Carries the live about fields: about_rendered (full HTML incl. an
    imageBox div with an onclick img, as served for user 1) and about_raw
    (wiki markup). get_user must sanitize both away.
    """
    return {
        "userid": user_id,
        "username": "kaykay",
        "unique_username": "kaykay",
        "image": {
            "id": 14056,
            "guid": "s.ABC",
            "original": "https://guide-images.cdn.ifixit.com/igi/s.ABC.original",
        },
        "teams": ["Masters"],
        "reputation": 12345,
        "join_date": "2010-01-01T00:00:00Z",
        "certification_count": 3,
        "badge_counts": {"bronze": 10, "silver": 5, "gold": 2, "total": 17},
        "summary": "iFixit staff",
        "url": "https://www.ifixit.com/User/1/kaykay",
        "about_rendered": (
            '<div class="imageBox imageBox_center"><img '
            'src="https://guide-images.cdn.ifixit.com/igi/CE4caKTjZxtrjMrS.medium" '
            'width="267" height="444" class="hasMenu hasLarge" alt="Image" '
            'onclick="window.open(&quot;https:\\/\\/guide-images.cdn.ifixit.com\\/igi\\/'
            'CE4caKTjZxtrjMrS.full&quot;, \'\', \'width=300,height=500\')" />'
            "</div><p>iFixit staff since 2010</p>"
        ),
        "about_raw": "[image|66427|size=medium|align=center]",
    }


# ---------------------------------------------------------------------------
# get_media() tests
# ---------------------------------------------------------------------------


async def test_get_media_returns_cdn_url_dict() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=_media_payload())

    ac = _client_with(handler)
    data = await ac.get_media(14056)

    assert "/media/images/14056" in seen["url"]
    assert isinstance(data, dict)
    image = data["image"]
    assert image["id"] == 14056
    assert image["guid"] == "s.ABC"
    assert image["original"].endswith(".original")
    # All size URLs pass through untouched.
    assert image["thumbnail"].endswith(".thumbnail")
    assert image["huge"].endswith(".huge")


@pytest.mark.parametrize("bad", ["junk", "Images", "IMAGE", "image", "", None, 42])
async def test_get_media_rejects_invalid_media_type(bad) -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json=_media_payload())

    ac = _client_with(handler)
    with pytest.raises(ValueError, match="media_type"):
        await ac.get_media(14056, media_type=bad)
    assert call_count == 0  # rejected before any network IO


@pytest.mark.parametrize("media_type", ["images", "videos", "documents"])
async def test_get_media_accepts_all_media_types(media_type) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=_media_payload())

    ac = _client_with(handler)
    await ac.get_media(14056, media_type=media_type)
    assert f"/media/{media_type}/14056" in seen["url"]


@pytest.mark.parametrize(
    "bad", [0, -5, 1.5, "abc", "12x", "-3", "", "  ", None, True, "²"]
)
async def test_get_media_rejects_invalid_media_id(bad) -> None:
    ac = _client_with(lambda r: httpx.Response(200, json=_media_payload()))
    with pytest.raises(ValueError, match="media_id"):
        await ac.get_media(bad)


async def test_get_media_403_raises_media_not_accessible() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "Forbidden"})

    ac = _client_with(handler)
    with pytest.raises(ValueError, match="Media not accessible"):
        await ac.get_media(14056)


async def test_get_media_forbidden_sentinel_translated(monkeypatch) -> None:
    # The translation keys off the ForbiddenError sentinel, not string
    # equality on the message (QA finding).
    ac = IfixitClient(min_request_interval=0)

    async def fake_get(path, params=None, cache_ttl=TTL_HOUR):
        raise ForbiddenError("forbidden")

    monkeypatch.setattr(ac, "get", fake_get)
    with pytest.raises(ValueError, match="Media not accessible"):
        await ac.get_media(14056)


async def test_get_media_404_raises_media_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Media not found"})

    ac = _client_with(handler)
    with pytest.raises(ValueError, match="Media not found: 999999"):
        await ac.get_media(999999)


async def test_get_media_cached_for_ttl_hour() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json=_media_payload())

    ac = _client_with(handler)
    first = await ac.get_media(14056)
    second = await ac.get_media("14056")  # numeric string, same key
    assert first == second
    assert call_count == 1  # TTL_HOUR cache served the second call


# ---------------------------------------------------------------------------
# get_user() tests
# ---------------------------------------------------------------------------


async def test_get_user_returns_profile_with_sanitized_about() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=_user_profile_payload())

    ac = _client_with(handler)
    data = await ac.get_user(1)

    assert "/users/1" in seen["url"]
    # HTML hygiene: about_rendered is converted to plain-text about_text,
    # about_raw wiki markup is dropped, and every other profile field
    # passes through untouched.
    assert "about_rendered" not in data
    assert "about_raw" not in data
    assert data["about_text"] == "iFixit staff since 2010"
    assert "<" not in data["about_text"] and "&" not in data["about_text"]
    raw = _user_profile_payload()
    for key, value in raw.items():
        if key not in ("about_rendered", "about_raw"):
            assert data[key] == value, key
    assert data["username"] == "kaykay"
    assert data["reputation"] == 12345
    assert data["badge_counts"]["total"] == 17


async def test_get_user_html_bearing_about_rendered_becomes_plain_text() -> None:
    # A messier about_rendered (block tags, entities, imageBox div with an
    # onclick img) must come back as clean plain text with no tags or
    # entities, and the wiki-markup about_raw must not survive.
    payload = _user_profile_payload()
    payload["about_rendered"] = (
        "<div class=\"imageBox imageBox_center\"><img src=\"https://example.com/x.jpg\" "
        'width="100" alt="Image" onclick="window.open(\'/x.jpg\')" /></div>'
        "<p>Hello<br>world &amp; friends</p><div><li>one</li><li>two</li></div>"
    )
    payload["about_raw"] = "[image|12345|size=medium]"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    ac = _client_with(handler)
    data = await ac.get_user(1)

    assert data["about_text"] == "Hello\nworld & friends\none\ntwo"
    assert "<" not in data["about_text"]
    assert "about_rendered" not in data
    assert "about_raw" not in data
    assert data["badge_counts"] == payload["badge_counts"]
    assert data["reputation"] == 12345
    assert data["teams"] == ["Masters"]


@pytest.mark.parametrize(
    "bad", [0, -5, 1.5, "abc", "12x", "-3", "", "  ", None, True, "²"]
)
async def test_get_user_rejects_invalid_user_id(bad) -> None:
    ac = _client_with(lambda r: httpx.Response(200, json=_user_profile_payload()))
    with pytest.raises(ValueError, match="user_id"):
        await ac.get_user(bad)


async def test_get_user_404_raises_user_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "User not found"})

    ac = _client_with(handler)
    with pytest.raises(ValueError, match="User not found: 999999"):
        await ac.get_user(999999)


async def test_get_user_cached_ttl_30min() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json=_user_profile_payload())

    ac = _client_with(handler)
    await ac.get_user(1)
    await ac.get_user(1)
    assert call_count == 1  # TTL_30MIN cache served the second call


# ---------------------------------------------------------------------------
# list_user_guides() tests
# ---------------------------------------------------------------------------


async def test_list_user_guides_returns_guide_summaries() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["params"] = request.url.params
        return httpx.Response(200, json=[_guide_summary(1), _guide_summary(2)])

    ac = _client_with(handler)
    data = await ac.list_user_guides(1)

    assert "/users/1/guides" in seen["url"]
    assert seen["params"]["offset"] == "0"
    assert seen["params"]["limit"] == "20"
    assert isinstance(data, list)
    assert len(data) == 2
    assert data[0]["guideid"] == 1  # summaries passed through as-is


async def test_list_user_guides_passes_offset_limit_params() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = request.url.params
        return httpx.Response(200, json=[])

    ac = _client_with(handler)
    await ac.list_user_guides(1, offset=40, limit=50)
    assert seen["params"]["offset"] == "40"
    assert seen["params"]["limit"] == "50"


@pytest.mark.parametrize("bad", [0, -1, 201, 20.5, True, None])
async def test_list_user_guides_rejects_invalid_limit(bad) -> None:
    ac = _client_with(lambda r: httpx.Response(200, json=[]))
    with pytest.raises(ValueError, match="limit"):
        await ac.list_user_guides(1, limit=bad)


async def test_list_user_guides_accepts_numeric_string_limit() -> None:
    # QA Round 6 (F1): the get_user tool str-types limit, so the client
    # must accept numeric strings ("5") and convert them like ints.
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = request.url.params
        return httpx.Response(200, json=[])

    ac = _client_with(handler)
    await ac.list_user_guides(1, limit="5")
    assert seen["params"]["limit"] == "5"


@pytest.mark.parametrize("bad", [-1, -100, 1.5, "0", True, None])
async def test_list_user_guides_rejects_invalid_offset(bad) -> None:
    ac = _client_with(lambda r: httpx.Response(200, json=[]))
    with pytest.raises(ValueError, match="offset"):
        await ac.list_user_guides(1, offset=bad)


@pytest.mark.parametrize("bad", [0, "abc", True, "²"])
async def test_list_user_guides_rejects_invalid_user_id(bad) -> None:
    ac = _client_with(lambda r: httpx.Response(200, json=[]))
    with pytest.raises(ValueError, match="user_id"):
        await ac.list_user_guides(bad)


async def test_list_user_guides_404_raises_user_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "User not found"})

    ac = _client_with(handler)
    with pytest.raises(ValueError, match="User not found: 999999"):
        await ac.list_user_guides(999999)


async def test_list_user_guides_not_cached() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json=[_guide_summary(call_count)])

    ac = _client_with(handler)
    await ac.list_user_guides(1)
    await ac.list_user_guides(1)
    assert call_count == 2  # paginated list endpoint is never cached
    assert len(ac._cache) == 0
