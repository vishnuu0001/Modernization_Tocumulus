# ---------------------------------------------------------------------------
# Author: Vishnuu A
# Scope: Modernization — tests (test_java_repair_round_hardening.py)
# Date: 2026-08-14
# ---------------------------------------------------------------------------
"""Regression coverage for a job that ran without ever reaching closure.

A real Java generation job stalled forever mid-"repairing" phase: a
`ThreadPoolExecutor` batch used `with ThreadPoolExecutor(...) as executor:`
(which joins every submitted thread on exit) together with an unbounded
`as_completed()` wait, and the `generate()` call each worker made had no
wall-clock ceiling of its own. One slow/stuck Ollama call for a single file
was therefore enough to block the entire compiler-repair round — and with
it, the whole job — from ever completing.

These tests exercise `_pf_repair_build_round`, `_pf_repair_java_module_
boundaries`, and `_run_bounded_round` directly with a worker that never
returns, and assert the caller still gets control back within a bounded
time, with the stuck file reported as a (recoverable) failure rather than
the whole call hanging.
"""
import threading
import time
import unittest
from unittest.mock import patch

from services.modernizer._shared import (
    _generation_wave_budget_seconds,
    _round_budget_seconds,
    _run_bounded_round,
)
from services.modernizer.prompt_pipeline import (
    _pf_repair_build_round,
    _pf_repair_java_module_boundaries,
    _pf_repair_strict_prebuild_output,
)
from services.modernizer.conversion_pipeline import (
    _caf_run_parallel_conversion,
    _mp_run_domain_generation,
)
from services.modernizer.validation_orchestration import _generate_validated
from services.validators import ValidationResult


class RunBoundedRoundTests(unittest.TestCase):
    # Function: test_hung_future_does_not_block_the_round
    def test_hung_future_does_not_block_the_round(self):
        from concurrent.futures import ThreadPoolExecutor

        executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="hardening-test")
        try:
            fast = executor.submit(lambda: "ok")
            # Long enough to prove the round didn't wait for it, short enough
            # not to hang pytest's own process exit.
            stuck = executor.submit(lambda: time.sleep(2.0))
            futures = {fast: "fast.txt", stuck: "stuck.txt"}

            started = time.monotonic()
            done, timed_out = _run_bounded_round(
                executor, futures, round_budget_seconds=0.2, label="test round",
            )
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 1.5, "round wait should be bounded near the budget, not the hang")
            self.assertEqual({futures[f] for f in done}, {"fast.txt"})
            self.assertEqual(list(timed_out), ["stuck.txt"])
            self.assertIn("stuck.txt", timed_out)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def test_active_future_can_outlive_round_budget_without_false_abandonment(self):
        from concurrent.futures import ThreadPoolExecutor

        activity = {"active.sql": time.monotonic()}

        def active_worker():
            deadline = time.monotonic() + 0.35
            while time.monotonic() < deadline:
                activity["active.sql"] = time.monotonic()
                time.sleep(0.03)
            return "complete"

        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="activity-test")
        try:
            future = executor.submit(active_worker)
            done, timed_out = _run_bounded_round(
                executor, {future: "active.sql"}, round_budget_seconds=0.1,
                label="active round", activity_by_key=activity,
            )

            self.assertEqual({future}, set(done))
            self.assertEqual({}, timed_out)
            self.assertEqual("complete", future.result())
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def test_java_source_conversion_does_not_wait_forever_for_one_file(self):
        output = {}
        conversion_log = []

        def stuck(_path):
            time.sleep(2.0)
            return "Demo.java", "class Demo {}", {
                "type": "llm_converted", "source": "Demo.java",
            }

        with patch(
            "services.modernizer._shared._round_budget_seconds",
            return_value=0.05,
        ):
            started = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, "Java source conversion"):
                _caf_run_parallel_conversion(
                    ["Demo.java"], stuck, 1, output, conversion_log, [0], 1,
                    threading.Lock(), lambda *_args: None, language="java",
                )
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.5)
        self.assertEqual(output, {}, "a timed-out partial file must never be published")

    def test_java_source_completion_is_reported_before_the_whole_wave_finishes(self):
        output = {}
        conversion_log = []
        progress_times = []
        started = time.monotonic()

        def convert(path):
            if path == "Slow.java":
                time.sleep(0.3)
            return path, f"class {path[:-5]} {{}}", {
                "type": "llm_converted", "source": path,
            }

        _caf_run_parallel_conversion(
            ["Fast.java", "Slow.java"], convert, 2, output, conversion_log, [0], 2,
            threading.Lock(),
            lambda _pct, message: progress_times.append(
                (time.monotonic() - started, message)
            ),
            language="java",
        )

        fast_event = next(elapsed for elapsed, message in progress_times if "Fast.java" in message)
        self.assertLess(fast_event, 0.2, "fast file progress was held until the slow file completed")
        self.assertEqual(set(output), {"Fast.java", "Slow.java"})

    def test_non_java_source_conversion_has_the_same_bounded_wave(self):
        output = {}
        conversion_log = []

        def stuck(_path):
            time.sleep(2.0)
            return "Demo.cs", "class Demo {}", {
                "type": "llm_converted", "source": "Demo.cs",
            }

        with patch(
            "services.modernizer._shared._round_budget_seconds",
            return_value=0.05,
        ):
            started = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, "Csharp source conversion"):
                _caf_run_parallel_conversion(
                    ["Demo.cs"], stuck, 1, output, conversion_log, [0], 1,
                    threading.Lock(), lambda *_args: None, language="csharp",
                )
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.5)
        self.assertEqual(output, {})

    # Function: test_round_budget_scales_with_batches_not_item_count
    def test_round_budget_scales_with_batches_not_item_count(self):
        # 5 items / 2 workers -> 3 sequential batches worst case.
        self.assertAlmostEqual(_round_budget_seconds(5, 2, 100), 3 * 100 + 60)
        # Never smaller than the margin, even with no items.
        self.assertEqual(_round_budget_seconds(0, 2, 100), 60)

    def test_initial_generation_wave_budget_covers_every_queued_batch(self):
        with patch("services.modernizer._shared._WAVE_ROUND_BUDGET_SECONDS", 1800), \
             patch("services.modernizer._shared._FILE_GENERATION_MAX_SECONDS", 600):
            self.assertEqual(_generation_wave_budget_seconds(5, 1, "csharp"), 3060)

    def test_initial_generation_wave_budget_retains_configured_minimum(self):
        with patch("services.modernizer._shared._WAVE_ROUND_BUDGET_SECONDS", 1800), \
             patch("services.modernizer._shared._FILE_GENERATION_MAX_SECONDS", 600):
            self.assertEqual(_generation_wave_budget_seconds(2, 2, "csharp"), 1800)


