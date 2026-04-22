# CLAUDE.md — Project Guide for AI Assistants

## What This Project Is

RO-ED AI Agent — document intelligence system that extracts structured data from import/export PDF documents. Master + Column Agent architecture with LLM verification, fee verification, and self-learning. Built by City AI Team — City Holdings Myanmar.

## Tech Stack

- **Frontend:** SvelteKit 5 (Svelte 5 runes) + TailwindCSS 4.2
- **Backend:** FastAPI 0.115 + Uvicorn (Python 3.12)
- **Database:** SQLite with WAL mode + 30s busy_timeout, 25+ tables
- **AI Models:** OpenRouter API (per-step model config)
  - Vision + Assembler: Google Gemini 3 Flash Preview (page extraction + table building)
  - Verifier: Anthropic Claude Sonnet 4.6 (cross-checks against images)
  - Fee Verifier: Gemini 3 Flash (text-based fee mapping verification)
- **Auth:** Dual-mode — Local JWT (HS256) + Keycloak OIDC (RS256 via PKCE)
- **Container:** Docker (single service, 4GB RAM, non-root user)
- **PDF Processing:** PyMuPDF (fitz) at 300 DPI + Pillow enhancement. NO Tesseract.

## Project Structure

```
RO-ED-Lang/
├── CLAUDE.md / README.md
├── Dockerfile / docker-compose.yml
├── frontend/
│   └── src/
│       ├── lib/
│       │   ├── api.ts                     # API client (handles redirect + auth)
│       │   ├── colors.ts                  # Theme + dynamic page type colors
│       │   └── components/
│       │       ├── ResultAccordion.svelte  # THE main component — tables, PDF, search, map, log
│       │       ├── PipelineVisualizer.svelte # Flow diagram (step boxes with arrows)
│       │       ├── AgentTerminal.svelte    # CLI-style streaming log
│       │       ├── PageDetail.svelte       # Per-page image + extracted data
│       │       └── Header.svelte           # Navigation
│       └── routes/
│           ├── agent/        # Upload + extract + results (persisted via localStorage)
│           ├── history/      # Job list + detail (uses ResultAccordion)
│           ├── review/       # Confidence-based queue (≥95% auto, <80% escalate)
│           ├── items/        # All items across jobs
│           ├── declarations/ # All declarations across jobs
│           ├── costs/        # Cost tracking + charts
│           ├── settings/     # Users + Auth + Groups
│           └── login/
└── backend/
    ├── main.py              # FastAPI app (v3.0.0, CORS *, redirect handling)
    ├── config.py            # Models: VISION_MODEL, ASSEMBLER_MODEL, VERIFIER_MODEL
    ├── auth.py / database.py / schemas.py / middleware.py
    ├── routes/
    │   ├── auth.py          # Login, JWT, Keycloak
    │   ├── jobs.py          # Upload (10 files max, 50MB each), details, Excel, annotated PDF
    │   ├── ws.py            # WebSocket — real-time pipeline streaming
    │   ├── corrections.py   # User corrections → few-shot learning + fee baseline auto-save
    │   ├── data.py / users.py / settings.py / groups.py
    ├── pipeline/            # THE extraction pipeline
    │   ├── splitter.py      # PDF → 300 DPI HD images (PyMuPDF + Pillow)
    │   ├── vision.py        # Per-page agents (parallel, semaphore=16) + QA gate + explicit fee labels
    │   ├── assembler.py     # Master + Column Agents:
    │   │                      Declaration Master (16 column agents, json_schema)
    │   │                      Items Master (10 column agents, json_schema)
    │   │                      QA after each + cross-validation + item dedup
    │   │                      verify_fees_with_llm() — text-based fee verification
    │   │                      _fix_fee_shift() — 7-layer deterministic fallback
    │   │                      Token-optimized (dedup fields, no metadata)
    │   ├── verifier.py      # Claude Sonnet cross-checks against page images
    │   └── pipeline.py      # Orchestrator (fee verify → memory cleanup)
    ├── v2/                  # Shared utilities
    │   ├── confidence.py    # Per-field confidence scoring
    │   ├── step5_report.py  # Save results to DB (per-job files)
    │   └── step4_validate.py
    └── agents/
        └── advanced.py      # Myanmar language, PDF annotation
```

## Pipeline Architecture

