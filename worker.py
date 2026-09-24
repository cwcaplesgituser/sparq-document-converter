import hmac
import html
import difflib
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import urllib.parse
import xml.etree.ElementTree as ET
from docx import Document
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

app = FastAPI(title="Sparq Accessible Document Worker", version="1.0.0")
MAX_BYTES = 25 * 1024 * 1024
ALLOWED = {".pdf", ".doc", ".docx", ".odt", ".rtf", ".txt", ".html", ".htm", ".md", ".csv", ".xls", ".xlsx", ".ppt", ".pptx", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}

def run(args, cwd, timeout=600):
    try:
        result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
        return result.returncode, (result.stdout + "\n" + result.stderr).strip()
    except Exception as exc:
        return -1, str(exc)

def authorize(value):
    expected = os.environ.get("CONVERSION_API_KEY", "")
    if not expected or not value or not hmac.compare_digest(value, expected):
        raise HTTPException(status_code=401, detail="Invalid worker API key")

@app.get("/health")
def health():
    versions = {}
    for name, args in {
        "libreoffice": ["soffice", "--version"],
        "ocrmypdf": ["ocrmypdf", "--version"],
        "verapdf": ["/opt/verapdf/verapdf", "--version"],
        "tesseract": ["tesseract", "--version"],
        "ghostscript": ["gs", "--version"],
    }.items():
        code, output = run(args, "/tmp", 30)
        versions[name] = output.splitlines()[0] if code == 0 and output else "unavailable"
    return {"status": "healthy", "versions": versions}

def validate_ua2(pdf, root):
    code, output = run(["/opt/verapdf/verapdf", "--format", "xml", "--flavour", "ua2", str(pdf)], root)
    try:
        tree = ET.fromstring(output)
        results = [element.attrib.get("isCompliant", "").lower() for element in tree.iter() if element.tag.rsplit("}", 1)[-1] == "validationReport"]
        return code == 0 and results == ["true"], output[-4000:]
    except ET.ParseError:
        return False, output[-4000:]

def fix_ua2_metadata(pdf, root):
    """Only retain a metadata repair if a second full PDF/UA-2 check passes."""
    fixed_dir = pathlib.Path(root) / "metadata-repair"
    fixed_dir.mkdir(exist_ok=True)
    code, output = run(["/opt/verapdf/verapdf", "--format", "xml", "--flavour", "ua2",
                        "--fixmetadata", "--savefolder", str(fixed_dir), str(pdf)], root)
    for candidate in fixed_dir.glob("*.pdf"):
        passed, validation = validate_ua2(candidate, root)
        if passed:
            return candidate, "veraPDF metadata repair passed full PDF/UA-2 validation. " + validation[-1200:]
    return None, "Metadata repair did not produce a validated PDF/UA-2 file. " + output[-1200:]

def scanned_pdf(pdf, root):
    code, output = run(["pdftotext", str(pdf), "-"], root, 90)
    return code == 0 and len(output.strip()) < 40

def rebuild_text_as_ua2(pdf, original, root):
    """Rebuild simple PDFs only, then check both conformance and text retention."""
    rebuilt = pathlib.Path(root) / "text-rebuilt-ua2.pdf"
    try:
        process = subprocess.run(["/opt/worker-venv/bin/python", "/app/pdfua2_rebuild_worker.py",
                                  str(pdf), str(rebuilt), "--verapdf", "/opt/verapdf/verapdf"],
                                 cwd=root, capture_output=True, text=True, timeout=300, check=False)
        code, output = process.returncode, process.stdout
    except Exception as exc:
        return None, "Conservative text rebuild failed: " + str(exc)[:300]
    try:
        details = json.loads(output)
    except ValueError:
        return None, "Conservative text rebuild failed: " + output[-700:]
    if code != 0 or details.get("ok") is not True or details.get("compliant") is not True or not rebuilt.is_file():
        reasons = details.get("reasons") or details.get("failed_rules") or details.get("error") or details.get("message") or details.get("stage")
        return None, "Conservative text rebuild skipped or failed: " + str(reasons)[:700]
    # Keep the existing independent validator gate, even after the rebuild worker reports success.
    passed, validation = validate_ua2(rebuilt, root)
    if passed:
        source_code, content = run(["pdftotext", str(pdf), "-"], root, 90)
        extracted, rebuilt_text = run(["pdftotext", str(rebuilt), "-"], root, 90)
        original_text = " ".join(content.split()) if source_code == 0 else ""
        rendered_text = " ".join(rebuilt_text.split())
        length_ratio = min(len(original_text), len(rendered_text)) / max(len(original_text), len(rendered_text), 1)
        similarity = (difflib.SequenceMatcher(None, original_text[:5000], rendered_text[:5000], autojunk=False).ratio() * length_ratio) if extracted == 0 and source_code == 0 else 0
        if similarity < 0.95:
            return None, f"Text rebuild discarded: extracted-text similarity {similarity:.1%} is below the 95% safety threshold."
        return rebuilt, f"Simple text document rebuilt and passed PDF/UA-2 machine validation; extracted-text similarity {similarity:.1%}. Human review of meaning, reading order and appearance remains required. " + validation[-800:]
    return None, "Text rebuild did not pass PDF/UA-2 validation. " + validation[-1000:]

