# -*- coding: utf-8 -*-
"""履约规则测试：版次规则、地址截止、补发幂等、库存竞争、退课拦截等。
每个用例使用独立临时库，互不影响。"""
import os
import tempfile
import threading
import unittest
from datetime import date, timedelta

from app import core, db as dbmod, logistics


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "t.db")
        self.conn = dbmod.reset_db(self.db_path)
        self.today = date(2026, 10, 1)
        self._fixture()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    # ---- 公共夹具：1 个班级、1 种教材两个版次、2 名学员 ----
    def _fixture(self, stock_v1=10, stock_v2=10):
        c = self.conn
        c.execute("INSERT INTO users VALUES('wh','仓管','warehouse',NULL)")
        c.execute("INSERT INTO users VALUES('fd','前台','frontdesk',NULL)")
        c.execute("INSERT INTO users VALUES('stu1','学员甲','student','S1')")
        c.execute("INSERT INTO textbooks VALUES('MATH','高等数学','#34507a')")
        c.execute("INSERT INTO textbook_editions VALUES('MATH','1','第1版','2024-01-01','')")
        c.execute("INSERT INTO textbook_editions VALUES('MATH','2','第2版','2026-09-01','第3章重写')")
        c.execute("INSERT INTO classes VALUES('C1','一班',?)",
                  ((self.today + timedelta(days=10)).isoformat(),))
        c.execute("INSERT INTO class_materials VALUES('C1','MATH','1',1)")
        c.execute("INSERT INTO students VALUES('S1','学员甲','13900000001')")
        c.execute("INSERT INTO students VALUES('S2','学员乙','13900000002')")
        for sid, eid in (("S1", "E1"), ("S2", "E2")):
            c.execute(
                "INSERT INTO enrollments(id,student_id,class_id,status,"
                "receiver_name,receiver_phone,receiver_address,enrolled_on) "
                "VALUES(?,?,?,'enrolled',?,?,?,?)",
                (eid, sid, "C1", sid, "139", f"{sid}市某路1号",
                 (self.today - timedelta(days=2)).isoformat()))
        c.execute("INSERT INTO inventory VALUES('MATH','1',?)", (stock_v1,))
        c.execute("INSERT INTO inventory VALUES('MATH','2',?)", (stock_v2,))

    def _first(self, eid="E1", actor="wh"):
        return core.create_first_issuance(self.conn, eid, actor)

    def _ship_sign(self, pid):
        t = core.ship_parcel(self.conn, pid, "wh")
        logistics.sign(self.conn, t, with_proof=False)
        return t


class TestFirstIssuance(Case):
    def test_first_only_once(self):
        """一个报名记录的首次发放只能创建一次（含取消后也不能重建）。"""
        pid = self._first()
        with self.assertRaises(core.FirstIssuanceExists):
            self._first()
        # 唯一索引层面再兜一次底
        with self.assertRaises(Exception):
            self.conn.execute(
                "INSERT INTO parcels(enrollment_id,kind,status,created_at) "
                "VALUES('E1','first','packed',?)", (core.now_iso(),))

    def test_first_blocks_on_missing_address(self):
        self.conn.execute("UPDATE enrollments SET receiver_address=NULL WHERE id='E2'")
        with self.assertRaises(core.BusinessError):
            self._first("E2")


class TestEditionRules(Case):
    def test_unshipped_pack_swaps_edition_and_restocks(self):
        """未交运：升级换版，旧版回库、新版扣减。"""
        pid = self._first("E2")
        r = core.upgrade_edition(self.conn, "C1", "MATH", "2")
        self.assertEqual(r["swapped"], ["E2"])
        ed = self.conn.execute(
            "SELECT edition FROM parcel_items WHERE parcel_id=?", (pid,)).fetchone()[0]
        self.assertEqual(ed, "2")
        after_v1 = self.conn.execute(
            "SELECT qty FROM inventory WHERE textbook_code='MATH' AND edition='1'").fetchone()[0]
        self.assertEqual(after_v1, 10)  # 首发扣 1，换版旧版回库后恢复初始
        v2 = self.conn.execute(
            "SELECT qty FROM inventory WHERE textbook_code='MATH' AND edition='2'").fetchone()[0]
        self.assertEqual(v2, 9)

    def test_shipped_old_edition_creates_supplement_keeps_history(self):
        """已寄旧版：生成补充资料；历史包裹版次原样保留。"""
        pid = self._first("E1")
        self._ship_sign(pid)
        r = core.upgrade_edition(self.conn, "C1", "MATH", "2")
        self.assertEqual(r["supplemented"], ["E1"])
        # 历史首包内件仍是第1版，不能被抹去/回写
        old = self.conn.execute(
            "SELECT edition FROM parcel_items WHERE parcel_id=?", (pid,)).fetchone()[0]
        self.assertEqual(old, "1")
        sup = self.conn.execute(
            "SELECT * FROM supplements WHERE enrollment_id='E1'").fetchone()
        self.assertEqual((sup["old_edition"], sup["new_edition"], sup["status"]),
                         ("1", "2", "pending"))
        # 补充资料发出
        spid = core.issue_supplements(self.conn, "E1", "wh")
        sed = self.conn.execute(
            "SELECT edition,is_supplement FROM parcel_items WHERE parcel_id=?",
            (spid,)).fetchone()
        self.assertEqual((sed["edition"], sed["is_supplement"]), ("2", 1))

    def test_upgrade_idempotent_supplement(self):
        """同一 old→new 的补充资料只生成一次。"""
        pid = self._first()
        self._ship_sign(pid)
        core.upgrade_edition(self.conn, "C1", "MATH", "2")
        # 再升级到同版应报错；即使重复调用也不会产生两条补充
        with self.assertRaises(core.BusinessError):
            core.upgrade_edition(self.conn, "C1", "MATH", "2")
        n = self.conn.execute(
            "SELECT COUNT(*) FROM supplements WHERE enrollment_id='E1'").fetchone()[0]
        self.assertEqual(n, 1)


