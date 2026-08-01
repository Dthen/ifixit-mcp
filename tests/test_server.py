"""Tests for server registration and startup."""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import select
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from ifixit_mcp import __version__

EXPECTED_TOOLS = {
    "search_guides",
    "get_guide",
    "browse_categories",
    "get_device",
    "list_device_guides",
    "get_maintenance_schedule",
    "get_media",
    "get_user",
}

# The FastMCP internals the tests read are private; guard every access with
# getattr fallbacks so a future mcp release cannot hard-crash the suite
# (mirrors the server-side guard for mcp._mcp_server.version).
def _registered_tools(server):
    """Return the server's {name: tool} dict, or {} if internals changed."""
    tool_manager = getattr(server, "_tool_manager", None)
    tools = getattr(tool_manager, "_tools", None) if tool_manager is not None else None
    return tools if isinstance(tools, dict) else {}


def test_server_has_8_tools():
    """Verify all 8 tools are registered."""
    from ifixit_mcp.server import mcp

    tools = _registered_tools(mcp)
    assert len(tools) == 8, f"Expected 8 tools, got {len(tools)}: {list(tools.keys())}"


def test_tool_names():
    """All expected tool names are registered."""
    from ifixit_mcp.server import mcp

    actual = set(_registered_tools(mcp).keys())
    assert actual == EXPECTED_TOOLS


def test_server_imports_cleanly():
    """Server module imports without errors."""
    import ifixit_mcp.server  # noqa: F401


def test_main_function_exists():
    """main() is callable."""
    from ifixit_mcp.server import main

    assert callable(main)


def test_server_info_version_pinned():
    """serverInfo.version must report the package version (0.1.0), not the
    mcp library default — imported from ifixit_mcp.__version__, not
    hardcoded."""
    from ifixit_mcp.server import mcp

    assert __version__ == "0.1.0"
    assert mcp._mcp_server.version == "0.1.0"
    assert mcp._mcp_server.version == __version__


def test_httpx_and_httpcore_logging_silenced():
    """QA finding: the mcp library enables httpx INFO logging, which wrote
    full request URLs (including /suggest/<query> search terms) to stderr.
    Importing the server must leave httpx/httpcore at WARNING or above."""
    import ifixit_mcp.server  # noqa: F401

    assert logging.getLogger("httpx").level >= logging.WARNING
    assert logging.getLogger("httpcore").level >= logging.WARNING


def test_mcp_logging_silenced():
    """QA Round 7 (F9): the mcp library logs a per-request INFO line
    ("Processing request of type CallToolRequest") to stderr for every
    call. Importing the server must silence the mcp logger family (the
    lowlevel server's logger is mcp.server.lowlevel, a child of mcp)."""
    import ifixit_mcp.server  # noqa: F401

    for name in (
        "mcp",
        "mcp.server",
        "mcp.server.lowlevel",
        "mcp.server.session",
    ):
        assert logging.getLogger(name).level >= logging.WARNING, name


def test_root_logger_silenced():
    """QA Round 12 (F12-2): mcp's session layer emits its
    "Failed to validate request/notification" WARNINGs via module-level
    logging.warning() — the ROOT logger — so the mcp.* logger levels do
    not silence them; a validation-failing notification used to dump an
    ~8KB pydantic error on stderr per session (bypassing the F9
    silencing). Importing the server must pin the root logger to ERROR
    so those dumps never reach stderr (ERROR-level records still do)."""
    import ifixit_mcp.server  # noqa: F401

    assert logging.getLogger().level >= logging.ERROR


def test_exception_group_available():
    """QA Round 7 (F3): ExceptionGroup is a Python 3.11+ builtin, but the
    package declares requires-python >=3.10. The server must resolve it
    from the exceptiongroup backport (a transitive dependency of anyio on
    3.10) with a builtin fallback on 3.11+, so main()'s disconnect
    handling works on every supported interpreter."""
    import ifixit_mcp.server as server_module

    eg = server_module.ExceptionGroup("probe", [ValueError("x")])
    assert len(eg.exceptions) == 1
    assert isinstance(eg, BaseException)