class RepairBuildRoundHardeningTests(unittest.TestCase):
    def test_java_prebuild_repair_excludes_sql_and_yaml_assets(self):
        output = {
            "Demo/src/main/java/demo/Demo.java": "class Demo {",
            "Demo/Database/schema.sql": "invalid sql",
            "Demo/application.yml": "invalid: [",
        }
        failures = [
            {"path": path, "checker": "compiler" if path.endswith(".java") else "parser",
             "diagnostics": ["invalid"]}
            for path in output
        ]
        captured = []

        def repair(fixable, *_args, **_kwargs):
            captured.append(set(fixable))
            return {}

        with patch(
            "services.modernizer.prompt_pipeline._pf_validate_final_output",
            return_value=({}, failures),
        ), patch(
            "services.modernizer.prompt_pipeline._pf_repair_build_round",
            side_effect=repair,
        ):
            _pf_repair_strict_prebuild_output(
                output, "java", "postgres", "", "", "model", "system",
                lambda *_args: None,
            )

        self.assertEqual([{"Demo/src/main/java/demo/Demo.java"}], captured)

    def test_repair_streams_token_heartbeat(self):
        path = "Demo/src/main/java/demo/Demo.java"
        output = {path: "class Demo {}"}
        events = []

        def generate(_prompt, **kwargs):
            for _ in range(32):
                kwargs["on_token"]("generated-token")
            return "class Demo { void fixed() {} }"

        passed = ValidationResult(path, "java", "compiler", True, [])
        with patch("services.llm.generate", side_effect=generate), patch(
            "services.validators.validate_file", return_value=passed,
        ):
            failures = _pf_repair_build_round(
                {path: ["cannot find symbol"]}, 1, 2, output, "", "",
                "model", "system", lambda *args: events.append(args), "java",
            )

        self.assertEqual({}, failures)
        self.assertTrue(any("tokens generated" in event[2] for event in events))

    def test_total_deadline_prevents_starting_the_full_queued_backlog(self):
        paths = [f"Demo/src/main/java/demo/Type{i}.java" for i in range(5)]
        output = {path: f"class Type{i} {{}}" for i, path in enumerate(paths)}
        calls = []

        def generate(prompt, **_kwargs):
            calls.append(prompt)
            time.sleep(0.08)
            return "class Fixed {}"

        passed = ValidationResult("Fixed.java", "java", "compiler", True, [])
        with patch.dict("os.environ", {"MODERNIZATION_JAVA_REPAIR_WORKERS": "1"}), patch(
            "services.llm.generate", side_effect=generate,
        ), patch(
            "services.validators.validate_file", return_value=passed,
        ):
            failures = _pf_repair_build_round(
                {path: ["cannot find symbol"] for path in paths},
                1, 2, output, "", "", "model", "system",
                lambda *_args: None, "java", deadline=time.monotonic() + 0.14,
            )

        self.assertLess(len(calls), len(paths))
        self.assertTrue(any("total repair deadline" in reason for reason in failures.values()))

    def test_java_repair_cannot_replace_a_class_with_package_only_source(self):
        path = "Demo/src/main/java/com/example/Demo.java"
        original = "package com.example; public class Demo {}"
        output = {path: original}

        with patch(
            "services.llm.generate",
            return_value="package com.example;\n",
        ):
            failures = _pf_repair_build_round(
                {path: ["cannot find symbol"]}, 1, 2, output,
                synthesized_contracts="", namespace_map_text="",
                llm_model="test-model", system="system prompt",
                progress=lambda *_args: None, language="java",
            )

        self.assertIn(path, failures)
        self.assertIn("contains no type declaration", failures[path])
        self.assertEqual(original, output[path])

    # Function: test_one_hung_repair_call_does_not_block_the_others
    def test_one_hung_repair_call_does_not_block_the_others(self):
        fixable = {
            "Demo/Fast.java": ["cannot find symbol: foo"],
            "Demo/Stuck.java": ["cannot find symbol: bar"],
        }
        output = {
            "Demo/Fast.java": "class Fast {}",
            "Demo/Stuck.java": "class Stuck {}",
        }

        # Function: fake_generate
        def fake_generate(prompt, **kwargs):
            # The repair prompt also lists every other project file by name
            # (the "AVAILABLE LOCAL SOURCE FILES" manifest), so match on the
            # precise "FILE PATH: <target>" line instead of a bare substring
            # to make sure only the call actually repairing Stuck.java hangs.
            if "FILE PATH: Demo/Stuck.java" in prompt:
                time.sleep(2.0)  # longer than the test's round budget below
            return "class Fast { void fixed() {} }"

        with patch("services.llm.generate", side_effect=fake_generate), \
             patch("services.modernizer._shared._REPAIR_CALL_MAX_SECONDS", 0.05), \
             patch(
                 "services.modernizer._shared._round_budget_seconds",
                 lambda *a, **k: 0.3,
             ):
            started = time.monotonic()
            failures = _pf_repair_build_round(
                fixable, 1, 2, output,
                synthesized_contracts="", namespace_map_text="",
                llm_model="test-model", system="system prompt",
                progress=lambda *a, **k: None,
            )
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 2.0, "a hung file must not block the whole repair round")
        self.assertIn("Demo/Stuck.java", failures)
        self.assertNotIn("Demo/Fast.java", failures)
        self.assertIn("fixed", output["Demo/Fast.java"])
        # The stuck file's pre-repair content is preserved, not clobbered.
        self.assertEqual(output["Demo/Stuck.java"], "class Stuck {}")


