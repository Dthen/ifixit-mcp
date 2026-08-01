"""FastMCP server for iFixit — repair guides, device info, repairability scores.

Read-only, anonymous access to the iFixit API 2.0: keyword search, repair
guides (summary or full detail), the category tree, device wiki pages,
maintenance schedules, media CDN URLs, and contributor profiles.

IMPORT SIDE EFFECT (QA Round 13, R13-3): importing this module (even
just ``import ifixit_mcp.server``) pins the PROCESS-WIDE ROOT logger to
ERROR and configures logging (``FastMCP(log_level="ERROR")`` runs
``configure_logging()``/``basicConfig`` at module top) — an embedding
host silently loses root-level INFO/WARNING log records. Embedding hosts
should import this module only to run the server (call ``server.main()``).

Attribution: Data from iFixit (CC BY-NC-SA). Non-commercial use only.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import signal
from typing import Annotated, Any, TypeVar

# ExceptionGroup is a builtin on Python 3.11+; on 3.10 it lives in the
# exceptiongroup backport (a transitive dependency of anyio there, since
# mcp depends on anyio). main() uses it to recognize the anyio disconnect
# group, so the name must resolve on every supported interpreter — the
# import either provides it (3.10) or the except branch rebinds the
# builtin (3.11+) (QA Round 7, F3).
try:
    from exceptiongroup import ExceptionGroup  # Python 3.10 backport
except ImportError:  # pragma: no cover - 3.11+ has it built in
    ExceptionGroup = ExceptionGroup  # noqa: F821 - resolve the builtin

# The HTTP client is created lazily on first tool call (importing the module
# must not open sockets or register heavyweight resources) and closed at
# interpreter exit via atexit.
_client: IfixitClient | None = None


def get_client() -> IfixitClient:
    """Return the shared IfixitClient, creating it on first use."""
    global _client
    if _client is None:
        _client = IfixitClient()
        atexit.register(_close_client)
    return _client


def _close_client() -> None:
    """Close the shared client at interpreter exit (atexit handler).

    The client is async, so the close runs in a throwaway event loop. All
    failures are swallowed: at exit there is nothing left to report to.
    """
    client = _client
    if client is None:
        return
    try:
        asyncio.run(client.aclose())
    except Exception:  # pragma: no cover - interpreter shutdown edge cases
        pass


def _signal_exit(signum: int, frame: object) -> None:
    """Close the client and exit immediately (os._exit avoids the anyio portal thread shutdown deadlock).

    The stdio transport runs the blocking stdin readline in an anyio
    worker thread (wrap_file -> to_thread), so raising SystemExit here
    would deadlock interpreter shutdown: threading._shutdown waits
    forever for the thread parked in readline(). Instead the handler
    closes the client and calls os._exit(0) — interpreter shutdown is
    skipped entirely, so the parked worker thread never matters.

    The signal handler runs on the main thread, where the asyncio loop
    is live (anyio's asyncio backend), so the client cannot be closed
    with a fresh asyncio.run() here. The close is scheduled onto the
    running loop via call_soon_threadsafe (which also writes the
    self-pipe, waking the selector the signal interrupted); the task
    awaits client.aclose() and then exits. Before the loop is up (or
    outside an event loop) the synchronous _close_client() path is used.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No event loop running: close synchronously and exit.
        _close_client()
        os._exit(0)
        return
    loop.call_soon_threadsafe(_schedule_close_then_exit)


def _schedule_close_then_exit() -> None:
    """Loop callback (from _signal_exit): close the client, then exit."""
    client = _client
    if client is None:
        os._exit(0)
        return
    asyncio.ensure_future(_close_then_exit(client))


async def _close_then_exit(client: IfixitClient) -> None:
    """Await the client close on the running loop, then exit immediately."""
    try:
        await client.aclose()
    except Exception:  # pragma: no cover - close is best-effort
        pass
    os._exit(0)


# QA Round 8 (R8-1): install the signal handlers at MODULE TOP, before the
# heavy mcp/httpx imports below — importing mcp.server.fastmcp takes ~1.2s,
# and a SIGTERM/SIGINT in that window used to die with the default
# disposition (exit -15) because the handlers were only installed inside
# main(). By the time this runs, everything the handler needs (_close_client,
# os, _client) is already defined. Guarded on __main__ so importing the
# module as a library does not hijack the importing process's signals.
if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _signal_exit)
    signal.signal(signal.SIGINT, _signal_exit)

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import BeforeValidator