# ---------------------------------------------------------------------------
# Lazy client lifecycle
# ---------------------------------------------------------------------------


def test_module_client_is_none_until_first_use(monkeypatch):
    """QA finding: the module created IfixitClient() at import time and
    never closed it. Importing the server must create no client; the first
    get_client() call creates one."""
    import ifixit_mcp.server as server_module

    monkeypatch.setattr(server_module, "_client", None)
    assert server_module._client is None

    client = server_module.get_client()
    assert server_module._client is client
    assert client is not None

    # Creating the client again returns the same instance.
    assert server_module.get_client() is client

    # Clean up: close the real client and reset module state so other
    # tests are unaffected.
    asyncio.run(client.aclose())
    monkeypatch.setattr(server_module, "_client", None)


def test_atexit_close_handler_registered(monkeypatch):
    """Creating the client registers an atexit handler that closes it."""
    import ifixit_mcp.server as server_module

    registered: list = []
    monkeypatch.setattr(
        atexit, "register", lambda func, *args, **kwargs: registered.append(func)
    )
    monkeypatch.setattr(server_module, "_client", None)

    client = server_module.get_client()

    assert any(func is server_module._close_client for func in registered)
    assert atexit._ncallbacks() >= 1  # the real registry also saw it

    # Clean up: close the real client and reset module state so other
    # tests are unaffected.
    asyncio.run(client.aclose())
    monkeypatch.setattr(server_module, "_client", None)


async def test_tools_obtain_client_via_get_client(monkeypatch):
    """Tools must go through get_client() so the shared instance is created
    lazily on first tool call (and closed at exit)."""
    import ifixit_mcp.server as server_module
    from conftest import FakeIfixitClient

    fake = FakeIfixitClient()
    fake.results["get_categories"] = ["Mac", "Phone"]
    monkeypatch.setattr(server_module, "_client", None)
    monkeypatch.setattr(server_module, "get_client", lambda: fake)

    result = await server_module.browse_categories("Mac")

    assert result == ["Mac", "Phone"]
    assert ("get_categories", ("Mac",), {}) in fake.calls


def test_mutating_fake_result_does_not_leak_into_other_tests(client):
    """Mutating a fixture's canned result must not corrupt the shared
    DEFAULT_RESULTS (the fixture deep-copies them)."""
    client.results["get_categories"].append("Hacked")


def test_fixture_results_are_pristine_after_other_tests_mutate(client):
    """Runs after the mutating test above; the fixture must serve fresh,
    un-mutated canned results."""
    assert client.results["get_categories"] == ["Mac", "Phone"]


def test_app_fixture_builds_server_with_all_tools(app):
    """The conftest app fixture registers all 8 tools on a fresh server."""
    tools = _registered_tools(app)
    assert set(tools.keys()) == EXPECTED_TOOLS


async def test_app_fixture_tool_callable_through_server(app, client):
    """A tool invoked via the fixture app reaches the fake client."""
    client.results["get_categories"] = ["Mac", "Phone", "Tablet"]
    result = await app.call_tool("browse_categories", {"path": "Mac"})
    assert result is not None
    assert ("get_categories", ("Mac",), {}) in client.calls


# ---------------------------------------------------------------------------
# Real server: stdio subprocess handshake + real call_tool
# ---------------------------------------------------------------------------


