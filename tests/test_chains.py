"""链归并与冲突识别的纯单元测试（不经过 HTTP）。"""
from __future__ import annotations

import json
import sqlite3

from app import chains, db


def _conn(tmp_path, monkeypatch) -> sqlite3.Connection:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "chains.db"))
    from app import config
    config.DB_PATH = tmp_path / "chains.db"
    conn = db.connect()
    db.init_db(conn)
    return conn


def _insert(conn, no, ntype, ref=None, deadline=None, project="P"):
    conn.execute(
        "INSERT INTO announcements (notice_no, project_no, notice_type, "
        "deadline, ref_notice_no, last_synced_at) VALUES (?, ?, ?, ?, ?, ?)",
        (no, project, ntype, deadline, ref, "2026-11-01 00:00:00"),
    )
    conn.commit()


def _map(conn):
    return {
        r["notice_no"]: dict(r)
        for r in conn.execute("SELECT * FROM announcements")
    }


def _reasons(row) -> list[str]:
    raw = row["conflict_reasons"]
    if not raw:
        return []
    value = json.loads(raw)
    return value if isinstance(value, list) else []


def test_self_loop_is_cycle_orphan(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    _insert(conn, "S-1", chains.CORRECTION, ref="S-1")
    chains.recompute(conn)
    row = _map(conn)["S-1"]
    assert row["chain_id"] is None
    assert row["chain_status"] == "orphan"
    assert "reference_cycle" in row["conflict_reasons"]


def test_first_notice_with_reference_flagged(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    _insert(conn, "R-1", chains.FIRST, ref="R-0", deadline="2026-11-20 09:30:00")
    chains.recompute(conn)
    row = _map(conn)["R-1"]
    # 引用的上级不存在，同时触发两个原因
    assert "first_notice_with_reference" in row["conflict_reasons"]
    assert "missing_reference" in row["conflict_reasons"]
    assert row["chain_status"] == "conflict"
    assert row["effective_deadline"] is None


def test_entry_after_revocation_becomes_orphan(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    _insert(conn, "V-1", chains.FIRST, deadline="2026-11-20 09:30:00")
    _insert(conn, "V-2", chains.REVOCATION, ref="V-1")
    _insert(conn, "V-3", chains.CORRECTION, ref="V-2",
            deadline="2026-12-01 09:30:00")
    chains.recompute(conn)
    rows = _map(conn)

    assert rows["V-1"]["chain_id"] == "V-1"
    assert rows["V-2"]["chain_id"] == "V-1"
    assert rows["V-1"]["chain_status"] == "cancelled"
    # 撤销之后挂接的公告不属于该链
    assert rows["V-3"]["chain_id"] is None
    assert "entry_after_revocation" in rows["V-3"]["conflict_reasons"]
    assert rows["V-3"]["blocked_by_notice_no"] == "V-2"


def test_parallel_corrections_after_revocation_do_not_conflict_chain(
    tmp_path, monkeypatch
):
    """两条更正并行引用撤销公告：原链保持 cancelled，后继孤立而非冲突。"""
    conn = _conn(tmp_path, monkeypatch)
    _insert(conn, "P-1", chains.FIRST, deadline="2026-11-20 09:30:00")
    _insert(conn, "P-2", chains.REVOCATION, ref="P-1")
    _insert(conn, "P-3", chains.CORRECTION, ref="P-2",
            deadline="2026-12-01 09:30:00")
    _insert(conn, "P-4", chains.CORRECTION, ref="P-2",
            deadline="2026-12-02 09:30:00")
    chains.recompute(conn)
    rows = _map(conn)

    # 原链仍是已撤销状态，空截止时间，且不报并行冲突
    assert rows["P-1"]["chain_status"] == "cancelled"
    assert rows["P-1"]["effective_deadline"] is None
    for no in ("P-1", "P-2"):
        assert "parallel_correction" not in _reasons(rows[no])
    # 两条撤销后后继作为孤立异常，阻断撤销编号都为 P-2
    for no in ("P-3", "P-4"):
        assert rows[no]["chain_id"] is None
        assert "entry_after_revocation" in _reasons(rows[no])
        assert "parallel_correction" not in _reasons(rows[no])
        assert rows[no]["blocked_by_notice_no"] == "P-2"


def test_multi_level_successor_after_revocation_traces_blocker(
    tmp_path, monkeypatch
):
    """隔代后继（父节点本身也是撤销后的孤立项）仍溯源到阻断撤销编号。"""
    conn = _conn(tmp_path, monkeypatch)
    _insert(conn, "G-1", chains.FIRST, deadline="2026-11-20 09:30:00")
    _insert(conn, "G-2", chains.REVOCATION, ref="G-1")
    _insert(conn, "G-3", chains.CORRECTION, ref="G-2",
            deadline="2026-12-01 09:30:00")
    _insert(conn, "G-4", chains.CORRECTION, ref="G-3",
            deadline="2026-12-03 09:30:00")
    chains.recompute(conn)
    rows = _map(conn)

    for no in ("G-3", "G-4"):
        assert rows[no]["chain_id"] is None
        assert "entry_after_revocation" in _reasons(rows[no])
        assert rows[no]["blocked_by_notice_no"] == "G-2"
    assert rows["G-1"]["chain_status"] == "cancelled"
    assert rows["G-1"]["effective_deadline"] is None


def test_parallel_corrections_before_revocation_still_conflict(
    tmp_path, monkeypatch
):
    """撤销之前的并行更正仍必须使原链报冲突。"""
    conn = _conn(tmp_path, monkeypatch)
    _insert(conn, "B-1", chains.FIRST, deadline="2026-11-20 09:30:00")
    _insert(conn, "B-2", chains.CORRECTION, ref="B-1",
            deadline="2026-11-25 09:30:00")
    _insert(conn, "B-3", chains.CORRECTION, ref="B-1",
            deadline="2026-11-26 09:30:00")
    _insert(conn, "B-4", chains.REVOCATION, ref="B-2")
    chains.recompute(conn)
    rows = _map(conn)

    assert rows["B-1"]["chain_status"] == "conflict"
    assert rows["B-1"]["effective_deadline"] is None
    for no in ("B-1", "B-2", "B-3"):
        assert "parallel_correction" in _reasons(rows[no])
    # 撤销节点仍在该链上（撤销之前的并行冲突决定链状态）
    assert rows["B-4"]["chain_id"] == "B-1"


def test_missing_deadline_on_correction_inherits(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    _insert(conn, "I-1", chains.FIRST, deadline="2026-11-20 09:30:00")
    _insert(conn, "I-2", chains.CORRECTION, ref="I-1")  # 无截止时间
    chains.recompute(conn)
    row = _map(conn)["I-1"]
    assert row["chain_status"] == "ok"
    assert row["effective_deadline"] == "2026-11-20 09:30:00"


def test_missing_reference_marks_chain_conflict(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    _insert(conn, "M-1", chains.FIRST, deadline="2026-11-20 09:30:00")
    _insert(conn, "M-2", chains.CORRECTION, ref="GHOST",
            deadline="2026-12-01 09:30:00")
    chains.recompute(conn)
    rows = _map(conn)
    # M-2 无法挂接 -> 孤立；M-1 自身仍是正常链
    assert rows["M-2"]["chain_id"] is None
    assert "missing_reference" in rows["M-2"]["conflict_reasons"]
    assert rows["M-1"]["chain_status"] == "ok"
    assert rows["M-1"]["effective_deadline"] == "2026-11-20 09:30:00"
