"""种子数据：内置错版案例与全部异常场景。

场景一览：
  E1 陈学员  已签旧版 → 版次升级生成「补充资料」；交运后改址（仅对后续补发有效）
  E2 林学员  待发 → 版次升级后包裹明细换成新版；交运前改址审批中
  E3 黄学员  错寄异常（王仓提交，待他人审批——自审批被禁）
  E4 周学员  物流拒收已退回回库 → 拒收异常已批 → 补发包裹待拣货
  E5 吴学员  包裹破损异常待审批
  E6 郑学员  已有一次补发签收，又提交「重复补发」异常（审批只登记不再发）
  E7 孙学员  开课前退课，未交运包裹取消并回库
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

from db import connect, init_db
from logistics import LogisticsService
from services import FulfillmentService, DomainError


def seed(conn: sqlite3.Connection, data_dir: str | Path) -> None:
    init_db(conn)
    svc = FulfillmentService(conn, LogisticsService())
    today = date.today()

    def d(offset: int) -> str:
        return (today + timedelta(days=offset)).isoformat()

    # ------------------------------------------------------------- 人员
    users = [
        ("wangc", "王仓",   "warehouse"),
        ("liku",  "李库",   "warehouse"),
        ("zhaoq", "赵前台", "frontdesk"),
        ("chen",  "陈学员", "student"),
        ("lin",   "林学员", "student"),
        ("huang", "黄学员", "student"),
        ("zhou",  "周学员", "student"),
        ("wu",    "吴学员", "student"),
        ("zheng", "郑学员", "student"),
        ("sun",   "孙学员", "student"),
    ]
    uid = {}
    for username, name, role in users:
        cur = conn.execute(
            "INSERT INTO users(username,name,role) VALUES(?,?,?)",
            (username, name, role))
        uid[username] = cur.lastrowid

    # ------------------------------------------------------------- 教材（两版）
    py1 = conn.execute(
        "INSERT INTO textbooks(isbn,title,edition,edition_seq,cover_color)"
        " VALUES('978-7-PY','Python 编程','第1版',1,'#7c5cbf')").lastrowid
    py2 = conn.execute(
        "INSERT INTO textbooks(isbn,title,edition,edition_seq,cover_color,supersedes_id)"
        " VALUES('978-7-PY','Python 编程','第2版',2,'#5b8def',?)", (py1,)).lastrowid
    math3 = conn.execute(
        "INSERT INTO textbooks(isbn,title,edition,edition_seq,cover_color)"
        " VALUES('978-7-MA','高等数学','第3版',3,'#2f7d6e')").lastrowid
    eng4 = conn.execute(
        "INSERT INTO textbooks(isbn,title,edition,edition_seq,cover_color)"
        " VALUES('978-7-EN','英语精读','第4版',4,'#b5533e')").lastrowid

    # ------------------------------------------------------------- 班级
    c1 = conn.execute(
        "INSERT INTO classes(name,start_date) VALUES(?,?)",
        ("软件开发班 2026 秋", d(20))).lastrowid
    c2 = conn.execute(
        "INSERT INTO classes(name,start_date) VALUES(?,?)",
        ("高数冲刺班 2026 秋", d(5))).lastrowid
    c3 = conn.execute(
        "INSERT INTO classes(name,start_date) VALUES(?,?)",
        ("英语提升班 2026 秋", d(40))).lastrowid
    for bid in (py1,):
        conn.execute("INSERT INTO class_books(class_id,book_id) VALUES(?,?)", (c1, bid))
    conn.execute("INSERT INTO class_books(class_id,book_id) VALUES(?,?)", (c2, math3))
    conn.execute("INSERT INTO class_books(class_id,book_id) VALUES(?,?)", (c3, eng4))

    # 库存：新版第2版期初为 0 —— 待发列表开箱即显示缺书，需补货后才能拣货
    for bid, qty in ((py1, 40), (math3, 20), (eng4, 20), (py2, 0)):
        svc.adjust_stock(bid, qty, reason="期初入库")

    # ------------------------------------------------------------- 报名（自动生成首發计划）
    student_names = {"chen": "陈学员", "lin": "林学员", "huang": "黄学员",
                     "zhou": "周学员", "wu": "吴学员", "zheng": "郑学员",
                     "sun": "孙学员"}

    def enroll(student: str, cls: int, addr: str, phone="13800000000") -> int:
        return svc.enroll(uid[student], cls, student_names[student], phone, addr)

    e1 = enroll("chen", c1, "北京市海淀区中关村大街 1 号")
    e2 = enroll("lin", c1, "上海市浦东新区张江路 88 号")
    e6 = enroll("zheng", c1, "广州市天河区体育西路 10 号")
    e3 = enroll("huang", c2, "深圳市南山区科技园南路 5 号")
    e4 = enroll("zhou", c2, "杭州市西湖区文三路 66 号")
    e5 = enroll("wu", c3, "成都市武侯区人民南路 12 号")
    e7 = enroll("sun", c3, "武汉市洪山区珞瑜路 99 号")

    initial_id = lambda eid: svc.initial_shipment(eid)["id"]

    def full_ship(eid: int, signer: str):
        """拣货→交运→送达→签收 的完整链路。"""
        sid = initial_id(eid)
        svc.pick_and_pack(sid)
        svc.hand_to_logistics(sid)
        svc.logistics_callback(sid, "in_transit", "转运中心")
        svc.logistics_callback(sid, "out_for_delivery", "派送网点")
        svc.logistics_callback(sid, "delivered", "目的地网点")
        svc.confirm_signature(sid, signer, data_dir)
        return sid

    # ---- E1 陈学员：旧版已签收（错版案例的「已寄旧版」）
    s1 = full_ship(e1, "陈学员")

    # E1 交运后申请改址 → 审批后只能作用于后续补发
    ar1 = svc.request_address_change(e1, "陈学员", "13800000000",
                                     "北京市朝阳区望京 SOHO T3", "搬家了")
    svc.approve_address_change(ar1, uid["zhaoq"])

    # ---- E3 黄学员：正常签收，之后发现错寄，王仓提交异常待审批
    full_ship(e3, "黄学员")
    svc.submit_exception(e3, "wrong_book",
                         "学员反馈收到《线性代数》而非《高等数学 第3版》，要求补发",
                         uid["wangc"], evidence="包裹照片 IMG_3301.jpg",
                         warehouse_note="疑似拣货位相邻拿错，需复盘货架标签")

    # ---- E4 周学员：交运后物流拒收 → 退回回库 → 前台批复发 → 补发待拣
    s4 = initial_id(e4)
    svc.pick_and_pack(s4)
    svc.hand_to_logistics(s4)
    svc.logistics_callback(s4, "in_transit", "杭州转运中心")
    svc.logistics_callback(s4, "rejected", "派送网点：收件人拒收")
    exc4 = svc.submit_exception(e4, "rejected_logistics",
                                "物流拒收退回，学员确认电话填写有误，要求重寄",
                                uid["zhaoq"], evidence="物流退回记录")
    # 审批人不是提交人即可；此处由仓库李库审批
    svc.approve_exception(exc4, uid["liku"])

    # ---- E5 吴学员：签收后包裹破损
    full_ship(e5, "吴学员")
    svc.submit_exception(e5, "damaged",
                         "外箱进水，《英语精读 第4版》封底脱落，申请补发",
                         uid["zhaoq"], evidence="破损照片 IMG_5510.jpg",
                         warehouse_note="雨季注意加防水袋")

    # ---- E6 郑学员：历史上已补发一次并签收；现又提重复补发
    full_ship(e6, "郑学员")
    exc6a = svc.submit_exception(e6, "damaged", "首次包裹书角压损",
                                 uid["zhaoq"])
    r6 = svc.approve_exception(exc6a, uid["liku"])
    svc.pick_and_pack(r6)
    svc.hand_to_logistics(r6)
    svc.logistics_callback(r6, "delivered", "广州派送网点")
    svc.confirm_signature(r6, "郑学员", data_dir)
    svc.submit_exception(e6, "duplicate_resend",
                         "学员再次声称未收到书，要求再发一本",
                         uid["zhaoq"],
                         warehouse_note="两次签收凭证均在，疑似重复申请，需核实")

    # ---- E7 孙学员：开课前退课，未交运包裹取消
    svc.withdraw(e7, uid["zhaoq"], "学员工作调动，无法参加课程")

    # ---- E2 林学员：交运前改址申请（待前台审批）
    svc.request_address_change(e2, "林学员", "13900000000",
                               "上海市静安区南京西路 1266 号", "公司地址更方便收件")

    # ------------------------------------------------------- 版次升级（最后）
    # 班级改用第2版：E1/E6 已寄旧版 → 补充资料清单；E2 待发包裹直接换新版。
    svc.upgrade_edition(
        c1, py1, py2,
        diff_note="第2版差异：第 4 章重写并新增异步编程章节；"
                  "第 7 章习题替换 30%。旧版读者请随补发新版教材配套学习，"
                  "原第1版笔记中的章节号需对照新版目录调整。")

    return {
        "users": uid,
        "books": {"py1": py1, "py2": py2, "math3": math3, "eng4": eng4},
        "classes": {"c1": c1, "c2": c2, "c3": c3},
        "enrollments": {"e1": e1, "e2": e2, "e3": e3, "e4": e4,
                        "e5": e5, "e6": e6, "e7": e7},
    }


def reset_and_seed(db_path: str | Path, data_dir: str | Path) -> dict:
    p = Path(db_path)
    for suffix in ("", "-wal", "-shm"):
        q = p.with_name(p.name + suffix)
        if q.exists():
            q.unlink()
    conn = connect(db_path)
    try:
        return seed(conn, data_dir)
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "data/fulfillment.db"
    refs = reset_and_seed(target, "data")
    print("种子数据已写入", target)
    print("场景：错版补充 / 交运前后改址 / 错寄 / 拒收补发 / 破损 / 重复补发 / 退课")
    print(refs)
