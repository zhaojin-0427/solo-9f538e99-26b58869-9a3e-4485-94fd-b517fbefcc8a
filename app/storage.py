"""Service / persistence operations with optimistic revision checks."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from . import db
from .errors import NotFound, StaleRevision
from .instance import parse_instance
from .ratios import ratio_range
from .solver import check_facts
from .taxonomy import parse_package


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


# ---------------------------------------------------------------- packages
def create_package(name: str, xsd: bytes, calc: bytes) -> dict:
    info = parse_package(xsd, calc)
    conn = db.get_conn()
    pid = _new_id("pkg")
    now = _now()
    with conn:
        conn.execute(
            "INSERT INTO packages(id, name, latest_rev, created_at, updated_at) "
            "VALUES (?,?,1,?,?)",
            (pid, name, now, now),
        )
        conn.execute(
            "INSERT INTO package_revisions(package_id, revision, xsd_raw, calc_raw, "
            "target_ns, prefix, concepts_json, equations_json, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (pid, 1, xsd, calc, info["target_ns"], info["prefix"],
             json.dumps(info["concepts"]), json.dumps(info["equations"]), now),
        )
    return get_package(pid)


def revise_package(pid: str, expected_revision: int, xsd: bytes, calc: bytes) -> dict:
    conn = db.get_conn()
    row = conn.execute("SELECT latest_rev FROM packages WHERE id=?", (pid,)).fetchone()
    if row is None:
        raise NotFound(f"package '{pid}' not found")
    if row["latest_rev"] != expected_revision:
        raise StaleRevision(
            f"expected package revision {expected_revision}, server has {row['latest_rev']}"
        )
    info = parse_package(xsd, calc)
    new_rev = expected_revision + 1
    now = _now()
    with conn:
        conn.execute(
            "INSERT INTO package_revisions(package_id, revision, xsd_raw, calc_raw, "
            "target_ns, prefix, concepts_json, equations_json, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (pid, new_rev, xsd, calc, info["target_ns"], info["prefix"],
             json.dumps(info["concepts"]), json.dumps(info["equations"]), now),
        )
        conn.execute(
            "UPDATE packages SET latest_rev=?, updated_at=? WHERE id=?",
            (new_rev, now, pid),
        )
    return get_package(pid)


def _package_revision_row(pid: str, revision: int) -> dict:
    row = db.get_conn().execute(
        "SELECT * FROM package_revisions WHERE package_id=? AND revision=?",
        (pid, revision),
    ).fetchone()
    if row is None:
        raise NotFound(f"package '{pid}' revision {revision} not found")
    return dict(row)


def get_package(pid: str) -> dict:
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM packages WHERE id=?", (pid,)).fetchone()
    if row is None:
        raise NotFound(f"package '{pid}' not found")
    rev = _package_revision_row(pid, row["latest_rev"])
    return {
        "package_id": row["id"],
        "name": row["name"],
        "latest_revision": row["latest_rev"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "latest": {
            "package_id": pid,
            "revision": rev["revision"],
            "target_ns": rev["target_ns"],
            "prefix": rev["prefix"],
            "concepts": json.loads(rev["concepts_json"]),
            "equations": json.loads(rev["equations_json"]),
            "created_at": rev["created_at"],
        },
    }


def list_packages() -> list[dict]:
    rows = db.get_conn().execute("SELECT id FROM packages ORDER BY created_at").fetchall()
    return [get_package(r["id"]) for r in rows]


# ---------------------------------------------------------------- instances
def create_instance(pid: str, raw: bytes) -> dict:
    conn = db.get_conn()
    if conn.execute("SELECT 1 FROM packages WHERE id=?", (pid,)).fetchone() is None:
        raise NotFound(f"package '{pid}' not found")
    pkg = get_package(pid)
    info = parse_instance(raw, pkg["latest"])
    iid = _new_id("inst")
    now = _now()
    with conn:
        conn.execute(
            "INSERT INTO instances(id, package_id, latest_rev, created_at, updated_at) "
            "VALUES (?,?,1,?,?)",
            (iid, pid, now, now),
        )
        conn.execute(
            "INSERT INTO instance_revisions(instance_id, revision, instance_raw, entity, "
            "period, currency, facts_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (iid, 1, raw, info["entity"], info["period"], info["currency"],
             json.dumps(info["facts"]), now),
        )
    return get_instance(iid)


def revise_instance(iid: str, expected_package_revision: int, raw: bytes) -> dict:
    conn = db.get_conn()
    row = conn.execute(
        "SELECT package_id, latest_rev FROM instances WHERE id=?", (iid,)
    ).fetchone()
    if row is None:
        raise NotFound(f"instance '{iid}' not found")
    pkg = get_package(row["package_id"])
    # 受理时校验两侧所见修订：实例提交必须针对当前 package 修订。
    if pkg["latest_revision"] != expected_package_revision:
        raise StaleRevision(
            f"expected package revision {expected_package_revision}, "
            f"server has {pkg['latest_revision']}"
        )
    info = parse_instance(raw, pkg["latest"])
    new_rev = row["latest_rev"] + 1
    now = _now()
    with conn:
        conn.execute(
            "INSERT INTO instance_revisions(instance_id, revision, instance_raw, entity, "
            "period, currency, facts_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (iid, new_rev, raw, info["entity"], info["period"], info["currency"],
             json.dumps(info["facts"]), now),
        )
        conn.execute(
            "UPDATE instances SET latest_rev=?, updated_at=? WHERE id=?",
            (new_rev, now, iid),
        )
    return get_instance(iid)


def _instance_revision_row(iid: str, revision: int) -> dict:
    row = db.get_conn().execute(
        "SELECT * FROM instance_revisions WHERE instance_id=? AND revision=?",
        (iid, revision),
    ).fetchone()
    if row is None:
        raise NotFound(f"instance '{iid}' revision {revision} not found")
    return dict(row)


def get_instance(iid: str) -> dict:
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone()
    if row is None:
        raise NotFound(f"instance '{iid}' not found")
    rev = _instance_revision_row(iid, row["latest_rev"])
    return {
        "instance_id": row["id"],
        "package_id": row["package_id"],
        "latest_revision": row["latest_rev"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "latest": {
            "instance_id": iid,
            "package_id": row["package_id"],
            "revision": rev["revision"],
            "entity": rev["entity"],
            "period": rev["period"],
            "currency": rev["currency"],
            "facts": json.loads(rev["facts_json"]),
            "created_at": rev["created_at"],
        },
    }


def list_instances(pid: str | None = None) -> list[dict]:
    conn = db.get_conn()
    if pid:
        rows = conn.execute(
            "SELECT id FROM instances WHERE package_id=? ORDER BY created_at", (pid,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT id FROM instances ORDER BY created_at").fetchall()
    return [get_instance(r["id"]) for r in rows]


# ---------------------------------------------------------------- reports
def _report_out(row: dict) -> dict:
    return {
        "report_id": row["id"],
        "instance_id": row["instance_id"],
        "package_id": row["package_id"],
        "instance_revision": row["instance_revision"],
        "package_revision": row["package_revision"],
        "status": row["status"],
        "is_correction": bool(row["is_correction"]),
        "superseded": bool(row["superseded"]),
        "late": bool(row["late"]),
        "witness": json.loads(row["witness_json"]) if row["witness_json"] else None,
        "conflicts": json.loads(row["conflicts_json"]) if row["conflicts_json"] else None,
        "missing_facts": json.loads(row["missing_json"]) if row["missing_json"] else None,
        "detail": json.loads(row["detail_json"]),
        "created_at": row["created_at"],
        "has_instance_snapshot": bool(row["instance_snapshot"]),
        "has_xsd_snapshot": bool(row["xsd_snapshot"]),
        "has_calc_snapshot": bool(row["calc_snapshot"]),
    }


def run_check(iid: str, expected_instance_revision: int,
              expected_package_revision: int, late: bool = False) -> dict:
    conn = db.get_conn()
    inst_row = conn.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone()
    if inst_row is None:
        raise NotFound(f"instance '{iid}' not found")

    pkg = get_package(inst_row["package_id"])

    # 受理时校验两侧所见修订：
    # - 客户端所见修订领先于服务器 → 过期拒绝（412）
    # - 落后于服务器：默认仍拒绝；仅当显式声明为迟到补报时受理，仅存历史。
    if expected_instance_revision > inst_row["latest_rev"]:
        raise StaleRevision(
            f"expected instance revision {expected_instance_revision} is ahead of "
            f"server latest {inst_row['latest_rev']}"
        )
    if expected_package_revision > pkg["latest_revision"]:
        raise StaleRevision(
            f"expected package revision {expected_package_revision} is ahead of "
            f"server latest {pkg['latest_revision']}"
        )
    behind = (expected_instance_revision < inst_row["latest_rev"]
              or expected_package_revision < pkg["latest_revision"])
    if behind and not late:
        raise StaleRevision(
            f"revisions ({expected_instance_revision},"
            f"{expected_package_revision}) are behind server latest "
            f"({inst_row['latest_rev']},{pkg['latest_revision']}); "
            f"re-check with late=true to file a late historical report"
        )

    i_rev = _instance_revision_row(iid, expected_instance_revision)
    p_rev = _package_revision_row(inst_row["package_id"], expected_package_revision)

    # 迟到报告：依据的修订已落后于两侧最新组合，仅存历史，不取代当前报告。
    is_late = behind
    facts = json.loads(i_rev["facts_json"])
    equations = json.loads(p_rev["equations_json"])

    result = check_facts(facts, equations)

    now = _now()
    report_id = _new_id("rpt")
    prior = conn.execute(
        "SELECT id FROM reports WHERE instance_id=? ORDER BY created_at, rowid", (iid,)
    ).fetchall()
    is_correction = len(prior) > 0

    detail = {
        "entity": i_rev["entity"],
        "period": i_rev["period"],
        "currency": i_rev["currency"],
        "fact_count": len(facts),
        "equation_count": len(equations),
        "solver": "scipy.optimize.linprog(highs)",
    }
    payload = {
        "id": report_id,
        "instance_id": iid,
        "package_id": inst_row["package_id"],
        "instance_revision": i_rev["revision"],
        "package_revision": p_rev["revision"],
        "status": result["status"],
        "is_correction": 1 if is_correction else 0,
        "late": 1 if is_late else 0,
        "witness_json": json.dumps(result["witness"]) if "witness" in result else None,
        "conflicts_json": json.dumps(result["conflicts"]) if "conflicts" in result else None,
        "missing_json": json.dumps(result["missing_facts"]) if "missing_facts" in result else None,
        "detail_json": json.dumps(detail),
        "instance_snapshot": i_rev["instance_raw"],
        "xsd_snapshot": p_rev["xsd_raw"],
        "calc_snapshot": p_rev["calc_raw"],
        "created_at": now,
    }

    superseded_ids: list[str] = []
    with conn:
        conn.execute(
            "INSERT INTO reports(id, instance_id, package_id, instance_revision, "
            "package_revision, status, is_correction, superseded, late, witness_json, "
            "conflicts_json, missing_json, detail_json, instance_snapshot, xsd_snapshot, "
            "calc_snapshot, created_at) VALUES (:id,:instance_id,:package_id,"
            ":instance_revision,:package_revision,:status,:is_correction,0,:late,"
            ":witness_json,:conflicts_json,:missing_json,:detail_json,:instance_snapshot,"
            ":xsd_snapshot,:calc_snapshot,:created_at)",
            payload,
        )
        if not is_late:
            old = conn.execute(
                "SELECT id FROM reports WHERE instance_id=? AND id!=? AND superseded=0",
                (iid, report_id),
            ).fetchall()
            superseded_ids = [r["id"] for r in old]
            conn.execute(
                "UPDATE reports SET superseded=1 WHERE instance_id=? AND id!=?",
                (iid, report_id),
            )

    out = _report_out(dict(conn.execute(
        "SELECT * FROM reports WHERE id=?", (report_id,)
    ).fetchone()))
    return {"report": out, "superseded_report_ids": superseded_ids}


def get_report(rid: str) -> dict:
    row = db.get_conn().execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
    if row is None:
        raise NotFound(f"report '{rid}' not found")
    return _report_out(dict(row))


def get_report_snapshot(rid: str, kind: str) -> tuple[bytes, str]:
    row = db.get_conn().execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
    if row is None:
        raise NotFound(f"report '{rid}' not found")
    column = {"instance": "instance_snapshot", "xsd": "xsd_snapshot",
              "calc": "calc_snapshot"}[kind]
    media = {"instance": "application/xml", "xsd": "application/xml",
             "calc": "application/xml"}[kind]
    return bytes(row[column]), media


def list_reports(iid: str) -> dict:
    conn = db.get_conn()
    if conn.execute("SELECT 1 FROM instances WHERE id=?", (iid,)).fetchone() is None:
        raise NotFound(f"instance '{iid}' not found")
    rows = conn.execute(
        "SELECT * FROM reports WHERE instance_id=? ORDER BY created_at, rowid", (iid,)
    ).fetchall()
    history = [_report_out(dict(r)) for r in rows]
    # 迟到报告仅存历史，永远不作为当前报告。
    current = next(
        (r for r in reversed(history) if not r["superseded"] and not r["late"]), None
    )
    return {"instance_id": iid, "current": current, "history": history}


# ---------------------------------------------------------------- ratio analyses
def _analysis_out(row: dict) -> dict:
    return {
        "analysis_id": row["id"],
        "report_id": row["report_id"],
        "instance_id": row["instance_id"],
        "package_id": row["package_id"],
        "instance_revision": row["instance_revision"],
        "package_revision": row["package_revision"],
        "numerator_concept": row["numerator_concept"],
        "denominator_concept": row["denominator_concept"],
        "min_ratio": row["min_ratio"],
        "max_ratio": row["max_ratio"],
        "denominator_max_feasible": row["denominator_max_feasible"],
        "min_witness": json.loads(row["min_witness_json"]),
        "max_witness": json.loads(row["max_witness_json"]),
        "detail": json.loads(row["detail_json"]),
        "stale": bool(row["stale"]),
        "stale_reason": row["stale_reason"],
        "created_at": row["created_at"],
    }


def _source_staleness(conn, instance_id: str, package_id: str,
                      instance_revision: int, package_revision: int) -> Optional[str]:
    """Return why the given source revisions are no longer current, or None."""
    inst = conn.execute(
        "SELECT latest_rev FROM instances WHERE id=?", (instance_id,)
    ).fetchone()
    if inst is not None and inst["latest_rev"] > instance_revision:
        return (
            f"instance revision {instance_revision} superseded by "
            f"{inst['latest_rev']}"
        )
    pkg = conn.execute(
        "SELECT latest_rev FROM packages WHERE id=?", (package_id,)
    ).fetchone()
    if pkg is not None and pkg["latest_rev"] > package_revision:
        return (
            f"package revision {package_revision} superseded by "
            f"{pkg['latest_rev']}"
        )
    return None


def _rebuild_sources(report: dict) -> tuple[list[dict], list[dict]]:
    """Rebuild the feasible truth-set inputs from the report's own snapshots."""
    package_info = parse_package(
        bytes(report["xsd_snapshot"]), bytes(report["calc_snapshot"])
    )
    instance_info = parse_instance(bytes(report["instance_snapshot"]), package_info)
    return instance_info["facts"], package_info["equations"]


