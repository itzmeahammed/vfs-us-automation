"""VFS accounts: the login pool and its health.

    GET  /v1/accounts                    health of every account, per route
    POST /v1/accounts/{email}/clear      lift a bench (after checking the account)
    POST /v1/accounts/{email}/bench      take an account out of rotation
"""

from fastapi import APIRouter, Depends

from src.api.modules.accounts import handlers as handlers
from src.api.core.errors import ERROR_RESPONSES
from src.api.core.security import require_token

router = APIRouter(prefix="/accounts", tags=["accounts"],
                   dependencies=[Depends(require_token)], responses=ERROR_RESPONSES)

router.get("", summary="Account health")(handlers.get_account_health)
router.post("/{email}/clear", summary="Lift a bench")(handlers.clear_account)
router.post("/{email}/bench", summary="Bench an account")(handlers.bench_account)
