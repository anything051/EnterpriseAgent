"""Metadata for courses whose documents live in the shared versioned store."""

from typing import Literal
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field

from enterprise_rag.core.documents import DocumentNotFound, Scope


class CatalogInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=120)


class CourseInput(CatalogInput):
    term: str = Field(min_length=1, max_length=80)


class ChapterInput(CatalogInput):
    position: int = Field(default=1, ge=1, le=10000)
    parent_id: UUID | None = None


class PointInput(CatalogInput):
    chapter_id: UUID
    notes: str = Field(default="", max_length=1600)


class MaterialInput(CatalogInput):
    chapter_id: UUID
    point_id: UUID | None = None
    format: Literal["pdf", "md"]


def logical_name(material_id: UUID, format: str) -> str:
    return f"material-{material_id.hex}.{format}"


class CourseCatalog:
    def __init__(self, dsn: str, app_id: str):
        self.dsn = dsn
        self.app_id = app_id

    def _connect(self):
        return psycopg.connect(self.dsn, connect_timeout=5, row_factory=dict_row)

    def _course(self, conn, course_id: UUID):
        row = conn.execute("SELECT id, title, term FROM course_rag.courses WHERE app_id=%s AND id=%s",
                           (self.app_id, course_id)).fetchone()
        if row is None:
            raise DocumentNotFound()
        return row

    def scope(self, course_id: UUID) -> Scope:
        with self._connect() as conn:
            self._course(conn, course_id)
        return Scope(self.app_id, f"course-{course_id.hex}")

    def list_courses(self):
        with self._connect() as conn:
            return conn.execute("SELECT id, title, term FROM course_rag.courses WHERE app_id=%s ORDER BY title, id",
                                (self.app_id,)).fetchall()

    def create_course(self, data: CourseInput):
        with self._connect() as conn:
            return conn.execute(
                "INSERT INTO course_rag.courses (id, app_id, title, term) VALUES (%s,%s,%s,%s) "
                "RETURNING id,title,term", (uuid4(), self.app_id, data.title, data.term),
            ).fetchone()

    def _chapter(self, conn, course_id, chapter_id):
        if conn.execute("SELECT 1 FROM course_rag.chapters WHERE course_id=%s AND id=%s",
                        (course_id, chapter_id)).fetchone() is None:
            raise DocumentNotFound()

    def create_chapter(self, course_id: UUID, data: ChapterInput):
        with self._connect() as conn:
            self._course(conn, course_id)
            if data.parent_id is not None:
                self._chapter(conn, course_id, data.parent_id)
            return conn.execute(
                "INSERT INTO course_rag.chapters (id,course_id,title,position,parent_id) VALUES (%s,%s,%s,%s,%s) "
                "RETURNING id,title,position,parent_id",
                (uuid4(), course_id, data.title, data.position, data.parent_id),
            ).fetchone()

    def create_point(self, course_id: UUID, data: PointInput):
        with self._connect() as conn:
            self._course(conn, course_id)
            self._chapter(conn, course_id, data.chapter_id)
            return conn.execute(
                "INSERT INTO course_rag.points (id,course_id,chapter_id,title,notes) VALUES (%s,%s,%s,%s,%s) "
                "RETURNING id,chapter_id,title,notes",
                (uuid4(), course_id, data.chapter_id, data.title, data.notes),
            ).fetchone()

    def create_material(self, course_id: UUID, data: MaterialInput):
        with self._connect() as conn:
            self._course(conn, course_id)
            self._chapter(conn, course_id, data.chapter_id)
            if data.point_id is not None and conn.execute(
                "SELECT 1 FROM course_rag.points WHERE course_id=%s AND chapter_id=%s AND id=%s",
                (course_id, data.chapter_id, data.point_id),
            ).fetchone() is None:
                raise DocumentNotFound()
            return conn.execute(
                "INSERT INTO course_rag.materials (id,course_id,chapter_id,point_id,title,format) "
                "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id,chapter_id,point_id,title,format",
                (uuid4(), course_id, data.chapter_id, data.point_id, data.title, data.format),
            ).fetchone()

    def material(self, course_id: UUID, material_id: UUID):
        with self._connect() as conn:
            self._course(conn, course_id)
            result = conn.execute(
                "SELECT id, title, chapter_id, point_id, format FROM course_rag.materials WHERE course_id=%s AND id=%s",
                (course_id, material_id),
            ).fetchone()
        if result is None:
            raise DocumentNotFound()
        return result

    def snapshot(self, course_id: UUID):
        with self._connect() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            course = self._course(conn, course_id)
            chapters = conn.execute(
                "SELECT id,title,position,parent_id FROM course_rag.chapters WHERE course_id=%s ORDER BY position,title,id",
                (course_id,),
            ).fetchall()
            points = conn.execute(
                "SELECT id,chapter_id,title,notes FROM course_rag.points WHERE course_id=%s ORDER BY title,id", (course_id,),
            ).fetchall()
            materials = conn.execute(
                """SELECT m.id,m.chapter_id,m.point_id,m.title,m.format,d.document_id,d.active_revision
                   FROM course_rag.materials m
                   LEFT JOIN rag.documents d ON d.app_id=%s AND d.collection_id=%s
                      AND d.logical_name='material-' || replace(m.id::text,'-','') || '.' || m.format
                   WHERE m.course_id=%s ORDER BY m.title,m.id""",
                (self.app_id, f"course-{course_id.hex}", course_id),
            ).fetchall()
        return {"course": course, "chapters": chapters, "points": points, "materials": materials}

    def revisions(self, course_id: UUID, material_id: UUID):
        material = self.material(course_id, material_id)
        with self._connect() as conn:
            return conn.execute(
                """SELECT v.version_id, v.revision, v.file_sha256, v.created_at, v.revision=d.active_revision AS active
                   FROM rag.documents d JOIN rag.document_versions v ON v.document_id=d.document_id
                   WHERE d.app_id=%s AND d.collection_id=%s AND d.logical_name=%s
                     AND EXISTS (SELECT 1 FROM rag.index_builds i WHERE i.version_id=v.version_id AND i.status='ready')
                   ORDER BY v.revision DESC""",
                (self.app_id, f"course-{course_id.hex}", logical_name(material_id, material["format"])),
            ).fetchall()
