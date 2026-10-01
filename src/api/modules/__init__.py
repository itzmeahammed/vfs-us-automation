"""The /v1 API, one package per business area.

    system/         health, readiness, master switches, config, overview, audit
    accounts/       the VFS account pool and its health
    catalog/        routes (countries) and their combos
    clients/        ONE client resource; the agent picks flow: waitlist | live
    waitlist/       registration runs, posture, journal repair, reconcile
    booking/        manual booking runs, invitations, payments
    jobs/           every background run: status, logs, live stream, cancel
    notifications/  webhook and Telegram

Each module's router.py is the public contract. Where behaviour already
existed, the router reuses the tested handler from the legacy module
(src/api/<name>.py) rather than copying it; new behaviour lives in the
module's service.py. The legacy paths stay mounted, marked deprecated.
"""

from fastapi import APIRouter


def v1_router() -> APIRouter:
    from src.api.modules.accounts.router import router as accounts
    from src.api.modules.booking.router import router as booking
    from src.api.modules.catalog.router import router as catalog
    from src.api.modules.clients.router import router as clients
    from src.api.modules.jobs.router import router as jobs
    from src.api.modules.notifications.router import router as notifications
    from src.api.modules.system.router import public_router, router as system
    from src.api.modules.waitlist.router import router as waitlist

    root = APIRouter(prefix="/v1")
    for sub in (public_router, system, accounts, catalog, clients, waitlist,
                booking, jobs, notifications):
        root.include_router(sub)
    return root
