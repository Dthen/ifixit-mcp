"""Tests for MCP tool functions in the server module."""

from __future__ import annotations

import asyncio

import httpx
import pytest

import ifixit_mcp.server as server_module
from conftest import make_mock_client, json_response


# ---------------------------------------------------------------------------
# search_guides
# ---------------------------------------------------------------------------


class TestSearchGuidesTool:
    async def test_returns_compact_guide_results(self, client):
        client.results["search"] = {
            "query": "battery",
            "results": [
                {
                    "dataType": "guide",
                    "guideid": 123,
                    "title": "Battery Replacement",
                    "url": "https://www.ifixit.com/Guide/123",
                    "type": "replacement",
                    "difficulty": "Moderate",
                    "summary": "A" * 300,
                    "revisionid": 99,
                    "image": {"thumbnail": "t.jpg"},
                },
                {"dataType": "wiki", "wikiid": 7, "title": "Battery"},
            ],
        }
        result = await server_module.search_guides("battery")
        assert isinstance(result, dict)
        results = result["results"]
        assert results[0] == {
            "guideid": 123,
            "title": "Battery Replacement",
            "url": "https://www.ifixit.com/Guide/123",
            "type": "replacement",
            "difficulty": "Moderate",
            "summary": "A" * 200,
        }
        # Non-guide results pass through unchanged.
        assert results[1] == {"dataType": "wiki", "wikiid": 7, "title": "Battery"}

    async def test_omits_absent_summary_and_difficulty(self, client):
        client.results["search"] = {
            "query": "battery",
            "results": [
                {"dataType": "guide", "guideid": 1, "title": "T", "url": "u"}
            ],
        }
        result = await server_module.search_guides("battery")
        assert result["results"] == [{"guideid": 1, "title": "T", "url": "u"}]

    async def test_device_and_doctypes_passed_to_client(self, client):
        client.results["search"] = {"query": "battery", "results": []}
        await server_module.search_guides(
            "battery", device="iPhone 13", doctypes="guide,wiki"
        )
        name, args, kwargs = client.calls[-1]
        assert name == "search"
        assert kwargs["device"] == "iPhone 13"
        assert kwargs["doctypes"] == "guide,wiki"

    async def test_lang_passed_to_client(self, client):
        client.results["search"] = {"query": "battery", "results": []}
        await server_module.search_guides("battery", lang="de")
        name, args, kwargs = client.calls[-1]
        assert name == "search"
        assert kwargs["lang"] == "de"

    async def test_lang_defaults_to_none(self, client):
        client.results["search"] = {"query": "battery", "results": []}
        await server_module.search_guides("battery")
        name, args, kwargs = client.calls[-1]
        assert kwargs["lang"] is None

    async def test_empty_query_raises_error(self, monkeypatch):
        monkeypatch.setattr(
            server_module, "_client", make_mock_client(lambda r: json_response({}))
        )
        with pytest.raises(ValueError, match="Search failed:"):
            await server_module.search_guides("")

    async def test_http_error_raises_error(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="Internal Server Error")

        monkeypatch.setattr(server_module, "_client", make_mock_client(handler))
        with pytest.raises(ValueError, match="Search failed:"):
            await server_module.search_guides("battery")

    async def test_malformed_client_result_raises_error(self, client):
        # If the client ever returned a non-dict, the projection must be
        # caught by the tool's error handling, not escape to the runtime.
        client.results["search"] = [1, 2, 3]
        with pytest.raises(ValueError, match="Search failed:"):
            await server_module.search_guides("battery")


# ---------------------------------------------------------------------------
# get_guide
# ---------------------------------------------------------------------------


class TestGetGuideTool:
    async def test_returns_summary_by_default(self, client):
        client.results["get_guide"] = {
            "guideid": 1220,
            "title": "Battery Replacement",
            "difficulty": "Moderate",
            "steps": [{"stepid": 1, "title": "Open the phone"}],
        }
        result = await server_module.get_guide(1220)
        assert isinstance(result, dict)
        assert result["guideid"] == 1220
        assert result["title"] == "Battery Replacement"

    async def test_detail_and_max_steps_passed_to_client(self, client):
        await server_module.get_guide(1220, detail="full", max_steps=3)
        name, args, kwargs = client.calls[-1]
        assert name == "get_guide"
        assert kwargs["detail"] == "full"
        assert kwargs["max_steps"] == 3

    async def test_lang_passed_to_client(self, client):
        await server_module.get_guide(1220, lang="de")
        name, args, kwargs = client.calls[-1]
        assert name == "get_guide"
        assert kwargs["lang"] == "de"

    async def test_lang_defaults_to_none(self, client):
        await server_module.get_guide(1220)
        name, args, kwargs = client.calls[-1]
        assert kwargs["lang"] is None

    async def test_summary_with_max_steps_is_allowed(self, client):
        # max_steps is ignored by the client in summary mode, not rejected.
        result = await server_module.get_guide(1220, detail="summary", max_steps=5)
        assert isinstance(result, dict)

    async def test_not_found_raises_error(self, client):
        client.errors["get_guide"] = ValueError("Guide not found: 999")
        with pytest.raises(ValueError, match="Guide lookup failed: Guide not found: 999"):
            await server_module.get_guide(999)

    async def test_http_error_raises_error(self, client):
        client.errors["get_guide"] = httpx.ConnectError("boom")
        with pytest.raises(ValueError, match="Guide lookup failed:"):
            await server_module.get_guide(1220)

    async def test_invalid_url_raises_family_error(self, client):
        # QA Round 14 (F14-2): httpx.InvalidURL is NOT an httpx.HTTPError
        # (it subclasses Exception directly), so it used to escape the
        # except-family as a bare InvalidURL with no "Guide lookup failed:"
        # prefix. It must be converted to the family ValueError.
        client.errors["get_guide"] = httpx.InvalidURL(
            "URL component 'query' too long"
        )
        with pytest.raises(ValueError, match="Guide lookup failed:"):
            await server_module.get_guide(1220)


# ---------------------------------------------------------------------------
# browse_categories
# ---------------------------------------------------------------------------


class TestBrowseCategoriesTool:
    async def test_returns_top_level_list(self, client):
        client.results["get_categories"] = ["Mac", "Phone", "Tablet"]
        result = await server_module.browse_categories()
        assert result == ["Mac", "Phone", "Tablet"]

    async def test_path_passed_to_client(self, client):
        client.results["get_categories"] = ["iPhone", "iPad"]
        result = await server_module.browse_categories("Mac")
        assert result == ["iPhone", "iPad"]
        name, args, kwargs = client.calls[-1]
        assert name == "get_categories"
        assert args == ("Mac",)

    async def test_invalid_path_raises_error(self, client):
        client.errors["get_categories"] = ValueError("Category not found: Nope")
        with pytest.raises(ValueError, match="Category lookup failed:"):
            await server_module.browse_categories("Nope")

    async def test_http_error_raises_error(self, client):
        client.errors["get_categories"] = httpx.ConnectError("boom")
        with pytest.raises(ValueError, match="Category lookup failed:"):
            await server_module.browse_categories("Mac")


