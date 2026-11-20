BEGIN;
CREATE SCHEMA IF NOT EXISTS course_rag;

CREATE TABLE IF NOT EXISTS course_rag.courses (
    id uuid PRIMARY KEY,
    app_id text NOT NULL,
    title text NOT NULL,
    term text NOT NULL
);

CREATE TABLE IF NOT EXISTS course_rag.chapters (
    id uuid PRIMARY KEY,
    course_id uuid NOT NULL REFERENCES course_rag.courses (id),
    title text NOT NULL,
    position integer NOT NULL CHECK (position > 0),
    parent_id uuid,
    UNIQUE (course_id, id),
    FOREIGN KEY (course_id, parent_id) REFERENCES course_rag.chapters (course_id, id)
);

CREATE TABLE IF NOT EXISTS course_rag.points (
    id uuid PRIMARY KEY,
    course_id uuid NOT NULL,
    chapter_id uuid NOT NULL,
    title text NOT NULL,
    notes text NOT NULL,
    UNIQUE (course_id, chapter_id, id),
    FOREIGN KEY (course_id, chapter_id) REFERENCES course_rag.chapters (course_id, id)
);

CREATE TABLE IF NOT EXISTS course_rag.materials (
    id uuid PRIMARY KEY,
    course_id uuid NOT NULL,
    chapter_id uuid NOT NULL,
    point_id uuid,
    title text NOT NULL,
    format text NOT NULL CHECK (format IN ('pdf', 'md')),
    UNIQUE (course_id, chapter_id, id),
    FOREIGN KEY (course_id, chapter_id) REFERENCES course_rag.chapters (course_id, id),
    FOREIGN KEY (course_id, chapter_id, point_id) REFERENCES course_rag.points (course_id, chapter_id, id)
);

GRANT USAGE ON SCHEMA course_rag TO rag_app;
GRANT SELECT, INSERT ON course_rag.courses, course_rag.chapters, course_rag.points, course_rag.materials TO rag_app;
COMMIT;
