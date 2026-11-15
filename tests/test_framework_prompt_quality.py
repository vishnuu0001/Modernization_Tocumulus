import json
import unittest
from unittest.mock import patch

from services.modernizer.domain_generators import dispatch
from services.modernizer.domain_generators.typescript import (
    _api_service_prompt,
    _framework_file_paths,
    _ui_component_prompt,
)


class FrameworkPromptQualityTests(unittest.TestCase):
    def test_domain_table_routing_never_falls_back_to_unrelated_tables(self):
        self.assertEqual(
            ["APP_USERS"],
            dispatch._select_domain_tables("Auth", ["HISTORY", "TRAINS", "APP_USERS"]),
        )
        self.assertEqual(
            [], dispatch._select_domain_tables("Inventory", ["HISTORY", "TRAINS"]),
        )

    def test_react_prompt_bounds_component_size_and_forbids_mock_data(self):
        prompt = _ui_component_prompt(
            "React", "Shashi", {"frontend_tech": "React 18"},
            "context", "rules", "SOURCE EVIDENCE: train search", "guide", [],
        )

        self.assertIn("prefer under 350 lines", prompt)
        self.assertIn("never embed mock/demo datasets", prompt)
        self.assertIn("sibling API module", prompt)

    def test_angular_prompt_is_framework_native_and_source_grounded(self):
        target = {"frontend_tech": "Angular 20"}
        prompt = _ui_component_prompt(
            "Angular", "Sample_dotnet6", target, "context", "rules",
            "SOURCE EVIDENCE: Hello(bool isHappy)", "guide", [],
        )
        api_prompt = _api_service_prompt(
            "Angular", "Sample_dotnet6", "context", "rules",
            "SOURCE EVIDENCE: Hello(bool isHappy)",
        )

        self.assertIn("Angular 20", prompt)
        self.assertIn("standalone: true", prompt)
        self.assertIn("signals/computed", prompt)
        self.assertIn("inject(TOKEN) field initializers", prompt)
        self.assertIn("do not use constructor parameter decorators", prompt)
        self.assertIn("Hello(bool isHappy)", prompt)
        self.assertIn("Do not invent business fields", prompt)
        self.assertNotIn("useEffect", prompt)
        self.assertNotIn("JSX", api_prompt)
        self.assertIn("inject(HttpClient)", api_prompt)
        self.assertIn("Do not use fetch", api_prompt)

    def test_angular_files_land_in_the_real_workspace(self):
        component, service = _framework_file_paths(
            "ModernizedApp/src/components/Sample_dotnet6", "Sample_dotnet6", "Angular",
        )
        self.assertEqual(
            "ModernizedApp/frontend/src/app/features/sample-dotnet6/sample-dotnet6.component.ts",
            component,
        )
        self.assertEqual(
            "ModernizedApp/frontend/src/app/features/sample-dotnet6/sample-dotnet6.api.ts",
            service,
        )

    def test_full_stack_frontend_receives_angular_system_profile(self):
        captured = {}

        def generate_frontend(*args, **_kwargs):
            captured["system"] = args[9]
            return set()

        target = {
            "name": ".NET 9 + Angular 20",
            "language": "csharp",
            "backend_tech": ".NET 9",
            "frontend_tech": "Angular 20",
            "db_target": "mssql",
        }
        with patch.object(dispatch, "_detect_frontend_framework", return_value="Angular"), patch(
            "services.modernizer.domain_generators.typescript._llm_domain_typescript",
            side_effect=generate_frontend,
        ):
            dispatch._maybe_generate_frontend(
                "csharp", target, {}, "Sample", "Demo", [], "context", "rules",
                "source", "guide", "model", "backend-system", None, None, None,
            )

        self.assertIn("Angular", captured["system"])
        self.assertNotEqual("backend-system", captured["system"])

    def test_csharp_domain_does_not_reauthor_deterministic_scaffolds(self):
        target = {
            "id": "dotnet9-angular",
            "name": ".NET 9 + Angular 20",
            "language": "csharp",
            "backend_tech": ".NET 9",
            "frontend_tech": "Angular 20",
        }
        analysis = {"folder_path": "", "architecture": {}, "metrics": {}, "antipatterns": []}

        with patch(
            "services.modernizer.conversion_pipeline._load_dom_cache", return_value=None,
        ), patch(
            "services.modernizer.conversion_pipeline._save_dom_cache",
        ), patch.object(
            dispatch, "_dispatch_backend_generation", return_value=set(),
        ), patch.object(
            dispatch, "_maybe_generate_frontend", return_value=set(),
        ), patch.object(
            dispatch, "_ollama_generate_all_sources",
        ) as blanket_generation:
            files = dispatch._llm_gen_domain(
                "Sample", target, analysis, "Demo", [], model="test-model",
            )

        blanket_generation.assert_not_called()
        provenance = json.loads(
            files["ModernizedApp/.strat-aqorynth/ollama-sample-provenance.json"]
        )
        self.assertEqual([], provenance["source_files"])


if __name__ == "__main__":
    unittest.main()