class TestAddressCutoff(Case):
    def test_change_before_ship_syncs_packed_label(self):
        pid = self._first("E1")
        rid = core.request_address_change(
            self.conn, "E1", "新地址99号", "新收件人", "137", "stu1")
        core.approve_address_change(self.conn, rid, "fd")
        self.assertEqual(self.conn.execute(
            "SELECT receiver_address FROM parcels WHERE id=?", (pid,)).fetchone()[0],
            "新地址99号")

    def test_change_after_ship_rejected(self):
        """交运后申请与审批都必须失败。"""
        pid = self._first()
        core.ship_parcel(self.conn, pid, "wh")
        with self.assertRaises(core.BusinessError):
            core.request_address_change(self.conn, "E1", "晚了", None, None, "stu1")

    def test_approve_race_after_ship_auto_rejects(self):
        """申请时未交运、批准瞬间已交运：自动驳回。"""
        pid = self._first("E1")
        rid = core.request_address_change(
            self.conn, "E1", "压线地址", None, None, "stu1")
        core.ship_parcel(self.conn, pid, "wh")
        with self.assertRaises(core.BusinessError):
            core.approve_address_change(self.conn, rid, "fd")
        st = self.conn.execute(
            "SELECT status,note FROM address_requests WHERE id=?", (rid,)).fetchone()
        self.assertEqual(st["status"], "rejected")


class TestResendIdempotency(Case):
    def _exc(self, etype="damaged", submitter="wh"):
        pid = self._first()
        self._ship_sign(pid)
        xid = core.submit_exception(
            self.conn, "E1", pid, etype, "包装破损/错寄，申请补发", submitter,
            internal_note="内部排查备注")
        return pid, xid

    def test_reason_required(self):
        pid = self._first()
        with self.assertRaises(core.BusinessError):
            core.submit_exception(self.conn, "E1", pid, "damaged", "  ", "wh")

    def test_duplicate_exception_same_type_blocked(self):
        pid, xid = self._exc()
        with self.assertRaises(core.ResendAlreadyIssued):
            core.submit_exception(self.conn, "E1", pid, "damaged", "再来一次", "wh")

    def test_approve_once_only(self):
        """一张异常单只能审批一次、只能有一个补发包裹。"""
        pid, xid = self._exc()
        r1 = core.approve_exception(self.conn, xid, "fd")
        with self.assertRaises(core.BusinessError):
            core.approve_exception(self.conn, xid, "fd")
        n = self.conn.execute(
            "SELECT COUNT(*) FROM parcels WHERE exception_id=?", (xid,)).fetchone()[0]
        self.assertEqual(n, 1)
        self.assertEqual(r1, self.conn.execute(
            "SELECT id FROM parcels WHERE exception_id=?", (xid,)).fetchone()[0])

    def test_segregation_submitter_and_warehouse_cannot_approve(self):
        # 仓库不能审批（任意异常单）
        _, xid = self._exc()
        with self.assertRaises(core.BusinessError):
            core.approve_exception(self.conn, xid, "wh")
        # 学员提交、前台是另一人：可批；本人不可批自己的
        pid2 = self._first("E2")
        t2 = core.ship_parcel(self.conn, pid2, "wh")
        logistics.reject(self.conn, t2)
        xid2 = core.submit_exception(
            self.conn, "E2", pid2, "rejected", "拒收未收到", "stu1")
        with self.assertRaises(core.BusinessError):
            core.approve_exception(self.conn, xid2, "stu1")
        core.approve_exception(self.conn, xid2, "fd")  # 前台可批

    def test_wrong_sent_resends_current_edition(self):
        """错寄补发按当前计划版次；破损补发若遇升级也按新版，并核销补充。"""
        pid = self._first()
        self._ship_sign(pid)
        xid = core.submit_exception(
            self.conn, "E1", pid, "wrong_sent", "收到第1版，计划是第2版", "wh")
        core.upgrade_edition(self.conn, "C1", "MATH", "2")
        rpid = core.approve_exception(self.conn, xid, "fd")
        ed = self.conn.execute(
            "SELECT edition FROM parcel_items WHERE parcel_id=?", (rpid,)).fetchone()[0]
        self.assertEqual(ed, "2")
        # 升级产生的 pending 补充已随补发核销，不重复寄
        pending = self.conn.execute(
            "SELECT COUNT(*) FROM supplements WHERE enrollment_id='E1' AND status='pending'"
        ).fetchone()[0]
        self.assertEqual(pending, 0)