# ---------------------------------------------------------------------------
# get_device
# ---------------------------------------------------------------------------


class TestGetDeviceTool:
    async def test_returns_device_dict(self, client):
        client.results["get_device"] = {
            "title": "iPhone",
            "repairability_score": 7,
            "summary": "A sturdy phone.",
        }
        result = await server_module.get_device("iPhone")
        assert isinstance(result, dict)
        assert result["title"] == "iPhone"
        assert result["repairability_score"] == 7

    async def test_not_found_raises_error(self, client):
        client.errors["get_device"] = ValueError("Device not found: Nokia 3310")
        with pytest.raises(ValueError, match="Device lookup failed:"):
            await server_module.get_device("Nokia 3310")

    async def test_http_error_raises_error(self, client):
        client.errors["get_device"] = httpx.ConnectError("boom")
        with pytest.raises(ValueError, match="Device lookup failed:"):
            await server_module.get_device("iPhone")


# ---------------------------------------------------------------------------
# list_device_guides
# ---------------------------------------------------------------------------


class TestListDeviceGuidesTool:
    async def test_projects_compact_fields(self, client):
        client.results["list_device_guides"] = [
            {
                "guideid": 1,
                "title": "Battery Replacement",
                "url": "https://www.ifixit.com/Guide/1",
                "difficulty": "Easy",
                "time_required_max": 900,
                "image": {"thumbnail": "https://cdn.example/thumb.jpg"},
                "revisionid": 5,
                "flags": ["GUIDE_USER_CONTRIBUTED"],
            }
        ]
        result = await server_module.list_device_guides("iPhone")
        assert result == [
            {
                "guideid": 1,
                "title": "Battery Replacement",
                "url": "https://www.ifixit.com/Guide/1",
                "difficulty": "Easy",
                "time_required_max": 900,
                "image_thumbnail": "https://cdn.example/thumb.jpg",
            }
        ]

    async def test_omits_absent_optional_keys(self, client):
        client.results["list_device_guides"] = [
            {"guideid": 2, "title": "Screen Swap", "url": "https://www.ifixit.com/Guide/2"}
        ]
        result = await server_module.list_device_guides("iPhone")
        assert result == [
            {"guideid": 2, "title": "Screen Swap", "url": "https://www.ifixit.com/Guide/2"}
        ]

    async def test_not_found_raises_error(self, client):
        client.errors["list_device_guides"] = ValueError("Device not found: iPhone")
        with pytest.raises(ValueError, match="Guide listing failed:"):
            await server_module.list_device_guides("iPhone")

    async def test_http_error_raises_error(self, client):
        client.errors["list_device_guides"] = httpx.ConnectError("boom")
        with pytest.raises(ValueError, match="Guide listing failed:"):
            await server_module.list_device_guides("iPhone")

    async def test_malformed_client_result_raises_error(self, client):
        # A non-iterable client result must be caught by the tool's error
        # handling (projection runs inside the try), not escape to the
        # MCP runtime as an uncaught TypeError.
        client.results["list_device_guides"] = 42
        with pytest.raises(ValueError, match="Guide listing failed:"):
            await server_module.list_device_guides("iPhone")


# ---------------------------------------------------------------------------
# get_maintenance_schedule
# ---------------------------------------------------------------------------


class TestGetMaintenanceScheduleTool:
    async def test_returns_schedules(self, client):
        client.results["get_maintenance_schedule"] = {
            "schedules": [{"task": "Replace battery", "trigger": "battery_health_percent"}]
        }
        result = await server_module.get_maintenance_schedule("iPhone")
        assert isinstance(result, dict)
        assert result["schedules"][0]["task"] == "Replace battery"

    async def test_missing_schedule_raises_error(self, client):
        client.errors["get_maintenance_schedule"] = ValueError(
            "No maintenance schedule for: iPhone"
        )
        with pytest.raises(ValueError, match="Maintenance schedule lookup failed:"):
            await server_module.get_maintenance_schedule("iPhone")

    async def test_http_error_raises_error(self, client):
        client.errors["get_maintenance_schedule"] = httpx.ConnectError("boom")
        with pytest.raises(ValueError, match="Maintenance schedule lookup failed:"):
            await server_module.get_maintenance_schedule("iPhone")


# ---------------------------------------------------------------------------
# get_media
# ---------------------------------------------------------------------------


class TestGetMediaTool:
    async def test_returns_media_dict(self, client):
        client.results["get_media"] = {
            "image": {"id": 14056, "guid": "abc", "original": "https://cdn.example/o.jpg"}
        }
        result = await server_module.get_media(14056)
        assert isinstance(result, dict)
        assert result["image"]["id"] == 14056

    async def test_media_type_passed_to_client(self, client):
        await server_module.get_media(14056, media_type="videos")
        name, args, kwargs = client.calls[-1]
        assert name == "get_media"
        assert args == (14056,)
        assert kwargs["media_type"] == "videos"

    async def test_not_found_raises_error(self, client):
        client.errors["get_media"] = ValueError("Media not found: 999")
        with pytest.raises(ValueError, match="Media lookup failed:"):
            await server_module.get_media(999)

    async def test_http_error_raises_error(self, client):
        client.errors["get_media"] = httpx.ConnectError("boom")
        with pytest.raises(ValueError, match="Media lookup failed:"):
            await server_module.get_media(14056)


# ---------------------------------------------------------------------------
# get_user
# ---------------------------------------------------------------------------


