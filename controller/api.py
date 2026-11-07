"""HTTP client for the simulator. Retries transport errors and 5xx; 4xx become ApiError for the caller."""
from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger("api")


class ApiError(Exception):
    def __init__(self, status: int, code: str | None, detail=None):
        super().__init__(f"{status} {code}: {detail}")
        self.status, self.code, self.detail = status, code, detail


class Sim:
    def __init__(self, url: str, retries: int = 8):
        self.c = httpx.Client(base_url=url, timeout=300)
        self.retries = retries

    def _req(self, method: str, path: str, **kw) -> httpx.Response:
        delay = 0.25
        for attempt in range(self.retries):
            try:
                r = self.c.request(method, path, **kw)
            except httpx.TransportError as e:
                log.warning("transport error %s %s: %s (attempt %d)", method, path, e, attempt + 1)
                time.sleep(delay)
                delay = min(delay * 2, 10)
                continue
            if r.status_code >= 500:
                log.warning("server error %s %s: %s (attempt %d)", method, path, r.status_code, attempt + 1)
                time.sleep(delay)
                delay = min(delay * 2, 10)
                continue
            if r.status_code >= 400:
                try:
                    b = r.json()
                except ValueError:
                    b = {}
                raise ApiError(r.status_code, b.get("error") or b.get("detail"), b.get("detail"))
            return r
        raise RuntimeError(f"simulator unreachable: {method} {path}")

    def get(self, path, **params):
        return self._req("GET", path, params=params or None).json()

    def post(self, path, body):
        return self._req("POST", path, json=body).json()

    def put(self, path, body):
        return self._req("PUT", path, json=body).json()

    def chunk_text(self, n: int) -> str:
        return self._req("GET", f"/events/chunks/{n}").text

    def advance(self, minutes: int, expect_from: int) -> dict:
        """Advance is not idempotent. If the response is lost we check the clock before retrying,
        so a retry never advances the world twice."""
        try:
            return self.post("/sim/advance", {"minutes": minutes})
        except RuntimeError:
            st = self.get("/sim/state")
            if st["sim_minute"] > expect_from:
                return {**st, "new_chunks": []}  # it did advance; the ingestor picks the chunks up by cursor
            raise
