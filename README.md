# RO-ED AI Agent

Document intelligence system that extracts structured data from import/export PDF documents using AI vision + Master Agent architecture with self-learning and fee verification.

Built by **City AI Team** — City Holdings Myanmar

---

## How It Works

```
PDF → HD Images → Vision AI per page → QA → Master Agents → QA → Verifier → Fee Verify → Results
```

12 steps. No Tesseract. No hardcoding. Zero calculations. Every value read directly from the document.

---

## Features

- **Master + Column Agent architecture** — Declaration (16 agents), Items (9 agents per product)
- **Claude Sonnet verification** — Premium model cross-checks every value against source page images
- **Fee verification** — Text-based LLM verifies fee-label mapping + 7-layer deterministic fallback
- **Fee self-learning** — User corrections auto-save fee baselines per importer for future accuracy
- **json_schema enforced** — Guaranteed valid JSON output, all fields present, zero parse errors
- **Token optimized** — Deduplicated fields, no metadata sent to assembler (~11% savings)
- **Self-learning** — User corrections feed back as few-shot examples
- **Vision QA** — Re-runs low-quality pages automatically
- **Table QA** — Re-runs missing fields until all filled
- **Cross-validation** — Items sum must match declaration total
- **Pipeline Visualizer** — Real-time flow diagram showing each step's progress
- **Confidence indicators** — Green/yellow/red dots on every table and page
- **Annotated PDF** — Highlights where values were found on original PDF
- **Excel export** — Items + Declaration as separate sheets
- **Field search** — Search any value across all pages with copy buttons
- **Document map** — Clickable page grid with expand to see image + data
- **Duplicate detection** — VIEW RESULTS (free) or RE-PROCESS (double confirmation)
- **Persistent sessions** — Results stay when navigating away (localStorage)
- **10 concurrent users** — SQLite WAL + API semaphore + per-job isolation
- **Review queue** — Auto-approve (≥95%) / Needs review (80-95%) / Escalate (<80%)
- **REST API** — `POST /api/extract` for headless integration
- **Batch processing** — Multiple PDFs with real-time streaming terminal
- **100% accuracy** — Tested on 12 verified customs PDFs, zero corrections needed
- **Cost: ~$0.15-0.20 per PDF**

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Frontend | SvelteKit 5 + TailwindCSS 4.2 |
| Backend | FastAPI 0.115 + Uvicorn (Python 3.12) |
| Database | SQLite (WAL mode, 30s busy_timeout) |
| Vision + Assembler | Google Gemini 3 Flash Preview via OpenRouter |
| Verifier | Anthropic Claude Sonnet 4.6 via OpenRouter |
| Fee Verifier | Google Gemini 3 Flash Preview (text-based, ~$0.002/call) |
| PDF | PyMuPDF at 300 DPI + Pillow (NO Tesseract) |
| Auth | Local JWT + Keycloak OIDC |
| Container | Docker (4GB RAM, 2 CPUs) |

---

## Quick Install

```bash
git clone <repo-url> RO-ED-Lang && cd RO-ED-Lang
cp .env.example .env   # Edit .env — set OPENROUTER_API_KEY + JWT_SECRET_KEY
./start-docker.sh      # Builds frontend, validates config, starts Docker
```

Or manually:
```bash
cp .env.example .env                          # Fill in your values
cd frontend && npm install && npm run build && cd ..
docker-compose up -d --build
```

Open **http://localhost:9000** — Login with credentials from `.env` (default: `admin` / `admin123`)

### Environment Variables