@app.post("/convert")
async def convert(file: UploadFile = File(...), targetFormat: str = Form("pdfua2"), x_worker_api_key: str | None = Header(default=None)):
    authorize(x_worker_api_key)
    target = targetFormat if targetFormat in {"pdfua2", "pdf", "html", "docx", "original"} else "pdfua2"
    original = pathlib.Path(file.filename or "document").name
    extension = pathlib.Path(original).suffix.lower()
    if extension not in ALLOWED:
        raise HTTPException(status_code=400, detail="Unsupported input type")
    root = tempfile.mkdtemp(prefix="sparq-convert-")
    source = pathlib.Path(root) / ("source" + extension)
    try:
        total = 0
        with source.open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_BYTES:
                    raise HTTPException(status_code=413, detail="Maximum file size is 25 MB")
                output.write(chunk)
        return convert_file(source, root, original, extension, target, file.content_type)
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise
    finally:
        await file.close()


def convert_file(source, root, original, extension, target, content_type):
    try:
        report = []
        if target == "original":
            result, media, conformance = source, content_type or "application/octet-stream", "Original retained; not validated"
        else:
            pdf = source
            if extension in {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}:
                pdf = pathlib.Path(root) / "image.pdf"
                code, output = run(["img2pdf", str(source), "-o", str(pdf)], root)
                report.append("Image normalized to PDF. " + output[-600:])
                if code != 0: raise HTTPException(status_code=422, detail="Image normalization failed")
            elif extension != ".pdf":
                pdf_options = 'pdf:writer_pdf_Export:{"UseTaggedPDF":{"type":"boolean","value":"true"},"PDFUACompliance":{"type":"boolean","value":"true"}}'
                code, output = run(["soffice", "--headless", "--convert-to", pdf_options, "--outdir", root, str(source)], root)
                candidates = list(pathlib.Path(root).glob("*.pdf"))
                report.append("LibreOffice tagged-PDF export: " + output[-1000:])
                if code != 0 or not candidates: raise HTTPException(status_code=422, detail="Office conversion failed")
                pdf = candidates[0]

            if target == "docx":
                if extension == ".docx":
                    result = source
                    report.append("Original Word document retained.")
                elif extension == ".pdf" or extension in {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}:
                    readable = pdf
                    if scanned_pdf(pdf, root):
                        ocr_pdf = pathlib.Path(root) / "text-layer.pdf"
                        code, output = run(["ocrmypdf", "--skip-text", "--rotate-pages", "--deskew", "--output-type", "pdf", str(pdf), str(ocr_pdf)], root)
                        if code == 0 and ocr_pdf.exists(): readable = ocr_pdf
                        report.append("Scanned pages received OCR before Word extraction. " + output[-500:])
                    code, text = run(["pdftotext", "-layout", str(readable), "-"], root, 90)
                    if code != 0 or not text.strip(): raise HTTPException(status_code=422, detail="No extractable text for Word output")
                    document = Document()
                    for page in text.split("\f"):
                        for paragraph in page.splitlines(): document.add_paragraph(paragraph)
                        if page.strip() and page != text.split("\f")[-1]: document.add_page_break()
                    result = pathlib.Path(root) / "converted.docx"
                    document.save(result)
                    report.append("Word document rebuilt from extracted text; check layout, images, tables, and reading order.")
                else:
                    code, output = run(["soffice", "--headless", "--convert-to", "docx", "--outdir", root, str(source)], root)
                    candidates = [candidate for candidate in pathlib.Path(root).glob("*.docx") if candidate != source]
                    if code != 0 or not candidates: raise HTTPException(status_code=422, detail="Word conversion failed")
                    result = candidates[0]
                    report.append("LibreOffice Word export: " + output[-600:])
                media, conformance = "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "Word output; human accessibility review required"
            elif target == "html":
                code, output = run(["soffice", "--headless", "--convert-to", "html", "--outdir", root, str(source)], root)
                candidates = list(pathlib.Path(root).glob("*.html"))
                report.append("HTML export: " + output[-1000:])
                if code != 0 or not candidates: raise HTTPException(status_code=422, detail="HTML conversion failed")
                result, media, conformance = candidates[0], "text/html", "Needs human accessibility review"
            elif target == "pdf":
                result, media, conformance = pdf, "application/pdf", "Standard PDF; not PDF/UA certified"
            else:
                passed, validation = validate_ua2(pdf, root)
                report.append("Original/tagged PDF veraPDF PDF/UA-2 validation: " + validation)
                result = pdf
                if not passed and scanned_pdf(pdf, root):
                    ocr_output = pathlib.Path(root) / "accessible.pdf"
                    code, output = run(["ocrmypdf", "--skip-text", "--rotate-pages", "--deskew", "--output-type", "pdf", str(pdf), str(ocr_output)], root)
                    report.append("Scanned-document OCR: " + output[-1200:])
                    if code == 0 and ocr_output.exists():
                        result = ocr_output
                        passed, validation = validate_ua2(result, root)
                        report.append("After OCR veraPDF PDF/UA-2 validation: " + validation)
                if not passed:
                    repaired, diagnostic = fix_ua2_metadata(result, root)
                    report.append(diagnostic)
                    if repaired is not None:
                        result, passed = repaired, True
                if not passed:
                    rebuilt, diagnostic = rebuild_text_as_ua2(result, original, root)
                    report.append(diagnostic)
                    if rebuilt is not None:
                        result, passed = rebuilt, True
                media = "application/pdf"
                conformance = "PDF/UA-2 machine checks passed; human review required" if passed else "Not PDF/UA-2 certified; remediation/review required"

        out_name = pathlib.Path(original).stem + ("-accessible" if target == "pdfua2" else "-converted") + result.suffix
        headers = {"X-Conformance": urllib.parse.quote(conformance[:500]), "X-Conversion-Report": urllib.parse.quote("\n".join(report)[-6000:]), "X-Output-Name": urllib.parse.quote(out_name)}
        return FileResponse(result, media_type=media, filename=out_name, headers=headers, background=BackgroundTask(shutil.rmtree, root, True))

    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise
