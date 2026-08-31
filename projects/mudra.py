"""Mudra project descriptor for the reusable MCP engine."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.project import ProjectDescriptor


REPO_ROOT = Path(__file__).resolve().parents[2]
MCP_PACKAGE_ROOT = REPO_ROOT / "mcp"

MUDRA_DOC_SCOPES: dict[str, dict[str, Any]] = {
    "android-myohid": {
        "title": "Android MyoHID App",
        "description": "Android BLE capture, calibration, controller, recording, and metrics UI.",
        "display_order": 10,
        "roots": ["MyoHID/android", "MyoHID/shared/sedna-android", "MyoHID/native/sedna-c-core", "MyoHID/native/sedna-rust-core"],
        "doc_paths": [
            "MyoHID/AGENTS.md",
            "MyoHID/android/README.md",
            "MyoHID/android/TODO.md",
            "MyoHID/shared/sedna-android/README.md",
            "MyoHID/native/sedna-rust-core/README.md",
            "docs/myohid-repository-layout.md",
            "docs/sedna/*.md",
            "MyoHID/android/docs/*.md",
            "docs/agents/*.md",
            "specs/mobile/features/*.yaml",
        ],
        "manifest_globs": [
            "MyoHID/android/app/src/main/java/**/*.kt",
            "MyoHID/android/app/src/main/kotlin/**/*.kt",
            "MyoHID/android/**/*.gradle.kts",
            "MyoHID/android/**/*.xml",
            "MyoHID/shared/sedna-android/src/**/*.java",
            "MyoHID/shared/sedna-android/**/*.gradle.kts",
            "MyoHID/native/sedna-c-core/**/*.c",
            "MyoHID/native/sedna-c-core/**/*.h",
            "MyoHID/native/sedna-c-core/**/CMakeLists.txt",
            "MyoHID/native/sedna-rust-core/**/*.rs",
            "MyoHID/native/sedna-rust-core/**/Cargo.toml",
        ],
    },
    "ios-myohid": {
        "title": "iOS MyoHID App",
        "description": "SwiftUI iPhone BLE capture, calibration, controller, tasks, metrics, and shared-core bridge.",
        "display_order": 15,
        "roots": ["MyoHID/ios"],
        "doc_paths": [
            "docs/agents/*.md",
            "MyoHID/ios/TODO.md",
            "MyoHID/ios/README.md",
            "docs/myohid-repository-layout.md",
            "specs/mobile/features/*.yaml",
        ],
        "manifest_globs": [
            "MyoHID/ios/Sources/**/*.swift",
            "MyoHID/ios/Resources/**/*.plist",
            "MyoHID/ios/Config/**/*.xcconfig",
            "MyoHID/ios/**/*.yml",
            "MyoHID/ios/scripts/*.sh",
        ],
    },
    "myohid-desktop": {
        "title": "MyoHID Desktop App",
        "description": (
            "Python/PyQt BLE capture, signal processing, recording, and "
            "visualization, plus native Windows/macOS Sedna/Ceres transport and desktop "
            "parity contracts."
        ),
        "display_order": 18,
        "roots": [
            "mudra",
            "MyoHID/native/sedna-desktop-host",
            "MyoHID/native/sedna-c-pipeline",
            "MyoHID/native/sedna-c-core",
            "specs/windows",
        ],
        "doc_paths": [
            "AGENTS.md",
            "README.md",
            "SERVICES.md",
            "mudra/TODO.md",
            "MyoHID/native/README.md",
            "MyoHID/native/sedna-c-core/README.md",
            "MyoHID/native/sedna-desktop-host/README.md",
            "MyoHID/native/sedna-desktop-host/STREAM_INIT_REFERENCE.md",
            "MyoHID/native/sedna-c-pipeline/README.md",
            "MyoHID/native/sedna-pipeline-studio/README.md",
            "docs/sedna/c-portable-core-ffi-abi.md",
            "docs/sedna/desktop-*.md",
            "docs/sedna/macos-corebluetooth-host.md",
            "docs/sedna/python-desktop-*.md",
            "docs/sedna/sedna-pipeline-studio-*.md",
            "specs/windows/features/*.yaml",
        ],
        "manifest_globs": [
            "mudra/**/*.py",
            "MyoHID/native/sedna-c-core/include/**/*.h",
            "MyoHID/native/sedna-c-core/src/**/*.c",
            "MyoHID/native/sedna-c-core/tests/**/*.c",
            "MyoHID/native/sedna-c-core/tools/*.c",
            "MyoHID/native/sedna-c-core/tools/*.py",
            "MyoHID/native/sedna-c-core/CMakeLists.txt",
            "MyoHID/native/sedna-desktop-host/src/**/*.c",
            "MyoHID/native/sedna-desktop-host/src/**/*.h",
            "MyoHID/native/sedna-desktop-host/tests/**/*.c",
            "MyoHID/native/sedna-desktop-host/CMakeLists.txt",
            "MyoHID/native/sedna-c-pipeline/src/**/*.c",
            "MyoHID/native/sedna-c-pipeline/src/**/*.h",
            "MyoHID/native/sedna-c-pipeline/tests/**/*.c",
            "MyoHID/native/sedna-c-pipeline/CMakeLists.txt",
            # Author-maintained protobuf schemas (not the generated *.pb.{c,h},
            # nor the vendored nanopb under third_party/).
            "MyoHID/native/sedna-c-pipeline/proto/*.proto",
            # Example pipeline configs: the C source of truth for the binaries
            # and the commented .pbtxt twins (the *.pipeline binaries themselves
            # are not text and are excluded).
            "MyoHID/native/sedna-c-pipeline/configs/*.c",
            "MyoHID/native/sedna-c-pipeline/configs/*.pbtxt",
            # Pipeline Studio native authoring crate (Rust eframe/egui). The
            # build output under target/ is excluded via the crate .gitignore.
            "MyoHID/native/sedna-pipeline-studio/src/**/*.rs",
            "MyoHID/native/sedna-pipeline-studio/Cargo.toml",
        ],
    },
    "shared-myohid-core": {
        "title": "Shared MyoHID Core",
        "description": "Kotlin Multiplatform shared decoder, feature, model, and mobile core logic.",
        "display_order": 20,
        "roots": ["MyoHID/shared/myohid-core"],
        "doc_paths": [
            "docs/agents/*.md",
            "MyoHID/shared/myohid-core/README.md",
            "MyoHID/AGENTS.md",
            "docs/myohid-repository-layout.md",
            "specs/mobile/features/*.yaml",
        ],
        "manifest_globs": [
            "MyoHID/shared/myohid-core/src/**/*.kt",
            "MyoHID/shared/myohid-core/**/*.gradle.kts",
        ],
    },
    "biomech-runtime": {
        "title": "Biomech Runtime",
        "description": (
            "Biomechanical runtime integration, model assets, native packaging, and "
            "calibration contracts. The runtime source checkout is external to this "
            "repository and is addressed through BIOMECH_RUNTIME_ROOT."
        ),
        "display_order": 25,
        "roots": [
            "$BIOMECH_RUNTIME_ROOT",
            "MyoHID/android/docs/biomech_runtime_integration.md",
            "specs/mobile/features/biomech-runtime.yaml",
        ],
        "doc_paths": [
            "AGENTS.md",
            "MyoHID/android/docs/biomech_runtime_integration.md",
            "docs/agents/biomech-*.md",
            "specs/mobile/features/biomech-runtime.yaml",
        ],
        "manifest_globs": [
            "MyoHID/android/app/build.gradle.kts",
            "MyoHID/android/gradle/libs.versions.toml",
            "MyoHID/android/app/src/main/java/**/*Biomech*.kt",
            "MyoHID/shared/myohid-core/src/**/*Biomech*.kt",
        ],
    },
    "mudra-gateway": {
        "title": "Mudra Gateway",
        "description": "Rust axum backend for sessions, uploads, metrics, models, and artifacts.",
        "display_order": 30,
        "roots": ["mudra-gateway"],
        "doc_paths": [
            "docs/agents/*.md",
            "mudra-gateway/AGENTS.md",
            "mudra-gateway/README.md",
            "mudra-gateway/TODO.md",
            "mudra-gateway/tools/*.md",
        ],
        "manifest_globs": [
            "mudra-gateway/src/**/*.rs",
            "mudra-gateway/Cargo.toml",
            "mudra-gateway/tools/*.py",
        ],
    },
    "python-gui": {
        "title": "Python Front-End GUI",
        "description": "Python BLE capture, signal pipeline, recording, analysis, and PyQt UI.",
        "display_order": 40,
        "roots": ["mudra"],
        "doc_paths": [
            "AGENTS.md",
            "README.md",
            "docs/agents/*.md",
            "docs/sedna/python-desktop-*.md",
            "mudra/TODO.md",
            "SERVICES.md",
        ],
        "manifest_globs": [
            "mudra/**/*.py",
            "scripts/*.py",
        ],
    },
    "matlab": {
        "title": "MATLAB Mudra SDK",
        "description": "MATLAB packet decoding, signal processing, analysis, and Mudra band integration.",
        "display_order": 45,
        "roots": ["matlab"],
        "doc_paths": [
            "matlab/README.md",
            "matlab/**/*.md",
        ],
        "manifest_globs": [
            "matlab/**/*.m",
            "matlab/**/*.md",
        ],
    },
    "mcp-server": {
        "title": "Local MCP Server",
        "description": "Repo-local MCP server, SQLite mcp.db schema, dashboard, todos, docs, and agent task state.",
        "display_order": 50,
        "roots": [
            "mcp",
            "scripts/mcp_config.py",
            "scripts/mcp_server.py",
            "scripts/mcp_state_sync.py",
            "scripts/mcp_gui",
            "scripts/render_mcp_diagrams.py",
            "start_mcp_dashboard.bat",
            "docs/mcp-server",
        ],
        "doc_paths": [
            "AGENTS.md",
            "README.md",
            "docs/agents/*.md",
            "mcp/PLAN.md",
            "mcp/NETWORK.md",
            "docs/mcp-server/*.md",
            "docs/mcp-server/diagrams/*.dot",
            "mcp/config.py",
            "mcp/project.py",
            "mcp/projects/*.py",
            "mcp/auth.py",
            "mcp/http_transport.py",
            "mcp/stdio_transport.py",
            "mcp/server.py",
            "mcp/state_sync.py",
            "mcp/gui/*.html",
            "mcp/gui/*.js",
            "mcp/gui/*.css",
            "mcp/gui/dashboard/**/*.js",
            "mcp/gui/styles/*.css",
            "scripts/mcp_config.py",
            "scripts/mcp_server.py",
            "scripts/mcp_state_sync.py",
            "scripts/render_mcp_diagrams.py",
            "scripts/mcp_gui/*.html",
            "scripts/mcp_gui/*.js",
            "scripts/mcp_gui/*.css",
            "start_mcp_dashboard.bat",
        ],
        "manifest_globs": [
            "mcp/**/*.py",
            "mcp/gui/*",
            "mcp/gui/dashboard/**/*",
            "mcp/gui/styles/*",
            "docs/mcp-server/*.md",
            "docs/mcp-server/diagrams/*.dot",
            "scripts/mcp_config.py",
            "scripts/mcp_server.py",
            "scripts/mcp_state_sync.py",
            "scripts/render_mcp_diagrams.py",
            "scripts/mcp_gui/*",
            "start_mcp_dashboard.bat",
        ],
    },
    "on-device-validation": {
        "title": "On-Device Hardware Validation",
        "description": (
            "Hardware-in-the-loop validation tasks that require a real band plus a human "
            "operator and cannot be completed at a desk: scripted on-body sessions, V3 "
            "stream/event recording, and offline decode/counting of the captured events. "
            "Cross-cutting scope (any app); the per-app code paths live in each todo's "
            "code_paths. See docs/validation/on-device-validation.md for the protocol."
        ),
        "display_order": 60,
        "roots": ["docs/validation", "mudra-gateway/tools/decode_v3.py"],
        "doc_paths": [
            "docs/validation/on-device-validation.md",
            "AGENTS.md",
        ],
        "manifest_globs": [
            "docs/validation/on-device-validation.md",
            "mudra-gateway/tools/decode_v3.py",
        ],
    },
    "user-validation": {
        "title": "User Validation",
        "description": (
            "Subjective, usability, acceptance, and other human-tester validation that "
            "does not inherently require physical-device execution. See "
            "docs/validation/user-validation.md for the protocol."
        ),
        "display_order": 65,
        "roots": ["docs/validation"],
        "doc_paths": [
            "docs/validation/user-validation.md",
        ],
        "manifest_globs": [],
    },
    "unity-myohid": {
        "title": "Unity MyoHID SDK",
        "description": (
            "Unity package for MyoHID BLE capture, EMG/IMU packet decoding, stream "
            "logging, calibration UI, and gesture model runtime code."
        ),
        "display_order": 70,
        "roots": ["unity/com.nml.myohid"],
        "doc_paths": [
            "unity/README.md",
            "unity/com.nml.myohid/README.md",
            "unity/com.nml.myohid/CHANGELOG.md",
            "unity/com.nml.myohid/package.json",
        ],
        "manifest_globs": [
            "unity/com.nml.myohid/Runtime/**/*.cs",
            "unity/com.nml.myohid/Runtime/**/*.java",
            "unity/com.nml.myohid/Samples~/**/*.cs",
            "unity/com.nml.myohid/Tests/**/*.cs",
            "unity/com.nml.myohid/**/*.asmdef",
        ],
    },
}

MUDRA_PROJECT = ProjectDescriptor(
    key="mudra",
    display_name="Mudra",
    server_name="Mudra MCP",
    resource_scheme="mudra-mcp",
    repo_root=REPO_ROOT,
    db_path=REPO_ROOT / "mcp.db",
    gui_dir=MCP_PACKAGE_ROOT / "gui",
    doc_scopes=MUDRA_DOC_SCOPES,
    doc_drift_audit_scope="mcp-server",
    default_mcp_host="10.0.0.14",
    default_mcp_port=8765,
    remote_base_url="https://mcp.nml.wtf/mudra",
    legacy_gateway_url="https://mudra.nml.wtf",
    cloudflare_env_prefix="MUDRA",
)
