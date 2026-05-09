"""
Point d'entrée principal — Microservice Comptabilité.
"""
import asyncio
import logging
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1 import accounts, auth, journals, reports, users
from app.core.config import settings
from app.core.exceptions import AccountingBaseError
from app.db.session import AsyncSessionFactory, engine
from app.models.accounting import Base
from app.models.auth import User  # noqa: F401 — registers User table with Base.metadata

# ─── Logging structuré ────────────────────────────────────────────────────────

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.add_log_level,
        structlog.processors.JSONRenderer(),
    ]
)
logger = structlog.get_logger(__name__)


# ─── Lifecycle ────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("accounting_service.starting", version=settings.APP_VERSION)

    # Créer les tables (en dev — utiliser Alembic en prod)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Seed admin par défaut
    from app.services.auth import seed_admin
    async with AsyncSessionFactory() as session:
        async with session.begin():
            await seed_admin(session)

    # Démarrer le consommateur Kafka en arrière-plan
    from app.services.kafka_consumer import run_consumer
    from app.services.kafka_producer import stop_producer
    kafka_task = asyncio.create_task(run_consumer())

    yield

    # Arrêt propre
    kafka_task.cancel()
    try:
        await kafka_task
    except asyncio.CancelledError:
        pass
    await stop_producer()
    await engine.dispose()
    logger.info("accounting_service.stopped")


# ─── Application ──────────────────────────────────────────────────────────────

app = FastAPI(
    title="Microservice Comptabilité",
    description="""
## Core Banking — Microservice Comptabilité

Gère le plan de comptes, les journaux, les écritures comptables
et les rapports financiers selon les normes SYSCOHADA / BCEAO.

### Règles fondamentales
- **Partie double** : ΣDébit = ΣCrédit (invariant systématique)
- **Intangibilité** : les écritures validées sont immuables
- **Clôture de période** : aucune écriture possible en période clôturée
- **Idempotence** : un événement externe → une seule écriture
    """,
    version=settings.APP_VERSION,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# ─── CORS ─────────────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.DEBUG else [],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Gestionnaires d'erreurs ──────────────────────────────────────────────────

@app.exception_handler(AccountingBaseError)
async def accounting_error_handler(request: Request, exc: AccountingBaseError):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error_code": exc.error_code,
            "message": exc.message,
            "details": exc.details,
        },
    )


@app.exception_handler(Exception)
async def generic_error_handler(request: Request, exc: Exception):
    logger.exception("unhandled_error", error=str(exc))
    return JSONResponse(
        status_code=500,
        content={"error_code": "INTERNAL_ERROR", "message": "Erreur interne du serveur."},
    )


# ─── Routes ───────────────────────────────────────────────────────────────────

API_PREFIX = "/api/v1"

app.include_router(auth.router, prefix=API_PREFIX)
app.include_router(users.router, prefix=API_PREFIX)
app.include_router(accounts.router, prefix=API_PREFIX)
app.include_router(journals.router, prefix=API_PREFIX)
app.include_router(reports.router, prefix=API_PREFIX)


@app.get("/health", tags=["Santé"])
async def health_check():
    """Point de contrôle de santé pour Kubernetes/Docker."""
    return {
        "status": "healthy",
        "service": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "environment": settings.ENVIRONMENT,
    }


@app.get("/", tags=["Info"])
async def root():
    return {
        "service": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "docs": "/docs",
    }
