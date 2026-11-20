"""Read-only, allowlisted business tools and provenance construction."""

from hashlib import sha256
import json
from pathlib import Path
from uuid import uuid4

import psycopg
from pydantic import BaseModel, ValidationError

from enterprise_rag.core.citations import evidence_from_candidate, verified_candidates
from enterprise_rag.core.documents import DocumentNotFound, Scope, parse_document, text_hash
from enterprise_rag.core.embeddings import EmbeddingError, IndexProfileConflict
from enterprise_rag.core.service import RAGService

from ..contracts import (
    APIEvidence, PolicyEvidence, SQLEvidence, SNAPSHOT, SalesData,
    inventory_status, sales_summary, warehouse_policy,
)
from .inventory import InventoryClient, ToolFailure

SALES_SQL = """
    SELECT count(*), coalesce(sum(units), 0), coalesce(sum(units::bigint * unit_price_cents), 0)
    FROM enterprise_demo.sales
    WHERE sales_day = %s AND region = %s AND snapshot = %s
"""
POLICY_HEADING = "盘点异常"
POLICY_QUERY = "发现账面库存与实物不符时，应在多久内登记盘点差异？"
POLICY_CANDIDATES = 10
POLICY_PATH = Path("data/samples/enterprise_inventory.md")
POLICY_FILENAME = "warehouse-policy.md"
READER_CHECK = """
    SELECT rolname = 'enterprise_reader'
       AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
       AND NOT EXISTS (SELECT 1 FROM pg_auth_members WHERE member = pg_roles.oid)
       AND NOT has_schema_privilege(current_user, 'rag', 'USAGE')
       AND NOT has_table_privilege(current_user, 'enterprise_demo.sales', 'INSERT, UPDATE, DELETE, TRUNCATE')
    FROM pg_roles WHERE rolname = current_user
"""


def content_digest(data: BaseModel) -> str:
    raw = json.dumps(data.model_dump(mode="json"), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return sha256(raw.encode()).hexdigest()


class SalesRepository:
    def __init__(self, dsn: str):
        self._dsn = dsn

    def lookup(self, params: sales_summary) -> SalesData:
        try:
            with psycopg.connect(self._dsn, connect_timeout=3,
                                 options="-c statement_timeout=2500 -c lock_timeout=800") as conn:
                conn.execute("SET TRANSACTION READ ONLY")
                if not conn.execute(READER_CHECK).fetchone()[0]:
                    raise ToolFailure("sql_role_invalid")
                count, units, cents = conn.execute(SALES_SQL, (params.day, params.region, SNAPSHOT)).fetchone()
                return SalesData(order_count=count, units=units, revenue_cents=int(cents))
        except psycopg.errors.QueryCanceled as error:
            raise ToolFailure("sql_timeout") from error
        except psycopg.Error as error:
            raise ToolFailure("sql_unavailable") from error
        except (ValidationError, ValueError) as error:
            raise ToolFailure("sql_invalid") from error


class BusinessTools:
    def __init__(self, sales: SalesRepository, inventory: InventoryClient, rag: RAGService, scope: Scope):
        self.sales = sales
        self.inventory = inventory
        self.rag = rag
        self.scope = scope
        policy = parse_document(POLICY_FILENAME, POLICY_PATH.read_bytes())
        self._passages = {
            text_hash(unit.text) for unit in policy.units
            if unit.heading_path and unit.heading_path[-1] == POLICY_HEADING and not unit.text.lstrip().startswith("#")
        }

    def policy_source(self, document_id, version_id, unit_id):
        source = self.rag.store.source(self.scope, document_id, version_id, unit_id)
        if source.text_sha256 not in self._passages or source.heading_path[-1:] != [POLICY_HEADING]:
            raise DocumentNotFound()
        return source

    def execute(self, params: sales_summary | inventory_status | warehouse_policy):
        if isinstance(params, sales_summary):
            data = self.sales.lookup(params)
            return SQLEvidence(evidence_id=uuid4().hex, parameters=params, data=data, content_hash=content_digest(data))
        if isinstance(params, inventory_status):
            data = self.inventory.lookup(params.sku)
            return APIEvidence(evidence_id=uuid4().hex, endpoint=f"/inventory/{params.sku}",
                               data=data, content_hash=content_digest(data))
        try:
            ranked = self.rag.retriever.search(self.scope, POLICY_QUERY, POLICY_CANDIDATES)
            candidates = verified_candidates(ranked, self.scope.id)
            for item in candidates.values():
                loc = item.locator
                if text_hash(item.source_text) not in self._passages or loc.heading_path[-1:] != [POLICY_HEADING]:
                    continue
                source = self.policy_source(item.source_id, loc.version_id, loc.unit_id)
                if source.source_text[loc.start:loc.end] == item.content:
                    return PolicyEvidence(evidence_id=item.evidence_id, document=evidence_from_candidate(item))
        except (EmbeddingError, IndexProfileConflict, DocumentNotFound, psycopg.Error) as error:
            raise ToolFailure("policy_unavailable") from error
        raise ToolFailure("policy_no_evidence")
