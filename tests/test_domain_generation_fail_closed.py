import unittest
from unittest.mock import patch

from services.modernizer.conversion_pipeline import _mp_run_domain_generation
from services.modernizer.domain_generators.go import _llm_domain_go
from services.modernizer.domain_generators.python_gen import _llm_domain_python
from services.validators import ValidationResult


class DomainGenerationFailClosedTests(unittest.TestCase):
    def test_python_validation_failure_does_not_create_placeholder_source(self):
        files = {}
        failure = ValidationResult("ModernizedApp/app/orders/models.py", "python", "compiler", False, ["invalid syntax"])
        with patch(
            "services.modernizer.validation_orchestration._generate_validated",
            return_value=("def broken(:\n", failure, 3),
        ):
            with self.assertRaisesRegex(RuntimeError, "Python model generation failed"):
                _llm_domain_python(
                    files, "Orders", "Demo", [], "context", "rules", "model", "system",
                    [], None, lambda *_args, **_kwargs: "", None,
                )
        self.assertEqual({}, files)

    def test_go_inference_exception_does_not_create_placeholder_source(self):
        files = {}
        with patch(
            "services.modernizer.validation_orchestration._generate_validated",
            side_effect=TimeoutError("model stalled"),
        ):
            with self.assertRaisesRegex(RuntimeError, "Go repository generation failed"):
                _llm_domain_go(
                    files, "Orders", "Demo", [], "context", "rules", "Go", "model",
                    "system", [], None, lambda *_args, **_kwargs: "", None,
                )
        self.assertEqual({}, files)

    def test_non_java_domain_worker_failure_aborts_the_generation_stage(self):
        with patch(
            "services.modernizer.conversion_pipeline._mp_gen_one_domain",
            side_effect=RuntimeError("strict generation failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "Python domain generation"):
                _mp_run_domain_generation(
                    ["orders"], True, "model", {}, {}, "Demo", [], "", "python",
                    "python_fastapi", {}, lambda *_args: None, None,
                )


if __name__ == "__main__":
    unittest.main()
