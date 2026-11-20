"""Independent EnterpriseAgent API and server-resolved operator roles."""

from dataclasses import dataclass
from hashlib import sha256
from hmac import compare_digest
import logging
import re
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from enterprise_rag.api.main import IngestResponse, _error, bearer, create_app
from enterprise_rag.config import SLUG_RE, TOKEN_RE, Settings
from enterprise_rag.core.documents import Scope, SourceView
from enterprise_rag.core.service import RAGService

from .agents.planner import Planner, create_planner
from .agents.sessions import SessionService
from .agents.state_store import AgentThread, SessionConflict, SessionRun, SessionStore
from .agents.workflow import AgentWorkflow
from .contracts import AgentReport, Role, TaskInput
from .tools import BusinessTools, InventoryClient, POLICY_FILENAME, POLICY_PATH, SalesRepository

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Operator:
    role: Role
    owner_id: str


class SessionInfo(BaseModel):
    role: Role
    threads_enabled: bool


class EnterpriseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ENTERPRISE_", env_file=".env", extra="ignore",
                                      hide_input_in_errors=True)

    app_id: str = Field(default="enterprise-agent-v05", pattern=SLUG_RE)
    analyst_token: SecretStr
    viewer_token: SecretStr
    reader_url: SecretStr
    rag_database_url: SecretStr | None = None
    inventory_url: str = "http://127.0.0.1:8003"

    @model_validator(mode="after")
    def validate_role_tokens(self):
        values = (self.analyst_token.get_secret_value(), self.viewer_token.get_secret_value())
        if len(set(values)) != 2 or any(
            re.fullmatch(TOKEN_RE, value) is None or value.startswith("REPLACE_WITH_") for value in values
        ):
            raise ValueError("Set distinct random EnterpriseAgent bearer tokens with at least 24 characters")
        return self


def policy_scope(app_id: str) -> Scope:
    return Scope(app_id, "warehouse-policy")


