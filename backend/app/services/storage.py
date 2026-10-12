"""Object storage: files on disk, metadata in Postgres (spec section 5).

Hard limits:
  * each upload is capped (10 MB), checked while streaming the body;
  * each organization's total stored bytes are capped by its plan, checked EXACTLY inside the
    upload transaction. The organization row is locked FOR UPDATE first, so concurrent uploads
    for one organization are serialized and can never push it past its cap together.
Objects are stored under generated UUID names, never under user-supplied paths.
"""

import os
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from app import db
from app.billing.plans import current_plan
from app.config import settings
from app.services.trust import Tenant, gateway_tenant

router = APIRouter(prefix="/v1/storage", tags=["storage"])

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
CHUNK = 64 * 1024


def _valid_name(name: str) -> str:
    if not NAME_RE.fullmatch(name) or ".." in name:
        raise HTTPException(status_code=400, detail="object names use letters, digits, '.', '_' and '-' (max 255)")
    return name


def _root() -> Path:
    root = Path(settings.storage_dir).resolve()
    (root / "tmp").mkdir(parents=True, exist_ok=True)
    return root


def _object_path(org_id: str, object_id: str) -> Path:
    directory = _root() / str(uuid.UUID(org_id))
    directory.mkdir(parents=True, exist_ok=True)
    return directory / str(uuid.UUID(object_id))


def _remove(path: Path | str | None) -> None:
    if path:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


@router.put("/objects/{name}", status_code=201)
async def upload(name: str, request: Request, tenant: Tenant = Depends(gateway_tenant)) -> dict:
    _valid_name(name)
    limit = settings.max_object_bytes
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail=f"objects are limited to {limit} bytes")

    tmp_path = _root() / "tmp" / str(uuid.uuid4())
    size = 0
    try:
        with open(tmp_path, "wb") as fh:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise HTTPException(status_code=413, detail=f"objects are limited to {limit} bytes")
                fh.write(chunk)
    except BaseException:
        _remove(tmp_path)
        raise

    content_type = request.headers.get("content-type") or "application/octet-stream"
    object_id = str(uuid.uuid4())
    final_path = _object_path(tenant.org_id, object_id)
    replaced_path: str | None = None
    try:
        async with db.connection() as conn:
            async with conn.transaction():
                # Serialize uploads per organization so the cap check below is exact.
                await conn.execute("SELECT 1 FROM orgs WHERE id = %s FOR UPDATE", (tenant.org_id,))
                plan = await current_plan(conn, tenant.org_id)
                cap = int(plan["limits"]["storage_bytes_cap"])
                cur = await conn.execute(
                    """
                    SELECT
                      COALESCE(SUM(size_bytes), 0) AS total,
                      COALESCE(SUM(size_bytes) FILTER (WHERE project_id = %s AND name = %s), 0) AS replaced
                    FROM stored_objects WHERE org_id = %s
                    """,
                    (tenant.project_id, name, tenant.org_id),
                )
                usage = await cur.fetchone()
                after = int(usage["total"]) - int(usage["replaced"]) + size
                if after > cap:
                    raise HTTPException(
                        status_code=429,
                        detail=f"storage quota exceeded: {after} bytes would exceed the plan cap of {cap}",
                    )
                cur = await conn.execute(
                    "SELECT path FROM stored_objects WHERE project_id = %s AND name = %s",
                    (tenant.project_id, name),
                )
                old = await cur.fetchone()
                replaced_path = old["path"] if old else None
                await conn.execute(
                    """
                    INSERT INTO stored_objects (id, org_id, project_id, name, size_bytes, path, content_type)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (project_id, name) DO UPDATE
                      SET id = EXCLUDED.id, size_bytes = EXCLUDED.size_bytes, path = EXCLUDED.path,
                          content_type = EXCLUDED.content_type, created_at = now()
                    """,
                    (object_id, tenant.org_id, tenant.project_id, name, size, str(final_path), content_type),
                )
                os.replace(tmp_path, final_path)
    except BaseException:
        # Clean up whichever file exists: the temp file, or the moved file if the commit failed.
        _remove(tmp_path)
        _remove(final_path)
        raise
    if replaced_path and replaced_path != str(final_path):
        _remove(replaced_path)
    return {"name": name, "size_bytes": size, "content_type": content_type}


async def _find(tenant: Tenant, name: str) -> dict:
    row = await db.fetch_one(
        """
        SELECT name, size_bytes, path, content_type, created_at FROM stored_objects
        WHERE org_id = %s AND project_id = %s AND name = %s
        """,
        (tenant.org_id, tenant.project_id, name),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="object not found")
    return row


@router.get("/objects/{name}")
async def download(name: str, tenant: Tenant = Depends(gateway_tenant)) -> StreamingResponse:
    _valid_name(name)
    row = await _find(tenant, name)
    path = row["path"]

    def chunks():
        with open(path, "rb") as fh:
            while chunk := fh.read(CHUNK):
                yield chunk

    return StreamingResponse(
        chunks(),
        media_type=row["content_type"],
        headers={"content-length": str(row["size_bytes"])},
    )


@router.get("/objects")
async def list_objects(tenant: Tenant = Depends(gateway_tenant)) -> dict:
    rows = await db.fetch_all(
        """
        SELECT name, size_bytes, content_type, created_at FROM stored_objects
        WHERE org_id = %s AND project_id = %s ORDER BY name
        """,
        (tenant.org_id, tenant.project_id),
    )
    return {"objects": rows}


@router.delete("/objects/{name}", status_code=204)
async def delete(name: str, tenant: Tenant = Depends(gateway_tenant)) -> Response:
    _valid_name(name)
    row = await db.fetch_one(
        "DELETE FROM stored_objects WHERE org_id = %s AND project_id = %s AND name = %s RETURNING path",
        (tenant.org_id, tenant.project_id, name),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="object not found")
    _remove(row["path"])
    return Response(status_code=204)
