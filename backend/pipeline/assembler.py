#!/usr/bin/env python3
"""
Master + Column Agent Assembler
Each table has a Master Agent. Each column has its own focused instruction.
Nothing gets missed — every field has a dedicated agent responsible for finding it.
"""

import json
import re
import requests
import time
from typing import Dict, List, Optional

import config

try:
    import cost_tracker
except ImportError:
    cost_tracker = None


# ═══════════════════════════════════════════════════════════════
# LLM CALL
# ═══════════════════════════════════════════════════════════════

def _call_llm(prompt: str, data: str, model: str = None, label: str = "assembler",
              response_schema: Dict = None) -> Optional[Dict]:
    """Send prompt + data to LLM, parse JSON response.
    If response_schema provided, uses json_schema mode for guaranteed valid output.
    """
    model = model or config.EXTRACTION_MODEL

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt + "\n\n" + data}],
        "temperature": 0,
        "max_tokens": 16000,
    }

    # Add structured output if schema provided
    if response_schema:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": response_schema,
        }

    for attempt in range(3):
        try:
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {config.API_KEY}", "Content-Type": "application/json"},
                json=payload, timeout=config.API_TIMEOUT,
            )
            if resp.status_code == 200:
                result = resp.json()
                if cost_tracker:
                    cost_tracker.record(label, result, model)
                content = result["choices"][0]["message"]["content"].strip()
                # With json_schema, response is already clean JSON
                if response_schema:
                    return json.loads(content)
                # Without schema, need cleanup
                cleaned = re.sub(r'```json\n?|```\n?', '', content).strip()
                if '{' in cleaned:
                    return json.loads(cleaned[cleaned.index('{'):cleaned.rindex('}') + 1])
            elif resp.status_code == 429:
                time.sleep(2 ** (attempt + 1))
                continue
        except json.JSONDecodeError as e:
            print(f"    {label} JSON error: {e}")
        except Exception as e:
            print(f"    {label} error: {e}")
        if attempt < 2:
            time.sleep(2 ** (attempt + 1))
    return None


def _build_page_summary(page_results: List[Dict]) -> str:
    """Build compact text summary of all page extractions.
    Only includes fields, tables, and amounts — no document metadata, visual info, or entities.
    Deduplicates fields with same value but different key names.
    """
    parts = []
    for pr in page_results:
        if pr.get("status") != "ok":
            continue
        pn = pr["page_number"]
        pt = pr.get("page_type", "unknown")
        parsed = pr.get("parsed", {})
        part = f"\n--- PAGE {pn} ({pt}) ---\n"

        # Fields — include ALL fields (no dedup — different labels with same value are important context)
        fields = parsed.get("fields", {})
        if fields:
            part += "Fields:\n"
            for k, v in fields.items():
                part += f"  {k}: {v}\n"

        # Tables — keep all (critical for items extraction)
        for ti, table in enumerate(parsed.get("tables", [])):
            headers = table.get("headers", [])
            rows = table.get("rows", [])
            title = table.get("title", f"Table {ti+1}")
            part += f"\nTable: {title}\n"
            if headers:
                part += f"  Headers: {' | '.join(str(h) for h in headers)}\n"
            for row in rows:
                part += f"  Row: {' | '.join(str(c) for c in row)}\n"

        # Amounts — keep (useful for tax/duty values)
        amounts = parsed.get("amounts", [])
        if amounts:
            part += "Amounts:\n"
            for a in amounts:
                part += f"  {a.get('label', '?')}: {a.get('value', '?')} {a.get('currency', '')}\n"

        # REMOVED: document metadata, visual info, entities (assembler doesn't use them)
        parts.append(part)
    return "".join(parts)


def _build_corrections_prompt() -> str:
    """Query past corrections from DB as few-shot examples."""
    try:
        import database
        corrections = database.get_corrections(limit=20)
        if not corrections:
            return ""
        by_field = {}
        for c in corrections:
            field = c.get("field_key", "")
            if field not in by_field:
                by_field[field] = []
            by_field[field].append(c)
        lines = ["\n## LEARNED FROM PAST CORRECTIONS:"]
        for field, corrs in by_field.items():
            seen = set()
            for c in corrs:
                key = f"{c.get('original_value')}→{c.get('corrected_value')}"
                if key not in seen:
                    seen.add(key)
                    lines.append(f"- {field}: \"{c.get('original_value')}\" → \"{c.get('corrected_value')}\"")
                if len(seen) >= 3:
                    break
        print(f"    Injecting {len(corrections)} corrections into prompt")
        return "\n".join(lines) + "\n"
    except Exception:
        return ""


# ═══════════════════════════════════════════════════════════════
# MASTER AGENT 1: DECLARATION (16 column agents in one call)
# ═══════════════════════════════════════════════════════════════

