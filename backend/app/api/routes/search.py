"""Search routes. Permission filtering happens inside the SQL query."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dependencies import CohortDep, DbDep, PaginationDep
from app.schemas.content import SearchResponseOut
from app.schemas.serializers import search_result_out
from app.search.queries import SearchFilters, SearchScope, search

router = APIRouter(prefix="/api/search", tags=["search"])


@router.get("", response_model=SearchResponseOut, summary="Search cohort content")
def run_search(
    db: DbDep,
    ctx: CohortDep,
    pagination: PaginationDep,
    q: Annotated[str, Query(min_length=1, max_length=200)],
    scope: SearchScope = SearchScope.ALL,
    channel_id: uuid.UUID | None = None,
    sender_id: uuid.UUID | None = None,
    date_from: dt.datetime | None = None,
    date_to: dt.datetime | None = None,
    include_direct_messages: bool = True,
) -> SearchResponseOut:
    response = search(
        db,
        cohort_id=ctx.cohort_id,
        user=ctx.user,
        filters=SearchFilters(
            query=q,
            scope=scope,
            channel_id=channel_id,
            sender_id=sender_id,
            date_from=date_from,
            date_to=date_to,
            include_direct_messages=include_direct_messages,
        ),
        limit=pagination.limit,
        offset=pagination.offset,
    )
    return SearchResponseOut(
        results=[search_result_out(item) for item in response.results],
        total=response.total,
        limit=response.limit,
        offset=response.offset,
        has_more=response.has_more,
    )