```
PDF Upload
  → Step 1:  SPLITTER          — PDF → HD images (300 DPI)
  → Step 2:  VISION AGENTS     — Per-page (gemini-3-flash, parallel, 8 workers)
  → Step 3:  VISION QA         — Re-run bad pages only
  → Step 4:  DECLARATION AGENT — 16 column agents (json_schema enforced)
  → Step 5:  DECLARATION QA    — Re-run missing fields only
  → Step 6:  ITEMS AGENT       — 10 column agents (json_schema enforced)
  → Step 7:  ITEMS QA          — Re-run missing fields only
  → Step 8:  ITEM DEDUP        — Remove duplicate items (same name + HS code)
  → Step 9:  PRICE FALLBACK    — Copy Invoice↔CIF price if one is missing
  → Step 10: CROSS-VALIDATION  — Items sum = declaration total
  → Step 11: VERIFIER          — Claude Sonnet checks against page images
  → Step 12: FEE VERIFY        — Text LLM verifies fee-label mapping (deterministic fallback)
  → Save to DB + Excel
  → Max 50 pages per PDF (memory guard)
```

## Models

| Step | Model | Cost |
|------|-------|------|
| Vision (per page) | google/gemini-3-flash-preview | ~$0.002/page |
| Declaration + Items + QA | google/gemini-3-flash-preview | ~$0.02 |
| Verifier | anthropic/claude-sonnet-4-6 | ~$0.12 |
| Fee Verifier | google/gemini-3-flash-preview | ~$0.002 |
| **Total** | | **~$0.15-0.20/PDF** |

## Key Design Principles

- **Zero hardcoded values** — no field names, currencies, tax codes in code
- **Zero calculations** — every value read directly from document
- **json_schema enforced** — guarantees valid JSON, all required fields present
- **Token optimized** — deduplicated fields, no metadata/visual/entities sent to assembler
- **Correction Memory** — user corrections feed back as few-shot examples
- **Fee self-learning** — user corrections auto-save fee baselines per importer
- **Per-job isolation** — each job gets own files, own cost tracker, no global state
- **Memory cleanup** — image data freed after verifier step
- **Fee verification** — text LLM verifies fee mapping + 7-layer deterministic fallback (see below)

## Fee Verification (Step 12)

LLMs consistently shift fee/tax values down by 1 position when reading Myanmar customs documents:
- CT→AT, SF→MF, MF→Exemption (CT and SF become 0)

**Primary fix:** `verify_fees_with_llm()` — a TEXT-BASED LLM call (no images, ~$0.002) that reads the raw vision output and matches fee labels to values. The vision agent reads labels correctly ("Security: 0", "Commercial Tax: 93,794") — the shift happens in the assembler mapping. Text-based verification avoids the visual layout confusion entirely.

**Fallback:** `_fix_fee_shift()` deterministic correction if LLM fee verifier fails or returns low confidence (<0.7). Has 7 safety layers:
0. **Importer baseline** — if user corrected fees before, use verified values from `importer_profiles.fee_baseline_json`
1. **Page text cross-check** — searches raw vision output for fee labels + values (multiple label patterns)
2. **Pattern detection** — 4 patterns (B: SF→MF with Exempt, C: SF→MF fee-sized, A: CT→AT with corroboration, D: full rotation with AT=0)
3. **Page text override** — blocks false positives when document confirms current values
4. **Deterministic shift-back** — CT=AT, AT=0, SF=MF, MF=Exempt, Exempt=0
5. **Post-fix sanity check** — reverts ALL changes if any fee exceeds customs value or CT > 5x duty
6. **Audit trail** — logs every change to `value_audit` table with stage="fee_shift_fix"

**Self-learning:** When user corrects a fee field → `corrections.py` auto-saves fee baseline to `importer_profiles.fee_baseline_json`. Future extractions for same importer use baseline as ground truth.

**Don't:**
- Don't use vision/images for fee verification — text avoids layout confusion and is 10x cheaper
- Don't skip deterministic fallback — LLM verifier may fail or return low confidence
- Don't remove importer baseline — it's the most reliable signal (human-verified)
- Don't let Pattern A (CT shift) fire standalone — requires `sf_shifted or sf == 0` corroboration
- Don't remove post-fix sanity check — it auto-reverts impossible values

## Output Tables — Column Definitions

