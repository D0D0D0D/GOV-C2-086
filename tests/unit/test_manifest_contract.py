"""Manifest/config alignment and placeholder elimination."""

import importlib
import pathlib

import yaml


def test_manifest_entrypoint_imports_and_declares_non_hitl_contract():
    manifest = yaml.safe_load(pathlib.Path("config/agent.yaml").read_text())
    module_name, class_name = manifest["class"].rsplit(".", 1)
    assert getattr(importlib.import_module(module_name), class_name)
    assert manifest["id"] == "GOV-C2-086"
    assert manifest["requires"]["secrets"] == []
    runtime = yaml.safe_load(pathlib.Path("config/config.yaml").read_text())
    assert runtime["memory_enabled"] is False
    assert runtime["hitl"]["enabled"] is False


def test_no_unresolved_placeholders_outside_examples():
    violations = []
    for root in (pathlib.Path("src"), pathlib.Path("config")):
        for path in root.rglob("*"):
            if (
                path.is_file()
                and path.suffix in {".py", ".yaml", ".yml"}
                and "src/examples" not in str(path)
                and "{{" in path.read_text(errors="ignore")
            ):
                violations.append(path)
    assert violations == []
