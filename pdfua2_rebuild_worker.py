#!/usr/bin/env python3
"""
Supt Search - conservative PDF/UA-2 rebuild fallback.

Purpose
-------
This worker is for *simple, text-dominant PDFs* that failed the normal
layout-preserving remediation path. It rebuilds semantic HTML from the PDF's
text structure, generates a tagged PDF/UA-2 candidate with WeasyPrint, then
requires a veraPDF UA-2 pass before reporting compliance.

Important
---------
- It never claims PDF/UA-2 compliance without a veraPDF UA-2 pass.
- It deliberately refuses image-heavy / complex-layout documents because a
  text-only rebuild could destroy meaning or omit required alternative text.
- Production PDF/UA-2 generation requires WeasyPrint >= 70.0 in this worker,
  because newer releases include PDF/UA-2 fixes (metadata / PDF 2 namespace).

CLI
---
python pdfua2_rebuild_worker.py input.pdf output.pdf

The script writes one JSON result object to stdout and exits:
  0 = generated and veraPDF reports compliant
  2 = not a safe rebuild candidate
  3 = generated but failed PDF/UA-2 validation
  4 = dependency/configuration problem
  5 = unexpected processing error
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

try:
    import pymupdf as fitz
except Exception as exc:  # pragma: no cover
    fitz = None
    FITZ_IMPORT_ERROR = exc
else:
    FITZ_IMPORT_ERROR = None

try:
    import weasyprint
    from weasyprint import HTML
except Exception as exc:  # pragma: no cover
    weasyprint = None
    HTML = None
    WEASY_IMPORT_ERROR = exc
else:
    WEASY_IMPORT_ERROR = None


MIN_WEASYPRINT = (70, 0)
DEFAULT_LANG = "en-US"
MAX_IMAGE_AREA_RATIO = 0.08
MAX_DRAWINGS_PER_PAGE = 40
MAX_COLUMNS = 1
MIN_TEXT_CHARS = 40


@dataclass
class TextBlock:
    page_number: int
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    max_font_size: float
    median_font_size: float
    bold_ratio: float
    italic_ratio: float


@dataclass
class CandidateAssessment:
    safe: bool
    reasons: list[str]
    page_count: int
    text_chars: int
    image_area_ratio: float
    suspected_multicolumn_pages: int
    drawing_count: int


def emit(result: dict[str, Any], exit_code: int) -> int:
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return exit_code


def version_tuple(version: str) -> tuple[int, ...]:
    parts = []
    for p in re.split(r"[.+-]", version):
        m = re.match(r"(\d+)", p)
        if not m:
            break
        parts.append(int(m.group(1)))
    return tuple(parts or [0])


def clean_text(value: str) -> str:
    value = value.replace("\u00ad", "")
    value = value.replace("\u00a0", " ")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n[ \t]+", "\n", value)
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def iter_spans(block: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for line in block.get("lines", []):
        for span in line.get("spans", []):
            text = span.get("text", "")
            if text and text.strip():
                yield span


def block_to_text(block: dict[str, Any]) -> str:
    lines = []
    for line in block.get("lines", []):
        line_text = "".join(span.get("text", "") for span in line.get("spans", []))
        line_text = clean_text(line_text)
        if line_text:
            lines.append(line_text)
    return clean_text("\n".join(lines))


def extract_blocks(doc: "fitz.Document") -> list[TextBlock]:
    out: list[TextBlock] = []
    for pno, page in enumerate(doc, start=1):
        d = page.get_text("dict", sort=True)
        for block in d.get("blocks", []):
            if block.get("type") != 0:
                continue
            text = block_to_text(block)
            if not text:
                continue
            spans = list(iter_spans(block))
            if not spans:
                continue
            sizes = [float(s.get("size", 0) or 0) for s in spans if float(s.get("size", 0) or 0) > 0]
            if not sizes:
                sizes = [11.0]
            flags = [int(s.get("flags", 0) or 0) for s in spans]
            # PyMuPDF flags: italic bit 1, serif bit 2, mono bit 3, bold bit 4.
            bold_ratio = sum(1 for f in flags if f & (1 << 4)) / max(1, len(flags))
            italic_ratio = sum(1 for f in flags if f & (1 << 1)) / max(1, len(flags))
            x0, y0, x1, y1 = map(float, block.get("bbox", [0, 0, 0, 0]))
            out.append(
                TextBlock(
                    page_number=pno,
                    x0=x0,
                    y0=y0,
                    x1=x1,
                    y1=y1,
                    text=text,
                    max_font_size=max(sizes),
                    median_font_size=statistics.median(sizes),
                    bold_ratio=bold_ratio,
                    italic_ratio=italic_ratio,
                )
            )
    return out


def page_image_area_ratio(page: "fitz.Page") -> float:
    page_area = max(1.0, float(page.rect.width * page.rect.height))
    total = 0.0
    # Image blocks from text dict provide occupied rectangles without decoding image bytes.
    d = page.get_text("dict")
    for b in d.get("blocks", []):
        if b.get("type") == 1:
            x0, y0, x1, y1 = map(float, b.get("bbox", [0, 0, 0, 0]))
            total += max(0.0, x1 - x0) * max(0.0, y1 - y0)
    return min(1.0, total / page_area)


def looks_multicolumn(page: "fitz.Page") -> bool:
    blocks = []
    d = page.get_text("dict", sort=True)
    for b in d.get("blocks", []):
        if b.get("type") != 0:
            continue
        text = block_to_text(b)
        if len(text) < 10:
            continue
        x0, y0, x1, y1 = map(float, b.get("bbox", [0, 0, 0, 0]))
        blocks.append((x0, y0, x1, y1))
    if len(blocks) < 4:
        return False
    width = float(page.rect.width)
    left = [b for b in blocks if b[2] <= width * 0.58]
    right = [b for b in blocks if b[0] >= width * 0.42]
    if len(left) < 2 or len(right) < 2:
        return False
    # If vertically overlapping content exists in both left and right regions, treat as columns.
    for a in left:
        for b in right:
            overlap = min(a[3], b[3]) - max(a[1], b[1])
            if overlap > 8:
                return True
    return False


def assess_candidate(doc: "fitz.Document", blocks: list[TextBlock]) -> CandidateAssessment:
    reasons: list[str] = []
    text_chars = sum(len(b.text) for b in blocks)
    image_ratios = [page_image_area_ratio(page) for page in doc]
    image_ratio = statistics.mean(image_ratios) if image_ratios else 0.0
    multicolumn = sum(1 for page in doc if looks_multicolumn(page))
    drawing_count = 0
    for page in doc:
        try:
            drawing_count += len(page.get_drawings())
        except Exception:
            pass
        if page.get_links() or page.first_annot or page.first_widget:
            reasons.append("document contains links, annotations, or form controls requiring preservation")
            break

    if text_chars < MIN_TEXT_CHARS:
        reasons.append("too little extractable text; document may be scanned or image-only")
    if image_ratio > MAX_IMAGE_AREA_RATIO:
        reasons.append(
            f"images occupy about {image_ratio:.1%} of page area on average; automatic alt text would be unreliable"
        )
    if multicolumn > 0:
        reasons.append(f"{multicolumn} page(s) appear to use multi-column layout")
    if drawing_count > len(doc) * MAX_DRAWINGS_PER_PAGE:
        reasons.append("document contains a high number of vector drawings / complex graphics")

    return CandidateAssessment(
        safe=not reasons,
        reasons=reasons,
        page_count=len(doc),
        text_chars=text_chars,
        image_area_ratio=image_ratio,
        suspected_multicolumn_pages=multicolumn,
        drawing_count=drawing_count,
    )


def body_font_size(blocks: list[TextBlock]) -> float:
    sizes = []
    for b in blocks:
        # Weight larger text blocks a bit more by adding the median size more than once.
        n = max(1, min(6, len(b.text) // 80 + 1))
        sizes.extend([b.median_font_size] * n)
    return statistics.median(sizes) if sizes else 11.0


def classify_block(block: TextBlock, body_size: float) -> str:
    text = block.text.strip()
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    words = len(text.split())
    ratio = block.max_font_size / max(body_size, 1.0)

    # PyMuPDF sometimes extracts list markers (• • •) as their own block.
    if lines and all(re.fullmatch(r"[-*•▪◦]", ln) for ln in lines):
        return "bullet-markers"

    # Short headings can wrap to a second visual line, so do not require a single line.
    if len(lines) <= 2 and words <= 18:
        if ratio >= 1.75:
            return "h1"
        if ratio >= 1.45:
            return "h2"
        if ratio >= 1.22 and (block.bold_ratio >= 0.45 or words <= 10):
            return "h3"
        if block.bold_ratio >= 0.85 and words <= 10 and not text.endswith(('.', ';', ':')):
            return "h3"

    if re.match(r"^\s*(?:[-*•▪◦]|\d+[.)]|[A-Za-z][.)])\s+", text):
        return "list"

    return "p"


def infer_title(blocks: list[TextBlock], fallback: str) -> str:
    if not blocks:
        return fallback
    body = body_font_size(blocks)
    first_page = [b for b in blocks if b.page_number == 1][:16]
    candidates = [b for b in first_page if classify_block(b, body) in {"h1", "h2"} and len(b.text) <= 180]
    if candidates:
        return clean_text(candidates[0].text.replace("\n", " "))
    # Fall back to the largest short block near the top of page 1.
    short = [b for b in first_page if len(b.text.split()) <= 24 and len(b.text) <= 180]
    if short:
        chosen = max(short, key=lambda b: (b.max_font_size, -b.y0))
        return clean_text(chosen.text.replace("\n", " "))
    first = clean_text(blocks[0].text.replace("\n", " "))
    return first[:160] if first else fallback


def paragraph_html(text: str) -> str:
    # Preserve intentional line breaks within a logical block, but collapse ordinary whitespace.
    parts = [clean_text(p) for p in text.split("\n") if clean_text(p)]
    return "<br>".join(html.escape(p) for p in parts)


def list_item_text(text: str) -> str:
    return re.sub(r"^\s*(?:[-*•▪◦]|\d+[.)]|[A-Za-z][.)])\s+", "", text).strip()


def build_semantic_html(
    blocks: list[TextBlock],
    title: str,
    lang: str = DEFAULT_LANG,
    page_width_pt: float = 612.0,
    page_height_pt: float = 792.0,
) -> str:
    body = body_font_size(blocks)
    parts: list[str] = []
    current_page = None
    list_open = False
    pending_bullets = 0

    def close_list() -> None:
        nonlocal list_open
        if list_open:
            parts.append("</ul>")
            list_open = False

    for block in blocks:
        if current_page != block.page_number:
            close_list()
            pending_bullets = 0
            if current_page is not None:
                parts.append('<div class="page-break"></div>')
            current_page = block.page_number

        role = classify_block(block, body)
        safe = paragraph_html(block.text)
        if role == "bullet-markers":
            pending_bullets = len([ln for ln in block.text.split("\n") if ln.strip()])
            continue

        text_lines = [clean_text(ln) for ln in block.text.split("\n") if clean_text(ln)]
        if pending_bullets and len(text_lines) == pending_bullets:
            close_list()
            parts.append("<ul>")
            for ln in text_lines:
                parts.append(f"<li>{html.escape(ln)}</li>")
            parts.append("</ul>")
            pending_bullets = 0
            continue
        pending_bullets = 0

        if role == "list":
            if not list_open:
                parts.append("<ul>")
                list_open = True
            parts.append(f"<li>{html.escape(list_item_text(block.text))}</li>")
        else:
            close_list()
            if role in {"h1", "h2", "h3"}:
                parts.append(f"<{role}>{safe}</{role}>")
            else:
                parts.append(f"<p>{safe}</p>")
    close_list()

    escaped_title = html.escape(title)
    return f"""<!doctype html>
