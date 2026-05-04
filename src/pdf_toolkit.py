"""PDF Toolkit — Commons guild's first wave.

Open-source replacement for SmallPDF / iLovePDF paid plans. Six tools:
merge, split, compress, OCR, image→PDF, PDF→image. SC-billed per call,
audited to `tool_calls`.

Auth: service-key + X-Agent-Id (same as the rest of `/api/v1/...`). The
public `/health` endpoint is unauthenticated so the Commons landing page
can probe availability.
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import time
import uuid
import zipfile
from pathlib import Path
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Response,
    UploadFile,
)
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.service_auth import require_service_key_and_agent
from app.models.agent import Agent
from app.services.ledger import (
    InsufficientFundsError,
    record_transaction,
)


router = APIRouter(prefix="/commons/pdf", tags=["commons-pdf"])


# ── SC cost table — flat per-call. Tweakable via env. ──────────────────────
SC_COST = {
    "merge": float(os.environ.get("PDF_SC_MERGE", "2")),
    "split": float(os.environ.get("PDF_SC_SPLIT", "2")),
    "compress": float(os.environ.get("PDF_SC_COMPRESS", "3")),
    "ocr": float(os.environ.get("PDF_SC_OCR", "10")),
    "image_to_pdf": float(os.environ.get("PDF_SC_IMG2PDF", "2")),
    "pdf_to_image": float(os.environ.get("PDF_SC_PDF2IMG", "3")),
}

MAX_UPLOAD_BYTES = int(os.environ.get("PDF_MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))


def _have(binary: str) -> bool:
    return shutil.which(binary) is not None


def _capabilities() -> dict[str, bool]:
    caps = {
        "merge": False,
        "split": False,
        "compress": False,
        "ocr": False,
        "image_to_pdf": False,
        "pdf_to_image": False,
    }
    try:
        import pypdf  # noqa: F401
        caps["merge"] = caps["split"] = True
    except ImportError:
        pass
    try:
        import pikepdf  # noqa: F401
        caps["compress"] = True
    except ImportError:
        pass
    if _have("tesseract") and _have("gs"):
        try:
            import ocrmypdf  # noqa: F401
            caps["ocr"] = True
        except ImportError:
            pass
    try:
        import img2pdf  # noqa: F401
        caps["image_to_pdf"] = True
    except ImportError:
        pass
    if _have("pdftoppm"):
        try:
            import pdf2image  # noqa: F401
            caps["pdf_to_image"] = True
        except ImportError:
            pass
    return caps


# ── Audit + billing helpers ────────────────────────────────────────────────

async def _read_upload(f: UploadFile, *, label: str) -> bytes:
    data = await f.read()
    if not data:
        raise HTTPException(status_code=400, detail=f"{label}_empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail={
                "error": "upload_too_large",
                "limit_bytes": MAX_UPLOAD_BYTES,
                "got_bytes": len(data),
                "field": label,
            },
        )
    return data


async def _log_tool_call(
    db: AsyncSession,
    *,
    agent_id: str,
    tool_name: str,
    input_summary: dict,
    output_len: int,
    sc_cost: float,
    latency_ms: int,
    error: str | None = None,
) -> None:
    """Insert a row into `tool_calls`. Raw SQL — no ORM model exists."""
    await db.execute(
        text(
            """
            INSERT INTO tool_calls
                (id, agent_id, tool_name, input_json, output_len,
                 sc_cost, latency_ms, error, created_at)
            VALUES
                (:id, :agent_id, :tool_name, CAST(:input_json AS JSONB),
                 :output_len, :sc_cost, :latency_ms, :error, NOW())
            """
        ),
        {
            "id": str(uuid.uuid4()),
            "agent_id": agent_id,
            "tool_name": tool_name,
            "input_json": _json_dump(input_summary),
            "output_len": output_len,
            "sc_cost": sc_cost,
            "latency_ms": latency_ms,
            "error": error,
        },
    )


def _json_dump(d: dict) -> str:
    import json
    return json.dumps(d, default=str)


async def _charge(
    db: AsyncSession,
    agent: Agent,
    *,
    tool: str,
    ref: str,
) -> float:
    cost = SC_COST[tool]
    if cost <= 0:
        return 0.0
    try:
        await record_transaction(
            db,
            agent_id=agent.id,
            delta=-cost,
            reason=f"commons.pdf.{tool}",
            ref_id=ref,
            note=f"PDF Toolkit: {tool}",
        )
    except InsufficientFundsError as e:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "insufficient_sc",
                "balance": e.balance,
                "needed": cost,
                "tool": f"commons.pdf.{tool}",
            },
        )
    return cost


# ── /health (public) ───────────────────────────────────────────────────────

@router.get("/health")
async def health() -> dict:
    caps = _capabilities()
    return {
        "service": "commons.pdf",
        "version": "1.0.0",
        "available": caps,
        "all_available": all(caps.values()),
        "sc_cost": SC_COST,
        "max_upload_bytes": MAX_UPLOAD_BYTES,
    }


# ── /merge ─────────────────────────────────────────────────────────────────

@router.post("/merge")
async def merge_pdfs(
    files: Annotated[list[UploadFile], File(description="2+ PDF files to merge in order")],
    db: AsyncSession = Depends(get_db),
    agent: Agent = Depends(require_service_key_and_agent),
) -> Response:
    if len(files) < 2:
        raise HTTPException(status_code=400, detail="need_at_least_2_files")

    from pypdf import PdfReader, PdfWriter

    t0 = time.monotonic()
    ref = str(uuid.uuid4())
    await _charge(db, agent, tool="merge", ref=ref)

    writer = PdfWriter()
    page_total = 0
    in_sizes: list[int] = []
    try:
        for i, f in enumerate(files):
            data = await _read_upload(f, label=f"file_{i}")
            in_sizes.append(len(data))
            try:
                reader = PdfReader(io.BytesIO(data))
            except Exception as e:
                raise HTTPException(
                    status_code=400,
                    detail={"error": "bad_pdf", "file_index": i, "msg": str(e)[:200]},
                )
            for p in reader.pages:
                writer.add_page(p)
                page_total += 1

        out = io.BytesIO()
        writer.write(out)
        body = out.getvalue()

        await _log_tool_call(
            db, agent_id=agent.id, tool_name="commons.pdf.merge",
            input_summary={"file_count": len(files), "in_bytes": in_sizes, "pages": page_total},
            output_len=len(body),
            sc_cost=SC_COST["merge"],
            latency_ms=int((time.monotonic() - t0) * 1000),
        )
        await db.commit()

        return Response(
            content=body,
            media_type="application/pdf",
            headers={
                "Content-Disposition": 'attachment; filename="merged.pdf"',
                "X-SC-Cost": str(SC_COST["merge"]),
                "X-Pages": str(page_total),
            },
        )
    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail={"error": "merge_failed", "msg": str(e)[:200]})


# ── /split ─────────────────────────────────────────────────────────────────

@router.post("/split")
async def split_pdf(
    file: Annotated[UploadFile, File(description="PDF to split")],
    ranges: Annotated[
        str,
        Form(description='Comma-separated page ranges, 1-indexed inclusive. e.g. "1-3,4-6,7"'),
    ],
    db: AsyncSession = Depends(get_db),
    agent: Agent = Depends(require_service_key_and_agent),
) -> Response:
    from pypdf import PdfReader, PdfWriter

    parsed = _parse_ranges(ranges)
    if not parsed:
        raise HTTPException(status_code=400, detail="ranges_invalid")

    t0 = time.monotonic()
    ref = str(uuid.uuid4())
    await _charge(db, agent, tool="split", ref=ref)

    data = await _read_upload(file, label="file")
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail={"error": "bad_pdf", "msg": str(e)[:200]})

    n = len(reader.pages)
    for r in parsed:
        if r[0] < 1 or r[1] > n or r[0] > r[1]:
            await db.rollback()
            raise HTTPException(
                status_code=400,
                detail={"error": "range_out_of_bounds", "range": list(r), "page_count": n},
            )

    try:
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for idx, (start, end) in enumerate(parsed, start=1):
                w = PdfWriter()
                for i in range(start - 1, end):
                    w.add_page(reader.pages[i])
                buf = io.BytesIO()
                w.write(buf)
                zf.writestr(f"part_{idx:03d}_p{start}-p{end}.pdf", buf.getvalue())
        body = zip_buf.getvalue()

        await _log_tool_call(
            db, agent_id=agent.id, tool_name="commons.pdf.split",
            input_summary={"in_bytes": len(data), "ranges": parsed, "page_count": n},
            output_len=len(body),
            sc_cost=SC_COST["split"],
            latency_ms=int((time.monotonic() - t0) * 1000),
        )
        await db.commit()

        return Response(
            content=body,
            media_type="application/zip",
            headers={
                "Content-Disposition": 'attachment; filename="split.zip"',
                "X-SC-Cost": str(SC_COST["split"]),
                "X-Parts": str(len(parsed)),
            },
        )
    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail={"error": "split_failed", "msg": str(e)[:200]})


def _parse_ranges(spec: str) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            try:
                ai, bi = int(a), int(b)
            except ValueError:
                return []
            out.append((ai, bi))
        else:
            try:
                v = int(part)
            except ValueError:
                return []
            out.append((v, v))
    return out


# ── /compress ──────────────────────────────────────────────────────────────

@router.post("/compress")
async def compress_pdf(
    file: Annotated[UploadFile, File(description="PDF to compress")],
    level: Annotated[
        str,
        Form(description="screen|ebook|printer (ghostscript -dPDFSETTINGS). Default ebook."),
    ] = "ebook",
    db: AsyncSession = Depends(get_db),
    agent: Agent = Depends(require_service_key_and_agent),
) -> Response:
    if level not in {"screen", "ebook", "printer", "prepress"}:
        raise HTTPException(status_code=400, detail="bad_level")

    t0 = time.monotonic()
    ref = str(uuid.uuid4())
    await _charge(db, agent, tool="compress", ref=ref)

    data = await _read_upload(file, label="file")
    in_size = len(data)

    tmpdir = Path(f"/tmp/pdftk-{ref}")
    tmpdir.mkdir(parents=True, exist_ok=True)
    in_path = tmpdir / "in.pdf"
    out_path = tmpdir / "out.pdf"
    try:
        in_path.write_bytes(data)

        # Ghostscript first — best size reduction. Falls back to pikepdf
        # save+linearize if gs is missing or fails.
        body: bytes
        used = "ghostscript"
        if _have("gs"):
            try:
                cmd = [
                    "gs", "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.5",
                    f"-dPDFSETTINGS=/{level}",
                    "-dNOPAUSE", "-dQUIET", "-dBATCH",
                    f"-sOutputFile={out_path}", str(in_path),
                ]
                r = subprocess.run(cmd, capture_output=True, timeout=120)
                if r.returncode != 0 or not out_path.exists():
                    raise RuntimeError(f"gs failed rc={r.returncode}: {r.stderr[:300].decode(errors='ignore')}")
                body = out_path.read_bytes()
            except Exception:
                used = "pikepdf"
                import pikepdf
                with pikepdf.open(str(in_path)) as pdf:
                    pdf.save(str(out_path), linearize=True)
                body = out_path.read_bytes()
        else:
            used = "pikepdf"
            import pikepdf
            with pikepdf.open(str(in_path)) as pdf:
                pdf.save(str(out_path), linearize=True)
            body = out_path.read_bytes()

        await _log_tool_call(
            db, agent_id=agent.id, tool_name="commons.pdf.compress",
            input_summary={"in_bytes": in_size, "level": level, "engine": used},
            output_len=len(body),
            sc_cost=SC_COST["compress"],
            latency_ms=int((time.monotonic() - t0) * 1000),
        )
        await db.commit()

        ratio = round(len(body) / in_size, 3) if in_size else 1.0
        return Response(
            content=body,
            media_type="application/pdf",
            headers={
                "Content-Disposition": 'attachment; filename="compressed.pdf"',
                "X-SC-Cost": str(SC_COST["compress"]),
                "X-Engine": used,
                "X-In-Bytes": str(in_size),
                "X-Out-Bytes": str(len(body)),
                "X-Ratio": str(ratio),
            },
        )
    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail={"error": "compress_failed", "msg": str(e)[:200]})
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ── /ocr ───────────────────────────────────────────────────────────────────

@router.post("/ocr")
async def ocr_pdf(
    file: Annotated[UploadFile, File(description="PDF or image to OCR")],
    languages: Annotated[
        str, Form(description="Tesseract language codes, '+' separated. e.g. eng+hin. Default eng.")
    ] = "eng",
    db: AsyncSession = Depends(get_db),
    agent: Agent = Depends(require_service_key_and_agent),
) -> Response:
    if not _have("tesseract") or not _have("gs"):
        raise HTTPException(status_code=503, detail="ocr_unavailable")
    try:
        import ocrmypdf  # noqa: F401
    except ImportError:
        raise HTTPException(status_code=503, detail="ocrmypdf_not_installed")

    t0 = time.monotonic()
    ref = str(uuid.uuid4())
    await _charge(db, agent, tool="ocr", ref=ref)

    data = await _read_upload(file, label="file")
    name = (file.filename or "").lower()
    is_image = name.endswith((".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".webp"))

    tmpdir = Path(f"/tmp/pdftk-{ref}")
    tmpdir.mkdir(parents=True, exist_ok=True)
    in_path = tmpdir / ("in.img" if is_image else "in.pdf")
    src_pdf = tmpdir / "src.pdf"
    out_path = tmpdir / "out.pdf"
    try:
        in_path.write_bytes(data)

        # Image input: pre-convert to PDF via img2pdf so ocrmypdf has
        # something to consume.
        if is_image:
            import img2pdf
            with open(src_pdf, "wb") as f:
                f.write(img2pdf.convert(str(in_path)))
        else:
            src_pdf = in_path

        import ocrmypdf as _ocr
        try:
            _ocr.ocr(
                str(src_pdf), str(out_path),
                language=languages,
                force_ocr=True,
                progress_bar=False,
                output_type="pdf",
                deskew=True,
            )
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail={"error": "ocr_failed", "msg": str(e)[:300]},
            )

        body = out_path.read_bytes()

        await _log_tool_call(
            db, agent_id=agent.id, tool_name="commons.pdf.ocr",
            input_summary={"in_bytes": len(data), "languages": languages, "is_image": is_image},
            output_len=len(body),
            sc_cost=SC_COST["ocr"],
            latency_ms=int((time.monotonic() - t0) * 1000),
        )
        await db.commit()

        return Response(
            content=body,
            media_type="application/pdf",
            headers={
                "Content-Disposition": 'attachment; filename="ocr.pdf"',
                "X-SC-Cost": str(SC_COST["ocr"]),
                "X-Languages": languages,
            },
        )
    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail={"error": "ocr_failed", "msg": str(e)[:200]})
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ── /image-to-pdf ──────────────────────────────────────────────────────────

@router.post("/image-to-pdf")
async def image_to_pdf(
    files: Annotated[list[UploadFile], File(description="1+ image files (jpg/png/etc)")],
    db: AsyncSession = Depends(get_db),
    agent: Agent = Depends(require_service_key_and_agent),
) -> Response:
    if not files:
        raise HTTPException(status_code=400, detail="need_at_least_1_image")

    import img2pdf
    from PIL import Image

    t0 = time.monotonic()
    ref = str(uuid.uuid4())
    await _charge(db, agent, tool="image_to_pdf", ref=ref)

    blobs: list[bytes] = []
    in_sizes: list[int] = []
    try:
        for i, f in enumerate(files):
            data = await _read_upload(f, label=f"file_{i}")
            in_sizes.append(len(data))
            # img2pdf rejects images with alpha channels — flatten via PIL.
            try:
                im = Image.open(io.BytesIO(data))
                if im.mode in ("RGBA", "LA", "P"):
                    bg = Image.new("RGB", im.size, (255, 255, 255))
                    if im.mode == "P":
                        im = im.convert("RGBA")
                    bg.paste(im, mask=im.split()[-1] if im.mode in ("RGBA", "LA") else None)
                    out = io.BytesIO()
                    bg.save(out, format="JPEG", quality=92)
                    blobs.append(out.getvalue())
                else:
                    blobs.append(data)
            except Exception as e:
                raise HTTPException(
                    status_code=400,
                    detail={"error": "bad_image", "file_index": i, "msg": str(e)[:200]},
                )

        body = img2pdf.convert(blobs)

        await _log_tool_call(
            db, agent_id=agent.id, tool_name="commons.pdf.image_to_pdf",
            input_summary={"image_count": len(files), "in_bytes": in_sizes},
            output_len=len(body),
            sc_cost=SC_COST["image_to_pdf"],
            latency_ms=int((time.monotonic() - t0) * 1000),
        )
        await db.commit()

        return Response(
            content=body,
            media_type="application/pdf",
            headers={
                "Content-Disposition": 'attachment; filename="images.pdf"',
                "X-SC-Cost": str(SC_COST["image_to_pdf"]),
                "X-Images": str(len(files)),
            },
        )
    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail={"error": "image_to_pdf_failed", "msg": str(e)[:200]})


# ── /pdf-to-image ──────────────────────────────────────────────────────────

@router.post("/pdf-to-image")
async def pdf_to_image(
    file: Annotated[UploadFile, File(description="PDF to rasterize")],
    fmt: Annotated[str, Form(description="png|jpeg")] = "png",
    dpi: Annotated[int, Form(description="DPI (50-300)")] = 150,
    db: AsyncSession = Depends(get_db),
    agent: Agent = Depends(require_service_key_and_agent),
) -> Response:
    if fmt not in {"png", "jpeg"}:
        raise HTTPException(status_code=400, detail="bad_fmt")
    if dpi < 50 or dpi > 300:
        raise HTTPException(status_code=400, detail="dpi_out_of_range")
    if not _have("pdftoppm"):
        raise HTTPException(status_code=503, detail="poppler_unavailable")

    from pdf2image import convert_from_bytes

    t0 = time.monotonic()
    ref = str(uuid.uuid4())
    await _charge(db, agent, tool="pdf_to_image", ref=ref)

    data = await _read_upload(file, label="file")
    try:
        try:
            pages = convert_from_bytes(data, dpi=dpi, fmt=fmt)
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail={"error": "bad_pdf_or_render", "msg": str(e)[:200]},
            )

        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for i, im in enumerate(pages, start=1):
                buf = io.BytesIO()
                im.save(buf, format="PNG" if fmt == "png" else "JPEG",
                        quality=92 if fmt == "jpeg" else None)
                zf.writestr(f"page_{i:03d}.{fmt}", buf.getvalue())
        body = zip_buf.getvalue()

        await _log_tool_call(
            db, agent_id=agent.id, tool_name="commons.pdf.pdf_to_image",
            input_summary={"in_bytes": len(data), "fmt": fmt, "dpi": dpi, "pages": len(pages)},
            output_len=len(body),
            sc_cost=SC_COST["pdf_to_image"],
            latency_ms=int((time.monotonic() - t0) * 1000),
        )
        await db.commit()

        return Response(
            content=body,
            media_type="application/zip",
            headers={
                "Content-Disposition": 'attachment; filename="pages.zip"',
                "X-SC-Cost": str(SC_COST["pdf_to_image"]),
                "X-Pages": str(len(pages)),
                "X-Format": fmt,
                "X-DPI": str(dpi),
            },
        )
    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail={"error": "pdf_to_image_failed", "msg": str(e)[:200]})
