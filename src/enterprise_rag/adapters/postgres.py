"""PostgreSQL persistence for document reservations, atomic publication, and exact search."""

from contextlib import contextmanager
from dataclasses import asdict
from uuid import UUID, uuid4, uuid5

import psycopg
from pgvector import Vector
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from enterprise_rag.core.citations import Candidate, DocumentLocator
from enterprise_rag.core.documents import (
    Build, Chunk, DocumentNotFound, ParsedDocument, Scope, SourceView, text_hash,
)
from enterprise_rag.core.embeddings import IndexProfile, IndexProfileConflict


class PostgresStore:
    def __init__(self, dsn: str):
        self._dsn = dsn

    @contextmanager
    def _connect(self, *, vectors: bool = False):
        with psycopg.connect(self._dsn, row_factory=dict_row, connect_timeout=5) as conn:
            if vectors:
                register_vector(conn)
            yield conn

    @staticmethod
    def _check_profile(row, profile_id: str) -> None:
        if row["index_profile_id"] != profile_id:
            raise IndexProfileConflict("The collection has a different embedding or parser profile.")

    def reserve(self, scope: Scope, parsed: ParsedDocument, data: bytes,
                profile: IndexProfile, document_id: UUID | None) -> Build:
        ids = (scope.app_id, scope.collection_id)
        profile_id = profile.id
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO rag.collections (app_id, collection_id, index_profile_id, profile)
                   VALUES (%s, %s, %s, %s) ON CONFLICT (app_id, collection_id) DO NOTHING""",
                (*ids, profile_id, Jsonb(asdict(profile))),
            )
            cur.execute("SELECT index_profile_id FROM rag.collections WHERE app_id = %s AND collection_id = %s", ids)
            self._check_profile(cur.fetchone(), profile_id)

            if document_id is None:
                cur.execute(
                    """INSERT INTO rag.documents (document_id, app_id, collection_id, logical_name)
                       VALUES (%s, %s, %s, %s) ON CONFLICT (app_id, collection_id, logical_name) DO NOTHING""",
                    (uuid4(), *ids, parsed.filename),
                )
                cur.execute(
                    """SELECT document_id FROM rag.documents
                       WHERE app_id = %s AND collection_id = %s AND logical_name = %s FOR UPDATE""",
                    (*ids, parsed.filename),
                )
            else:
                cur.execute(
                    """SELECT document_id FROM rag.documents
                       WHERE app_id = %s AND collection_id = %s AND document_id = %s FOR UPDATE""",
                    (*ids, document_id),
                )
            document = cur.fetchone()
            if document is None:
                raise DocumentNotFound()
            document_id = document["document_id"]

            cur.execute(
                """SELECT version_id, revision FROM rag.document_versions
                   WHERE document_id = %s AND file_sha256 = %s""",
                (document_id, parsed.sha256),
            )
            version = cur.fetchone()
            if version is None:
                cur.execute("INSERT INTO rag.blobs VALUES (%s, %s) ON CONFLICT (sha256) DO NOTHING",
                            (parsed.sha256, data))
                cur.execute(
                    """INSERT INTO rag.document_versions
                           (version_id, document_id, revision, filename, media_type, file_sha256)
                       VALUES (%s, %s, (SELECT COALESCE(MAX(revision), 0)+1 FROM rag.document_versions
                                       WHERE document_id = %s), %s, %s, %s)
                       RETURNING version_id, revision""",
                    (uuid4(), document_id, document_id, parsed.filename, parsed.media_type, parsed.sha256),
                )
                version = cur.fetchone()

            cur.execute(
                """INSERT INTO rag.index_builds (index_id, version_id, index_profile_id, dimension, status)
                   VALUES (%s, %s, %s, %s, 'pending') ON CONFLICT (version_id, index_profile_id) DO NOTHING""",
                (uuid4(), version["version_id"], profile_id, profile.dimension),
            )
            cur.execute(
                """SELECT index_id, chunk_count FROM rag.index_builds
                   WHERE version_id = %s AND index_profile_id = %s""",
                (version["version_id"], profile_id),
            )
            index = cur.fetchone()
            return Build(document_id, version["version_id"], index["index_id"],
                         version["revision"], index["chunk_count"])

    def record_failure(self, index_id: UUID, code: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE rag.index_builds SET status = 'failed', failure_code = %s WHERE index_id = %s AND status <> 'ready'",
                (code, index_id),
            )

    def publish(self, build: Build, parsed: ParsedDocument, profile: IndexProfile,
                chunks: list[Chunk], vectors: list[list[float]]) -> bool:
        unit_ids = {unit.key: uuid5(build.version_id, f"{profile.parser_profile_id}:{unit.key}")
                    for unit in parsed.units}
        with self._connect(vectors=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT document_id FROM rag.documents WHERE document_id = %s FOR UPDATE",
                        (build.document_id,))
            cur.execute("SELECT status FROM rag.index_builds WHERE index_id = %s FOR UPDATE", (build.index_id,))
            if cur.fetchone()["status"] == "ready":
                return False

            cur.executemany(
                """INSERT INTO rag.source_units
                       (unit_id, version_id, parser_profile_id, unit_key, source_text, text_sha256,
                        paragraphs, page, page_label, heading_path, start_line, end_line)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (unit_id) DO NOTHING""",
                ((unit_ids[unit.key], build.version_id, profile.parser_profile_id, unit.key, unit.text,
                  text_hash(unit.text), Jsonb([asdict(item) for item in unit.paragraphs]), unit.page,
                  unit.page_label, list(unit.heading_path), unit.start_line, unit.end_line)
                 for unit in parsed.units),
            )
            cur.executemany(
                """INSERT INTO rag.chunks
                       (chunk_id, index_id, version_id, unit_id, paragraph_id,
                        start_offset, end_offset, content, dimension, embedding)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                ((uuid5(build.index_id, f"{chunk.unit_key}:{chunk.start}:{chunk.end}"), build.index_id,
                  build.version_id, unit_ids[chunk.unit_key], chunk.paragraph_id, chunk.start, chunk.end,
                  chunk.text, profile.dimension, Vector(vector))
                 for chunk, vector in zip(chunks, vectors, strict=True)),
            )
            cur.execute(
                """UPDATE rag.index_builds SET status = 'ready', failure_code = NULL, ready_at = now(), chunk_count = %s
                   WHERE index_id = %s""", (len(chunks), build.index_id),
            )
            cur.execute(
                """UPDATE rag.documents SET active_revision = %s
                   WHERE document_id = %s AND (active_revision IS NULL OR active_revision < %s)""",
                (build.revision, build.document_id, build.revision),
            )
            return True

    def has_ready_documents(self, scope: Scope, profile_id: str) -> bool:
        ids = (scope.app_id, scope.collection_id)
        with self._connect() as conn:
            row = conn.execute(
                """SELECT c.index_profile_id,
                          EXISTS(SELECT 1 FROM rag.documents d
                                 WHERE d.app_id = c.app_id AND d.collection_id = c.collection_id
                                   AND d.active_revision IS NOT NULL) AS ready
                   FROM rag.collections c WHERE c.app_id = %s AND c.collection_id = %s""", ids,
            ).fetchone()
            if row is None:
                return False
            self._check_profile(row, profile_id)
            return row["ready"]

    @contextmanager
    def search_snapshot(self, scope: Scope, profile_id: str):
        with self._connect() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            register_vector(conn)
            yield PostgresSearchSnapshot(conn, scope, profile_id)

    def top_chunks(self, scope: Scope, profile_id: str, vector: list[float], k: int) -> list[Candidate]:
        with self.search_snapshot(scope, profile_id) as snapshot:
            return snapshot.candidates(snapshot.vector_ids(vector, k))

    def highlight_source(self, scope: Scope, document_id: UUID, version_id: UUID, unit_id: UUID):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM rag.highlight_source(%s, %s, %s, %s, %s)",
                               (scope.app_id, scope.collection_id, document_id, version_id, unit_id)).fetchone()
        if row is None:
            raise DocumentNotFound()
        return row["page"], row["source_text"], row["original"]

    def source(self, scope: Scope, document_id: UUID, version_id: UUID, unit_id: UUID) -> SourceView:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT d.document_id, u.version_id, u.unit_id, u.parser_profile_id, u.source_text,
                          u.text_sha256, u.page, u.page_label, u.heading_path, u.start_line, u.end_line, u.paragraphs
                   FROM rag.documents d
                   JOIN rag.document_versions v ON v.document_id = d.document_id
                   JOIN rag.source_units u ON u.version_id = v.version_id
                   WHERE d.app_id = %s AND d.collection_id = %s AND d.document_id = %s
                     AND v.version_id = %s AND u.unit_id = %s""",
                (scope.app_id, scope.collection_id, document_id, version_id, unit_id),
            ).fetchone()
        if row is None:
            raise DocumentNotFound()
        return SourceView.model_validate(row)


ACTIVE_CHUNKS = """FROM rag.documents d
    JOIN rag.document_versions v ON v.document_id = d.document_id AND v.revision = d.active_revision
    JOIN rag.index_builds b ON b.version_id = v.version_id
    JOIN rag.chunks c ON c.index_id = b.index_id
    WHERE d.app_id = %s AND d.collection_id = %s
      AND b.index_profile_id = %s AND b.status = 'ready'"""


class PostgresSearchSnapshot:
    def __init__(self, conn, scope: Scope, profile_id: str):
        self.conn = conn
        self.scope = scope
        self.params = (scope.app_id, scope.collection_id, profile_id)

    def vector_ids(self, vector: list[float], k: int) -> list[UUID]:
        rows = self.conn.execute(
            f"""WITH scoped AS MATERIALIZED (SELECT c.chunk_id, c.embedding {ACTIVE_CHUNKS})
                SELECT chunk_id FROM scoped ORDER BY embedding <=> %s, chunk_id LIMIT %s""",
            (*self.params, Vector(vector), k),
        ).fetchall()
        return [row["chunk_id"] for row in rows]

    def lexical_corpus(self, limit: int) -> list[tuple[UUID, str]]:
        rows = self.conn.execute(f"""SELECT c.chunk_id, c.content {ACTIVE_CHUNKS}
                                     ORDER BY c.chunk_id LIMIT %s""", (*self.params, limit)).fetchall()
        return [(row["chunk_id"], row["content"]) for row in rows]

    def candidates(self, ids: list[UUID]) -> list[Candidate]:
        if not ids:
            return []
        rows = self.conn.execute(
            f"""WITH selected AS (
                   SELECT c.*, d.document_id {ACTIVE_CHUNKS} AND c.chunk_id = ANY(%s)
               )
               SELECT r.document_id, r.version_id, r.unit_id, r.chunk_id, r.content, r.paragraph_id,
                      r.start_offset, r.end_offset, u.parser_profile_id, u.page, u.page_label,
                      u.heading_path, u.start_line, u.end_line, u.source_text
               FROM selected r JOIN rag.source_units u ON u.unit_id = r.unit_id""",
            (*self.params, ids),
        ).fetchall()
        candidates = {
            row["chunk_id"]: Candidate(
                evidence_id=str(row["chunk_id"]), scope_id=self.scope.id, source_id=row["document_id"],
                content=row["content"], source_text=row["source_text"],
                locator=DocumentLocator(
                    version_id=row["version_id"], unit_id=row["unit_id"], chunk_id=row["chunk_id"],
                    parser_profile_id=row["parser_profile_id"], page=row["page"], page_label=row["page_label"],
                    heading_path=row["heading_path"], start_line=row["start_line"], end_line=row["end_line"],
                    paragraph_id=row["paragraph_id"], start=row["start_offset"], end=row["end_offset"],
                ),
            )
            for row in rows
        }
        return [candidates[chunk_id] for chunk_id in ids]
