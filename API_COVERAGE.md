# API Coverage Audit — iFixit MCP Server

> **Date:** 2026-07-31 · **Auditor:** Task 9 (final verification + coverage audit)
> **Scope:** Every read endpoint in `RESEARCH.md` §2 (endpoint inventory) cross-referenced against the implemented client (`src/ifixit_mcp/client.py`, 10 methods) and server (`src/ifixit_mcp/server.py`, 8 FastMCP tools).
> **Result:** **11 / 18 covered** (10 via MCP tools, 1 client-only) · **7 documented exclusions** · **0 genuine gaps**.

## Coverage table

| Endpoint (RESEARCH.md §2) | Tool / client method | Status | Notes |
|---|---|---|---|
| `GET /guides/{guideid}` | `get_guide` (tool) | ✅ covered | `detail=summary\|full`, `max_steps` truncation, `lang` (langid) passthrough, HTML→plain text under honest `*_text` keys; cached 30 min (§2, §3a). |
| `GET /guides` | `list_guides` (client) | ✅ covered — client-only | Implemented + tested in client, **not exposed as a tool** — see Exclusion 1. |
| `GET /suggest/{query}` | `search_guides` (tool) | ✅ covered | `doctypes` + `guideDevice` + `lang` (langid) params; ≤10 results, never cached (§3c, §4). |
| `GET /categories` | `browse_categories` (tool) | ✅ covered | ~1.5 MB tree cached 30 min; names-only projection, raw tree never leaves client; empty path = top level; navigation reads the cached tree without deep-copying (§3d). |
| `GET /wikis/{namespace}/{title}` | `get_device` + `list_device_guides` (tools) | ✅ covered | Two tools, one endpoint, different projections (device overview vs. guide list); `parts_count` sums the live object-shape category counts; `ancestors` projected to names (§3e). |
| `GET /wikis/maintenance/{namespace}/{title}` | `get_maintenance_schedule` (tool) | ✅ covered | Returns `schedules[]` with triggers (e.g. `battery_health_percent`), `inherited_from` when inherited, `{"schedules": []}` (HTTP 200) when none (§2). |
| `GET /users/{userid}` | `get_user` (tool) | ✅ covered | Profile incl. reputation, join_date, `badge_counts` (§3f). |
| `GET /users/{userid}/guides` | `get_user(include_guides=True)` (tool) | ✅ covered | Guides projected to `{guideid, title, url}`; `limit` param (1-200, default 20). |
| `GET /users/{userid}/badges` | — | ⛔ excluded | See Exclusion 2. |
| `GET /teams/{teamid}` | — | ⛔ excluded | See Exclusion 3. |
| `GET /teams` | — | ⛔ excluded | See Exclusion 3. |
| `GET /tags` | — | ⛔ excluded | See Exclusion 4. |
| `GET /media/images/{id}` | `get_media` (tool) | ✅ covered | CDN size URLs; 403 → "not accessible" (§3g). |
| `GET /media/videos/{id}` | `get_media` (tool) | ✅ covered | Same tool, `media_type="videos"`. |
| `GET /media/documents/{id}` | `get_media` (tool) | ✅ covered | Same tool, `media_type="documents"`. |
| `GET /comments` | — | ⛔ excluded | See Exclusion 5. |
| `GET /stories` | — | ⛔ excluded | See Exclusion 6. |
| `GET /stories/{id}` | — | ⛔ excluded | See Exclusion 6. |

**Summary:** 10 endpoints covered by MCP tools, 1 covered at client level (`GET /guides`), 7 excluded with documented reasons. Every row of the plan's Task 9 coverage checklist (`.hermes/plans/2026-07-29_ifixit-mcp.md`) is addressed above. No endpoint is excluded for auth reasons — all §2 read endpoints are anonymous (§1).

## Exclusion rationale (must be defensible — references RESEARCH.md)

