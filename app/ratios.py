"""Ratio range over the feasible true-value polytope.

Given a feasible filing, the *true* amounts form a convex polytope defined by
the closed rounding boxes of every fact intersected with all weighted
summation equations.  For two facts ``N`` (numerator) and ``D``
(denominator) the analyst needs the exact range of the ratio ``N / D`` over
that whole polytope — not the quotient of two independent box extrema, and
not a single report witness.

The feasible set is a linear program (boxes + equalities), and extrema of a
linear-fractional function over a non-empty bounded polyhedron are attained
at vertices.  We solve each endpoint with the **Charnes–Cooper
transformation**: with ``y = t * x`` and an auxiliary scale ``t`` the

    minimise / maximise  (c_N . x) / (c_D . x)   subject to  c_D . x > 0

becomes the plain linear program

    min/max  c_N . y
        s.t. A_eq y = 0                          (equalities, homogeneous)
             c_D . y = 1                         (normalisation; forces D > 0)
             lo_i * t <= y_i <= hi_i * t         (scaled rounding boxes)
             t >= eps

and the original solution is recovered as ``x = y / t``.  The scale ``t``
keeps every coefficient O(magnitude of the data) so HiGHS stays numerically
well conditioned regardless of the currency unit.
"""
from __future__ import annotations

import math

import numpy as np
from scipy.optimize import linprog

from . import config
from .errors import ApiError, BadPayload
from .solver import _bounds, _residual, _solve, _witness_rows


class DenominatorNotPositive(ApiError):
    def __init__(self, concept: str, maximum: float):
        super().__init__(
            422, "denominator_not_positive",
            f"denominator concept '{concept}' has no feasible true value strictly "
            f"greater than zero (best feasible value {round(maximum, 10)})",
        )


def _check_concepts(facts: list[dict], equations: list[dict],
                    numerator: str, denominator: str) -> None:
    known = {f["concept"] for f in facts}
    missing = sorted({c for c in (numerator, denominator) if c not in known})
    if missing:
        raise BadPayload(
            f"concept(s) {missing} are not facts of the report's instance revision",
            code="unknown_concept",
        )
    if numerator == denominator:
        raise BadPayload("numerator_concept and denominator_concept must differ")
    # Reject the pending case explicitly: the truth set is only defined when
    # every equation references an available fact.
    unresolved = sorted({
        c for eq in equations for c in eq["coefficients"] if c not in known
    })
    if unresolved:
        raise BadPayload(
            f"report is pending; equations reference missing facts {unresolved}",
            code="report_not_feasible",
        )


def _denominator_maximum(concepts: list[str], bounds, equations: list[dict],
                         denominator: str) -> float:
    """Largest feasible true value of the denominator (LP vertex)."""
    d_idx = concepts.index(denominator)
    c = np.zeros(len(concepts))
    c[d_idx] = -1.0  # linprog minimises -> push denominator to its maximum
    A_eq = np.array([[eq["coefficients"].get(k, 0) for k in concepts]
                     for eq in equations], dtype=float)
    res = linprog(c, A_eq=A_eq, b_eq=np.zeros(len(equations)),
                  bounds=bounds, method="highs")
    if not res.success:
        raise ApiError(500, "solver_error",
                       f"denominator feasibility solver failed: {res.message}")
    return float(res.x[d_idx])


def _fractional_extremum(concepts: list[str], bounds, equations: list[dict],
                         numerator: str, denominator: str,
                         maximize: bool) -> tuple[float, np.ndarray, float]:
    """Charnes-Cooper LP for one ratio endpoint.

    With ``y = t * x`` the fractional problem is normalised by the equality
    ``c_D . y = 1`` (equivalently ``t * (c_D . x) = 1``).  The objective then
    collapses to the linear term ``c_N . y``; the original feasible amounts are
    recovered as ``x = y / t``.  The normalisation itself guarantees a strictly
    positive denominator on every feasible point of the transformed LP.

    Returns ``(ratio, true_value_vector, scale_t)``.
    """
    n = len(concepts)
    n_idx = concepts.index(numerator)
    d_idx = concepts.index(denominator)

    # Variables: y_1..y_n (scaled amounts) then t (scaling variable).
    # Equalities A.x = 0 are homogeneous, so A.y = 0 (t does not appear).
    eq_rows = []
    for eq in equations:
        row = [float(eq["coefficients"].get(k, 0)) for k in concepts]
        row.append(0.0)
        eq_rows.append(row)
    # Charnes-Cooper normalisation: c_D . y = 1.
    norm_row = [0.0] * (n + 1)
    norm_row[d_idx] = 1.0
    eq_rows.append(norm_row)
    A_eq = np.array(eq_rows)
    b_eq = np.zeros(len(eq_rows))
    b_eq[-1] = 1.0

    # Scaled boxes: lo_i * t <= y_i <= hi_i * t, written as two A_ub rows each
    # (lo/hi may be negative, so the direction cannot be expressed as bounds).
    magnitude = max(1.0, max(max(abs(lo), abs(hi)) for lo, hi in bounds))
    eps = config.RATIO_EPS / magnitude  # floor on t given c_D.y = 1
    box_rows: list[list[float]] = []
    box_rhs: list[float] = []
    for i, (lo, hi) in enumerate(bounds):
        row_hi = [0.0] * (n + 1)   # y_i - hi_i * t <= 0
        row_hi[i] = 1.0
        row_hi[n] = -hi
        box_rows.append(row_hi)
        box_rhs.append(0.0)
        row_lo = [0.0] * (n + 1)   # lo_i * t - y_i <= 0
        row_lo[i] = -1.0
        row_lo[n] = lo
        box_rows.append(row_lo)
        box_rhs.append(0.0)
    A_ub = np.array(box_rows)
    b_ub = np.array(box_rhs)

    # y_i are coupled to t through A_ub (free LP variables); t stays positive.
    cc_bounds: list[tuple[float | None, float | None]] = \
        [(None, None)] * n + [(eps, None)]

    sign = -1.0 if maximize else 1.0  # linprog minimises c_N.y
    c = np.zeros(n + 1)
    c[n_idx] = sign
    res = linprog(c, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq,
                  bounds=cc_bounds, method="highs")
    if not res.success:
        raise ApiError(500, "solver_error",
                       f"ratio endpoint solver failed: {res.message}")

    t = float(res.x[n])
    if not t > 0.0:
        raise ApiError(500, "solver_error", "ratio solver returned a non-positive scale")
    x = np.array(res.x[:n]) / t
    ratio = float(x[n_idx] / x[d_idx])
    return ratio, x, t


