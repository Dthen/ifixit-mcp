# iFixit API — Research Findings for MCP Server

> Research date: 2026-07-29 · API version: **2.0** · Base: `https://www.ifixit.com/api/2.0`
> Source of truth: official OpenAPI 3.1 spec at `https://www.ifixit.com/api/nextjs/openapi` (1.16 MB, 56 paths, 53 schemas). Saved locally as `openapi.json`.
> All endpoints below were **live-tested with curl --max-time 15, unauthenticated**, and returned HTTP 200.

---

## 0. CRITICAL LICENSING / POLICY WARNING ⚠️

From the official API description (verbatim):

- The API is provided under a **non-commercial BY-NC-SA license**. Commercial use requires contacting `api@ifixit.com` for pricing.
- **"Training Large Language Models on iFixit content is prohibited by our Terms of Service."**

**Implication for this MCP server:** An MCP server that surfaces iFixit guide content *to an AI assistant at a user's request* is a different use case than *training* an LLM on the corpus, but it is adjacent. This is a **read-only, on-demand lookup tool** (like a search engine returning snippets), not bulk ingestion for training. Still:
- Keep the server's use of iFixit data **non-commercial** (BY-NC-SA) — the code is 0BSD, but the data it serves stays non-commercial.
- Do **not** build any feature that bulk-dumps/caches the entire guide corpus locally (that looks like dataset assembly for training). Fetch on demand, cache lightly (respect the 1800s CDN TTL), and attribute iFixit.
- Surface the license/attribution in the server README and ideally in tool descriptions.
- This is a judgment call worth flagging to the user before heavy investment.

---

## 1. Authentication

**Most public read endpoints require NO authentication.** This is the single most important finding — the entire read surface this MCP server needs works anonymously.

- Docs: *"You don't need an app ID for most requests."* / *"Most public endpoints do not require authentication. A few endpoints require an App ID."*
- **There is NO self-service API key / X-App-Id header flow.** The task brief's assumption ("free registration + X-App-Id header") is **outdated/wrong for API 2.0**. That was the legacy API. In 2.0:
  - An **App ID** exists but is only for "complex integrations," requested by emailing `api@ifixit.com`. Not needed for read access.
  - **User auth** (for write endpoints + viewer-only fields) is email/password → token:
    ```
    POST /user/token  {"email":"...","password":"***"}  → {"authToken":"..."}
    Authorization: api ***    # NOTE: scheme is "api", NOT "Bearer"
    ```
- **Verified unauthenticated (HTTP 200):** `/guides/{id}`, `/guides`, `/suggest/{q}`, `/categories`, `/tags`, `/users/{id}`, `/teams/{id}`, `/wikis/{ns}/{title}`, `/media/images/{id}`, `/media/videos/{id}`, `/comments`.
- **Verified requires auth:**
  - `GET /user` → `401 {"message":"Invalid login"}` (current-user endpoint).
  - `POST /user/token` with bad creds → `400 {"message":"Invalid app id"}`.
- **Conclusion:** Build the MCP server **fully unauthenticated / read-only**. No key management, no env vars for secrets required. Optionally support an `IFIXIT_AUTH_TOKEN` env var later if user-context features are wanted — not needed for v1.

---

## 2. Endpoint inventory (56 paths)

Base URL: `https://www.ifixit.com/api/2.0`. All return **JSON by default** (`?pretty` for formatted). Error shape: `{"error":true,"message":"..."}` (note: some 404s return `{"message":"Endpoint not found"}` without the `error` key).

### READ endpoints relevant to the MCP server

