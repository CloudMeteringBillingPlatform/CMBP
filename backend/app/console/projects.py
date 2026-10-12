"""Projects and API keys. Every query is scoped by the caller's organization."""

import uuid

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from app import db
from app.console.deps import Principal, current_principal, rate_limited, require_owner
from app.gateway.keys import invalidate
from app.redis_client import get_redis
from app.security import generate_api_key

router = APIRouter(prefix="/api", tags=["projects"])


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9 _.-]*$")


class KeyIn(BaseModel):
    name: str = Field(default="", max_length=64)


def _uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="not found") from exc


@router.get("/projects")
async def list_projects(p: Principal = Depends(current_principal)) -> dict:
    rows = await db.fetch_all(
        """
        SELECT pr.id, pr.name, pr.created_at,
               COUNT(k.id) FILTER (WHERE k.revoked_at IS NULL) AS active_keys,
               COALESCE((SELECT SUM(size_bytes) FROM stored_objects s WHERE s.project_id = pr.id), 0) AS stored_bytes
        FROM projects pr LEFT JOIN api_keys k ON k.project_id = pr.id
        WHERE pr.org_id = %s
        GROUP BY pr.id ORDER BY pr.created_at
        """,
        (p.org_id,),
    )
    return {"projects": rows}


@router.post("/projects", status_code=201)
async def create_project(body: ProjectIn, p: Principal = Depends(require_owner)) -> dict:
    try:
        row = await db.fetch_one(
            "INSERT INTO projects (org_id, name) VALUES (%s, %s) RETURNING id, name, created_at",
            (p.org_id, body.name.strip()),
        )
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(status_code=409, detail="a project with this name already exists") from exc
    return row


async def _own_project(p: Principal, project_id: str) -> dict:
    row = await db.fetch_one(
        "SELECT id, name FROM projects WHERE id = %s AND org_id = %s", (_uuid(project_id), p.org_id)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="project not found")
    return row


@router.post(
    "/projects/{project_id}/keys",
    status_code=201,
    dependencies=[Depends(rate_limited("key-create"))],
)
async def create_key(project_id: str, body: KeyIn, p: Principal = Depends(require_owner)) -> dict:
    project = await _own_project(p, project_id)
    full_key, prefix, key_hash = generate_api_key()
    row = await db.fetch_one(
        """
        INSERT INTO api_keys (org_id, project_id, name, prefix, key_hash)
        VALUES (%s, %s, %s, %s, %s) RETURNING id, name, prefix, created_at
        """,
        (p.org_id, project["id"], body.name.strip(), prefix, key_hash),
    )
    # The full key appears only in this response; only its SHA-256 hash is stored.
    return {**row, "project_id": project["id"], "key": full_key}


@router.get("/projects/{project_id}/keys")
async def list_keys(project_id: str, p: Principal = Depends(require_owner)) -> dict:
    project = await _own_project(p, project_id)
    rows = await db.fetch_all(
        """
        SELECT id, name, prefix, created_at, revoked_at,
               CASE WHEN revoked_at IS NULL THEN 'active' ELSE 'revoked' END AS status
        FROM api_keys WHERE project_id = %s AND org_id = %s ORDER BY created_at DESC
        """,
        (project["id"], p.org_id),
    )
    return {"keys": rows}


@router.delete("/keys/{key_id}", status_code=204)
async def revoke_key(key_id: str, p: Principal = Depends(require_owner)) -> Response:
    row = await db.fetch_one(
        """
        UPDATE api_keys SET revoked_at = COALESCE(revoked_at, now())
        WHERE id = %s AND org_id = %s RETURNING key_hash
        """,
        (_uuid(key_id), p.org_id),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="key not found")
    try:
        await invalidate(get_redis(), [row["key_hash"]])
    except Exception:  # noqa: BLE001 - the cache TTL still bounds revocation
        pass
    return Response(status_code=204)
