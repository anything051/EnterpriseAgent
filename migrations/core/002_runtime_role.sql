\set ON_ERROR_STOP on
\getenv rag_password RAG_DB_APP_PASSWORD
BEGIN;
CREATE ROLE rag_app LOGIN PASSWORD :'rag_password';
REVOKE CONNECT ON DATABASE enterprise_rag FROM PUBLIC;
GRANT CONNECT ON DATABASE enterprise_rag TO rag_app;
GRANT USAGE ON SCHEMA rag TO rag_app;
GRANT SELECT, INSERT ON rag.collections, rag.documents, rag.document_versions,
    rag.index_builds, rag.source_units, rag.chunks TO rag_app;
GRANT INSERT, SELECT (sha256) ON rag.blobs TO rag_app;
GRANT UPDATE (active_revision) ON rag.documents TO rag_app;
GRANT UPDATE (status, failure_code, ready_at, chunk_count) ON rag.index_builds TO rag_app;
ALTER ROLE rag_app SET statement_timeout = '15s';
ALTER ROLE rag_app SET lock_timeout = '5s';
COMMIT;