class JavaAggregateGenerationBudgetTests(unittest.TestCase):
    def test_validation_repairs_share_one_java_deadline(self):
        budgets = []
        validation_attempt = [0]

        def fake_generate(_prompt, **kwargs):
            budgets.append(kwargs["max_seconds"])
            time.sleep(0.02)
            return "package demo; class Demo {}"

        def always_new_diagnostic(path, _content, language, dialect_hint=""):
            validation_attempt[0] += 1
            return ValidationResult(
                path, language, "compiler", False,
                [f"failure-{validation_attempt[0]}"],
            )

        with patch("services.llm.generate", side_effect=fake_generate), \
             patch("services.validators.validate_file", side_effect=always_new_diagnostic):
            _content, result, attempts = _generate_validated(
                "generate Demo", model="test-model", system="system",
                max_tokens=128, num_ctx=1024, rel_path="Demo.java",
                language="java", generation_max_seconds=0.5,
            )

        self.assertFalse(result.passed)
        self.assertEqual(attempts, 3)
        self.assertEqual(len(budgets), 3)
        self.assertGreater(budgets[0], budgets[1])
        self.assertGreater(budgets[1], budgets[2])
        self.assertLessEqual(budgets[0], 0.5)

    def test_non_java_validation_retry_timeout_contract_is_unchanged(self):
        budgets = []
        validation_attempt = [0]

        def fake_generate(_prompt, **kwargs):
            budgets.append(kwargs["max_seconds"])
            return "public class Demo {}"

        def always_new_diagnostic(path, _content, language, dialect_hint=""):
            validation_attempt[0] += 1
            return ValidationResult(
                path, language, "compiler", False,
                [f"failure-{validation_attempt[0]}"],
            )

        with patch("services.llm.generate", side_effect=fake_generate), \
             patch("services.validators.validate_file", side_effect=always_new_diagnostic):
            _generate_validated(
                "generate Demo", model="test-model", system="system",
                max_tokens=128, num_ctx=1024, rel_path="Demo.cs",
                language="csharp", generation_max_seconds=0.5,
            )

        self.assertEqual(budgets, [0.5, 0.5, 0.5])


