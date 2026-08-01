"""Shared pytest fixtures for ifixit-mcp.

Provides a FakeIfixitClient stub (per-method canned results/errors) patched
in as ``ifixit_mcp.server._client``, an ``app`` fixture that builds a
FastMCP server wired to the fake client, plus MockTransport helpers that
mirror the internet-archive-mcp conftest pattern.
"""

from __future__ import annotations

import copy

import httpx
import pytest

from ifixit_mcp.client import IfixitClient

TOOL_NAMES = (
    "search_guides",
    "get_guide",
    "browse_categories",
    "get_device",
    "list_device_guides",
    "get_maintenance_schedule",
    "get_media",
    "get_user",
)


def make_mock_client(handler):
    """Create an IfixitClient backed by a MockTransport handler."""
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    return IfixitClient(
        client=http_client, min_request_interval=0, backoff_base=0.01
    )


def json_response(data, status_code=200):
    """Build a mock httpx.Response with JSON body."""
    return httpx.Response(
        status_code=status_code,
        json=data,
        headers={"Content-Type": "application/json"},
    )


class FakeIfixitClient:
    """Async stub for IfixitClient with per-method canned results/errors.

    Configure ``results[name]`` with the value a method should return (or
    ``errors[name]`` with an exception it should raise) before invoking the
    tool under test. Every call is recorded in ``calls`` as
    ``(name, args, kwargs)``.
    """

    METHODS = (
        "search",
        "get_guide",
        "get_categories",
        "get_device",
        "list_device_guides",
        "get_maintenance_schedule",
        "get_media",
        "get_user",
        "list_user_guides",
    )

    def __init__(self) -> None:
        self.results: dict[str, object] = {}
        self.errors: dict[str, Exception] = {}
        self.calls: list[tuple[str, tuple, dict]] = []

    def _dispatch(self, name: str, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        if name in self.errors:
            raise self.errors[name]
        if name in self.results:
            return self.results[name]
        raise AssertionError(
            f"FakeIfixitClient.{name} called but no result/error configured"
        )

    async def search(self, query, doctypes="guide", device=None, lang=None):
        return self._dispatch(
            "search", query, doctypes=doctypes, device=device, lang=lang
        )

    async def get_guide(self, guideid, detail="summary", lang=None, max_steps=None):
        return self._dispatch(
            "get_guide", guideid, detail=detail, lang=lang, max_steps=max_steps
        )

    async def get_categories(self, path=None):
        return self._dispatch("get_categories", path)

    async def get_device(self, title, namespace="CATEGORY"):
        return self._dispatch("get_device", title, namespace=namespace)

    async def list_device_guides(self, title, namespace="CATEGORY"):
        return self._dispatch("list_device_guides", title, namespace=namespace)

    async def get_maintenance_schedule(self, title, namespace="CATEGORY"):
        return self._dispatch(
            "get_maintenance_schedule", title, namespace=namespace
        )

    async def get_media(self, media_id, media_type="images"):
        return self._dispatch("get_media", media_id, media_type=media_type)

    async def get_user(self, user_id):
        return self._dispatch("get_user", user_id)

    async def list_user_guides(self, user_id, offset=0, limit=20):
        return self._dispatch(
            "list_user_guides", user_id, offset=offset, limit=limit
        )


# Canned defaults so a bare FakeIfixitClient already satisfies most tools.
DEFAULT_RESULTS: dict[str, object] = {
    "search": {"query": "battery", "results": []},
    "get_guide": {"guideid": 1220, "title": "Battery Replacement", "steps": []},
    "get_categories": ["Mac", "Phone"],
    "get_device": {"title": "iPhone", "repairability_score": 7},
    "list_device_guides": [],
    "get_maintenance_schedule": {"schedules": []},
    "get_media": {
        "image": {"id": 1, "guid": "abc", "original": "https://cdn.example/o.jpg"}
    },
    "get_user": {"userid": 1, "username": "kimbo", "reputation": 100},
    "list_user_guides": [],
}


@pytest.fixture
def client(monkeypatch):
    """A FakeIfixitClient patched in as ifixit_mcp.server._client."""
    import ifixit_mcp.server as server_module

    fake = FakeIfixitClient()
    # Deep-copy the canned defaults so a test that mutates a result can
    # never leak state into the shared module-level DEFAULT_RESULTS.
    fake.results.update(copy.deepcopy(DEFAULT_RESULTS))
    monkeypatch.setattr(server_module, "_client", fake)
    return fake


@pytest.fixture
def app(client):
    """A FastMCP app with all 8 tools registered, wired to the fake client."""
    import ifixit_mcp.server as server_module
    from mcp.server.fastmcp import FastMCP

    app = FastMCP("ifixit-test")
    for name in TOOL_NAMES:
        app.tool()(getattr(server_module, name))
    return app
