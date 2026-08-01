# iFixit MCP Server

Full-coverage iFixit MCP server — repair guides, device info, repairability scores, categories, search, media, and contributor profiles in one server.

> ⚠️ **LICENSE — READ FIRST**
>
> **Two licenses apply — do not confuse them:**
>
> 1. **The code** in this repository is licensed under the **0-Clause BSD License (0BSD)** — free to use, copy, modify, and distribute for any purpose, **including commercial use**.
> 2. **The data** the server surfaces comes from the iFixit API and **remains iFixit content** under a **non-commercial CC BY-NC-SA license**. Commercial use of iFixit data requires contacting `api@ifixit.com` for pricing, and per iFixit's Terms of Service: **"Training Large Language Models on iFixit content is prohibited."**
>
> This server is a **read-only, on-demand lookup tool** — like a search engine returning snippets, not bulk ingestion for training. It deliberately:
>
> - **Never bulk-downloads or persistently caches the guide corpus.** Every request fetches on demand; the in-memory cache is bounded (256 entries) and mirrors iFixit's own CDN TTL (30 min).
> - **Has no LLM-training features** — no dataset export, no bulk endpoints, no scraping modes.
> - **Attributes iFixit** in the server description and every tool description (CC BY-NC-SA).
>
> Your 0BSD rights cover the code only, not iFixit's content: keep your use of iFixit data non-commercial, and contact iFixit first if your use case is commercial or involves training.

## Why this server?

Research found **no existing iFixit MCP servers** (see [RESEARCH.md §8](RESEARCH.md)); this is the first server to cover the **full public read surface** of the iFixit API 2.0:

- **Search** across guides, wikis, questions, products (`/suggest`)
- **Repair guides** — summary or full detail, with steps, parts, and tools
- **Device wiki pages** — repairability scores (when iFixit has published one), featured guides, parts/tools counts
- **Category tree** — ~16 top-level categories down to nested sub-devices
- **Maintenance schedules** — battery/SSD health triggers and other upkeep tasks
- **Media CDN URLs** — images, videos, documents by id
- **Contributor profiles** — reputation, badges, and guide lists
- **8 tools**, all **anonymous and read-only** — zero configuration, no API keys, no tokens

## Quick start

```bash
cd /home/kimbo/projects/ifixit-mcp
# Any Python 3.10+ venv works; this example uses the Hermes agent venv
/mnt/HC_Volume_105667182/kimbo/.hermes/hermes-agent/venv/bin/python3 -m pip install -e '.[dev]'
```

Run the server (stdio):

```bash
ifixit-mcp                                  # console script
# or
python -m ifixit_mcp.server                 # module entry point
```

## Configuration

### Hermes Agent

Add to your Hermes `config.yaml`:

```yaml
mcp_servers:
  ifixit:
    command: /mnt/HC_Volume_105667182/kimbo/.hermes/hermes-agent/venv/bin/python3
    args: ['-m', 'ifixit_mcp.server']
    env:
      PYTHONPATH: /home/kimbo/projects/ifixit-mcp/src
```

### Claude Desktop

Add to `claude_desktop_config.json` — **no npx, no Node.js**: this is a pure-Python stdio server:

```json
{
  "mcpServers": {
    "ifixit": {
      "command": "/mnt/HC_Volume_105667182/kimbo/.hermes/hermes-agent/venv/bin/python3",
      "args": ["-m", "ifixit_mcp.server"],
      "env": {"PYTHONPATH": "/home/kimbo/projects/ifixit-mcp/src"}
    }
  }
}
```

Any Python 3.10+ interpreter works in place of the Hermes venv path, as long as the package is installed (or `PYTHONPATH` points at `src/`). No environment variables, API keys, or tokens are required for any tool.

## Tool reference

All tools are read-only and anonymous. Errors are **raised** and delivered to the MCP client as error results with `isError: true` — never returned as success strings and never as stack traces. The wire message is `Error executing tool <name>: <family prefix>: <reason>` (e.g. `Error executing tool get_guide: Guide lookup failed: Guide not found: 1220`).

