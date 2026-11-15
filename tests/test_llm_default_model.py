import unittest
from unittest.mock import patch

from services import llm
from services.modernizer.prompt_pipeline import _prompt_readme, _readme


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

    def test_devstral_remains_the_installation_recommendation(self):
        self.assertEqual(llm.DEVSTRAL_MODEL, "devstral-small-2:24b")
        self.assertEqual(llm.PREFERRED_MODELS[0], llm.DEVSTRAL_MODEL)
        self.assertEqual(llm.CODEGEN_PREFERRED_MODELS[0], llm.DEVSTRAL_MODEL)

    def test_status_and_generation_select_devstral(self):
        with patch.object(llm._httpx, "get", return_value=_Response()):
            status = llm.check_status()
            selected = llm.pick_codegen_model()

        self.assertEqual(status["recommended"], "devstral-small-2:24b")
        self.assertEqual(status["active_model"], "devstral-small-2:24b")
        self.assertEqual(selected, "devstral-small-2:24b")

    def test_compiler_repair_uses_top_level_selected_model(self):
        with patch.object(llm, "check_status", return_value={
            "models": ["devstral-small-2:24b", "deepseek-coder:6.7b"],
            "active_model": "deepseek-coder:6.7b",
            "runtime": {"selected_model": "deepseek-coder:6.7b"},
        }):
            self.assertEqual(llm.pick_compiler_repair_model(), "deepseek-coder:6.7b")

    def test_fast_single_file_tier_uses_top_level_selected_model(self):
        with patch.object(
            llm, "check_status", return_value={
                "models": ["devstral-small-2:24b", "qwen3.5:9b"],
                "active_model": "devstral-small-2:24b",
                "runtime": {"selected_model": "devstral-small-2:24b"},
            },
        ):
            self.assertEqual(
                llm.pick_fast_codegen_model("devstral-small-2:24b"),
                "devstral-small-2:24b",
            )

    def test_fast_model_environment_override_cannot_bypass_top_selection(self):
        with patch.dict("os.environ", {"OLLAMA_FAST_CODE_MODEL": "approved-fast:7b"}), patch.object(
            llm, "check_status", return_value={
                "models": ["qwen3.5:9b", "approved-fast:7b"],
                "active_model": "qwen3.5:9b",
                "runtime": {"selected_model": "qwen3.5:9b"},
            },
        ):
            self.assertEqual(
                llm.pick_fast_codegen_model("devstral-small-2:24b"),
                "qwen3.5:9b",
            )

    def test_generated_readmes_report_the_captured_runtime_model(self):
        target = {
            "name": "Python API", "frontend_tech": "None",
            "backend_tech": "FastAPI", "db_tech": "PostgreSQL",
        }
        prompt_readme = _prompt_readme(
            "Create an API", target, "Demo", 0, "qwen3.5:9b",
        )
        modernization_readme = _readme(
            {"architecture": {}}, "Demo", target, llm_model="qwen3.5:9b",
        )

        self.assertIn("`qwen3.5:9b`", prompt_readme)
        self.assertIn("`qwen3.5:9b`", modernization_readme)
        self.assertNotIn("Devstral", prompt_readme)
        self.assertNotIn("Devstral", modernization_readme)

    def test_devstral_trailing_fence_and_explanation_are_removed(self):
        source = "public class Demo {}\n```\nThis file defines the class."
        self.assertEqual(llm._strip_code_fences(source), "public class Demo {}")

    def test_fenced_source_is_extracted_when_surrounded_by_prose(self):
        response = "Here is the file:\n```csharp\npublic class Demo {}\n```\nDone."
        self.assertEqual(llm._strip_code_fences(response), "public class Demo {}")


if __name__ == "__main__":
    unittest.main()