class TestStockContention(unittest.TestCase):
    """库存竞争：库存只够一个包裹时，并发起两个首发，恰好一个成功一个失败，
    库存不能为负、不能超卖。"""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "t.db")
        dbmod.reset_db(self.db_path)
        c = dbmod.get_conn(self.db_path)
        c.execute("INSERT INTO users VALUES('wh','仓管','warehouse',NULL)")
        c.execute("INSERT INTO textbooks VALUES('B','教材','#000')")
        c.execute("INSERT INTO textbook_editions VALUES('B','1','第1版','2024-01-01','')")
        c.execute("INSERT INTO classes VALUES('C','班','2026-10-20')")
        c.execute("INSERT INTO class_materials VALUES('C','B','1',1)")
        c.execute("INSERT INTO inventory VALUES('B','1',1)")  # 全局只剩 1 本
        for i in range(2):
            sid, eid = f"S{i}", f"E{i}"
            c.execute("INSERT INTO students VALUES(?,?,?)", (sid, sid, "139"))
            c.execute(
                "INSERT INTO enrollments(id,student_id,class_id,status,"
                "receiver_name,receiver_phone,receiver_address,enrolled_on) "
                "VALUES(?,?,'C','enrolled',?,'139','地址','2026-09-01')",
                (eid, sid, sid))
        c.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_concurrent_first_issuance_oversell(self):
        results = {}
        barrier = threading.Barrier(2)

        def worker(eid, idx):
            conn = dbmod.get_conn(self.db_path)
            barrier.wait()
            try:
                core.create_first_issuance(conn, eid, "wh")
                results[idx] = "ok"
            except core.InsufficientStock:
                results[idx] = "short"
            except Exception as e:  # noqa
                results[idx] = f"err:{type(e).__name__}"
            finally:
                conn.close()

        t1 = threading.Thread(target=worker, args=("E0", 0))
        t2 = threading.Thread(target=worker, args=("E1", 1))
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertEqual(sorted(results.values()), ["ok", "short"])
        c = dbmod.get_conn(self.db_path)
        qty = c.execute("SELECT qty FROM inventory WHERE textbook_code='B'").fetchone()[0]
        parcels = c.execute(
            "SELECT COUNT(*) FROM parcels WHERE kind='first' AND status='packed'").fetchone()[0]
        c.close()
        self.assertEqual(qty, 0)
        self.assertEqual(parcels, 1)  # 没有超卖


class TestWithdraw(Case):
    def test_withdraw_shipped_blocked(self):
        """在途/已签收包裹拦截退课。"""
        pid = self._first()
        core.ship_parcel(self.conn, pid, "wh")
        with self.assertRaises(core.BusinessError):
            core.withdraw_enrollment(self.conn, "E1")
        self.assertEqual(self.conn.execute(
            "SELECT status FROM enrollments WHERE id='E1'").fetchone()[0], "enrolled")

    def test_withdraw_packed_cancels_and_restocks(self):
        """未交运：取消包裹、教材回库、待审异常与地址申请关闭。"""
        pid = self._first("E2")
        core.request_address_change(self.conn, "E2", "改地址", None, None, "stu1")
        core.withdraw_enrollment(self.conn, "E2")
        self.assertEqual(self.conn.execute(
            "SELECT status FROM parcels WHERE id=?", (pid,)).fetchone()[0], "cancelled")
        qty = self.conn.execute(
            "SELECT qty FROM inventory WHERE textbook_code='MATH' AND edition='1'").fetchone()[0]
        self.assertEqual(qty, 10)  # 扣 1 又回 1
        self.assertEqual(self.conn.execute(
            "SELECT status FROM address_requests WHERE enrollment_id='E2'").fetchone()[0],
            "rejected")
        # 退课后不可再发放
        with self.assertRaises(core.BusinessError):
            self._first("E2")

    def test_rejected_parcel_allows_withdraw(self):
        """拒收已退回入库后允许退课。"""
        pid = self._first()
        t = core.ship_parcel(self.conn, pid, "wh")
        logistics.reject(self.conn, t)
        core.withdraw_enrollment(self.conn, "E1")  # 不抛异常
        self.assertEqual(self.conn.execute(
            "SELECT status FROM enrollments WHERE id='E1'").fetchone()[0], "withdrawn")


class TestStudentView(Case):
    def test_student_sees_only_own_and_no_internal_note(self):
        self._first("E1")
        self._first("E2")
        mine = core.student_parcels(self.conn, "S1")
        self.assertTrue(all(p["enrollment_id"] == "E1" for p in mine))
        # 无内部备注字段
        self.assertNotIn("internal_note", repr(mine))


if __name__ == "__main__":
    unittest.main(verbosity=2)
