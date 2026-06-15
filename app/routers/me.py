"""Current-user endpoint — lets the frontend read the caller's own identity
and capabilities (admin? can submit PMs?) which live in portal_users and are
not present in the Supabase session the browser holds.
"""

from __future__ import annotations

from fastapi import APIRouter

from ..auth import CurrentUser
from ..config import DEV_BYPASS_AUTH

router = APIRouter(prefix="/api", tags=["me"])

ADMIN_EMAIL = "kris.hall@rechargealaska.net"


@router.get("/me")
async def get_me(user: CurrentUser):
    is_admin = DEV_BYPASS_AUTH or user.email == ADMIN_EMAIL
    return {
        "email": user.email,
        "name": user.name,
        "is_admin": is_admin,
        # Admins implicitly can submit; otherwise honor the per-user flag.
        "can_submit_pm": is_admin or user.can_submit_pm,
        "allowed_evse_ids": user.allowed_evse_ids,
    }