def create_enterprise_app(
    settings: Settings | None = None,
    service: RAGService | None = None,
    *,
    tools: BusinessTools | None = None,
    planner: Planner | None = None,
    analyst_token: str | None = None,
    viewer_token: str | None = None,
    include_policy: bool = True,
    session_store: SessionStore | None = None,
    enable_sessions: bool = True,
    session_retry: bool = True,
    inventory_client: InventoryClient | None = None,
) -> FastAPI:
    config = None
    if settings is None or tools is None or analyst_token is None or viewer_token is None:
        overrides = {}
        if analyst_token is not None:
            overrides["analyst_token"] = analyst_token
        if viewer_token is not None:
            overrides["viewer_token"] = viewer_token
        config = EnterpriseSettings(**overrides)
        analyst_token = config.analyst_token.get_secret_value()
        viewer_token = config.viewer_token.get_secret_value()
        if settings is None:
            database = {"database_url": config.rag_database_url} if config.rag_database_url else {}
            settings = Settings(app_id=config.app_id, collection_tokens={"operator": analyst_token}, **database)

    if compare_digest(analyst_token.encode(), viewer_token.encode()):
        raise ValueError("EnterpriseAgent role tokens must differ")
    settings = settings.model_copy(update={"embedding_timeout": min(settings.embedding_timeout, 8)})
    roles = [("analyst", analyst_token.encode()), ("viewer", viewer_token.encode())]

    def operator(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> Operator:
        if credentials:
            raw = credentials.credentials.encode()
            role = next((name for name, token in roles if compare_digest(raw, token)), None)
            if role is not None:
                owner = sha256(b"enterprise-thread-v1\0" + role.encode() + b"\0" + raw).hexdigest()
                return Operator(role, owner)
        raise HTTPException(401, "EnterpriseAgent credentials are required.", headers={"WWW-Authenticate": "Bearer"})

    def authenticate(identity: Annotated[Operator, Depends(operator)]) -> Role:
        return identity.role

    def register(app: FastAPI, rag: RAGService) -> None:
        scope = policy_scope(settings.app_id)
        business = tools
        if business is None:
            business = BusinessTools(SalesRepository(config.reader_url.get_secret_value()),
                                     inventory_client or InventoryClient.from_url(config.inventory_url), rag, scope)
        plan = planner or create_planner(settings)
        workflow = AgentWorkflow(plan, business, include_policy=include_policy)
        router = APIRouter(prefix="/agent")
        store = session_store if include_policy and enable_sessions else None
        if store is None and include_policy and enable_sessions and service is None:
            store = SessionStore(settings.database_url.get_secret_value(), settings.app_id)

        @router.get("/session")
        def session(role: Annotated[Role, Depends(authenticate)]) -> SessionInfo:
            return SessionInfo(role=role, threads_enabled=store is not None)

        @router.post("/tasks", max_body_size=16 * 1024)
        def task(request: Request, data: TaskInput, role: Annotated[Role, Depends(authenticate)]) -> AgentReport:
            result = workflow.run(data, role)
            LOGGER.info("agent_task request_id=%s task_id=%s role=%s status=%s model_calls=%d tool_calls=%d reason=%s",
                        request.state.request_id, result.task_id, role, result.status, result.model_calls,
                        len(result.audit), result.stop_reason)
            return result

        @router.post("/demo/policy", status_code=201, max_body_size=1024)
        def seed(response: Response, role: Annotated[Role, Depends(authenticate)]) -> IngestResponse:
            if role != "analyst":
                raise HTTPException(403, "This role cannot seed the demo policy.")
            result = rag.ingest(scope, POLICY_FILENAME, POLICY_PATH.read_bytes())
            if not result.created:
                response.status_code = 200
            return IngestResponse(document_id=result.document_id, version_id=result.version_id,
                                  index_id=result.index_id, chunk_count=result.chunk_count,
                                  skipped_pages=list(result.skipped_pages))

        @router.get("/sources/{document_id}/{version_id}/{unit_id}")
        def source(document_id: UUID, version_id: UUID, unit_id: UUID,
                   _role: Annotated[Role, Depends(authenticate)]) -> SourceView:
            return business.policy_source(document_id, version_id, unit_id)

        if store is not None:
            sessions = SessionService(store, plan, business, retry=session_retry)

            @app.exception_handler(SessionConflict)
            async def thread_conflict(request: Request, error: SessionConflict):
                return _error(request, 409, "thread_conflict", str(error))

            @router.post("/threads", status_code=201, max_body_size=1024)
            def create_thread(identity: Annotated[Operator, Depends(operator)]) -> AgentThread:
                return store.create_thread(identity.owner_id, identity.role)

            @router.get("/threads")
            def list_threads(identity: Annotated[Operator, Depends(operator)]) -> list[AgentThread]:
                return store.list_threads(identity.owner_id)

            @router.get("/threads/{thread_id}/runs")
            def runs(thread_id: UUID, identity: Annotated[Operator, Depends(operator)]) -> list[SessionRun]:
                return store.history(identity.owner_id, thread_id)

            @router.put("/threads/{thread_id}/runs/{run_id}", max_body_size=16 * 1024)
            def start(thread_id: UUID, run_id: UUID, task: TaskInput,
                      identity: Annotated[Operator, Depends(operator)]) -> SessionRun:
                return sessions.start(identity.owner_id, identity.role, thread_id, run_id, task)

            @router.post("/threads/{thread_id}/runs/{run_id}/resume", max_body_size=1024)
            def resume(thread_id: UUID, run_id: UUID,
                       identity: Annotated[Operator, Depends(operator)]) -> SessionRun:
                return sessions.resume(identity.owner_id, identity.role, thread_id, run_id)

        app.include_router(router)

    state_ready = enable_sessions and include_policy and (session_store is not None or service is None)
    relations = (("enterprise_agent", "runs"), ("enterprise_agent", "checkpoints")) if state_ready else ()
    return create_app(settings, service, expose_core=False, register=register, title="EnterpriseAgent",
                      ready_relations=relations)