def create_ratio_analysis(rid: str, numerator: str, denominator: str) -> dict:
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
    if row is None:
        raise NotFound(f"report '{rid}' not found")
    report = dict(row)

    if report["status"] != "feasible":
        from .errors import BadPayload
        raise BadPayload(
            f"ratio analysis requires a feasible report; report status is "
            f"'{report['status']}'",
            code="report_not_feasible",
        )

    # 发布前强校验来源修订：任一来源已更正即拒绝发布（412）。
    reason = _source_staleness(
        conn, report["instance_id"], report["package_id"],
        report["instance_revision"], report["package_revision"],
    )
    if reason is not None:
        raise StaleRevision(
            f"report '{rid}' is based on corrected sources: {reason}; "
            f"publish a new report before creating an analysis"
        )

    # 按报告保存的实例、XSD 与计算关系重建可行真值集合（不沿用原报告见证）。
    facts, equations = _rebuild_sources(report)

    # 求解在写库之前完成：任何失败都不会留下半份分析。
    result = ratio_range(facts, equations, numerator, denominator)

    detail = {
        "entity": json.loads(report["detail_json"]).get("entity"),
        "period": json.loads(report["detail_json"]).get("period"),
        "currency": json.loads(report["detail_json"]).get("currency"),
        "fact_count": result["concept_count"],
        "equation_count": result["equation_count"],
        "solver": result["solver"],
        "sources": "rebuilt from report snapshots (instance/xsd/calc)",
    }
    analysis_id = _new_id("ana")
    now = _now()
    with conn:
        conn.execute(
            "INSERT INTO ratio_analyses(id, report_id, instance_id, package_id, "
            "instance_revision, package_revision, numerator_concept, "
            "denominator_concept, min_ratio, max_ratio, denominator_max_feasible, "
            "min_witness_json, max_witness_json, detail_json, stale, stale_reason, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,NULL,?)",
            (analysis_id, rid, report["instance_id"], report["package_id"],
             report["instance_revision"], report["package_revision"],
             numerator, denominator, result["min_ratio"], result["max_ratio"],
             result["denominator_max_feasible"],
             json.dumps(result["min_witness"]),
             json.dumps(result["max_witness"]),
             json.dumps(detail), now),
        )
    saved = conn.execute(
        "SELECT * FROM ratio_analyses WHERE id=?", (analysis_id,)
    ).fetchone()
    return _analysis_out(dict(saved))


