#!/usr/bin/env python3
"""
RO-ED Pipeline — 4 steps, zero hardcoding.
PDF → HD images → Vision AI per page → LLM Assembler → LLM Verifier → Save.
"""

import time
from typing import Dict, List, Optional, Callable
from pathlib import Path

from pipeline.splitter import split_pdf
from pipeline.vision import extract_all_pages
from pipeline.assembler import assemble
from pipeline.verifier import verify

import config

try:
    import cost_tracker
except ImportError:
    cost_tracker = None

try:
    import database
except ImportError:
    database = None


def run_pipeline(
    pdf_path: str,
    model: str = None,
    progress_callback: Callable = None,
    max_workers: int = 8,
) -> Optional[Dict]:
    """10-step pipeline: Split → Vision → QA → Assemble → QA → Verify."""
    total_start = time.time()
    try:
        if cost_tracker:
            cost_tracker.reset()
    except Exception:
        pass

    pdf_name = Path(pdf_path).name

    def _log(msg: str):
        if progress_callback:
            progress_callback(msg)
        print(f"  [pipeline] {msg}")

    _log("Starting extraction pipeline...")

    # ═══ Step 1: Split PDF → HD images ═══
    _log("Step 1: Splitting PDF into HD images...")
    pages = split_pdf(pdf_path)
    _log(f"Split: {len(pages)} pages")
    if not pages:
        return None

    # Guard: cap at 50 pages to prevent OOM (4GB container, 10 concurrent users)
    MAX_PAGES = 50
    if len(pages) > MAX_PAGES:
        _log(f"⚠ PDF has {len(pages)} pages — capping at {MAX_PAGES} to prevent memory issues")
        pages = pages[:MAX_PAGES]

    # ═══ Step 2: Vision AI reads every page ═══
    vision_model = config.VISION_MODEL or model
    _log(f"Step 2: Vision AI reading {len(pages)} pages... (model: {vision_model or config.EXTRACTION_MODEL})")
    page_results = extract_all_pages(pages, model=vision_model, max_workers=max_workers)
    ok = sum(1 for r in page_results if r["status"] == "ok")
    _log(f"Extracted: {ok}/{len(pages)} pages")
    if ok == 0:
        return None

    # ═══ Step 3: LLM Assembler builds tables ═══
    assembler_model = config.ASSEMBLER_MODEL or model
    _log(f"Step 3: AI assembling... (model: {assembler_model or config.EXTRACTION_MODEL})")
    assembled = assemble(page_results, model=assembler_model)
    declaration = assembled.get("declaration", {})
    items = assembled.get("items", [])
    filled = sum(1 for v in declaration.values() if v is not None)
    _log(f"Assembled: {filled}/16 declaration fields, {len(items)} items")

    # ═══ Step 4: LLM Verifier cross-checks against source ═══
    verifier_model = config.VERIFIER_MODEL or model
    _log(f"Step 4: AI verifying... (model: {verifier_model or config.EXTRACTION_MODEL})")
    verified = verify(declaration, items, pages, model=verifier_model)

    declaration = verified.get("declaration", declaration)
    items = verified.get("items", items)
    corrections = verified.get("corrections", [])
    filled = sum(1 for v in declaration.values() if v is not None)
    _log(f"Verified: {filled}/16 fields, {len(corrections)} corrections")

    # Free image memory after verifier (no longer needed — fee verify uses text, not images)
    for p in pages:
        p.pop("image_b64", None)

    # ═══ Step 5: Fee verification — LLM text-based (primary) + deterministic (fallback) ═══
    from pipeline.assembler import verify_fees_with_llm, _fix_fee_shift, _build_page_summary

    fee_fields = ["Commercial Tax (CT)", "Advance Income Tax (AT)",
                  "Security Fee (SF)", "MACCS Service Fee (MF)", "Exemption/Reduction"]

    # Save assembler's original fee values — if everything else fails, these are our baseline
    original_fees = {f: declaration.get(f, 0) or 0 for f in fee_fields}

    # Primary: Focused text LLM call to verify fee mapping using raw page data
    _log("Step 5: Verifying fee field mapping...")
    fee_result = verify_fees_with_llm(declaration, page_results)
    fee_confidence = float(fee_result.get("confidence", 0)) if fee_result else 0

    fee_applied = False
    if fee_result and fee_confidence >= 0.7:
        # ── Sanity check LLM output before applying ──
        customs_value = float(declaration.get("Total Customs Value", 0) or 0)
        duty = float(declaration.get("Import/Export Customs Duty", 0) or 0)
        sane = True
        for field in fee_fields:
            try:
                val = float(fee_result.get(field, 0) or 0)
            except (ValueError, TypeError):
                val = 0
            # No fee should exceed customs value
            if customs_value > 0 and val > customs_value:
                _log(f"  ⚠ Fee LLM sanity fail: {field}={val:,.0f} > customs_value={customs_value:,.0f}")
                sane = False
            # CT should not be absurdly large vs duty
            if field == "Commercial Tax (CT)" and duty > 0 and val > duty * 5:
                _log(f"  ⚠ Fee LLM sanity fail: CT={val:,.0f} > 5x duty={duty:,.0f}")
                sane = False

        if sane:
            # Apply LLM-verified values
            changes = []
            for field in fee_fields:
                new_val = fee_result.get(field)
                if new_val is not None:
                    old_val = declaration.get(field, 0) or 0
                    try:
                        new_val = float(new_val)
                    except (ValueError, TypeError):
                        continue
                    if abs(float(old_val or 0) - new_val) > 0.01:
                        changes.append((field, old_val, new_val))
                    declaration[field] = new_val

            fee_applied = True
            if changes:
                _log(f"Fee verifier corrected {len(changes)} fields (confidence={fee_confidence:.2f}):")
                for field, old_val, new_val in changes:
                    _log(f"  {field}: {old_val} → {new_val}")
            else:
                _log(f"Fee verifier confirmed all values correct (confidence={fee_confidence:.2f})")
        else:
            _log(f"Fee verifier output failed sanity check — ignoring LLM result")

    if not fee_applied:
        # Fallback: conservative deterministic correction (requires page text evidence)
        _log("Fee verifier unavailable or failed — using deterministic fallback")
        page_summary = _build_page_summary(page_results)
        declaration = _fix_fee_shift(declaration, page_summary, "", None, job_id=None)

        # ── Final safety net: if deterministic made fees WORSE, revert to assembler original ──
        customs_value = float(declaration.get("Total Customs Value", 0) or 0)
        if customs_value > 0:
            for field in fee_fields:
                val = float(declaration.get(field, 0) or 0)
                if val > customs_value:
                    _log(f"  ⚠ Reverting ALL fee changes — {field}={val:,.0f} exceeds customs value")
                    for f in fee_fields:
                        declaration[f] = original_fees[f]
                    _log(f"  Fees restored to assembler values: {original_fees}")
                    break

    # ═══ Compute accuracy ═══
    total_fields = filled + sum(sum(1 for v in item.values() if v is not None) for item in items)
    max_fields = 16 + len(items) * 9
    accuracy = (total_fields / max_fields * 100) if max_fields > 0 else 0

    # Confidence scoring
    confidence = None
    try:
        from v2.confidence import compute_field_confidence
        confidence = compute_field_confidence(declaration=declaration, items=items, page_results=page_results)
        if confidence and confidence.get("summary"):
            s = confidence["summary"]
            _log(f"Confidence: avg={s['average_confidence']:.2f} | high={s['high']} medium={s['medium']} low={s['low']}")
    except Exception:
        pass

    total_duration = time.time() - total_start
    total_cost = cost_tracker.get_total_cost() if cost_tracker else 0

    _log(f"Complete: {len(items)} items, {filled}/16 decl, {total_duration:.1f}s, ${total_cost:.4f}")

    return {
        "pipeline_version": "ro-ed",
        "pipeline_mode": "ro_ed",
        "items_count": len(items),
        "declaration": declaration,
        "items": items,
        "accuracy": accuracy,
        "duration_seconds": total_duration,
        "cost_usd": total_cost,
        "pages_total": len(pages),
        "pages_ok": ok,
        "validation": {"overall_accuracy": accuracy},
        "confidence": confidence,
        "corrections": corrections,
    }


if __name__ == "__main__":
    import sys
    pdf = sys.argv[1] if len(sys.argv) > 1 else None
    if not pdf:
        print("Usage: python -m pipeline.pipeline <pdf_path>")
        sys.exit(1)

    config.PDF_PATH = pdf
    if database:
        database.init_database()

    result = run_pipeline(pdf)
    if result:
        print(f"\nItems: {result['items_count']}")
        print(f"Declaration: {sum(1 for v in result['declaration'].values() if v is not None)}/16")
        print(f"Duration: {result['duration_seconds']:.1f}s")
        print(f"Cost: ${result['cost_usd']:.4f}")
        if result.get("corrections"):
            print(f"Corrections: {len(result['corrections'])}")
            for c in result["corrections"]:
                print(f"  {c.get('field')}: {c.get('original')} → {c.get('corrected')}")
