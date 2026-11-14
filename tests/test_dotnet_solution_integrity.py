import shutil
import subprocess
import tempfile
from pathlib import Path

from services.build_runner import _dotnet_solution_integrity_error
from services.modernizer.build_artifacts import (
    _csharp_llm_domain_contract_complete,
    _reconcile_csharp_generation_output,
    _reconcile_dotnet_solution_files,
)
from services.modernizer.conversion_pipeline import _mp_generate_build_files
from services.modernizer.scaffolds.csharp import _solution_file
from services.modernizer.target_config import TARGET_STACKS


def _materialize(output: dict[str, str], root: Path) -> None:
    for relative, content in output.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def test_incomplete_llm_domain_is_replaced_by_closed_scaffold():
    base = "ModernizedApp/Services/OrdersService"
    output = {
        f"{base}/OrdersService.csproj": (
            '<Project Sdk="Microsoft.NET.Sdk.Web"><PropertyGroup>'
            '<TargetFramework>net9.0</TargetFramework>'
            '<RootNamespace>Demo.OrdersService</RootNamespace>'
            '</PropertyGroup><ItemGroup>'
            '<PackageReference Include="Microsoft.EntityFrameworkCore.SqlServer" Version="9.0.0" />'
            '</ItemGroup></Project>'
        ),
        f"{base}/Endpoints/OrdersEndpoints.cs": (
            "public static class OrdersEndpoints {}\n"
            "public record OrdersCreateRequest(string Name);\n"
            "public record OrdersUpdateRequest(string Name);\n"
            "public record OrdersResponse(int Id, string Name);\n"
        ),
        f"{base}/Services/OrdersServiceImpl.cs": (
            "public interface IOrdersService {}\npublic record PagedResult<T>(T Items);\n"
        ),
        f"{base}/Repositories/OrdersRepository.cs": "public interface IOrdersRepository {}\n",
    }

    changed = _reconcile_csharp_generation_output(output)

    assert changed
    assert f"{base}/Endpoints/OrdersEndpoints.cs" not in output
    assert f"{base}/Services/OrdersServiceImpl.cs" not in output
    assert f"{base}/Models/Order.cs" in output
    assert f"{base}/Repositories/OrderRepository.cs" in output
    assert f"{base}/Services/OrdersService.cs" in output
    assert "<TargetFramework>net9.0</TargetFramework>" in output[f"{base}/OrdersService.csproj"]
    assert "using Demo.OrdersService.Models;" in output[f"{base}/Program.cs"]


def test_complete_llm_domain_contract_is_preserved():
    base = "ModernizedApp/Services/OrdersService"
    output = {
        f"{base}/OrdersService.csproj": (
            '<Project Sdk="Microsoft.NET.Sdk.Web"><PropertyGroup>'
            '<TargetFramework>net9.0</TargetFramework>'
            '<RootNamespace>Demo.OrdersService</RootNamespace>'
            '</PropertyGroup></Project>'
        ),
        f"{base}/Endpoints/OrdersEndpoints.cs": (
            "public static class OrdersEndpoints {}\n"
            "public record OrdersCreateRequest(string Name);\n"
            "public record OrdersUpdateRequest(string Name);\n"
            "public record OrdersResponse(int Id, string Name);\n"
        ),
        f"{base}/Services/OrdersServiceImpl.cs": (
            "public interface IOrdersService {}\n"
            "public class OrdersServiceImpl {}\n"
            "public record PagedResult<T>(T Items);\n"
        ),
        f"{base}/Repositories/OrdersRepository.cs": (
            "public interface IOrdersRepository {}\n"
            "public class OrdersRepository {}\n"
            "public class OrdersEntity {}\n"
            "public class DbContext {}\n"
        ),
    }

    assert _csharp_llm_domain_contract_complete(output, base, "Orders", is_dapper=False)
    before = dict(output)
    assert _reconcile_csharp_generation_output(output) == set()
    assert output == before


