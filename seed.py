# -*- coding: utf-8 -*-
"""种子数据：重置本地库并演示完整履约链路，自带错版案例。

人物：
  仓库 李仓管(u_wh_li)  前台 王老师(u_fd_wang)
  S001 张一鸣  已寄旧版，版次升级后挂补充资料（历史不抹）
  S002 李思    首包已打包待交运
  S003 王武    未打包，库存缺 1 本《计算机基础》
  S004 赵琳    物流拒收退回，异常单待前台审批
  S005 陈晨    包裹破损，补发已交运（升级补充随补发核销）
  S006 周雨    已打包未交运，申请改地址待批
  S007 孙悦    已签收，退课将被拦截
  S008 吴迪(C2602) 错寄：收到第1版，错寄异常待批，批后补发第2版
"""
import os
import shutil
from datetime import date, timedelta

from app import core, db as dbmod, logistics

T = date.today()


def seed():
    conn = dbmod.reset_db()
    proofs = os.path.join(dbmod.DATA_DIR, "proofs")
    if os.path.isdir(proofs):
        shutil.rmtree(proofs)

    conn.executemany(
        "INSERT INTO users(id,name,role,student_id) VALUES(?,?,?,?)",
        [("u_wh_li", "李仓管", "warehouse", None),
         ("u_fd_wang", "王老师（前台）", "frontdesk", None)] +
        [(f"u_{sid}", name, "student", sid)
         for sid, name in [
             ("S001", "张一鸣"), ("S002", "李思"), ("S003", "王武"),
             ("S004", "赵琳"), ("S005", "陈晨"), ("S006", "周雨"),
             ("S007", "孙悦"), ("S008", "吴迪")]],
    )

    conn.executemany(
        "INSERT INTO textbooks(code,title,cover_color) VALUES(?,?,?)",
        [("MATH", "高等数学", "#34507a"),
         ("ENG", "大学英语", "#7a3b52"),
         ("CS", "计算机基础", "#2f5d50")],
    )
    conn.executemany(
        "INSERT INTO textbook_editions(textbook_code,edition,label,"
        "released_on,diff_note) VALUES(?,?,?,?,?)",
        [("MATH", "1", "第1版", "2024-01-10", ""),
         ("MATH", "2", "第2版", "2026-08-20",
          "第2版重写第3章极限应用，新增两套综合习题；旧版第5章附录已删除。"),
         ("ENG", "1", "第1版", "2024-06-01", ""),
         ("ENG", "2", "第2版", "2026-09-01", "听力材料更换，课文顺序微调。"),
         ("CS", "1", "第1版", "2025-02-15", "")],
    )
    conn.executemany(
        "INSERT INTO classes(id,name,start_date) VALUES(?,?,?)",
        [("C2601", "2026春季一班", (T + timedelta(days=6)).isoformat()),
         ("C2602", "2026春季二班", (T + timedelta(days=21)).isoformat())],
    )
    # 注意：C2601 先以 MATH 第1版建计划，稍后再升级到第2版
    conn.executemany(
        "INSERT INTO class_materials(class_id,textbook_code,edition,qty) "
        "VALUES(?,?,?,?)",
        [("C2601", "MATH", "1", 1), ("C2601", "ENG", "1", 1),
         ("C2601", "CS", "1", 1),
         ("C2602", "MATH", "2", 1), ("C2602", "ENG", "1", 1)],
    )
    students = [("S001", "张一鸣", "13800000001"),
                ("S002", "李思", "13800000002"),
                ("S003", "王武", "13800000003"),
                ("S004", "赵琳", "13800000004"),
                ("S005", "陈晨", "13800000005"),
                ("S006", "周雨", "13800000006"),
                ("S007", "孙悦", "13800000007"),
                ("S008", "吴迪", "13800000008")]
    conn.executemany("INSERT INTO students(id,name,phone) VALUES(?,?,?)", students)

    addr = {  # 收件人默认与学员同名
        "S001": ("张一鸣", "北京市海淀区学院路1号"),
        "S002": ("李思", "北京市朝阳区光华路8号"),
        "S003": ("王武", "上海市杨浦区邯郸路2号"),
        "S004": ("赵琳", "广州市天河区五山路3号"),
        "S005": ("陈晨", "深圳市南山区科技园4号"),
        "S006": ("周雨", "杭州市西湖区文三路5号"),
        "S007": ("孙悦", "成都市武侯区一环路6号"),
        "S008": ("吴迪", "武汉市洪山区珞瑜路7号"),
    }
    enrollments = [
        ("E-S001", "S001", "C2601"), ("E-S002", "S002", "C2601"),
        ("E-S003", "S003", "C2601"), ("E-S004", "S004", "C2601"),
        ("E-S005", "S005", "C2601"), ("E-S006", "S006", "C2601"),
        ("E-S007", "S007", "C2601"), ("E-S008", "S008", "C2602"),
    ]
    for eid, sid, cid in enrollments:
        name, address = addr[sid]
        conn.execute(
            "INSERT INTO enrollments(id,student_id,class_id,status,"
            "receiver_name,receiver_phone,receiver_address,enrolled_on) "
            "VALUES(?,?,?, 'enrolled',?,?,?,?)",
            (eid, sid, cid, name,
             next(ph for s, _, ph in students if s == sid), address,
             (T - timedelta(days=12)).isoformat()),
        )

    # 库存（计算机基础备 7 本：6 次首发 + 破损补发 + 拒收补发后耗尽，S003 缺 1 本；
    # MATH 第2版 8 本，供换包、补发与升级补充）
    conn.executemany(
        "INSERT INTO inventory(textbook_code,edition,qty) VALUES(?,?,?)",
        [("MATH", "1", 20), ("MATH", "2", 8),
         ("ENG", "1", 20), ("ENG", "2", 6), ("CS", "1", 7)],
    )

    WH, FD = "u_wh_li", "u_fd_wang"

    # —— S001 正常签收（旧版时代寄出）——
    p1 = core.create_first_issuance(conn, "E-S001", WH)
    t1 = core.ship_parcel(conn, p1, WH)
    logistics.sign(conn, t1)

    # —— S002 已打包待交运 ——
    core.create_first_issuance(conn, "E-S002", WH)

    # —— S004 交运后被拒收，退回入库；仓库登记拒收异常，待前台审批 ——
    p4 = core.create_first_issuance(conn, "E-S004", WH)
    t4 = core.ship_parcel(conn, p4, WH)
    logistics.reject(conn, t4, reason="收件人电话无人接听，拒收退回")
    core.submit_exception(
        conn, "E-S004", p4, "rejected",
        "包裹被物流拒收并退回，学员要求重新确认地址后寄出",
        WH, internal_note="面单电话疑似少一位，已电话与学员核对为 13800000004")

    # —— S005 正常签收后反馈破损 ——
    p5 = core.create_first_issuance(conn, "E-S005", WH)
    t5 = core.ship_parcel(conn, p5, WH)
    logistics.sign(conn, t5, with_proof=False)

    # —— S007 正常签收 ——
    p7 = core.create_first_issuance(conn, "E-S007", WH)
    t7 = core.ship_parcel(conn, p7, WH)
    logistics.sign(conn, t7)

    # —— S006 已打包（旧版时期），稍后升级时换包；再提交改地址 ——
    p6 = core.create_first_issuance(conn, "E-S006", WH)

    # ===== 版次升级：C2601《高等数学》第1版 → 第2版 =====
    up = core.upgrade_edition(conn, "C2601", "MATH", "2", actor=FD)

    # S005 破损异常：升级之后由仓库登记、前台审批，补发按第2版并核销补充资料
    x5 = core.submit_exception(
        conn, "E-S005", p5, "damaged",
        "签收时外箱浸水，《高等数学》封底破损无法使用，申请补发",
        WH, internal_note="怀疑转运中心淋雨，照片已存工单 WX-0915")
    r5 = core.approve_exception(conn, x5, FD)
    core.ship_parcel(conn, r5, WH)

    # S006 改地址（包裹仍 packed，受理时应同步面单）
    core.request_address_change(
        conn, "E-S006", "杭州市余杭区未来科技城66号", "周雨",
        "13800000006", submitted_by="u_S006")

    # ===== S008 错版案例：C2602 计划是第2版，历史却错寄了第1版 =====
    # 以“历史导入”方式落一笔已签收的错版首包（业务系统之外的遗留数据）
    conn.execute("BEGIN IMMEDIATE")
    cur = conn.execute(
        "INSERT INTO parcels(enrollment_id,kind,status,receiver_name,"
        "receiver_phone,receiver_address,tracking_no,created_by,created_at,"
        "shipped_at,closed_at) VALUES('E-S008','first','delivered',?,?,?,"
        "?, '历史导入', ?, ?, ?)",
        ("吴迪", "13800000008", "武汉市洪山区珞瑜路7号",
         f"SFLEGACY08", core.now_iso(), core.now_iso(), core.now_iso()),
    )
    p8 = cur.lastrowid
    core._deduct_stock(conn, [
        {"textbook_code": "MATH", "edition": "1", "qty": 1},
        {"textbook_code": "ENG", "edition": "1", "qty": 1}],
        "historical_wrong_import", f"parcel#{p8}", "历史导入")
    conn.execute(
        "INSERT INTO parcel_items(parcel_id,textbook_code,edition,qty,item_title)"
        " VALUES(?,?,?,1,?)", (p8, "MATH", "1", "高等数学（错寄：第1版）"))
    conn.execute(
        "INSERT INTO parcel_items(parcel_id,textbook_code,edition,qty,item_title)"
        " VALUES(?,?,?,1,?)", (p8, "ENG", "1", "大学英语"))
    conn.execute(
        "INSERT INTO tracking_events(parcel_id,event,detail,at) VALUES(?,?,?,?)",
        (p8, "shipped", "历史数据导入：运单 SFLEGACY08", core.now_iso()))
    conn.execute(
        "INSERT INTO tracking_events(parcel_id,event,detail,at) VALUES(?,?,?,?)",
        (p8, "signed", "学员已签收（旧系统导入）", core.now_iso()))
    conn.execute("COMMIT")
    core.submit_exception(
        conn, "E-S008", p8, "wrong_sent",
        "班级计划为《高等数学》第2版，实际收到的是第1版，申请补发正确版次",
        WH, internal_note="旧系统开班计划配错，C2602 共需排查是否还有他人")

    conn.close()
    print("种子数据完成：")
    print(f"  版次升级：已寄旧版待补充 {len(up['supplemented'])} 人，"
          f"待交运换包 {len(up['swapped'])} 人")
    print("  待审批：S004 拒收异常、S008 错寄异常、S006 改地址")
    print("  待处理：S001/S007 版次升级补充资料，S003 缺书待发")


if __name__ == "__main__":
    seed()