def _refresh_staleness(conn, row: dict) -> dict:
    """Lazily flag an analysis stale when either source was corrected."""
    if not row["stale"]:
        reason = _source_staleness(
            conn, row["instance_id"], row["package_id"],
            row["instance_revision"], row["package_revision"],
        )
        if reason is not None:
            with conn:
                conn.execute(
                    "UPDATE ratio_analyses SET stale=1, stale_reason=? WHERE id=?",
                    (reason, row["id"]),
                )
            row["stale"] = 1
            row["stale_reason"] = reason
    return row


def get_ratio_analysis(aid: str) -> dict:
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM ratio_analyses WHERE id=?", (aid,)).fetchone()
    if row is None:
        raise NotFound(f"ratio analysis '{aid}' not found")
    row = _refresh_staleness(conn, dict(row))
    return _analysis_out(row)


def list_ratio_analyses(rid: str) -> dict:
    conn = db.get_conn()
    if conn.execute("SELECT 1 FROM reports WHERE id=?", (rid,)).fetchone() is None:
        raise NotFound(f"report '{rid}' not found")
    rows = conn.execute(
        "SELECT * FROM ratio_analyses WHERE report_id=? ORDER BY created_at, rowid",
        (rid,),
    ).fetchall()
    return {
        "report_id": rid,
        "analyses": [_analysis_out(_refresh_staleness(conn, dict(r))) for r in rows],
    }
