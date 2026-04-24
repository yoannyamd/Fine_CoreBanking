"""
Router — Écritures comptables (Journaux)
"""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    AccountNotActiveError, AccountNotFoundError,
    JournalEntryAlreadyPostedError, JournalEntryAlreadyReversedError,
    JournalEntryNotFoundError, PeriodClosedError, PeriodNotFoundError,
)
from app.db.session import get_session
from app.repositories.accounting import JournalEntryRepository
from app.schemas.accounting import (
    JournalEntryCreate, JournalEntryResponse,
    LetteringRequest, LetteringResponse, PaginatedResponse,
)
from app.services.accounting import JournalEntryService
from pydantic import BaseModel

router = APIRouter(prefix="/journal-entries", tags=["Écritures comptables"])


def get_entry_service(session: AsyncSession = Depends(get_session)) -> JournalEntryService:
    return JournalEntryService(session)


class PostEntryRequest(BaseModel):
    posted_by: str


class ReverseEntryRequest(BaseModel):
    reversed_by: str
    reversal_date: date | None = None


@router.post("/", response_model=JournalEntryResponse, status_code=status.HTTP_201_CREATED)
async def create_entry(
    data: JournalEntryCreate,
    created_by: str = Query(..., description="Identifiant de l'utilisateur"),
    svc: JournalEntryService = Depends(get_entry_service),
):
    """
    Crée une écriture comptable en brouillon (DRAFT).
    
    La règle de la partie double (ΣDébit = ΣCrédit) est vérifiée automatiquement.
    L'écriture doit ensuite être validée via POST /journal-entries/{id}/post.
    """
    try:
        entry = await svc.create_entry(data, created_by=created_by)
        return await svc.entry_repo.get_by_id(entry.id, with_lines=True)
    except (PeriodNotFoundError, AccountNotFoundError, AccountNotActiveError) as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)


@router.get("/{entry_id}", response_model=JournalEntryResponse)
async def get_entry(
    entry_id: str,
    svc: JournalEntryService = Depends(get_entry_service),
):
    try:
        return await svc.entry_repo.get_by_id(entry_id, with_lines=True)
    except JournalEntryNotFoundError as e:
        raise HTTPException(status_code=404, detail=e.message)


@router.get("/", response_model=PaginatedResponse[JournalEntryResponse])
async def list_entries(
    period_id: str = Query(...),
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=500),
    svc: JournalEntryService = Depends(get_entry_service),
):
    from app.models.accounting import EntryStatus
    status_enum = EntryStatus(status) if status else None
    items, total = await svc.entry_repo.list_by_period(
        period_id, status=status_enum, offset=(page - 1) * size, limit=size
    )
    import math
    return PaginatedResponse(
        items=items, total=total, page=page, size=size,
        pages=math.ceil(total / size) if total > 0 else 0
    )


@router.post("/{entry_id}/post", response_model=JournalEntryResponse)
async def post_entry(
    entry_id: str,
    body: PostEntryRequest,
    svc: JournalEntryService = Depends(get_entry_service),
):
    """
    Valide une écriture (DRAFT → POSTED).
    Une fois validée, l'écriture est immuable (règle d'intangibilité).
    """
    try:
        entry = await svc.post_entry(entry_id, posted_by=body.posted_by)
        return await svc.entry_repo.get_by_id(entry.id, with_lines=True)
    except JournalEntryNotFoundError as e:
        raise HTTPException(status_code=404, detail=e.message)
    except (JournalEntryAlreadyPostedError, JournalEntryAlreadyReversedError, PeriodClosedError) as e:
        raise HTTPException(status_code=422, detail=e.message)


@router.post("/{entry_id}/reverse", response_model=JournalEntryResponse)
async def reverse_entry(
    entry_id: str,
    body: ReverseEntryRequest,
    svc: JournalEntryService = Depends(get_entry_service),
):
    """
    Extourne une écriture validée (crée l'écriture miroir avec débit/crédit inversés).
    Seules les écritures POSTED peuvent être extournées.
    """
    try:
        reversal = await svc.reverse_entry(
            entry_id, reversed_by=body.reversed_by, reversal_date=body.reversal_date
        )
        return await svc.entry_repo.get_by_id(reversal.id, with_lines=True)
    except JournalEntryNotFoundError as e:
        raise HTTPException(status_code=404, detail=e.message)
    except (JournalEntryAlreadyPostedError, JournalEntryAlreadyReversedError) as e:
        raise HTTPException(status_code=422, detail=e.message)


@router.post("/letter", response_model=LetteringResponse)
async def letter_lines(
    data: LetteringRequest,
    lettered_by: str = Query(...),
    svc: JournalEntryService = Depends(get_entry_service),
):
    """
    Lettrage de lignes comptables.
    Le lettrage rapproche des mouvements débiteurs et créditeurs sur le même compte
    (ex: facture + règlement).
    """
    from app.core.exceptions import LetteringImbalancedError, LineAlreadyLetteredError
    try:
        return await svc.letter_lines(data.line_ids, lettered_by, data.lettering_code)
    except (LetteringImbalancedError, LineAlreadyLetteredError) as e:
        raise HTTPException(status_code=422, detail=e.message)