DECLARATION_PROMPT = """You are the DECLARATION MASTER AGENT. You have 16 column agents, each responsible for finding ONE field.

Search ALL pages and fill EVERY field. Return ONLY valid JSON.

## YOUR 16 COLUMN AGENTS — each one searches ALL pages for its specific field:

AGENT 1 — Declaration No:
  Search for: customs declaration number, entry number, release order number
  Usually on the first page. Copy the exact number.

AGENT 2 — Declaration Date:
  Search for: declaration date, entry date
  Convert to YYYY-MM-DD format.

AGENT 3 — Importer (Name):
  Search for: importer name, buyer, importing company
  Return the FULL company name, not the code/ID.

AGENT 4 — Consignor (Name):
  Search for: consignor, exporter, shipper, seller
  Return the full company name.

AGENT 5 — Invoice Number:
  Search for: invoice number, invoice no, commercial invoice
  Include any prefix (A-, B-, etc.)

AGENT 6 — Invoice Price:
  Search for: invoice price, total invoice amount, invoice value
  Return the total amount in FOREIGN currency (USD, THB, EUR, etc.), NOT in local currency (MMK/Kyat).
  Look on the customs declaration — the field labeled "Invoice Price" or "Invoice Value".
  Do NOT use CIF value — that may include insurance and freight.

AGENT 7 — Currency:
  Search for: the 3-letter currency code of the invoice price
  Examples: USD, THB, EUR, JPY, CNY, KRW, INR, SGD, etc.

AGENT 8 — Exchange Rate:
  Search for: exchange rate, conversion rate, FX rate
  This is the rate to convert foreign currency to local currency (MMK).

AGENT 9 — Total Customs Value:
  Search for: total customs value, total CIF value in local currency
  Look for the field labeled "Total Customs Value" or "Total CIF Value" on the customs declaration.
  Return the amount in local currency (MMK/Kyat) as printed on the document.

AGENT 10 — Import/Export Customs Duty:
  Search for: customs duty, import duty, duty amount
  Look in tax tables, fees sections. If duty is FREE or exempt, return 0.

AGENT 11 — Commercial Tax (CT):
  Search for: commercial tax, CT, VAT, sales tax
  This is a TAX amount — usually a large number (millions in MMK).
  Look in the tax/fees summary table. The row labeled "Commercial Tax" or "CT".
  Do NOT confuse with Advance Income Tax (AT) — CT and AT are DIFFERENT rows.
  If exempt, return 0.

AGENT 12 — Advance Income Tax (AT):
  Search for: advance income tax, AT, withholding tax, income tax
  This is a TAX amount — can be large or zero.
  Look in the tax/fees summary table. The row labeled "Advance Income Tax" or "AT".
  Do NOT confuse with Commercial Tax (CT) — AT and CT are DIFFERENT rows.
  If not present or exempt, return 0.

AGENT 13 — Security Fee (SF):
  Search for: security fee, SF
  This is usually a SMALL fixed fee (e.g. 20,000 MMK).
  Look in the tax/fees summary table. The row labeled "Security Fee" or "SF".
  Do NOT confuse with MACCS Service Fee (MF) — SF and MF are DIFFERENT rows.
  Return 0 if not present.

AGENT 14 — MACCS Service Fee (MF):
  Search for: MACCS fee, MF, MACCS service fee, system service fee
  This is usually a LARGE fee amount (millions in MMK).
  Look in the tax/fees summary table. The row labeled "MACCS Service Fee" or "MF".
  Do NOT confuse with Security Fee (SF) — MF and SF are DIFFERENT rows.
  Return 0 if not present.

AGENT 15 — Exemption/Reduction:
  Search for: exemption, reduction, deduction, discount
  Return 0 if none.

AGENT 16 — Currency 2:
  Same as Currency (Agent 7).

## OUTPUT FORMAT:
{
  "Declaration No": <value>,
  "Declaration Date": <value>,
  "Importer (Name)": <value>,
  "Consignor (Name)": <value>,
  "Invoice Number": <value>,
  "Invoice Price": <numeric>,
  "Currency": <value>,
  "Currency 2": <value>,
  "Exchange Rate": <numeric>,
  "Total Customs Value": <numeric>,
  "Import/Export Customs Duty": <numeric>,
  "Commercial Tax (CT)": <numeric>,
  "Advance Income Tax (AT)": <numeric>,
  "Security Fee (SF)": <numeric>,
  "MACCS Service Fee (MF)": <numeric>,
  "Exemption/Reduction": <numeric>
}

RULES:
- Each agent searches ALL pages independently.
- Read EXACT values from the document. Do NOT calculate or guess.
- Taxes and fees: read from tax/fee tables. Match EACH value to its SPECIFIC labeled row.
  CRITICAL: CT, AT, SF, and MF are 4 SEPARATE line items in the fee table. Do NOT shift values between them.
  If a fee row exists, read its exact amount. If a fee row does NOT exist, return 0 for that field.
- Return ONLY valid JSON.

## PAGE DATA:
"""


# ═══════════════════════════════════════════════════════════════
# MASTER AGENT 2: ITEMS (9 column agents in one call)
# ═══════════════════════════════════════════════════════════════

ITEMS_PROMPT_TEMPLATE = """You are the ITEMS MASTER AGENT. You have 9 column agents, each responsible for finding ONE field for EVERY product item.

The declaration has already been extracted:
  Exchange Rate = {exchange_rate}
  Currency = {currency}

Search ALL pages and fill EVERY field for EVERY item. Return ONLY valid JSON.

## YOUR 9 COLUMN AGENTS — each searches ALL pages for its specific field:

AGENT 1 — Item name:
  Search for: product description, goods description, item name
  Look in invoices, item tables, customs declarations.
  Return an array with one name per item.

AGENT 2 — Quantity (1):
  Search for: quantity, qty, amount, pieces, weight
  Include the unit (KG, PCS, CTN, L, etc.) — e.g. "500 KG"
  Look in invoices, packing lists, item tables.

AGENT 3 — Invoice unit price:
  Search for: unit price, unit cost, price per unit on the COMMERCIAL INVOICE page.
  Look for the invoice/price list table — the column showing price per KG or per unit.
  This is the SUPPLIER price (FOB/FCA) — usually the LOWER price.
  Do NOT use the customs declaration "Invoice unit price" — that's the CIF price (higher).
  Return in FOREIGN currency (not local/MMK).
  Read the EXACT value from the document. Do NOT calculate.

AGENT 10 — CIF unit price:
  Search for: "Invoice unit price" field on the CUSTOMS DECLARATION / RELEASE ORDER pages.
  Look for the field labeled "Invoice unit price" on the customs declaration item rows.
  This is the CIF price (includes freight + insurance) — usually HIGHER than the invoice price.
  Return in FOREIGN currency (not local/MMK).
  Read the EXACT value from the document. Do NOT calculate.

AGENT 4 — Customs Value (MMK):
  Search for: customs value per item in LOCAL currency (MMK/Kyat)
  Look in customs declaration item rows — the column labeled "Customs Value" or "CIF Value (MMK)".
  This is usually a LARGE number (millions in MMK).
  Read the EXACT value from the document. Do NOT calculate.

AGENT 5 — Customs duty rate:
  Search for: duty rate, tariff rate, import duty percentage PER ITEM
  Look for the rate printed on the customs declaration for each item row (e.g. "15%", "M-15", "5%").
  Return as DECIMAL (e.g. 0.15 for 15%, 0.05 for 5%).
  If FREE or exempt, return 0.
  Read the EXACT rate from the document. Do NOT calculate from totals.

AGENT 6 — Commercial tax %:
  Search for: commercial tax rate, CT rate PER ITEM
  Look for the tax rate printed on the customs declaration for each item row (e.g. "5%", "CT 5%").
  Return as DECIMAL (e.g. 0.05 for 5%).
  If exempt or zero, return 0.
  Read the EXACT rate from the document. Do NOT calculate from totals.

AGENT 7 — HS Code:
  Search for: HS code, tariff code, harmonized system code
  Return the FULL code with dots (e.g. "1104.12.00 00").
  Look in customs declarations, import licences.

AGENT 8 — Origin Country:
  Search for: country of origin, origin, country
  Return the full country name or 2-letter code.
  Look in customs declarations, certificates of origin, invoices.

AGENT 9 — Exchange Rate (1):
  Use the declaration exchange rate: {exchange_rate}

## OUTPUT FORMAT:
{{
  "items": [
    {{
      "Item name": "<value>",
      "Quantity (1)": "<qty with unit>",
      "Invoice unit price": <numeric>,
      "CIF unit price": <numeric>,
      "Customs Value (MMK)": <numeric>,
      "Customs duty rate": <decimal>,
      "Commercial tax %": <decimal>,
      "HS Code": "<value>",
      "Origin Country": "<value>",
      "Exchange Rate (1)": <numeric>
    }}
  ]
}}

CRITICAL RULES:
- You MUST fill ALL 10 fields for EVERY item. No nulls allowed.
- Read EVERY value directly from the document. Do NOT calculate or guess.
- Invoice unit price = from COMMERCIAL INVOICE (supplier FOB price, LOWER).
- CIF unit price = from CUSTOMS DECLARATION (CIF price, HIGHER).
- These are TWO DIFFERENT prices from TWO DIFFERENT pages. Both must be filled.
- Exchange Rate is ALWAYS {exchange_rate} — just copy it.
- Duty rate and tax %: READ from the customs declaration per-item rows. Do NOT divide totals.
- Return ONLY valid JSON.

## PAGE DATA:
"""




