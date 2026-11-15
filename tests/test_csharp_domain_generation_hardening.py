import unittest
from unittest.mock import patch

from services.modernizer.domain_generators.csharp import (
    _gen_csharp_repository,
    _gen_csharp_service,
)
from services.validators import ValidationResult


class CsharpDomainGenerationHardeningTests(unittest.TestCase):
    def _capture_generation_prompt(self, invoke):
        prompts = []

        def generate_validated(prompt, **kwargs):
            prompts.append(prompt)
            path = kwargs["rel_path"]
            return "namespace Demo; public class Generated {}\n", ValidationResult(
                path, "csharp", "compiler", True, [],
            ), 1

        files = {}
        with patch(
            "services.modernizer.validation_orchestration._generate_validated",
            side_effect=generate_validated,
        ):
            self.assertTrue(invoke(files))
        self.assertEqual(1, len(prompts))
        return prompts[0]

    def test_service_prompt_requests_one_compilation_unit(self):
        prompt = self._capture_generation_prompt(lambda files: _gen_csharp_service(
            files, "ModernizedApp/Services/OrdersService", "Orders", "Demo",
            "context", "rules", "source", "model", "system", None, None,
        ))

        self.assertIn("Generate ONE complete C# compilation unit", prompt)
        self.assertIn("one using block before one namespace", prompt)
        self.assertIn("never restart the file", prompt)
        self.assertNotIn("Generate TWO", prompt)
        self.assertNotIn("FILE 1:", prompt)
        self.assertNotIn("FILE 2:", prompt)

    def test_repository_prompt_requests_one_compilation_unit_and_no_project_xml(self):
        prompt = self._capture_generation_prompt(lambda files: _gen_csharp_repository(
            files, "ModernizedApp/Services/OrdersService", "Orders", "Demo",
            "context", "rules", "source", False, "model", "system", None, None,
            {"backend_tech": ".NET 9"}, "mssql",
        ))

        self.assertIn("Generate ONE complete C# compilation unit", prompt)
        self.assertIn("one using block before one namespace", prompt)
        self.assertIn("Do not emit project XML", prompt)
        self.assertIn("do not redeclare it", prompt)
        self.assertNotIn("Generate TWO", prompt)
        self.assertNotIn("FILE 1:", prompt)
        self.assertNotIn("FILE 2:", prompt)


if __name__ == "__main__":
    unittest.main()