| Endpoint | Purpose | Auth | Notes |
|---|---|---|---|
| `GET /guides/{guideid}` | **Full guide** (steps, parts, tools, images) | No | The money endpoint. ~25 KB for a small guide; large guides much bigger. |
| `GET /guides` | List guide **summaries** | No | Params: `guideids` (CSV, first 50 only), `filter` (type), `order`, `offset`, `limit`, `modifiedSince`. |
| `GET /suggest/{query}` | **SEARCH** across guides/wikis/etc. | No | The de-facto search endpoint. See §4. |
| `GET /categories` | Full category **tree** | No | 1.49 MB nested tree, 16 top-level categories. Cache aggressively. |
| `GET /wikis/{namespace}/{title}` | Wiki / **device CATEGORY page** | No | ns enum: `WIKI,CATEGORY,INFO,ITEM,USER,TEAM`. Rich device info. |
| `GET /wikis/maintenance/CATEGORY/{title}` | Maintenance schedules | No | Returns `schedules[]` w/ triggers (e.g. battery_health_percent). |
| `GET /users/{userid}` | User profile | No | reputation, badge_counts, join_date. |
| `GET /users/{userid}/guides` | Guides by a user | No | |
| `GET /users/{userid}/badges` | User badges | No | |
| `GET /teams/{teamid}` | Team members | No | Returns array of members (not paginated). |
| `GET /teams` | List teams | No | |
| `GET /tags` | List tags | No | offset/limit/order. |
| `GET /media/images/{imageid}` | Image info + CDN URLs | No | 403 if exists but not viewable; 404 unknown. |
| `GET /media/videos/{videoid}` | Video info + CDN URLs | No | |
| `GET /media/documents/{id}` | Document info | No | |
| `GET /comments` | List comments | No | context filter: guide/step/wiki/info/post/story/wp_post. |
| `GET /stories`, `/stories/{id}` | "Stories" (blog-ish) | No | |

### Endpoints NOT in API 2.0 (task-brief assumptions that are wrong)
- ❌ `/guides/search?q=` → **404.** Search is `GET /suggest/{query}`.
- ❌ `/devices` and `/devices/{device}` → **do not exist.** Devices are wiki pages in the `CATEGORY` namespace (`/wikis/CATEGORY/{Device Name}`) and nodes in the `/categories` tree.
- ❌ `/wikis/{id}` (by numeric id) → it's `/wikis/{namespace}/{title}`.
- ❌ `/media/{id}` (generic) → split into `/media/images|videos|documents/{id}`.

### Write endpoints (NOT needed — skip for read-only MCP)
`POST/PATCH/DELETE /guides...`, `/guides/{id}/completed`, `/guides/{id}/public`, `/guides/{id}/steps`, `/guides/{id}/tag`, `/guides/{id}/teams`, `/guides/{id}/users`, `/user/media/images`, `/user/favorites/guides`, `/wikis` (create/edit/revert), `/teams/{id}/repair_services`, `/users/reset_password`, etc. These require auth + privileges; out of scope.

---

## 3. Response shapes (real, verified)

### 3a. Full guide — `GET /guides/1220`
Top-level keys (44): `author, available_langids, can_edit, category, comments, completed, conclusion_raw, conclusion_rendered, created_date, difficulty, documents, favorited, featured_document_embed_url, featured_document_thumbnail_url, featured_documentid, flags, guideid, image, intro_video, intro_video_url, introduction_raw, introduction_rendered, langid, locale, modified_date, parts, patrol_threshold, prereq_modified_date, prerequisites, public, published_date, revisionid, steps, subject, summary, time_required, time_required_max, time_required_min, title, tools, type, url`.

Key fields:
- `title`, `summary`, `type` (enum: replacement/installation/repair/disassembly/teardown/technique/maintenance/how-to), `category`, `subject`.
- `difficulty`: localized string, e.g. `"Moderate"` (also Easy/Hard/Very difficult; null when unset).
- `time_required`: human string (`"30 minutes"`, `"No estimate"`); `time_required_min`/`_max` in **seconds** (0 when no estimate). Summary objects only carry `time_required_max`.
- `introduction_raw` (wiki markup) + `introduction_rendered` (HTML); same pair for `conclusion`.
- `steps[]`: each = `{stepid, guideid, orderby (1-indexed), revisionid, title, lines[], media, comments[], comment_count}`.
  - `lines[]` = `{text_raw (wiki markup), text_rendered (HTML), bullet, level (0-2), lineid}`. `bullet` enum: black/red/orange/yellow/light_blue/blue/green/violet/icon_note/icon_caution/icon_reminder.
  - `media` = `{type: image|video|embed, data: [...]}`. Image data items carry full CDN size URLs.
  - **Prerequisite guide steps are inlined into `steps` by default** (important for size).
- `parts[]` = `{type, quantity, text, notes, url, thumbnail, isoptional, featured}`.
- `tools[]` = same shape as parts (e.g. `{"text":"Phillips #0 Screwdriver","quantity":1,"url":".../products/phillips-0-screwdriver",...}`).
- `prerequisites[]`, `flags[]` (e.g. `GUIDE_USER_CONTRIBUTED`, `GUIDE_STARRED`), `image` (CDN sizes), `author` (`{userid, username, unique_username, join_date, image, reputation, url, teams}`), `available_langids[]`.