# ═══════════════════════════════════════════════════════════════
# MASTER ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════

DECL_FIX_PROMPT = """You previously extracted a customs declaration but MISSED some fields.

Here is what you already found:
{existing}

These fields are STILL MISSING — you MUST find them now:
{missing_list}

Search ALL pages again. Focus ONLY on the missing fields. Return ONLY valid JSON with the missing fields filled:
{missing_json}

## PAGE DATA:
"""

ITEMS_FIX_PROMPT = """You previously extracted product items but MISSED some fields.

Declaration data:
  Exchange Rate = {exchange_rate}

Here are the items with missing fields marked as null:
{existing_items}

You MUST fill every null field. For each missing field:
- Search ALL pages again carefully for the value
- Look in customs declaration rows, invoice tables, licence documents
- Read the EXACT value from the document. Do NOT calculate or guess.
- Exchange Rate (1) = {exchange_rate} (copy this)

Return the COMPLETE items array with ALL fields filled:
{{
  "items": [<complete items with all 9 fields>]
}}

## PAGE DATA:
"""

DECL_REQUIRED = [
    "Declaration No", "Declaration Date", "Importer (Name)", "Consignor (Name)",
    "Invoice Number", "Invoice Price", "Currency", "Exchange Rate",
    "Total Customs Value", "Import/Export Customs Duty", "Commercial Tax (CT)",
    "Advance Income Tax (AT)",
]

ITEM_REQUIRED = [
    "Item name", "Quantity (1)", "Invoice unit price", "CIF unit price",
    "Customs Value (MMK)", "Customs duty rate", "Commercial tax %",
    "HS Code", "Origin Country", "Exchange Rate (1)",
]


def _parse_fees_from_page_summary(page_summary: str) -> Dict[str, float]:
    """Deterministic: search page summary text for fee labels and extract their numeric values.
    Uses regex to match label → number patterns in fields and table rows.
    """
    found = {}

    # Patterns: "label: value" or "label | value" in fields/tables
    # Each tuple: (output_key, list_of_regex_patterns_to_match)
    fee_patterns = [
        ("Commercial Tax (CT)", [
            r'(?:commercial\s*tax|(?<!\w)CT(?!\w))[\s:|\-]+[\s]*([\d,]+(?:\.\d+)?)',
        ]),
        ("Advance Income Tax (AT)", [
            r'(?:advance\s*income\s*tax|(?<!\w)AT(?!\w))[\s:|\-]+[\s]*([\d,]+(?:\.\d+)?)',
        ]),
        ("Security Fee (SF)", [
            r'(?:security\s*fee|(?<!\w)SF(?!\w))[\s:|\-]+[\s]*([\d,]+(?:\.\d+)?)',
        ]),
        ("MACCS Service Fee (MF)", [
            r'(?:MACCS\s*(?:service\s*)?fee|(?<!\w)MF(?!\w))[\s:|\-]+[\s]*([\d,]+(?:\.\d+)?)',
        ]),
        ("Exemption/Reduction", [
            r'(?:exemption|reduction|deduction)[\s:|\-]+[\s]*([\d,]+(?:\.\d+)?)',
        ]),
    ]

    for field_name, patterns in fee_patterns:
        for pattern in patterns:
            matches = re.findall(pattern, page_summary, re.IGNORECASE)
            if matches:
                # Take the last match (usually the summary/total, not a per-item value)
                val_str = matches[-1].replace(",", "")
                try:
                    found[field_name] = float(val_str)
                except ValueError:
                    pass
                break

    return found


def _search_page_summary_for_fee(page_summary: str, label_patterns: List[str]) -> float:
    """Search raw page text for a fee label and extract its numeric value.

    Searches ALL pages in the summary for ANY of the given label patterns.
    If found on multiple pages, returns the value from the page with highest specificity
    (labeled field > amount > table row).

    Returns the value if found, or -1 if not found.
    """
    if not page_summary:
        return -1

    found_values = []
    for label_pattern in label_patterns:
        for pattern in [
            # "Commercial Tax (CT): 93,794.28" or "Security Fee (SF): 0"
            rf'{label_pattern}\s*[:=]\s*([\d,]+(?:\.\d+)?)',
            # "Commercial tax Amount: 93,794.28"
            rf'{label_pattern}\s+(?:Amount|Value)\s*[:=]?\s*([\d,]+(?:\.\d+)?)',
            # "Security Fee (SF)": 0  (JSON-style in fields)
            rf'"{label_pattern}[^"]*"\s*[:=]\s*([\d,]+(?:\.\d+)?)',
        ]:
            for match in re.finditer(pattern, page_summary, re.IGNORECASE):
                try:
                    found_values.append(float(match.group(1).replace(",", "")))
                except ValueError:
                    continue

    if not found_values:
        return -1

    # If all found values agree, return that value
    # If they disagree (e.g. per-item CT vs total CT), return the most common one
    from collections import Counter
    counts = Counter(found_values)
    return counts.most_common(1)[0][0]


def _is_fixed_fee(value: float) -> bool:
    """Check if a value looks like a fixed government fee (SF or MF).
    Myanmar customs fixed fees are typically 20,000 or 30,000 MMK.
    """
    return value > 0 and value <= 100_000


