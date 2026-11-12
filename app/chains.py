"""引用链归并、冲突识别与有效截止时间计算。

业务规则
--------
1. 公告分三类：first（首次公告）、correction（更正）、revocation（撤销）。
   更正/撤销通过「引用编号」指向上一条公告，沿引用反向追溯得到一条链，
   链标识取链上首次公告的公告编号。
2. 同一项目编号下可以有多条独立的首次公告，彼此保持独立、不合并。
3. 出现以下情况之一，链上相关公告标记为冲突（conflict），且**不推测截止
   时间**（有效截止时间为 null）：
     - missing_reference：引用编号在库中不存在；
     - reference_cycle：引用关系成环；
     - parallel_correction：同一公告存在两个及以上并行后继（并行更正）；
     - first_notice_with_reference：首次公告却带引用编号（结构异常）。
   注意：并行更正只在链内（撤销边界之前）才构成冲突。撤销公告之后出现的
   分叉后继不进入原链，因此不能改变原链的已撤销状态，按孤立异常处理。
4. 撤销使整条链失效：链状态为 cancelled，有效截止时间为 null；
   撤销之后又挂接的公告（直接后继及任意多级后继）不再属于该链，统一作为
   孤立异常处理，原因 entry_after_revocation，并记录阻断它们的撤销编号。
5. 无法归入任何链的公告进入 orphans（孤立异常公告）。
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict, deque
from typing import Optional

FIRST = "first"
CORRECTION = "correction"
REVOCATION = "revocation"

# 冲突原因常量
MISSING_REFERENCE = "missing_reference"
REFERENCE_CYCLE = "reference_cycle"
PARALLEL_CORRECTION = "parallel_correction"
FIRST_NOTICE_WITH_REFERENCE = "first_notice_with_reference"
ENTRY_AFTER_REVOCATION = "entry_after_revocation"
UNREACHABLE = "unreachable"


def recompute(conn: sqlite3.Connection) -> None:
    """依据库内全部公告重新计算链归属、状态与有效截止时间，并落库。"""
    rows = conn.execute(
        "SELECT * FROM announcements ORDER BY id"
    ).fetchall()
    notices = {r["notice_no"]: dict(r) for r in rows}

    conflicts: dict[str, set[str]] = defaultdict(set)
    # 被引用编号 -> 后继公告列表（仅引用存在时）
    successors: dict[str, list[str]] = defaultdict(list)
    for n in notices.values():
        ref = n["ref_notice_no"]
        if n["notice_type"] != FIRST and not ref:
            # 更正/撤销必须引用上级公告，缺失即冲突
            conflicts[n["notice_no"]].add(MISSING_REFERENCE)
        if ref:
            if ref not in notices:
                conflicts[n["notice_no"]].add(MISSING_REFERENCE)
            else:
                successors[ref].append(n["notice_no"])

    # 首次公告带引用：结构异常
    for n in notices.values():
        if n["notice_type"] == FIRST and n["ref_notice_no"]:
            conflicts[n["notice_no"]].add(FIRST_NOTICE_WITH_REFERENCE)

    # 引用成环检测：沿 ref 向上追溯，重复访问即环
    _mark_cycles(notices, conflicts)

    # 找根：首次公告且自身不在环中（环内节点全部走孤立流程）
    roots = [
        no for no, n in notices.items()
        if n["notice_type"] == FIRST and REFERENCE_CYCLE not in conflicts[no]
    ]

    # BFS 收集每条链的成员；撤销节点之后不再向下展开。
    # 先确定链归属，再识别并行后继，确保撤销之后的分叉不参与冲突判定。
    chain_members: dict[str, list[str]] = {}
    membership: dict[str, str] = {}
    for root in roots:
        members: list[str] = []
        queue: deque[str] = deque([root])
        while queue:
            current = queue.popleft()
            if current in membership or current not in notices:
                continue
            membership[current] = root
            members.append(current)
            node = notices[current]
            # 撤销使链在此终止，其后继不再属于本链
            if node["notice_type"] == REVOCATION:
                continue
            for child in successors.get(current, []):
                if child not in membership:
                    queue.append(child)
        chain_members[root] = members

    # 并行后继只在链内（撤销边界之前）构成冲突。
    # 撤销公告自身虽在链上，但它的后继已被排除在链外，其分叉不得把原链的
    # cancelled 状态改写成 conflict；链外节点之间的分叉同理不打标。
    for parent_no, children in successors.items():
        parent = notices.get(parent_no)
        if parent is None or len(children) <= 1:
            continue
        if parent_no not in membership or parent["notice_type"] == REVOCATION:
            continue
        conflicts[parent_no].add(PARALLEL_CORRECTION)
        for child in children:
            conflicts[child].add(PARALLEL_CORRECTION)

    # 孤立公告：未归入任何链的公告
    orphans = [no for no in notices if no not in membership]

    # 撤销之后挂接的公告：直接后继与任意多级后继都沿引用向上追溯到阻断
    # 它们的撤销公告，统一给出 entry_after_revocation 与撤销编号。
    blocked_by: dict[str, Optional[str]] = {}
    for no in orphans:
        revocation_no = _find_blocking_revocation(notices, no)
        if revocation_no is not None:
            blocked_by[no] = revocation_no
            conflicts[no].add(ENTRY_AFTER_REVOCATION)
        else:
            ref = notices[no]["ref_notice_no"]
            if ref and ref in membership:
                # 兜底：父节点在链上但自己未被收编（正常结构下不应到达）
                conflicts[no].add(UNREACHABLE)

    # 计算每条链的状态与有效截止时间
    chain_updates: dict[str, tuple[str, Optional[str], Optional[str]]] = {}
    for root, members in chain_members.items():
        chain_conflict = sorted({c for no in members for c in conflicts[no]})
        cancelled = any(notices[no]["notice_type"] == REVOCATION for no in members)

        if chain_conflict:
            status = "conflict"
            effective_deadline = None
        elif cancelled:
            status = "cancelled"
            effective_deadline = None
        else:
            status = "ok"
            effective_deadline = _effective_deadline(notices, members)

        chain_updates[root] = (status, effective_deadline,
                               json.dumps(chain_conflict, ensure_ascii=False))

    # 落库：先重置全部公告的计算字段
    conn.execute(
        "UPDATE announcements SET chain_id = NULL, chain_status = 'orphan', "
        "conflict_reasons = NULL, effective_deadline = NULL, "
        "blocked_by_notice_no = NULL"
    )
    for root, members in chain_members.items():
        status, effective, reasons_json = chain_updates[root]
        for no in members:
            per_notice = sorted(conflicts[no])
            conn.execute(
                "UPDATE announcements SET chain_id = ?, chain_status = ?, "
                "conflict_reasons = ?, effective_deadline = ? WHERE notice_no = ?",
                (
                    root,
                    status,
                    json.dumps(per_notice, ensure_ascii=False)
                    if per_notice else None,
                    effective if not per_notice and status == "ok" else None,
                    no,
                ),
            )
    for no in orphans:
        per_notice = sorted(conflicts[no])
        conn.execute(
            "UPDATE announcements SET chain_id = NULL, chain_status = 'orphan', "
            "conflict_reasons = ?, effective_deadline = NULL, "
            "blocked_by_notice_no = ? WHERE notice_no = ?",
            (
                json.dumps(per_notice, ensure_ascii=False) if per_notice else None,
                blocked_by.get(no),
                no,
            ),
        )
    conn.commit()


def _find_blocking_revocation(
    notices: dict[str, dict], notice_no: str
) -> Optional[str]:
    """沿引用链向上追溯阻断该公告的撤销公告编号，不存在则返回 None。

    适用于撤销之后挂接的直接及多级后继：无论间隔多少层，只要上游存在
    撤销公告，就返回沿引用路径遇到的第一个（最近的）撤销编号。
    追溯过程中遇到缺失引用或回到自身（成环）即停止，避免无限循环。
    """
    current = notices[notice_no].get("ref_notice_no")
    visited: set[str] = set()
    while current and current in notices and current not in visited:
        visited.add(current)
        node = notices[current]
        if node["notice_type"] == REVOCATION:
            return current
        current = node["ref_notice_no"]
    return None


def _mark_cycles(
    notices: dict[str, dict], conflicts: dict[str, set[str]]
) -> None:
    """检测引用环，环上每个公告都标记 reference_cycle。"""
    state: dict[str, int] = {}  # 0=访问中 1=已完成
    stack: list[str] = []
    in_stack: set[str] = set()

    def visit(no: str) -> None:
        state[no] = 0
        stack.append(no)
        in_stack.add(no)
        ref = notices[no]["ref_notice_no"]
        if ref and ref in notices:
            mark = state.get(ref)
            if mark == 0:
                # 发现环：从环入口到栈顶全部标记
                idx = stack.index(ref)
                for cyclic in stack[idx:]:
                    conflicts[cyclic].add(REFERENCE_CYCLE)
            elif mark is None:
                visit(ref)
        stack.pop()
        in_stack.discard(no)
        state[no] = 1

    for no in notices:
        if state.get(no) is None:
            visit(no)


def _effective_deadline(
    notices: dict[str, dict], members: list[str]
) -> Optional[str]:
    """计算链的有效截止时间。

    沿链从首次公告向后续公告推进，取链上明确给出的最新截止时间；
    更正未给出截止时间时沿用链上此前的截止时间（继承而非推测）。
    """
    deadline: Optional[str] = None
    for no in members:
        node_deadline = notices[no]["deadline"]
        if node_deadline:
            if deadline is None or node_deadline > deadline:
                deadline = node_deadline
    return deadline