| Tool | Params | Returns |
|------|--------|---------|
| `search_guides` | `query`, `device?`, `doctypes="guide"`, `lang?` | `{query, results}` — up to 10 search results; guide results compacted to `{guideid, title, url, type, difficulty, summary}` with summary truncated to 200 chars; other types (wiki, question, product) passed through. `lang` (e.g. `"de"`) requests localized results. Never cached. |
| `get_guide` | `guideid`, `detail="summary"`, `max_steps?`, `lang?` | Guide metadata, parts/tools lists, and step titles only (`summary`, default) or the full guide (`full`). In both modes `*_rendered` HTML is converted to plain text and renamed `*_text` (step lines: `text`); summary steps with empty titles fall back to the first line's text (teardown guides often have blank titles); `max_steps` truncates the steps list in `full` mode. `lang` (e.g. `"de"`) requests a localized guide. Cached 30 min. |
| `browse_categories` | `path?` | ~16 top-level category names (no path), or the child category names of a subtree (e.g. `"Mac/Mac Laptop"`); an empty path means top level. Only names are returned — the raw ~1.5 MB tree never leaves the client. Cached 30 min. |
| `get_device` | `title` | Compact device overview: `title`, `display_title`, `repairability_score` (included only when iFixit has published one — it is `null`/absent for many devices), `summary` (first 500 chars), `featured_guides` (title/guideid/url), `children` (names only), `parts_count` (list length or, for the live object shape `{url, categories:[{tag,count}]}`, the sum of the category counts), `tools_count`, `ancestors` (breadcrumb names only). Cached 30 min. |
| `list_device_guides` | `title` | The device's guides and featured guides (deduplicated) as a compact list — each entry `{guideid, title, url, difficulty, time_required_max, image_thumbnail}`, optional fields omitted when absent. Cached 30 min. |
| `get_maintenance_schedule` | `title` | `{schedules: [...]}` — maintenance tasks and their triggers (e.g. `battery_health_percent`); `{schedules: []}` (HTTP 200) when the device has none; `inherited_from` (parent device name) when the schedule is inherited. Cached 30 min. |
| `get_media` | `media_id`, `media_type="images"` | The media object with CDN size URLs (`mini`, `thumbnail`, `standard`, `original`, ...); ungenerated sizes are absent. `media_type` ∈ images/videos/documents. Cached 1 hour. |
| `get_user` | `user_id`, `include_guides=False`, `limit=20` | Contributor profile (`username`, `reputation`, `join_date`, `badge_counts`, ...); with `include_guides=True`, merges the user's guides projected to `{guideid, title, url}` (up to `limit` guides, 1-200). Cached 30 min. |

## Response-size management

iFixit's raw API responses are **far too large for LLM context budgets**, so every tool compacts what it returns:

