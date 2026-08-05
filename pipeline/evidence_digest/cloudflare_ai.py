"""Bounded stdlib client for Cloudflare Workers AI summary generation."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable

from evidence_digest.zh_summary import (
    MODEL_ID,
    SummaryValidationError,
    build_messages,
    parse_response,
)


_MAX_RESPONSE_BYTES = 1024 * 1024


class CloudflareAIError(RuntimeError):
    """A sanitized error from the Workers AI boundary."""


class _EnvelopeError(ValueError):
    """The Workers AI response envelope is incomplete or unsuccessful."""


_RETRYABLE_ERRORS = (
    urllib.error.HTTPError,
    urllib.error.URLError,
    TimeoutError,
    json.JSONDecodeError,
    SummaryValidationError,
    _EnvelopeError,
)


class CloudflareAIClient:
    def __init__(
        self,
        account_id: str,
        api_token: str,
        *,
        opener: Callable = urllib.request.urlopen,
        sleep_fn: Callable[[float], None] = time.sleep,
        timeout: float = 30.0,
    ):
        self._account_id = account_id
        self._api_token = api_token
        self._opener = opener
        self._sleep_fn = sleep_fn
        self._timeout = timeout

    def summarize(self, study: dict) -> dict[str, str]:
        for attempt in range(3):
            try:
                return self._request_once(study)
            except _RETRYABLE_ERRORS as exc:
                if isinstance(exc, urllib.error.HTTPError) and not (
                    exc.code == 429 or 500 <= exc.code <= 599
                ):
                    raise CloudflareAIError(
                        f"non-retryable HTTP {exc.code}"
                    ) from None
                if attempt == 2:
                    raise CloudflareAIError(
                        f"Workers AI failed after 3 attempts: {type(exc).__name__}"
                    ) from None
                self._sleep_fn(float(2**attempt))
        raise AssertionError("unreachable")

    def _request_once(self, study: dict) -> dict[str, str]:
        url = (
            "https://api.cloudflare.com/client/v4/accounts/"
            f"{self._account_id}/ai/run/{MODEL_ID}"
        )
        body = json.dumps(
            {
                "messages": build_messages(study),
                "max_tokens": 800,
                "temperature": 0.1,
                "stream": False,
                "response_format": {"type": "json_object"},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {self._api_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        with self._opener(request, timeout=self._timeout) as response:
            envelope = json.loads(response.read(_MAX_RESPONSE_BYTES))

        if not isinstance(envelope, dict) or envelope.get("success") is not True:
            raise _EnvelopeError("unsuccessful response envelope")
        result = envelope.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("response"), str):
            raise _EnvelopeError("missing model response")
        return parse_response(result["response"])
