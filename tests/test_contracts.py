"""Contract tests for the public API schemas.

Both services use the package name `app`, so they cannot both sit on
sys.path. Each schema module is loaded by file path instead. These modules
import only pydantic — no database or broker — so the contract is testable
without the stack running. Integration behaviour is verified against the
live stack; see README "Verifying persistence".
"""

import importlib.util
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

SERVICES = Path(__file__).resolve().parents[1] / "services"


def _load(service: str, alias: str):
    path = SERVICES / service / "app" / "schemas.py"
    spec = importlib.util.spec_from_file_location(alias, path)
    module = importlib.util.module_from_spec(spec)
    # Must be registered before exec: the modules use `from __future__ import
    # annotations`, so pydantic resolves forward refs via sys.modules.
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


orchestrator_schemas = _load("orchestrator", "orchestrator_schemas")
registry_schemas = _load("connector-registry", "registry_schemas")

AgentCreate = orchestrator_schemas.AgentCreate
ConnectorCreate = registry_schemas.ConnectorCreate


def test_agent_contract_accepts_connector_ids():
    model = AgentCreate(
        name="research-agent",
        description="Processes research inputs",
        connector_ids=["github", "drive"],
    )
    assert model.name == "research-agent"
    assert model.connector_ids == ["github", "drive"]


def test_agent_name_has_a_minimum_length():
    with pytest.raises(ValidationError):
        AgentCreate(name="ab")


def test_agent_connector_ids_default_to_empty():
    assert AgentCreate(name="planner").connector_ids == []


def test_connector_contract_rejects_unknown_kind():
    with pytest.raises(ValidationError):
        ConnectorCreate(name="github", kind="ftp")


def test_connector_contract_accepts_known_kinds():
    for kind in ("http", "mcp", "webhook", "internal"):
        model = ConnectorCreate(name=f"conn-{kind}", kind=kind)
        assert model.kind == kind
        # `enabled` was replaced by an authoritative status enum in M1.3;
        # two sources of truth for "may this run" eventually disagree.
        assert model.status.value == "enabled"


def test_connector_cannot_be_created_in_a_deletion_state():
    for bad in ("deletion_requested", "deleted"):
        with pytest.raises(ValidationError):
            ConnectorCreate(name="c", kind="http", status=bad)


def test_connector_config_rejects_literal_secrets():
    for key in ("signing_secret", "api_key", "password", "token", "client_secret"):
        with pytest.raises(ValidationError):
            ConnectorCreate(name="c", kind="http", config={key: "hunter2"})


def test_connector_config_accepts_secret_references():
    model = ConnectorCreate(
        name="wh", kind="webhook", config={"signing_secret_ref": "env:WEBHOOK_KEY"}
    )
    assert model.config["signing_secret_ref"] == "env:WEBHOOK_KEY"


def test_connector_rejects_a_non_url_base_url():
    with pytest.raises(ValidationError):
        ConnectorCreate(name="github", kind="http", base_url="not-a-url")
