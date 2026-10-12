"""Image processing: resize and convert PNG, JPEG or WebP (spec section 5).

Jobs run one at a time on a single dedicated thread, so a single CPU core is never
oversubscribed; a small admission limit bounds how many jobs (and their input bytes) can wait.
The service measures CPU time around each job and emits its own usage events.
"""

import asyncio
import io
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from PIL import Image, UnidentifiedImageError

from app.config import settings
from app.pipeline.events import UsageEvent, request_event_id
from app.pipeline.producer import emit
from app.services.trust import Tenant, gateway_tenant
from app.timeutil import utcnow

router = APIRouter(prefix="/v1/images", tags=["images"])

ALLOWED_FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
FORMAT_ALIASES = {"png": "PNG", "jpeg": "JPEG", "jpg": "JPEG", "webp": "WEBP"}
MAX_WAITING_JOBS = 8

# Pillow's own decompression-bomb guard, as a second line behind the explicit check below.
Image.MAX_IMAGE_PIXELS = settings.max_image_pixels

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="image-job")
_admission = asyncio.Semaphore(MAX_WAITING_JOBS)


class JobError(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail


def run_job(data: bytes, width: int | None, height: int | None, out_format: str | None) -> tuple[bytes, str, int]:
    """Runs on the image thread. Returns (output bytes, media type, CPU milliseconds)."""
    started = time.thread_time()
    try:
        image = Image.open(io.BytesIO(data))
    except Image.DecompressionBombError as exc:
        raise JobError(413, f"images are limited to {settings.max_image_pixels} pixels") from exc
    except (UnidentifiedImageError, OSError) as exc:
        raise JobError(415, "input is not a PNG, JPEG or WebP image") from exc
    if image.format not in ALLOWED_FORMATS:
        raise JobError(415, "input is not a PNG, JPEG or WebP image")
    src_w, src_h = image.size
    # Checked from the header, before decoding, to defeat decompression bombs.
    if src_w * src_h > settings.max_image_pixels:
        raise JobError(413, f"images are limited to {settings.max_image_pixels} pixels")

    if width and height:
        target = (width, height)
    elif width:
        target = (width, max(1, round(src_h * width / src_w)))
    elif height:
        target = (max(1, round(src_w * height / src_h)), height)
    else:
        target = (src_w, src_h)
    if target[0] * target[1] > settings.max_output_pixels:
        raise JobError(413, f"output is limited to {settings.max_output_pixels} pixels")

    fmt = out_format or image.format
    try:
        image.load()
        if target != (src_w, src_h):
            image = image.resize(target, Image.Resampling.LANCZOS)
        if fmt == "JPEG" and image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        out = io.BytesIO()
        image.save(out, format=fmt)
    except Image.DecompressionBombError as exc:
        raise JobError(413, "image is too large") from exc
    except OSError as exc:
        raise JobError(422, f"could not process image: {exc}") from exc
    cpu_ms = max(1, round((time.thread_time() - started) * 1000))
    return out.getvalue(), ALLOWED_FORMATS[fmt], cpu_ms


@router.post("/transform")
async def transform(
    request: Request,
    width: int | None = Query(default=None, ge=1, le=8000),
    height: int | None = Query(default=None, ge=1, le=8000),
    format: str | None = Query(default=None, description="png, jpeg or webp"),
    tenant: Tenant = Depends(gateway_tenant),
) -> Response:
    out_format = None
    if format is not None:
        out_format = FORMAT_ALIASES.get(format.lower())
        if out_format is None:
            raise HTTPException(status_code=400, detail="format must be png, jpeg or webp")

    limit = settings.max_image_bytes
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail=f"image inputs are limited to {limit} bytes")
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > limit:
            raise HTTPException(status_code=413, detail=f"image inputs are limited to {limit} bytes")
    if not buf:
        raise HTTPException(status_code=400, detail="send the image as the request body")

    if _admission.locked():
        raise HTTPException(status_code=503, detail="image queue is full", headers={"Retry-After": "2"})
    async with _admission:
        loop = asyncio.get_running_loop()
        try:
            output, media_type, cpu_ms = await loop.run_in_executor(
                _executor, run_job, bytes(buf), width, height, out_format
            )
        except JobError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc

    job_id = str(uuid.uuid4())
    now = utcnow()
    await emit(
        [
            UsageEvent(
                event_id=request_event_id(job_id, metric),
                org_id=tenant.org_id,
                project_id=tenant.project_id,
                metric=metric,
                quantity=quantity,
                event_time=now,
                source="image-service",
            )
            for metric, quantity in (("image.bytes_processed", len(buf)), ("image.cpu_seconds", cpu_ms))
        ]
    )
    return Response(content=output, media_type=media_type, headers={"x-cpu-ms": str(cpu_ms), "x-job-id": job_id})
