"""CourseRAG catalog, scoped questions, original excerpts and evaluation exports."""

from hmac import compare_digest
import json
from pathlib import Path
import re
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from enterprise_rag.adapters.pdf_highlight import Highlight, locate_span
from enterprise_rag.api.main import IngestResponse, QuestionResponse, bearer, create_app
from enterprise_rag.config import SLUG_RE, TOKEN_RE, Settings
from enterprise_rag.core.documents import DocumentInputError, DocumentNotFound, SourceView
from enterprise_rag.core.service import RAGService

from .catalog import ChapterInput, CourseCatalog, CourseInput, MaterialInput, PointInput, logical_name
from .evaluations import list_runs, run_files, zip_run
from .preview import render_page


class CourseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="COURSE_RAG_", env_file=".env", extra="ignore",
                                      hide_input_in_errors=True)

    token: SecretStr
    app_id: str = Field(default="course-rag-v04", pattern=SLUG_RE)
    database_url: SecretStr | None = None
    evaluation_root: Path = Path("docs/evaluation_runs")

    @field_validator("token")
    @classmethod
    def check_token(cls, token):
        value = token.get_secret_value()
        if not re.fullmatch(TOKEN_RE, value) or value.startswith("REPLACE_WITH_"):
            raise ValueError("Set COURSE_RAG_TOKEN to a random bearer token with at least 24 characters.")
        return token


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str = Field(min_length=1, max_length=2000)
    k: int = Field(default=5, ge=1, le=10, strict=True)


