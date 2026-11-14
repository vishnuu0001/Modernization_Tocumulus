"""Fail-fast Python runtime policy for the production application.

Python 3.15 adds useful opt-in execution modes, but enabling them globally can
move import failures, change extension-module behavior, or expose shared state
to true parallel execution.  Modernization keeps the established eager-import,
GIL-enabled, non-JIT contract until each alternate mode is qualified separately.
"""
from __future__ import annotations

import sys
import sysconfig
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


# MappingProxyType retains the familiar Mapping contract without exposing the
# new frozendict concrete type to JSON, Pydantic, or isinstance(..., dict) code.
SAFE_RUNTIME_ENV: Mapping[str, str] = MappingProxyType({
    "PYTHON_LAZY_IMPORTS": "normal",
    "PYTHON_JIT": "0",
    "PYTHON_GIL": "1",
})

# A private identity marker is sufficient for an in-process capability probe.
# The Python 3.15 sentinel type is unnecessary here and would add global naming
# and pickling semantics to a value that must never cross a process boundary.
_UNAVAILABLE = object()


@dataclass(frozen=True, slots=True)
class PythonRuntimeProfile:
    version: str
    lazy_imports: str
    gil_enabled: bool | None
    free_threaded_build: bool
    jit_available: bool
    jit_enabled: bool

    def public_dict(self) -> dict[str, str | bool | None]:
        """Return a JSON-safe copy without exposing immutable internals."""
        return {
            "version": self.version,
            "lazy_imports": self.lazy_imports,
            "gil_enabled": self.gil_enabled,
            "free_threaded_build": self.free_threaded_build,
            "jit_available": self.jit_available,
            "jit_enabled": self.jit_enabled,
        }


def _optional_bool(owner: object, attribute: str) -> bool | None:
    value = getattr(owner, attribute, _UNAVAILABLE)
    if value is _UNAVAILABLE:
        return None
    return bool(value() if callable(value) else value)


def inspect_python_runtime() -> PythonRuntimeProfile:
    """Inspect only documented flags plus guarded CPython capabilities."""
    lazy_probe = getattr(sys, "get_lazy_imports", _UNAVAILABLE)
    lazy_imports = "normal" if lazy_probe is _UNAVAILABLE else str(lazy_probe())
    gil_enabled = _optional_bool(sys, "_is_gil_enabled")
    free_threaded_build = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))

    jit = getattr(sys, "_jit", _UNAVAILABLE)
    jit_available = False
    jit_enabled = False
    if jit is not _UNAVAILABLE:
        jit_available = bool(jit.is_available())
        jit_enabled = bool(jit.is_enabled())

    return PythonRuntimeProfile(
        version=sys.version.split()[0],
        lazy_imports=lazy_imports,
        gil_enabled=gil_enabled,
        free_threaded_build=free_threaded_build,
        jit_available=jit_available,
        jit_enabled=jit_enabled,
    )


def enforce_safe_python_runtime(
    profile: PythonRuntimeProfile | None = None,
) -> PythonRuntimeProfile:
    """Reject execution modes not yet qualified for the application."""
    current = profile or inspect_python_runtime()
    violations: list[str] = []
    if sys.version_info < (3, 15, 2):
        violations.append("Python 3.15.2 or newer is required")
    if current.lazy_imports != "normal":
        violations.append("global lazy imports must be disabled")
    if current.free_threaded_build:
        violations.append("a standard GIL build is required")
    if current.gil_enabled is False:
        violations.append("the GIL must be enabled")
    if current.jit_enabled:
        violations.append("the experimental JIT must be disabled")
    if violations:
        raise RuntimeError("Unsafe Python runtime: " + "; ".join(violations))
    return current