def test_conversion_solution_contains_restorable_global_configuration():
    solution = _solution_file("Sample", ["sample_dotnet6"])

    assert '"Services\\Sample_dotnet6Service\\Sample_dotnet6Service.csproj"' in solution
    assert '"ApiGateway\\ApiGateway.csproj"' in solution
    assert "GlobalSection(SolutionConfigurationPlatforms)" in solution
    assert "GlobalSection(ProjectConfigurationPlatforms)" in solution
    assert solution.rstrip().endswith("EndGlobal")


def test_solution_reconciliation_uses_only_actual_project_manifests():
    output = {
        "ModernizedApp/Sample.sln": (
            "Microsoft Visual Studio Solution File, Format Version 12.00\n"
            'Project("{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}") = '
            '"Ghost", "Ghost/Ghost.csproj", "{11111111-1111-1111-1111-111111111111}"\n'
            "EndProject\n"
        ),
        "ModernizedApp/Services/Core/Core.csproj": "<Project Sdk=\"Microsoft.NET.Sdk\" />\n",
        "ModernizedApp/ApiGateway/ApiGateway.csproj": "<Project Sdk=\"Microsoft.NET.Sdk\" />\n",
    }

    changed = _reconcile_dotnet_solution_files(output)

    assert changed == {"ModernizedApp/Sample.sln"}
    solution = output["ModernizedApp/Sample.sln"]
    assert "Ghost.csproj" not in solution
    assert "Services\\Core\\Core.csproj" in solution
    assert "ApiGateway\\ApiGateway.csproj" in solution

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _materialize(output, root)
        assert _dotnet_solution_integrity_error(
            root / "ModernizedApp" / "Sample.sln", root,
        ) is None


def test_build_preflight_rejects_empty_and_missing_project_solutions():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        solution = root / "Empty.sln"
        solution.write_text(
            "Microsoft Visual Studio Solution File, Format Version 12.00\nGlobal\nEndGlobal\n",
            encoding="utf-8",
        )
        assert "contains no .csproj entries" in _dotnet_solution_integrity_error(solution, root)

        solution.write_text(
            _solution_file("Demo", ["missing"]), encoding="utf-8",
        )
        assert "references missing project(s)" in _dotnet_solution_integrity_error(solution, root)


def test_every_supported_csharp_stack_reconciles_to_existing_projects():
    csharp_stacks = {
        stack_id: target for stack_id, target in TARGET_STACKS.items()
        if target.get("language") == "csharp"
    }
    assert csharp_stacks

    for stack_id, target in csharp_stacks.items():
        output = _mp_generate_build_files(
            "csharp", stack_id, "Sample", ["sample_dotnet6"],
            target.get("backend_tech", ""),
        )
        output.update({
            "ModernizedApp/Services/Sample_dotnet6Service/Sample_dotnet6Service.csproj": (
                "<Project Sdk=\"Microsoft.NET.Sdk\" />\n"
            ),
            "ModernizedApp/ApiGateway/ApiGateway.csproj": (
                "<Project Sdk=\"Microsoft.NET.Sdk\" />\n"
            ),
        })
        _reconcile_dotnet_solution_files(output)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _materialize(output, root)
            solution = root / "ModernizedApp" / "Sample.sln"
            assert _dotnet_solution_integrity_error(solution, root) is None, stack_id


def test_generated_solution_is_restorable_by_real_dotnet_cli():
    dotnet = shutil.which("dotnet")
    if not dotnet:
        return

    output = {
        "ModernizedApp/Sample.sln": _solution_file("Sample", ["sample_dotnet6"]),
        "ModernizedApp/Services/Sample_dotnet6Service/Sample_dotnet6Service.csproj": (
            '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
            '<TargetFramework>net8.0</TargetFramework>'
            '</PropertyGroup></Project>\n'
        ),
        "ModernizedApp/ApiGateway/ApiGateway.csproj": (
            '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
            '<TargetFramework>net8.0</TargetFramework>'
            '</PropertyGroup></Project>\n'
        ),
    }
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _materialize(output, root)
        result = subprocess.run(
            [dotnet, "build", str(root / "ModernizedApp" / "Sample.sln"),
             "--nologo", "--verbosity", "quiet"],
            capture_output=True, text=True, timeout=60,
        )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Unable to find a project to restore" not in result.stdout + result.stderr