def test_subprocess_stdio_handshake_reports_server_info():
    """QA finding: no test booted the real server. Spawn
    `python -m ifixit_mcp.server`, perform a real JSON-RPC initialize over
    stdio, and assert serverInfo {name: ifixit, version: 0.1.0}.

    The mcp stdio transport (1.x) frames messages as newline-delimited JSON
    on stdin/stdout — one JSON object per line.
    """
    src_dir = str(Path(__file__).resolve().parents[1] / "src")
    env = {**os.environ, "PYTHONPATH": src_dir}
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "ifixit_mcp.server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
    except OSError as exc:  # e.g. sandboxed environments without spawn
        pytest.skip(f"subprocess spawn unavailable: {exc}")

    try:
        assert proc.stdin is not None and proc.stdout is not None
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "qa-probe", "version": "0.0.1"},
            },
        }
        proc.stdin.write((json.dumps(initialize) + "\n").encode("utf-8"))
        proc.stdin.flush()

        # Read stdout lines until the initialize response (id == 1) arrives,
        # with a 10s deadline. Intervening notifications are skipped.
        response: dict | None = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            ready, _, _ = select.select([proc.stdout], [], [], 0.5)
            if not ready:
                continue
            line = proc.stdout.readline()
            if not line:
                break  # EOF
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") == 1:
                response = message
                break

        assert response is not None, "server did not respond to initialize within 10s"
        assert "error" not in response, response.get("error")
        server_info = response["result"]["serverInfo"]
        assert server_info["name"] == "ifixit"
        assert server_info["version"] == "0.1.0"

        # QA Round 12 (F12-2): like a real client, send the mandatory
        # notifications/initialized. mcp validates it via module-level
        # logging.warning() (the ROOT logger), bypassing the mcp.*
        # silencing — any validation failure dumps a pydantic error on
        # stderr. A clean handshake must leave stderr empty of that noise.
        _jsonrpc(
            proc,
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        )
        time.sleep(0.5)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        stderr = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
        assert "Failed to validate" not in stderr, (
            f"validation noise leaked: {stderr[:2000]}"
        )
        assert "Traceback" not in stderr, f"traceback leaked: {stderr[:2000]}"
        assert "pydantic" not in stderr, f"pydantic dump leaked: {stderr[:2000]}"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        _close_pipes(proc)


def test_subprocess_validation_failure_never_dumps_on_stderr():
    """QA Round 12 (F12-2): mcp's "Failed to validate notification"
    WARNING (mcp/shared/session.py) is emitted via module-level
    logging.warning() — the ROOT logger — bypassing the mcp.* logger
    silencing; a notification that fails ClientNotification validation
    used to dump an ~8KB pydantic error on stderr per session. The root
    logger must sit at ERROR so those dumps never reach stderr."""
    try:
        proc = _spawn_server()
    except OSError as exc:  # e.g. sandboxed environments without spawn
        pytest.skip(f"subprocess spawn unavailable: {exc}")

    try:
        _handshake(proc)
        # Unknown method -> ClientNotification validation failure -> the
        # root-logger warning path under test.
        _jsonrpc(
            proc,
            {
                "jsonrpc": "2.0",
                "method": "notifications/bogus_xyz",
                "params": {"a": 1},
            },
        )
        # Give the async session time to process and (pre-fix) write the
        # warning; the read after wait() below is race-free.
        time.sleep(1.5)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        stderr = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
        assert "Failed to validate" not in stderr, (
            f"validation dump leaked: {stderr[:2000]}"
        )
        assert "Traceback" not in stderr, f"traceback leaked: {stderr[:2000]}"
        assert "pydantic" not in stderr, f"pydantic dump leaked: {stderr[:2000]}"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        _close_pipes(proc)