# ═══════════════════════════════════════════════════════════════
# FEE VERIFIER — LLM-based fee field mapping verification
# ═══════════════════════════════════════════════════════════════

FEE_VERIFY_PROMPT = """Below is the RAW extracted data from a customs document (extracted by a vision AI).
The vision AI read every page and extracted fields, tables, and amounts with their labels.

I then assembled these fee values from the raw data:
  Import/Export Customs Duty: {duty}
  Commercial Tax (CT): {ct}
  Advance Income Tax (AT): {at}
  Security Fee (SF): {sf}
  MACCS Service Fee (MF): {mf}
  Exemption/Reduction: {exempt}

YOUR TASK: Check the RAW PAGE DATA below. Match each fee label to its correct value.

The raw data contains fields like "Commercial Tax Amount: 93,794" or "Security: 0" or
"Other taxes/fees Type: MF, Amount: 20,000". These are the ACTUAL labels from the document.
Use these labels to verify the mapping above.

For each fee:
- Find the label in the raw data (e.g. "Commercial Tax", "Security Fee", "MACCS", "Advance Income Tax")
- Read the value next to that label
- If a fee label does NOT appear in the raw data, return 0
- If a label appears but shows 0 or no amount, return 0

Return ONLY this JSON:
{{
  "Commercial Tax (CT)": <number>,
  "Advance Income Tax (AT)": <number>,
  "Security Fee (SF)": <number>,
  "MACCS Service Fee (MF)": <number>,
  "Exemption/Reduction": <number>,
  "confidence": <0.0-1.0 how confident you are>,
  "reasoning": "<1-2 sentences: which labels you found and matched>"
}}

## RAW PAGE DATA:
{page_data}"""


def verify_fees_with_llm(declaration: Dict, page_results: List[Dict], model: str = None) -> Dict:
    """Focused TEXT-BASED LLM call to verify fee field mapping.

    Uses the raw page_summary (already extracted by vision) — NOT images.
    This avoids the visual layout confusion that causes fee shifting.
    The raw text has correct labels like "Security: 0", "Commercial Tax: 93,794".
    The LLM just matches labels → values. Simple text task, no vision needed.

    Cost: ~$0.002 (text only, no image tokens).

    Returns: corrected fee values dict, or empty dict if verification fails.
    """
    model = model or config.EXTRACTION_MODEL

    ct = declaration.get("Commercial Tax (CT)", 0) or 0
    at = declaration.get("Advance Income Tax (AT)", 0) or 0
    sf = declaration.get("Security Fee (SF)", 0) or 0
    mf = declaration.get("MACCS Service Fee (MF)", 0) or 0
    exempt = declaration.get("Exemption/Reduction", 0) or 0
    duty = declaration.get("Import/Export Customs Duty", 0) or 0

    # Build page summary from page_results (text only — no images)
    page_data = _build_page_summary(page_results) if page_results else ""
    if not page_data:
        return {}

    prompt = FEE_VERIFY_PROMPT.format(
        duty=duty, ct=ct, at=at, sf=sf, mf=mf, exempt=exempt,
        page_data=page_data,
    )

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 500,
    }

    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {config.API_KEY}", "Content-Type": "application/json"},
            json=payload, timeout=config.API_TIMEOUT,
        )
        if resp.status_code == 200:
            result = resp.json()
            if cost_tracker:
                cost_tracker.record("fee_verify", result, model)

            raw = result["choices"][0]["message"]["content"].strip()
            cleaned = re.sub(r'```json\n?|```\n?', '', raw).strip()
            if '{' in cleaned:
                parsed = json.loads(cleaned[cleaned.index('{'):cleaned.rindex('}') + 1])
                confidence = float(parsed.get("confidence", 0))
                reasoning = parsed.get("reasoning", "")
                print(f"    Fee verifier: confidence={confidence:.2f}, reasoning: {reasoning}")

                # Clamp negative fee values to 0 (fees are never negative)
                for fk in ["Commercial Tax (CT)", "Advance Income Tax (AT)",
                           "Security Fee (SF)", "MACCS Service Fee (MF)",
                           "Exemption/Reduction"]:
                    v = parsed.get(fk)
                    if v is not None:
                        try:
                            if float(v) < 0:
                                parsed[fk] = 0
                        except (ValueError, TypeError):
                            parsed[fk] = 0

                return parsed
    except Exception as e:
        print(f"    Fee verifier error: {e}")

    return {}


