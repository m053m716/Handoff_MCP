"""Deterministic MyoHID validation suites exposed through the MCP server."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from mcp.lifecycle import ToolExecutionError


MYOHID_PROJECT_RELATIVE = Path("MyoHID") / "android"
SUITE_TIMEOUT_SECONDS = 30 * 60
MAX_DIAGNOSTIC_CHARS = 4_000

_SUITE_TASKS: Mapping[str, tuple[str, ...]] = {
    "debug": (
        ":app:testDebugUnitTest",
        ":shared:myohid-core:testDebugUnitTest",
        ":app:assembleDebug",
    ),
    "release": (
        ":app:testReleaseUnitTest",
        ":shared:myohid-core:testReleaseUnitTest",
        ":app:assembleRelease",
    ),
}

_SUITE_DESCRIPTIONS: Mapping[str, str] = {
    "debug": "Run app and shared Android-unit tests, then assemble the debug app.",
    "release": "Run app and shared Android-unit tests, then assemble the release app.",
}


def myohid_test_tool_definitions() -> list[dict[str, Any]]:
    """Return the stable schemas for the MyoHID test tools."""

    return [
        {
            "name": "mudra_myohid_test_catalog",
            "title": "MyoHID Test Catalog",
            "description": (
                "List the pinned, read-only MyoHID validation suites and their "
                "prerequisites."
            ),
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
            },
        },
        {
            "name": "mudra_myohid_test_suite",
            "title": "Run MyoHID Test Suite",
            "description": (
                "Run one named deterministic MyoHID Gradle suite. Arbitrary "
                "Gradle tasks and connected-device tests are not accepted."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "suite": {
                        "type": "string",
                        "enum": sorted(_SUITE_TASKS),
                        "description": "Pinned suite name to execute.",
                    },
                },
                "required": ["suite"],
                "additionalProperties": False,
            },
        },
    ]


class MyoHidTestService:
    """Resolve and run the repository's named MyoHID validation suites."""

    def __init__(
        self,
        repo_root: Path,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        java_locator: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.runner = runner or subprocess.run
        self.monotonic = monotonic
        self.java_locator = java_locator

    def myohid_test_catalog(self, _args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return a deterministic manifest without inspecting or changing Gradle."""

        suites = []
        for suite in sorted(_SUITE_TASKS):
            suites.append(
                {
                    "suite": suite,
                    "description": _SUITE_DESCRIPTIONS[suite],
                    "tasks": list(_SUITE_TASKS[suite]),
                    "wrapper": {
                        "windows": "gradlew.bat",
                        "unix": "gradlew",
                    },
                    "project": MYOHID_PROJECT_RELATIVE.as_posix(),
                    "timeout_seconds": SUITE_TIMEOUT_SECONDS,
                    "prerequisites": [
                        "Java executable available on PATH",
                        "MyoHID/android Gradle wrapper present",
                        "Android SDK configured for the project",
                    ],
                    "connected_device_required": False,
                    "biomech_runtime_required": False,
                }
            )
        return {
            "manifest_version": 1,
            "project": MYOHID_PROJECT_RELATIVE.as_posix(),
            "suites": suites,
        }

    def myohid_test_suite(self, args: dict[str, Any]) -> dict[str, Any]:
        suite = args.get("suite") if isinstance(args, dict) else None
        if not isinstance(suite, str) or suite not in _SUITE_TASKS:
            raise ToolExecutionError(
                "`suite` must be one of: " + ", ".join(sorted(_SUITE_TASKS)) + "."
            )

        command = self._command(suite)
        tasks = list(_SUITE_TASKS[suite])
        base = {
            "suite": suite,
            "tasks": tasks,
            "command": command,
            "command_identity": self._command_identity(command),
            "timeout_seconds": SUITE_TIMEOUT_SECONDS,
            "connected_device_required": False,
            "biomech_runtime_required": False,
        }

        prerequisites = self._prerequisite_errors()
        if prerequisites:
            return {
                **base,
                "status": "blocked",
                "passed": False,
                "exit_code": None,
                "duration_seconds": 0.0,
                "diagnostic_excerpt": " ".join(prerequisites),
                "prerequisite_errors": prerequisites,
            }

        started = self.monotonic()
        try:
            completed = self.runner(
                command,
                cwd=str(self.repo_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=SUITE_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            output = _output_text(getattr(exc, "output", None))
            duration = self._duration(started)
            return {
                **base,
                "status": "timed_out",
                "passed": False,
                "exit_code": None,
                "duration_seconds": duration,
                "diagnostic_excerpt": _diagnostic_excerpt(output),
                "prerequisite_errors": [],
            }
        except OSError as exc:
            duration = self._duration(started)
            return {
                **base,
                "status": "blocked",
                "passed": False,
                "exit_code": None,
                "duration_seconds": duration,
                "diagnostic_excerpt": _diagnostic_excerpt(str(exc)),
                "prerequisite_errors": [f"Unable to start Gradle wrapper: {exc}"],
            }

        duration = self._duration(started)
        passed = completed.returncode == 0
        return {
            **base,
            "status": "passed" if passed else "failed",
            "passed": passed,
            "exit_code": completed.returncode,
            "duration_seconds": duration,
            "diagnostic_excerpt": (
                "" if passed else _diagnostic_excerpt(_output_text(completed.stdout))
            ),
            "prerequisite_errors": [],
        }

    def _command(self, suite: str) -> list[str]:
        project_root = (self.repo_root / MYOHID_PROJECT_RELATIVE).resolve()
        try:
            project_root.relative_to(self.repo_root)
        except ValueError as exc:
            raise ToolExecutionError("MyoHID project path escapes the repository root.") from exc
        wrapper_name = "gradlew.bat" if os.name == "nt" else "gradlew"
        wrapper = project_root / wrapper_name
        return [
            str(wrapper),
            "-p",
            MYOHID_PROJECT_RELATIVE.as_posix(),
            *_SUITE_TASKS[suite],
        ]

    def _safe_project_root(self) -> Path:
        project_root = (self.repo_root / MYOHID_PROJECT_RELATIVE).resolve()
        try:
            project_root.relative_to(self.repo_root)
        except ValueError as exc:
            raise ToolExecutionError("MyoHID project path escapes the repository root.") from exc
        if not project_root.is_dir():
            raise ToolExecutionError(
                f"MyoHID project directory is missing: {MYOHID_PROJECT_RELATIVE.as_posix()}."
            )
        return project_root

    def _prerequisite_errors(self) -> list[str]:
        errors: list[str] = []
        if self.java_locator("java") is None:
            errors.append("Java executable was not found on PATH.")
        try:
            self._safe_project_root()
        except ToolExecutionError as exc:
            errors.append(str(exc))
            return errors
        project_root = self.repo_root / MYOHID_PROJECT_RELATIVE
        wrapper_name = "gradlew.bat" if os.name == "nt" else "gradlew"
        if not (project_root / wrapper_name).is_file():
            errors.append(
                f"MyoHID Gradle wrapper is missing: {(MYOHID_PROJECT_RELATIVE / wrapper_name).as_posix()}."
            )
        return errors

    def _duration(self, started: float) -> float:
        return round(max(0.0, self.monotonic() - started), 3)

    @staticmethod
    def _command_identity(command: Sequence[str]) -> str:
        return " ".join(
            [Path(command[0]).name, *command[1:]]
        )


def _output_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)


def _diagnostic_excerpt(output: str) -> str:
    text = output.strip()
    if len(text) <= MAX_DIAGNOSTIC_CHARS:
        return text
    return "..." + text[-(MAX_DIAGNOSTIC_CHARS - 3):]


__all__ = [
    "MAX_DIAGNOSTIC_CHARS",
    "MYOHID_PROJECT_RELATIVE",
    "MyoHidTestService",
    "SUITE_TIMEOUT_SECONDS",
    "myohid_test_tool_definitions",
]