<html lang="{html.escape(lang)}">
<head>
<meta charset="utf-8">
<title>{escaped_title}</title>
<style>
  @page {{ size: {page_width_pt:.2f}pt {page_height_pt:.2f}pt; margin: 0.72in; }}
  html {{ font-family: Arial, Helvetica, sans-serif; font-size: 11pt; line-height: 1.38; }}
  body {{ margin: 0; color: #111; }}
  h1 {{ font-size: 20pt; margin: 0 0 14pt; line-height: 1.15; }}
  h2 {{ font-size: 16pt; margin: 14pt 0 8pt; line-height: 1.2; }}
  h3 {{ font-size: 13pt; margin: 12pt 0 6pt; line-height: 1.25; }}
  /* WeasyPrint's default heading bookmarks use page destinations, which
     fail the PDF/UA-2 structure-destination rule in veraPDF. Keep headings
     tagged but omit these generated outline entries. */
  h1, h2, h3 {{ bookmark-level: none; }}
  p {{ margin: 0 0 8pt; orphans: 2; widows: 2; }}
  ul {{ margin: 0 0 8pt 20pt; padding: 0; }}
  li {{ margin: 0 0 3pt; }}
  .page-break {{ break-before: page; }}
</style>
</head>
<body>
<main>
{''.join(parts)}
</main>
</body>
</html>"""


def find_verapdf(explicit: str | None) -> str | None:
    if explicit:
        p = Path(explicit)
        if p.exists():
            return str(p)
        found = shutil.which(explicit)
        return found
    for name in ("verapdf", "veraPDF", "verapdf.bat"):
        found = shutil.which(name)
        if found:
            return found
    return None


def parse_verapdf_json(raw: str) -> tuple[bool, dict[str, Any]]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Some wrappers write banner text before JSON. Try the widest JSON object.
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise
        data = json.loads(raw[start : end + 1])

    def walk(obj: Any) -> Iterable[dict[str, Any]]:
        if isinstance(obj, dict):
            yield obj
            for v in obj.values():
                yield from walk(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from walk(v)

    flags: list[bool] = []
    for d in walk(data):
        for key in ("isCompliant", "is_compliant", "compliant"):
            if key in d and isinstance(d[key], bool):
                flags.append(d[key])
    if flags:
        # A validation job is compliant only if all discovered validation compliance flags are true.
        return all(flags), data

    # Fall back to batch summary counts when present.
    text = json.dumps(data)
    if '"nonCompliant":0' in text and '"compliant":1' in text:
        return True, data
    return False, data


def validate_ua2(verapdf: str, pdf_path: Path) -> tuple[bool, dict[str, Any], str]:
    cmd = [verapdf, "--format", "json", "--flavour", "ua2", str(pdf_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    raw = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if not raw:
        return False, {"error": "veraPDF returned no JSON", "stderr": err, "returncode": proc.returncode}, err
    try:
        compliant, parsed = parse_verapdf_json(raw)
    except Exception as exc:
        return False, {"error": f"unable to parse veraPDF JSON: {exc}", "raw": raw[:12000], "stderr": err}, err
    return proc.returncode == 0 and compliant, parsed, err


def failed_rule_summary(report: dict[str, Any], limit: int = 20) -> list[dict[str, Any]]:
    failed: list[dict[str, Any]] = []

    def walk(obj: Any) -> None:
        if len(failed) >= limit:
            return
        if isinstance(obj, dict):
            status = obj.get("status")
            failed_checks = obj.get("failedChecks") or obj.get("failed_checks")
            if status == "failed" or (isinstance(failed_checks, int) and failed_checks > 0):
                failed.append(
                    {
                        k: obj.get(k)
                        for k in ("specification", "clause", "testNumber", "status", "failedChecks", "description")
                        if obj.get(k) is not None
                    }
                )
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(report)
    return failed


def main() -> int:
    parser = argparse.ArgumentParser(description="Conservative PDF/UA-2 rebuild fallback")
    parser.add_argument("input_pdf")
    parser.add_argument("output_pdf")
    parser.add_argument("--lang", default=DEFAULT_LANG)
    parser.add_argument("--title", default=None)
    parser.add_argument("--verapdf", default=os.getenv("VERAPDF_BIN"))
    parser.add_argument("--keep-html", default=None, help="Optional path to save generated semantic HTML")
    parser.add_argument(
        "--allow-old-weasyprint",
        action="store_true",
        help="Development-only: allow WeasyPrint < 70.0. Production should not use this for UA-2.",
    )
    args = parser.parse_args()

    src = Path(args.input_pdf).resolve()
    dst = Path(args.output_pdf).resolve()

    if fitz is None:
        return emit({"ok": False, "stage": "dependency", "error": f"PyMuPDF unavailable: {FITZ_IMPORT_ERROR}"}, 4)
    if HTML is None or weasyprint is None:
        return emit({"ok": False, "stage": "dependency", "error": f"WeasyPrint unavailable: {WEASY_IMPORT_ERROR}"}, 4)
    if not src.exists():
        return emit({"ok": False, "stage": "input", "error": f"Input file not found: {src}"}, 4)

    wp_version = getattr(weasyprint, "__version__", "0")
    if version_tuple(wp_version) < MIN_WEASYPRINT and not args.allow_old_weasyprint:
        return emit(
            {
                "ok": False,
                "stage": "dependency",
                "error": f"WeasyPrint {wp_version} is too old for the production UA-2 fallback; require >= 70.0",
                "weasyprint_version": wp_version,
            },
            4,
        )

    verapdf = find_verapdf(args.verapdf)
    if not verapdf:
        return emit(
            {
                "ok": False,
                "stage": "dependency",
                "error": "veraPDF CLI not found; refusing to claim PDF/UA-2 compliance without validation",
                "hint": "Install veraPDF and set VERAPDF_BIN or pass --verapdf.",
            },
            4,
        )

    try:
        with fitz.open(src) as doc:
            if doc.needs_pass:
                return emit({"ok": False, "stage": "input", "error": "Encrypted/password-protected PDF is not supported by fallback"}, 2)
            blocks = extract_blocks(doc)
            assessment = assess_candidate(doc, blocks)
            if not assessment.safe:
                return emit(
                    {
                        "ok": False,
                        "stage": "candidate-assessment",
                        "compliant": False,
                        "safe_for_text_rebuild": False,
                        "reasons": assessment.reasons,
                        "assessment": assessment.__dict__,
                    },
                    2,
                )

            title = args.title or infer_title(blocks, src.stem)
            first_rect = doc[0].rect if len(doc) else fitz.Rect(0, 0, 612, 792)
            semantic_html = build_semantic_html(
                blocks,
                title=title,
                lang=args.lang,
                page_width_pt=float(first_rect.width),
                page_height_pt=float(first_rect.height),
            )

        if args.keep_html:
            keep_html = Path(args.keep_html).resolve()
            keep_html.parent.mkdir(parents=True, exist_ok=True)
            keep_html.write_text(semantic_html, encoding="utf-8")

        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp_dir = Path(tempfile.mkdtemp(prefix="pdfua2-rebuild-"))
        tmp_pdf = tmp_dir / "candidate.pdf"
        try:
            HTML(string=semantic_html, base_url=str(src.parent)).write_pdf(
                target=str(tmp_pdf),
                pdf_variant="pdf/ua-2",
                pdf_tags=True,
                custom_metadata=True,
            )
            compliant, report, stderr = validate_ua2(verapdf, tmp_pdf)
            if not compliant:
                return emit(
                    {
                        "ok": False,
                        "stage": "validation",
                        "compliant": False,
                        "safe_for_text_rebuild": True,
                        "output_written": False,
                        "title": title,
                        "weasyprint_version": wp_version,
                        "failed_rules": failed_rule_summary(report),
                        "validator_stderr": stderr[-4000:] if stderr else "",
                        "message": "Rebuilt candidate did not pass veraPDF PDF/UA-2 validation; original file should remain unchanged.",
                    },
                    3,
                )

            shutil.copy2(tmp_pdf, dst)
            return emit(
                {
                    "ok": True,
                    "stage": "complete",
                    "compliant": True,
                    "safe_for_text_rebuild": True,
                    "output_written": True,
                    "output_pdf": str(dst),
                    "title": title,
                    "language": args.lang,
                    "weasyprint_version": wp_version,
                    "validator": "veraPDF",
                    "validator_profile": "ua2",
                    "assessment": assessment.__dict__,
                },
                0,
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    except Exception as exc:
        return emit({"ok": False, "stage": "exception", "error": f"{type(exc).__name__}: {exc}"}, 5)


if __name__ == "__main__":
    raise SystemExit(main())
