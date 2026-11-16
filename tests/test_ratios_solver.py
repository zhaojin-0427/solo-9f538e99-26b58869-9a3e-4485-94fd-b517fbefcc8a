"""Unit tests for the Charnes-Cooper ratio-range solver."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.errors import ApiError, BadPayload
from app.ratios import ratio_range


def _fact(concept, value, decimals, lower, upper):
    return {"concept": concept, "value": value, "decimals": decimals,
            "lower": lower, "upper": upper}


def _eq(summation, items):
    coef = {summation: -1}
    for c, w in items:
        coef[c] = coef.get(c, 0) + w
    return {"id": "eq1", "role": "r", "summation": summation,
            "items": [{"concept": c, "weight": w} for c, w in items],
            "coefficients": coef, "concept_count": len(coef)}


def _assert_vertex_witness(res, facts, equations, endpoint):
    rows = res[endpoint]
    x = {r["concept"]: r["true_value"] for r in rows}
    by_fact = {f["concept"]: f for f in facts}
    for concept, value in x.items():
        f = by_fact[concept]
        assert f["lower"] - 1e-9 <= value <= f["upper"] + 1e-9
    for eq in equations:
        residual = sum(coef * x[c] for c, coef in eq["coefficients"].items())
        assert abs(residual) < 1e-8
    ratio = x[res["numerator_concept"]] / x[res["denominator_concept"]]
    assert abs(ratio - res[
        "min_ratio" if endpoint == "min_witness" else "max_ratio"
    ]) < 1e-9
    assert x[res["denominator_concept"]] > 0


def test_ratio_pinned_for_exact_facts():
    facts = [
        _fact("t:A", 2, "INF", 2, 2),
        _fact("t:B", 3, "INF", 3, 3),
        _fact("t:S", 5, "INF", 5, 5),
    ]
    eqs = [_eq("t:S", [("t:A", 1), ("t:B", 1)])]
    res = ratio_range(facts, eqs, "t:A", "t:S")
    assert res["min_ratio"] == pytest.approx(0.4)
    assert res["max_ratio"] == pytest.approx(0.4)
    _assert_vertex_witness(res, facts, eqs, "min_witness")
    _assert_vertex_witness(res, facts, eqs, "max_witness")


def test_range_narrower_than_independent_box_quotient():
    # A + B = S with +-0.5 boxes; the equation removes independent slack.
    facts = [
        _fact("t:A", 10, 0, 9.5, 10.5),
        _fact("t:B", 20, 0, 19.5, 20.5),
        _fact("t:S", 30, 0, 29.5, 30.5),
    ]
    eqs = [_eq("t:S", [("t:A", 1), ("t:B", 1)])]
    res = ratio_range(facts, eqs, "t:A", "t:S")
    # naive independent quotient would allow [9.5/30.5, 10.5/29.5]
    assert res["min_ratio"] > 9.5 / 30.5
    assert res["max_ratio"] < 10.5 / 29.5
    # hand-computed vertices: min at A=9.5,B=20.5,S=30; max at A=10.5,B=19.5,S=30
    assert res["min_ratio"] == pytest.approx(9.5 / 30.0)
    assert res["max_ratio"] == pytest.approx(10.5 / 30.0)
    _assert_vertex_witness(res, facts, eqs, "min_witness")
    _assert_vertex_witness(res, facts, eqs, "max_witness")


def test_shared_true_value_couples_two_equations():
    # S = A - B and T = A - C; A is one shared true value across both.
    facts = [
        _fact("t:A", 10, 0, 9.5, 10.5),
        _fact("t:B", 4, 0, 3.5, 4.5),
        _fact("t:S", 6, 0, 5.5, 6.5),
        _fact("t:C", 3, 0, 2.5, 3.5),
        _fact("t:T", 7, 0, 6.5, 7.5),
    ]
    eqs = [
        _eq("t:S", [("t:A", 1), ("t:B", -1)]),
        _eq("t:T", [("t:A", 1), ("t:C", -1)]),
    ]
    res = ratio_range(facts, eqs, "t:S", "t:A")
    _assert_vertex_witness(res, facts, eqs, "min_witness")
    _assert_vertex_witness(res, facts, eqs, "max_witness")
    # the two witnesses must share one coherent A value per endpoint
    for endpoint in ("min_witness", "max_witness"):
        x = {r["concept"]: r["true_value"] for r in res[endpoint]}
        assert abs((x["t:A"] - x["t:B"]) - x["t:S"]) < 1e-9
        assert abs((x["t:A"] - x["t:C"]) - x["t:T"]) < 1e-9


def test_denominator_not_positive_rejected():
    facts = [
        _fact("t:A", -2, "INF", -2, -2),
        _fact("t:B", -3, "INF", -3, -3),
        _fact("t:S", -5, "INF", -5, -5),
    ]
    eqs = [_eq("t:S", [("t:A", 1), ("t:B", 1)])]
    with pytest.raises(ApiError) as exc:
        ratio_range(facts, eqs, "t:A", "t:S")
    assert exc.value.code == "denominator_not_positive"


def test_denominator_box_touching_zero_rejected():
    # largest feasible denominator is exactly 0, never strictly positive:
    # A in [-1.5,-0.5], B in [0.5,1.5] and A+B=S pin S to 0.
    facts = [
        _fact("t:A", -1, "INF", -1, -1),
        _fact("t:B", 1, "INF", 1, 1),
        _fact("t:S", 0, "INF", 0, 0),
    ]
    eqs = [_eq("t:S", [("t:A", 1), ("t:B", 1)])]
    with pytest.raises(ApiError) as exc:
        ratio_range(facts, eqs, "t:A", "t:S")
    assert exc.value.code == "denominator_not_positive"


def test_unknown_concept_and_self_ratio_rejected():
    facts = [
        _fact("t:A", 2, "INF", 2, 2),
        _fact("t:S", 2, "INF", 2, 2),
    ]
    eqs = [_eq("t:S", [("t:A", 1)])]
    with pytest.raises(BadPayload) as exc:
        ratio_range(facts, eqs, "t:Ghost", "t:S")
    assert exc.value.code == "unknown_concept"
    with pytest.raises(BadPayload):
        ratio_range(facts, eqs, "t:A", "t:A")


def test_infeasible_truth_set_rejected():
    facts = [
        _fact("t:A", 2, "INF", 2, 2),
        _fact("t:B", 3, "INF", 3, 3),
        _fact("t:S", 9, "INF", 9, 9),
    ]
    eqs = [_eq("t:S", [("t:A", 1), ("t:B", 1)])]
    with pytest.raises(BadPayload) as exc:
        ratio_range(facts, eqs, "t:A", "t:S")
    assert exc.value.code == "report_not_feasible"


def test_unreferenced_fact_stays_bounded_in_witness():
    # U is not referenced by any equation but must remain inside its box
    facts = [
        _fact("t:A", 10, 0, 9.5, 10.5),
        _fact("t:B", 20, 0, 19.5, 20.5),
        _fact("t:S", 30, 0, 29.5, 30.5),
        _fact("t:U", 7, 0, 6.5, 7.5),
    ]
    eqs = [_eq("t:S", [("t:A", 1), ("t:B", 1)])]
    res = ratio_range(facts, eqs, "t:A", "t:S")
    for endpoint in ("min_witness", "max_witness"):
        u = next(r for r in res[endpoint] if r["concept"] == "t:U")
        assert 6.5 <= u["true_value"] <= 7.5
