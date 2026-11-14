import unittest
from unittest.mock import patch

from services import llm
from services.requirements_documentation import REQUIREMENTS_PREFERRED_MODELS


class _Response:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "models": [
                {"name": "devstral-small-2:24b"},
                {"name": "deepseek-coder:6.7b"},
            ]
        }


class ModernizationDefaultModelTests(unittest.TestCase):
    def test_oracle_profile_forbids_cross_version_multirow_values_syntax(self):
        oracle = llm.STACK_PROFILES["oracle"]

        self.assertIn("INSERT ALL", oracle)
        self.assertIn("comma-separated row", oracle)
        self.assertIn("SELECT column => value", oracle)
        self.assertIn("Views cannot contain bind variables", oracle)

    def test_devstral_is_the_supported_default_for_all_generation(self):
        self.assertEqual(llm.DEVSTRAL_MODEL, "devstral-small-2:24b")
        self.assertEqual(llm.PREFERRED_MODELS[0], llm.DEVSTRAL_MODEL)
        self.assertEqual(llm.CODEGEN_PREFERRED_MODELS[0], llm.DEVSTRAL_MODEL)
        self.assertEqual(REQUIREMENTS_PREFERRED_MODELS[0], llm.DEVSTRAL_MODEL)

    def test_status_and_generation_select_devstral(self):
        with patch.object(llm._httpx, "get", return_value=_Response()):
            status = llm.check_status()
            selected = llm.pick_codegen_model()

        self.assertEqual(status["recommended"], "devstral-small-2:24b")
        self.assertEqual(status["active_model"], "devstral-small-2:24b")
        self.assertEqual(selected, "devstral-small-2:24b")

    def test_compiler_repair_uses_devstral(self):
        with patch.object(llm, "check_status", return_value={
            "models": ["devstral-small-2:24b", "deepseek-coder:6.7b"],
        }):
            self.assertEqual(llm.pick_compiler_repair_model(), "devstral-small-2:24b")

    def test_fast_single_file_tier_uses_installed_compact_model(self):
        with patch.dict("os.environ", {"OLLAMA_FAST_CODE_MODEL": ""}), patch.object(
            llm, "check_status", return_value={
                "models": ["devstral-small-2:24b", "qwen3.5:9b"],
            },
        ):
            self.assertEqual(
                llm.pick_fast_codegen_model("devstral-small-2:24b"),
                "qwen3.5:9b",
            )

    def test_missing_configured_fast_model_falls_back_without_substitution(self):
        with patch.dict("os.environ", {"OLLAMA_FAST_CODE_MODEL": "approved-fast:7b"}), patch.object(
            llm, "check_status", return_value={"models": ["qwen3.5:9b"]},
        ):
            self.assertEqual(
                llm.pick_fast_codegen_model("devstral-small-2:24b"),
                "devstral-small-2:24b",
            )

    def test_devstral_trailing_fence_and_explanation_are_removed(self):
        source = "public class Demo {}\n```\nThis file defines the class."
        self.assertEqual(llm._strip_code_fences(source), "public class Demo {}")

    def test_fenced_source_is_extracted_when_surrounded_by_prose(self):
        response = "Here is the file:\n```csharp\npublic class Demo {}\n```\nDone."
        self.assertEqual(llm._strip_code_fences(response), "public class Demo {}")


if __name__ == "__main__":
    unittest.main()
