"""HTTP adapter and loopback service for the synthetic inventory snapshot."""

import json
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException
import httpx
from pydantic import ValidationError

from ..contracts import InventoryData, SKU

MAX_RESPONSE = 8192
DEFAULT_FIXTURE = Path("data/enterprise_agent/demo/inventory.json")


class ToolFailure(Exception):
    """A safe error code for user-visible tool audit."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class InventoryClient:
    def __init__(self, get):
        self._get = get

    @classmethod
    def from_url(cls, base_url: str):
        parts = urlsplit(base_url)
        if (parts.scheme != "http" or parts.hostname != "127.0.0.1" or parts.port is None
                or parts.username is not None or parts.password is not None or parts.path not in ("", "/")
                or parts.query or parts.fragment):
            raise ValueError("Inventory URL must be a local 127.0.0.1 HTTP origin with an explicit port")

        def get(path: str):
            with httpx.Client(timeout=httpx.Timeout(3, connect=2), follow_redirects=False, trust_env=False,
                              headers={"Accept-Encoding": "identity"}) as client:
                with client.stream("GET", base_url.rstrip("/") + path) as reply:
                    reply.raise_for_status()
                    if reply.headers.get("content-encoding", "identity") != "identity":
                        raise ValueError("Unexpected inventory encoding")
                    payload = bytearray()
                    for chunk in reply.iter_bytes(chunk_size=MAX_RESPONSE + 1):
                        payload.extend(chunk)
                        if len(payload) > MAX_RESPONSE:
                            raise ValueError("Inventory response too large")
                    return json.loads(payload)

        return cls(get)

    def lookup(self, sku: SKU) -> InventoryData:
        try:
            item = InventoryData.model_validate(self._get(f"/inventory/{sku}"))
            if item.sku != sku:
                raise ValueError("Mismatched inventory SKU")
            return item
        except httpx.TimeoutException as error:
            raise ToolFailure("inventory_timeout") from error
        except httpx.HTTPError as error:
            raise ToolFailure("inventory_unavailable") from error
        except (ValidationError, ValueError, TypeError) as error:
            raise ToolFailure("inventory_invalid") from error


def create_inventory_app(fixture: Path = DEFAULT_FIXTURE) -> FastAPI:
    snapshot = json.loads(fixture.read_text(encoding="utf-8"))
    data = [InventoryData.model_validate({**row, "snapshot": snapshot["snapshot"]}) for row in snapshot["items"]]
    by_sku = {item.sku: item for item in data}
    if len(by_sku) != len(data):
        raise ValueError("Duplicate inventory item")

    app = FastAPI(title="Synthetic Inventory API")

    @app.get("/inventory/{sku}")
    def lookup(sku: SKU) -> InventoryData:
        if sku not in by_sku:
            raise HTTPException(404, "Inventory item not found.")
        return by_sku[sku]

    return app