| Raw API payload | Size | What the server returns |
|-----------------|------|-------------------------|
| Full guide (`/guides/{id}`) | ~25 KB small guides, **100 KB+ for large ones** (prerequisite steps inlined, full HTML, comments, flags) | `summary` mode: metadata + parts/tools + step titles only (empty titles fall back to the first line's text); `full` mode: `*_raw` markup stripped, `*_rendered` HTML converted to plain text and renamed `*_text` (step lines: `text`), `max_steps` truncation |
| Category tree (`/categories`) | **~1.5 MB** nested object | Projected name lists only — top-level names or one subtree's children; the tree itself never leaves the client |
| Device wiki page (`/wikis/CATEGORY/{title}`) | **~238 KB**, 39 keys | 9-field projection: repairability score, 500-char summary, featured-guide stubs, child names, parts/tools counts, ancestors |
| Search results (`/suggest/{q}`) | Mixed guide/wiki/question objects | Guide results projected to 6 fields with **summary truncated to 200 chars at the tool layer**; other types already small, passed through |

Beyond projection, the client keeps memory bounded:

- **Bounded TTL cache** — 256-entry in-memory cache, oldest entries evicted first. TTLs mirror iFixit's CDN edge TTL (observed `x-debug-ttl: 1800`): 30 min for guides/devices/categories/schedules/users, 1 hour for media. Entries are deep-copied on read and write so callers can never corrupt cached data (category-tree *navigation* reads the cached tree without copying — it is strictly read-only).
- **Cache stampede protection** — concurrent identical requests share one in-flight upstream call; a failed fetch clears its marker so retries work.
- **Volatile endpoints never cached** — search (`/suggest`) and paginated lists (`/guides`, `/users/{id}/guides`) bypass the cache entirely.
- **No bulk access** — there is no tool that enumerates the corpus; every tool is a targeted, on-demand lookup.

## Development

Test-driven workflow: client behavior → server tool wiring → end-to-end tool tests.

```bash
# Install with dev dependencies
pip install -e '.[dev]'

# Run the full suite (542 tests)
pytest tests/ -v
```

Project layout:

```
ifixit-mcp/
├── src/ifixit_mcp/
│   ├── __init__.py
│   ├── client.py      # IfixitClient — async httpx client, all API logic
│   ├── launcher.py    # stdlib-only entry point: installs startup signal
│   │                  #   handlers before importing mcp, then delegates to
│   │                  #   server.main()
│   └── server.py      # FastMCP tool definitions (thin wrappers + projections)
├── tests/
│   ├── conftest.py
│   ├── test_client.py
│   ├── test_server.py
│   └── test_tools.py
├── RESEARCH.md        # API research findings (live-tested with curl)
├── openapi.json       # Official iFixit API 2.0 OpenAPI 3.1 spec (1.16 MB)
└── pyproject.toml
```

## Architecture

```
LLM / MCP client
      │  (JSON-RPC over stdio)
      ▼
launcher.py ── stdlib-only console-script entry (ifixit-mcp command)
      │        • installs startup signal handlers BEFORE importing mcp
      │        • delegates to server.main()
      ▼
server.py  ── 8 × @mcp.tool() async wrappers
      │        • input validation, response projection
      │        • every exception → raised error (isError: true on the
      │          wire, message "<prefix>: <reason>", never a traceback)
      │        • lazy shared client (created on first tool call, closed at exit)
      ▼
client.py  ── IfixitClient (async httpx → https://www.ifixit.com/api/2.0)
              • descriptive User-Agent on every request (no fabricated URL)
              • bounded in-memory TTL cache (256 entries, deep-copied)
              • concurrency-safe rate limiter (0.5 s minimum interval, locked)
              • cache stampede dedup (one upstream call per concurrent key)
              • 429 retry with exponential backoff (honors Retry-After, capped at 8s, ≤3 retries)
              • error mapping: 400/401 → ValueError, 403 → ForbiddenError,
                404 → NotFoundError (both ValueError subclasses)
              • response compaction: _summarize_guide, _full_guide,
                _project_device, _compact_guide_item, _html_to_text
```

- **`launcher.py`** — the stdlib-only console-script entry (backing the `ifixit-mcp` command): installs startup signal handlers before the heavy `mcp` import, then delegates to `server.main()` (which re-installs the full close-client handler before serving). **Import-footgun:** importing `ifixit_mcp.launcher` (rather than running the `ifixit-mcp` command) installs process-wide SIGTERM/SIGINT handlers (`os._exit(0)`) as an import-time side effect, with no `__main__` guard — library consumers embedding ifixit-mcp should import `ifixit_mcp.server` instead.
- **Startup signals:** a SIGTERM/SIGINT in the first ~100ms of interpreter bootstrap (before the module-top handler install runs) may hit the default disposition (exit -15); signals after startup are handled cleanly (exit 0, client closed).
- **`client.py`** — `IfixitClient` owns all HTTP, caching, rate limiting, retry, and response compaction. Every method validates its inputs *before* any network I/O, and translates 404s into per-resource messages (`Guide not found: 1220`, `Device not found: iPhone`, ...).
- **`server.py`** — a FastMCP server (`ifixit`) with 8 thin async tools. Each tool delegates to the client, applies the final compact projection (e.g. the 200-char search summary truncation), and converts every exception into a raised error (`Error executing tool <name>: <prefix>: <reason>` on the wire, `isError: true`). The server's instructions and every tool description carry the iFixit data attribution (CC BY-NC-SA).

## Known limitations

- Every tool parameter is string-typed with explicit coercion: JSON numbers are coerced to strings and then validated (`get_guide` with `guideid: 1220` works; `search_guides` with `query: 0` searches for `"0"`); JSON booleans and junk types are rejected, and nulls are rejected for required params (omitted for optional ones) — all with clean family messages, never silent `true → 1` fetches, searches for the literal `"None"`, or raw pydantic dumps. The one residual library-level leak: FastMCP (mcp 1.26) exposes no validation-error hook, so a **missing required parameter** (e.g. calling `search_guides` with no arguments at all) still surfaces as FastMCP's own `[type=missing]` pydantic dump — there is no hook to intercept it. Everything the client or tool body can see is converted to a clean message.

## License

This project has a **dual license structure** — the code and the data it serves are licensed separately:

- **Code** — the software in this repository is licensed under the **0-Clause BSD License (0BSD)**: free to use, copy, modify, and distribute for any purpose, with or without fee (SPDX: `0BSD`). [https://opensource.org/license/0bsd](https://opensource.org/license/0bsd) · the full text is in [LICENSE](LICENSE).
- **Data** — the iFixit content surfaced through this server (guides, wiki pages, media, and other API responses) remains iFixit's content under the **Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International (CC BY-NC-SA 4.0)** license: non-commercial use only, attribution required, and LLM training on iFixit content is prohibited per iFixit's Terms of Service. [https://creativecommons.org/licenses/by-nc-sa/4.0/](https://creativecommons.org/licenses/by-nc-sa/4.0/)

The 0BSD grant covers the code only. This server is a read-only, on-demand lookup tool — it never bulk-caches the guide corpus, and nothing in the code license authorizes bulk-ingesting or training on iFixit content.

## Links

- [RESEARCH.md](RESEARCH.md) — full API research: endpoint inventory, verified response shapes, licensing analysis (§0)
- [openapi.json](openapi.json) — official iFixit API 2.0 OpenAPI 3.1 spec (56 paths, 53 schemas)
- [iFixit API docs](https://www.ifixit.com/api-docs) — official API documentation and licensing terms
