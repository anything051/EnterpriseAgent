"""Local REST API for authorized document intake, questions, and source lookup."""

from hmac import compare_digest
import logging
from time import perf_counter
from typing import Annotated, Literal
from uuid import UUID, uuid4

import psycopg
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from enterprise_rag.adapters.models import create_models
from enterprise_rag.adapters.postgres import PostgresStore
from enterprise_rag.config import SLUG_RE, Settings
from enterprise_rag.core.citations import Answer
from enterprise_rag.core.documents import DocumentInputError, DocumentNotFound, Scope, SourceView
from enterprise_rag.core.embeddings import EmbeddingError, IndexProfileConflict
from enterprise_rag.core.generation import GenerationError
from enterprise_rag.core.service import RAGService

logger = logging.getLogger(__name__)
bearer = HTTPBearer(auto_error=False)
MODEL_ERRORS = {
    "embedding_unavailable": (502, "The embedding provider is unavailable."),
    "embedding_timeout": (504, "The embedding request timed out."),
    "embedding_revision_mismatch": (409, "The Ollama embedding model differs from the configured digest."),
    "invalid_embedding": (502, "The embedding response did not match the configured vector shape or values."),
    "chat_unavailable": (502, "The chat provider is unavailable."),
    "chat_timeout": (504, "The chat request timed out."),
    "invalid_generation": (502, "The chat response did not match the expected structure."),
}


class IngestResponse(BaseModel):
    document_id: UUID
    version_id: UUID
    index_id: UUID
    status: Literal["ready"] = "ready"
    chunk_count: int
    skipped_pages: list[int]


class QuestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    collection_id: str = Field(pattern=SLUG_RE)
    question: str = Field(min_length=1, max_length=2000)
    k: int = Field(default=5, ge=1, le=10, strict=True)


class QuestionResponse(Answer):
    request_id: str


def _error(request: Request, status: int, code: str, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, headers=headers,
                        content={"error": {"code": code, "message": message},
                                 "request_id": request.state.request_id})


def create_app(settings: Settings | None = None, service: RAGService | None = None) -> FastAPI:
    settings = settings if settings is not None else Settings()
    if service is None:
        embedding, chat = create_models(settings)
        service = RAGService(PostgresStore(settings.database_url.get_secret_value()),
                             embedding, chat, settings.index_profile())

    app = FastAPI(title="EnterpriseRAG", version="0.1.0")

    @app.middleware("http")
    async def correlate(request: Request, call_next):
        request_id = uuid4().hex
        request.state.request_id = request_id
        started = perf_counter()
        response = await call_next(request)
        if response.status_code == 413 and not response.headers.get("content-type", "").startswith("application/json"):
            code = "document_too_large" if request.url.path == "/documents" else "request_too_large"
            response = _error(request, 413, code, "The request exceeds the allowed size.")
        response.headers["X-Request-ID"] = request_id
        logger.info("request_id=%s method=%s route=%s status=%s elapsed_ms=%.1f",
                    request_id, request.method, getattr(request.scope.get("route"), "path", "<unmatched>"),
                    response.status_code, (perf_counter() - started) * 1000)
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, _error_detail: RequestValidationError):
        return _error(request, 422, "invalid_request", "Invalid or missing request fields.")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, error: StarletteHTTPException):
        codes = {401: "unauthorized", 404: "not_found", 413: "document_too_large"}
        return _error(request, error.status_code, codes.get(error.status_code, "invalid_request"),
                      str(error.detail), error.headers)

    @app.exception_handler(DocumentNotFound)
    async def missing_source(request: Request, _exc: DocumentNotFound):
        return _error(request, 404, "not_found", "The requested resource was not found.")

    @app.exception_handler(DocumentInputError)
    async def invalid_document(request: Request, error: DocumentInputError):
        status = {"unsupported_document": 415, "document_too_large": 413}.get(error.code, 422)
        return _error(request, status, error.code, str(error))

    @app.exception_handler(EmbeddingError)
    @app.exception_handler(GenerationError)
    async def model_error(request: Request, error: EmbeddingError | GenerationError):
        status, message = MODEL_ERRORS[error.code]
        return _error(request, status, error.code, message)

    @app.exception_handler(IndexProfileConflict)
    async def profile_conflict(request: Request, _exc: IndexProfileConflict):
        return _error(request, 409, "index_profile_conflict",
                      "The collection uses another index profile. Restore its configuration or use a new collection.")

    @app.exception_handler(psycopg.Error)
    async def database_error(request: Request, error: psycopg.Error):
        logger.warning("database_error request_id=%s type=%s sqlstate=%s",
                       request.state.request_id, type(error).__name__, error.sqlstate)
        return _error(request, 503, "database_unavailable", "The database request failed.")

    def authorized_scope(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> Scope:
        if credentials:
            supplied = credentials.credentials.encode("utf-8")
            for collection, token in settings.collection_tokens.items():
                if compare_digest(supplied, token.get_secret_value().encode("utf-8")):
                    return Scope(settings.app_id, collection)
        raise HTTPException(401, "A valid bearer token is required.", headers={"WWW-Authenticate": "Bearer"})

    def require_collection(scope: Scope, collection_id: str) -> None:
        if scope.collection_id != collection_id:
            raise DocumentNotFound()

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/documents", status_code=201, response_model=IngestResponse,
              responses={200: {"model": IngestResponse, "description": "Already indexed"}},
              max_body_size=settings.max_upload_bytes + 64 * 1024,
              form_limits={"max_files": 1, "max_fields": 2, "max_part_size": 4096})
    def ingest(response: Response, scope: Annotated[Scope, Depends(authorized_scope)],
               file: Annotated[UploadFile, File()], collection_id: Annotated[str, Form(pattern=SLUG_RE)],
               document_id: Annotated[UUID | None, Form()] = None) -> IngestResponse:
        require_collection(scope, collection_id)
        data = file.file.read(settings.max_upload_bytes + 1)
        if len(data) > settings.max_upload_bytes:
            raise DocumentInputError("document_too_large", "The upload exceeds the file size limit.")
        result = service.ingest(scope, file.filename or "", data, document_id)
        if not result.created:
            response.status_code = 200
        return IngestResponse(document_id=result.document_id, version_id=result.version_id,
                              index_id=result.index_id, chunk_count=result.chunk_count,
                              skipped_pages=list(result.skipped_pages))

    @app.post("/questions", max_body_size=16 * 1024)
    def question(request: Request, query: QuestionRequest,
                 scope: Annotated[Scope, Depends(authorized_scope)]) -> QuestionResponse:
        require_collection(scope, query.collection_id)
        result = service.ask(scope, query.question, query.k, request.state.request_id)
        return QuestionResponse(**result.model_dump(), request_id=request.state.request_id)

    @app.get("/documents/{document_id}/versions/{version_id}/units/{unit_id}")
    def original(document_id: UUID, version_id: UUID, unit_id: UUID,
                 scope: Annotated[Scope, Depends(authorized_scope)]) -> SourceView:
        return service.store.source(scope, document_id, version_id, unit_id)

    return app
