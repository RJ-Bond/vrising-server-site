from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_

from ..database import get_db
from ..models import User, Report, AutoFlagRule
from ..auth import get_current_user, get_moderator_user
from ..rate_limit import limiter
from ..helpers import log_audit
from ..schemas import (
    ReportCreate,
    ReportReview,
    ReportBulkReviewIn,
    ReportBulkResult,
    ReportBulkReviewOut,
    AutoFlagRuleCreate,
    AutoFlagRuleUpdate,
    AutoFlagRuleOut,
)

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
    q: str = Query(""),
    _: User = Depends(get_moderator_user),
    db: AsyncSession = Depends(get_db),
):
    filters = []
    if status.strip():
        filters.append(Report.status == status.strip())
    if q.strip():
        like = f"%{q.strip()}%"
        filters.append(or_(Report.reason.ilike(like), Report.admin_note.ilike(like)))
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


@router.post("/api/admin/reports/bulk-review", response_model=ReportBulkReviewOut)
async def bulk_review_reports(
    body: ReportBulkReviewIn,
    current_user: User = Depends(get_moderator_user),
    db: AsyncSession = Depends(get_db),
):
    """Bulk variant of PATCH /api/admin/reports/{id} — same moderator tier, one shared
    status/admin_note applied to every id in body.ids. Per-id success/failure result:
    a report already resolved by another moderator, or a stale/bogus id, shouldn't fail
    the rest of the batch — same defensive pattern as POST /api/admin/points/grant-bulk
    and POST /api/admin/comments/bulk-delete."""
    results: list[ReportBulkResult] = []
    reviewed_ids: list[int] = []
    for rid in body.ids:
        r = (await db.execute(select(Report).where(Report.id == rid))).scalar_one_or_none()
        if r is None:
            results.append(ReportBulkResult(id=rid, success=False, error="Жалоба не найдена"))
            continue
        r.status = body.status
        r.admin_note = body.admin_note
        r.reviewed_at = datetime.now(timezone.utc)
        reviewed_ids.append(rid)
        results.append(ReportBulkResult(id=rid, success=True))
    if reviewed_ids:
        await log_audit(db, current_user, "bulk_review_report", f"ids={reviewed_ids} status={body.status}")
    await db.commit()
    succeeded = sum(1 for r in results if r.success)
    return ReportBulkReviewOut(results=results, succeeded=succeeded, failed=len(results) - succeeded)


# ─── Auto-flag rules ───────────────────────────────────────────────────────────
# Keyword-based rules checked against every new Comment on creation (see
# _check_auto_flag_rules in routers/news.py, called from POST /api/news/{slug}/comments)
# — a match creates a Report here rather than blocking the comment, so it lands in the
# same moderation queue above for a human to review. CRUD is moderator-tier, matching
# the comments/reports queues themselves (see AutoFlagRule's model docstring).

@router.get("/api/admin/auto-flag-rules", response_model=list[AutoFlagRuleOut])
async def list_auto_flag_rules(
    _: User = Depends(get_moderator_user),
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(
        select(AutoFlagRule).order_by(AutoFlagRule.created_at.desc())
    )).scalars().all()
    return [AutoFlagRuleOut.model_validate(r) for r in rows]


@router.post("/api/admin/auto-flag-rules", response_model=AutoFlagRuleOut, status_code=201)
async def create_auto_flag_rule(
    body: AutoFlagRuleCreate,
    current_user: User = Depends(get_moderator_user),
    db: AsyncSession = Depends(get_db),
):
    rule = AutoFlagRule(keyword=body.keyword, created_by=current_user.username, is_active=True)
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    await log_audit(db, current_user, "auto_flag_rule.create", f"keyword={rule.keyword}")
    await db.commit()
    return AutoFlagRuleOut.model_validate(rule)


@router.patch("/api/admin/auto-flag-rules/{rule_id}", response_model=AutoFlagRuleOut)
async def update_auto_flag_rule(
    rule_id: int,
    body: AutoFlagRuleUpdate,
    current_user: User = Depends(get_moderator_user),
    db: AsyncSession = Depends(get_db),
):
    rule = (await db.execute(select(AutoFlagRule).where(AutoFlagRule.id == rule_id))).scalar_one_or_none()
    if rule is None:
        raise HTTPException(404, "Rule not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(rule, field, value)
    await log_audit(db, current_user, "auto_flag_rule.update", f"id={rule_id}")
    await db.commit()
    await db.refresh(rule)
    return AutoFlagRuleOut.model_validate(rule)


@router.delete("/api/admin/auto-flag-rules/{rule_id}", status_code=204)
async def delete_auto_flag_rule(
    rule_id: int,
    current_user: User = Depends(get_moderator_user),
    db: AsyncSession = Depends(get_db),
):
    rule = (await db.execute(select(AutoFlagRule).where(AutoFlagRule.id == rule_id))).scalar_one_or_none()
    if rule is None:
        raise HTTPException(404, "Rule not found")
    await log_audit(db, current_user, "auto_flag_rule.delete", f"keyword={rule.keyword}")
    await db.delete(rule)
    await db.commit()