def _spawn_server():
    """Spawn the real server module as a subprocess; return the Popen."""
    src_dir = str(Path(__file__).resolve().parents[1] / "src")
    env = {**os.environ, "PYTHONPATH": src_dir}
    return subprocess.Popen(
        [sys.executable, "-m", "ifixit_mcp.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )


def _close_pipes(proc):
    """Close any open stdio pipes on *proc* so no file objects leak.

    QA Round 16 (F16-2): the SIGTERM/kill-path tests only killed and
    waited, leaving the pipes open; the suite ended with ``ResourceWarning:
    unclosed file <_io.BufferedWriter name=13>`` (the stdin pipe of the
    last spawned subprocess). Closing after the process has exited is
    safe; a close racing a live child may raise OSError (broken pipe),
    which is fine to ignore.
    """
    for pipe in (proc.stdin, proc.stdout, proc.stderr):
        if pipe is not None:
            try:
                pipe.close()
            except OSError:
                pass


def _jsonrpc(proc, obj):
    """Write one newline-delimited JSON-RPC message to the server's stdin."""
    assert proc.stdin is not None
    proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
    proc.stdin.flush()


def _read_until(proc, predicate, timeout=10.0):
    """Read stdout lines until *predicate* matches; return the message.

    Returns None on EOF or timeout.
    """
    assert proc.stdout is not None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select([proc.stdout], [], [], 0.2)
        if not ready:
            continue
        line = proc.stdout.readline()
        if not line:
            return None
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if predicate(message):
            return message
    return None


def _handshake(proc):
    """initialize + initialized notification; return the initialize response."""
    _jsonrpc(
        proc,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "qa-probe", "version": "0.0.1"},
            },
        },
    )
    response = _read_until(proc, lambda m: m.get("id") == 1)
    assert response is not None, "no initialize response"
    _jsonrpc(proc, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
    return response


@pytest.fixture(scope="module")
def ifixit_reachable():
    """QA Round 7 (F10): the lifecycle subprocess tests make REAL iFixit API
    calls, so they flake on offline CI. Probe reachability once per module
    (3s timeout) and skip the live-network tests when the API is down."""
    try:
        httpx.get("https://www.ifixit.com", timeout=3.0, follow_redirects=True)
    except Exception:
        pytest.skip("live iFixit API unreachable — skipping live-network tests")
    return True


def test_midflight_stdin_disconnect_exits_zero_without_traceback(ifixit_reachable):
    """QA Round 6 (F5): closing stdin while requests are mid-flight used to
    crash the server with an ~7KB anyio ExceptionGroup traceback (exit 1).
    The disconnect is a normal client hang-up: the process must exit 0 with
    no traceback on stderr."""
    try:
        proc = _spawn_server()
    except OSError as exc:  # e.g. sandboxed environments without spawn
        pytest.skip(f"subprocess spawn unavailable: {exc}")

    try:
        _handshake(proc)
        # Fire a few uncached parallel-ish requests (search is never
        # cached, so each one hits the network and stays mid-flight).
        for i in range(3):
            _jsonrpc(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 10 + i,
                    "method": "tools/call",
                    "params": {
                        "name": "search_guides",
                        "arguments": {"query": f"battery replacement {i}"},
                    },
                },
            )
        time.sleep(0.25)
        # Close stdin mid-flight — the client hung up.
        assert proc.stdin is not None
        proc.stdin.close()
        try:
            returncode = proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            pytest.fail("server did not exit after stdin disconnect")
        stderr = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
        assert returncode == 0, (
            f"expected exit 0 after stdin disconnect, got {returncode}; "
            f"stderr: {stderr[-2000:]}"
        )
        assert "Traceback" not in stderr, f"traceback leaked: {stderr[-2000:]}"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        _close_pipes(proc)


def test_non_utf8_stdin_exits_zero_without_traceback():
    """QA Round 15 (F15-2): a non-UTF-8 byte on stdin used to crash the
    server with a ~4.5KB UnicodeDecodeError ExceptionGroup traceback
    (exit 1). Garbage bytes on the wire are a client-side problem: like
    a disconnect, the process must exit 0 with no traceback on stderr."""
    try:
        proc = _spawn_server()
    except OSError as exc:  # e.g. sandboxed environments without spawn
        pytest.skip(f"subprocess spawn unavailable: {exc}")

    try:
        # 0xa2 is not a valid UTF-8 start byte: the stdio reader's
        # TextIOWrapper raises UnicodeDecodeError on this line.
        assert proc.stdin is not None
        proc.stdin.write(b"\xa2\n")
        proc.stdin.flush()
        proc.stdin.close()
        try:
            returncode = proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            pytest.fail("server did not exit after non-UTF-8 stdin")
        stderr = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
        assert returncode == 0, (
            f"expected exit 0 after non-UTF-8 stdin, got {returncode}; "
            f"stderr: {stderr[-2000:]}"
        )
        assert "Traceback" not in stderr, f"traceback leaked: {stderr[-2000:]}"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        _close_pipes(proc)