### 3b. Guide summary (list items, `/guides` and `/suggest` guide results)
`dataType:"guide", guideid, locale, revisionid, modified_date, prereq_modified_date, url, type, category, subject, title, summary, difficulty, time_required_max, public, userid, username, flags[], image{...}`.

### 3c. Search — `GET /suggest/{query}`
Response: `{query, results[]}`. Up to 10 results, spread across requested doctypes.
- `doctypes` param (CSV): `guide, item, topic, device, category, question, post, all`. **`device` and `category` are aliases for `topic`; `post` is alias for `question`; `all` (default) = topic+guide.** Invalid values ignored; no valid doctypes → 400.
- Other params: `langid`, `fallbackToDefaultLanguage`, `showAllLanguages`, `guideDevice` (restrict to one device by name).
- Special: query `guideid:<id>` bypasses search and returns that single guide.
- Result object varies by `dataType` discriminator (`guide`/`wiki`/question/product/page). Guide results = full GuideSummary. Wiki results = `{dataType:"wiki", wikiid, namespace, langid, title, display_title, summary, text, url, modified_date, author, image}`.

### 3d. Categories — `GET /categories`
1.49 MB nested object. Keys = canonical category wiki titles; values = nested child trees or `null`. **16 top-level:** `Mac, Game Console, Phone, Camera, Vehicle, Household, Electronics, Tablet, Appliance, Tool, PC, Computer Hardware, Skills, Car and Truck, Apparel, Medical Device`. Excellent electronics coverage (consoles, phones, laptops/Mac, tablets, appliances all first-class).

### 3e. Wiki / device page — `GET /wikis/CATEGORY/iPhone` (238 KB)
Keys (39): `ancestors, author, authors, available_langids, can_edit, category_info, category_lists, children, contents_json, contents_raw, contents_rendered, created_date, description, diagrams, display_title, documents, featured_guides, flags, guides, image, info, is_troubleshooting, langid, linked_wikis, maintenance_schedule, modified_date, namespace, page_title, parts, related_wikis, repairability_score, revisionid, solutions_url, source_revisionid, tags, title, tools, wikiid`.
- **`repairability_score`** is exposed here (valuable!). `featured_guides`, `guides`, `parts`, `tools`, `maintenance_schedule`, `children` (sub-devices), `ancestors` (breadcrumb).

### 3f. User — `GET /users/1`
`{userid, username, unique_username, image{...}, teams[], reputation, join_date, certification_count, badge_counts:{bronze,silver,gold,total}, summary, ...}`.

### 3g. Media — `GET /media/images/14056`
`{image:{id, guid, mini, thumbnail, 140x105, 200x150, standard, 440x330, medium, large, huge, original}}`. Image object always has `id, guid, original`; ungenerated sizes omitted.

---

## 4. Search strategy (no dedicated search endpoint)

There is **no global full-text search with filters/pagination**. Options:
1. **`GET /suggest/{query}`** — best for "find guides/devices about X." Returns ≤10 mixed results. Supports `doctypes=guide` to focus, and `guideDevice=` to scope to a device. This is the primary search tool.
2. **`GET /guides?filter=<type>`** — browse by guide type, paginated, but no keyword filter.
3. **`GET /wikis/CATEGORY/{device}`** then read its `guides`/`featured_guides` — device-scoped guide listing.

For an MCP server, `suggest` (keyword) + `wikis/CATEGORY` (device browse) + `guides` (filter/browse) cover the discovery needs.

---

## 5. Pagination

- List endpoints (`/guides`, `/tags`, `/comments`, `/users`, `/stories`): **offset/limit**. `limit` default **20**, max **200** (verified: `limit=500` clamps to 200). Increment `offset` by `limit` until fewer items than requested return. Plain arrays, no cursor, no total count, no `next` link.
- `/teams/{id}` members: **not paginated** (full list).
- `/categories`: single full tree (no pagination).
- `/suggest`: fixed ≤10 results (no pagination).
- **Guide count estimate:** binary-search via offset → **~61,000–61,500 public guides** total (offset 61000 returns data, 61500 empty).

---

## 6. Rate limits

