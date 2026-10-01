"""业务规则测试：版次规则、地址截止、补发幂等、库存竞争、退课拦截等。

运行：python3 -m unittest tests.py -v
每个用例使用独立临时 SQLite 库，互不影响。
"""
from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from datetime import date, timedelta
from pathlib import Path

from db import connect, init_db
from logistics import LogisticsService
from services import FulfillmentService, DomainError


class FulfillmentTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.conn = connect(self.dir / "t.db")
        init_db(self.conn)
        self.svc = FulfillmentService(self.conn, LogisticsService())
        self._fixture()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    # ------------------------------------------------------------ 公共夹具

    def _fixture(self):
        c = self.conn.cursor()
        c.execute("INSERT INTO users(username,name,role) VALUES('w','王仓','warehouse')")
        self.wang = c.lastrowid
        c.execute("INSERT INTO users(username,name,role) VALUES('l','李库','warehouse')")
        self.li = c.lastrowid
        c.execute("INSERT INTO users(username,name,role) VALUES('q','赵前台','frontdesk')")
        self.zhao = c.lastrowid
        c.execute("INSERT INTO users(username,name,role) VALUES('s','学员','student')")
        self.stu = c.lastrowid
        self.b1 = c.execute(
            "INSERT INTO textbooks(isbn,title,edition,edition_seq,cover_color)"
            " VALUES('X','教材','第1版',1,'#7c5cbf')").lastrowid
        self.b2 = c.execute(
            "INSERT INTO textbooks(isbn,title,edition,edition_seq,cover_color,supersedes_id)"
            " VALUES('X','教材','第2版',2,'#5b8def',?)", (self.b1,)).lastrowid
        start = (date.today() + timedelta(days=10)).isoformat()
        self.cls = c.execute(
            "INSERT INTO classes(name,start_date) VALUES('班',?)", (start,)).lastrowid
        c.execute("INSERT INTO class_books(class_id,book_id) VALUES(?,?)",
                  (self.cls, self.b1))
        self.svc.adjust_stock(self.b1, 100)
        self.svc.adjust_stock(self.b2, 100)
        self.eid = self.svc.enroll(self.stu, self.cls, "学员", "138", "旧地址 A")
        self.sid = self.svc.initial_shipment(self.eid)["id"]

    def _full_ship(self, sid=None):
        sid = sid or self.sid
        self.svc.pick_and_pack(sid)
        self.svc.hand_to_logistics(sid)
        self.svc.logistics_callback(sid, "delivered", "网点")
        self.svc.confirm_signature(sid, "学员", self.dir)
        return sid

    # ====================================================== 1. 首次发放唯一

    def test_initial_shipment_only_once(self):
        with self.assertRaises(DomainError):
            self.svc.create_initial_shipment(self.eid)
        # 直接插库也会被部分唯一索引挡住
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO shipments(enrollment_id,kind,status,address_snapshot)"
                " VALUES(?, 'initial','planned','x')", (self.eid,))
        n = self.conn.execute(
            "SELECT COUNT(*) FROM shipments WHERE enrollment_id=? AND kind='initial'",
            (self.eid,)).fetchone()[0]
        self.assertEqual(n, 1)

    # ====================================================== 2. 版次规则

    def test_edition_upgrade_swap_before_ship(self):
        """未交运：版次升级后包裹明细直接换新版，不再含旧版。"""
        self.svc.upgrade_edition(self.cls, self.b1, self.b2, "新版差异说明")
        books = {r["book_id"] for r in self.conn.execute(
            "SELECT book_id FROM shipment_items WHERE shipment_id=?", (self.sid,))}
        self.assertEqual(books, {self.b2})
        # 不产生补充资料
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM edition_supplements").fetchone()[0], 0)

    def test_edition_upgrade_supplement_after_shipped(self):
        """已寄旧版：只生成补充资料，历史包裹与旧版明细保留。"""
        self._full_ship()
        res = self.svc.upgrade_edition(self.cls, self.b1, self.b2, "新增第8章")
        self.assertEqual(len(res["supplements"]), 1)
        sup = self.conn.execute("SELECT * FROM edition_supplements WHERE id=?",
                                (res["supplements"][0],)).fetchone()
        self.assertEqual(sup["old_book_id"], self.b1)
        self.assertEqual(sup["new_book_id"], self.b2)
        self.assertIsNone(sup["shipment_id"])
        # 历史包裹仍是旧版
        old = self.conn.execute(
            "SELECT book_id FROM shipment_items WHERE shipment_id=?",
            (self.sid,)).fetchall()
        self.assertEqual([r["book_id"] for r in old], [self.b1])

    def test_supplement_issue_idempotent(self):
        """补充资料只能发一次。"""
        self._full_ship()
        sup_id = self.svc.upgrade_edition(
            self.cls, self.b1, self.b2, "x")["supplements"][0]
        ns = self.svc.issue_supplement(sup_id, self.zhao)
        self.assertEqual(self.conn.execute(
            "SELECT kind FROM shipments WHERE id=?", (ns,)).fetchone()[0], "resend")
        with self.assertRaises(DomainError):
            self.svc.issue_supplement(sup_id, self.zhao)
        # 底层 idempotency_key 也兜底
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO shipments(enrollment_id,kind,status,address_snapshot,"
                "idempotency_key) VALUES(?, 'resend','planned','x',?)",
                (self.eid, f"sup-{sup_id}"))

    # ====================================================== 3. 地址截止

    def test_address_change_before_handoff_updates_package(self):
        rid = self.svc.request_address_change(
            self.eid, "学员", "139", "新地址 B", "")
        scope = self.svc.approve_address_change(rid, self.zhao)
        self.assertEqual(scope, "pending_packages")
        snap = self.conn.execute("SELECT address_snapshot FROM shipments WHERE id=?",
                                 (self.sid,)).fetchone()[0]
        self.assertIn("新地址 B", snap)

    def test_address_change_after_handoff_only_future_resends(self):
        self.svc.pick_and_pack(self.sid)
        self.svc.hand_to_logistics(self.sid)
        rid = self.svc.request_address_change(
            self.eid, "学员", "139", "新地址 B", "")
        scope = self.svc.approve_address_change(rid, self.zhao)
        self.assertEqual(scope, "future_resends")
        # 已交运包裹地址快照冻结
        snap = self.conn.execute("SELECT address_snapshot FROM shipments WHERE id=?",
                                 (self.sid,)).fetchone()[0]
        self.assertIn("旧地址 A", snap)
        # 之后新建的补发用新地址
        exc = self.svc.submit_exception(
            self.eid, "damaged", "破损", self.zhao)
        nsid = self.svc.approve_exception(exc, self.wang)
        new_snap = self.conn.execute(
            "SELECT address_snapshot FROM shipments WHERE id=?",
            (nsid,)).fetchone()[0]
        self.assertIn("新地址 B", new_snap)

    def test_address_request_decided_once(self):
        rid = self.svc.request_address_change(self.eid, "学员", "139", "B", "")
        self.svc.approve_address_change(rid, self.zhao)
        with self.assertRaises(DomainError):
            self.svc.approve_address_change(rid, self.zhao)

    # ====================================================== 4. 补发幂等 + 审批

    def test_resend_requires_reason(self):
        with self.assertRaises(DomainError):
            self.svc.submit_exception(self.eid, "damaged", "  ", self.zhao)

    def test_warehouse_cannot_approve_own_exception(self):
        rid = self.svc.submit_exception(
            self.eid, "wrong_book", "错寄了", self.wang)
        with self.assertRaises(DomainError):
            self.svc.approve_exception(rid, self.wang)
        # 换一名仓库人员即可审批并产生补发
        nsid = self.svc.approve_exception(rid, self.li)
        self.assertIsNotNone(nsid)

    def test_frontdesk_can_approve_warehouse_submission(self):
        rid = self.svc.submit_exception(self.eid, "damaged", "破损", self.wang)
        nsid = self.svc.approve_exception(rid, self.zhao)
        self.assertIsNotNone(nsid)

    def test_student_cannot_approve(self):
        rid = self.svc.submit_exception(self.eid, "damaged", "破损", self.zhao)
        with self.assertRaises(DomainError):
            self.svc.approve_exception(rid, self.stu)

    def test_resend_idempotent_per_exception(self):
        """一张异常单最多补发一次：再次审批被拒，且无第二条 resend。"""
        rid = self.svc.submit_exception(self.eid, "damaged", "破损", self.zhao)
        nsid = self.svc.approve_exception(rid, self.wang)
        with self.assertRaises(DomainError):
            self.svc.approve_exception(rid, self.li)
        n = self.conn.execute(
            "SELECT COUNT(*) FROM shipments WHERE idempotency_key=?",
            (f"exc-{rid}",)).fetchone()[0]
        self.assertEqual(n, 1)
        self.assertEqual(nsid, self.conn.execute(
            "SELECT resend_shipment_id FROM exception_requests WHERE id=?",
            (rid,)).fetchone()[0])

    def test_duplicate_active_exception_blocked(self):
        """同类异常处理中不能重复提交。"""
        self.svc.submit_exception(self.eid, "damaged", "第一次", self.zhao)
        with self.assertRaises(DomainError):
            self.svc.submit_exception(self.eid, "damaged", "又来一次", self.zhao)

    def test_duplicate_resend_kind_closed_no_shipment(self):
        """duplicate_resend 只登记结案，不再发书。"""
        rid = self.svc.submit_exception(
            self.eid, "duplicate_resend", "学员声称没收到", self.zhao)
        result = self.svc.approve_exception(rid, self.wang)
        self.assertIsNone(result)
        status = self.conn.execute(
            "SELECT status FROM exception_requests WHERE id=?", (rid,)).fetchone()[0]
        self.assertEqual(status, "closed")

    # ====================================================== 5. 库存竞争

    def test_pick_atomic_when_shortage_rollback(self):
        """单包裹部分品种不足：整单不扣减。"""
        # 再给包裹加一种教材并令其库存为 0
        b3 = self.conn.execute(
            "INSERT INTO textbooks(isbn,title,edition,edition_seq,cover_color)"
            " VALUES('Y','数学','第1版',1,'#2f7d6e')").lastrowid
        self.conn.execute(
            "INSERT INTO shipment_items(shipment_id,book_id,qty) VALUES(?,? ,1)",
            (self.sid, b3))
        before = self.conn.execute(
            "SELECT qty FROM inventory WHERE book_id=?", (self.b1,)).fetchone()[0]
        with self.assertRaises(DomainError):
            self.svc.pick_and_pack(self.sid)
        after = self.conn.execute(
            "SELECT qty FROM inventory WHERE book_id=?", (self.b1,)).fetchone()[0]
        self.assertEqual(before, after)  # 已扣的回滚
        picked = self.conn.execute(
            "SELECT picked FROM shipment_items WHERE shipment_id=? AND book_id=?",
            (self.sid, self.b1)).fetchone()[0]
        self.assertEqual(picked, 0)

    def test_concurrent_picks_never_overdraw(self):
        """并发拣货：恰好够 N 单时只有 N 单成功，库存绝不为负。

        新建两个各需 1 本的包裹，库存仅 1 本，两线程并发：成功数恰为 1。
        """
        # 初始夹具的包裹 sid 占 100 本库存，这里用新书隔离竞争
        bk = self.conn.execute(
            "INSERT INTO textbooks(isbn,title,edition,edition_seq,cover_color)"
            " VALUES('Z','稀缺书','第1版',1,'#b5533e')").lastrowid
        self.svc.adjust_stock(bk, 1)
        ship_ids = []
        for i in range(2):
            cur = self.conn.execute(
                "INSERT INTO shipments(enrollment_id,kind,status,address_snapshot)"
                " VALUES(?, 'resend','planned','竞争地址')", (self.eid,))
            shid = cur.lastrowid
            self.conn.execute(
                "INSERT INTO shipment_items(shipment_id,book_id,qty) VALUES(?,? ,1)",
                (shid, bk))
            ship_ids.append(shid)

        results = []
        barrier = threading.Barrier(2)

        def worker(shid):
            # 每线程独立连接
            lc = connect(self.dir / "t.db")
            try:
                local_svc = FulfillmentService(lc, LogisticsService())
                barrier.wait()
                try:
                    local_svc.pick_and_pack(shid)
                    results.append(("ok", shid))
                except DomainError:
                    results.append(("fail", shid))
            finally:
                lc.close()

        t1 = threading.Thread(target=worker, args=(ship_ids[0],))
        t2 = threading.Thread(target=worker, args=(ship_ids[1],))
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertEqual(sorted(r[0] for r in results), ["fail", "ok"])
        qty = self.conn.execute("SELECT qty FROM inventory WHERE book_id=?",
                                (bk,)).fetchone()[0]
        self.assertEqual(qty, 0)

    def test_concurrent_initial_creation_single_winner(self):
        """并发为同一报名记录创建首次发放：只有一个赢家。"""
        # 用一个没有初始包裹的新报名记录
        eid2 = self.svc.enroll(self.stu, self.cls, "学员", "138", "另一地址")
        # 清掉 enroll 自动建的 initial（先删明细再删包裹，满足外键）
        old_sid = self.svc.initial_shipment(eid2)["id"]
        self.conn.execute("DELETE FROM shipment_items WHERE shipment_id=?", (old_sid,))
        self.conn.execute("DELETE FROM shipments WHERE id=?", (old_sid,))
        outcomes = []
        barrier = threading.Barrier(5)

        def worker():
            lc = connect(self.dir / "t.db")
            try:
                svc = FulfillmentService(lc, LogisticsService())
                barrier.wait()
                try:
                    svc.create_initial_shipment(eid2)
                    outcomes.append("ok")
                except DomainError:
                    outcomes.append("dup")
            finally:
                lc.close()

        ts = [threading.Thread(target=worker) for _ in range(5)]
        for t in ts: t.start()
        for t in ts: t.join()
        self.assertEqual(outcomes.count("ok"), 1)
        self.assertEqual(outcomes.count("dup"), 4)

    # ====================================================== 6. 退课拦截

    def test_withdraw_cancels_pending_and_restocks(self):
        self.svc.pick_and_pack(self.sid)
        picked_before = self.conn.execute(
            "SELECT qty FROM inventory WHERE book_id=?", (self.b1,)).fetchone()[0]
        self.svc.withdraw(self.eid, self.zhao, "不想学了")
        status = self.conn.execute(
            "SELECT status,cancel_reason FROM shipments WHERE id=?",
            (self.sid,)).fetchone()
        self.assertEqual(status["status"], "cancelled")
        # 已拣图书回库
        qty = self.conn.execute(
            "SELECT qty FROM inventory WHERE book_id=?", (self.b1,)).fetchone()[0]
        self.assertEqual(qty, picked_before + 1)

    def test_withdraw_blocks_new_shipment(self):
        """退课后：新异常/补发一律拦截，待发包裹已取消不能拣货。"""
        self.svc.withdraw(self.eid, self.zhao, "x")
        with self.assertRaises(DomainError):
            self.svc.submit_exception(self.eid, "damaged", "x", self.zhao)
        with self.assertRaises(DomainError):
            self.svc.pick_and_pack(self.sid)
        # 退课前提交的异常，退课后审批也被拦
        # （重新报名到同班验证 approve 路径）
        eid2 = self.svc.enroll(self.stu, self.cls, "学员", "138", "地址 C")
        rid = self.svc.submit_exception(eid2, "damaged", "x", self.zhao)
        self.svc.withdraw(eid2, self.zhao, "x")
        with self.assertRaises(DomainError):
            self.svc.approve_exception(rid, self.wang)

    def test_withdraw_rejects_pending_approvals(self):
        rid = self.svc.submit_exception(self.eid, "damaged", "x", self.zhao)
        arid = self.svc.request_address_change(self.eid, "学员", "139", "B", "")
        self.svc.withdraw(self.eid, self.zhao, "x")
        self.assertEqual(self.conn.execute(
            "SELECT status FROM exception_requests WHERE id=?", (rid,)).fetchone()[0],
            "rejected")
        self.assertEqual(self.conn.execute(
            "SELECT status FROM address_requests WHERE id=?", (arid,)).fetchone()[0],
            "rejected")

    def test_withdraw_keeps_shipped_history(self):
        """已交运包裹不取消，物流轨迹继续保留；但不能再产生新补发。"""
        self.svc.pick_and_pack(self.sid)
        self.svc.hand_to_logistics(self.sid)
        self.svc.withdraw(self.eid, self.zhao, "x")
        st = self.conn.execute("SELECT status FROM shipments WHERE id=?",
                               (self.sid,)).fetchone()[0]
        self.assertEqual(st, "shipped")
        # 物流照常回传，轨迹保留
        self.svc.logistics_callback(self.sid, "delivered", "网点")
        self.assertEqual(self.conn.execute(
            "SELECT status FROM shipments WHERE id=?", (self.sid,)).fetchone()[0],
            "delivered")
        # 新补发仍被拦截
        with self.assertRaises(DomainError):
            self.svc.submit_exception(self.eid, "damaged", "x", self.zhao)

    def test_withdraw_idempotent(self):
        self.svc.withdraw(self.eid, self.zhao, "x")
        with self.assertRaises(DomainError):
            self.svc.withdraw(self.eid, self.zhao, "y")

    # ====================================================== 7. 拒收 / 退回

    def test_rejected_logistics_restocks_and_resend(self):
        self.svc.pick_and_pack(self.sid)
        qty_after_pick = self.conn.execute(
            "SELECT qty FROM inventory WHERE book_id=?", (self.b1,)).fetchone()[0]
        self.svc.hand_to_logistics(self.sid)
        self.svc.logistics_callback(self.sid, "rejected", "拒收")
        self.assertEqual(self.conn.execute(
            "SELECT status FROM shipments WHERE id=?", (self.sid,)).fetchone()[0],
            "returned")
        # 图书退回入库
        self.assertEqual(self.conn.execute(
            "SELECT qty FROM inventory WHERE book_id=?", (self.b1,)).fetchone()[0],
            qty_after_pick + 1)
        # 审批后生成补发
        rid = self.svc.submit_exception(
            self.eid, "rejected_logistics", "拒收重寄", self.zhao)
        nsid = self.svc.approve_exception(rid, self.wang)
        self.assertIsNotNone(nsid)

    def test_invalid_transition_rejected(self):
        """未交运不能直接送达/签收。"""
        with self.assertRaises(DomainError):
            self.svc.logistics_callback(self.sid, "delivered", "网点")

    def test_signature_twice_rejected(self):
        self._full_ship()
        with self.assertRaises(DomainError):
            self.svc.confirm_signature(self.sid, "学员", self.dir)


if __name__ == "__main__":
    unittest.main(verbosity=2)
