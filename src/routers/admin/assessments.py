"""Admin opportunity-assessment pages."""

import uuid

from fastapi import Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import OpportunityAssessment, User
from src.routers.admin._common import _ADMIN, _DB, router
from src.routers.workspace.compatibility import redirect, require_resource


@router.get("/assessments", response_class=HTMLResponse, name="legacy_admin_assessments")
async def admin_assessments(request: Request, current_user: User = _ADMIN):
    return redirect(request, current_user, "assessments", "workspace_assessments")



@router.get("/assessments/{assessment_id}", response_class=HTMLResponse, name="legacy_admin_assessment_detail")
async def admin_assessment_detail(assessment_id: uuid.UUID, request: Request, db: AsyncSession = _DB, current_user: User = _ADMIN):
    await require_resource(db, OpportunityAssessment, assessment_id, "Assessment not found")
    return redirect(request, current_user, "assessment_detail", "workspace_assessment_detail", assessment_id=assessment_id)