### Items Table (13 columns — synced across UI, Excel per-job, Excel all-items)

| Column | DB Key | Source |
|--------|--------|--------|
| Job | job_id | Auto-generated |
| Item Name | item_name | Assembler |
| Customs Duty Rate | customs_duty_rate | Assembler (decimal, e.g. 0.15 = 15%) |
| Quantity (1) | quantity | Assembler (with unit, e.g. "17,280 KG") |
| Invoice Unit Price | invoice_unit_price | Commercial invoice (FOB/supplier price, lower) |
| CIF Unit Price | cif_unit_price | Customs declaration (CIF price, higher) |
| Currency | currency | From declaration |
| Commercial Tax % | commercial_tax_percent | Assembler (decimal, e.g. 0.05 = 5%) |
| Exchange Rate (1) | exchange_rate | From declaration |
| HS Code | hs_code | Assembler |
| Origin Country | origin_country | Assembler |
| Customs Value (MMK) | customs_value_mmk | Assembler |
| Processed | created_at | Auto-generated |

**Invoice Unit Price vs CIF Unit Price:** Two different prices from two different pages.
Invoice = supplier FOB price (from commercial invoice). CIF = customs-assessed price (from customs declaration, includes freight+insurance).
If one is missing, the other is copied as fallback so both columns always have a value.

### Declaration Table (18 columns)

| Column | DB Key | Source |
|--------|--------|--------|
| Job | job_id | Auto-generated |
| Declaration No | declaration_no | Assembler (STRING, not float) |
| Date | declaration_date | Assembler |
| Importer | importer_name | Assembler |
| Consignor | consignor_name | Assembler |
| Invoice Number | invoice_number | Assembler |
| Invoice Price | invoice_price | Assembler |
| Currency | currency | Assembler |
| Exchange Rate | exchange_rate | Assembler |
| Currency 2 | currency_2 | Assembler (fallback: Currency) |
| Customs Value | total_customs_value | Assembler |
| Duty | import_export_customs_duty | Assembler |
| Tax (CT) | commercial_tax_ct | Fee verifier / assembler |
| Income Tax (AT) | advance_income_tax_at | Fee verifier / assembler |
| Security (SF) | security_fee_sf | Fee verifier / assembler |
| MACCS (MF) | maccs_service_fee_mf | Fee verifier / assembler |
| Exemption | exemption_reduction | Fee verifier / assembler |
| Processed | created_at | Auto-generated |

**All 3 Excel exports (per-job, all-items, all-declarations) match these columns.**

## Concurrency (10 users)

- SQLite WAL mode + 30s busy_timeout
- API semaphore: max 16 simultaneous OpenRouter calls
- Per-job file isolation (no overwrites)
- Docker: 4GB RAM, 2 CPUs
- Upload: max 10 files/batch, 50MB/file
- Uvicorn: concurrency limit 20, keep-alive 300s

## Duplicate Detection

Upload API hashes PDF (SHA256), checks DB. User sees: VIEW RESULTS (free) | RE-PROCESS (double confirmation) | CANCEL.

## UI Architecture

ResultAccordion is THE main component (agent + history):
- Tabs: RESULTS | PDF (ANNOTATED) | PIPELINE LOG
- RESULTS: Pipeline Visualizer → KPIs → Confidence → Insights → Items table → Declaration table → Corrections Log → Document Summary → Field Search → Document Map (expandable)

## Production Deployment

```bash
cp .env.example .env   # Fill in OPENROUTER_API_KEY + JWT_SECRET_KEY
./start-docker.sh      # Builds frontend, validates config, starts Docker
```

## Don't

- Don't use Tesseract
- Don't add calculations to prompts
- Don't hardcode field names, currencies, or patterns
- Don't commit `.env`
- Don't use `sqlite3.connect()` — use `database._connect()`
- Don't use bare `except:` — use `except Exception:`
- Don't mutate global state
- Don't skip pages (data could be anywhere)
- Don't use `json_object` mode (9x slower than `json_schema`)
- Don't use `res.json()` in `api.ts` — use `res.text()` + `JSON.parse()` (see troubleshooting)
- Don't use `{@const}` and reference it outside its block scope in Svelte
- Don't mutate `$state` array entries in-place for view transitions — replace with new object
- Don't convert Declaration No to float — it's a string field (see STRING_FIELDS in assembler.py)
- Don't save Currency 2 with key `Currency.1` — the assembler uses `Currency 2`
- Don't use vision/images for fee verification — text-based avoids layout confusion and is 10x cheaper
- Don't skip deterministic fee fallback — LLM verifier may return low confidence
- Don't let Pattern A (CT shift) fire without corroboration from SF shift
- Don't define columns in only one place — sync UI, Excel exports, and API schemas together

