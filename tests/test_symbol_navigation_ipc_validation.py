from __future__ import annotations

import json
from typing import Any

import pytest

from harness.ipc import IpcProtocolError, _project_symbol_navigation_from_wire
from harness.retrieval import MAX_SYMBOL_NAVIGATION_BYTES, MAX_SYMBOL_NAVIGATION_RELATIONS


def _navigation(**relation_updates: object) -> dict[str, Any]:
    return {
        "needle": "target_call",
        "precise_languages": ["python"],
        "candidate_precise_files": 1,
        "parsed_precise_files": 1,
        "parse_failures": 0,
        "parse_skipped_files": 0,
        "matching_unsupported_files": 0,
        "definition_count": 0,
        "call_count": 1,
        "test_call_count": 0,
        "import_count": 0,
        "inheritance_count": 0,
        "precise_classification_complete": True,
        "relations_truncated": False,
        "evidence_truncated": False,
        "relations": [
            {
                "kind": "call",
                "path": "src/caller.py",
                "line": 2,
                "column": 1,
                "scope": None,
                "target": "target_call",
                "symbol_kind": None,
                "in_test": False,
                "evidence": None,
                **relation_updates,
            }
        ],
    }


def _resolved_navigation() -> dict[str, Any]:
    return _navigation(
        resolved_target="service.target_call",
        resolution_kind="python_from_import_binding",
        resolved_definition_path="src/service.py",
        resolved_definition_line=1,
        resolved_definition_column=1,
        resolved_definition_kind="function",
        resolution_validation_kind="python_workspace_direct_export",
    )


def test_legacy_unresolved_navigation_keeps_its_wire_shape() -> None:
    payload = _navigation()
    decoded = _project_symbol_navigation_from_wire(payload)
    assert decoded is not None
    assert decoded.to_wire() == payload
    assert _project_symbol_navigation_from_wire(None) is None


@pytest.mark.parametrize(
    "resolution_kind",
    [
        "python_import_binding",
        "python_from_import_binding",
        "python_self_method_binding",
        "python_cls_method_binding",
        "python_self_inherited_method_binding",
        "python_cls_inherited_method_binding",
    ],
)
def test_lexical_binding_survives_without_claiming_definition_proof(resolution_kind: str) -> None:
    payload = _navigation(resolved_target="service.target_call", resolution_kind=resolution_kind)
    decoded = _project_symbol_navigation_from_wire(payload)
    assert decoded is not None
    assert decoded.to_wire() == payload
    assert decoded.relations[0].resolved_definition_path is None
    assert decoded.relations[0].resolution_module is None


@pytest.mark.parametrize(
    "validation_kind", ["python_workspace_direct_export", "python_workspace_reexport_chain"]
)
@pytest.mark.parametrize("definition_kind", ["class", "function", "variable"])
def test_validated_import_keeps_the_exact_export_proof(
    validation_kind: str, definition_kind: str
) -> None:
    payload = _resolved_navigation()
    payload["relations"][0]["resolved_definition_kind"] = definition_kind
    payload["relations"][0]["resolution_validation_kind"] = validation_kind
    decoded = _project_symbol_navigation_from_wire(payload)
    assert decoded is not None
    assert decoded.to_wire() == payload
    assert decoded.relations[0].resolution_module is None


@pytest.mark.parametrize(
    "field",
    [
        "resolved_target",
        "resolution_kind",
        "resolved_definition_path",
        "resolved_definition_line",
        "resolved_definition_column",
        "resolved_definition_kind",
        "resolution_validation_kind",
    ],
)
def test_partial_resolution_proofs_are_rejected(field: str) -> None:
    payload = _resolved_navigation()
    del payload["relations"][0][field]
    with pytest.raises(IpcProtocolError):
        _project_symbol_navigation_from_wire(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("resolved_target", None),
        ("resolved_target", ""),
        ("resolved_target", "x" * 521),
        ("resolution_kind", "unknown_binding"),
        ("resolved_definition_path", None),
        ("resolved_definition_path", ""),
        ("resolved_definition_path", "x" * 4097),
        ("resolved_definition_line", 0),
        ("resolved_definition_line", True),
        ("resolved_definition_column", -1),
        ("resolved_definition_column", "1"),
        ("resolved_definition_kind", "method"),
        ("resolution_validation_kind", "unknown_validation"),
    ],
)
def test_invalid_resolution_values_are_rejected(field: str, value: object) -> None:
    payload = _resolved_navigation()
    payload["relations"][0][field] = value
    with pytest.raises(IpcProtocolError):
        _project_symbol_navigation_from_wire(payload)


@pytest.mark.parametrize("kind", ["definition", "import", "inheritance"])
def test_resolution_is_only_accepted_for_calls(kind: str) -> None:
    payload = _resolved_navigation()
    payload["relations"][0]["kind"] = kind
    with pytest.raises(IpcProtocolError):
        _project_symbol_navigation_from_wire(payload)


@pytest.mark.parametrize(
    "resolution_kind",
    [
        "python_self_method_binding",
        "python_cls_method_binding",
        "python_self_inherited_method_binding",
        "python_cls_inherited_method_binding",
    ],
)
def test_receiver_binding_does_not_claim_workspace_export_proof(resolution_kind: str) -> None:
    payload = _resolved_navigation()
    payload["relations"][0]["resolution_kind"] = resolution_kind
    with pytest.raises(IpcProtocolError):
        _project_symbol_navigation_from_wire(payload)


@pytest.mark.parametrize("field", ["resolution_module", "private_internal_field"])
def test_internal_and_unknown_fields_are_rejected_without_echoing_values(field: str) -> None:
    payload = _navigation(**{field: "private-value-must-not-be-disclosed"})
    with pytest.raises(IpcProtocolError) as error:
        _project_symbol_navigation_from_wire(payload)
    assert field not in str(error.value)
    assert "private-value" not in str(error.value)


@pytest.mark.parametrize(
    "languages",
    [None, "python", [1], ["kotlin"], ["python", "python"], ["typescript", "python"]],
)
def test_invalid_or_noncanonical_language_lists_are_rejected(languages: object) -> None:
    payload = _navigation()
    payload["precise_languages"] = languages
    with pytest.raises(IpcProtocolError):
        _project_symbol_navigation_from_wire(payload)


def test_navigation_byte_and_relation_budgets_remain_enforced() -> None:
    payload = _navigation()
    payload["relations"] *= MAX_SYMBOL_NAVIGATION_RELATIONS + 1
    with pytest.raises(IpcProtocolError, match="item limit"):
        _project_symbol_navigation_from_wire(payload)

    payload = _resolved_navigation()
    payload["relations"] *= MAX_SYMBOL_NAVIGATION_RELATIONS
    assert len(json.dumps(payload).encode()) > MAX_SYMBOL_NAVIGATION_BYTES
    with pytest.raises(IpcProtocolError, match="byte limit"):
        _project_symbol_navigation_from_wire(payload)
