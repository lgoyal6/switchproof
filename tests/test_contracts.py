import math

import pytest
from pydantic import ValidationError

from switchproof.compare import compare_rows, digest
from switchproof.db import normalize
from switchproof.engine import green
from switchproof.movies import endpoint_contract
from switchproof.planner import Decision


def test_unordered_rows_retain_duplicates():
    assert compare_rows([{"x": 1}, {"x": 2}], [{"x": 2}, {"x": 1}], False)["status"] == "passed"
    assert compare_rows([{"x": 1}, {"x": 1}], [{"x": 1}], False)["status"] == "mismatch"


@pytest.mark.parametrize("actual", [1.0, "1", True, None])
def test_scalar_types_are_part_of_the_contract(actual):
    assert compare_rows([{"x": 1}], [{"x": actual}])["status"] == "mismatch"


def test_ordered_nested_roles_remain_ordered():
    assert (
        compare_rows([{"roles": ["lead", "support"]}], [{"roles": ["support", "lead"]}], False)["status"]
        == "mismatch"
    )


def test_empty_and_null_are_distinct():
    assert compare_rows([], [])["status"] == "passed"
    assert compare_rows([{"x": None}], [{}])["status"] == "mismatch"


@pytest.mark.parametrize("value", [math.nan, math.inf, object()])
def test_unsupported_values_fail_closed(value):
    with pytest.raises(TypeError):
        normalize(value)


def test_model_cannot_invent_repair_or_inject_extra_fields():
    with pytest.raises(ValidationError):
        Decision(repair_ids=["run_shell"], reasoning="An unsupported action")
    with pytest.raises(ValidationError):
        Decision(repair_ids=[], reasoning="I will abstain here.", command="delete")


def test_green_does_not_accept_errors_or_unknown_states():
    assert green([{"status": "passed"}, {"status": "inconclusive"}])
    assert not green([{"status": "error"}])
    assert not green([{"status": "unknown"}])
    assert not green([])
    assert not green([{"status": "inconclusive"}])


def test_graph_contract_ignores_positions_but_keeps_duplicate_links():
    a = {"title": "Movie", "label": "movie"}
    b = {"title": "Actor", "label": "actor"}
    left = {"nodes": [a, b], "links": [{"source": 1, "target": 0}, {"source": 1, "target": 0}]}
    right = {"nodes": [b, a], "links": [{"source": 0, "target": 1}, {"source": 0, "target": 1}]}
    assert endpoint_contract("/graph", left) == endpoint_contract("/graph", right)
    right["links"].pop()
    assert endpoint_contract("/graph", left) != endpoint_contract("/graph", right)


def test_hashes_bind_scalar_types():
    assert digest({"x": 1}) != digest({"x": "1"})


def test_missing_provider_key_fails_before_network(monkeypatch):
    from switchproof.planner import choose_repair

    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="Anthropic key is missing"):
        choose_repair({}, "anthropic")


def test_anthropic_uses_structured_decision_and_preserves_provenance(monkeypatch):
    from types import SimpleNamespace

    from switchproof.planner import choose_repair

    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-unused")
    seen = {}

    class Client:
        def __init__(self, **kwargs):
            self.messages = self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def parse(self, **kwargs):
            seen.update(kwargs)
            return SimpleNamespace(
                id="test-message",
                stop_reason="end_turn",
                parsed_output=Decision(
                    repair_ids=[], reasoning="No supported repair applies to this evidence."
                ),
                usage=SimpleNamespace(model_dump=lambda: {"input_tokens": 20, "output_tokens": 15}),
            )

    monkeypatch.setattr("anthropic.Anthropic", Client)
    result = choose_repair({"failures": []}, "anthropic")
    assert seen["output_format"] is Decision
    assert result["request_id"] == "test-message"
    assert result["usage"]["output_tokens"] == 15
    assert result["decision"]["repair_ids"] == []
