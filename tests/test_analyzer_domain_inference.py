import tempfile
import unittest
from pathlib import Path

from services.analyzer import _infer_domains


class AnalyzerDomainInferenceTests(unittest.TestCase):
    def test_deployment_path_segments_do_not_become_business_domains(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "app" / "data" / "projects" / "APP-001" / "source" / "v001" / "sample"
            files = []
            for project in ("Sample.DotNet6.Api", "Sample.DotNet6.Api.Core", "Sample.DotNet6.Domain"):
                path = root / "src" / project / "Example.cs"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("namespace Sample.DotNet6; class Example {}", encoding="utf-8")
                files.append(path)

            domains = _infer_domains(files)

        self.assertEqual(["sample_dotnet6"], list(domains))
        self.assertNotIn("data", domains)
        self.assertNotIn("v001", domains)


if __name__ == "__main__":
    unittest.main()
