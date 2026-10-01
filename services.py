"""Business services: fulfillment rules live here, not in the web layer.

规则要点：
- 首次发放（initial）每个报名记录只能创建一次（DB 部分唯一索引兜底）。
- 改地址仅在交运前作用于待发包裹；交运后只对之后新建的补发生效，历史快照不动。
- 补发必须有异常理由与审批；仓库人员不能批准自己提交的异常；每个异常最多补发一次。
- 版次升级：未交运的包裹改用新版；已寄旧版只生成「补充资料」清单，历史不抹除。
- 库存拣货原子扣减（BEGIN IMMEDIATE + 条件 UPDATE），不足即整单失败。
- 退课拦截一切后续发放；未交运包裹取消并回库，已发出的保留物流轨迹。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, date

from logistics import LogisticsService

# 交运后不可改地址的状态
HANDED_OFF = ("shipped", "delivered", "signed")
PICKABLE = ("planned", "picking")


class DomainError(Exception):
    """业务规则冲突，消息可直接展示给操作员。"""


@dataclass
class Shortage:
    book_id: int
    title: str
    edition: str
    required: int
    available: int

    @property
    def missing(self) -> int:
        return max(0, self.required - self.available)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return date.today().isoformat()


class FulfillmentService:
    def __init__(self, conn: sqlite3.Connection, logistics: LogisticsService | None = None):
        self.conn = conn
        self.logistics = logistics or LogisticsService()

    # ------------------------------------------------------------------ 查询

    def get_enrollment(self, enrollment_id: int) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM enrollments WHERE id=?", (enrollment_id,)
        ).fetchone()
        if not row:
            raise DomainError(f"报名记录 {enrollment_id} 不存在")
        return row

    def _class_book_ids(self, class_id: int) -> list[int]:
        return [r["book_id"] for r in self.conn.execute(
            "SELECT book_id FROM class_books WHERE class_id=? ORDER BY book_id",
            (class_id,),
        )]

    def current_address(self, enrollment_id: int) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM addresses WHERE enrollment_id=? AND is_current=1",
            (enrollment_id,),
        ).fetchone()
        if not row:
            raise DomainError("报名记录尚无收货地址")
        return row

    def _address_text(self, addr: sqlite3.Row) -> str:
        return f"{addr['recipient']} {addr['phone']} {addr['address']}"

    def initial_shipment(self, enrollment_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM shipments WHERE enrollment_id=? AND kind='initial'",
            (enrollment_id,),
        ).fetchone()

    # ------------------------------------------------------------ 报名 + 首發

    def enroll(self, student_id: int, class_id: int,
               recipient: str, phone: str, address: str) -> int:
        """报名并自动生成首次发放计划（备货清单）。"""
        if not self._class_book_ids(class_id):
            raise DomainError("班级尚未配置教材清单，无法生成备货计划")
        cur = self.conn.execute(
            "INSERT INTO enrollments(student_id, class_id, enroll_date, status) "
            "VALUES(?,?,?,'active')",
            (student_id, class_id, _today()),
        )
        eid = cur.lastrowid
        cur = self.conn.execute(
            "INSERT INTO addresses(enrollment_id, recipient, phone, address) "
            "VALUES(?,?,?,?)",
            (eid, recipient, phone, address),
        )
        self.create_initial_shipment(eid, address_id=cur.lastrowid)
        return eid

    def create_initial_shipment(self, enrollment_id: int,
                                address_id: int | None = None) -> int:
        """显式创建首次发放计划。已存在则报错——首次发放只能一次。"""
        enr = self.get_enrollment(enrollment_id)
        if enr["status"] == "withdrawn":
            raise DomainError("学员已退课，不能创建发放计划")
        if self.initial_shipment(enrollment_id):
            raise DomainError("该报名记录的首次发放已创建，不能重复创建")
        addr_id = address_id or self.current_address(enrollment_id)["id"]
        addr = self.conn.execute("SELECT * FROM addresses WHERE id=?",
                                 (addr_id,)).fetchone()
        try:
            cur = self.conn.execute(
                "INSERT INTO shipments(enrollment_id, kind, status, address_snapshot,"
                " address_id) VALUES(?,?, 'planned', ?,?)",
                (enrollment_id, "initial", self._address_text(addr), addr_id),
            )
        except sqlite3.IntegrityError:
            # 并发下唯一索引兜底
            raise DomainError("该报名记录的首次发放已创建，不能重复创建")
        sid = cur.lastrowid
        for book_id in self._class_book_ids(enr["class_id"]):
            self.conn.execute(
                "INSERT INTO shipment_items(shipment_id, book_id, qty) VALUES(?,? ,1)",
                (sid, book_id),
            )
        return sid

    # ------------------------------------------------------------ 待发/缺书

    def pending_list(self) -> list[sqlite3.Row]:
        """待发列表：开课倒计时升序，含缺书数量。"""
        rows = self.conn.execute(
            """
            SELECT s.id AS shipment_id, e.id AS enrollment_id, c.id AS class_id,
                   c.name AS class_name, c.start_date, u.name AS student_name,
                   s.status, s.kind,
                   CAST(julianday(c.start_date) - julianday(date('now','localtime'))
                        AS INTEGER) AS days_to_start
            FROM shipments s
            JOIN enrollments e ON e.id = s.enrollment_id
            JOIN classes c ON c.id = e.class_id
            JOIN users u ON u.id = e.student_id
            WHERE e.status='active' AND s.status IN ('planned','picking')
            ORDER BY days_to_start ASC, s.id ASC
            """
        ).fetchall()
        return rows

    def shipment_shortage(self, shipment_id: int) -> list[Shortage]:
        rows = self.conn.execute(
            """
            SELECT si.book_id, t.title, t.edition, SUM(si.qty) AS need,
                   COALESCE(i.qty,0) AS avail
            FROM shipment_items si
            JOIN textbooks t ON t.id = si.book_id
            LEFT JOIN inventory i ON i.book_id = si.book_id
            WHERE si.shipment_id=?
            GROUP BY si.book_id
            HAVING need > avail
            """,
            (shipment_id,),
        ).fetchall()
        return [Shortage(r["book_id"], r["title"], r["edition"],
                         r["need"], r["avail"]) for r in rows]

    # ------------------------------------------------------------ 拣货/交运

    def pick_and_pack(self, shipment_id: int) -> list[int]:
        """仓库拣书打包：原子扣减库存。任一品种不足则整单不动。"""
        ship = self.conn.execute("SELECT * FROM shipments WHERE id=?",
                                 (shipment_id,)).fetchone()
        if not ship:
            raise DomainError("包裹不存在")
        if ship["status"] not in PICKABLE:
            raise DomainError(f"包裹状态为 {ship['status']}，不能拣货")
        enr = self.get_enrollment(ship["enrollment_id"])
        if enr["status"] == "withdrawn":
            raise DomainError("学员已退课，包裹应取消，不能拣货")

        items = self.conn.execute(
            "SELECT book_id, qty FROM shipment_items WHERE shipment_id=?",
            (shipment_id,),
        ).fetchall()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for it in items:
                res = self.conn.execute(
                    "UPDATE inventory SET qty = qty - ? "
                    "WHERE book_id=? AND qty >= ?",
                    (it["qty"], it["book_id"], it["qty"]),
                )
                if res.rowcount == 0:
                    avail = self.conn.execute(
                        "SELECT qty FROM inventory WHERE book_id=?",
                        (it["book_id"],),
                    ).fetchone()
                    avail_q = avail["qty"] if avail else 0
                    book = self.conn.execute(
                        "SELECT title, edition FROM textbooks WHERE id=?",
                        (it["book_id"],),
                    ).fetchone()
                    raise DomainError(
                        f"库存不足：{book['title']}（{book['edition']}）"
                        f"需要 {it['qty']}，现有 {avail_q}")
            for it in items:
                self.conn.execute(
                    "INSERT INTO inventory_log(book_id, change_qt, reason, ref_type, ref_id)"
                    " VALUES(?,?,'pick','shipment',?)",
                    (it["book_id"], -it["qty"], shipment_id),
                )
                self.conn.execute(
                    "UPDATE shipment_items SET picked=1 WHERE shipment_id=? AND book_id=?",
                    (shipment_id, it["book_id"]),
                )
            self.conn.execute(
                "UPDATE shipments SET status='picking' WHERE id=? AND status IN ('planned','picking')",
                (shipment_id,),
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return [it["book_id"] for it in items]

    def hand_to_logistics(self, shipment_id: int) -> str:
        """交运：地址快照此刻冻结，生成运单。之后改地址不影响本包裹。"""
        ship = self.conn.execute("SELECT * FROM shipments WHERE id=?",
                                 (shipment_id,)).fetchone()
        if not ship:
            raise DomainError("包裹不存在")
        if ship["status"] != "picking":
            raise DomainError("包裹尚未完成拣货打包，不能交运")
        enr = self.get_enrollment(ship["enrollment_id"])
        if enr["status"] == "withdrawn":
            raise DomainError("学员已退课，不能交运")
        tracking_no = self.logistics.create_waybill(shipment_id, ship["address_snapshot"])
        self.conn.execute(
            "UPDATE shipments SET status='shipped', tracking_no=?, shipped_at=? "
            "WHERE id=?",
            (tracking_no, _now(), shipment_id),
        )
        self._add_event(shipment_id, "picked_up", "仓库交接")
        return tracking_no

    def _add_event(self, shipment_id: int, event: str, location: str | None = None,
                   ts: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO tracking_events(shipment_id, event, location, occurred_at)"
            " VALUES(?,?,?,?)",
            (shipment_id, event, location, ts or _now()),
        )

    # --------------------------------------------------- 物流回传（模拟接口）

    def logistics_callback(self, shipment_id: int, event: str,
                           location: str | None = None) -> None:
        ship = self.conn.execute("SELECT * FROM shipments WHERE id=?",
                                 (shipment_id,)).fetchone()
        if not ship:
            raise DomainError("包裹不存在")
        if ship["status"] in ("cancelled", "returned"):
            raise DomainError(f"包裹已{ship['status']}，不再接收物流事件")
        transitions = {
            "in_transit":        {"from": ("shipped",),                     "to": "shipped"},
            "out_for_delivery":  {"from": ("shipped",),                     "to": "shipped"},
            "delivered":         {"from": ("shipped",),                     "to": "delivered"},
            "signed":            {"from": ("delivered",),                   "to": "signed"},
            "rejected":          {"from": ("shipped", "delivered"),         "to": "returned"},
        }
        if event not in transitions:
            raise DomainError(f"未知物流事件 {event}")
        rule = transitions[event]
        if ship["status"] not in rule["from"]:
            raise DomainError(
                f"包裹当前为「{ship['status']}」，不能回传事件「{event}」")
        self._add_event(shipment_id, event, location)
        if event == "delivered":
            self.conn.execute(
                "UPDATE shipments SET status='delivered', delivered_at=? WHERE id=?",
                (_now(), shipment_id))
        elif event == "signed":
            self.conn.execute(
                "UPDATE shipments SET status='signed', signed_at=? WHERE id=?",
                (_now(), shipment_id))
        elif event == "rejected":
            self.conn.execute(
                "UPDATE shipments SET status='returned', returned_at=? WHERE id=?",
                (_now(), shipment_id))
            self._handle_return(shipment_id, restock=True,
                                reason="物流拒收，包裹退回")
        # in_transit / out_for_delivery 仅记录轨迹，不改顶层状态

    def confirm_signature(self, shipment_id: int, signer: str,
                          data_dir: str) -> str:
        """签收：模拟回传签收图（本地 SVG 文件）。"""
        ship = self.conn.execute("SELECT * FROM shipments WHERE id=?",
                                 (shipment_id,)).fetchone()
        if ship["status"] != "delivered":
            raise DomainError("仅已送达的包裹可以签收")
        if self.conn.execute("SELECT 1 FROM signatures WHERE shipment_id=?",
                             (shipment_id,)).fetchone():
            raise DomainError("该包裹已签收，不能重复签收")
        image_file = self.logistics.make_signature_image(
            shipment_id, signer, ship["tracking_no"], data_dir)
        self.conn.execute(
            "INSERT INTO signatures(shipment_id, signer, image_file) VALUES(?,?,?)",
            (shipment_id, signer, image_file))
        self.conn.execute(
            "UPDATE shipments SET status='signed', signed_at=? WHERE id=?",
            (_now(), shipment_id))
        self._add_event(shipment_id, "signed", f"签收人：{signer}")
        return image_file

    def _handle_return(self, shipment_id: int, restock: bool, reason: str) -> None:
        items = self.conn.execute(
            "SELECT book_id, qty, picked FROM shipment_items WHERE shipment_id=?",
            (shipment_id,),
        ).fetchall()
        if restock:
            for it in items:
                if it["picked"]:
                    self.conn.execute(
                        "INSERT INTO inventory_log(book_id, change_qt, reason, ref_type, ref_id)"
                        " VALUES(?,?,'return','shipment',?)",
                        (it["book_id"], it["qty"], shipment_id))
                    self.conn.execute(
                        "UPDATE inventory SET qty = qty + ? WHERE book_id=?",
                        (it["qty"], it["book_id"]))
        self._add_event(shipment_id, "return", reason)

    # ------------------------------------------------------------ 改地址

    def request_address_change(self, enrollment_id: int, recipient: str,
                               phone: str, address: str, note: str = "") -> int:
        self.get_enrollment(enrollment_id)
        cur = self.conn.execute(
            "INSERT INTO addresses(enrollment_id, recipient, phone, address)"
            " VALUES(?,?,?,?)",
            (enrollment_id, recipient, phone, address),
        )
        # 新地址先不置为 current，审批通过才生效；旧的仍是当前地址
        self.conn.execute("UPDATE addresses SET is_current=0 WHERE id=?",
                          (cur.lastrowid,))
        req = self.conn.execute(
            "INSERT INTO address_requests(enrollment_id, address_id, note)"
            " VALUES(?,?,?)",
            (enrollment_id, cur.lastrowid, note),
        )
        return req.lastrowid

    def approve_address_change(self, request_id: int, approver_id: int) -> str:
        """审批改址。返回实际生效范围。"""
        req = self.conn.execute("SELECT * FROM address_requests WHERE id=?",
                                (request_id,)).fetchone()
        if not req:
            raise DomainError("改址申请不存在")
        if req["status"] != "pending":
            raise DomainError("该改址申请已处理")
        enr = self.get_enrollment(req["enrollment_id"])
        if enr["status"] == "withdrawn":
            raise DomainError("学员已退课，改址申请作废")
        addr = self.conn.execute("SELECT * FROM addresses WHERE id=?",
                                 (req["address_id"],)).fetchone()

        pending = self.conn.execute(
            "SELECT id FROM shipments WHERE enrollment_id=? AND status IN ('planned','picking')",
            (req["enrollment_id"],),
        ).fetchall()
        applies_to = "pending_packages" if pending else "future_resends"

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "UPDATE addresses SET is_current=0 WHERE enrollment_id=?",
                (req["enrollment_id"],))
            self.conn.execute("UPDATE addresses SET is_current=1 WHERE id=?",
                              (addr["id"],))
            for row in pending:
                self.conn.execute(
                    "UPDATE shipments SET address_snapshot=?, address_id=? WHERE id=?",
                    (self._address_text(addr), addr["id"], row["id"]))
            self.conn.execute(
                "UPDATE address_requests SET status='approved', decided_by=?,"
                " decided_at=?, applies_to=? WHERE id=?",
                (approver_id, _now(), applies_to, request_id))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return applies_to

    def reject_address_change(self, request_id: int, approver_id: int) -> None:
        req = self.conn.execute("SELECT * FROM address_requests WHERE id=?",
                                (request_id,)).fetchone()
        if not req or req["status"] != "pending":
            raise DomainError("申请不存在或已处理")
        self.conn.execute(
            "UPDATE address_requests SET status='rejected', decided_by=?, decided_at=?"
            " WHERE id=?",
            (approver_id, _now(), request_id))

    # ------------------------------------------------------------ 版次升级

    def upgrade_edition(self, class_id: int, old_book_id: int, new_book_id: int,
                        diff_note: str) -> dict:
        """班级换用新版教材。

        - 未交运包裹：明细直接换成新版（旧版明细移除，拣货以新版为准）。
        - 已寄旧版：生成补充资料清单（edition_supplements），历史包裹与旧版
          明细原样保留。
        """
        swapped_planned: list[int] = []
        supplements: list[int] = []
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "UPDATE class_books SET book_id=? WHERE class_id=? AND book_id=?",
                (new_book_id, class_id, old_book_id))
            if self.conn.total_changes == 0:
                self.conn.execute(
                    "INSERT INTO class_books(class_id, book_id) VALUES(?,?)",
                    (class_id, new_book_id))

            enrollments = self.conn.execute(
                "SELECT id FROM enrollments WHERE class_id=? AND status='active'",
                (class_id,)).fetchall()
            for e in enrollments:
                eid = e["id"]
                shipped_old = self.conn.execute(
                    "SELECT 1 FROM shipment_items si JOIN shipments s ON s.id=si.shipment_id"
                    " WHERE s.enrollment_id=? AND si.book_id=? AND s.status IN "
                    "('shipped','delivered','signed') LIMIT 1",
                    (eid, old_book_id)).fetchone()
                if shipped_old:
                    exists = self.conn.execute(
                        "SELECT 1 FROM edition_supplements WHERE enrollment_id=?"
                        " AND old_book_id=? AND new_book_id=?",
                        (eid, old_book_id, new_book_id)).fetchone()
                    if not exists:
                        cur = self.conn.execute(
                            "INSERT INTO edition_supplements(enrollment_id, old_book_id,"
                            " new_book_id, note) VALUES(?,?,?,?)",
                            (eid, old_book_id, new_book_id, diff_note))
                        supplements.append(cur.lastrowid)
                else:
                    # 未交运：全部待发包裹换新版
                    planned = self.conn.execute(
                        "SELECT s.id FROM shipments s WHERE s.enrollment_id=?"
                        " AND s.status IN ('planned','picking')", (eid,)).fetchall()
                    for p in planned:
                        pid = p["id"]
                        self.conn.execute(
                            "DELETE FROM shipment_items WHERE shipment_id=? AND book_id=?",
                            (pid, old_book_id))
                        dup = self.conn.execute(
                            "SELECT 1 FROM shipment_items WHERE shipment_id=? AND book_id=?",
                            (pid, new_book_id)).fetchone()
                        if not dup:
                            self.conn.execute(
                                "INSERT INTO shipment_items(shipment_id, book_id, qty)"
                                " VALUES(?,? ,1)", (pid, new_book_id))
                        swapped_planned.append(pid)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"swapped_planned": swapped_planned, "supplements": supplements}

    def issue_supplement(self, supplement_id: int, user_id: int) -> int:
        """前台据补充资料清单发出补充包裹（kind=resend）。幂等：一条资料一次。"""
        user = self.conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not user or user["role"] not in ("frontdesk", "warehouse"):
            raise DomainError("只有前台/仓库可以发起补充资料发放")
        sup = self.conn.execute("SELECT * FROM edition_supplements WHERE id=?",
                                (supplement_id,)).fetchone()
        if not sup:
            raise DomainError("补充资料记录不存在")
        if sup["shipment_id"]:
            raise DomainError("该补充资料已发出，不能重复发放（幂等）")
        enr = self.get_enrollment(sup["enrollment_id"])
        if enr["status"] == "withdrawn":
            raise DomainError("学员已退课，停止发放补充资料")
        addr = self.current_address(enr["id"])
        cur = self.conn.execute(
            "INSERT INTO shipments(enrollment_id, kind, status, address_snapshot,"
            " address_id, idempotency_key) VALUES(?, 'resend','planned',?, ?,"
            " ?)",
            (enr["id"], self._address_text(addr), addr["id"],
             f"sup-{supplement_id}"))
        sid = cur.lastrowid
        self.conn.execute(
            "INSERT INTO shipment_items(shipment_id, book_id, qty) VALUES(?,? ,1)",
            (sid, sup["new_book_id"]))
        self.conn.execute(
            "UPDATE edition_supplements SET shipment_id=? WHERE id=?",
            (sid, supplement_id))
        return sid

    # ------------------------------------------------------- 异常 + 补发审批

    def submit_exception(self, enrollment_id: int, kind: str, reason: str,
                         submitter_id: int, evidence: str = "",
                         warehouse_note: str = "") -> int:
        if not reason or not reason.strip():
            raise DomainError("补发/异常申请必须填写理由")
        enr = self.get_enrollment(enrollment_id)
        if enr["status"] == "withdrawn":
            raise DomainError("学员已退课，不能再提交异常申请")
        submitter = self.conn.execute("SELECT * FROM users WHERE id=?",
                                      (submitter_id,)).fetchone()
        if not submitter:
            raise DomainError("提交人不存在")
        # 同一报名记录 + 同类异常，已有未结案的，禁止重复提交（防重复补发）
        dup = self.conn.execute(
            "SELECT id FROM exception_requests WHERE enrollment_id=? AND kind=?"
            " AND status IN ('submitted','approved') LIMIT 1",
            (enrollment_id, kind)).fetchone()
        if dup:
            raise DomainError(f"已有进行中的{kind}异常单 #{dup['id']}，请勿重复提交")
        cur = self.conn.execute(
            "INSERT INTO exception_requests(enrollment_id, kind, reason, evidence,"
            " submitted_by, warehouse_note) VALUES(?,?,?,?,?,?)",
            (enrollment_id, kind, reason, evidence, submitter_id, warehouse_note))
        return cur.lastrowid

    def approve_exception(self, request_id: int, approver_id: int) -> int | None:
        """审批异常。通过后按类型分别处理；返回新建的补发包裹 id（无则 None）。"""
        req = self.conn.execute("SELECT * FROM exception_requests WHERE id=?",
                                (request_id,)).fetchone()
        if not req:
            raise DomainError("异常单不存在")
        if req["status"] != "submitted":
            raise DomainError("该异常单已处理")
        approver = self.conn.execute("SELECT * FROM users WHERE id=?",
                                     (approver_id,)).fetchone()
        if not approver:
            raise DomainError("审批人不存在")
        if approver["role"] not in ("warehouse", "frontdesk"):
            raise DomainError("学员无权审批异常单")
        # 仓库人员不能批准自己提交的异常
        submitter = self.conn.execute("SELECT * FROM users WHERE id=?",
                                      (req["submitted_by"],)).fetchone()
        if submitter["role"] == "warehouse" and submitter["id"] == approver["id"]:
            raise DomainError("仓库人员不能批准自己提交的异常单")

        enr = self.get_enrollment(req["enrollment_id"])
        if enr["status"] == "withdrawn":
            raise DomainError("学员已退课，补发被拦截")

        new_shipment: int | None = None
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if req["kind"] in ("wrong_book", "rejected_logistics", "damaged"):
                # 幂等：该异常单若已产生过补发，不再产生
                existed = self.conn.execute(
                    "SELECT id FROM shipments WHERE idempotency_key=?",
                    (f"exc-{request_id}",)).fetchone()
                if existed:
                    raise DomainError("该异常单已补发，不能重复补发")
                addr = self.current_address(enr["id"])
                cur = self.conn.execute(
                    "INSERT INTO shipments(enrollment_id, kind, status, address_snapshot,"
                    " address_id, idempotency_key) VALUES(?, 'resend','planned',?, ?,"
                    " ?)",
                    (enr["id"], self._address_text(addr), addr["id"],
                     f"exc-{request_id}"))
                new_shipment = cur.lastrowid
                # 补发按班级当前版次清单配齐
                for book_id in self._class_book_ids(enr["class_id"]):
                    self.conn.execute(
                        "INSERT OR IGNORE INTO shipment_items(shipment_id, book_id, qty)"
                        " VALUES(?,? ,1)", (new_shipment, book_id))
                self.conn.execute(
                    "UPDATE exception_requests SET status='approved', approved_by=?,"
                    " approved_at=?, resend_shipment_id=? WHERE id=?",
                    (approver_id, _now(), new_shipment, request_id))
            elif req["kind"] == "duplicate_resend":
                # 重复补发：不允许再发，仅登记结案
                self.conn.execute(
                    "UPDATE exception_requests SET status='closed', approved_by=?,"
                    " approved_at=? WHERE id=?",
                    (approver_id, _now(), request_id))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return new_shipment

    def reject_exception(self, request_id: int, approver_id: int) -> None:
        req = self.conn.execute("SELECT * FROM exception_requests WHERE id=?",
                                (request_id,)).fetchone()
        if not req or req["status"] != "submitted":
            raise DomainError("异常单不存在或已处理")
        approver = self.conn.execute("SELECT * FROM users WHERE id=?",
                                     (approver_id,)).fetchone()
        if approver and approver["role"] not in ("warehouse", "frontdesk"):
            raise DomainError("无权审批")
        self.conn.execute(
            "UPDATE exception_requests SET status='rejected', approved_by=?, approved_at=?"
            " WHERE id=?",
            (approver_id, _now(), request_id))

    # ------------------------------------------------------------ 退课

    def withdraw(self, enrollment_id: int, user_id: int, reason: str) -> None:
        if not reason or not reason.strip():
            raise DomainError("退课需填写原因")
        enr = self.get_enrollment(enrollment_id)
        if enr["status"] == "withdrawn":
            raise DomainError("该报名记录已退课")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "UPDATE enrollments SET status='withdrawn', withdraw_date=? WHERE id=?",
                (_today(), enrollment_id))
            self.conn.execute(
                "INSERT INTO withdrawals(enrollment_id, requested_by, reason) VALUES(?,?,?)",
                (enrollment_id, user_id, reason))
            # 未交运包裹：取消，已拣图书回库
            pending = self.conn.execute(
                "SELECT id FROM shipments WHERE enrollment_id=? AND status IN ('planned','picking')",
                (enrollment_id,)).fetchall()
            for p in pending:
                pid = p["id"]
                items = self.conn.execute(
                    "SELECT book_id, qty FROM shipment_items WHERE shipment_id=? AND picked=1",
                    (pid,)).fetchall()
                for it in items:
                    self.conn.execute(
                        "UPDATE inventory SET qty=qty+? WHERE book_id=?",
                        (it["qty"], it["book_id"]))
                    self.conn.execute(
                        "INSERT INTO inventory_log(book_id, change_qt, reason, ref_type, ref_id)"
                        " VALUES(?,?,'withdraw_return','shipment',?)",
                        (it["book_id"], it["qty"], pid))
                self.conn.execute(
                    "UPDATE shipments SET status='cancelled', cancel_reason='学员退课'"
                    " WHERE id=?", (pid,))
            # 待审批改址 / 异常自动关闭
            self.conn.execute(
                "UPDATE address_requests SET status='rejected', decided_at=?,"
                " note = COALESCE(note,'') || '（退课自动驳回）' WHERE enrollment_id=?"
                " AND status='pending'", (_now(), enrollment_id))
            self.conn.execute(
                "UPDATE exception_requests SET status='rejected', approved_at=?,"
                " warehouse_note = COALESCE(warehouse_note,'') || '（退课拦截）'"
                " WHERE enrollment_id=? AND status='submitted'",
                (_now(), enrollment_id))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------ 库存

    def adjust_stock(self, book_id: int, qty: int, reason: str = "purchase") -> None:
        self.conn.execute(
            "INSERT INTO inventory(book_id, qty) VALUES(?,?) "
            "ON CONFLICT(book_id) DO UPDATE SET qty = qty + ?",
            (book_id, qty, qty))
        self.conn.execute(
            "INSERT INTO inventory_log(book_id, change_qt, reason) VALUES(?,?,?)",
            (book_id, qty, reason))

    def stock_levels(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT t.id, t.isbn, t.title, t.edition, t.cover_color,"
            " COALESCE(i.qty,0) AS qty FROM textbooks t"
            " LEFT JOIN inventory i ON i.book_id=t.id ORDER BY t.isbn, t.edition_seq"
        ).fetchall()
