"""A small HTTP client for SEC EDGAR.

Responsibilities:
- Always send the SEC-required ``User-Agent`` header.
- Stay under SEC's 10 requests/second limit (a simple thread-safe throttle).
- Retry transient failures (429/5xx) with backoff.

Callers get back ``requests.Response`` (or parsed text/json/bytes). Network and
HTTP errors propagate as ``requests`` exceptions; the orchestration layer turns
them into per-ticker error rows so one failure never aborts a batch.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import config

logger = logging.getLogger(__name__)


class SECRequestThrottle:
    """Thread-safe request pacing that can be shared across client sessions."""

    def __init__(self, max_rps: float) -> None:
        self._min_interval = 1.0 / max_rps if max_rps > 0 else 0.0
        self._lock = threading.Lock()
        self._last_request = 0.0

    def wait(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            elapsed = time.monotonic() - self._last_request
            wait = self._min_interval - elapsed
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()


class SECClient:
    """Rate-limited, retrying HTTP client scoped to SEC endpoints."""

    def __init__(
        self,
        user_agent: str = config.USER_AGENT,
        max_rps: float = config.MAX_REQUESTS_PER_SECOND,
        timeout: int = config.REQUEST_TIMEOUT,
        max_retries: int = config.MAX_RETRIES,
        session: Optional[requests.Session] = None,
        throttle: Optional[SECRequestThrottle] = None,
    ) -> None:
        self.timeout = timeout
        self.throttle = throttle or SECRequestThrottle(max_rps)

        self.session = session or requests.Session()
        self.session.headers.update(
            {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
        )
        retry = Retry(
            total=max_retries,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def _throttle(self) -> None:
        """Block just long enough to honour the max requests/second limit."""
        self.throttle.wait()

    def get(self, url: str, params: Optional[dict] = None) -> requests.Response:
        self._throttle()
        logger.debug("GET %s params=%s", url, params)
        response = self.session.get(url, params=params, timeout=self.timeout)
        response.raise_for_status()
        return response

    def get_text(self, url: str, params: Optional[dict] = None) -> str:
        return self.get(url, params=params).text

    def get_json(self, url: str, params: Optional[dict] = None) -> Any:
        return self.get(url, params=params).json()

    def get_bytes(self, url: str, params: Optional[dict] = None) -> bytes:
        return self.get(url, params=params).content

    def close(self) -> None:
        self.session.close()