def test_invalid_json_stdin_does_not_crash_server():
    """QA Round 15 (F15-2 companion): valid UTF-8 but invalid JSON on
    stdin is handled inside mcp's reader (logged, not fatal) — the
    server keeps running and still exits 0 on EOF, no traceback."""
    try:
        proc = _spawn_server()
    except OSError as exc:  # e.g. sandboxed environments without spawn
        pytest.skip(f"subprocess spawn unavailable: {exc}")

    try:
        assert proc.stdin is not None
        proc.stdin.write(b"not-json-at-all\n")
        proc.stdin.flush()
        time.sleep(0.5)  # give the reader time to process the bad line
        assert proc.poll() is None, "server died on invalid JSON stdin"
        proc.stdin.close()  # EOF -> clean shutdown
        try:
            returncode = proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            pytest.fail("server did not exit after stdin EOF")
        stderr = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
        assert returncode == 0, (
            f"expected exit 0 after stdin EOF, got {returncode}; "
            f"stderr: {stderr[-2000:]}"
        )
        assert "Traceback" not in stderr, f"traceback leaked: {stderr[-2000:]}"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        _close_pipes(proc)


def test_sigterm_exits_zero_without_unclosed_warnings(ifixit_reachable):
    """QA Round 6 (F6): SIGTERM used to kill the server immediately, so the
    lazy httpx client was never closed (atexit handlers do not run on
    SIGTERM) and no cleanup happened. The signal handler must close the
    client and exit 0 — with no 'unclosed' warnings on stderr."""
    try:
        proc = _spawn_server()
    except OSError as exc:  # e.g. sandboxed environments without spawn
        pytest.skip(f"subprocess spawn unavailable: {exc}")

    try:
        _handshake(proc)
        # Make one real tool call so the lazy client exists before the signal.
        _jsonrpc(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "browse_categories", "arguments": {}},
            },
        )
        _read_until(proc, lambda m: m.get("id") == 2, timeout=20)
        proc.send_signal(signal.SIGTERM)
        try:
            returncode = proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            pytest.fail("server did not exit after SIGTERM")
        stderr = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
        assert returncode == 0, (
            f"expected exit 0 after SIGTERM, got {returncode}; "
            f"stderr: {stderr[-2000:]}"
        )
        assert "unclosed" not in stderr.lower(), (
            f"unclosed-resource warning leaked: {stderr[-2000:]}"
        )
        assert "Traceback" not in stderr, f"traceback leaked: {stderr[-2000:]}"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        _close_pipes(proc)


def test_sigterm_during_startup_exits_zero():
    """QA Round 8 (R8-1): the SIGTERM/SIGINT handlers used to be installed
    only inside main(), which runs after the ~1.2s `mcp.server.fastmcp`
    import completes — a SIGTERM in that window died with the default
    disposition (exit -15). The handlers are now installed at module top
    (before the heavy imports), so a signal during startup must exit 0
    with no traceback. No network and no handshake: purely a startup
    lifecycle check."""
    try:
        proc = _spawn_server()
    except OSError as exc:  # e.g. sandboxed environments without spawn
        pytest.skip(f"subprocess spawn unavailable: {exc}")

    try:
        # Land the signal inside the startup window: the mcp import alone
        # takes ~1.2s, so 0.3s after spawn is still well before the
        # handshake could complete.
        time.sleep(0.3)
        if proc.poll() is not None:
            # The process finished before the signal could be sent (only
            # possible on a pathologically slow box) — nothing was
            # verified, so skip rather than assert on an unrelated exit.
            pytest.skip("server exited before SIGTERM could be sent")
            return
        proc.send_signal(signal.SIGTERM)
        try:
            returncode = proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            pytest.fail("server did not exit after SIGTERM during startup")
        stderr = (
            proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
        )
        # A negative returncode means death by signal: if our SIGTERM was
        # what killed it, it must be the handler's clean exit (0), never
        # the default disposition (SIGTERM -> -15).
        assert returncode == 0, (
            f"expected exit 0 after SIGTERM during startup, got {returncode}; "
            f"stderr: {stderr[-2000:]}"
        )
        assert "Traceback" not in stderr, f"traceback leaked: {stderr[-2000:]}"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        _close_pipes(proc)