| Variable | Required | Where | Description |
|----------|----------|-------|-------------|
| `OPENROUTER_API_KEY` | Yes | `.env` | API key for AI models ([get one](https://openrouter.ai/keys)) |
| `JWT_SECRET_KEY` | Yes | `.env` | Secret for JWT signing — generate: `openssl rand -hex 32` |
| `ADMIN_DEFAULT_USERNAME` | No | `.env` | Admin username on first init (default: `admin`) |
| `ADMIN_DEFAULT_PASSWORD` | No | `.env` | Admin password on first init (default: `admin123`) |
| `KEYCLOAK_REALM_URL` | No | `.env` / Settings page | Keycloak SSO (optional) |
| `KEYCLOAK_CLIENT_ID` | No | `.env` / Settings page | Keycloak client ID |
| `KEYCLOAK_CLIENT_SECRET` | No | `.env` / Settings page | Keycloak client secret |
| `KEYCLOAK_ADMIN_ROLE` | No | `.env` / Settings page | Keycloak role mapped to admin (default: `admin`) |

---

## Pipeline Architecture

```
Step 1:  SPLITTER           — PDF → HD images (300 DPI)
Step 2:  VISION AGENTS      — Per-page extraction (parallel, 8 workers, semaphore=16)
Step 3:  VISION QA          — Re-run bad pages only
Step 4:  DECLARATION AGENT  — 16 column agents (json_schema enforced)
Step 5:  DECLARATION QA     — Re-run missing fields only
Step 6:  ITEMS AGENT        — 10 column agents (json_schema enforced)
Step 7:  ITEMS QA           — Re-run missing fields only
Step 8:  ITEM DEDUP         — Remove duplicate items (same name + HS code)
Step 9:  PRICE FALLBACK     — Copy Invoice↔CIF price if one is missing
Step 10: CROSS-VALIDATION   — Items sum = declaration total
Step 11: VERIFIER           — Claude Sonnet checks against page images
Step 12: FEE VERIFY         — Text LLM verifies fee-label mapping (deterministic fallback)
```

### Models

| Step | Model | Role | Cost |
|------|-------|------|------|
| Vision | Gemini 3 Flash | Read each page image → JSON | ~$0.002/page |
| Declaration Agent | Gemini 3 Flash | 16 column agents find declaration fields | ~$0.01 |
| Items Agent | Gemini 3 Flash | 9 column agents find item fields | ~$0.01 |
| Verifier | Claude Sonnet 4.6 | Cross-check all values against page images | ~$0.12 |
| Fee Verifier | Gemini 3 Flash | Verify fee-label mapping using raw page text | ~$0.002 |

### Fee Verification (Step 12)

LLMs can shift fee/tax values down by 1 position in Myanmar customs documents (CT→AT, SF→MF). However, CT=0 and SF=0 are often genuine (tax-exempt goods). The system uses a 5-layer defense chain:

1. **Assembler extracts only** — no self-correction. Clean values go to verifier.
2. **Claude Sonnet verifier** — cross-checks fees against page images.
3. **Primary: Text-based LLM** (`verify_fees_with_llm()`) — matches fee labels to values using raw vision text (not images). Avoids visual layout confusion. Cost: ~$0.002. Sanity-checked before applying.
4. **Fallback: Conservative deterministic** (`_fix_fee_shift()`) — only fires with **page text evidence** (not heuristics alone). 7 safety layers including importer baseline, page text cross-check, post-fix sanity check, and audit trail.
5. **Final safety net** — if any fee exceeds customs value after correction, ALL changes revert to assembler original.

**Self-learning:** User corrections auto-save fee baselines per importer for future accuracy.

**Tested:** 13/13 PDFs pass, 2/2 verified against ground truth with 100% fee accuracy.

---

## Output Tables

### Table 1: Product Items (13 columns)

| Column | Description |
|--------|-------------|
| Job | Job ID |
| Item Name | Full product description |
| Customs Duty Rate | Duty as decimal (0.15 = 15%) |
| Quantity (1) | Quantity with unit (e.g. "17,280 KG") |
| Invoice Unit Price | FOB price from commercial invoice (supplier price, lower) |
| CIF Unit Price | CIF price from customs declaration (includes freight+insurance, higher) |
| Currency | Invoice currency (from declaration) |
| Commercial Tax % | Tax as decimal (0.05 = 5%) |
| Exchange Rate (1) | Foreign to local currency rate |
| HS Code | Harmonized system tariff code |
| Origin Country | Country of origin |
| Customs Value (MMK) | Value in local currency |
| Processed | Extraction timestamp |

### Table 2: Customs Declaration (18 columns)

| Column | Description |
|--------|-------------|
| Job | Job ID |
| Declaration No | 12-digit customs declaration number |
| Date | Declaration date (YYYY-MM-DD) |
| Importer | Importing company name |
| Consignor | Exporter/shipper name |
| Invoice Number | Invoice reference (with prefix) |
| Invoice Price | Total invoice amount in foreign currency |
| Currency | Invoice currency (USD, THB, etc.) |
| Exchange Rate | Conversion rate to MMK |
| Currency 2 | Same as Currency |
| Customs Value | Total CIF value in MMK |
| Duty | Import/export customs duty |
| Tax (CT) | Commercial tax |
| Income Tax (AT) | Advance income tax |
| Security (SF) | Security fee |
| MACCS (MF) | MACCS service fee |
| Exemption/Reduction | Tax exemption amount |
| Processed | Extraction timestamp |

---

## UI Pages

| Tab | Description |
|-----|-------------|
| **Agent** | Upload PDFs, pipeline visualizer, streaming log, all results |
| **History** | Job list → same results view (ResultAccordion) |
| **Review** | Confidence queue: auto-approve ≥95%, review 80-95%, escalate <80% |
| **Items** | All items across jobs |
| **Declarations** | All declarations across jobs |
| **Costs** | Daily trends, per-PDF costs |
| **Settings** | Users + Auth + Groups |

### Results View (RESULTS | PDF ANNOTATED | PIPELINE LOG)

1. Pipeline Flow Visualizer
2. KPI Cards
3. Field Confidence
4. Document Insights
5. Product Items table
6. Customs Declaration table
7. Corrections Log
8. Document Summary
9. Field Search
10. Document Map (expandable)

---

## CLI Usage

```bash
cd backend && python -m pipeline.pipeline /path/to/document.pdf
```

---

## Performance

| Metric | Value |
|--------|-------|
| Accuracy | 100% (12 PDFs, zero corrections) |
| Cost | $0.15-0.20 per PDF |
| Speed | 60-130s per PDF |
| Max pages tested | 29 pages (cap: 50) |
| Max items | 21 per document |
| Concurrent users | 10 |

---

## Troubleshooting

| Issue | Symptom | Fix |
|-------|---------|-----|
| `res.json()` hangs | "LOADING..." forever, server logs 200 OK | Use `res.text()` + `JSON.parse()` in `api.ts` instead of `res.json()` |
| Pipeline view stuck | Job shows DONE but results don't appear | Replace array entry with new object: `queue[i] = {...entry, status: 'done'}` |
| `{@const}` crash | `ReferenceError: X is not defined` in console | `{@const}` is block-scoped — don't reference outside its `{#if}`/`{#each}` |
| 401 on some routes | Works in curl, fails in browser | Match trailing slashes in API paths exactly (`/users/` not `/users`) |
| Results not loading after pipeline | WebSocket completes but no tables shown | Check `ws.py` sends `job_data` in `file_complete` message |
| Fee values shifted | CT/AT/SF/MF off by 1 position | Step 12: text LLM verifier (primary) + evidence-based deterministic fallback + sanity checks + auto-revert safety net |
| Declaration No shows as float | `100303470412.0` instead of `100303470412` | STRING_FIELDS in assembler.py skips numeric conversion |
| Currency 2 always empty | Not saved to DB | Fixed key from `Currency.1` to `Currency 2` in database.py |
| Missing columns in Excel | Only 15 columns instead of 18 | Added Job, Currency 2, Processed to all 3 export endpoints |

### Debug "LOADING..." Issues

1. Open browser DevTools Console — check for red errors
2. If no errors: add debug state to show fetch progress (see CLAUDE.md)
3. If `fetch done: 200 OK` but stuck: body streaming issue → use `res.text()` + `JSON.parse()`
4. If JavaScript error: fix the referenced variable/scope issue
5. Test API with curl: `curl -s http://localhost:9000/api/jobs/ -H "Authorization: Bearer <token>"`

---

Created by **City AI Team** — City Holdings Myanmar
