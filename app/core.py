# -*- coding: utf-8 -*-
"""核心领域服务：发放计划、首发唯一、库存竞争、地址截止、版次升级、
异常补发审批（职责分离 + 幂等）、退课拦截、模拟物流回调。"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime

from . import db as dbmod


class BusinessError(Exception):
    """业务规则冲突，消息可直接展示给前台/学员。"""


class FirstIssuanceExists(BusinessError):
    pass


class InsufficientStock(BusinessError):
    pass


class ResendAlreadyIssued(BusinessError):
    pass


def today() -> date:
    return date.today()


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _rollback_safe(conn: sqlite3.Connection) -> None:
    """回滚；若事务已结束（内层已回滚或已提交）则静默。"""
    try:
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError:
        pass


# ---------------------------------------------------------------- 内部工具

def _deduct_stock(conn: sqlite3.Connection, items: list[dict],
                  reason: str, ref: str, actor: str) -> None:
    """原子条件扣减：库存不足则整体回滚。高并发下靠条件 UPDATE 串行化。"""
    for it in items:
        cur = conn.execute(
            "UPDATE inventory SET qty = qty - ? "
            "WHERE textbook_code = ? AND edition = ? AND qty >= ?",
            (it["qty"], it["textbook_code"], it["edition"], it["qty"]),
        )
        if cur.rowcount == 0:
            row = conn.execute(
                "SELECT qty FROM inventory WHERE textbook_code=? AND edition=?",
                (it["textbook_code"], it["edition"]),
            ).fetchone()
            have = row["qty"] if row else 0
            raise InsufficientStock(
                f"教材 {it['textbook_code']}（{it['edition']}版）库存不足："
                f"需要 {it['qty']}，现有 {have}")
        conn.execute(
            "INSERT INTO stock_movements"
            "(textbook_code,edition,change,reason,ref,actor,at) "
            "VALUES(?,?,?,?,?,?,?)",
            (it["textbook_code"], it["edition"], -it["qty"],
             reason, ref, actor, now_iso()),
        )


def _restock(conn: sqlite3.Connection, items: list[sqlite3.Row],
             reason: str, ref: str, actor: str) -> None:
    for it in items:
        conn.execute(
            "INSERT INTO inventory(textbook_code,edition,qty) VALUES(?,?,?) "
            "ON CONFLICT(textbook_code,edition) DO UPDATE SET qty=qty+?",
            (it["textbook_code"], it["edition"], it["qty"], it["qty"]),
        )
        conn.execute(
            "INSERT INTO stock_movements"
            "(textbook_code,edition,change,reason,ref,actor,at) "
            "VALUES(?,?,?,?,?,?,?)",
            (it["textbook_code"], it["edition"], it["qty"],
             reason, ref, actor, now_iso()),
        )


def _required_items(conn: sqlite3.Connection, class_id: str) -> list[dict]:
    """班级当前计划教材（升级后取新版次）。"""
    rows = conn.execute(
        "SELECT cm.textbook_code, cm.edition, cm.qty, t.title "
        "FROM class_materials cm JOIN textbooks t ON t.code=cm.textbook_code "
        "WHERE cm.class_id=?",
        (class_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _active_parcel(conn: sqlite3.Connection, enrollment_id: str,
                   kinds=("first", "resend", "supplement")):
    return conn.execute(
        "SELECT * FROM parcels WHERE enrollment_id=? AND kind IN (%s) "
        "AND status NOT IN ('cancelled') ORDER BY id" %
        ",".join("?" * len(kinds)),
        (enrollment_id, *kinds),
    ).fetchall()


# ---------------------------------------------------------------- 发放计划

def pending_list(conn: sqlite3.Connection, on: date | None = None) -> list[dict]:
    """待发列表：未交运的报名，优先显示开课倒计时与缺书数量。"""
    on = on or today()
    rows = conn.execute(
        """
        SELECT e.id AS enrollment_id, e.receiver_address, e.receiver_name,
               e.receiver_phone, s.id AS student_id, s.name AS student_name,
               c.id AS class_id, c.name AS class_name, c.start_date,
               p.id AS parcel_id, p.status AS parcel_status,
               CAST(julianday(c.start_date) - julianday(?) AS INTEGER) AS countdown
        FROM enrollments e
        JOIN students s ON s.id = e.student_id
        JOIN classes c  ON c.id = e.class_id
        LEFT JOIN parcels p ON p.enrollment_id = e.id AND p.kind='first'
        WHERE e.status='enrolled'
          AND NOT EXISTS (
                SELECT 1 FROM parcels q WHERE q.enrollment_id=e.id
                  AND q.status IN ('packed','shipped','delivered'))
          AND NOT EXISTS (
                SELECT 1 FROM exceptions x WHERE x.enrollment_id=e.id
                  AND x.status='submitted')
        ORDER BY countdown ASC, e.id
        """,
        (on.isoformat(),),
    ).fetchall()
    result = []
    for r in rows:
        req = _required_items(conn, r["class_id"])
        missing = 0
        for m in req:
            inv = conn.execute(
                "SELECT qty FROM inventory WHERE textbook_code=? AND edition=?",
                (m["textbook_code"], m["edition"]),
            ).fetchone()
            avail = inv["qty"] if inv else 0
            missing += max(0, m["qty"] - avail)
        d = dict(r)
        d["missing"] = missing
        d["materials"] = req
        d["address_ready"] = bool(r["receiver_address"])
        result.append(d)
    result.sort(key=lambda x: (x["countdown"], -x["missing"], x["enrollment_id"]))
    return result


def class_plan(conn: sqlite3.Connection, class_id: str) -> dict:
    """班级发放计划：每人的计划版次、实际寄出版次、补充资料状态。"""
    cls = conn.execute("SELECT * FROM classes WHERE id=?", (class_id,)).fetchone()
    if not cls:
        raise BusinessError("班级不存在")
    materials = _required_items(conn, class_id)
    rows = conn.execute(
        """SELECT e.*, s.name AS student_name FROM enrollments e
           JOIN students s ON s.id=e.student_id
           WHERE e.class_id=? ORDER BY e.id""",
        (class_id,),
    ).fetchall()
    people = []
    for e in rows:
        items = conn.execute(
            "SELECT pi.* FROM parcel_items pi JOIN parcels p ON p.id=pi.parcel_id "
            "WHERE p.enrollment_id=? AND p.status!='cancelled' AND pi.is_supplement=0",
            (e["id"],),
        ).fetchall()
        issued = {(i["textbook_code"], i["edition"]): i["qty"] for i in items}
        sup = conn.execute(
            "SELECT * FROM supplements WHERE enrollment_id=? AND status='pending'",
            (e["id"],),
        ).fetchall()
        line_status = []
        for m in materials:
            key_new = (m["textbook_code"], m["edition"])
            qty_new = issued.get(key_new, 0)
            old_hits = [k for k in issued if k[0] == m["textbook_code"] and k[1] != m["edition"]]
            if qty_new >= m["qty"]:
                tag = "current"          # 已发新版
            elif old_hits:
                tag = "old_with_supp" if sup else "old_pending_supp"
            elif issued:
                tag = "partial"
            else:
                tag = "unissued"
            line_status.append({"material": m, "tag": tag,
                                "issued_edition": (key_new[1] if qty_new else
                                                   (old_hits[0][1] if old_hits else None))})
        parcel = conn.execute(
            "SELECT * FROM parcels WHERE enrollment_id=? AND kind='first'",
            (e["id"],),
        ).fetchone()
        people.append({"enrollment": e, "lines": line_status,
                       "supplements": sup, "parcel": parcel})
    return {"class": cls, "materials": materials, "people": people}


# ---------------------------------------------------------------- 首发 / 拣包

def create_first_issuance(conn: sqlite3.Connection, enrollment_id: str,
                          actor: str = "") -> int:
    """一个报名记录的首次发放只能创建一次（DB 唯一索引兜底）。"""
    e = conn.execute("SELECT * FROM enrollments WHERE id=?",
                     (enrollment_id,)).fetchone()
    if not e:
        raise BusinessError("报名记录不存在")
    if e["status"] == "withdrawn":
        raise BusinessError("学员已退课，不能发放教材")
    if not e["receiver_address"]:
        raise BusinessError("收货地址为空，不能成包")
    conn.execute("BEGIN IMMEDIATE")
    try:
        try:
            cur = conn.execute(
                "INSERT INTO parcels(enrollment_id,kind,status,receiver_name,"
                "receiver_phone,receiver_address,created_by,created_at) "
                "VALUES(?,?, 'packed',?,?,?,?,?)",
                (enrollment_id, "first", e["receiver_name"], e["receiver_phone"],
                 e["receiver_address"], actor, now_iso()),
            )
        except sqlite3.IntegrityError as exc:
            raise FirstIssuanceExists("该报名记录已创建过首次发放，不能重复创建") from exc
        parcel_id = cur.lastrowid
        items = _required_items(conn, e["class_id"])
        try:
            _deduct_stock(conn, items, "first_issue", f"parcel#{parcel_id}", actor)
        except InsufficientStock:
            _rollback_safe(conn)
            raise
        for m in items:
            conn.execute(
                "INSERT INTO parcel_items(parcel_id,textbook_code,edition,qty,"
                "item_title) VALUES(?,?,?,?,?)",
                (parcel_id, m["textbook_code"], m["edition"], m["qty"], m["title"]),
            )
        conn.execute(
            "INSERT INTO tracking_events(parcel_id,event,detail,at) "
            "VALUES(?,?,?,?)",
            (parcel_id, "packed", "仓库已拣书打包", now_iso()),
        )
        conn.execute("COMMIT")
        return parcel_id
    except Exception:
        _rollback_safe(conn)
        raise


def packing_list(conn: sqlite3.Connection, parcel_id: int) -> dict:
    p = conn.execute("SELECT * FROM parcels WHERE id=?", (parcel_id,)).fetchone()
    if not p:
        raise BusinessError("包裹不存在")
    items = conn.execute("SELECT * FROM parcel_items WHERE parcel_id=?",
                         (parcel_id,)).fetchall()
    return {"parcel": p, "items": items}


# ---------------------------------------------------------------- 改地址

def request_address_change(conn: sqlite3.Connection, enrollment_id: str,
                           new_address: str, new_receiver: str | None,
                           new_phone: str | None, submitted_by: str = "") -> int:
    """改地址仅在交运前生效：已有 shipped/delivered 包裹直接拒绝受理。"""
    if not new_address or not new_address.strip():
        raise BusinessError("新地址不能为空")
    e = conn.execute("SELECT * FROM enrollments WHERE id=?",
                     (enrollment_id,)).fetchone()
    if not e:
        raise BusinessError("报名记录不存在")
    if e["status"] == "withdrawn":
        raise BusinessError("学员已退课")
    shipped = conn.execute(
        "SELECT id FROM parcels WHERE enrollment_id=? "
        "AND status IN ('shipped','delivered','rejected','returned') LIMIT 1",
        (enrollment_id,),
    ).fetchone()
    if shipped:
        raise BusinessError("包裹已交运，改地址不再生效；如未收到请走异常补发")
    cur = conn.execute(
        "INSERT INTO address_requests(enrollment_id,new_receiver,new_phone,"
        "new_address,submitted_by,submitted_at) VALUES(?,?,?,?,?,?)",
        (enrollment_id, new_receiver, new_phone, new_address.strip(),
         submitted_by, now_iso()),
    )
    return cur.lastrowid


def approve_address_change(conn: sqlite3.Connection, request_id: int,
                           handled_by: str = "") -> None:
    r = conn.execute("SELECT * FROM address_requests WHERE id=?",
                     (request_id,)).fetchone()
    if not r or r["status"] != "pending":
        raise BusinessError("地址申请不存在或已处理")
    # 受理时再次校验截止时刻（申请之后可能恰好交运）
    shipped = conn.execute(
        "SELECT id FROM parcels WHERE enrollment_id=? "
        "AND status IN ('shipped','delivered','rejected','returned') LIMIT 1",
        (r["enrollment_id"],),
    ).fetchone()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if shipped:
            conn.execute(
                "UPDATE address_requests SET status='rejected',handled_by=?,"
                "handled_at=?,note='审批时包裹已交运' WHERE id=?",
                (handled_by, now_iso(), request_id),
            )
            conn.execute("COMMIT")
            raise BusinessError("包裹已交运，改地址不再生效")
        conn.execute(
            "UPDATE enrollments SET receiver_name=COALESCE(?,receiver_name),"
            "receiver_phone=COALESCE(?,receiver_phone),receiver_address=? "
            "WHERE id=?",
            (r["new_receiver"], r["new_phone"], r["new_address"],
             r["enrollment_id"]),
        )
        # 已打包未交运的包裹同步改面单
        conn.execute(
            "UPDATE parcels SET receiver_name=COALESCE(?,receiver_name),"
            "receiver_phone=COALESCE(?,receiver_phone),receiver_address=? "
            "WHERE enrollment_id=? AND status='packed'",
            (r["new_receiver"], r["new_phone"], r["new_address"],
             r["enrollment_id"]),
        )
        conn.execute(
            "UPDATE address_requests SET status='approved',handled_by=?,"
            "handled_at=? WHERE id=?",
            (handled_by, now_iso(), request_id),
        )
        conn.execute("COMMIT")
    except Exception:
        _rollback_safe(conn)
        raise


def reject_address_change(conn: sqlite3.Connection, request_id: int,
                          handled_by: str = "", note: str = "") -> None:
    conn.execute(
        "UPDATE address_requests SET status='rejected',handled_by=?,handled_at=?,"
        "note=? WHERE id=? AND status='pending'",
        (handled_by, now_iso(), note, request_id),
    )


# ---------------------------------------------------------------- 版次升级

def upgrade_edition(conn: sqlite3.Connection, class_id: str,
                    textbook_code: str, new_edition: str,
                    actor: str = "") -> dict:
    """班级教材版次升级。
    - 未发放/未交运：直接按新版拣包或换版；
    - 已寄旧版：逐人生成补充资料，历史包裹原样保留，绝不抹去。"""
    cm = conn.execute(
        "SELECT * FROM class_materials WHERE class_id=? AND textbook_code=?",
        (class_id, textbook_code),
    ).fetchone()
    if not cm:
        raise BusinessError("该班级未使用此教材")
    te = conn.execute(
        "SELECT * FROM textbook_editions WHERE textbook_code=? AND edition=?",
        (textbook_code, new_edition),
    ).fetchone()
    if not te:
        raise BusinessError("新版次教材不存在，请先入库建档")
    old_edition = cm["edition"]
    if old_edition == new_edition:
        raise BusinessError("与当前版次相同")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE class_materials SET edition=? WHERE class_id=? AND textbook_code=?",
            (new_edition, class_id, textbook_code),
        )
        enrollments = conn.execute(
            "SELECT id FROM enrollments WHERE class_id=? AND status='enrolled'",
            (class_id,),
        ).fetchall()
        supplemented, swapped = [], []
        for e in enrollments:
            old_item = conn.execute(
                """SELECT pi.* FROM parcel_items pi
                   JOIN parcels p ON p.id=pi.parcel_id
                   WHERE p.enrollment_id=? AND pi.textbook_code=?
                     AND pi.edition=? AND pi.is_supplement=0
                     AND p.status IN ('shipped','delivered')""",
                (e["id"], textbook_code, old_edition),
            ).fetchone()
            if old_item:
                # 已寄旧版 → 列补充资料（幂等：唯一索引兜底）
                try:
                    conn.execute(
                        "INSERT INTO supplements(class_id,enrollment_id,"
                        "textbook_code,old_edition,new_edition,title,note,"
                        "created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (class_id, e["id"], textbook_code, old_edition,
                         new_edition, te["label"], te["diff_note"], now_iso()),
                    )
                    supplemented.append(e["id"])
                except sqlite3.IntegrityError:
                    pass
                continue
            # 已打包未交运 → 换回新版（旧版回库、新版扣库）
            packed_item = conn.execute(
                """SELECT pi.* FROM parcel_items pi
                   JOIN parcels p ON p.id=pi.parcel_id
                   WHERE p.enrollment_id=? AND pi.textbook_code=?
                     AND pi.edition=? AND p.status='packed'""",
                (e["id"], textbook_code, old_edition),
            ).fetchone()
            if packed_item:
                conn.execute(
                    "INSERT INTO inventory(textbook_code,edition,qty) VALUES(?,?,?) "
                    "ON CONFLICT(textbook_code,edition) DO UPDATE SET qty=qty+?",
                    (textbook_code, old_edition, packed_item["qty"],
                     packed_item["qty"]),
                )
                cur = conn.execute(
                    "UPDATE inventory SET qty=qty-? WHERE textbook_code=? "
                    "AND edition=? AND qty>=?",
                    (packed_item["qty"], textbook_code, new_edition,
                     packed_item["qty"]),
                )
                if cur.rowcount == 0:
                    raise InsufficientStock(
                        f"新版教材 {textbook_code}（{new_edition}版）库存不足，"
                        "无法为待交运包裹换版")
                conn.execute(
                    "UPDATE parcel_items SET edition=? WHERE id=?",
                    (new_edition, packed_item["id"]),
                )
                conn.execute(
                    "INSERT INTO tracking_events(parcel_id,event,detail,at) "
                    "VALUES(?,?,?,?)",
                    (packed_item["parcel_id"], "repack",
                     f"版次升级换包：{old_edition}版 → {new_edition}版", now_iso()),
                )
                swapped.append(e["id"])
        conn.execute("COMMIT")
        return {"old_edition": old_edition, "supplemented": supplemented,
                "swapped": swapped}
    except Exception:
        _rollback_safe(conn)
        raise


def issue_supplements(conn: sqlite3.Connection, enrollment_id: str,
                      actor: str = "") -> int:
    """把某学员所有待发补充资料合并成一个 supplement 包裹发出。"""
    rows = conn.execute(
        "SELECT * FROM supplements WHERE enrollment_id=? AND status='pending'",
        (enrollment_id,),
    ).fetchall()
    if not rows:
        raise BusinessError("没有待发的补充资料")
    conn.execute("BEGIN IMMEDIATE")
    try:
        e = conn.execute("SELECT * FROM enrollments WHERE id=?",
                         (enrollment_id,)).fetchone()
        if e["status"] == "withdrawn":
            raise BusinessError("学员已退课")
        cur = conn.execute(
            "INSERT INTO parcels(enrollment_id,kind,status,receiver_name,"
            "receiver_phone,receiver_address,created_by,created_at) "
            "VALUES(?,?, 'packed',?,?,?,?,?)",
            (enrollment_id, "supplement", e["receiver_name"], e["receiver_phone"],
             e["receiver_address"], actor, now_iso()),
        )
        parcel_id = cur.lastrowid
        items = [{"textbook_code": r["textbook_code"], "edition": r["new_edition"],
                  "qty": 1} for r in rows]
        try:
            _deduct_stock(conn, items, "supplement", f"parcel#{parcel_id}", actor)
        except InsufficientStock:
            _rollback_safe(conn)
            raise
        for r in rows:
            conn.execute(
                "INSERT INTO parcel_items(parcel_id,textbook_code,edition,qty,"
                "is_supplement,item_title) VALUES(?,?,?,1,1,?)",
                (parcel_id, r["textbook_code"], r["new_edition"],
                 f"补充资料：{r['title']}（{r['old_edition']}版→{r['new_edition']}版）"),
            )
            conn.execute(
                "UPDATE supplements SET status='issued',parcel_id=? WHERE id=?",
                (parcel_id, r["id"]),
            )
        conn.execute(
            "INSERT INTO tracking_events(parcel_id,event,detail,at) VALUES(?,?,?,?)",
            (parcel_id, "packed", "版次升级补充资料已打包", now_iso()),
        )
        conn.execute("COMMIT")
        return parcel_id
    except Exception:
        _rollback_safe(conn)
        raise


# ---------------------------------------------------------------- 异常与补发

def submit_exception(conn: sqlite3.Connection, enrollment_id: str,
                     parcel_id: int, exc_type: str, reason: str,
                     submitted_by: str, internal_note: str = "") -> int:
    """登记异常：错寄 / 拒收 / 破损。理由必填；同包裹同类型只允许一单。"""
    if exc_type not in ("wrong_sent", "rejected", "damaged"):
        raise BusinessError("异常类型不合法")
    if not reason or not reason.strip():
        raise BusinessError("补发须核对理由，理由不能为空")
    p = conn.execute(
        "SELECT * FROM parcels WHERE id=? AND enrollment_id=?",
        (parcel_id, enrollment_id),
    ).fetchone()
    if not p:
        raise BusinessError("包裹不存在")
    if p["status"] == "cancelled":
        raise BusinessError("包裹已取消")
    # 重复补发拦截：该包裹已有获批补发包裹，不能就同一包裹再次登记异常
    dup = conn.execute(
        """SELECT x.id FROM exceptions x JOIN parcels rp ON rp.exception_id=x.id
           WHERE x.parcel_id=? AND x.status='approved'
             AND rp.status NOT IN ('cancelled') LIMIT 1""",
        (parcel_id,),
    ).fetchone()
    if dup:
        raise ResendAlreadyIssued("该包裹已获批补发，不能重复申请")
    idem_key = f"{parcel_id}:{exc_type}"
    try:
        cur = conn.execute(
            "INSERT INTO exceptions(enrollment_id,parcel_id,type,reason,"
            "internal_note,idem_key,submitted_by,submitted_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (enrollment_id, parcel_id, exc_type, reason.strip(),
             internal_note, idem_key, submitted_by, now_iso()),
        )
    except sqlite3.IntegrityError as exc:
        raise ResendAlreadyIssued(
            "该包裹的同类异常已登记，不能重复提交") from exc
    return cur.lastrowid


def approve_exception(conn: sqlite3.Connection, exception_id: int,
                      approver: str) -> int:
    """审批通过并生成补发包裹。
    - 审批人不能是提交人（仓库人员不能批准自己提交的异常）；
    - 一张异常单最多一个补发包裹（唯一索引 + 事务，并发安全）。"""
    import sqlite3 as _sq
    exc = conn.execute("SELECT * FROM exceptions WHERE id=?",
                       (exception_id,)).fetchone()
    if not exc:
        raise BusinessError("异常单不存在")
    if exc["status"] != "submitted":
        raise BusinessError("异常单已处理，不能重复审批")
    user = conn.execute("SELECT * FROM users WHERE id=?", (approver,)).fetchone()
    if not user:
        raise BusinessError("审批人不存在")
    if user["role"] == "warehouse":
        raise BusinessError("仓库人员不能审批异常补发，请由前台审批")
    if approver == exc["submitted_by"]:
        raise BusinessError("不能审批自己提交的异常单")
    e = conn.execute("SELECT * FROM enrollments WHERE id=?",
                     (exc["enrollment_id"],)).fetchone()
    if e["status"] == "withdrawn":
        raise BusinessError("学员已退课，不能补发")

    conn.execute("BEGIN IMMEDIATE")
    try:
        # 补发一律按班级当前计划版次拣书：
        # 错寄要纠正版次；拒收/破损若恰逢版次升级，也不再寄旧版。
        target = _required_items(conn, e["class_id"])
        items = [{"textbook_code": m["textbook_code"],
                  "edition": m["edition"], "qty": m["qty"],
                  "title": m["title"]} for m in target]
        cur = conn.execute(
            "INSERT INTO parcels(enrollment_id,kind,status,exception_id,"
            "receiver_name,receiver_phone,receiver_address,created_by,created_at) "
            "VALUES(?, 'resend','packed', ?,?,?,?,?,?)",
            (exc["enrollment_id"], exception_id, e["receiver_name"],
             e["receiver_phone"], e["receiver_address"], approver, now_iso()),
        )
        parcel_id = cur.lastrowid
        try:
            _deduct_stock(conn, items, f"resend:{exc['type']}",
                          f"exc#{exception_id}", approver)
        except InsufficientStock:
            _rollback_safe(conn)
            raise
        for m in items:
            conn.execute(
                "INSERT INTO parcel_items(parcel_id,textbook_code,edition,qty,"
                "item_title) VALUES(?,?,?,?,?)",
                (parcel_id, m["textbook_code"], m["edition"], m["qty"],
                 m["title"]),
            )
        # 补发已按新版拣书：核销该生所有待发补充资料，避免与补发重复寄递
        conn.execute(
            "UPDATE supplements SET status='issued',parcel_id=? "
            "WHERE enrollment_id=? AND status='pending'",
            (parcel_id, exc["enrollment_id"]),
        )
        conn.execute(
            "UPDATE exceptions SET status='approved',approved_by=?,approved_at=?,"
            "resolution=? WHERE id=?",
            (approver, now_iso(), f"已生成补发包裹 #{parcel_id}", exception_id),
        )
        conn.execute(
            "INSERT INTO tracking_events(parcel_id,event,detail,at) VALUES(?,?,?,?)",
            (parcel_id, "packed",
             f"异常补发（{ {'wrong_sent':'错寄','rejected':'拒收','damaged':'破损'}[exc['type']] }）已打包",
             now_iso()),
        )
        conn.execute("COMMIT")
        return parcel_id
    except _sq.IntegrityError as ex:
        _rollback_safe(conn)
        raise ResendAlreadyIssued("该异常单已生成过补发包裹") from ex
    except Exception:
        _rollback_safe(conn)
        raise


def reject_exception(conn: sqlite3.Connection, exception_id: int,
                     approver: str, note: str = "") -> None:
    exc = conn.execute("SELECT * FROM exceptions WHERE id=?",
                       (exception_id,)).fetchone()
    if not exc or exc["status"] != "submitted":
        raise BusinessError("异常单不存在或已处理")
    if approver == exc["submitted_by"]:
        raise BusinessError("不能审批自己提交的异常单")
    conn.execute(
        "UPDATE exceptions SET status='rejected',approved_by=?,approved_at=?,"
        "resolution=? WHERE id=?",
        (approver, now_iso(), note or "审批驳回", exception_id),
    )


# ---------------------------------------------------------------- 退课

def withdraw_enrollment(conn: sqlite3.Connection, enrollment_id: str,
                        actor: str = "") -> dict:
    """退课拦截：有在途/已签收包裹则不能直接退课；
    未交运包裹取消并回库，待审异常/地址申请一并关闭。"""
    e = conn.execute("SELECT * FROM enrollments WHERE id=?",
                     (enrollment_id,)).fetchone()
    if not e:
        raise BusinessError("报名记录不存在")
    if e["status"] == "withdrawn":
        raise BusinessError("该报名已退课")
    shipped = conn.execute(
        "SELECT id FROM parcels WHERE enrollment_id=? "
        "AND status IN ('shipped','delivered') LIMIT 1",
        (enrollment_id,),
    ).fetchall()
    if shipped:
        ids = ", ".join(str(r["id"]) for r in shipped)
        raise BusinessError(
            f"已有包裹在途或已签收（{ids}），需先完成物流退回/异常处理后方可退课")
    conn.execute("BEGIN IMMEDIATE")
    try:
        cancelled = []
        packed = conn.execute(
            "SELECT * FROM parcels WHERE enrollment_id=? AND status='packed'",
            (enrollment_id,),
        ).fetchall()
        for p in packed:
            items = conn.execute("SELECT * FROM parcel_items WHERE parcel_id=?",
                                 (p["id"],)).fetchall()
            _restock(conn, items, "withdraw_cancel", f"parcel#{p['id']}", actor)
            conn.execute(
                "UPDATE parcels SET status='cancelled',closed_at=? WHERE id=?",
                (now_iso(), p["id"]),
            )
            cancelled.append(p["id"])
        # rejected/returned 的异常单不再补发；待审单据关闭
        conn.execute(
            "UPDATE exceptions SET status='rejected',approved_by=?,approved_at=?,"
            "resolution='学员退课，自动关闭' WHERE enrollment_id=? AND status='submitted'",
            (actor, now_iso(), enrollment_id),
        )
        conn.execute(
            "UPDATE address_requests SET status='rejected',handled_by=?,"
            "handled_at=?,note='学员退课' WHERE enrollment_id=? AND status='pending'",
            (actor, now_iso(), enrollment_id),
        )
        conn.execute(
            "UPDATE supplements SET status='issued' WHERE enrollment_id=? "
            "AND status='pending'",
            (enrollment_id,),
        )
        conn.execute(
            "UPDATE enrollments SET status='withdrawn',withdrawn_on=? WHERE id=?",
            (now_iso(), enrollment_id),
        )
        conn.execute("COMMIT")
        return {"cancelled_parcels": cancelled}
    except Exception:
        _rollback_safe(conn)
        raise


# ---------------------------------------------------------------- 物流（模拟接口）

def ship_parcel(conn: sqlite3.Connection, parcel_id: int, actor: str = "") -> str:
    """交运：生成模拟运单号。交运后改地址截止。"""
    p = conn.execute("SELECT * FROM parcels WHERE id=?", (parcel_id,)).fetchone()
    if not p:
        raise BusinessError("包裹不存在")
    if p["status"] != "packed":
        raise BusinessError(f"包裹当前状态为 {p['status']}，不能交运")
    tracking = f"SF{p['id']:06d}{datetime.now().strftime('%H%M%S')}"
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE parcels SET status='shipped',tracking_no=?,shipped_at=? WHERE id=?",
            (tracking, now_iso(), parcel_id),
        )
        conn.execute(
            "INSERT INTO tracking_events(parcel_id,event,detail,at) VALUES(?,?,?,?)",
            (parcel_id, "shipped", f"已交运，运单号 {tracking}", now_iso()),
        )
        conn.execute("COMMIT")
    except Exception:
        _rollback_safe(conn)
        raise
    return tracking


def logistics_callback(conn: sqlite3.Connection, tracking_no: str,
                       event: str, detail: str = "",
                       proof_path: str | None = None) -> dict:
    """模拟物流接口回传：signed(签收) / rejected(拒收) / returning(退回)。"""
    p = conn.execute("SELECT * FROM parcels WHERE tracking_no=?",
                     (tracking_no,)).fetchone()
    if not p:
        raise BusinessError("运单号不存在")
    if p["status"] in ("delivered", "returned", "cancelled"):
        raise BusinessError(f"包裹已终态（{p['status']}），物流事件不再接受")
    if event == "signed":
        new_status, detail_out, final = "delivered", detail or "学员已签收", True
    elif event == "rejected":
        new_status, detail_out, final = "rejected", detail or "物流拒收", False
    elif event == "returning":
        new_status, detail_out, final = "returned", detail or "包裹已退回仓库", True
    else:
        raise BusinessError("未知物流事件")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE parcels SET status=?,closed_at=? WHERE id=?",
            (new_status, now_iso() if final else None, p["id"]),
        )
        conn.execute(
            "INSERT INTO tracking_events(parcel_id,event,detail,at) VALUES(?,?,?,?)",
            (p["id"], event, detail_out + (f"｜签收凭证：{proof_path}" if proof_path else ""),
             now_iso()),
        )
        # 拒收后退回入库：包裹内教材（不含补发说明性质的补充件）归还库存
        if event == "returning":
            items = conn.execute(
                "SELECT * FROM parcel_items WHERE parcel_id=?", (p["id"],)).fetchall()
            _restock(conn, items, "logistics_return", f"parcel#{p['id']}", "logistics")
        conn.execute("COMMIT")
    except Exception:
        _rollback_safe(conn)
        raise
    return {"parcel_id": p["id"], "status": new_status, "proof": proof_path}


def save_proof(upload_bytes: bytes, parcel_id: int) -> str:
    """签收图存本地文件 data/proofs/，返回相对路径。"""
    import os
    proofs_dir = os.path.join(dbmod.DATA_DIR, "proofs")
    os.makedirs(proofs_dir, exist_ok=True)
    fname = f"parcel_{parcel_id}_sign.png"
    path = os.path.join(proofs_dir, fname)
    with open(path, "wb") as f:
        f.write(upload_bytes)
    return f"data/proofs/{fname}"


# ---------------------------------------------------------------- 查询视图

def student_parcels(conn: sqlite3.Connection, student_id: str) -> list[dict]:
    """学员端：只展示自己的包裹，不出现仓库内部备注。"""
    rows = conn.execute(
        """SELECT p.*, s.name AS student_name, c.name AS class_name
           FROM parcels p
           JOIN enrollments e ON e.id=p.enrollment_id
           JOIN students s ON s.id=e.student_id
           JOIN classes c ON c.id=e.class_id
           WHERE s.id=? ORDER BY p.id""",
        (student_id,),
    ).fetchall()
    out = []
    for p in rows:
        d = dict(p)
        d["items"] = [dict(i) for i in conn.execute(
            "SELECT * FROM parcel_items WHERE parcel_id=?", (p["id"],)).fetchall()]
        d["events"] = [dict(ev) for ev in conn.execute(
            "SELECT event,detail,at FROM tracking_events WHERE parcel_id=? ORDER BY id",
            (p["id"],)).fetchall()]
        # 异常单只回传对外理由，internal_note 明确剔除
        d["exceptions"] = [
            {"type": x["type"], "reason": x["reason"], "status": x["status"],
             "resolution": x["resolution"]}
            for x in conn.execute(
                "SELECT type,reason,status,resolution FROM exceptions "
                "WHERE parcel_id=? ORDER BY id", (p["id"],)).fetchall()]
        out.append(d)
    return out
