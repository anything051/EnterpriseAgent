BEGIN;
CREATE OR REPLACE FUNCTION rag.highlight_source(text, text, uuid, uuid, uuid)
RETURNS TABLE (page integer, source_text text, original bytea)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT u.page, u.source_text, raw.content
    FROM rag.documents d
    JOIN rag.document_versions v ON v.document_id = d.document_id
    JOIN rag.source_units u ON u.version_id = v.version_id
    LEFT JOIN rag.blobs raw ON raw.sha256 = v.file_sha256 AND v.media_type = 'application/pdf'
    WHERE d.app_id = $1 AND d.collection_id = $2 AND d.document_id = $3
      AND v.version_id = $4 AND u.unit_id = $5;
$function$;
REVOKE ALL ON FUNCTION rag.highlight_source(text, text, uuid, uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION rag.highlight_source(text, text, uuid, uuid, uuid) TO rag_app;
COMMIT;
