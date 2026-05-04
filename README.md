# surface-commons-pdf-toolkit

Open-source PDF toolkit — drop-in replacement for SmallPDF / iLovePDF paid plans.

Six endpoints:

| Endpoint | What it does | Stack |
|---|---|---|
| `POST /commons/pdf/merge` | combine N PDFs into one | pypdf |
| `POST /commons/pdf/split` | split by page ranges → zip of parts | pypdf |
| `POST /commons/pdf/compress` | smaller PDF (screen/ebook/printer) | ghostscript → pikepdf fallback |
| `POST /commons/pdf/ocr` | searchable PDF from scanned PDF or image | ocrmypdf + tesseract |
| `POST /commons/pdf/image-to-pdf` | N images → one PDF | img2pdf + Pillow |
| `POST /commons/pdf/pdf-to-image` | rasterize each page → zip of png/jpeg | pdf2image (poppler) |
| `GET  /commons/pdf/health` | what's available + SC cost table | (public, no auth) |

## SC cost (per call)

| Tool | SC |
|---|---|
| merge | 2 |
| split | 2 |
| compress | 3 |
| image-to-pdf | 2 |
| pdf-to-image | 3 |
| ocr | 10 |

Costs are flat per-call and configurable via env (`PDF_SC_MERGE`, `PDF_SC_OCR`, etc.).

## System dependencies

- `tesseract-ocr` (+ language packs you want)
- `ghostscript`
- `poppler-utils` (`pdftoppm`)

## Python dependencies

```
pypdf>=4.0.0
pikepdf>=8.0.0
pdf2image>=1.17.0
img2pdf>=0.5.0
ocrmypdf>=16.0.0
Pillow>=10.0.0
```

## Auth

Service-key + `X-Agent-Id` header pair (Surface convention). Each call pre-debits SC from the caller's wallet and writes an audit row to `tool_calls`.

## Upload limit

25 MB per file by default (`PDF_MAX_UPLOAD_BYTES`).

## License

MIT — part of the **Surface Commons** initiative. Replace one paid SaaS at a time.

## Source of truth

Lives upstream in [agentnation/surface](https://github.com/ratneshs230/surface) at `app/api/commons/pdf_toolkit.py`. This repo is a one-way mirror.