def _fix_fee_shift(declaration: Dict, page_summary: str, corrections: str, model: str,
                   job_id: str = None) -> Dict:
    """Detect and correct fee field shifting using DETERMINISTIC methods.

    The LLM consistently shifts fee values down by 1 position in the fee table:
      Duty(correct), CT→AT, AT→SF, SF→MF, MF→Exemption (CT and SF become 0).

    This handles ALL shift patterns:
    - Full shift: CT=0, AT=CT_real, SF=0, MF=SF_real, Exemption=MF_real
    - Partial CT shift only: CT=0, AT=CT_real (SF/MF correct)
    - Partial SF shift only: SF=0, MF=SF_real, Exemption=MF_real (CT correct)
    - Values that look swapped between adjacent fields

    Safety layers:
    0. Importer fee baseline (if we've seen this importer before, use verified values)
    1. Page text cross-check (search raw vision output for actual values)
    2. Pattern detection with corroboration requirements
    3. Page text override (contradict heuristics with document evidence)
    4. Deterministic shift-back
    5. Post-fix sanity check (verify result is more reasonable than original)
    6. Audit trail (log every change to value_audit table)
    """
    ct = float(declaration.get("Commercial Tax (CT)", 0) or 0)
    at = float(declaration.get("Advance Income Tax (AT)", 0) or 0)
    sf = float(declaration.get("Security Fee (SF)", 0) or 0)
    mf = float(declaration.get("MACCS Service Fee (MF)", 0) or 0)
    exempt = float(declaration.get("Exemption/Reduction", 0) or 0)
    duty = float(declaration.get("Import/Export Customs Duty", 0) or 0)
    customs_value = float(declaration.get("Total Customs Value", 0) or 0)

    # ══════════════════════════════════════════════════════
    # Layer 0: Importer fee baseline
    # If we have verified fee values from past corrections for this importer,
    # use them as ground truth. This is the most reliable signal — it comes
    # from a human who checked the actual document.
    # ══════════════════════════════════════════════════════
    importer_name = declaration.get("Importer (Name)", "")
    baseline = {}
    if importer_name:
        try:
            import database
            baseline = database.get_fee_baseline(importer_name)
        except Exception:
            pass

    if baseline and baseline.get("verified"):
        print(f"    Fee baseline found for {importer_name}: {baseline}")

        # Use baseline to detect shifts with HIGH confidence
        # Baseline tells us the TYPICAL fee structure for this importer
        baseline_sf = baseline.get("SF")
        baseline_mf = baseline.get("MF")

        # SF baseline check: if baseline says SF should be ~20,000 but current SF=0
        # and current MF matches baseline SF → confirmed SF→MF shift
        if baseline_sf is not None and baseline_sf > 0 and sf == 0 and mf > 0:
            if abs(mf - baseline_sf) / max(baseline_sf, 1) < 0.1:  # MF ≈ baseline SF (within 10%)
                print(f"    Baseline confirms SF→MF shift: SF=0, MF={mf:,.0f} ≈ baseline SF={baseline_sf:,.0f}")
                declaration["Security Fee (SF)"] = mf
                declaration["MACCS Service Fee (MF)"] = exempt
                declaration["Exemption/Reduction"] = 0

                changes = [
                    ("Security Fee (SF)", sf, mf),
                    ("MACCS Service Fee (MF)", mf, exempt),
                    ("Exemption/Reduction", exempt, 0),
                ]
                print(f"      Fixed: SF: {sf:,.0f} → {mf:,.0f}")
                print(f"      Fixed: MF: {mf:,.0f} → {exempt:,.0f}")
                print(f"      Fixed: Exemption: {exempt:,.0f} → 0")

                # Log audit
                if job_id:
                    try:
                        for fk, ov, nv in changes:
                            database.save_value_audit(job_id, "declaration", fk,
                                "fee_shift_fix_baseline", str(ov), str(nv), "importer_baseline")
                    except Exception:
                        pass

                # SF side fixed via baseline. Now check CT/AT — if baseline says CT=0 is normal,
                # don't touch it. Otherwise fall through to pattern detection.
                baseline_ct = baseline.get("CT")
                if baseline_ct is not None and baseline_ct == 0 and ct == 0:
                    print(f"    Baseline confirms CT=0 is normal for this importer ✓")
                    return declaration

                # If CT/AT don't match baseline pattern, fall through to Layer 2 for pattern check
                print(f"    SF fixed via baseline. CT/AT will use pattern detection...")
                # Update local vars for pattern detection below
                sf = float(declaration.get("Security Fee (SF)", 0) or 0)
                mf = float(declaration.get("MACCS Service Fee (MF)", 0) or 0)
                exempt = float(declaration.get("Exemption/Reduction", 0) or 0)

        # SF baseline check: baseline says SF=0 is genuine (e.g. certain document types)
        elif baseline_sf is not None and baseline_sf == 0 and sf == 0:
            print(f"    Baseline confirms SF=0 is normal for this importer ✓ (skip SF shift detection)")
            # Skip SF shift detection below — we know SF=0 is correct

    # ══════════════════════════════════════════════════════
    # Layer 1: Page text cross-check
    # Search the raw vision output for fee labels and their actual values.
    # If we find explicit values in the document text, trust those over heuristics.
    # ══════════════════════════════════════════════════════
    page_sf = _search_page_summary_for_fee(page_summary, [
        r'Security Fee \(SF\)', r'Security Fee', r'Security', r'SF',
    ])
    page_mf = _search_page_summary_for_fee(page_summary, [
        r'MACCS Service Fee \(MF\)', r'MACCS Service Fee', r'MACCS',
        r'Other taxes/fees', r'MF',
    ])
    page_ct = _search_page_summary_for_fee(page_summary, [
        r'Commercial Tax \(CT\)', r'Commercial [Tt]ax', r'CT',
    ])
    page_at = _search_page_summary_for_fee(page_summary, [
        r'Advance Income Tax \(AT\)', r'Advance[d]? [Ii]ncome [Tt]ax', r'AT',
    ])

    if page_sf >= 0 or page_mf >= 0 or page_ct >= 0 or page_at >= 0:
        print(f"    Page text cross-check: SF={page_sf}, MF={page_mf}, CT={page_ct}, AT={page_at}")

    # ══════════════════════════════════════════════════════
    # Layer 2: Pattern-based shift detection
    # CRITICAL: Heuristics alone cause false positives (CT=0 and SF=0 are often
    # genuine in Myanmar customs docs). Every pattern now REQUIRES positive
    # page text evidence — absence of evidence is NOT evidence of shift.
    # ══════════════════════════════════════════════════════

    sf_shifted = False
    ct_shifted = False

    # --- SF shift detection ---
    # Only shift if page text CONTRADICTS current values (shows SF should be non-zero,
    # or MF should be different from current mf).
    if sf == 0 and mf > 0:
        if page_sf > 0:
            # Page text says SF should be non-zero but extracted as 0 → shifted
            sf_shifted = True
            print(f"    SF shift: page text shows SF={page_sf:,.0f} but extracted SF=0")
        elif page_mf >= 0 and page_mf != mf:
            # Page text shows MF should be different → current MF might hold SF's value
            sf_shifted = True
            print(f"    SF shift: page text shows MF={page_mf:,.0f} but extracted MF={mf:,.0f}")
        # If no page text evidence → DON'T assume shift (CT=0, SF=0 can be genuine)

    # --- CT shift detection ---
    # Only shift if we have POSITIVE evidence CT should be non-zero.
    if ct == 0 and at > 0 and duty > 0 and at > duty * 0.1:
        if sf_shifted:
            # SF shift confirmed → systemic shift proven → CT probably shifted too
            ct_shifted = True
            print(f"    CT shift: corroborated by confirmed SF shift")
        elif page_ct > 0:
            # Page text says CT should be non-zero but extracted as 0 → shifted
            ct_shifted = True
            print(f"    CT shift: page text shows CT={page_ct:,.0f} but extracted CT=0")
        elif page_at >= 0 and page_at != at and page_at == 0:
            # Page text says AT should be 0 but AT has a big value → AT holds CT's value
            ct_shifted = True
            print(f"    CT shift: page text shows AT={page_at:,.0f} but extracted AT={at:,.0f}")
        # If no evidence → DON'T shift. CT=0 is genuine for many importers.

    # Pattern D: Full rotation — sf_shifted confirmed AND CT>0, AT=0
    # (CT value is in the wrong position, AT was really 0)
    if not ct_shifted and sf_shifted and ct > 0 and at == 0 and duty > 0:
        if page_ct >= 0 and page_ct != ct:
            ct_shifted = True
            print(f"    Full rotation: page text shows CT={page_ct:,.0f} but extracted CT={ct:,.0f}")
        elif page_at >= 0 and page_at == 0:
            # Page confirms AT=0, so current CT value is likely shifted there
            if customs_value > 0 and ct > customs_value * 0.01:
                ct_shifted = True
                print(f"    Full rotation: CT={ct:,.0f}, AT=0 confirmed by page text")

    # ══════════════════════════════════════════════════════
    # Layer 3: Page text safety override
    # Even with evidence-based detection above, double-check: if page text
    # CONFIRMS the current values, cancel the shift (evidence may have been ambiguous).
    # ══════════════════════════════════════════════════════
    if ct_shifted and page_ct >= 0 and page_ct == ct:
        print(f"    Page text override: CT={page_ct:,.0f} confirmed in document → skip CT shift")
        ct_shifted = False

    if ct_shifted and page_at >= 0 and page_at == at:
        print(f"    Page text override: AT={page_at:,.0f} confirmed in document → skip CT shift")
        ct_shifted = False

    if sf_shifted and page_sf >= 0 and page_sf == sf:
        print(f"    Page text override: SF={page_sf:,.0f} confirmed in document → skip SF shift")
        sf_shifted = False

    if sf_shifted and page_mf >= 0 and page_mf == mf:
        print(f"    Page text override: MF={page_mf:,.0f} confirmed in document → skip SF shift")
        sf_shifted = False

    if not ct_shifted and not sf_shifted:
        print("    Fee shift check: no shift detected ✓")
        return declaration

    print(f"    ⚠ Fee shift detected!")
    if ct_shifted:
        print(f"      CT={ct:,.0f}, AT={at:,.0f}, duty={duty:,.0f}")
    if sf_shifted:
        print(f"      SF={sf:,.0f}, MF={mf:,.0f}, Exemption={exempt:,.0f}")

    # ══════════════════════════════════════════════════════
    # Layer 4: Apply deterministic shift-back correction
    # ══════════════════════════════════════════════════════
    print("    Applying deterministic shift-back correction...")
    changes = []  # Track changes for audit trail

    if ct_shifted:
        new_ct = at       # AT has CT's real value
        new_at = 0        # AT was empty/0 on document
        changes.append(("Commercial Tax (CT)", ct, new_ct))
        changes.append(("Advance Income Tax (AT)", at, new_at))
        print(f"      Fixed: CT: {ct:,.0f} → {new_ct:,.0f}")
        print(f"      Fixed: AT: {at:,.0f} → {new_at}")
        declaration["Commercial Tax (CT)"] = new_ct
        declaration["Advance Income Tax (AT)"] = new_at

    if sf_shifted:
        new_sf = mf       # MF has SF's real value
        new_mf = exempt   # Exemption has MF's real value
        new_exempt = 0    # Exemption was empty/0 on document
        changes.append(("Security Fee (SF)", sf, new_sf))
        changes.append(("MACCS Service Fee (MF)", mf, new_mf))
        changes.append(("Exemption/Reduction", exempt, new_exempt))
        print(f"      Fixed: SF: {sf:,.0f} → {new_sf:,.0f}")
        print(f"      Fixed: MF: {mf:,.0f} → {new_mf:,.0f}")
        print(f"      Fixed: Exemption: {exempt:,.0f} → {new_exempt}")
        declaration["Security Fee (SF)"] = new_sf
        declaration["MACCS Service Fee (MF)"] = new_mf
        declaration["Exemption/Reduction"] = new_exempt

    # ══════════════════════════════════════════════════════
    # Layer 5: Post-fix sanity check
    # Verify the corrected values are more reasonable than originals.
    # If not, REVERT and log a warning.
    # ══════════════════════════════════════════════════════
    new_ct_val = float(declaration.get("Commercial Tax (CT)", 0) or 0)
    new_at_val = float(declaration.get("Advance Income Tax (AT)", 0) or 0)
    new_sf_val = float(declaration.get("Security Fee (SF)", 0) or 0)
    new_mf_val = float(declaration.get("MACCS Service Fee (MF)", 0) or 0)
    new_exempt_val = float(declaration.get("Exemption/Reduction", 0) or 0)

    failed_sanity = False

    # Check 1: No fee should exceed customs value
    if customs_value > 0:
        for fname, fval in [("CT", new_ct_val), ("AT", new_at_val), ("SF", new_sf_val),
                            ("MF", new_mf_val), ("Exemption", new_exempt_val)]:
            if fval > customs_value:
                print(f"    ⚠ SANITY FAIL: {fname}={fval:,.0f} exceeds customs value={customs_value:,.0f}")
                failed_sanity = True

    # Check 2: CT should not exceed duty (CT is typically % of goods value, not > duty)
    if new_ct_val > 0 and duty > 0 and new_ct_val > duty * 5:
        print(f"    ⚠ SANITY FAIL: CT={new_ct_val:,.0f} is 5x larger than duty={duty:,.0f}")
        failed_sanity = True

    # Check 3: SF and MF should be fixed-fee sized (<=100,000) if non-zero
    if new_sf_val > 0 and not _is_fixed_fee(new_sf_val):
        print(f"    ⚠ SANITY WARN: SF={new_sf_val:,.0f} is unusually large for a fixed fee")
    if new_mf_val > 0 and not _is_fixed_fee(new_mf_val):
        print(f"    ⚠ SANITY WARN: MF={new_mf_val:,.0f} is unusually large for a fixed fee")

    if failed_sanity:
        # REVERT all changes
        print("    ⚠ REVERTING fee shift correction — sanity check failed!")
        for field_key, old_val, _new_val in changes:
            declaration[field_key] = old_val
        print("    Fee values restored to original extraction.")
        return declaration

    # ══════════════════════════════════════════════════════
    # Layer 6: Audit trail — log every change
    # ══════════════════════════════════════════════════════
    if job_id and changes:
        try:
            import database
            for field_key, old_val, new_val in changes:
                database.save_value_audit(
                    job_id=job_id, table_key="declaration", field_key=field_key,
                    stage="fee_shift_fix", old_value=str(old_val), new_value=str(new_val),
                    source="deterministic_shift_back"
                )
            print(f"    Audit: {len(changes)} changes logged to value_audit")
        except Exception as e:
            print(f"    Audit log failed (non-critical): {e}")

    return declaration