def create_course_app(settings: Settings | None = None, service: RAGService | None = None, *,
                      catalog: CourseCatalog | None = None, token: str | None = None,
                      evaluation_root: Path | None = None) -> FastAPI:
    if settings is None or token is None:
        course = CourseSettings(token=token) if token is not None else CourseSettings()
        token = course.token.get_secret_value()
        if settings is None:
            overrides = {"database_url": course.database_url} if course.database_url else {}
            settings = Settings(app_id=course.app_id, collection_tokens={"operator": token}, **overrides)
        if evaluation_root is None:
            evaluation_root = course.evaluation_root
    if catalog is None:
        catalog = CourseCatalog(settings.database_url.get_secret_value(), settings.app_id)
    root = evaluation_root if evaluation_root is not None else Path("docs/evaluation_runs")

    def require_operator(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> None:
        if not credentials or not compare_digest(credentials.credentials.encode(), token.encode()):
            raise HTTPException(401, "Course operator authentication is required.",
                                headers={"WWW-Authenticate": "Bearer"})

    def register(app: FastAPI, rag: RAGService) -> None:
        router = APIRouter(dependencies=[Depends(require_operator)])

        @router.get("/courses")
        def courses():
            return catalog.list_courses()

        @router.post("/courses", status_code=201, max_body_size=16 * 1024)
        def create_course(data: CourseInput):
            return catalog.create_course(data)

        @router.get("/courses/{course_id}")
        def detail(course_id: UUID):
            return catalog.snapshot(course_id)

        @router.post("/courses/{course_id}/chapters", status_code=201, max_body_size=16 * 1024)
        def chapter(course_id: UUID, data: ChapterInput):
            return catalog.create_chapter(course_id, data)

        @router.post("/courses/{course_id}/points", status_code=201, max_body_size=16 * 1024)
        def point(course_id: UUID, data: PointInput):
            return catalog.create_point(course_id, data)

        @router.post("/courses/{course_id}/materials", status_code=201, max_body_size=16 * 1024)
        def material(course_id: UUID, data: MaterialInput):
            return catalog.create_material(course_id, data)

        @router.get("/courses/{course_id}/materials/{material_id}/revisions")
        def revisions(course_id: UUID, material_id: UUID):
            return catalog.revisions(course_id, material_id)

        @router.post("/courses/{course_id}/materials/{material_id}/revisions", status_code=201,
                     max_body_size=settings.max_upload_bytes + 64 * 1024,
                     form_limits={"max_files": 1, "max_fields": 0, "max_part_size": 4096})
        def publish(course_id: UUID, material_id: UUID, response: Response,
                    file: Annotated[UploadFile, File()]) -> IngestResponse:
            entry = catalog.material(course_id, material_id)
            suffix = Path(file.filename or "").suffix.lower()
            allowed = {".pdf"} if entry["format"] == "pdf" else {".md", ".markdown"}
            if suffix not in allowed:
                raise DocumentInputError("unsupported_document", "The file extension must match the material type.")
            raw = file.file.read(settings.max_upload_bytes + 1)
            if len(raw) > settings.max_upload_bytes:
                raise DocumentInputError("document_too_large", "The upload exceeds the file size limit.")
            result = rag.ingest(catalog.scope(course_id), logical_name(material_id, entry["format"]), raw)
            if not result.created:
                response.status_code = 200
            return IngestResponse(document_id=result.document_id, version_id=result.version_id,
                                  index_id=result.index_id, chunk_count=result.chunk_count,
                                  skipped_pages=list(result.skipped_pages))

        @router.post("/courses/{course_id}/questions", max_body_size=16 * 1024)
        def ask(request: Request, course_id: UUID, data: Question) -> QuestionResponse:
            answer = rag.ask(catalog.scope(course_id), data.question, data.k, request.state.request_id)
            return QuestionResponse(**answer.model_dump(), request_id=request.state.request_id)

        @router.get("/courses/{course_id}/sources/{document_id}/{version_id}/{unit_id}")
        def source(course_id: UUID, document_id: UUID, version_id: UUID, unit_id: UUID) -> SourceView:
            return rag.store.source(catalog.scope(course_id), document_id, version_id, unit_id)

        @router.get("/courses/{course_id}/sources/{document_id}/{version_id}/{unit_id}/highlight")
        def highlight(course_id: UUID, document_id: UUID, version_id: UUID, unit_id: UUID,
                      start: Annotated[int, Query(ge=0)], end: Annotated[int, Query(gt=0)]) -> Highlight:
            page, text, raw = rag.store.highlight_source(catalog.scope(course_id), document_id, version_id, unit_id)
            return locate_span(raw, page, text, start, end)

        @router.get("/courses/{course_id}/sources/{document_id}/{version_id}/{unit_id}/preview")
        def preview(course_id: UUID, document_id: UUID, version_id: UUID, unit_id: UUID,
                    start: Annotated[int | None, Query(ge=0)] = None,
                    end: Annotated[int | None, Query(gt=0)] = None):
            if (start is None) != (end is None):
                raise DocumentInputError("invalid_span", "Provide both highlight offsets or neither.")
            page, text, raw = rag.store.highlight_source(catalog.scope(course_id), document_id, version_id, unit_id)
            if page is None or raw is None:
                raise DocumentNotFound()
            span = locate_span(raw, page, text, start, end) if start is not None and end is not None else None
            png, mode = render_page(raw, page, span)
            return Response(png, media_type="image/png",
                            headers={"Cache-Control": "private, no-store", "X-Preview-Mode": mode})

        @router.get("/evaluations")
        def evaluations():
            return list_runs(root)

        @router.get("/evaluations/{run_id}")
        def evaluation(run_id: str):
            return json.loads(run_files(root, run_id)["metrics.json"])

        @router.get("/evaluations/{run_id}/export")
        def export(run_id: str):
            return Response(zip_run(root, run_id), media_type="application/zip",
                            headers={"Content-Disposition": f'attachment; filename="{run_id}.zip"',
                                     "Cache-Control": "private, no-store"})

        app.include_router(router)

    return create_app(settings, service, expose_core=False, register=register, title="CourseRAG",
                      ready_relations=(("course_rag", "courses"), ("course_rag", "materials")))
