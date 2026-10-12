"""The services trust only the gateway: requests must carry the internal shared secret.

They read the tenant from headers the gateway sets, so moving a service to another machine
is a configuration change (the gateway forwards over HTTP instead of calling in-process).
"""

from dataclasses import dataclass

from fastapi import Header, HTTPException

from app.config import settings
from app.security import constant_time_equals


@dataclass(frozen=True)
class Tenant:
    org_id: str
    project_id: str
    request_id: str


async def gateway_tenant(
    x_internal_secret: str | None = Header(default=None),
    x_tenant_org: str | None = Header(default=None),
    x_tenant_project: str | None = Header(default=None),
    x_request_id: str | None = Header(default=None),
) -> Tenant:
    if not x_internal_secret or not constant_time_equals(x_internal_secret, settings.internal_secret):
        raise HTTPException(status_code=403, detail="services accept requests only from the gateway")
    if not x_tenant_org or not x_tenant_project or not x_request_id:
        raise HTTPException(status_code=403, detail="missing tenant context")
    return Tenant(org_id=x_tenant_org, project_id=x_tenant_project, request_id=x_request_id)