def _qa_declaration(declaration: Dict, page_summary: str, corrections: str, model: str) -> Dict:
    """QA check declaration — re-run for missing required fields."""
    missing = [f for f in DECL_REQUIRED if declaration.get(f) is None or str(declaration.get(f, "")).strip() == ""]
    if not missing:
        print("    QA Declaration: ALL fields filled ✓")
        return declaration

    print(f"    QA Declaration: {len(missing)} missing fields — re-running: {missing}")
    missing_json = json.dumps({f: "<find this>" for f in missing}, indent=2)
    existing = json.dumps({k: v for k, v in declaration.items() if v is not None}, indent=2)

    fix_prompt = DECL_FIX_PROMPT.format(
        existing=existing, missing_list=", ".join(missing), missing_json=missing_json
    )
    fix_result = _call_llm(fix_prompt + corrections, page_summary, model, "decl_qa")

    if fix_result:
        for field in missing:
            val = fix_result.get(field)
            if val is not None and str(val).strip():
                declaration[field] = val
                print(f"      Fixed: {field} = {str(val)[:50]}")

    still_missing = [f for f in DECL_REQUIRED if declaration.get(f) is None or str(declaration.get(f, "")).strip() == ""]
    if still_missing:
        print(f"    QA Declaration: still missing {len(still_missing)}: {still_missing}")
    else:
        print("    QA Declaration: ALL fields filled after fix ✓")

    return declaration


