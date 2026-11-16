"""Ratio interval API: rebuilt truth set, witnesses, staleness, persistence."""
from __future__ import annotations

from conftest import check, create_instance, create_package


def _analysis(client, report_id, numerator, denominator, expected=201):
    r = client.post(
        f"/reports/{report_id}/ratio-analyses",
        json={"numerator_concept": numerator,
              "denominator_concept": denominator},
    )
    assert r.status_code == expected, r.text
    return r


def _feasible_report(client, samples, xml_name="feasible"):
    pkg = create_package(client, samples)
    inst = create_instance(client, pkg["package_id"], samples[xml_name])
    report = check(client, inst["instance_id"], 1, 1).json()["report"]
    assert report["status"] == "feasible"
    return pkg, inst, report


def _check_witness(analysis, endpoint, pkg, numerator, denominator):
    witness = analysis[endpoint]
    by_concept = {w["concept"]: w for w in witness}
    # every amount stays inside its own closed rounding interval
    for w in witness:
        assert w["lower"] <= w["true_value"] <= w["upper"]
    # every calculation equation holds on the same shared true values
    for eq in pkg["latest"]["equations"]:
        residual = sum(coef * by_concept[c]["true_value"]
                       for c, coef in eq["coefficients"].items())
        assert abs(residual) < 1e-7
    ratio = (by_concept[numerator]["true_value"]
             / by_concept[denominator]["true_value"])
    assert abs(ratio - analysis[
        "min_ratio" if endpoint == "min_witness" else "max_ratio"
    ]) < 1e-8
    assert by_concept[denominator]["true_value"] > 0
    return ratio


def test_ratio_endpoints_have_full_attaining_witnesses(client, samples):
    pkg, _inst, report = _feasible_report(client, samples)
    r = _analysis(client, report["report_id"],
                  "acct:NetIncome", "acct:Revenues")
    analysis = r.json()
    assert analysis["min_ratio"] <= analysis["max_ratio"]
    assert analysis["stale"] is False and analysis["stale_reason"] is None
    assert analysis["instance_revision"] == 1
    assert analysis["package_revision"] == 1

    _check_witness(analysis, "min_witness", pkg,
                   "acct:NetIncome", "acct:Revenues")
    _check_witness(analysis, "max_witness", pkg,
                   "acct:NetIncome", "acct:Revenues")
    # full-amount witness: every fact of the instance appears
    assert len(analysis["min_witness"]) == 9
    assert {w["concept"] for w in analysis["min_witness"]} == \
        {w["concept"] for w in analysis["max_witness"]}


def test_ratio_range_is_not_independent_box_quotient(client, samples):
    # Rounding instance: the shared equations couple the facts. The returned
    # range must be narrower than dividing the independent box extrema.
    pkg, _inst, report = _feasible_report(client, samples, "rounding")
    analysis = _analysis(
        client, report["report_id"],
        "acct:CurrentAssets", "acct:Assets"
    ).json()
    facts = {f["concept"]: f for f in _inst_facts(client, report["instance_id"])}
    ca, assets = facts["acct:CurrentAssets"], facts["acct:Assets"]
    naive_min = ca["lower"] / assets["upper"]
    naive_max = ca["upper"] / assets["lower"]
    assert analysis["min_ratio"] > naive_min
    assert analysis["max_ratio"] < naive_max
    _check_witness(analysis, "min_witness", pkg,
                   "acct:CurrentAssets", "acct:Assets")
    _check_witness(analysis, "max_witness", pkg,
                   "acct:CurrentAssets", "acct:Assets")


def _inst_facts(client, instance_id):
    return client.get(f"/instances/{instance_id}").json()["latest"]["facts"]


def test_analysis_uses_rebuilt_sources_not_saved_witness(client, samples):
    # The stored report witness is never reused: re-requesting returns
    # LP-vertex witnesses that differ from the report witness whenever the
    # polytope has slack.
    pkg, inst, report = _feasible_report(client, samples, "rounding")
    saved = {w["concept"]: w["true_value"] for w in report["witness"]}
    analysis = _analysis(
        client, report["report_id"], "acct:Cash", "acct:Assets"
    ).json()
    min_w = {w["concept"]: w["true_value"] for w in analysis["min_witness"]}
    max_w = {w["concept"]: w["true_value"] for w in analysis["max_witness"]}
    assert min_w != saved or max_w != saved
    # the two endpoints are genuinely different points
    assert min_w != max_w


