"""Owner-scoped thread metadata and a durable read-only tool execution journal."""

from contextlib import contextmanager
from datetime import datetime
from hashlib import sha256
from uuid import UUID, uuid4

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from enterprise_rag.core.documents import DocumentNotFound

from ..contracts import AgentReport, Role, TaskInput, ToolAudit

STATE_VERSION = 1
SETUP_LOCK = 605721820121


class SessionConflict(Exception):
    pass


class AgentThread(BaseModel):
    thread_id: UUID
    created_at: datetime


class SessionRun(BaseModel):
    run_id: UUID
    task: TaskInput
    started_at: datetime
    report: AgentReport | None


class SessionStore:
    def __init__(self, dsn: str, app_id: str):
        self.dsn = dsn
        self.app_id = app_id
        with self.connect() as conn:
            conn.execute("SELECT pg_advisory_lock(%s)", (SETUP_LOCK,))
            # The connection is closed after setup, so the lock also releases on errors.
            self.checkpointer(conn).setup()

    def connect(self):
        return psycopg.connect(self.dsn, connect_timeout=5, autocommit=True, prepare_threshold=0,
                               row_factory=dict_row, options=(
                                   "-c search_path=enterprise_agent,pg_catalog "
                                   "-c statement_timeout=5000 -c lock_timeout=2000"
                               ))

    @staticmethod
    def checkpointer(conn):
        return PostgresSaver(conn, serde=JsonPlusSerializer(allowed_msgpack_modules=[]))

    def create_thread(self, owner: str, role: Role) -> AgentThread:
        with self.connect() as conn:
            return AgentThread.model_validate(conn.execute(
                "INSERT INTO threads (thread_id,app_id,owner_id,role) VALUES (%s,%s,%s,%s) "
                "RETURNING thread_id,created_at", (uuid4(), self.app_id, owner, role),
            ).fetchone())

    def list_threads(self, owner: str) -> list[AgentThread]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT thread_id,created_at FROM threads WHERE app_id=%s AND owner_id=%s "
                "ORDER BY created_at DESC,thread_id DESC LIMIT 50", (self.app_id, owner),
            ).fetchall()
        return [AgentThread.model_validate(row) for row in rows]

    def _role(self, conn, owner: str, thread: UUID) -> Role:
        row = conn.execute("SELECT role FROM threads WHERE app_id=%s AND owner_id=%s AND thread_id=%s",
                           (self.app_id, owner, thread)).fetchone()
        if row is None:
            raise DocumentNotFound()
        return row["role"]

    @contextmanager
    def lock_run(self, owner: str, role: Role, thread: UUID):
        with self.connect() as conn:
            if self._role(conn, owner, thread) != role:
                raise DocumentNotFound()
            key = int.from_bytes(sha256(thread.bytes).digest()[:8], "big", signed=True)
            if not conn.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (key,)).fetchone()["acquired"]:
                raise SessionConflict("Another request is running on this thread.")
            yield conn

    def history(self, owner: str, thread: UUID) -> list[SessionRun]:
        with self.connect() as conn:
            self._role(conn, owner, thread)
            rows = conn.execute(
                "SELECT run_id,task,started_at,report FROM runs WHERE thread_id=%s "
                "ORDER BY started_at DESC,run_id DESC LIMIT 25", (thread,),
            ).fetchall()
        return [SessionRun.model_validate(row) for row in rows]

    @staticmethod
    def load_run(conn, thread: UUID, run: UUID):
        return conn.execute(
            "SELECT run_id,task,started_at,report,state_version,retry_enabled "
            "FROM runs WHERE thread_id=%s AND run_id=%s",
            (thread, run),
        ).fetchone()

    def prepare(self, conn, thread: UUID, run: UUID, task: TaskInput | None, retry: bool = True):
        row = self.load_run(conn, thread, run)
        if row is not None:
            if task is not None and row["task"] != task.model_dump(mode="json"):
                raise SessionConflict("This run ID already belongs to a different task.")
            return row
        if task is None:
            raise DocumentNotFound()
        if conn.execute("SELECT 1 FROM runs WHERE thread_id=%s AND report IS NULL", (thread,)).fetchone() is not None:
            raise SessionConflict("Resume the active run before starting another task in this thread.")
        return conn.execute(
            "INSERT INTO runs (thread_id,run_id,state_version,task,retry_enabled) VALUES (%s,%s,%s,%s,%s) "
            "RETURNING run_id,task,started_at,report,state_version,retry_enabled",
            (thread, run, STATE_VERSION, Jsonb(task.model_dump(mode="json")), retry),
        ).fetchone()

    @staticmethod
    def memory(conn, thread: UUID) -> list[dict]:
        rows = conn.execute(
            "SELECT task,report->>'status' AS status FROM runs WHERE thread_id=%s AND report IS NOT NULL "
            "ORDER BY started_at DESC,run_id DESC LIMIT 3", (thread,),
        ).fetchall()
        return [{"task": {**row["task"], "question": row["task"]["question"][:160]}, "status": row["status"]}
                for row in reversed(rows)]

    @staticmethod
    def save_report(conn, thread: UUID, run: UUID, report: AgentReport) -> None:
        conn.execute("UPDATE runs SET report=%s WHERE thread_id=%s AND run_id=%s AND report IS NULL",
                     (Jsonb(report.model_dump(mode="json")), thread, run))

    @staticmethod
    def model_count(conn, thread: UUID, run: UUID) -> int:
        return conn.execute("SELECT model_calls FROM runs WHERE thread_id=%s AND run_id=%s",
                            (thread, run)).fetchone()["model_calls"]

    @staticmethod
    def reserve_model(conn, thread: UUID, run: UUID, limit: int) -> bool:
        return conn.execute(
            "UPDATE runs SET model_calls=model_calls+1 "
            "WHERE thread_id=%s AND run_id=%s AND model_calls<%s RETURNING model_calls",
            (thread, run, limit),
        ).fetchone() is not None

    @staticmethod
    def attempts(conn, thread: UUID, run: UUID, key: str) -> list[dict]:
        return conn.execute(
            "SELECT attempt,call_id,tool,audit,evidence FROM attempts "
            "WHERE thread_id=%s AND run_id=%s AND execution_key=%s ORDER BY attempt",
            (thread, run, key),
        ).fetchall()

    @staticmethod
    def reserve_attempt(conn, thread: UUID, run: UUID, key: str, attempt: int, call_id: str, tool: str) -> None:
        conn.execute(
            "INSERT INTO attempts (thread_id,run_id,execution_key,attempt,call_id,tool) VALUES (%s,%s,%s,%s,%s,%s)",
            (thread, run, key, attempt, call_id, tool),
        )

    @staticmethod
    def finish_attempt(conn, thread: UUID, run: UUID, key: str, entry: ToolAudit, evidence: dict | None) -> None:
        conn.execute(
            "UPDATE attempts SET audit=%s,evidence=%s "
            "WHERE thread_id=%s AND run_id=%s AND execution_key=%s AND attempt=%s AND audit IS NULL",
            (Jsonb(entry.model_dump(mode="json")), Jsonb(evidence) if evidence is not None else None,
             thread, run, key, entry.attempt),
        )
