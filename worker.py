import hmac
import os
import pathlib
import shutil
import subprocess
import tempfile
import urllib.parse
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

@app.post("/convert")
async def convert(file: UploadFile = File(...), targetFormat: str = Form("pdfua2"), x_worker_api_key: str | None = Header(default=None)):
    authorize(x_worker_api_key)
    target = targetFormat if targetFormat in {"pdfua2", "pdf", "html", "original"} else "pdfua2"
    original = pathlib.Path(file.filename or "document").name
    extension = pathlib.Path(original).suffix.lower()
    if extension not in ALLOWED:
        raise HTTPException(status_code=400, detail="Unsupported input type")
    root = tempfile.mkdtemp(prefix="sparq-convert-")
    source = pathlib.Path(root) / ("source" + extension)
    total = 0
    with source.open("wb") as output:
        while chunk := await file.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_BYTES:
                shutil.rmtree(root, ignore_errors=True)
                raise HTTPException(status_code=413, detail="Maximum file size is 25 MB")
            output.write(chunk)

    report = []
    if target == "original":
        result, media, conformance = source, file.content_type or "application/octet-stream", "Original retained; not validated"
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

        if target == "html":
            code, output = run(["soffice", "--headless", "--convert-to", "html", "--outdir", root, str(source)], root)
            candidates = list(pathlib.Path(root).glob("*.html"))
            report.append("HTML export: " + output[-1000:])
            if code != 0 or not candidates: raise HTTPException(status_code=422, detail="HTML conversion failed")
            result, media, conformance = candidates[0], "text/html", "Needs human accessibility review"
        elif target == "pdf":
            result, media, conformance = pdf, "application/pdf", "Standard PDF; not PDF/UA certified"
        else:
            ocr_output = pathlib.Path(root) / "accessible.pdf"
            code, output = run(["ocrmypdf", "--skip-text", "--rotate-pages", "--deskew", "--clean-final", str(pdf), str(ocr_output)], root)
            report.append("OCRmyPDF: " + output[-1600:])
            if code != 0 or not ocr_output.exists():
                shutil.copy2(pdf, ocr_output)
                report.append("OCR was not applied; source PDF preserved.")
            code, validation = run(["/opt/verapdf/verapdf", "--format", "text", "--defaultflavour", "ua2", str(ocr_output)], root)
            report.append("veraPDF: " + validation[-4000:])
            passed = code == 0 and "compliant" in validation.lower() and "non-compliant" not in validation.lower()
            result, media = ocr_output, "application/pdf"
            conformance = "PDF/UA-2 machine checks passed; human review required" if passed else "Not PDF/UA-2 certified; remediation/review required"

    out_name = pathlib.Path(original).stem + ("-accessible" if target == "pdfua2" else "-converted") + result.suffix
    headers = {"X-Conformance": urllib.parse.quote(conformance[:500]), "X-Conversion-Report": urllib.parse.quote("\n".join(report)[-6000:]), "X-Output-Name": urllib.parse.quote(out_name)}
    return FileResponse(result, media_type=media, filename=out_name, headers=headers, background=BackgroundTask(shutil.rmtree, root, True))