class TestGetUserTool:
    async def test_returns_user_dict_by_default(self, client):
        client.results["get_user"] = {
            "userid": 1,
            "username": "kimbo",
            "reputation": 100,
        }
        result = await server_module.get_user(1)
        assert isinstance(result, dict)
        assert result["username"] == "kimbo"
        assert "guides" not in result

    async def test_include_guides_merges_user_and_projected_guides(self, client):
        client.results["get_user"] = {
            "userid": 1,
            "username": "kimbo",
            "reputation": 100,
        }
        client.results["list_user_guides"] = [
            {
                "guideid": 10,
                "title": "Guide One",
                "url": "https://www.ifixit.com/Guide/10",
                "difficulty": "Easy",
                "image": {"thumbnail": "t.jpg"},
            },
            {"guideid": 11, "title": "Guide Two", "url": "https://www.ifixit.com/Guide/11"},
        ]
        result = await server_module.get_user(1, include_guides=True)
        assert result == {
            "user": {"userid": 1, "username": "kimbo", "reputation": 100},
            "guides": [
                {"guideid": 10, "title": "Guide One", "url": "https://www.ifixit.com/Guide/10"},
                {"guideid": 11, "title": "Guide Two", "url": "https://www.ifixit.com/Guide/11"},
            ],
        }

    async def test_user_error_raises_error(self, client):
        client.errors["get_user"] = ValueError("User not found: 999")
        with pytest.raises(ValueError, match="User lookup failed:"):
            await server_module.get_user(999)

    async def test_guides_error_with_include_raises_error(self, client):
        client.errors["list_user_guides"] = ValueError("User not found: 999")
        with pytest.raises(ValueError, match="User lookup failed:"):
            await server_module.get_user(999, include_guides=True)

    async def test_limit_passed_to_client(self, client):
        await server_module.get_user(1, include_guides=True, limit=50)
        name, args, kwargs = client.calls[-1]
        assert name == "list_user_guides"
        assert kwargs["limit"] == 50

    async def test_limit_defaults_to_20(self, client):
        # The tool-layer default is the numeric string "20"; the client
        # converts it to the int 20.
        await server_module.get_user(1, include_guides=True)
        name, args, kwargs = client.calls[-1]
        assert name == "list_user_guides"
        assert kwargs["limit"] == "20"

    async def test_numeric_string_limit_passed_to_client(self, client):
        # QA Round 6 (F1): limit is str-typed at the tool layer; the client
        # converts numeric strings to ints (verified end-to-end in
        # TestStrTypedNonIdParams.test_call_tool_string_limit_limits_guides).
        await server_module.get_user(1, include_guides=True, limit="5")
        name, args, kwargs = client.calls[-1]
        assert name == "list_user_guides"
        assert kwargs["limit"] == "5"

    async def test_invalid_limit_raises_error(self, client):
        client.errors["list_user_guides"] = ValueError(
            "limit must be an integer between 1 and 200"
        )
        with pytest.raises(ValueError, match="User lookup failed:"):
            await server_module.get_user(1, include_guides=True, limit=999)

    async def test_guides_http_error_with_include_raises_error(self, client):
        # An httpx failure on the guides fetch must produce the same clean
        # all-or-nothing error as a ValueError — never partial data.
        client.errors["list_user_guides"] = httpx.ConnectError("boom")
        with pytest.raises(ValueError, match="User lookup failed:"):
            await server_module.get_user(999, include_guides=True)

    async def test_malformed_guides_result_raises_error(self, client):
        # A non-iterable guides result must be caught by the tool's error
        # handling (projection runs inside the try), not escape to the
        # MCP runtime as an uncaught TypeError.
        client.results["list_user_guides"] = 42
        with pytest.raises(ValueError, match="User lookup failed:"):
            await server_module.get_user(1, include_guides=True)

    async def test_non_dict_guide_items_are_skipped(self, client):
        # QA Round 12 (F12-4): malformed /users/{id}/guides entries must
        # be SKIPPED — never projected into fabricated {} dicts — matching
        # list_device_guides' defensive posture (no crash, no fake data).
        client.results["get_user"] = {
            "userid": 1,
            "username": "kimbo",
            "reputation": 100,
        }
        client.results["list_user_guides"] = [
            {
                "guideid": 10,
                "title": "Guide One",
                "url": "https://www.ifixit.com/Guide/10",
            },
            "junk",
            42,
            None,
            {
                "guideid": 11,
                "title": "Guide Two",
                "url": "https://www.ifixit.com/Guide/11",
            },
        ]
        result = await server_module.get_user(1, include_guides=True)
        assert result["guides"] == [
            {
                "guideid": 10,
                "title": "Guide One",
                "url": "https://www.ifixit.com/Guide/10",
            },
            {
                "guideid": 11,
                "title": "Guide Two",
                "url": "https://www.ifixit.com/Guide/11",
            },
        ]

    async def test_junk_limit_rejected_even_without_guides(self, client):
        # QA Round 12 (F12-7): limit must be validated unconditionally —
        # junk must fail even when include_guides=false leaves the value
        # unused (get_guide validates max_steps the same way). No network
        # call may happen for a junk limit.
        with pytest.raises(
            ValueError,
            match=r"User lookup failed: limit must be a positive integer",
        ):
            await server_module.get_user(1, include_guides=False, limit="abc")
        assert client.calls == []

    async def test_http_error_raises_error(self, client):
        client.errors["get_user"] = httpx.ConnectError("boom")
        with pytest.raises(ValueError, match="User lookup failed:"):
            await server_module.get_user(1)


# ---------------------------------------------------------------------------
# No-stack-trace contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("boom"),
        KeyError("boom"),
        TypeError("boom"),
        AttributeError("boom"),
        RecursionError("boom"),
        httpx.InvalidURL("boom"),
    ],
    ids=[
        "ValueError",
        "KeyError",
        "TypeError",
        "AttributeError",
        "RecursionError",
        "InvalidURL",
    ],
)
async def test_tools_never_leak_tracebacks_for_client_exceptions(client, exc):
    """QA finding: RecursionError (and other weird exceptions) from the
    client layer must never escape a tool as a traceback — every tool
    converts each caught exception type into a clean ValueError whose
    message carries the family prefix and no traceback text. QA Round 14
    (F14-2): httpx.InvalidURL is NOT an httpx.HTTPError (it subclasses
    Exception directly) — an overlong URL component used to escape the
    except-family as a bare InvalidURL; it must be caught like the rest."""
    calls = [
        ("search", server_module.search_guides, ("battery",), "Search failed:"),
        ("get_guide", server_module.get_guide, (1220,), "Guide lookup failed:"),
        ("get_categories", server_module.browse_categories, ("Mac",), "Category lookup failed:"),
        ("get_device", server_module.get_device, ("iPhone",), "Device lookup failed:"),
        ("list_device_guides", server_module.list_device_guides, ("iPhone",), "Guide listing failed:"),
        ("get_maintenance_schedule", server_module.get_maintenance_schedule, ("iPhone",), "Maintenance schedule lookup failed:"),
        ("get_media", server_module.get_media, (14056,), "Media lookup failed:"),
        ("get_user", server_module.get_user, (1,), "User lookup failed:"),
    ]
    for name, tool, args, prefix in calls:
        client.errors[name] = exc
        with pytest.raises(ValueError) as excinfo:
            await tool(*args)
        message = str(excinfo.value)
        assert message.startswith(prefix), f"{name}: {message!r}"
        assert "Traceback" not in message, f"{name} leaked a traceback: {message!r}"
        assert 'File "' not in message, f"{name} leaked a traceback: {message!r}"


async def test_call_tool_huge_lang_capped_cleanly(monkeypatch):
    """QA Round 14 (F14-2): get_guide's lang was uncapped — "L"*100001
    used to hit httpx.InvalidURL ("URL component 'query' too long") which
    escaped the except-family with no "Guide lookup failed:" prefix and a
    misleading 'query' name. The client cap must fire first: clean family
    message naming lang, with NO network call."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no network call for a huge lang")

    real_client = make_mock_client(handler)
    monkeypatch.setattr(server_module, "_client", real_client)

    with pytest.raises(Exception) as excinfo:
        await server_module.mcp.call_tool(
            "get_guide", {"guideid": 1220, "lang": "L" * 100_001}
        )
    message = str(excinfo.value)
    assert "Guide lookup failed:" in message
    assert "lang exceeds maximum length of 100000 characters" in message
    assert "InvalidURL" not in message
    assert "URL component" not in message


async def test_tool_converts_real_task_cancellation_to_clean_string(
    client, monkeypatch
):
    """QA Round 3 (HIGH): a tool task cancelled mid-client-call must come
    back as a clean \"Request cancelled\" string — a bare CancelledError
    escaping the tool corrupts JSON-RPC handling (FastMCP catches only
    Exception)."""
    started = asyncio.Event()

    async def slow_get_guide(*args, **kwargs):
        started.set()
        await asyncio.sleep(10)
        return {}

    monkeypatch.setattr(client, "get_guide", slow_get_guide)
    t = asyncio.create_task(server_module.get_guide(1220))
    await started.wait()  # the client call is now mid-flight
    t.cancel()
    result = await t  # must NOT raise CancelledError
    assert isinstance(result, str)
    assert "cancelled" in result.lower()
    assert "Traceback" not in result


async def test_all_tools_convert_client_cancelled_error_to_clean_string(client):
    """Same guarantee across every tool: a CancelledError raised by the
    client call is converted to the clean string, never re-raised."""
    calls = [
        ("search", server_module.search_guides, ("battery",)),
        ("get_guide", server_module.get_guide, (1220,)),
        ("get_categories", server_module.browse_categories, ("Mac",)),
        ("get_device", server_module.get_device, ("iPhone",)),
        ("list_device_guides", server_module.list_device_guides, ("iPhone",)),
        (
            "get_maintenance_schedule",
            server_module.get_maintenance_schedule,
            ("iPhone",),
        ),
        ("get_media", server_module.get_media, (14056,)),
        ("get_user", server_module.get_user, (1,)),
    ]
    for name, tool, args in calls:
        client.errors[name] = asyncio.CancelledError()
        result = await tool(*args)
        assert isinstance(result, str), f"{name} leaked CancelledError"
        assert "cancelled" in result.lower(), f"{name}: {result!r}"


# ---------------------------------------------------------------------------
# Real server object end-to-end (not the synthetic app fixture)
# ---------------------------------------------------------------------------


async def test_real_server_call_tool_with_mock_transport_client(monkeypatch):
    """QA finding: tools were only exercised through the synthetic app
    fixture. Drive the REAL FastMCP server object (ifixit_mcp.server.mcp)
    through its call_tool path with a real IfixitClient backed by a
    MockTransport — no network, but the real registration pipeline."""
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return json_response({"Mac": {"MacBook": None}, "Phone": None})

    real_client = make_mock_client(handler)
    monkeypatch.setattr(server_module, "_client", real_client)

    contents, meta = await server_module.mcp.call_tool(
        "browse_categories", {"path": "Mac"}
    )
    assert meta == {"result": ["MacBook"]}
    assert "MacBook" in contents[0].text
    assert len(seen_urls) == 1
    assert "/categories" in seen_urls[0]


async def test_real_server_call_tool_raises_clean_error(monkeypatch):
    """An error through the REAL server object arrives as a ToolError with
    a clean message (FastMCP prefixes the tool name) — never a traceback.
    On the wire this becomes isError: true with that message as content
    (verified in QA Round 6: mcp 1.26's lowlevel handler converts a
    raised exception into an error CallToolResult)."""
    from mcp.server.fastmcp.exceptions import ToolError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "not found"})

    real_client = make_mock_client(handler)
    monkeypatch.setattr(server_module, "_client", real_client)

    with pytest.raises(ToolError) as excinfo:
        await server_module.mcp.call_tool("get_device", {"title": "Nokia 3310"})
    message = str(excinfo.value)
    assert "Device not found: Nokia 3310" in message
    assert "Traceback" not in message


# ---------------------------------------------------------------------------
# QA Round 5 (R5-3 / R5-4): id-param bool coercion + pydantic dump reduction
# ---------------------------------------------------------------------------


class TestIdParamBoolCoercion:
    """JSON true must never silently fetch id 1 through the REAL FastMCP
    call_tool path (pydantic used to coerce true -> 1 before the tool body
    ran). It must produce the clean family rejection string instead, and
    numbers / numeric strings must keep working."""

    async def test_call_tool_bool_guideid_rejected_cleanly(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no network call for a bool guideid")

        real_client = make_mock_client(handler)
        monkeypatch.setattr(server_module, "_client", real_client)

        with pytest.raises(Exception, match="guideid must be a positive integer"):
            await server_module.mcp.call_tool("get_guide", {"guideid": True})

    async def test_call_tool_int_guideid_coerced_and_works(self, monkeypatch):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return json_response(
                {
                    "guideid": 1220,
                    "title": "Battery Replacement",
                    "difficulty": "Moderate",
                    "steps": [{"stepid": 1, "title": "Open the phone"}],
                }
            )

        real_client = make_mock_client(handler)
        monkeypatch.setattr(server_module, "_client", real_client)

        contents, meta = await server_module.mcp.call_tool("get_guide", {"guideid": 1})
        assert meta["result"]["guideid"] == 1220
        assert seen["url"].endswith("/guides/1")

    async def test_call_tool_string_guideid_works(self, monkeypatch):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return json_response(
                {
                    "guideid": 1220,
                    "title": "Battery Replacement",
                    "difficulty": "Moderate",
                    "steps": [{"stepid": 1, "title": "Open the phone"}],
                }
            )

        real_client = make_mock_client(handler)
        monkeypatch.setattr(server_module, "_client", real_client)

        contents, meta = await server_module.mcp.call_tool(
            "get_guide", {"guideid": "1220"}
        )
        assert meta["result"]["guideid"] == 1220
        assert seen["url"].endswith("/guides/1220")

    async def test_call_tool_junk_guideid_rejected_cleanly(self, monkeypatch):
        # R5-4: malformed JSON types for the id param must yield the clean
        # family message — never a raw pydantic ValidationError dump.
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no network call for junk guideid")

        real_client = make_mock_client(handler)
        monkeypatch.setattr(server_module, "_client", real_client)

        with pytest.raises(Exception, match="guideid must be a positive integer"):
            await server_module.mcp.call_tool("get_guide", {"guideid": [1, 2]})

    async def test_call_tool_bool_user_id_rejected_cleanly(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no network call for a bool user_id")

        real_client = make_mock_client(handler)
        monkeypatch.setattr(server_module, "_client", real_client)

        with pytest.raises(Exception, match="user_id must be a positive integer"):
            await server_module.mcp.call_tool("get_user", {"user_id": True})

    async def test_call_tool_bool_media_id_rejected_cleanly(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no network call for a bool media_id")

        real_client = make_mock_client(handler)
        monkeypatch.setattr(server_module, "_client", real_client)

        with pytest.raises(Exception, match="media_id must be a positive integer"):
            await server_module.mcp.call_tool("get_media", {"media_id": True})


# ---------------------------------------------------------------------------
# QA Round 6 (F1/F3): str-typed non-id params — no bool->int coercion, no
# pydantic dumps on coercible values
# ---------------------------------------------------------------------------


class TestStrTypedNonIdParams:
    """JSON true must never silently become max_steps=1 / limit=1 (pydantic
    used to coerce true -> 1 before the tool body ran), and coercible junk
    (numbers, floats) on string params must reach the client validator as
    plain strings — yielding the clean family rejection (raised as a
    ToolError through the real server, isError: true on the wire), never a
    raw pydantic ValidationError dump."""

    async def test_call_tool_bool_max_steps_rejected_cleanly(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no network call for a bool max_steps")

        real_client = make_mock_client(handler)
        monkeypatch.setattr(server_module, "_client", real_client)

        with pytest.raises(Exception, match="max_steps must be a positive integer"):
            await server_module.mcp.call_tool(
                "get_guide", {"guideid": 1220, "detail": "full", "max_steps": True}
            )

    async def test_call_tool_float_max_steps_rejected_cleanly(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no network call for a float max_steps")

        real_client = make_mock_client(handler)
        monkeypatch.setattr(server_module, "_client", real_client)

        with pytest.raises(Exception, match="max_steps must be a positive integer"):
            await server_module.mcp.call_tool(
                "get_guide", {"guideid": 1220, "detail": "full", "max_steps": 1.5}
            )

    async def test_call_tool_string_max_steps_truncates(self, monkeypatch):
        steps = [{"stepid": i, "title": f"Step {i}"} for i in range(1, 4)]

        def handler(request: httpx.Request) -> httpx.Response:
            return json_response({"guideid": 1220, "title": "T", "steps": steps})

        real_client = make_mock_client(handler)
        monkeypatch.setattr(server_module, "_client", real_client)

        contents, meta = await server_module.mcp.call_tool(
            "get_guide", {"guideid": 1220, "detail": "full", "max_steps": "2"}
        )
        assert [s["stepid"] for s in meta["result"]["steps"]] == [1, 2]

    async def test_call_tool_bool_limit_rejected_cleanly(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            # The profile fetch is fine; the guides fetch must never happen
            # with a bool limit.
            if "/users/1/guides" in str(request.url):
                raise AssertionError("no guides call for a bool limit")
            return json_response({"userid": 1, "username": "kimbo"})

        real_client = make_mock_client(handler)
        monkeypatch.setattr(server_module, "_client", real_client)

        with pytest.raises(Exception, match="limit must be a positive integer"):
            await server_module.mcp.call_tool(
                "get_user", {"user_id": 1, "include_guides": True, "limit": True}
            )

    async def test_call_tool_string_limit_limits_guides(self, monkeypatch):
        guides = [
            {
                "guideid": i,
                "title": f"Guide {i}",
                "url": f"https://www.ifixit.com/Guide/{i}",
            }
            for i in range(1, 11)
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            if "/users/1/guides" in str(request.url):
                # The API clamps server-side: honor the limit param.
                limit = int(request.url.params.get("limit", 20))
                return json_response(guides[:limit])
            return json_response({"userid": 1, "username": "kimbo"})

        real_client = make_mock_client(handler)
        monkeypatch.setattr(server_module, "_client", real_client)

        contents, meta = await server_module.mcp.call_tool(
            "get_user", {"user_id": 1, "include_guides": True, "limit": "5"}
        )
        assert len(meta["result"]["guides"]) == 5

    async def test_call_tool_numeric_detail_rejected_cleanly(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no network call for a numeric detail")

        real_client = make_mock_client(handler)
        monkeypatch.setattr(server_module, "_client", real_client)

        with pytest.raises(Exception, match="detail must be 'summary' or 'full'"):
            await server_module.mcp.call_tool(
                "get_guide", {"guideid": 1220, "detail": 5}
            )

    async def test_call_tool_bool_media_type_rejected_cleanly(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no network call for a bool media_type")

        real_client = make_mock_client(handler)
        monkeypatch.setattr(server_module, "_client", real_client)

        with pytest.raises(Exception, match="media_type must be one of"):
            await server_module.mcp.call_tool(
                "get_media", {"media_id": 14056, "media_type": True}
            )

    async def test_call_tool_numeric_doctypes_rejected_cleanly(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no network call for numeric doctypes")

        real_client = make_mock_client(handler)
        monkeypatch.setattr(server_module, "_client", real_client)

        with pytest.raises(Exception, match="doctypes must be"):
            await server_module.mcp.call_tool(
                "search_guides", {"query": "battery", "doctypes": 5}
            )

    async def test_call_tool_bool_title_rejected_cleanly(self, monkeypatch):
        # title:true -> "True" is a non-empty string, so it reaches the
        # client and becomes a clean not-found error — never a pydantic
        # dump and never a fetch of a real device.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"message": "not found"})

        real_client = make_mock_client(handler)
        monkeypatch.setattr(server_module, "_client", real_client)

        with pytest.raises(Exception, match="Device lookup failed"):
            await server_module.mcp.call_tool("get_device", {"title": True})

    async def test_call_tool_numeric_query_coerced_harmlessly(self, monkeypatch):
        # query:123 -> "123" is a harmless (if useless) search — never a
        # pydantic dump.
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return json_response({"query": "123", "results": []})

        real_client = make_mock_client(handler)
        monkeypatch.setattr(server_module, "_client", real_client)

        contents, meta = await server_module.mcp.call_tool(
            "search_guides", {"query": 123}
        )
        assert meta["result"]["query"] == "123"
        assert "/suggest/123" in seen["url"]

    async def test_call_tool_object_path_no_pydantic_dump(self, monkeypatch):
        # Residual case: an object passed to a str param is coerced via
        # repr before the client validator sees it, so even path:{} cannot
        # produce a raw pydantic dump — it becomes a clean not-found error
        # for the literal path "{}" (after a real tree fetch).
        def handler(request: httpx.Request) -> httpx.Response:
            return json_response({"Mac": {"MacBook": None}, "Phone": None})

        real_client = make_mock_client(handler)
        monkeypatch.setattr(server_module, "_client", real_client)

        with pytest.raises(Exception, match="Category lookup failed: Category not found: {}"):
            await server_module.mcp.call_tool("browse_categories", {"path": {}})


# ---------------------------------------------------------------------------
# QA Round 7 (F1): include_guides is the one bare-bool param
# ---------------------------------------------------------------------------


class TestQA7IncludeGuides:
    """QA Round 7 (F1): include_guides was the only param annotated as a
    bare bool, so JSON 2 leaked a raw pydantic bool_parsing dump (with a
    pydantic.dev link) through the real server. It must accept
    true/false/1/0 (as JSON booleans, numbers, or strings) and reject
    everything else with the clean family message."""

    async def test_call_tool_include_guides_two_rejected_cleanly(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no network call for a junk include_guides")

        real_client = make_mock_client(handler)
        monkeypatch.setattr(server_module, "_client", real_client)

        with pytest.raises(Exception, match="include_guides must be a boolean"):
            await server_module.mcp.call_tool(
                "get_user", {"user_id": 1, "include_guides": 2}
            )

    async def test_call_tool_include_guides_junk_object_rejected_cleanly(
        self, monkeypatch
    ):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no network call for a junk include_guides")

        real_client = make_mock_client(handler)
        monkeypatch.setattr(server_module, "_client", real_client)

        with pytest.raises(Exception, match="include_guides must be a boolean"):
            await server_module.mcp.call_tool(
                "get_user", {"user_id": 1, "include_guides": {"a": 1}}
            )

    async def test_call_tool_include_guides_string_true_works(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            if "/users/1/guides" in str(request.url):
                return json_response(
                    [{"guideid": 10, "title": "Guide One", "url": "https://www.ifixit.com/Guide/10"}]
                )
            return json_response({"userid": 1, "username": "kimbo"})

        real_client = make_mock_client(handler)
        monkeypatch.setattr(server_module, "_client", real_client)

        contents, meta = await server_module.mcp.call_tool(
            "get_user", {"user_id": 1, "include_guides": "true"}
        )
        assert meta["result"]["guides"] == [
            {"guideid": 10, "title": "Guide One", "url": "https://www.ifixit.com/Guide/10"}
        ]

    async def test_call_tool_include_guides_json_bool_true_works(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            if "/users/1/guides" in str(request.url):
                return json_response(
                    [{"guideid": 10, "title": "Guide One", "url": "https://www.ifixit.com/Guide/10"}]
                )
            return json_response({"userid": 1, "username": "kimbo"})

        real_client = make_mock_client(handler)
        monkeypatch.setattr(server_module, "_client", real_client)

        contents, meta = await server_module.mcp.call_tool(
            "get_user", {"user_id": 1, "include_guides": True}
        )
        assert meta["result"]["guides"] == [
            {"guideid": 10, "title": "Guide One", "url": "https://www.ifixit.com/Guide/10"}
        ]

    async def test_call_tool_include_guides_zero_means_false(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            if "/users/1/guides" in str(request.url):
                raise AssertionError("no guides fetch when include_guides is falsy")
            return json_response({"userid": 1, "username": "kimbo"})

        real_client = make_mock_client(handler)
        monkeypatch.setattr(server_module, "_client", real_client)

        contents, meta = await server_module.mcp.call_tool(
            "get_user", {"user_id": 1, "include_guides": 0}
        )
        assert meta["result"]["username"] == "kimbo"
        assert "guides" not in meta["result"]


# ---------------------------------------------------------------------------
# QA Round 7 (F2): JSON null — required params reject, optional params omit
# ---------------------------------------------------------------------------


class TestQA7JsonNull:
    """QA Round 7 (F2): JSON null on a required str param used to be coerced
    to the literal string "None" — search_guides({"query": null}) ran a real
    search for the word "None" and get_device({"title": null}) looked up a
    device literally named "None". Required params must reject null with the
    clean family message (no network call); optional params must treat null
    as omitted."""

    async def test_call_tool_null_query_rejected_cleanly_no_network(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no network call for a null query")

        real_client = make_mock_client(handler)
        monkeypatch.setattr(server_module, "_client", real_client)

        with pytest.raises(Exception, match="query must be a non-empty string"):
            await server_module.mcp.call_tool("search_guides", {"query": None})

    async def test_call_tool_null_title_rejected_cleanly_no_network(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no network call for a null title")

        real_client = make_mock_client(handler)
        monkeypatch.setattr(server_module, "_client", real_client)

        with pytest.raises(Exception, match="title must be a non-empty string"):
            await server_module.mcp.call_tool("get_device", {"title": None})

    async def test_call_tool_null_lang_omitted(self, monkeypatch):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return json_response({"guideid": 1220, "title": "T", "steps": []})

        real_client = make_mock_client(handler)
        monkeypatch.setattr(server_module, "_client", real_client)

        contents, meta = await server_module.mcp.call_tool(
            "get_guide", {"guideid": 1220, "lang": None}
        )
        assert meta["result"]["guideid"] == 1220
        assert "langid" not in seen["url"]

    async def test_call_tool_null_device_omitted(self, monkeypatch):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return json_response({"query": "battery", "results": []})

        real_client = make_mock_client(handler)
        monkeypatch.setattr(server_module, "_client", real_client)

        contents, meta = await server_module.mcp.call_tool(
            "search_guides", {"query": "battery", "device": None}
        )
        assert meta["result"]["query"] == "battery"
        assert "guideDevice" not in seen["url"]

    async def test_call_tool_null_path_means_top_level(self, monkeypatch):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return json_response({"Mac": {"MacBook": None}, "Phone": None})

        real_client = make_mock_client(handler)
        monkeypatch.setattr(server_module, "_client", real_client)

        contents, meta = await server_module.mcp.call_tool(
            "browse_categories", {"path": None}
        )
        assert meta["result"] == ["Mac", "Phone"]
        assert seen["url"].endswith("/categories")

    async def test_call_tool_null_max_steps_omitted(self, monkeypatch):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return json_response(
                {
                    "guideid": 1220,
                    "title": "T",
                    "steps": [
                        {"stepid": i, "title": f"Step {i}"} for i in range(1, 4)
                    ],
                }
            )

        real_client = make_mock_client(handler)
        monkeypatch.setattr(server_module, "_client", real_client)

        contents, meta = await server_module.mcp.call_tool(
            "get_guide", {"guideid": 1220, "detail": "full", "max_steps": None}
        )
        assert len(meta["result"]["steps"]) == 3


# ---------------------------------------------------------------------------
# QA Round 7 (F4): exact-str annotations kill the pre_parse_json escape
# ---------------------------------------------------------------------------


class TestQA7PreParseJsonEscape:
    """QA Round 7 (F4): FastMCP runs json.loads() on string values whose
    field annotation is not exactly `str`. The str|None optional params
    (max_steps, lang, device, path) took that path, so a 5000-digit string
    raised the Python 3.11 int-digit-limit ValueError ("Exceeds the limit
    (4300 digits)") which bypassed the clean family messages. Every tool
    param must be exactly-str annotated (optional params use "" as the
    omitted sentinel) so pre_parse_json never runs on our params."""

    async def test_call_tool_huge_max_steps_clean_message(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no network call for a huge max_steps")

        real_client = make_mock_client(handler)
        monkeypatch.setattr(server_module, "_client", real_client)

        with pytest.raises(Exception) as excinfo:
            await server_module.mcp.call_tool(
                "get_guide",
                {"guideid": 1220, "detail": "full", "max_steps": "1" * 5000},
            )
        message = str(excinfo.value)
        assert "max_steps must be a positive integer" in message
        assert "Exceeds the limit" not in message
        assert "4300" not in message

    async def test_call_tool_huge_lang_clean_message(self, monkeypatch):
        # lang takes the same pre_parse_json path; a huge numeric string
        # must not raise the int-digit-limit ValueError.
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no network call for a huge lang")

        real_client = make_mock_client(handler)
        monkeypatch.setattr(server_module, "_client", real_client)

        with pytest.raises(Exception) as excinfo:
            await server_module.mcp.call_tool(
                "get_guide", {"guideid": 1220, "lang": "1" * 5000}
            )
        message = str(excinfo.value)
        assert "Exceeds the limit" not in message
        assert "4300" not in message

    async def test_call_tool_empty_string_lang_omitted(self, monkeypatch):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return json_response({"guideid": 1220, "title": "T", "steps": []})

        real_client = make_mock_client(handler)
        monkeypatch.setattr(server_module, "_client", real_client)

        contents, meta = await server_module.mcp.call_tool(
            "get_guide", {"guideid": 1220, "lang": ""}
        )
        assert meta["result"]["guideid"] == 1220
        assert "langid" not in seen["url"]

    async def test_call_tool_empty_string_path_means_top_level(self, monkeypatch):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return json_response({"Mac": {"MacBook": None}, "Phone": None})

        real_client = make_mock_client(handler)
        monkeypatch.setattr(server_module, "_client", real_client)

        contents, meta = await server_module.mcp.call_tool(
            "browse_categories", {"path": ""}
        )
        assert meta["result"] == ["Mac", "Phone"]
        assert seen["url"].endswith("/categories")


# ---------------------------------------------------------------------------
# QA Round 9 (R9-3): whitespace-only optional params are omitted; padded
# values are stripped before they reach the client
# ---------------------------------------------------------------------------


class TestQA9OptionalParamStripping:
    """QA Round 9 (R9-3): lang="   " / device="   " used to reach the client
    as whitespace-padded strings — the API silently ignored them, and a
    future API change could silently switch languages or devices. A
    whitespace-only optional param must be treated as omitted (no langid /
    guideDevice on the wire) and a padded value must be stripped
    (lang=" de " -> langid=de) before the client sees it."""

    # -- whitespace-only -> omitted (wire path through FastMCP) --------------

    async def test_whitespace_only_lang_omitted(self, client):
        client.results["search"] = {"query": "battery", "results": []}
        await server_module.mcp.call_tool(
            "search_guides", {"query": "battery", "lang": "   "}
        )
        name, args, kwargs = client.calls[-1]
        assert name == "search"
        assert kwargs["lang"] is None

    async def test_whitespace_only_lang_not_on_wire(self, monkeypatch):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return json_response({"query": "battery", "results": []})

        real_client = make_mock_client(handler)
        monkeypatch.setattr(server_module, "_client", real_client)

        await server_module.mcp.call_tool(
            "search_guides", {"query": "battery", "lang": "   "}
        )
        assert "langid" not in seen["url"]
        assert "guideDevice" not in seen["url"]

    async def test_whitespace_only_device_omitted(self, client):
        client.results["search"] = {"query": "battery", "results": []}
        await server_module.mcp.call_tool(
            "search_guides", {"query": "battery", "device": " \t "}
        )
        name, args, kwargs = client.calls[-1]
        assert name == "search"
        assert kwargs["device"] is None

    async def test_whitespace_only_max_steps_omitted(self, client):
        await server_module.mcp.call_tool(
            "get_guide", {"guideid": 1220, "detail": "full", "max_steps": "  "}
        )
        name, args, kwargs = client.calls[-1]
        assert name == "get_guide"
        assert kwargs["max_steps"] is None

    async def test_whitespace_only_path_omitted(self, client):
        client.results["get_categories"] = ["Mac", "Phone"]
        await server_module.mcp.call_tool("browse_categories", {"path": "  "})
        name, args, kwargs = client.calls[-1]
        assert name == "get_categories"
        assert args == (None,)

    # -- padded values -> stripped -------------------------------------------

    async def test_padded_lang_stripped(self, client):
        client.results["search"] = {"query": "battery", "results": []}
        await server_module.mcp.call_tool(
            "search_guides", {"query": "battery", "lang": " de "}
        )
        name, args, kwargs = client.calls[-1]
        assert name == "search"
        assert kwargs["lang"] == "de"

    async def test_padded_device_stripped(self, client):
        client.results["search"] = {"query": "battery", "results": []}
        await server_module.mcp.call_tool(
            "search_guides", {"query": "battery", "device": " iPhone 13 "}
        )
        name, args, kwargs = client.calls[-1]
        assert name == "search"
        assert kwargs["device"] == "iPhone 13"

    async def test_padded_detail_stripped_to_full(self, client):
        await server_module.mcp.call_tool(
            "get_guide", {"guideid": 1220, "detail": " full "}
        )
        name, args, kwargs = client.calls[-1]
        assert name == "get_guide"
        assert kwargs["detail"] == "full"

    async def test_padded_media_type_stripped(self, client):
        await server_module.mcp.call_tool(
            "get_media", {"media_id": 14056, "media_type": " images "}
        )
        name, args, kwargs = client.calls[-1]
        assert name == "get_media"
        assert kwargs["media_type"] == "images"

    async def test_padded_path_stripped(self, client):
        client.results["get_categories"] = ["MacBook"]
        await server_module.mcp.call_tool("browse_categories", {"path": " Mac "})
        name, args, kwargs = client.calls[-1]
        assert name == "get_categories"
        assert args == ("Mac",)

    # -- direct calls (bypass pydantic; _empty_to_none must strip) -----------

    async def test_direct_call_padded_lang_stripped(self, client):
        client.results["search"] = {"query": "battery", "results": []}
        await server_module.search_guides("battery", lang=" de ")
        name, args, kwargs = client.calls[-1]
        assert name == "search"
        assert kwargs["lang"] == "de"

    async def test_direct_call_whitespace_only_device_omitted(self, client):
        client.results["search"] = {"query": "battery", "results": []}
        await server_module.search_guides("battery", device="   ")
        name, args, kwargs = client.calls[-1]
        assert name == "search"
        assert kwargs["device"] is None

    # -- coercer units --------------------------------------------------------

    def test_empty_to_none_strips_and_omits_whitespace(self):
        assert server_module._empty_to_none("") is None
        assert server_module._empty_to_none("   ") is None
        assert server_module._empty_to_none(" de ") == "de"
        assert server_module._empty_to_none(None) is None
        # Non-strings (direct-call ints, e.g. max_steps=3) pass through.
        assert server_module._empty_to_none(3) == 3

    def test_coerce_string_strips_whitespace(self):
        assert server_module._coerce_string(" de ") == "de"
        assert server_module._coerce_string("   ") == ""
        assert server_module._coerce_string("iPhone 13") == "iPhone 13"


# ---------------------------------------------------------------------------
# QA Round 10 (R10-1): null/whitespace-only values for the optional params
# WITH defaults fall back to the default (omission semantics)
# ---------------------------------------------------------------------------


class TestQA10OptionalWithDefaultParams:
    """QA Round 10 (R10-1): detail/media_type/doctypes/limit are optional
    params WITH defaults, so a null or whitespace-only value means
    "omitted" — it must fall back to the declared default ("summary" /
    "images" / "guide" / "20") before validation, exactly like the
    optional params without defaults (lang/device/max_steps/path) fall
    back to None. Null used to hard-error: get_guide({detail: null}) ->
    "detail must be 'summary' or 'full'", get_media({media_type: null}) ->
    "media_type must be one of: ...", search_guides({doctypes: null}) ->
    "doctypes must be one or more of: ...", and get_user({limit: null,
    include_guides: true}) -> "limit must be a positive integer, got ''"."""

    # -- detail (get_guide) ------------------------------------------------

    async def test_call_tool_null_detail_falls_back_to_summary(self, client):
        await server_module.mcp.call_tool(
            "get_guide", {"guideid": 1220, "detail": None}
        )
        name, args, kwargs = client.calls[-1]
        assert name == "get_guide"
        assert kwargs["detail"] == "summary"

    async def test_call_tool_whitespace_detail_falls_back_to_summary(self, client):
        await server_module.mcp.call_tool(
            "get_guide", {"guideid": 1220, "detail": "   "}
        )
        name, args, kwargs = client.calls[-1]
        assert name == "get_guide"
        assert kwargs["detail"] == "summary"

    async def test_direct_call_null_detail_falls_back_to_summary(self, client):
        # Direct calls bypass pydantic, so the coercer sees raw None.
        await server_module.get_guide(1220, detail=None)
        name, args, kwargs = client.calls[-1]
        assert name == "get_guide"
        assert kwargs["detail"] == "summary"

    # -- media_type (get_media) --------------------------------------------

    async def test_call_tool_null_media_type_falls_back_to_images(self, client):
        await server_module.mcp.call_tool(
            "get_media", {"media_id": 14056, "media_type": None}
        )
        name, args, kwargs = client.calls[-1]
        assert name == "get_media"
        assert kwargs["media_type"] == "images"

    async def test_call_tool_whitespace_media_type_falls_back_to_images(self, client):
        await server_module.mcp.call_tool(
            "get_media", {"media_id": 14056, "media_type": " \t "}
        )
        name, args, kwargs = client.calls[-1]
        assert name == "get_media"
        assert kwargs["media_type"] == "images"

    async def test_direct_call_whitespace_media_type_falls_back_to_images(
        self, client
    ):
        await server_module.get_media(14056, media_type="   ")
        name, args, kwargs = client.calls[-1]
        assert name == "get_media"
        assert kwargs["media_type"] == "images"

    # -- doctypes (search_guides) ------------------------------------------

    async def test_call_tool_null_doctypes_falls_back_to_guide(self, client):
        client.results["search"] = {"query": "battery", "results": []}
        await server_module.mcp.call_tool(
            "search_guides", {"query": "battery", "doctypes": None}
        )
        name, args, kwargs = client.calls[-1]
        assert name == "search"
        assert kwargs["doctypes"] == "guide"

    async def test_call_tool_whitespace_doctypes_falls_back_to_guide(self, client):
        client.results["search"] = {"query": "battery", "results": []}
        await server_module.mcp.call_tool(
            "search_guides", {"query": "battery", "doctypes": "  "}
        )
        name, args, kwargs = client.calls[-1]
        assert name == "search"
        assert kwargs["doctypes"] == "guide"

    # -- limit (get_user, include_guides=true) -----------------------------

    async def test_call_tool_null_limit_defaults_to_20(self, client):
        await server_module.mcp.call_tool(
            "get_user", {"user_id": 1, "include_guides": True, "limit": None}
        )
        name, args, kwargs = client.calls[-1]
        assert name == "list_user_guides"
        assert kwargs["limit"] == "20"

    async def test_call_tool_whitespace_limit_defaults_to_20(self, client):
        await server_module.mcp.call_tool(
            "get_user", {"user_id": 1, "include_guides": True, "limit": "  "}
        )
        name, args, kwargs = client.calls[-1]
        assert name == "list_user_guides"
        assert kwargs["limit"] == "20"

    async def test_call_tool_null_limit_without_guides_still_works(self, client):
        # limit is only consumed when include_guides is true; a null limit
        # with include_guides false must not error (it never reaches the
        # client), and no guides fetch may happen.
        contents, meta = await server_module.mcp.call_tool(
            "get_user", {"user_id": 1, "include_guides": False, "limit": None}
        )
        assert meta["result"]["username"] == "kimbo"
        assert client.calls[-1][0] == "get_user"

    # -- coercer unit -------------------------------------------------------

    def test_empty_to_default_falls_back_and_strips(self):
        assert server_module._empty_to_default("", "summary") == "summary"
        assert server_module._empty_to_default("   ", "guide") == "guide"
        assert server_module._empty_to_default(None, "images") == "images"
        assert server_module._empty_to_default(" full ", "summary") == "full"
        assert server_module._empty_to_default("videos", "images") == "videos"
        # Non-strings (direct-call ints, e.g. limit=5) pass through.
        assert server_module._empty_to_default(5, "20") == 5
