"""Read-only access to the in-app log (see backend/logstore.py)."""
from fastapi import APIRouter, Query

from backend import logstore

router = APIRouter(prefix="/api/logs", tags=["logs"])


@router.get("")
def get_logs(
    level: str | None = Query(None, description="minimum level: INFO, WARNING or ERROR"),
    category: str | None = Query(None, description="comma separated: trade,engine,exchange,telegram,init,api,other"),
    q: str | None = Query(None, description="case-insensitive; every word must match"),
    group_id: int | None = Query(None),
    before_id: int | None = Query(None, description="pagination: only rows older than this id"),
    after_id: int | None = Query(None, description="polling: only rows newer than this id"),
    limit: int = Query(200, ge=1, le=1000),
):
    st = logstore.store
    if st is None:
        return {"items": [], "matched": 0, "last_id": 0, "total": 0, "has_more": False,
                "categories": list(logstore.CATEGORIES)}
    res = st.query(level=level, category=category, q=q, group_id=group_id,
                   before_id=before_id, after_id=after_id, limit=limit)
    res["categories"] = list(logstore.CATEGORIES)
    return res