## Known Issues & Troubleshooting

### 1. `res.json()` hangs in browser (fetch returns 200 but body never arrives)

**Symptom:** Page shows "LOADING..." forever. Server logs show `200 OK`. curl works fine.
**Root cause:** Starlette's SPA middleware wraps API responses in a streaming proxy. In some browsers (confirmed Brave), `res.json()` hangs reading the response body stream even though headers arrive.
**Fix:** In `api.ts`, always use `res.text()` + `JSON.parse()` instead of `res.json()`. This reads the body as a complete string first (more reliable), then parses.
**How to diagnose:** Add `debugMsg` state + visible output showing fetch progress:
```javascript
debugMsg = 'calling fetch...';
const res = await fetch(url, opts);
debugMsg = `fetch done: ${res.status}`; // If stuck here → res.json() is the problem
const text = await res.text();
debugMsg = `text: ${text.length} chars`;
```

### 2. Svelte 5 `$derived` doesn't re-render after `$state` array mutation

**Symptom:** Queue shows "DONE" but pipeline view stays visible. Counter shows wrong number.
**Root cause:** `$derived(queue[selectedIndex])` returns the same object reference even after mutating its properties. Svelte 5 skips re-render if the derived value reference hasn't changed.
**Fix:** Replace the array entry with a new object instead of mutating:
```javascript
// BAD: entry.status = 'done'; queue = [...queue];
// GOOD: queue[idx] = { ...entry, status: 'done' }; queue = [...queue];
```
**Alternative:** Use explicit `$state` for view mode (`viewMode = 'results'`) instead of deriving from array entries. Simple strings always trigger re-render.

### 3. `{@const}` scoping in Svelte 5

**Symptom:** `ReferenceError: <variable> is not defined` in browser console.
**Root cause:** `{@const}` is scoped to its enclosing `{#if}`, `{#each}`, or `{#snippet}` block. Using it outside that block causes a runtime crash.
**Fix:** Either move the `{@const}` into the correct block, inline the expression, or use a `$derived` at the component script level.

### 4. WebSocket data delivery vs HTTP fetch

**Symptom:** After pipeline completes, results don't load (even though API works).
**Root cause:** The separate HTTP `getJob()` call after pipeline completion hits issue #1.
**Fix:** Send full `job_data` inline in the WebSocket `file_complete` message from `ws.py`. The frontend receives results directly — no HTTP call needed. Fallback to HTTP `getJob()` only if `msg.job_data` is null.

### 5. FastAPI trailing slash redirects (307)

**Symptom:** 401 Unauthorized on some API calls. Works in curl but not browser.
**Root cause:** FastAPI routes with trailing slash (`/users/`) redirect requests without slash (`/users`) with 307. Browser strips `Authorization` header on redirect.
**Fix:** Match frontend API paths exactly to backend route definitions (include trailing slash where defined).

### 6. Fee values shifted (CT/AT/SF/MF wrong)

**Symptom:** CT=0 but AT has CT's value, or SF=0 but MF has SF's value.
**Root cause:** LLMs shift fee values down by 1 position due to Myanmar customs document layout.
**Fix:** Step 12 fee verification: text-based LLM verifies mapping using raw vision labels, with 7-layer deterministic fallback. Self-learns per importer after user corrections.

## Debugging Checklist

When the UI shows "LOADING..." but server logs show 200:

1. **Check browser console** for JavaScript errors (ReferenceError, TypeError)
2. **Add debug state** to the loading template to show fetch progress
3. **Check `api.ts`** — ensure using `res.text()` + `JSON.parse()`, NOT `res.json()`
4. **Check Svelte reactivity** — are you mutating objects in-place? Replace with spread.
5. **Check `{@const}` scope** — is it used outside its block?
6. **Check WebSocket** — is `job_data` being sent and received?
7. **Test with curl** — if curl works but browser doesn't, it's a body streaming issue