- Docs only say: *"API requests are rate limited. When limits are exceeded, the API returns a 429."* **No documented numbers, no key tiers.**
- **No rate-limit response headers** observed (no `X-RateLimit-*`, no `Retry-After`). Responses are served via **CloudFront + Varnish** with `x-debug-ttl: 1800` (30-min cache) and `x-debug-cache: HIT`.
- **Practical test:** 30 rapid sequential requests → all `200`, no 429. The heavy CDN caching means read traffic is cheap on their side.
- **Guidance for MCP server:** be polite anyway — add a small concurrency cap and basic 429 backoff (exponential, honor `Retry-After` if ever present). Cache `/categories` and hot guide lookups in-process for ~30 min to mirror their TTL.

---

## 7. oManual / XML

- Underlying data model is **IEEE 1874 (oManual)** — a standard for procedural manuals (XML or JSON, offline bundle or REST).
- API 2.0 returns **JSON by default**. Docs claim *"Some endpoints additionally support XML via `Accept: application/xml`."*
- **Tested:** `Accept: application/xml` on `/guides/1220` and `/suggest` → **still returned JSON** (`content-type` JSON). XML support appears absent/inconsistent in 2.0 for these endpoints. **Recommendation: treat the API as JSON-only.** (Legacy ProgrammableWeb notes mention JSON/JSONP/XML + oManual, but that's the old API.)
- An **offline oManual archive** is mentioned ("an archived collection of oManual files is also available") for offline tools — not needed for a live MCP server, and bulk download conflicts with the no-LLM-training posture anyway.

---

## 8. Existing MCP servers — NONE FOUND ✅ (green field)

- **GitHub repo search:** `ifixit mcp` → **0 results**; `ifixit mcp server` → **0 results**. 173 `ifixit` repos total, none MCP. Notable existing (non-MCP) libs: `xiongchiamiov/pyfixit` (Python wrapper for the **legacy Dozuki** API, ★17), `openzim/ifixit` (ZIM scraper), `xiongchiamiov/ifixit-repairability-scores`.
- **mcp.so / smithery.ai / glama.ai:** no iFixit server surfaced (mcp.so is Cloudflare-challenged; searches returned nothing relevant).
- **Conclusion:** No known existing iFixit MCP server. This would be novel. `pyfixit` targets the *old* API and is not MCP — not directly reusable but confirms interest.

---

## 9. Data coverage

- **~61,000+ public guides.** Types: replacement, installation, repair, disassembly, teardown, technique, maintenance, how-to.
- **16 top-level categories** with strong electronics depth: **Game Console, Phone, Mac, PC, Tablet, Electronics, Appliance, Camera, Computer Hardware**, plus Vehicle, Household, Tool, Skills, Medical Device, Apparel, Car and Truck.
- Consoles (PS5/Xbox/Switch via "Game Console"), phones (iPhone/Android), laptops (Mac/PC), appliances all first-class. Device pages expose **repairability scores**, parts, tools, featured guides, maintenance schedules.
- Multi-language: guides/wikis carry `available_langids`; `langid` param selects language.

---

## 10. Limitations / what you CAN'T do

- **No keyword search with filters/pagination** — only `suggest` (≤10 results).
- **No parts store / commerce API, no Answers Q&A database** access (confirmed by legacy docs; 2.0 has no parts-search endpoint — parts only appear *inside* guides/wikis).
- **No bulk export** via API (pagination only; ~61k guides would need 300+ pages).
- **Auth-gated:** current-user data, favorites, completions, all writes.
- **Media access is permission-checked:** `/media/*` returns 403 if the asset isn't referenced by content you can view.
- **XML unreliable** — plan for JSON only.
- **Images/CDN:** served from `guide-images.cdn.ifixit.com` (pattern: `/igi/{guid}.{size}` — sizes: mini/thumbnail/140x105/200x150/standard/440x330/medium/large/huge/original). Product thumbnails from `cart-products.cdn.ifixit.com`. CDN URLs are public/direct-linkable; no auth needed to fetch the image bytes.
- **LLM-training prohibition** (see §0) — the big legal caveat.

---

## 11. MCP server design recommendation

Pattern (matches user's existing servers): `src/ifixit_mcp/{client.py, server.py}`, `httpx` async, `FastMCP`, `pytest`. **No auth required** → client is a thin `httpx.AsyncClient` wrapper over `https://www.ifixit.com/api/2.0`. Optional `IFIXIT_AUTH_TOKEN` env (passthrough `Authorization: api *** only if user-context tools are added later.

### Recommended tool set (7 tools)

| Tool | Endpoint(s) | Purpose | Size handling |
|---|---|---|---|
| `search_guides` | `GET /suggest/{q}?doctypes=guide` | Keyword search for repair guides (+ optional `device` scope via `guideDevice`) | Small (≤10). Return title, guideid, device, difficulty, time, url, thumbnail. |
| `get_guide` | `GET /guides/{id}` | Full step-by-step guide | **LARGE** — see below. |
| `browse_categories` | `GET /categories` (cached) | Discover device taxonomy | **1.5 MB** — never return raw. Return top-level list, or children of a named category. |
| `get_device` | `GET /wikis/CATEGORY/{title}` | Device overview: repairability score, featured guides, parts, sub-devices | Large (238 KB) — project to a summary. |
| `list_device_guides` | `GET /wikis/CATEGORY/{title}` → `guides`/`featured_guides` | Guides for a specific device | Medium — return compact list. |
| `get_image` / `get_media` | `GET /media/images/{id}` (videos/documents) | Resolve media CDN URLs | Small. |
| `get_user` | `GET /users/{id}` (+ `/users/{id}/guides`) | Contributor profile + their guides | Small. |

Optional 8th: `get_maintenance_schedule` → `GET /wikis/maintenance/CATEGORY/{title}` (battery-health triggers etc.) — nice differentiator.

### Response-size concerns (the main engineering risk)
- Full guides (25 KB–hundreds of KB) and especially `/categories` (1.5 MB) and device wikis (238 KB) will **blow MCP/LLM context budgets** if returned raw.
- **Mitigations:**
  - `get_guide`: add a `detail` param (`summary` | `full`, default `summary`). Default returns metadata + parts/tools + step *titles/count*; `full` returns rendered step text. Strip `*_raw` (keep `text_rendered` or a plain-text strip of HTML), drop `revisionid`/`patrol_threshold`/`can_edit`/`comments` unless asked. Offer `max_steps`/pagination over steps. Convert HTML → markdown to shrink.
  - `browse_categories`: cache the tree in-process (30-min TTL); expose only the requested subtree or top-level names.
  - `get_device`: project to `{title, repairability_score, summary, featured_guides[compact], children[], parts_count}`.
  - Central `client.py` helper to prune/compact JSON before returning to FastMCP.
- Images: return CDN URLs (small strings), never bytes.

### Client.py shape
```
class IfixitClient:
    BASE = "https://www.ifixit.com/api/2.0"
    async def get(self, path, **params) -> dict/list   # httpx, timeout=15, 429 backoff
    async def get_guide(guideid, lang=None)
    async def search(query, doctypes="guide", device=None)
    async def categories()  # cached
    async def wiki(namespace, title)
    ...
```
Tests: pytest with `respx`/`httpx.MockTransport` fixtures seeded from the real JSON saved in `tests/fixtures/` (we have `g1220.json`, `cats.json`, `wiki.json`, `sug.json` captured during research).

---

## Appendix: verified curl examples

```bash
# Full guide (no auth)
curl --max-time 15 "https://www.ifixit.com/api/2.0/guides/1220"

# Search guides
curl --max-time 15 "https://www.ifixit.com/api/2.0/suggest/iphone?doctypes=guide"

# Device-scoped search
curl --max-time 15 "https://www.ifixit.com/api/2.0/suggest/battery?doctypes=guide&guideDevice=iPhone%2013"

# Category tree (1.5 MB — cache it)
curl --max-time 15 "https://www.ifixit.com/api/2.0/categories"

# Device wiki page (repairability score etc.)
curl --max-time 15 "https://www.ifixit.com/api/2.0/wikis/CATEGORY/iPhone"

# Maintenance schedule
curl --max-time 15 "https://www.ifixit.com/api/2.0/wikis/maintenance/CATEGORY/iPhone"

# Paginated guide list (limit clamps to 200)
curl --max-time 15 "https://www.ifixit.com/api/2.0/guides?limit=200&offset=0"

# User / team / media / tags
curl --max-time 15 "https://www.ifixit.com/api/2.0/users/1"
curl --max-time 15 "https://www.ifixit.com/api/2.0/teams/1"
curl --max-time 15 "https://www.ifixit.com/api/2.0/media/images/14056"
curl --max-time 15 "https://www.ifixit.com/api/2.0/tags?limit=50"
```

**Error codes:** 400 bad params · 401 auth required · 403 forbidden · 404 not found · 429 rate limited.