from ifixit_mcp import __version__
from ifixit_mcp.client import (
    GUIDES_MAX_LIMIT,
    IfixitClient,
    _brief,
    _validate_positive_int,
)

# The mcp library enables INFO logging on httpx/httpcore (writing every
# request URL, including /suggest/<query> search terms, to stderr) and
# logs its own per-request INFO lines ("Processing request of type
# CallToolRequest"). Silence those loggers so search queries and request
# chatter stay out of server logs (QA Round 7, F9).
for _logger_name in (
    "httpx",
    "httpcore",
    "mcp",
    "mcp.server",
    "mcp.server.lowlevel",
    "mcp.server.session",
):
    logging.getLogger(_logger_name).setLevel(logging.WARNING)

# QA Round 12 (F12-2): mcp's session layer emits its "Failed to validate
# request/notification" warnings via module-level logging.warning() — the
# ROOT logger — bypassing the mcp.* silencing above; a notification that
# fails validation dumps an ~8KB pydantic error on stderr per session.
# Pin the root logger to ERROR so those dumps never reach stderr
# (ERROR-level records, e.g. from the mcp session, still surface).
logging.getLogger().setLevel(logging.ERROR)

mcp = FastMCP(
    "ifixit",
    instructions=(
        "Search iFixit's repair-guide catalog, read step-by-step guides, "
        "browse the device category tree, look up device repairability "
        "scores and maintenance schedules, resolve media CDN URLs, and "
        "fetch contributor profiles. All operations are read-only and "
        "anonymous. "
        "Data from iFixit (CC BY-NC-SA). Non-commercial use only."
    ),
    # QA Round 12 (F12-2): mcp.run() calls configure_logging(log_level),
    # whose logging.basicConfig(level=...) RESETS the root logger level —
    # wiping the ERROR pin above. Pass "ERROR" so the run path keeps the
    # root logger at ERROR and validation-failure pydantic dumps never
    # reach stderr.
    log_level="ERROR",
)

# Pin serverInfo.version to the package version instead of the mcp library's
# own version. (FastMCP in the supported mcp>=1.0 range does not accept a
# version= kwarg; set it on the underlying server directly.) FastMCP's
# internals may change across mcp releases, so skip the pin gracefully if
# the attribute disappears rather than hard-crashing at import time.
try:
    mcp._mcp_server.version = __version__
except AttributeError:  # pragma: no cover - future mcp internals
    pass


# ---------------------------------------------------------------------------
# Compact projections
# ---------------------------------------------------------------------------