def test_denominator_never_positive_is_rejected(client, samples):
    pkg = create_package(client, samples)
    # custom instance with every feasible Assets value strictly negative
    xml = _instance({
        "Cash": (-100, 0), "Receivables": (-60, 0), "Inventory": (-40, 0),
        "CurrentAssets": (-200, 0), "NonCurrentAssets": (-300, 0),
        "Assets": (-500, 0), "Revenues": (800, "INF"),
        "Expenses": (500, "INF"), "NetIncome": (300, "INF"),
    })
    inst = create_instance(client, pkg["package_id"], xml)
    report = check(client, inst["instance_id"], 1, 1).json()["report"]
    assert report["status"] == "feasible"
    r = _analysis(client, report["report_id"],
                  "acct:Cash", "acct:Assets", expected=422)
    assert r.json()["error"]["code"] == "denominator_not_positive"
    # nothing was persisted
    listing = client.get(
        f"/reports/{report['report_id']}/ratio-analyses"
    ).json()
    assert listing["analyses"] == []


def test_denominator_crossing_zero_accepted_when_positive_exists(client, samples):
    pkg = create_package(client, samples)
    # Assets box includes zero and negative values, but the equations also
    # admit positive Assets -> the ratio is well defined.
    xml = _instance({
        "Cash": (100, 0), "Receivables": (60, 0), "Inventory": (40, 0),
        "CurrentAssets": (200, 0), "NonCurrentAssets": (300, 0),
        "Assets": (500, -2),  # decimals=-2 -> [450, 550], always positive
        "Revenues": (800, "INF"), "Expenses": (500, "INF"),
        "NetIncome": (300, "INF"),
    })
    inst = create_instance(client, pkg["package_id"], xml)
    report = check(client, inst["instance_id"], 1, 1).json()["report"]
    r = _analysis(client, report["report_id"],
                  "acct:Ghost", "acct:Assets", expected=422)
    assert r.json()["error"]["code"] == "unknown_concept"
    ok = _analysis(
        client, report["report_id"], "acct:Cash", "acct:Assets"
    ).json()
    assert ok["max_witness"] and ok["min_ratio"] > 0


def test_infeasible_and_pending_reports_reject_analysis(client, samples):
    pkg = create_package(client, samples)
    for scenario, code in (("infeasible", "report_not_feasible"),
                           ("pending", "report_not_feasible")):
        inst = create_instance(client, pkg["package_id"], samples[scenario])
        report = check(client, inst["instance_id"], 1, 1).json()["report"]
        r = _analysis(client, report["report_id"],
                      "acct:Cash", "acct:Assets", expected=422)
        assert r.json()["error"]["code"] == code


def test_same_numerator_and_denominator_rejected(client, samples):
    _pkg, _inst, report = _feasible_report(client, samples)
    r = _analysis(client, report["report_id"],
                  "acct:Assets", "acct:Assets", expected=422)
    assert r.json()["error"]["code"] == "bad_payload"


def test_unknown_report_and_concept_404_and_422(client, samples):
    _pkg, _inst, report = _feasible_report(client, samples)
    assert client.post(
        "/reports/rpt_nope/ratio-analyses",
        json={"numerator_concept": "acct:Cash",
              "denominator_concept": "acct:Assets"},
    ).status_code == 404
    r = _analysis(client, report["report_id"],
                  "acct:Ghost", "acct:Assets", expected=422)
    assert r.json()["error"]["code"] == "unknown_concept"


def test_source_correction_blocks_publish_but_keeps_history(client, samples):
    pkg, inst, report = _feasible_report(client, samples)
    analysis = _analysis(
        client, report["report_id"], "acct:NetIncome", "acct:Revenues"
    ).json()

    # instance correction while calculations are still accepted
    r = client.post(
        f"/instances/{inst['instance_id']}/revisions",
        headers={"Expected-Package-Revision": "1"},
        files={"instance": ("i.xml", samples["rounding"], "application/xml")},
    )
    assert r.status_code == 200

    # any source corrected -> publishing a new analysis is refused (412)
    r = client.post(
        f"/reports/{report['report_id']}/ratio-analyses",
        json={"numerator_concept": "acct:Cash", "denominator_concept": "acct:Assets"},
    )
    assert r.status_code == 412
    assert r.json()["error"]["code"] == "stale_revision"
    # no half-written analysis landed
    assert len(client.get(
        f"/reports/{report['report_id']}/ratio-analyses").json()["analyses"]) == 1

    # historical analysis is still readable and gets marked stale; the
    # original report is not rewritten.
    got = client.get(f"/ratio-analyses/{analysis['analysis_id']}").json()
    assert got["stale"] is True
    assert "instance revision" in got["stale_reason"]
    assert client.get(f"/reports/{report['report_id']}").json()[
        "report_id"] == report["report_id"]
    # idempotent: reading again keeps the stale marker
    assert client.get(
        f"/ratio-analyses/{analysis['analysis_id']}").json()["stale"] is True


