"""Explicit REST timing, pacing, and retries. No Lamin imports or hidden retries."""

from __future__ import annotations

import os
import time

import httpx

from benchmarks.measurements import write_measurement

METRICS = (
    "api_request_count",
    "api_request_ms",
    "rate_limit_wait_ms",
    "retry_count",
    "api_response_bytes",
)


class NotionError(RuntimeError):
    pass


class NotionClient:
    def __init__(
        self, params: dict, *, transport=None, clock=time.perf_counter, sleep=time.sleep
    ):
        token = os.environ.get("NOTION_API_KEY", "").strip()
        if not token:
            raise ValueError("Set NOTION_API_KEY to a PAT or internal connection token")
        self.http = httpx.Client(
            base_url="https://api.notion.com/v1/",
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": params["notion_api_version"],
            },
            timeout=60,
            transport=transport,
        )
        self.params, self.clock, self.sleep = params, clock, sleep
        self.next_request = 0.0
        self.rows: list[dict] = []
        self.totals = dict.fromkeys(METRICS, 0)
        self.operation = "setup"
        self.batch_index = self.repetition = 0

    def snapshot(self) -> dict:
        return self.totals.copy()

    def delta(self, previous: dict) -> dict:
        return {key: round(self.totals[key] - previous[key], 3) for key in METRICS}

    def request(
        self, method: str, path: str, *, body=None, params=None, read_only=False
    ) -> dict:
        """Only read operations retry ambiguous 5xx/network failures.

        A create that times out or returns 5xx may already have committed. Stop
        rather than create duplicates; a resumed setup inventories existing UIDs.
        """
        retry_wait = 0.0
        for attempt in range(self.params["notion_max_retries"] + 1):
            wait = max(0.0, self.next_request - self.clock()) + retry_wait
            if wait:
                self.sleep(wait)
            started = self.clock()
            self.next_request = started + 1 / self.params["notion_requests_per_second"]
            response = None
            try:
                response = self.http.request(method, path, json=body, params=params)
            except httpx.TransportError:
                # Do not print the request object (it contains credentials).
                if not read_only:
                    reason = "ambiguous_write"
                else:
                    reason = "transport_error"
            else:
                reason = "http_error"
            elapsed = (self.clock() - started) * 1000
            status = response.status_code if response is not None else 0
            size = len(response.content) if response is not None else 0
            self.rows.append(
                {
                    "repetition": self.repetition,
                    "batch_index": self.batch_index,
                    "operation": self.operation,
                    "method": method,
                    "endpoint": path,
                    "attempt": attempt + 1,
                    "status_code": status,
                    "latency_ms": round(elapsed, 3),
                    "wait_ms": round(wait * 1000, 3),
                    "response_bytes": size,
                    "request_id": response.headers.get("x-request-id", "")
                    if response is not None
                    else "",
                }
            )
            for key, value in zip(
                METRICS, (1, elapsed, wait * 1000, int(attempt > 0), size), strict=True
            ):
                self.totals[key] += value
            if response is not None and response.is_success:
                return response.json()
            detail = {}
            if response is not None:
                try:
                    detail = response.json()
                except ValueError:
                    pass
            blocked = (
                detail.get("additional_data", {}).get("rate_limit_reason")
                == "public_api_request_blocked"
            )
            retryable = not blocked and (
                status in (429, 529)
                or (read_only and status in (0, 500, 502, 503, 504))
            )
            if not retryable or attempt == self.params["notion_max_retries"]:
                raise NotionError(
                    f"Notion {method} {path}: status {status}; {detail.get('code', reason)}"
                )
            header = (
                response.headers.get("retry-after") if response is not None else None
            )
            retry_wait = max(float(header) if header else 0, min(2**attempt, 60))
        raise AssertionError("unreachable")

    def query(self, source: str, *, filter=None, properties=None):
        cursor = None
        while True:
            body = {"page_size": 100}
            if filter is not None:
                body["filter"] = filter
            if cursor:
                body["start_cursor"] = cursor
            response = self.request(
                "POST",
                f"data_sources/{source}/query",
                body=body,
                read_only=True,
                params=[("filter_properties", name) for name in properties]
                if properties
                else None,
            )
            if response.get("request_status", {}).get("type") == "incomplete":
                raise NotionError(
                    "Notion capped this query; refusing incomplete measurements"
                )
            yield from response["results"]
            if not response.get("has_more"):
                return
            next_cursor = response.get("next_cursor")
            if not next_cursor or next_cursor == cursor:
                raise NotionError("Invalid pagination cursor")
            cursor = next_cursor

    def children(self, page: str):
        cursor = None
        while True:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            result = self.request(
                "GET", f"blocks/{page}/children", params=params, read_only=True
            )
            yield from result["results"]
            if not result.get("has_more"):
                return
            next_cursor = result.get("next_cursor")
            if not next_cursor or next_cursor == cursor:
                raise NotionError("Invalid block pagination cursor")
            cursor = next_cursor

    def close(self, stage: str):
        try:
            write_measurement(
                f"{stage}_requests",
                self.rows,
                columns=(
                    "repetition",
                    "batch_index",
                    "operation",
                    "method",
                    "endpoint",
                    "attempt",
                    "status_code",
                    "latency_ms",
                    "wait_ms",
                    "response_bytes",
                    "request_id",
                ),
                key=f"benchmarks/measurements/experiment_005/{stage}_requests.parquet",
                description="One row per HTTP attempt; latency excludes separately recorded pacing/retry waits.",
                features={
                    "measurement_kind": "http_requests",
                    "query_name": f"{stage}_requests",
                    "n_rows": self.params["n_rows"],
                    "backend": "notion",
                    **self.snapshot(),
                },
            )
        finally:
            self.http.close()
