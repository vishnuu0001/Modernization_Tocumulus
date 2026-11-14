import json
import time
import unittest
from unittest.mock import patch

from services import llm


class _StreamResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self):
        return None

    def iter_lines(self):
        yield json.dumps({"response": "backend/Program.cs", "done": True})


class _LengthLimitedStreamResponse(_StreamResponse):
    def iter_lines(self):
        yield json.dumps({"response": "public class Demo {", "done": False})
        yield json.dumps({"response": "", "done": True, "done_reason": "length"})


class _RetryProgress:
    def __init__(self):
        self.retries = []

    def __call__(self, _token):
        return None

    def on_retry(self, attempt, total, error):
        self.retries.append((attempt, total, error))


class LlmReasoningControlTests(unittest.TestCase):
    def test_generate_rejects_an_explicit_token_limit_cutoff(self):
        with self.assertRaisesRegex(llm.GenerationTruncatedError, "truncated at the configured token limit") as raised:
            llm._read_generation_response(
                _LengthLimitedStreamResponse(), time.monotonic(), None, None,
            )
        self.assertEqual("public class Demo {", raised.exception.partial_text)

    @patch.object(llm, "_stream_generate_tokens")
    def test_generate_continues_and_deduplicates_a_length_cutoff(self, stream):
        stream.side_effect = [
            llm.GenerationTruncatedError("public class Demo {\n// repeated seam"),
            "// repeated seam\n}\n// <<<END_OF_FILE>>>",
        ]

        result = llm.generate("write Demo", model="devstral-small-2:24b", max_tokens=2048)

        self.assertEqual(
            "public class Demo {\n// repeated seam\n}\n// <<<END_OF_FILE>>>", result,
        )
        continuation_payload = stream.call_args_list[1].args[0]
        self.assertIn("CUT-OFF OUTPUT TAIL", continuation_payload["prompt"])
        self.assertEqual(32768, continuation_payload["options"]["num_ctx"])

    def test_continuation_restart_is_stitched_at_repeated_inner_seam(self):
        previous = (
            "INSERT INTO transactions (txn_id, account_id, amount)\n"
            "VALUES (9, 3"
        )
        continuation = (
            "INSERT INTO transactions (txn_id, account_id, amount)\n"
            "VALUES (9, 3, 100.00);\n"
        )

        stitched = llm._stitch_generation_chunks(previous, continuation)

        self.assertEqual(
            "INSERT INTO transactions (txn_id, account_id, amount)\n"
            "VALUES (9, 3, 100.00);\n",
            stitched,
        )
        self.assertNotIn("3INSERT", stitched)

    def test_java_continuation_restart_replaces_incomplete_compilation_unit(self):
        previous = (
            "package com.example;\n\n"
            "public class Demo {\n"
            "    void run() { throw new IllegalStateException(\"cut off\");"
        )
        continuation = (
            "package com.example;\n\n"
            "public class Demo {\n"
            "    void run() { }\n"
            "}\n"
        )

        stitched = llm._stitch_generation_chunks(previous, continuation)

        self.assertEqual(continuation, stitched)
        self.assertEqual(1, stitched.count("package com.example;"))

    @patch.object(llm, "_httpx")
    def test_generate_disables_reasoning_by_default(self, httpx):
        httpx.stream.return_value = _StreamResponse()
        self.assertEqual("backend/Program.cs", llm.generate("plan", model="qwen3.5:9b"))
        payload = httpx.stream.call_args.kwargs["json"]
        self.assertIs(payload["think"], False)

    @patch.object(llm, "_httpx")
    def test_generate_allows_explicit_reasoning_opt_in(self, httpx):
        httpx.stream.return_value = _StreamResponse()
        llm.generate("analyze", model="qwen3.5:9b", think=True)
        payload = httpx.stream.call_args.kwargs["json"]
        self.assertIs(payload["think"], True)

    @patch.object(llm, "_httpx")
    def test_legacy_none_value_still_disables_reasoning(self, httpx):
        httpx.stream.return_value = _StreamResponse()
        llm.generate("generate", model="qwen3.5:9b", think=None)
        payload = httpx.stream.call_args.kwargs["json"]
        self.assertIs(payload["think"], False)

    @patch.object(llm, "_httpx")
    def test_generate_sends_call_specific_stop_sequences_to_ollama(self, httpx):
        httpx.stream.return_value = _StreamResponse()
        llm.generate("plan", model="qwen3.5:9b", stop=("END_OF_PLAN",))
        payload = httpx.stream.call_args.kwargs["json"]
        self.assertEqual(["END_OF_PLAN"], payload["options"]["stop"])

    @patch.object(llm.time, "sleep")
    @patch.object(llm, "_stream_generation_attempt")
    def test_transient_retry_is_reported_to_progress_callback(self, attempt, _sleep):
        attempt.side_effect = [llm._httpx.ReadTimeout("timed out"), "complete"]
        progress = _RetryProgress()

        self.assertEqual("complete", llm._stream_generate_tokens({}, progress, 30))
        self.assertEqual([(2, 3, "timed out")], progress.retries)


if __name__ == "__main__":
    unittest.main()