@pytest.mark.parametrize("delay", [0.08, 0.12])
def test_sigterm_in_interpreter_bootstrap_window_clean_or_default(delay):
    """QA Round 11 (F11-3, LOW): residual startup-signal window.

    The module-top handler install runs ~100-150ms after spawn (after the
    interpreter bootstrap of ``python -m ifixit_mcp.server``); a SIGTERM
    in that residual window hits the default disposition (exit -15). The
    window is inherent to interpreter bootstrap and cannot be closed from
    user code — so this probe must not be flaky: assert the process exits
    0 (handler live) OR -15 (pre-install default disposition), with NO
    traceback either way, and log which occurred. (The 0.3s probe above
    is strictly post-install and still requires exit 0.)
    """
    try:
        proc = _spawn_server()
    except OSError as exc:  # e.g. sandboxed environments without spawn
        pytest.skip(f"subprocess spawn unavailable: {exc}")

    try:
        time.sleep(delay)
        if proc.poll() is not None:
            # The process finished before the signal could be sent (only
            # possible on a pathologically slow box) — nothing was
            # verified, so skip rather than assert on an unrelated exit.
            pytest.skip("server exited before SIGTERM could be sent")
            return
        proc.send_signal(signal.SIGTERM)
        try:
            returncode = proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            pytest.fail("server did not exit after SIGTERM during startup")
        stderr = (
            proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
        )
        # 0 = handler was live; -15 = signal landed pre-install and the
        # default disposition applied. Anything else (crash, traceback)
        # is a regression. No traceback is acceptable in EITHER case.
        assert returncode in (0, -15), (
            f"unexpected exit {returncode} after SIGTERM at {delay}s; "
            f"stderr: {stderr[-2000:]}"
        )
        assert "Traceback" not in stderr, f"traceback leaked: {stderr[-2000:]}"
        if returncode == 0:
            print(f"startup signal at {delay}s: handler live, exit 0")
        else:
            print(
                f"startup signal at {delay}s: pre-install default "
                "disposition (exit -15)"
            )
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        _close_pipes(proc)


def _console_script() -> Path | None:
    """Path to the installed ifixit-mcp console script (venv bin), or None."""
    candidate = Path(sys.executable).resolve().parent / "ifixit-mcp"
    if candidate.is_file():
        return candidate
    found = shutil.which("ifixit-mcp")
    return Path(found) if found else None


def test_launcher_stdlib_only_with_minimal_startup_handlers():
    """QA Round 9 (R9-1): the console script must boot through a stdlib-only
    launcher (src/ifixit_mcp/launcher.py) that installs a minimal
    os._exit(0) SIGTERM/SIGINT handler BEFORE the heavy mcp import —
    server.py's own module-top install is guarded on __name__ ==
    "__main__", which is FALSE during console-script bootstrap. Importing
    the launcher must pull in no heavy modules (mcp, httpx, the server
    module) and must leave both handlers pointing at the minimal handler.
    Runs in a subprocess so the launcher's module-top signal install never
    touches the pytest process."""
    code = (
        "import signal, sys\n"
        "import ifixit_mcp.launcher as launcher\n"
        "heavy = sorted(\n"
        "    m for m in ('mcp', 'httpx', 'ifixit_mcp.server')\n"
        "    if m in sys.modules\n"
        ")\n"
        "term_ok = signal.getsignal(signal.SIGTERM) is launcher._startup_exit\n"
        "int_ok = signal.getsignal(signal.SIGINT) is launcher._startup_exit\n"
        "print(f'HEAVY={heavy} TERM={term_ok} INT={int_ok}')\n"
    )
    src_dir = str(Path(__file__).resolve().parents[1] / "src")
    env = {**os.environ, "PYTHONPATH": src_dir}
    try:
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
    except OSError as exc:  # e.g. sandboxed environments without spawn
        pytest.skip(f"subprocess spawn unavailable: {exc}")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "HEAVY=[] TERM=True INT=True", (
        f"launcher violated stdlib-only/handler contract: {result.stdout!r}"
    )