def _qa_items(items: List[Dict], declaration: Dict, page_summary: str, corrections: str, model: str) -> List[Dict]:
    """QA check items — re-run for missing fields."""
    all_filled = True
    for i, item in enumerate(items):
        missing = [f for f in ITEM_REQUIRED if item.get(f) is None or str(item.get(f, "")).strip() == ""]
        if missing:
            all_filled = False
            print(f"    QA Item {i+1}: missing {missing}")

    if all_filled:
        print("    QA Items: ALL fields filled for all items ✓")
        return items

    print(f"    QA Items: re-running for missing fields...")
    existing_items = json.dumps(items, indent=2, ensure_ascii=False)

    fix_prompt = ITEMS_FIX_PROMPT.format(
        exchange_rate=declaration.get("Exchange Rate", 0),
        existing_items=existing_items,
    )
    fix_result = _call_llm(fix_prompt + corrections, page_summary, model, "items_qa")

    if fix_result and fix_result.get("items"):
        fixed_items = fix_result["items"]
        # Merge: only fill in what was missing
        for i, item in enumerate(items):
            if i < len(fixed_items):
                for field in ITEM_REQUIRED:
                    if (item.get(field) is None or str(item.get(field, "")).strip() == "") and fixed_items[i].get(field) is not None:
                        item[field] = fixed_items[i][field]
                        print(f"      Fixed Item {i+1}.{field} = {str(item[field])[:50]}")

    # Final check
    for i, item in enumerate(items):
        missing = [f for f in ITEM_REQUIRED if item.get(f) is None or str(item.get(f, "")).strip() == ""]
        if missing:
            print(f"    QA Item {i+1}: still missing {missing}")
        else:
            print(f"    QA Item {i+1}: ALL 9 fields filled ✓")

    return items


# ═══════════════════════════════════════════════════════════════
# MASTER ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════

# JSON schemas for structured output
DECL_SCHEMA = {
    "name": "declaration",
    "schema": {
        "type": "object",
        "properties": {
            "Declaration No": {"type": ["string", "null"]},
            "Declaration Date": {"type": ["string", "null"]},
            "Importer (Name)": {"type": ["string", "null"]},
            "Consignor (Name)": {"type": ["string", "null"]},
            "Invoice Number": {"type": ["string", "null"]},
            "Invoice Price": {"type": ["number", "null"]},
            "Currency": {"type": ["string", "null"]},
            "Currency 2": {"type": ["string", "null"]},
            "Exchange Rate": {"type": ["number", "null"]},
            "Total Customs Value": {"type": ["number", "null"]},
            "Import/Export Customs Duty": {"type": ["number", "null"]},
            "Commercial Tax (CT)": {"type": ["number", "null"]},
            "Advance Income Tax (AT)": {"type": ["number", "null"]},
            "Security Fee (SF)": {"type": ["number", "null"]},
            "MACCS Service Fee (MF)": {"type": ["number", "null"]},
            "Exemption/Reduction": {"type": ["number", "null"]},
        },
        "required": ["Declaration No", "Declaration Date", "Importer (Name)", "Consignor (Name)",
                      "Invoice Number", "Invoice Price", "Currency", "Exchange Rate",
                      "Total Customs Value", "Import/Export Customs Duty", "Commercial Tax (CT)",
                      "Advance Income Tax (AT)", "Security Fee (SF)", "MACCS Service Fee (MF)",
                      "Exemption/Reduction"],
    }
}

ITEMS_SCHEMA = {
    "name": "items_result",
    "schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "Item name": {"type": ["string", "null"]},
                        "Quantity (1)": {"type": ["string", "null"]},
                        "Invoice unit price": {"type": ["number", "null"]},
                        "CIF unit price": {"type": ["number", "null"]},
                        "Customs Value (MMK)": {"type": ["number", "null"]},
                        "Customs duty rate": {"type": ["number", "null"]},
                        "Commercial tax %": {"type": ["number", "null"]},
                        "HS Code": {"type": ["string", "null"]},
                        "Origin Country": {"type": ["string", "null"]},
                        "Exchange Rate (1)": {"type": ["number", "null"]},
                    },
                    "required": ["Item name", "Quantity (1)", "Invoice unit price", "CIF unit price",
                                  "Customs Value (MMK)", "Customs duty rate", "Commercial tax %",
                                  "HS Code", "Origin Country", "Exchange Rate (1)"],
                }
            }
        },
        "required": ["items"],
    }
}

# json_schema mode — guarantees valid JSON output, all required fields present
USE_JSON_SCHEMA = True


