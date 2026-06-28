"""Current-user endpoint — lets the frontend read the caller's own identity
and capabilities (admin? can submit PMs?) which live in portal_users and are
not present in the Supabase session the browser holds.
"""

from __future__ import annotations

from fastapi import APIRouter

from ..auth import CurrentUser, filter_evse_ids
from ..config import DEV_BYPASS_AUTH
from ..constants import (
    display_name,
    get_all_station_ids,
    get_archived_station_ids,
    location_label,
)

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


@router.get("/evses")
async def list_evses(user: CurrentUser):
    """Live EVSE roster for filter UIs (Sessions, Export).

    Reflects EVSEs registered via the Admin tab (runtime overrides) so newly
    onboarded chargers appear without a frontend redeploy. Scoped to the caller's
    allowed EVSEs and excludes archived units.
    """
    archived = set(get_archived_station_ids())
    visible  = [s for s in get_all_station_ids() if s not in archived]
    allowed  = filter_evse_ids(visible, user.allowed_evse_ids)
    return [
        {
            "id":       sid,
            "name":     display_name(sid),
            "location": location_label(sid),
        }
        for sid in sorted(allowed, key=display_name)
    ]
