from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from ..database import get_db
from ..models import User, Report
from ..auth import get_current_user, get_moderator_user
from ..rate_limit import limiter
from ..helpers import log_audit
from ..schemas import ReportCreate, ReportReview

router = APIRouter()


# ─── Reports ─────────────────────────────────────────────────────────────────

@router.post("/api/reports", status_code=201)
@limiter.limit("5/minute")
async def create_report(
    request: Request,
    body: ReportCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    report = Report(
        reporter_id=current_user.id,
        target_type=body.target_type,
        target_id=body.target_id,
        reason=body.reason,
    )
    db.add(report)
    await db.commit()
    return {"ok": True, "id": report.id}


@router.get("/api/admin/reports")
async def list_reports(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    status: str = Query(""),
    _: User = Depends(get_moderator_user),
    db: AsyncSession = Depends(get_db),
):
    filters = []
    if status.strip():
        filters.append(Report.status == status.strip())
    total = (await db.execute(select(func.count(Report.id)).where(*filters))).scalar_one()
    rows = (await db.execute(
        select(Report).where(*filters).order_by(Report.created_at.desc())
        .offset((page - 1) * per_page).limit(per_page)
    )).scalars().all()
    return {
        "total": total,
        "items": [
            {
                "id": r.id, "reporter_id": r.reporter_id,
                "target_type": r.target_type, "target_id": r.target_id,
                "reason": r.reason, "status": r.status,
                "admin_note": r.admin_note,
                "created_at": r.created_at.isoformat(),
                "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
            }
            for r in rows
        ],
    }


@router.patch("/api/admin/reports/{report_id}")
async def review_report(
    report_id: int,
    body: ReportReview,
    current_user: User = Depends(get_moderator_user),
    db: AsyncSession = Depends(get_db),
):
    r = (await db.execute(select(Report).where(Report.id == report_id))).scalar_one_or_none()
    if not r:
        raise HTTPException(404, "Report not found")
    r.status = body.status
    r.admin_note = body.admin_note
    r.reviewed_at = datetime.now(timezone.utc)
    await log_audit(db, current_user, "review_report", f"id={report_id} status={body.status}")
    await db.commit()
    return {"ok": True}