1. **`GET /guides` as a tool** — Paginated browse by guide *type* only, with **no keyword filter** (§4: "browse by guide type, paginated, but no keyword filter"; params per §2: `guideids`, `filter`, `order`, `offset`, `limit`, `modifiedSince`). An agent's discovery needs are already covered by `search_guides` (`/suggest`, §4 option 1) and device-scoped `list_device_guides` (§4 option 3). Exposing an unranked page of up to 200 summaries (limit clamps to 200, §5) invites context bloat without a query to focus it. Kept as client method `list_guides` (tested) for programmatic completeness.

2. **`GET /users/{userid}/badges`** — Redundant with the profile: `GET /users/{userid}` already returns `badge_counts: {bronze, silver, gold, total}` (§3f), which is the agent-relevant aggregate for a contributor profile. Per-badge detail adds no repair-content value. Additionally, §1's live-verified anonymous set includes `/users/{id}` but **not** `/users/{id}/badges`, so this endpoint was never exercised during research.

3. **`GET /teams` + `GET /teams/{teamid}`** — Peripheral to the server's mission (repair guides, device info, repairability scores, categories, media, contributors). §2: `/teams/{teamid}` "returns array of members (not paginated)"; §3f: user profiles already embed `teams[]`, so team membership is visible without a dedicated endpoint. Team-member browsing is not a repair-lookup operation; both are absent from the plan's recommended tool set (§11).

4. **`GET /tags`** — No agent-usable path: §2 lists it as a bare paginated tag list (`offset`/`limit`/`order`, plain array without totals per §5), and **no endpoint filters guides by tag** (guide summaries carry no tags, §3b; `/guides` has no tag param, §2). An unbounded tag list with no way to act on it has low utility and would invite context bloat.

5. **`GET /comments`** — Standalone site-wide comment browsing (context filter: guide/step/wiki/info/post/story/wp_post, §2) is not a repair-lookup operation, and guide-level comment context is already available inside `get_guide`: step objects carry `comments[]` and `comment_count` (§3a). Low marginal utility.

6. **`GET /stories` + `GET /stories/{id}`** — §2 classifies Stories as "blog-ish" content: marketing/editorial, not repair data. Outside the mission scope defined in the plan (guides, devices, repairability, categories, media, contributors) and absent from §11's recommended tool set. Low utility.

## Verification evidence (2026-07-31, refreshed after QA Round 15)

| Check | Command | Result |
|---|---|---|
| Full test suite | `python3 -m pytest tests/ -q` | **542 passed** in ~20s |
| Git state | `git status --short` | clean (after QA Round 1 fixes) |
| Server boot | `timeout 3 python3 -m ifixit_mcp.server < /dev/null` | **exit=0**, no traceback |
| MCP stdio handshake | real JSON-RPC `initialize` over stdin/stdout (newline-delimited JSON framing, mcp 1.x transport) | **OK** — response `serverInfo: {name: "ifixit", version: "0.1.0"}`, `protocolVersion: 2024-11-05`, `tools` capability advertised, BY-NC-SA attribution instructions echoed |

## Findings (QA Round 1 resolved)

- **No genuine coverage gaps.** Every endpoint the plan's Task 9 checklist requires is either tool-covered or explicitly excluded above with reasons tied to RESEARCH.md specifics.
- **Resolved (Round 1):** `serverInfo.version` is now pinned to the package version `0.1.0` (imported from `ifixit_mcp.__version__`, guarded against future mcp internals) — the old observation about `1.26.0` no longer applies.
- **Resolved (Round 1):** the stdio handshake is now covered by an automated test (`tests/test_server.py::test_subprocess_stdio_handshake_reports_server_info`), which also corrected the transport framing detail: the mcp 1.x stdio transport uses newline-delimited JSON, not Content-Length headers.
- **Resolved (Round 7):** the plan's Task 9 checklist described `GET /guides → list_guides` as "used internally by tools", but no server tool calls `list_guides` — it is client-only. The plan wording has been corrected; functional coverage is unaffected (the method exists and is tested).