def _verify_endpoint(concepts: list[str], x: np.ndarray, facts_by_concept: dict,
                     equations: list[dict], numerator: str,
                     denominator: str, ratio: float) -> None:
    """Independent post-solve verification of a recovered witness."""
    x_map = {c: float(x[i]) for i, c in enumerate(concepts)}
    tol = config.WITNESS_TOL * max(
        1.0, max(abs(v) for v in x_map.values())
    )
    for c in concepts:
        f = facts_by_concept[c]
        if not (f["lower"] - tol <= x_map[c] <= f["upper"] + tol):
            raise ApiError(500, "solver_error",
                           f"witness value for {c} leaves its rounding interval")
    for eq in equations:
        if not math.isclose(_residual(eq, x_map), 0.0, abs_tol=tol):
            raise ApiError(500, "solver_error",
                           f"witness violates equation {eq['id']}")
    if x_map[denominator] <= 0.0:
        raise ApiError(500, "solver_error",
                       "recovered witness denominator is not strictly positive")
    if not math.isclose(x_map[numerator] / x_map[denominator], ratio,
                        rel_tol=1e-8, abs_tol=tol):
        raise ApiError(500, "solver_error",
                       "recovered witness does not attain the reported ratio endpoint")


def ratio_range(facts: list[dict], equations: list[dict],
                numerator: str, denominator: str) -> dict:
    """Compute min/max of ``numerator / denominator`` over the feasible set.

    The feasible set is rebuilt from the stored rounding boxes and equations
    (never from a previously saved witness).  Raises
    :class:`DenominatorNotPositive` when the denominator cannot be strictly
    positive in any feasible solution, or ``BadPayload`` when the report is
    not a feasible one / the concepts are unknown.
    """
    _check_concepts(facts, equations, numerator, denominator)

    facts_by_concept = {f["concept"]: f for f in facts}
    # Every fact of the instance is a bounded variable of the truth set,
    # including facts that no equation references: they still constrain which
    # amounts are admissible and appear in the full-amount witnesses.
    concepts = sorted(facts_by_concept)
    bounds = _bounds(facts_by_concept, concepts)

    # A ratio is only defined on a non-empty feasible polytope; re-run the
    # feasibility check instead of trusting the report's saved status.
    if _solve(concepts, bounds, equations) is None:
        raise BadPayload(
            "the stored instance/XSD/calculation sources have no feasible solution",
            code="report_not_feasible",
        )

    best_denominator = _denominator_maximum(
        concepts, bounds, equations, denominator
    )
    positivity_tol = config.WITNESS_TOL * max(
        1.0, max(max(abs(lo), abs(hi)) for lo, hi in bounds)
    )
    if best_denominator <= positivity_tol:
        raise DenominatorNotPositive(denominator, best_denominator)

    min_ratio, x_min, _ = _fractional_extremum(
        concepts, bounds, equations, numerator, denominator, maximize=False
    )
    max_ratio, x_max, _ = _fractional_extremum(
        concepts, bounds, equations, numerator, denominator, maximize=True
    )
    _verify_endpoint(concepts, x_min, facts_by_concept, equations,
                     numerator, denominator, min_ratio)
    _verify_endpoint(concepts, x_max, facts_by_concept, equations,
                     numerator, denominator, max_ratio)

    return {
        "numerator_concept": numerator,
        "denominator_concept": denominator,
        "min_ratio": min_ratio,
        "max_ratio": max_ratio,
        "denominator_max_feasible": best_denominator,
        "min_witness": _witness_rows(concepts, x_min, facts_by_concept),
        "max_witness": _witness_rows(concepts, x_max, facts_by_concept),
        "concept_count": len(concepts),
        "equation_count": len(equations),
        "solver": "scipy.optimize.linprog(highs)+charnes-cooper",
    }