def _coerce_string(value: Any) -> str:
    """Coerce a tool-layer string argument to its string form.

    QA Round 5 (R5-3) / Round 6 (F1, F3): FastMCP validates JSON arguments
    with pydantic BEFORE the tool body runs, and pydantic 2.x does not
    coerce numbers to strings (``str`` annotations reject ints/floats with
    a raw ValidationError dump) while lax ``int`` annotations turn JSON
    ``true`` into ``1`` — so {"guideid": true} used to silently fetch
    real guide 1. This validator performs the coercion explicitly and
    totalizes the field:

    - None -> "" (the omission sentinel). Required params then fail the
      client's non-empty check with the clean family message before any
      network IO; optional params treat "" as omitted. JSON null must
      NEVER become the literal string "None" (QA Round 7, F2) — that used
      to make search_guides({"query": null}) run a real search for the
      word "None".
    - bool -> "True"/"False" (the client validator rejects it with the
      clean family message)
    - int -> "1220" (accepted, as before)
    - float -> "1.5" (rejected by the client validator, as before)
    - str -> stripped of leading/trailing whitespace (QA Round 9, R9-3:
      whitespace-padded values must never reach the client — the API
      silently ignores them, and a future API change could silently
      change behavior; valid values never contain edge whitespace)
    - anything else (list/dict) -> repr, which the client validator
      rejects with the clean family message instead of a raw pydantic
      ValidationError dump leaking to the LLM (R5-4, F3).
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value.strip()
    return repr(value)


# QA Round 11 (F11-4): _empty_to_none/_empty_to_default used to declare
# -> str / -> str | None while passing non-string values (direct calls
# like limit=5) through untouched — the annotations lied. A TypeVar keeps
# the annotation honest: T is inferred from the argument, and the return
# is T | None (resp. T | str) because the value either passes through
# unchanged or becomes the string default/None.
T = TypeVar("T")


def _empty_to_none(value: T) -> T | None:
    """Map the optional-param omission sentinel to None; strip whitespace.

    QA Round 7 (F2/F4): optional params are exactly-str annotated with ""
    as the default; JSON null and "" both arrive as "". Converting back
    to None here restores the client's native "param omitted" contract.

    QA Round 9 (R9-3): a whitespace-only value is the same as omitted —
    the iFixit API silently ignores whitespace-only langid/guideDevice
    params, and a future API change could silently switch languages or
    devices. Non-empty values are stripped before reaching the client, so
    lang=" de " sends langid=de. Non-string values (direct calls, e.g.
    max_steps=3) pass through untouched.
    """
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if stripped == "":
            return None
        return stripped
    return value


def _empty_to_default(value: T, default: str) -> T | str:
    """Map the optional-with-default omission sentinel to the declared default.

    QA Round 10 (R10-1): detail/media_type/doctypes/limit are optional
    params WITH defaults, so JSON null / whitespace-only means "omitted"
    — fall back to the declared default ("summary", "images", "guide",
    "20") BEFORE validation instead of hard-erroring (get_guide{detail:
    null} used to raise "detail must be 'summary' or 'full'", and
    get_user{limit: null, include_guides: true} used to raise "limit
    must be a positive integer, got ''"). This mirrors _empty_to_none's
    omission semantics for the optional params without defaults
    (lang/device/max_steps/path -> None). Non-empty values are stripped
    before reaching the client (already stripped by _coerce_string on
    the wire path; direct calls need it here too). Non-string values
    (direct calls, e.g. limit=5) pass through untouched.
    """
    if value is None:
        return default
    if isinstance(value, str):
        stripped = value.strip()
        if stripped == "":
            return default
        return stripped
    return value


def _parse_include_guides(value: Any) -> bool:
    """Parse the include_guides tool param into a bool.

    QA Round 7 (F1): include_guides was the one bare-bool param, so JSON
    2 leaked a raw pydantic bool_parsing dump (pydantic.dev link) through
    the real server. It is string-typed like every other param; accepted
    spellings are true/false/1/0 (case-insensitive; JSON booleans and
    numbers arrive here as "True"/"1"/... via _coerce_string). Anything
    else raises the clean family ValueError. "" (JSON null or omitted)
    and None (direct calls) mean False.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if not isinstance(value, str):
        value = repr(value)
    normalized = value.strip().lower()
    if normalized in ("true", "1"):
        return True
    if normalized in ("false", "0", ""):
        return False
    raise ValueError(f"include_guides must be a boolean, got {_brief(value)}")


# Tool-layer string parameter type: string-typed in the tool schema, with
# the coercion above guaranteeing JSON numbers keep working and JSON
# booleans (and other junk) reach the client validator as plain strings.
# Every tool param is annotated as exactly ``str`` (no unions): FastMCP's
# pre_parse_json runs json.loads() on string values whose annotation is
# not exactly ``str``, and a 5000-digit string then raises the Python
# 3.11 int-digit-limit ValueError, bypassing the clean family messages
# (QA Round 7, F4). Optional params use "" as the omitted sentinel.
StrParam = Annotated[str, BeforeValidator(_coerce_string)]
OptStrParam = StrParam
# Tool-layer id parameter type (ids are strings at the schema level too).
IdParam = StrParam


def _project_search_result(item: Any) -> Any:
    """Project a guide-type search result to its compact shape.

    Guide results (discriminator ``dataType == "guide"``) are reduced to
    {guideid, title, url, type, difficulty, summary} with ``summary``
    truncated to 200 characters; absent optional keys are omitted. All
    other result types (wiki, question, product, page) pass through
    unchanged — the API's own objects are already small.
    """
    if not isinstance(item, dict) or item.get("dataType") != "guide":
        return item
    compact: dict[str, Any] = {}
    for key in ("guideid", "title", "url", "type", "difficulty"):
        value = item.get(key)
        if value is not None:
            compact[key] = value
    summary = item.get("summary")
    if isinstance(summary, str) and summary:
        compact["summary"] = summary[:200]
    return compact


def _project_guide_item(item: Any) -> dict[str, Any]:
    """Project a guide entry to {guideid, title, url, difficulty, ...}.

    Reduces each guide entry to {guideid, title, url, difficulty,
    time_required_max, image_thumbnail}; optional keys are omitted when
    absent or null — never fabricated.
    """
    compact: dict[str, Any] = {}
    for key in ("guideid", "title", "url", "difficulty", "time_required_max"):
        value = item.get(key) if isinstance(item, dict) else None
        if value is not None:
            compact[key] = value
    if isinstance(item, dict):
        thumbnail = item.get("image_thumbnail")
        if thumbnail is None:
            image = item.get("image")
            if isinstance(image, dict):
                thumbnail = image.get("thumbnail")
        if thumbnail is not None:
            compact["image_thumbnail"] = thumbnail
    return compact


def _project_user_guide(item: Any) -> dict[str, Any]:
    """Project a user's guide entry to {guideid, title, url} only.

    The caller (get_user) filters out non-dict entries before calling —
    a malformed /users/{id}/guides item is SKIPPED, never fabricated
    into {} (QA Round 12, F12-4).
    """
    compact: dict[str, Any] = {}
    for key in ("guideid", "title", "url"):
        value = item.get(key) if isinstance(item, dict) else None
        if value is not None:
            compact[key] = value
    return compact


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def search_guides(
    query: StrParam,
    device: OptStrParam = "",
    doctypes: StrParam = "guide",
    lang: OptStrParam = "",
) -> dict | str:
    """Search iFixit's catalog for repair guides (and other content types).

    Returns up to 10 results as {"query": ..., "results": [...]}. Guide
    results are compacted to {guideid, title, url, type, difficulty,
    summary} with summary truncated to 200 characters; other result types
    (wiki, question, product) pass through as returned by the API. Use the
    device parameter to scope results to a specific device (e.g.
    "iPhone 13"). Search results are volatile and never cached.

    Args:
        query: Free-text search query (e.g. "battery replacement").
        device: Restrict results to a single device by name (maps to the
            API's guideDevice parameter).
        doctypes: Comma-separated result types: guide, item, topic,
            device, category, question, post, or all (default "guide").
        lang: Optional language code (e.g. "de") for localized results
            (maps to the API's langid parameter).

    Note: Data from iFixit (CC BY-NC-SA). Non-commercial use only.
    """
    try:
        client = get_client()
        data = await client.search(
            query,
            doctypes=_empty_to_default(doctypes, "guide"),
            device=_empty_to_none(device),
            lang=_empty_to_none(lang),
        )
        results = [_project_search_result(item) for item in data.get("results", [])]
    except asyncio.CancelledError:
        # QA Round 3: a cancelled request must not let CancelledError
        # escape the tool (FastMCP's runtime catches only Exception, so a
        # bare CancelledError corrupts JSON-RPC handling). The client has
        # already cleaned its in-flight markers by this point (shielded
        # fetches keep running in the background).
        return "Request cancelled"
    except (
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        RecursionError,
        httpx.HTTPError,
        httpx.InvalidURL,
    ) as e:
        # QA Round 6 (F4): errors RAISE so MCP clients get proper
        # isError: true results ("Error executing tool <name>: ..." on the
        # wire, converted by FastMCP) instead of isError: false strings.
        raise ValueError(f"Search failed: {e}") from e
    return {"query": data.get("query", query), "results": results}


@mcp.tool()
async def get_guide(
    guideid: IdParam,
    detail: StrParam = "summary",
    max_steps: OptStrParam = "",
    lang: OptStrParam = "",
) -> dict | str:
    """Fetch a repair guide by id.

    With detail="summary" (default) returns the guide's metadata, parts
    and tools lists, and step titles only — a compact overview (steps with
    empty titles fall back to the first line's text). With detail="full"
    returns everything (steps with rendered text converted to plain text).
    max_steps truncates the steps list; note it only applies when
    detail="full" — in summary mode the client ignores it, so the
    combination is accepted but has no effect.

    WARNING: detail="full" guides can exceed 250KB of text — prefer
    detail="summary" or set max_steps for large guides.

    Args:
        guideid: The guide id (e.g. 1220 or "1220").
        detail: One of "summary" (default) or "full".
        max_steps: Maximum number of steps to include when detail="full"
            (e.g. 10 or "10").
        lang: Optional language code (e.g. "de") for a localized guide
            (maps to the API's langid parameter).

    Note: Data from iFixit (CC BY-NC-SA). Non-commercial use only.
    """
    try:
        client = get_client()
        return await client.get_guide(
            guideid,
            detail=_empty_to_default(detail, "summary"),
            lang=_empty_to_none(lang),
            max_steps=_empty_to_none(max_steps),
        )
    except asyncio.CancelledError:
        return "Request cancelled"
    except (
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        RecursionError,
        httpx.HTTPError,
        httpx.InvalidURL,
    ) as e:
        raise ValueError(f"Guide lookup failed: {e}") from e


@mcp.tool()
async def browse_categories(path: OptStrParam = "") -> list | dict | str:
    """Browse iFixit's device category tree.

    With no path, returns the ~16 top-level category names (e.g. Mac,
    Phone, Game Console). With a path ("Mac" or "Mac/Mac Laptop"),
    navigates the nested tree and returns that subtree's child category
    names. A leaf or empty subtree yields an empty list. Only category
    names are returned — the raw tree is ~1.5MB and never leaves the
    client.

    Args:
        path: Optional slash-separated category path to descend into
            (e.g. "Mac" or "Mac/Mac Laptop"); an empty string means the
            top level.

    Note: Data from iFixit (CC BY-NC-SA). Non-commercial use only.
    """
    try:
        client = get_client()
        return await client.get_categories(_empty_to_none(path))
    except asyncio.CancelledError:
        return "Request cancelled"
    except (
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        RecursionError,
        httpx.HTTPError,
        httpx.InvalidURL,
    ) as e:
        raise ValueError(f"Category lookup failed: {e}") from e


@mcp.tool()
async def get_device(title: StrParam) -> dict | str:
    """Get a compact overview of a device wiki page.

    Returns {title, display_title, repairability_score (when iFixit has
    published one), summary (first 500 chars), featured_guides
    (title/guideid/url only), children (names only), parts_count,
    tools_count, ancestors (breadcrumb names)}. The raw wiki page can be
    ~238KB; this projection keeps the response small for context budgets.

    Args:
        title: The device title (e.g. "iPhone").

    Note: Data from iFixit (CC BY-NC-SA). Non-commercial use only.
    """
    try:
        client = get_client()
        return await client.get_device(title)
    except asyncio.CancelledError:
        return "Request cancelled"
    except (
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        RecursionError,
        httpx.HTTPError,
        httpx.InvalidURL,
    ) as e:
        raise ValueError(f"Device lookup failed: {e}") from e


@mcp.tool()
async def list_device_guides(title: StrParam) -> list[dict] | str:
    """List the repair guides available for a device.

    Returns the device's guides (and featured guides) as a compact list —
    each entry is projected to {guideid, title, url, difficulty,
    time_required_max, image_thumbnail}, with optional fields omitted when
    the API did not provide them.

    Args:
        title: The device title (e.g. "iPhone").

    Note: Data from iFixit (CC BY-NC-SA). Non-commercial use only.
    """
    try:
        client = get_client()
        guides = await client.list_device_guides(title)
        return [_project_guide_item(item) for item in guides]
    except asyncio.CancelledError:
        return "Request cancelled"
    except (
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        RecursionError,
        httpx.HTTPError,
        httpx.InvalidURL,
    ) as e:
        raise ValueError(f"Guide listing failed: {e}") from e


@mcp.tool()
async def get_maintenance_schedule(title: StrParam) -> dict | str:
    """Get a device's maintenance schedule.

    Returns {"schedules": [...]} where each schedule describes a
    maintenance task and its trigger (e.g. battery_health_percent). When
    the device inherits its schedule from a parent device, the dict also
    carries "inherited_from" (the parent's name). A device with no
    maintenance schedule yields {"schedules": []} — that is the API's
    normal "no schedule" signal (HTTP 200), not an error.

    Args:
        title: The device title (e.g. "iPhone").

    Note: Data from iFixit (CC BY-NC-SA). Non-commercial use only.
    """
    try:
        client = get_client()
        return await client.get_maintenance_schedule(title)
    except asyncio.CancelledError:
        return "Request cancelled"
    except (
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        RecursionError,
        httpx.HTTPError,
        httpx.InvalidURL,
    ) as e:
        raise ValueError(f"Maintenance schedule lookup failed: {e}") from e


@mcp.tool()
async def get_media(
    media_id: IdParam,
    media_type: StrParam = "images",
) -> dict | str:
    """Resolve a media object's CDN URLs by id.

    Returns the API's image/media object with CDN size URLs (mini,
    thumbnail, standard, original, ...); ungenerated sizes are absent.
    Media access is permission-checked: assets not referenced by content
    you can view return an error.

    Args:
        media_id: The media id (e.g. 14056 or "14056").
        media_type: One of "images" (default), "videos", or "documents".

    Note: Data from iFixit (CC BY-NC-SA). Non-commercial use only.
    """
    try:
        client = get_client()
        return await client.get_media(
            media_id, media_type=_empty_to_default(media_type, "images")
        )
    except asyncio.CancelledError:
        return "Request cancelled"
    except (
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        RecursionError,
        httpx.HTTPError,
        httpx.InvalidURL,
    ) as e:
        raise ValueError(f"Media lookup failed: {e}") from e


@mcp.tool()
async def get_user(
    user_id: IdParam,
    include_guides: StrParam = "false",
    limit: StrParam = "20",
) -> dict | str:
    """Fetch an iFixit contributor profile.

    Returns the user's profile dict (username, reputation, join_date,
    badge_counts, ...). With include_guides=True, merges the profile with
    the user's guide list into {"user": ..., "guides": [...]} where each
    guide is projected to {guideid, title, url}.

    include_guides is all-or-nothing: if either the profile fetch or the
    guide-list fetch fails (ValueError, network error, or malformed
    response), the whole call raises a single clean error — partial data
    is never returned.

    Args:
        user_id: The user id (e.g. 1 or "1").
        include_guides: When True, also fetch and include the user's
            guides (projected to guideid/title/url). Accepts true/false
            (also as JSON booleans) or 1/0; any other value is rejected
            with a clean error.
        limit: Maximum number of guides to include when
            include_guides=True (1-200, default 20; e.g. 5 or "5"). The
            API clamps at 200. Validated unconditionally — junk is
            rejected even when include_guides=False leaves it unused
            (QA Round 12, F12-7).

    Note: Data from iFixit (CC BY-NC-SA). Non-commercial use only.
    """
    try:
        client = get_client()
        # Parse (and reject junk) BEFORE any network IO: a bad
        # include_guides must never trigger a profile fetch.
        want_guides = _parse_include_guides(include_guides)
        # QA Round 12 (F12-7): validate limit unconditionally — junk must
        # be rejected even when include_guides=false leaves the value
        # unused (get_guide validates max_steps even in summary mode).
        # The checks mirror client.list_user_guides exactly, so the
        # include_guides=true path errors identically; the client
        # re-validates harmlessly when the value is used.
        limit_value = _empty_to_default(limit, "20")
        parsed_limit = _validate_positive_int("limit", limit_value)
        if parsed_limit > GUIDES_MAX_LIMIT:
            raise ValueError("limit must be an integer between 1 and 200")
        user = await client.get_user(user_id)
        if not want_guides:
            return user
        guides = await client.list_user_guides(user_id, limit=limit_value)
        return {
            "user": user,
            # QA Round 12 (F12-4): skip malformed (non-dict) entries
            # rather than projecting them into fabricated {} dicts —
            # list_device_guides' defensive posture.
            "guides": [
                _project_user_guide(item)
                for item in guides
                if isinstance(item, dict)
            ],
        }
    except asyncio.CancelledError:
        return "Request cancelled"
    except (
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        RecursionError,
        httpx.HTTPError,
        httpx.InvalidURL,
    ) as e:
        raise ValueError(f"User lookup failed: {e}") from e


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _is_disconnect_error(exc: BaseException) -> bool:
    """True for the errors a mid-flight stdio disconnect raises.

    anyio is a transitive dependency of the mcp library, but import it
    lazily so importing this module never hard-depends on its internals.
    UnicodeDecodeError is included (QA Round 15, F15-2): a non-UTF-8
    byte on stdin is equally a client-side wire problem — garbage bytes
    must shut the server down as cleanly as a hang-up, not crash it.
    """
    try:
        import anyio
    except ImportError:  # pragma: no cover - anyio ships with mcp
        return False
    return isinstance(
        exc,
        (
            anyio.ClosedResourceError,
            anyio.BrokenResourceError,
            anyio.EndOfStream,
            UnicodeDecodeError,
        ),
    )


def _group_is_disconnect(eg: ExceptionGroup) -> bool:
    """True when EVERY exception in the group is a disconnect-family error."""
    for exc in eg.exceptions:
        if isinstance(exc, ExceptionGroup):
            if not _group_is_disconnect(exc):
                return False
        elif not _is_disconnect_error(exc):
            return False
    return True


def main() -> None:
    """Entry point for running the server over stdio.

    The signal handlers were already installed at module top when this
    module runs as __main__ (QA Round 8, R8-1); re-installing here is
    idempotent and covers entry paths that import the module instead
    (e.g. the console-script entry point, where __name__ != "__main__").
    """
    signal.signal(signal.SIGTERM, _signal_exit)
    signal.signal(signal.SIGINT, _signal_exit)
    try:
        mcp.run()
    except ExceptionGroup as eg:
        # QA Round 6 (F5): a client hanging up mid-request (closing stdin
        # while calls are in flight) used to crash the server with an ~7KB
        # anyio ExceptionGroup traceback (exit 1). A disconnect is a
        # normal exit condition: when every contained exception is a
        # closed-resource / end-of-stream error, exit 0 silently.
        if _group_is_disconnect(eg):
            raise SystemExit(0)
        raise
    except UnicodeDecodeError:
        # QA Round 15 (F15-2): a non-UTF-8 byte on stdin used to crash
        # the server with a ~4.5KB traceback (exit 1). Garbage bytes on
        # the wire are a client-side problem, like a disconnect — exit 0
        # silently. (Usually arrives wrapped in an ExceptionGroup from
        # the stdin reader task, handled above via _group_is_disconnect;
        # this bare branch covers an unwrapped escape.)
        raise SystemExit(0)
    finally:
        # Runs on normal exit AND on SystemExit from the signal handler
        # (the loop has unwound by now, so asyncio.run is safe). The
        # atexit-registered close remains as a backstop; aclose() is
        # idempotent.
        _close_client()


if __name__ == "__main__":
    main()
