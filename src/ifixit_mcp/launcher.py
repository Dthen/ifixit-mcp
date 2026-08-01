"""Stdlib-only launcher for the ifixit-mcp console script.

IMPORT-FOOTGUN WARNING: importing this module (even just ``import
ifixit_mcp.launcher``) installs process-wide SIGTERM/SIGINT handlers (a
minimal ``os._exit(0)`` handler) as a module-top side effect. There is
deliberately NO ``__main__`` guard: the console-script bootstrap must
have the handlers live before the heavy ``mcp`` import, and module-top
code is the only place that runs early enough. Library consumers
embedding ifixit-mcp should import ``ifixit_mcp.server`` (and call
``server.main()``) instead — importing this launcher will hijack the
embedding process's SIGTERM/SIGINT disposition.

QA Round 9 (R9-1): the console-script entry used to point at
``ifixit_mcp.server:main``. server.py installs its startup signal
handlers at module top guarded on ``__name__ == "__main__"`` — which is
FALSE during console-script bootstrap (``from ifixit_mcp.server import
main``) — so a SIGTERM/SIGINT in the ~1.2s ``mcp.server.fastmcp`` import
window died with the default disposition (exit -15).

This module is deliberately STDLIB-ONLY (signal, os, sys) so the
handlers below are live before ANY heavy import. The minimal handler
just ``os._exit(0)``s: during the startup window there is no client yet
(the httpx client is created lazily on the first tool call, which cannot
happen before the server starts serving), so no cleanup is needed.
server.main() re-installs its full handler (close client + exit) as its
first act, replacing the minimal one before the server serves anything.

``python -m ifixit_mcp.server`` and direct-file runs keep working
unchanged: server.py's own module-top install covers those paths.
Residual window (QA Round 11, F11-3): a signal in the first ~100ms of
interpreter bootstrap — before the module-top install above runs — may
hit the default disposition (exit -15); signals after startup are
handled cleanly.
"""

from __future__ import annotations

import os
import signal
import sys


def _startup_exit(signum: int, frame: object) -> None:
    """Minimal startup-window handler: exit immediately, no cleanup.

    Only live until server.main() installs its full handler. os._exit
    skips interpreter shutdown entirely, which is required here: the
    stdio worker thread may be parked in readline(), and a normal exit
    would deadlock threading._shutdown.
    """
    os._exit(0)


# Install BEFORE the heavy mcp import below (inside main()): a signal in
# the ~1.2s import window must exit 0, never die with the default
# disposition. Module top (not main()) so the handlers are live from the
# very first byte of launcher execution, including the console-script
# bootstrap's own import of this module.
signal.signal(signal.SIGTERM, _startup_exit)
signal.signal(signal.SIGINT, _startup_exit)


def main() -> None:
    """Console-script entry point: import the server, then run it.

    The minimal handlers installed at module top stay live for the whole
    server import; server.main() re-installs its full handler (close the
    client, then exit) before it starts serving, replacing them.
    """
    from ifixit_mcp.server import main as server_main

    server_main()


if __name__ == "__main__":
    main()