def test_package_correction_blocks_publish_and_marks_stale(client, samples):
    pkg, inst, report = _feasible_report(client, samples)
    analysis = _analysis(
        client, report["report_id"], "acct:Cash", "acct:Assets"
    ).json()

    r = client.post(
        f"/packages/{pkg['package_id']}/revisions",
        headers={"Expected-Revision": "1"},
        files={
            "xsd": ("acct.xsd", samples["xsd"], "application/xml"),
            "calculation": ("acct-calc.xml", samples["calc"], "application/xml"),
        },
    )
    assert r.status_code == 200
    r = client.post(
        f"/reports/{report['report_id']}/ratio-analyses",
        json={"numerator_concept": "acct:Cash", "denominator_concept": "acct:Assets"},
    )
    assert r.status_code == 412
    got = client.get(f"/ratio-analyses/{analysis['analysis_id']}").json()
    assert got["stale"] is True and "package revision" in got["stale_reason"]


def test_new_report_on_corrected_sources_allows_fresh_analysis(client, samples):
    pkg, inst, first = _feasible_report(client, samples)
    old_analysis = _analysis(
        client, first["report_id"], "acct:NetIncome", "acct:Revenues"
    ).json()

    client.post(
        f"/instances/{inst['instance_id']}/revisions",
        headers={"Expected-Package-Revision": "1"},
        files={"instance": ("i.xml", samples["rounding"], "application/xml")},
    )
    second = check(client, inst["instance_id"], 2, 1).json()["report"]
    assert second["status"] == "feasible"
    fresh = _analysis(
        client, second["report_id"], "acct:NetIncome", "acct:Revenues"
    ).json()
    assert fresh["stale"] is False
    assert fresh["instance_revision"] == 2
    # old analysis remains available, now stale, and unchanged in content
    got = client.get(f"/ratio-analyses/{old_analysis['analysis_id']}").json()
    assert got["stale"] is True
    assert got["min_ratio"] == old_analysis["min_ratio"]

    listing = client.get(
        f"/reports/{second['report_id']}/ratio-analyses").json()
    assert [a["analysis_id"] for a in listing["analyses"]] == \
        [fresh["analysis_id"]]


def test_late_report_rejects_analysis_its_sources_are_behind(client, samples):
    pkg = create_package(client, samples)
    inst = create_instance(client, pkg["package_id"], samples["feasible"])
    first = check(client, inst["instance_id"], 1, 1).json()["report"]
    assert first["status"] == "feasible"
    # correction to rev 2 then a current report
    client.post(
        f"/instances/{inst['instance_id']}/revisions",
        headers={"Expected-Package-Revision": "1"},
        files={"instance": ("i.xml", samples["rounding"], "application/xml")},
    )
    check(client, inst["instance_id"], 2, 1)
    # a late, still-feasible report deliberately targeting old revision 1
    late = check(client, inst["instance_id"], 1, 1, late=True).json()["report"]
    assert late["late"] is True and late["status"] == "feasible"
    r = client.post(
        f"/reports/{late['report_id']}/ratio-analyses",
        json={"numerator_concept": "acct:Cash",
              "denominator_concept": "acct:Assets"},
    )
    assert r.status_code == 412
    assert r.json()["error"]["code"] == "stale_revision"


def test_analyses_survive_restart(client, samples, tmp_path):
    from app import db
    pkg, inst, report = _feasible_report(client, samples)
    analysis = _analysis(
        client, report["report_id"], "acct:NetIncome", "acct:Revenues"
    ).json()

    db.reset_conn()
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c2:
        got = c2.get(f"/ratio-analyses/{analysis['analysis_id']}")
        assert got.status_code == 200
        body = got.json()
        assert body["min_ratio"] == analysis["min_ratio"]
        assert body["max_ratio"] == analysis["max_ratio"]
        assert len(body["min_witness"]) == 9


def _instance(values: dict[int, tuple], decimals_attr=None) -> bytes:
    facts = []
    for name, (value, dec) in values.items():
        d = "INF" if dec == "INF" else dec
        facts.append(
            f'<acct:{name} contextRef="ctx1" unitRef="usd" decimals="{d}">'
            f'{value}</acct:{name}>'
        )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:acct="http://example.com/acct"
            xmlns:xbrli="http://www.xbrl.org/2003/instance"
            xmlns:iso4217="http://www.xbrl.org/2003/iso4217">
  <xbrli:context id="ctx1">
    <xbrli:entity><xbrli:identifier scheme="http://example.com/ids">E-001</xbrli:identifier></xbrli:entity>
    <xbrli:period><xbrli:instant>2026-03-31</xbrli:instant></xbrli:period>
  </xbrli:context>
  <xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
  {''.join(facts)}
</xbrli:xbrl>""".encode()
