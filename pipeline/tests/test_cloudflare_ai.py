import io
import json
import unittest
import urllib.error

from evidence_digest.cloudflare_ai import CloudflareAIClient, CloudflareAIError
from evidence_digest.zh_summary import MODEL_ID, build_messages


STUDY = {
    "pmid": "12345678",
    "title": "A controlled trial",
    "abstract": "A concise English abstract.",
}
VALID_SUMMARY = {
    "objective": "研究目的",
    "methods": "研究方法",
    "results": "主要结果",
    "conclusion": "结论",
}
TOKEN = "secret-api-token"


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.read_sizes = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, size=-1):
        self.read_sizes.append(size)
        return self.payload[:size] if size >= 0 else self.payload


class FakeOpener:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = []
        self.timeouts = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        self.timeouts.append(timeout)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def success_response(summary=VALID_SUMMARY):
    envelope = {
        "success": True,
        "result": {"response": json.dumps(summary, ensure_ascii=False)},
    }
    return FakeResponse(json.dumps(envelope, ensure_ascii=False).encode("utf-8"))


def structured_success_response(summary=VALID_SUMMARY):
    envelope = {
        "success": True,
        "result": {"response": summary},
    }
    return FakeResponse(json.dumps(envelope, ensure_ascii=False).encode("utf-8"))


def http_error(code):
    return urllib.error.HTTPError(
        "https://api.cloudflare.com", code, "sensitive server message", {}, io.BytesIO()
    )


class CloudflareAIClientTests(unittest.TestCase):
    def test_builds_workers_ai_request_and_returns_validated_summary(self):
        response = success_response()
        opener = FakeOpener([response])
        client = CloudflareAIClient(
            "account-123", TOKEN, opener=opener, sleep_fn=self.fail, timeout=12.5
        )

        result = client.summarize(STUDY)

        self.assertEqual(result, VALID_SUMMARY)
        self.assertEqual(len(opener.requests), 1)
        request = opener.requests[0]
        self.assertIn("/accounts/account-123/ai/run/" + MODEL_ID, request.full_url)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + TOKEN)
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(opener.timeouts, [12.5])
        self.assertEqual(
            json.loads(request.data.decode("utf-8")),
            {
                "messages": build_messages(STUDY),
                "max_tokens": 800,
                "temperature": 0.1,
                "stream": False,
                "response_format": {"type": "json_object"},
            },
        )
        self.assertEqual(response.read_sizes, [1024 * 1024])

    def test_accepts_and_validates_structured_json_response(self):
        structured_summary = {
            "objective": "  研究目的  ",
            "methods": "  研究方法  ",
            "results": "  主要结果  ",
            "conclusion": "  结论  ",
        }
        opener = FakeOpener([structured_success_response(structured_summary)])
        client = CloudflareAIClient(
            "account-123", TOKEN, opener=opener, sleep_fn=self.fail
        )

        result = client.summarize(STUDY)

        self.assertEqual(result, VALID_SUMMARY)
        self.assertEqual(len(opener.requests), 1)

    def test_retries_retryable_failures_twice_then_succeeds(self):
        retryable_failures = {
            "HTTP 429": http_error(429),
            "HTTP 500": http_error(500),
            "URL error": urllib.error.URLError("network detail"),
            "timeout": TimeoutError("timeout detail"),
            "invalid envelope JSON": FakeResponse(b"not-json"),
            "missing response": FakeResponse(b'{"success":true,"result":{}}'),
            "false success": FakeResponse(b'{"success":false,"result":{}}'),
            "invalid model JSON": FakeResponse(
                b'{"success":true,"result":{"response":"not-json"}}'
            ),
        }

        for name, failure in retryable_failures.items():
            with self.subTest(name=name):
                sleeps = []
                opener = FakeOpener([failure, failure, success_response()])
                result = CloudflareAIClient(
                    "account-123", TOKEN, opener=opener, sleep_fn=sleeps.append
                ).summarize(STUDY)

                self.assertEqual(result, VALID_SUMMARY)
                self.assertEqual(len(opener.requests), 3)
                self.assertEqual(sleeps, [1.0, 2.0])

    def test_raises_sanitized_error_after_exactly_three_attempts(self):
        full_model_response = "private model response with sensitive source text"
        invalid = FakeResponse(
            json.dumps(
                {"success": True, "result": {"response": full_model_response}}
            ).encode("utf-8")
        )
        sleeps = []
        opener = FakeOpener([invalid, invalid, invalid])

        with self.assertRaises(CloudflareAIError) as raised:
            CloudflareAIClient(
                "account-123", TOKEN, opener=opener, sleep_fn=sleeps.append
            ).summarize(STUDY)

        message = str(raised.exception)
        self.assertIn("after 3 attempts", message)
        self.assertIn("SummaryValidationError", message)
        self.assertNotIn(TOKEN, message)
        self.assertNotIn(full_model_response, message)
        self.assertEqual(len(opener.requests), 3)
        self.assertEqual(sleeps, [1.0, 2.0])

    def test_http_400_fails_immediately_without_leaking_token(self):
        opener = FakeOpener([http_error(400)])
        sleeps = []

        with self.assertRaisesRegex(
            CloudflareAIError, r"^non-retryable HTTP 400$"
        ) as raised:
            CloudflareAIClient(
                "account-123", TOKEN, opener=opener, sleep_fn=sleeps.append
            ).summarize(STUDY)

        self.assertNotIn(TOKEN, str(raised.exception))
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(sleeps, [])


if __name__ == "__main__":
    unittest.main()