class JavaDomainContentionTests(unittest.TestCase):
    def test_domain_generation_round_is_bounded_by_all_sequential_calls(self):
        observed = []

        def fake_domain(name, *_args, **_kwargs):
            return {f"{name}.java": f"class {name} {{}}"}

        def bounded(executor, futures, *, round_budget_seconds, label):
            observed.append((round_budget_seconds, label))
            done = set(futures)
            for future in done:
                future.result()
            executor.shutdown(wait=True)
            return done, {}

        with patch(
            "services.modernizer.conversion_pipeline._mp_gen_one_domain",
            side_effect=fake_domain,
        ), patch(
            "services.modernizer._shared._JAVA_FILE_GENERATION_MAX_SECONDS", 10,
        ), patch(
            "services.modernizer._shared._run_bounded_round", side_effect=bounded,
        ), patch.dict("os.environ", {"MODERNIZATION_JAVA_DOM_WORKERS": "2"}):
            _mp_run_domain_generation(
                ["orders", "billing", "customer"], True, "test-model",
                {}, {}, "demo", [], "", "java", "spring_boot", {},
                lambda *_args: None, None,
            )

        # Java uses two sequential LLM calls per domain. Three domains over
        # two workers means two batches: 2 * (2 * 10s) + 60s margin.
        self.assertEqual([(100.0, "Domain generation round")], observed)

    def test_java_domains_inherit_the_shared_async_batch_default(self):
        active = 0
        max_active = 0
        lock = threading.Lock()

        def fake_domain(name, *_args, **_kwargs):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.025)
            with lock:
                active -= 1
            return {f"{name}.java": f"class {name} {{}}"}

        with patch(
            "services.modernizer.conversion_pipeline._mp_gen_one_domain",
            side_effect=fake_domain,
        ), patch.dict("os.environ", {"MODERNIZATION_JAVA_DOM_WORKERS": "2"}, clear=False):
            import os
            previous = os.environ.get("MODERNIZATION_JAVA_DOM_WORKERS")
            try:
                _mp_run_domain_generation(
                    ["orders", "billing", "customer"], True, "test-model",
                    {}, {}, "demo", [], "", "java", "spring_boot", {},
                    lambda *_args: None, None,
                )
            finally:
                if previous is not None:
                    os.environ["MODERNIZATION_JAVA_DOM_WORKERS"] = previous

        self.assertEqual(max_active, 2)


class BoundaryRepairHardeningTests(unittest.TestCase):
    # Function: test_empty_llm_response_does_not_crash_the_whole_repair
    def test_empty_llm_response_does_not_crash_the_whole_repair(self):
        """A single unparsable/empty repair response used to propagate
        uncaught out of the whole function (no try/except around
        future.result()), aborting the entire generation job over one bad
        response for one file. It must instead be recorded and skipped."""
        output = {
            "service-a/src/main/java/com/a/A.java": (
                "package com.a;\nimport com.b.B;\nclass A { B b; }\n"
            ),
            "service-b/src/main/java/com/b/B.java": "package com.b;\nclass B {}\n",
        }

        with patch("services.llm.generate", return_value=""), \
             patch("services.modernizer._shared._REPAIR_CALL_MAX_SECONDS", 5):
            # Must return normally (0 successful repairs), not raise.
            repaired = _pf_repair_java_module_boundaries(
                output, "test-model", "system prompt", progress=lambda *a, **k: None,
            )
        self.assertEqual(repaired, 0)


if __name__ == "__main__":
    unittest.main()
