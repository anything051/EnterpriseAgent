BEGIN;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA rag;

CREATE TABLE rag.collections (
    app_id text NOT NULL,
    collection_id text NOT NULL,
    index_profile_id text NOT NULL,
    profile jsonb NOT NULL,
    PRIMARY KEY (app_id, collection_id)
);

CREATE TABLE rag.blobs (
    sha256 text PRIMARY KEY CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    content bytea NOT NULL
);

CREATE TABLE rag.documents (
    document_id uuid PRIMARY KEY,
    app_id text NOT NULL,
    collection_id text NOT NULL,
    logical_name text NOT NULL,
    active_revision integer,
    FOREIGN KEY (app_id, collection_id) REFERENCES rag.collections (app_id, collection_id),
    UNIQUE (app_id, collection_id, logical_name)
);

CREATE TABLE rag.document_versions (
    version_id uuid PRIMARY KEY,
    document_id uuid NOT NULL REFERENCES rag.documents,
    revision integer NOT NULL CHECK (revision > 0),
    filename text NOT NULL,
    media_type text NOT NULL CHECK (media_type IN ('text/markdown', 'application/pdf')),
    file_sha256 text NOT NULL REFERENCES rag.blobs (sha256),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id, revision),
    UNIQUE (document_id, file_sha256)
);

ALTER TABLE rag.documents ADD FOREIGN KEY (document_id, active_revision)
    REFERENCES rag.document_versions (document_id, revision);

CREATE TABLE rag.index_builds (
    index_id uuid PRIMARY KEY,
    version_id uuid NOT NULL REFERENCES rag.document_versions,
    index_profile_id text NOT NULL,
    dimension integer NOT NULL CHECK (dimension BETWEEN 1 AND 16000),
    status text NOT NULL CHECK (status IN ('pending', 'failed', 'ready')),
    failure_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    ready_at timestamptz,
    chunk_count integer CHECK (chunk_count > 0),
    CHECK ((status = 'ready') = (ready_at IS NOT NULL AND chunk_count IS NOT NULL)),
    UNIQUE (version_id, index_profile_id),
    UNIQUE (index_id, version_id, dimension)
);

CREATE TABLE rag.source_units (
    unit_id uuid PRIMARY KEY,
    version_id uuid NOT NULL REFERENCES rag.document_versions,
    parser_profile_id text NOT NULL,
    unit_key text NOT NULL,
    source_text text NOT NULL,
    text_sha256 text NOT NULL,
    paragraphs jsonb NOT NULL,
    page integer CHECK (page > 0),
    page_label text,
    heading_path text[] NOT NULL,
    start_line integer CHECK (start_line > 0),
    end_line integer CHECK (end_line >= start_line),
    UNIQUE (version_id, parser_profile_id, unit_key),
    UNIQUE (unit_id, version_id)
);

CREATE TABLE rag.chunks (
    chunk_id uuid PRIMARY KEY,
    index_id uuid NOT NULL,
    version_id uuid NOT NULL,
    unit_id uuid NOT NULL,
    paragraph_id text NOT NULL,
    start_offset integer NOT NULL CHECK (start_offset >= 0),
    end_offset integer NOT NULL CHECK (end_offset > start_offset),
    content text NOT NULL CHECK (char_length(content) = end_offset - start_offset),
    dimension integer NOT NULL,
    embedding vector NOT NULL CHECK (vector_dims(embedding) = dimension),
    FOREIGN KEY (index_id, version_id, dimension) REFERENCES rag.index_builds (index_id, version_id, dimension),
    FOREIGN KEY (unit_id, version_id) REFERENCES rag.source_units (unit_id, version_id),
    UNIQUE (index_id, unit_id, start_offset, end_offset)
);

COMMIT;
