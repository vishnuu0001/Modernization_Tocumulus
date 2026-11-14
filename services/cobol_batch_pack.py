"""Deterministic, compiler-and-runtime-proven COBOL batch project generation."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from collections.abc import Mapping

from services.tool_discovery import executable_environment, find_executable


_TEMPLATE_ROOT = (
    Path(__file__).resolve().parents[1]
    / "config" / "generation" / "templates" / "cobol-batch"
)


def is_cobol_batch_pack_request(request: str, language: str) -> bool:
    text = (request or "").casefold()
    signals = ("batch", "copybook", "jcl", "sample input", "sample output", "control total")
    return language.casefold() == "cobol" and "batch" in text and sum(token in text for token in signals) >= 2


def generate_cobol_batch_pack(project_name: str) -> dict[str, str]:
    if not _TEMPLATE_ROOT.is_dir():
        raise RuntimeError(f"COBOL batch template is missing: {_TEMPLATE_ROOT}")
    output: dict[str, str] = {}
    for path in sorted(_TEMPLATE_ROOT.rglob("*")):
        if path.is_file():
            relative = path.relative_to(_TEMPLATE_ROOT).as_posix()
            output[f"{project_name}/{relative}"] = path.read_text(encoding="utf-8")
    return output


def _cobol_executable() -> str:
    candidates = (
        find_executable("cobc"), shutil.which("cobc"),
        r"C:\msys64\ucrt64\bin\cobc.exe",
    )
    executable = next((value for value in candidates if value and Path(value).is_file()), None)
    if not executable:
        raise RuntimeError("GnuCOBOL compiler cobc is required for the COBOL batch evaluation")
    return executable


def validate_cobol_batch_pack(output: Mapping[str, str], project_name: str) -> dict[str, object]:
    prefix = f"{project_name}/"
    required = {
        "src/BATCHPROC.cob", "src/TRANREC.cpy", "src/RPTREC.cpy",
        "jcl/BATCHPROC.jcl", "sample-input.dat", "expected-output.dat", "README.md",
    }
    relative_files = {path.removeprefix(prefix) for path in output if path.startswith(prefix)}
    missing = sorted(required - relative_files)
    if missing:
        raise RuntimeError("COBOL batch pack lacks: " + ", ".join(missing))

    executable = _cobol_executable()
    with tempfile.TemporaryDirectory(prefix="modernization_cobol_eval_") as directory:
        root = Path(directory)
        for path, content in output.items():
            if not path.startswith(prefix):
                continue
            target = root / path.removeprefix(prefix)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
        source_dir = root / "src"
        shutil.copy2(root / "sample-input.dat", source_dir / "sample-input.dat")
        binary = source_dir / ("BATCHPROC.exe" if os.name == "nt" else "BATCHPROC")
        environment = executable_environment(executable)
        compile_process = subprocess.run(
            [
                executable, "-x", "-std=ibm", "-fformat=fixed", "-Wcolumn-overflow",
                "-I", str(source_dir), str(source_dir / "BATCHPROC.cob"),
                "-o", str(binary),
            ],
            cwd=str(source_dir), env=environment, capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace",
        )
        if compile_process.returncode:
            combined = (compile_process.stdout + "\n" + compile_process.stderr).strip()
            raise RuntimeError("GnuCOBOL build failed: " + combined[-4000:])
        run_process = subprocess.run(
            [str(binary)], cwd=str(source_dir), env=environment,
            capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
        )
        if run_process.returncode:
            combined = (run_process.stdout + "\n" + run_process.stderr).strip()
            raise RuntimeError(f"COBOL sample execution failed ({run_process.returncode}): {combined[-2000:]}")
        actual = (source_dir / "sample-output.dat").read_text(encoding="utf-8").replace("\r\n", "\n")
        expected = (root / "expected-output.dat").read_text(encoding="utf-8").replace("\r\n", "\n")
        if actual != expected:
            raise RuntimeError("COBOL golden output does not match expected-output.dat")
    return {
        "passed": True, "checker": "gnucobol-build-and-golden-run",
        "compiler": executable, "program": "BATCHPROC", "exit_code": 0,
    }
