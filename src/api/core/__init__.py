"""Cross-cutting pieces every /v1 module uses.

    errors.py      ApiError and the ONE error envelope /v1 returns
    context.py     request ids, the X-Actor header, the audit log
    pagination.py  limit/offset in, {items, total, ...} out
"""