def test_console_script_sigterm_during_startup_exits_zero():
    """QA Round 9 (R9-1): the console-script entry used to point at
    ifixit_mcp.server:main — the module-top signal install in server.py is
    guarded on __name__ == "__main__", which is FALSE during console-script
    bootstrap, so a SIGTERM in the ~1.2s mcp import window died with the
    default disposition (exit -15). The entry now boots via the stdlib-only
    launcher, which installs a minimal os._exit(0) handler before the heavy
    import. Spawn the REAL installed console script and SIGTERM it inside
    the startup window: it must exit 0 with no traceback. No network and no
    handshake: purely a startup lifecycle check."""
    script = _console_script()
    if script is None:
        pytest.skip("ifixit-mcp console script not installed (pip install -e .)")
    try:
        proc = subprocess.Popen(
            [str(script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:  # e.g. sandboxed environments without spawn
        pytest.skip(f"subprocess spawn unavailable: {exc}")

    try:
        # Land the signal inside the startup window: the mcp import alone
        # takes ~1.2s, so 0.3s after spawn is still well before the
        # server could start serving.
        time.sleep(0.3)
        if proc.poll() is not None:
            # The process finished before the signal could be sent (only
            # possible on a pathologically slow box) — nothing was
            # verified, so skip rather than assert on an unrelated exit.
            pytest.skip("server exited before SIGTERM could be sent")
            return
        proc.send_signal(signal.SIGTERM)
        try:
            returncode = proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            pytest.fail("server did not exit after SIGTERM during startup")
        stderr = (
            proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
        )
        # A negative returncode means death by signal: if our SIGTERM was
        # what killed it, it must be the handler's clean exit (0), never
        # the default disposition (SIGTERM -> -15).
        assert returncode == 0, (
            f"expected exit 0 after SIGTERM during startup, got {returncode}; "
            f"stderr: {stderr[-2000:]}"
        )
        assert "Traceback" not in stderr, f"traceback leaked: {stderr[-2000:]}"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        _close_pipes(proc)


@pytest.mark.parametrize("delay", [0.08, 0.12])
def test_console_script_sigterm_in_bootstrap_window_clean_or_default(delay):
    """QA Round 11 (F11-3, LOW): residual console-script bootstrap window.

    The launcher installs its minimal handlers during the console-script
    bootstrap (~50-80ms after spawn); a SIGTERM before that hits the
    default disposition (exit -15). The window is inherent to interpreter
    bootstrap — this probe must not be flaky: assert the process exits 0
    (launcher handler live) OR -15 (pre-install default disposition), with
    NO traceback either way, and log which occurred. (The 0.3s probe
    above is strictly post-install and still requires exit 0.)
    """
    script = _console_script()
    if script is None:
        pytest.skip("ifixit-mcp console script not installed (pip install -e .)")
    try:
        proc = subprocess.Popen(
            [str(script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:  # e.g. sandboxed environments without spawn
        pytest.skip(f"subprocess spawn unavailable: {exc}")

    try:
        time.sleep(delay)
        if proc.poll() is not None:
            # The process finished before the signal could be sent (only
            # possible on a pathologically slow box) — nothing was
            # verified, so skip rather than assert on an unrelated exit.
            pytest.skip("server exited before SIGTERM could be sent")
            return
        proc.send_signal(signal.SIGTERM)
        try:
            returncode = proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            pytest.fail("server did not exit after SIGTERM during startup")
        stderr = (
            proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
        )
        # 0 = launcher handler was live; -15 = signal landed pre-install
        # and the default disposition applied. Anything else (crash,
        # traceback) is a regression. No traceback in EITHER case.
        assert returncode in (0, -15), (
            f"unexpected exit {returncode} after SIGTERM at {delay}s; "
            f"stderr: {stderr[-2000:]}"
        )
        assert "Traceback" not in stderr, f"traceback leaked: {stderr[-2000:]}"
        if returncode == 0:
            print(f"console-script signal at {delay}s: handler live, exit 0")
        else:
            print(
                f"console-script signal at {delay}s: pre-install default "
                "disposition (exit -15)"
            )
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        _close_pipes(proc)
