"""limit/offset in, a self-describing page out.

    GET /v1/clients?limit=50&offset=100
    {"items": [...], "total": 312, "limit": 50, "offset": 100, "next_offset": 150}

`next_offset` is null on the last page, so a client loops until it is null
instead of computing anything.
"""

from __future__ import annotations

from typing import Any, Generic, List, Optional, Sequence, TypeVar

from fastapi import Query
from pydantic import BaseModel

T = TypeVar("T")

MAX_LIMIT = 200


class PageParams:
    def __init__(self,
                 limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
                 offset: int = Query(default=0, ge=0)) -> None:
        self.limit = limit
        self.offset = offset


class Page(BaseModel, Generic[T]):
    items: List[T]
    total: int
    limit: int
    offset: int
    next_offset: Optional[int] = None


def paginate(rows: Sequence[Any], params: PageParams) -> dict:
    total = len(rows)
    items = list(rows[params.offset:params.offset + params.limit])
    end = params.offset + len(items)
    return {"items": items, "total": total, "limit": params.limit,
            "offset": params.offset,
            "next_offset": end if end < total else None}