def _cross_validate(declaration: Dict, items: List[Dict]) -> List[str]:
    """Cross-validate items against declaration totals. Returns list of warnings."""
    warnings = []
    try:
        total_cv = float(declaration.get("Total Customs Value", 0) or 0)
        if total_cv > 0 and items:
            # Check: sum of item customs values ≈ total customs value
            item_cv_sum = 0
            for item in items:
                cv = item.get("Customs Value (MMK)")
                if cv is not None:
                    item_cv_sum += float(cv)

            if item_cv_sum > 0:
                ratio = item_cv_sum / total_cv
                if ratio < 0.8 or ratio > 1.2:
                    warnings.append(f"Items customs value sum ({item_cv_sum:,.0f}) differs from declaration ({total_cv:,.0f}) by {abs(1-ratio)*100:.0f}%")
                else:
                    print(f"    Cross-check: items sum {item_cv_sum:,.0f} vs declaration {total_cv:,.0f} — OK ({ratio:.2f}x)")

        # Check: all items have same exchange rate as declaration
        decl_fx = float(declaration.get("Exchange Rate", 0) or 0)
        if decl_fx > 0:
            for i, item in enumerate(items):
                item_fx = float(item.get("Exchange Rate (1)", 0) or 0)
                if item_fx > 0 and abs(item_fx - decl_fx) > 0.01:
                    warnings.append(f"Item {i+1} exchange rate ({item_fx}) differs from declaration ({decl_fx})")
                    item["Exchange Rate (1)"] = decl_fx  # Auto-fix

        # Check: duty rates are reasonable (0 to 1)
        for i, item in enumerate(items):
            dr = item.get("Customs duty rate")
            if dr is not None and (float(dr) > 1 or float(dr) < 0):
                warnings.append(f"Item {i+1} duty rate {dr} is out of range (expected 0-1)")
            tp = item.get("Commercial tax %")
            if tp is not None and (float(tp) > 1 or float(tp) < 0):
                warnings.append(f"Item {i+1} tax % {tp} is out of range (expected 0-1)")

    except Exception as e:
        warnings.append(f"Cross-validation error: {e}")

    return warnings


def assemble(page_results: List[Dict], model: str = None) -> Dict:
    """Master orchestrator: Declaration + Items agents + QA + cross-validation."""
    print("  Assembler: Master + Column Agent + QA + Cross-Validation")

    page_summary = _build_page_summary(page_results)
    corrections = _build_corrections_prompt()
    print(f"    Page data: {len(page_summary):,} chars from {len(page_results)} pages")

    # ══════════════════════════════════════════════════════
    # Phase 1: Declaration Agent
    # ══════════════════════════════════════════════════════
    print("    Phase 1: Declaration Master (16 columns)...")
    decl_result = _call_llm(DECLARATION_PROMPT + corrections, page_summary, model, "decl_agent",
                             response_schema=DECL_SCHEMA if USE_JSON_SCHEMA else None)
    declaration = decl_result if decl_result else {}

    # Clean declaration numeric values (skip fields that must stay as strings)
    STRING_FIELDS = {"Declaration No", "Declaration Date", "Importer (Name)", "Consignor (Name)",
                     "Invoice Number", "Currency", "Currency 2"}
    for field, val in declaration.items():
        if field in STRING_FIELDS:
            continue
        if isinstance(val, str):
            cleaned = val.replace(",", "").strip()
            try:
                declaration[field] = float(cleaned)
            except (ValueError, TypeError):
                pass

    if not declaration.get("Currency 2") and declaration.get("Currency"):
        declaration["Currency 2"] = declaration["Currency"]
    for f in ["Security Fee (SF)", "MACCS Service Fee (MF)", "Exemption/Reduction"]:
        if declaration.get(f) is None:
            declaration[f] = 0

    filled = sum(1 for v in declaration.values() if v is not None)
    print(f"    Declaration: {filled}/16 fields")

    # ── QA: Declaration ──
    declaration = _qa_declaration(declaration, page_summary, corrections, model)

    # NOTE: Fee shift correction is NOT applied here anymore.
    # The assembler's job is to EXTRACT — correction belongs in the dedicated
    # fee verification step (Step 5 in pipeline.py) which has the LLM fee verifier
    # as primary + deterministic fallback. Running it here caused false positives
    # that corrupted correct values BEFORE the verifier/fee-LLM could see them.

    # ══════════════════════════════════════════════════════
    # Phase 2: Items Agent (needs declaration data)
    # ══════════════════════════════════════════════════════
    print("    Phase 2: Items Master (9 columns per item)...")
    items_prompt = ITEMS_PROMPT_TEMPLATE.format(
        exchange_rate=declaration.get("Exchange Rate", 0),
        currency=declaration.get("Currency", ""),
    )
    items_result = _call_llm(items_prompt + corrections, page_summary, model, "items_agent",
                              response_schema=ITEMS_SCHEMA if USE_JSON_SCHEMA else None)
    items = items_result.get("items", []) if items_result else []

    # Ensure exchange rate on every item + fallback Invoice Unit Price from CIF
    for item in items:
        if not item.get("Exchange Rate (1)") and declaration.get("Exchange Rate"):
            item["Exchange Rate (1)"] = declaration["Exchange Rate"]
        # If Invoice Unit Price is missing but CIF exists, copy CIF as fallback
        inv_price = item.get("Invoice unit price")
        cif_price = item.get("CIF unit price")
        if (inv_price is None or inv_price == 0 or str(inv_price).strip() == "") and cif_price:
            item["Invoice unit price"] = cif_price
            print(f"    Fallback: Invoice unit price missing, copied from CIF: {cif_price}")
        # If CIF is missing but Invoice exists, copy Invoice as fallback
        if (cif_price is None or cif_price == 0 or str(cif_price).strip() == "") and inv_price:
            item["CIF unit price"] = inv_price
            print(f"    Fallback: CIF unit price missing, copied from Invoice: {inv_price}")

    # Deduplicate items (same item name + same HS code = duplicate)
    if len(items) > 1:
        seen = set()
        unique_items = []
        for item in items:
            key = (str(item.get("Item name", "")).strip()[:50].lower(),
                   str(item.get("HS Code", "")).strip())
            if key not in seen:
                seen.add(key)
                unique_items.append(item)
            else:
                print(f"    Dedup: removed duplicate item '{str(item.get('Item name', ''))[:40]}...'")
        if len(unique_items) < len(items):
            print(f"    Dedup: {len(items)} → {len(unique_items)} items")
            items = unique_items

    print(f"    Items: {len(items)} products")

    # ── QA: Items ──
    items = _qa_items(items, declaration, page_summary, corrections, model)

    # ══════════════════════════════════════════════════════
    # Phase 3: Cross-Validation (zero cost — just math)
    # ══════════════════════════════════════════════════════
    print("    Phase 3: Cross-validation...")
    warnings = _cross_validate(declaration, items)
    if warnings:
        for w in warnings:
            print(f"    ⚠ {w}")
    else:
        print("    Cross-validation: ALL checks passed ✓")

    return {"declaration": declaration, "items": items}
