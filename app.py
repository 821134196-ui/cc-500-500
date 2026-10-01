"""教材履约系统 —— 单文件 Web 应用（标准库 http.server，无外部依赖）。

启动：python3 app.py [--port 8000] [--reset]
三类视图：
  仓库  待发列表 / 拣包单 / 交运 / 库存
  前台  物流跟踪 / 签收查看 / 补发与改址审批 / 退课
  学员  仅自己的包裹（隐藏仓库内部备注）/ 申请改址 / 申请补发
"""
from __future__ import annotations

import argparse
import mimetypes
import os
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from db import connect
from logistics import LogisticsService
from seed import reset_and_seed
from services import FulfillmentService, DomainError

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "fulfillment.db"

STATUS_LABEL = {
    "planned": "待拣货", "picking": "已拣货待交运", "shipped": "运输中",
    "delivered": "已送达", "signed": "已签收", "returned": "已退回",
    "cancelled": "已取消",
}
KIND_LABEL = {"initial": "首次发放", "resend": "补发"}
EXC_LABEL = {"wrong_book": "错寄", "rejected_logistics": "物流拒收",
             "damaged": "包裹破损", "duplicate_resend": "重复补发"}
EVENT_LABEL = {
    "picked_up": "仓库交接", "in_transit": "运输中",
    "out_for_delivery": "派送中", "delivered": "已送达",
    "signed": "已签收", "rejected": "拒收退回", "return": "退回入库",
}
ROLE_LABEL = {"warehouse": "仓库", "frontdesk": "前台", "student": "学员"}


# ================================================================ 页面渲染

def esc(x) -> str:
    from html import escape
    return escape("" if x is None else str(x))


def q(params: dict, **extra) -> str:
    merged = {k: v for k, v in params.items() if v is not None}
    merged.update(extra)
    return urllib.parse.urlencode(merged)


CSS = """
:root{--paper:#fbfaf6;--ink:#2b2a27;--ink2:#5b5750;--line:#e3ddd2;
      --accent:#5b8def;--warn:#b5533e;--ok:#2f7d6e;}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
 font:15px/1.6 "Songti SC","Noto Serif CJK SC",serif;}
header{border-bottom:2px solid var(--ink);background:#fff;padding:10px 22px;
 display:flex;align-items:baseline;gap:18px;flex-wrap:wrap}
header h1{font-size:19px;margin:0;letter-spacing:2px}
nav a{margin-right:14px;color:var(--ink2);text-decoration:none}
nav a:hover{color:var(--accent)}
.who{margin-left:auto;font-size:13px;color:var(--ink2)}
.who select{font:inherit;padding:2px 4px}
main{max-width:1080px;margin:22px auto;padding:0 18px}
h2{font-size:17px;border-left:4px solid var(--accent);padding-left:10px;margin:26px 0 12px}
.card{background:#fff;border:1px solid var(--line);border-radius:6px;padding:14px 16px;margin:12px 0}
table{border-collapse:collapse;width:100%;background:#fff;font-size:14px}
th,td{border:1px solid var(--line);padding:7px 10px;text-align:left;vertical-align:top}
th{background:#f3efe6;font-weight:600}
.tag{display:inline-block;padding:1px 9px;border-radius:10px;font-size:12px;color:#fff}
.s-planned{background:#8a857c}.s-picking{background:#a9823f}
.s-shipped{background:#5b8def}.s-delivered{background:#6f6a61}
.s-signed{background:#2f7d6e}.s-returned{background:#b5533e}
.s-cancelled{background:#aaa}
.dim{color:var(--ink2);font-size:13px}
.warn{color:var(--warn);font-weight:600}
.ok{color:var(--ok)}
form.inline{display:inline}
button{font:inherit;border:1px solid var(--ink2);background:var(--paper);
 border-radius:4px;padding:3px 12px;cursor:pointer}
button.primary{background:var(--ink);color:#fff;border-color:var(--ink)}
button.danger{color:var(--warn);border-color:var(--warn)}
input,textarea,select{font:inherit;padding:4px 7px;border:1px solid var(--line);
 border-radius:4px;background:#fff}
.flash{padding:9px 14px;border-radius:5px;margin:10px 0}
.flash.ok{background:#e7f1ee;border:1px solid #b9d6cf}
.flash.err{background:#f7e9e5;border:1px solid #e0b8ad}
.edition-diff{background:#f6f3ea;border-left:3px solid #a9823f;padding:8px 12px;
 font-size:13px;color:#5a5142;margin:6px 0}
.swatch{display:inline-block;width:11px;height:11px;border-radius:2px;margin-right:5px;
 vertical-align:middle;border:1px solid rgba(0,0,0,.15)}
.sigimg{max-width:380px;border:1px solid var(--line);border-radius:4px;background:#fff}
.small{font-size:12.5px}
"""


def page(title: str, who, users, body: str, flash="") -> str:
    from html import escape
    opts = []
    for u in users:
        sel = " selected" if who and u["id"] == who["id"] else ""
        opts.append(f'<option value="{u["username"]}"{sel}>'
                    f'{escape(u["name"])}（{ROLE_LABEL[u["role"]]}）</option>')
    role = who["role"] if who else "warehouse"
    if role == "student":
        nav = ('<a href="/student">我的包裹</a>'
               '<a href="/address">改地址申请</a>'
               '<a href="/resend">申请补发</a>')
    else:
        nav = ('<a href="/pending">待发列表</a>'
               '<a href="/plan">班级发放计划</a>'
               '<a href="/tracking">物流跟踪</a>'
               '<a href="/approvals">审批中心</a>'
               '<a href="/inventory">教材库存</a>')
    flash_html = ""
    if flash:
        cls = "ok" if flash[0] == "o" else "err"
        flash_html = f'<div class="flash {cls}">{escape(flash[1])}</div>'
    return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>{escape(title)} · 教材履约</title><style>{CSS}</style></head>
<body>
<header><h1>教材履约台</h1><nav>{nav}</nav>
<form class="who" method="get" action="/switch">身份：
<select name="u" onchange="this.form.submit()">{''.join(opts)}</select></form></header>
<main>{flash_html}{body}</main></body></html>"""


class App:
    """ThreadingHTTPServer 下每个工作线程持有自己的 SQLite 连接。"""

    def __init__(self, db_path):
        self.db_path = db_path
        self.logistics = LogisticsService()
        self._local = threading.local()

    @property
    def conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = connect(self.db_path)
            self._local.conn = c
        return c

    @property
    def svc(self):
        return FulfillmentService(self.conn, self.logistics)

    # --------------------------------------------------------- 小工具
    def user(self, username: str):
        return self.conn.execute("SELECT * FROM users WHERE username=?",
                                 (username,)).fetchone()

    def require_user(self, params: dict):
        u = self.user(params.get("u", "wangc"))
        if not u:
            raise DomainError("未知用户")
        return u

    def require_staff(self, params: dict):
        u = self.require_user(params)
        if u["role"] == "student":
            raise DomainError("该功能仅限仓库/前台")
        return u

    def shipment_rows(self, where: str = "", args=()):
        return self.conn.execute(
            f"""SELECT s.*, e.id enrollment_id, e.status enr_status, c.name class_name,
                       u.name student_name, c.start_date
                FROM shipments s
                JOIN enrollments e ON e.id=s.enrollment_id
                JOIN classes c ON c.id=e.class_id
                JOIN users u ON u.id=e.student_id
                {where} ORDER BY s.id""", args).fetchall()

    def items_of(self, sid: int):
        return self.conn.execute(
            """SELECT si.*, t.title, t.edition, t.cover_color, t.isbn
               FROM shipment_items si JOIN textbooks t ON t.id=si.book_id
               WHERE si.shipment_id=?""", (sid,)).fetchall()

    def events_of(self, sid: int):
        return self.conn.execute(
            "SELECT * FROM tracking_events WHERE shipment_id=? ORDER BY id",
            (sid,)).fetchall()

    # --------------------------------------------------------- GET 页面
    def view_pending(self, params, user) -> str:
        rows = self.svc.pending_list()
        trs = []
        for r in rows:
            short = self.svc.shipment_shortage(r["shipment_id"])
            miss = sum(x.missing for x in short)
            d = r["days_to_start"]
            if r["kind"] == "resend":
                cd = '<span class="dim">补发</span>'
            elif d < 0:
                cd = f'<span class="warn">已超开课 {-d} 天</span>'
            elif d <= 7:
                cd = f'<span class="warn">{d} 天后开课</span>'
            else:
                cd = f'{d} 天后开课'
            miss_html = (f'<span class="warn">缺 {miss} 本</span>'
                         if miss else '<span class="ok">库存齐套</span>')
            trs.append(f"""<tr><td>{r['shipment_id']}</td>
              <td>{esc(r['student_name'])}<div class="dim">{esc(r['class_name'])}</div></td>
              <td>{KIND_LABEL[r['kind']]}</td>
              <td>{STATUS_LABEL[r['status']]}</td><td>{cd}</td><td>{miss_html}</td>
              <td><a href="/shipment?{q(params, id=r['shipment_id'])}">拣包单</a></td></tr>""")
        return f"""<h2>待发列表（按开课倒计时排序）</h2>
        <table><tr><th>包裹</th><th>学员/班级</th><th>类型</th><th>状态</th>
        <th>开课倒计时</th><th>缺书</th><th></th></tr>{''.join(trs)}</table>"""

    def view_plan(self, params, user) -> str:
        classes = self.conn.execute("SELECT * FROM classes ORDER BY start_date").fetchall()
        blocks = []
        for c in classes:
            books = self.conn.execute(
                """SELECT t.* FROM class_books cb JOIN textbooks t ON t.id=cb.book_id
                   WHERE cb.class_id=? ORDER BY t.edition_seq""", (c["id"],)).fetchall()
            stats = self.conn.execute(
                """SELECT s.status, COUNT(DISTINCT s.id) n FROM shipments s
                   JOIN enrollments e ON e.id=s.enrollment_id
                   WHERE e.class_id=? GROUP BY s.status""", (c["id"],)).fetchall()
            stat_txt = "、".join(f"{STATUS_LABEL[r['status']]} {r['n']}" for r in stats)
            sups = self.conn.execute(
                """SELECT es.*, u.name student_name, ot.edition old_e, nt.edition new_e
                   FROM edition_supplements es
                   JOIN enrollments e ON e.id=es.enrollment_id
                   JOIN users u ON u.id=e.student_id
                   JOIN textbooks ot ON ot.id=es.old_book_id
                   JOIN textbooks nt ON nt.id=es.new_book_id
                   WHERE e.class_id=? ORDER BY es.id""", (c["id"],)).fetchall()
            sup_html = ""
            if sups:
                lis = []
                for s in sups:
                    state = ("已发补充包裹 #" + str(s["shipment_id"])
                             if s["shipment_id"] else '<span class="warn">待发补充资料</span>')
                    btn = ""
                    if not s["shipment_id"]:
                        btn = (f'<form class="inline" method="post" action="/issue_supplement">'
                               f'<input type="hidden" name="u" value="{esc(user["username"])}">'
                               f'<input type="hidden" name="sid" value="{s["id"]}">'
                               f'<button>发放补充资料</button></form>')
                    lis.append(f"<li>{esc(s['student_name'])}：{esc(s['old_e'])} → "
                               f"{esc(s['new_e'])} {state} {btn}"
                               f"<div class='edition-diff'>版次差异说明：{esc(s['note'])}</div></li>")
                sup_html = ("<h2 style='border-color:#a9823f'>版次升级 · 补充资料清单"
                            f"（旧版已寄，历史保留）</h2><ul>{''.join(lis)}</ul>")
            book_html = "、".join(
                f'<span class="swatch" style="background:{esc(b["cover_color"])}"></span>'
                f'{esc(b["title"])} {esc(b["edition"])}' for b in books)
            blocks.append(f"""<div class="card">
              <b>{esc(c['name'])}</b> · 开课 {esc(c['start_date'])}
              <div>教材：{book_html}</div>
              <div class="dim small">发放进度：{esc(stat_txt) or '尚无包裹'}</div>
              </div>{sup_html}""")
        return "<h2>班级发放计划</h2>" + "".join(blocks)

    def view_shipment(self, params, user) -> str:
        sid = int(params["id"])
        s = self.shipment_rows("WHERE s.id=?", (sid,))[0]
        items = self.items_of(sid)
        events = self.events_of(sid)
        short = self.svc.shipment_shortage(sid)
        is_student = user["role"] == "student"
        if is_student and self.conn.execute(
                "SELECT 1 FROM shipments s JOIN enrollments e ON e.id=s.enrollment_id"
                " WHERE s.id=? AND e.student_id=?", (sid, user["id"])).fetchone() is None:
            raise DomainError("只能查看自己的包裹")
        item_trs = []
        for it in items:
            mark = '<span class="ok">已拣</span>' if it["picked"] else "待拣"
            item_trs.append(
                f"<tr><td><span class='swatch' style='background:{esc(it['cover_color'])}'>"
                f"</span>{esc(it['title'])}</td><td>{esc(it['edition'])}</td>"
                f"<td>{esc(it['isbn'])}</td><td>{it['qty']}</td><td>{mark}</td></tr>")
        shortage_html = ""
        if short and s["status"] in ("planned", "picking"):
            shortage_html = '<div class="card"><span class="warn">库存缺口：</span><ul>' + \
                "".join(f"<li>{esc(x.title)} {esc(x.edition)}：需 {x.required}，"
                        f"现 {x.available}，缺 {x.missing}</li>" for x in short) + "</ul></div>"
        ev_html = "".join(
            f"<li>{esc(e['occurred_at'])} · {EVENT_LABEL.get(e['event'], e['event'])}"
            f"{' · ' + esc(e['location']) if e['location'] else ''}</li>"
            for e in events) or "<li class='dim'>暂无物流轨迹</li>"
        sig = self.conn.execute("SELECT * FROM signatures WHERE shipment_id=?",
                                (sid,)).fetchone()
        sig_html = ""
        if sig:
            sig_html = f"""<div class="card"><b>签收凭证</b>
              <div class="dim">签收人：{esc(sig['signer'])} · {esc(sig['signed_at'])}</div>
              <a href="/signature/{esc(sig['image_file'])}">
              <img class="sigimg" src="/signature/{esc(sig['image_file'])}"></a></div>"""

        actions = []
        if not is_student:
            if s["status"] == "planned" and not short:
                actions.append(("pick", "拣书打包（扣减库存）", "primary"))
            if s["status"] == "picking":
                actions.append(("handoff", "交运并生成运单", "primary"))
            if s["status"] == "shipped":
                actions += [("evt_in_transit", "回传：运输中", ""),
                            ("evt_out_for_delivery", "回传：派送中", ""),
                            ("evt_delivered", "回传：已送达", ""),
                            ("evt_rejected", "回传：拒收退回", "danger")]
            if s["status"] == "delivered":
                actions.append(("sign", "确认签收（回传签收图）", "primary"))
        btns = " ".join(
            f'<form class="inline" method="post" action="/shipment_action">'
            f'<input type="hidden" name="u" value="{esc(user["username"])}">'
            f'<input type="hidden" name="id" value="{sid}">'
            f'<input type="hidden" name="act" value="{a}">'
            f'<button class="{cls}">{esc(label)}</button></form>'
            for a, label, cls in actions)
        return f"""<h2>拣包单 / 包裹 #{sid}
          <span class="tag s-{s['status']}">{STATUS_LABEL[s['status']]}</span></h2>
        <div class="card">
          学员：{esc(s['student_name'])} · 班级：{esc(s['class_name'])} ·
          类型：{KIND_LABEL[s['kind']]}<br>
          收货地址（{'交运快照，不可改' if s['status'] in
                    ('shipped','delivered','signed','returned') else '待发，可随改址更新'}）：
          {esc(s['address_snapshot'])}<br>
          运单号：{esc(s['tracking_no'] or '—')}
          {('<br><span class="warn">学员已退课</span>') if s['enr_status']=='withdrawn' else ''}
        </div>
        <table><tr><th>教材</th><th>版次</th><th>ISBN</th><th>数量</th><th>拣货</th></tr>
        {''.join(item_trs)}</table>
        {shortage_html}
        <h2>物流跟踪</h2><div class="card"><ul>{ev_html}</ul></div>
        {sig_html}
        <div style="margin:14px 0">{btns}</div>
        <a href="/pending?{q(params)}">← 返回待发列表</a>"""

    def view_tracking(self, params, user) -> str:
        rows = self.shipment_rows(
            "WHERE s.status IN ('shipped','delivered','signed','returned')")
        trs = []
        for r in rows:
            sig = self.conn.execute(
                "SELECT image_file FROM signatures WHERE shipment_id=?",
                (r["id"],)).fetchone()
            sig_link = (f'<a href="/signature/{esc(sig["image_file"])}">签收图</a>'
                        if sig else "—")
            trs.append(f"<tr><td>{r['id']}</td><td>{esc(r['student_name'])}</td>"
                       f"<td>{KIND_LABEL[r['kind']]}</td>"
                       f"<td><span class='tag s-{r['status']}'>"
                       f"{STATUS_LABEL[r['status']]}</span></td>"
                       f"<td>{esc(r['tracking_no'] or '—')}</td>"
                       f"<td>{esc(r['shipped_at'] or '—')}</td>"
                       f"<td>{esc(r['signed_at'] or '—')}</td><td>{sig_link}</td>"
                       f"<td><a href='/shipment?{q(params,id=r['id'])}'>详情</a></td></tr>")
        return ("<h2>发货与签收（前台物流跟踪）</h2><table>"
                "<tr><th>包裹</th><th>学员</th><th>类型</th><th>状态</th><th>运单号</th>"
                "<th>交运时间</th><th>签收时间</th><th>凭证</th><th></th></tr>"
                + "".join(trs) + "</table>")

    def view_approvals(self, params, user) -> str:
        # 异常审批
        excs = self.conn.execute(
            """SELECT x.*, u.name student_name, su.name submitter_name
               FROM exception_requests x
               JOIN enrollments e ON e.id=x.enrollment_id
               JOIN users u ON u.id=e.student_id
               JOIN users su ON su.id=x.submitted_by
               ORDER BY CASE x.status WHEN 'submitted' THEN 0 ELSE 1 END, x.id""").fetchall()
        exc_rows = []
        for x in excs:
            btns = ""
            if x["status"] == "submitted":
                same = x["submitted_by"] == user["id"] and user["role"] == "warehouse"
                hint = ('<span class="warn">你提交的异常单，需他人审批</span>'
                        if same else "")
                btns = f"""<form class="inline" method="post" action="/approve_exception">
                  <input type="hidden" name="u" value="{esc(user['username'])}">
                  <input type="hidden" name="id" value="{x['id']}">
                  <button class="primary" {'disabled' if same else ''}>审批通过并补发</button></form>
                  <form class="inline" method="post" action="/reject_exception">
                  <input type="hidden" name="u" value="{esc(user['username'])}">
                  <input type="hidden" name="id" value="{x['id']}">
                  <button class="danger">驳回</button></form> {hint}"""
            resend = (f'补发包裹 <a href="/shipment?{q(params,id=x["resend_shipment_id"])}">'
                      f'#{x["resend_shipment_id"]}</a>'
                      if x["resend_shipment_id"] else "")
            exc_rows.append(f"""<tr><td>{x['id']}</td><td>{esc(x['student_name'])}</td>
              <td>{EXC_LABEL[x['kind']]}</td><td>{esc(x['reason'])}</td>
              <td>{esc(x['submitter_name'])}</td>
              <td>{('待审批' if x['status']=='submitted' else
                    {'approved':'已批准','rejected':'已驳回','closed':'登记结案'}[x['status']])}
              <div class="dim">{resend}</div></td><td>{btns}</td></tr>""")
        exc_html = ("<h2>补发 / 异常审批</h2><table>"
                    "<tr><th>单号</th><th>学员</th><th>类型</th><th>理由</th>"
                    "<th>提交人</th><th>状态</th><th>操作</th></tr>"
                    + "".join(exc_rows) + "</table>"
                    "<p class='dim'>规则：补发须核对理由并经审批；仓库人员不能批准自己提交的异常；"
                    "重复补发只登记结案，不再发书。</p>")

        # 改址审批
        ars = self.conn.execute(
            """SELECT a.*, u.name student_name, ad.recipient, ad.phone, ad.address
               FROM address_requests a
               JOIN enrollments e ON e.id=a.enrollment_id
               JOIN users u ON u.id=e.student_id
               JOIN addresses ad ON ad.id=a.address_id
               ORDER BY CASE a.status WHEN 'pending' THEN 0 ELSE 1 END, a.id""").fetchall()
        ar_rows = []
        for a in ars:
            btns = ""
            if a["status"] == "pending":
                btns = f"""<form class="inline" method="post" action="/approve_address">
                  <input type="hidden" name="u" value="{esc(user['username'])}">
                  <input type="hidden" name="id" value="{a['id']}">
                  <button class="primary">通过</button></form>
                  <form class="inline" method="post" action="/reject_address">
                  <input type="hidden" name="u" value="{esc(user['username'])}">
                  <input type="hidden" name="id" value="{a['id']}">
                  <button class="danger">驳回</button></form>"""
            scope = {"pending_packages": "已作用于待发包裹",
                     "future_resends": "无待发包裹：仅对后续补发有效"}.get(
                         a["applies_to"], "")
            ar_rows.append(f"""<tr><td>{a['id']}</td><td>{esc(a['student_name'])}</td>
              <td>{esc(a['address'])}<div class="dim">
              {esc(a['recipient'])} {esc(a['phone'])}</div></td>
              <td>{('待审批' if a['status']=='pending' else
                    {'approved':'已通过','rejected':'已驳回'}[a['status']])}
              <div class='dim'>{scope}</div>{esc('｜'+a['note']) if a['note'] else ''}</td>
              <td>{btns}</td></tr>""")
        addr_html = ("<h2>改地址审批</h2><table>"
                     "<tr><th>申请</th><th>学员</th><th>新地址</th><th>状态</th><th>操作</th></tr>"
                     + "".join(ar_rows) + "</table>"
                     "<p class='dim'>规则：改地址仅在交运前对包裹生效；交运后地址快照冻结，"
                     "新地址只用于之后新建的补发。</p>")

        # 退课
        ens = self.conn.execute(
            """SELECT e.id, u.name student_name, c.name class_name, e.status
               FROM enrollments e JOIN users u ON u.id=e.student_id
               JOIN classes c ON c.id=e.class_id ORDER BY e.id""").fetchall()
        w_rows = []
        for e in ens:
            btn = ""
            if e["status"] == "active":
                btn = f"""<form class="inline" method="post" action="/withdraw">
                  <input type="hidden" name="u" value="{esc(user['username'])}">
                  <input type="hidden" name="id" value="{e['id']}">
                  <input name="reason" placeholder="退课原因" required size="16">
                  <button class="danger">办理退课</button></form>"""
            else:
                btn = '<span class="dim">已退课（未发包裹已取消回库）</span>'
            w_rows.append(f"<tr><td>{e['id']}</td><td>{esc(e['student_name'])}</td>"
                          f"<td>{esc(e['class_name'])}</td><td>{btn}</td></tr>")
        wd_html = ("<h2>退课办理</h2><table><tr><th>报名</th><th>学员</th>"
                   "<th>班级</th><th>操作</th></tr>" + "".join(w_rows) + "</table>")
        return exc_html + addr_html + wd_html

    def view_inventory(self, params, user) -> str:
        rows = self.svc.stock_levels()
        trs = []
        for r in rows:
            trs.append(f"""<tr><td><span class="swatch" style="background:{esc(r['cover_color'])}">
              </span>{esc(r['title'])}</td><td>{esc(r['edition'])}</td>
              <td>{esc(r['isbn'])}</td><td><b>{r['qty']}</b></td>
              <td><form class="inline" method="post" action="/restock">
              <input type="hidden" name="u" value="{esc(user['username'])}">
              <input type="hidden" name="book" value="{r['id']}">
              <input name="qty" size="5" value="5" required>
              <button>入库</button></form></td></tr>""")
        log = self.conn.execute(
            """SELECT l.*, t.title, t.edition FROM inventory_log l
               JOIN textbooks t ON t.id=l.book_id ORDER BY l.id DESC LIMIT 15""").fetchall()
        log_html = "".join(
            f"<li>{esc(l['created_at'])} · {esc(l['title'])} {esc(l['edition'])} "
            f"{l['change_qt']:+d}（{esc(l['reason'])}）</li>" for l in log)
        return ("<h2>教材库存（按版次）</h2><table>"
                "<tr><th>教材</th><th>版次</th><th>ISBN</th><th>库存</th><th>补货</th></tr>"
                + "".join(trs) + f"</table><h2>最近库存流水</h2><ul>{log_html}</ul>")

    def view_student(self, params, user) -> str:
        rows = self.shipment_rows(
            "WHERE e.student_id=? AND s.status != 'cancelled'", (user["id"],))
        cards = []
        for s in rows:
            items = self.items_of(s["id"])
            events = self.events_of(s["id"])
            books = "、".join(
                f'<span class="swatch" style="background:{esc(it["cover_color"])}"></span>'
                f'{esc(it["title"])} {esc(it["edition"])}' for it in items)
            ev = "".join(
                f"<li>{esc(e['occurred_at'])} · "
                f"{EVENT_LABEL.get(e['event'], e['event'])}</li>" for e in events)
            sig = self.conn.execute("SELECT 1 FROM signatures WHERE shipment_id=?",
                                    (s["id"],)).fetchone()
            # 学员可见的版次差异（纯文字说明，不暴露内部字段）
            diffs = self.conn.execute(
                """SELECT es.note, ot.title, ot.edition AS old_edition,
                          nt.edition AS new_edition FROM edition_supplements es
                   JOIN textbooks ot ON ot.id=es.old_book_id
                   JOIN textbooks nt ON nt.id=es.new_book_id
                   WHERE es.enrollment_id=? AND
                     (es.shipment_id=? OR es.shipment_id IS NULL)""",
                (s["enrollment_id"], s["id"])).fetchall()
            diff_html = "".join(
                f"<div class='edition-diff'>版次说明：您已收到《{esc(d['title'])}》"
                f"{esc(d['old_edition'])}，现升级为{esc(d['new_edition'])}。"
                f"{esc(d['note'])}</div>" for d in diffs)
            cards.append(f"""<div class="card">
              <b>包裹 #{s['id']}</b>（{KIND_LABEL[s['kind']]}）
              <span class="tag s-{s['status']}">{STATUS_LABEL[s['status']]}</span>
              {('<span class="dim">签收凭证已回传</span>') if sig else ''}
              <div>{books}</div>
              <div class="dim small">收货地址：{esc(s['address_snapshot'])}</div>
              {diff_html}
              <ul class="small">{ev or '<li class=dim>暂无物流信息</li>'}</ul>
              </div>""")
        return ("<h2>我的教材包裹</h2>" + "".join(cards)
                + '<p class="dim">仅展示您本人的包裹；如有问题可申请'
                  '<a href="/address?u=' + esc(user['username']) + '">改地址</a>或'
                  '<a href="/resend?u=' + esc(user['username']) + '">补发</a>。</p>')

    def form_address(self, params, user) -> str:
        ars = self.conn.execute(
            """SELECT a.*, ad.address FROM address_requests a
               JOIN addresses ad ON ad.id=a.address_id
               WHERE a.enrollment_id IN (SELECT id FROM enrollments WHERE student_id=?)
               ORDER BY a.id DESC""", (user["id"],)).fetchall()
        status_text = {"pending": "审批中", "approved": "已通过", "rejected": "已驳回"}
        scope_text = {"pending_packages": "作用于待发包裹",
                      "future_resends": "仅后续补发有效"}
        history = "".join(
            f"<li>{esc(a['address'])} — {status_text[a['status']]}"
            f"<span class='dim'>（{scope_text.get(a['applies_to'], '')}）</span></li>"
            for a in ars)
        enr = self.conn.execute(
            "SELECT id FROM enrollments WHERE student_id=? AND status='active' LIMIT 1",
            (user["id"],)).fetchone()
        form = ""
        if enr:
            form = f"""<div class="card"><form method="post" action="/request_address">
              <input type="hidden" name="u" value="{esc(user['username'])}">
              <input type="hidden" name="eid" value="{enr['id']}">
              收货人 <input name="recipient" value="{esc(user['name'])}" required><br><br>
              电话 <input name="phone" required size="14"><br><br>
              新地址 <input name="address" size="50" required><br><br>
              说明 <input name="note" size="30">
              <button class="primary">提交改址申请</button></form>
              <p class="dim small">交运前通过的改址直接更新待发包裹；已交运包裹地址不再变更，
              新地址只用于后续补发。</p></div>"""
        return ("<h2>申请修改收货地址</h2>" + form
                + f"<h2>我的申请记录</h2><ul>{history or '<li class=dim>无</li>'}</ul>")

    def form_resend(self, params, user) -> str:
        enr = self.conn.execute(
            "SELECT id FROM enrollments WHERE student_id=? AND status='active' LIMIT 1",
            (user["id"],)).fetchone()
        form = ""
        if enr:
            opts = "".join(f"<option value='{k}'>{v}</option>"
                           for k, v in EXC_LABEL.items())
            form = f"""<div class="card"><form method="post" action="/request_exception">
              <input type="hidden" name="u" value="{esc(user['username'])}">
              <input type="hidden" name="eid" value="{enr['id']}">
              异常类型 <select name="kind">{opts}</select><br><br>
              情况说明（必填，供核对）<br>
              <textarea name="reason" rows="3" cols="60" required></textarea><br><br>
              <button class="primary">提交补发申请</button></form>
              <p class="dim small">补发需前台/仓库核对理由并审批；同类异常处理中不能重复提交。</p></div>"""
        return "<h2>申请补发 / 异常上报</h2>" + form


# ================================================================ HTTP 入口

class Handler(BaseHTTPRequestHandler):
    app: App = None

    def log_message(self, fmt, *args):
        pass

    def _send(self, html: str, status=200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, path: str, flash_kind: str, msg: str):
        loc = path + (("&" if "?" in path else "?") +
                      urllib.parse.urlencode({"flash": f"{flash_kind}:{msg}"}))
        self.send_response(303)
        self.send_header("Location", loc)
        self.end_headers()

    def _params(self) -> dict:
        p = urllib.parse.urlparse(self.path)
        return urllib.parse.parse_qs(p.query), p

    def do_GET(self):
        params, p = self._params()
        flat = {k: v[-1] for k, v in params.items()}
        try:
            if p.path == "/switch":
                self.send_response(303)
                self.send_header("Location",
                                 "/" + ("student" if self.app.user(flat["u"])["role"]
                                        == "student" else "pending") + "?u=" + flat["u"])
                self.end_headers()
                return
            user = self.app.require_user(flat)
            flash = flat.get("flash", "")
            route = p.path
            if route == "/" :
                route = "/student" if user["role"] == "student" else "/pending"
            renders = {
                "/pending": (self.app.view_pending, False),
                "/plan": (self.app.view_plan, True),
                "/shipment": (self.app.view_shipment, None),
                "/tracking": (self.app.view_tracking, True),
                "/approvals": (self.app.view_approvals, True),
                "/inventory": (self.app.view_inventory, True),
                "/student": (self.app.view_student, False),
                "/address": (self.app.form_address, False),
                "/resend": (self.app.form_resend, False),
            }
            if route not in renders:
                if route.startswith("/signature/"):
                    return self._serve_signature(route.split("/", 2)[-1])
                self._send(page("404", user,
                                self.app.conn.execute(
                                    "SELECT * FROM users ORDER BY id").fetchall(),
                                "<h2>404</h2>"), 404)
                return
            fn, staff = renders[route]
            if staff is True and user["role"] == "student":
                raise DomainError("该页面对学员不可见")
            body = fn(flat, user)
            users = self.app.conn.execute("SELECT * FROM users ORDER BY id").fetchall()
            self._send(page(route, user, users, body,
                            flash=(flash[0], flash.split(":", 1)[1]) if flash else ""))
        except DomainError as e:
            users = self.app.conn.execute("SELECT * FROM users ORDER BY id").fetchall()
            u = self.app.user(flat.get("u", "wangc"))
            self._send(page("错误", u, users,
                            f'<div class="flash err">{esc(e)}</div>'
                            f'<a href="/pending?u={esc(flat.get("u","wangc"))}">返回</a>'), 400)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._send(f"<h2>服务器错误</h2><pre>{esc(traceback.format_exc())}</pre>", 500)

    def _serve_signature(self, name: str):
        # 防路径穿越：只允许 data/signatures 下的文件
        safe = Path(name).name
        fp = DATA_DIR / "signatures" / safe
        if not fp.exists():
            self.send_error(404)
            return
        data = fp.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(str(fp))[0] or "image/svg+xml")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
        f = {k: v[-1] for k, v in form.items()}
        path = urllib.parse.urlparse(self.path).path
        u = f.get("u", "wangc")
        back = f.get("back", "/pending")
        try:
            user = self.app.require_user(f)
            svc = self.app.svc
            if path == "/shipment_action":
                sid = int(f["id"]); act = f["act"]
                if act == "pick":
                    svc.pick_and_pack(sid); msg = "拣货完成，库存已扣减"
                elif act == "handoff":
                    tn = svc.hand_to_logistics(sid)
                    msg = f"已交运，运单号 {tn}"
                elif act.startswith("evt_"):
                    ev = act[4:]
                    svc.logistics_callback(sid, ev, "模拟物流回传")
                    msg = f"已回传：{EVENT_LABEL.get(ev, ev)}"
                elif act == "sign":
                    ship = svc.conn.execute(
                        "SELECT e.student_id, u.name FROM shipments s "
                        "JOIN enrollments e ON e.id=s.enrollment_id "
                        "JOIN users u ON u.id=e.student_id WHERE s.id=?",
                        (sid,)).fetchone()
                    svc.confirm_signature(sid, ship["name"], DATA_DIR)
                    msg = "签收完成，签收图已保存到本地"
                back = f"/shipment?id={sid}"
            elif path == "/approve_exception":
                rid = int(f["id"])
                nsid = svc.approve_exception(rid, user["id"])
                msg = (f"已批准，补发包裹 #{nsid} 已生成，等待拣货"
                       if nsid else "已登记结案（重复补发不再发书）")
                back = "/approvals"
            elif path == "/reject_exception":
                svc.reject_exception(int(f["id"]), user["id"])
                msg = "异常单已驳回"; back = "/approvals"
            elif path == "/approve_address":
                scope = svc.approve_address_change(int(f["id"]), user["id"])
                msg = ("改址通过，已更新待发包裹"
                       if scope == "pending_packages"
                       else "改址通过：无待发包裹，仅对后续补发有效")
                back = "/approvals"
            elif path == "/reject_address":
                svc.reject_address_change(int(f["id"]), user["id"])
                msg = "改址申请已驳回"; back = "/approvals"
            elif path == "/issue_supplement":
                nsid = svc.issue_supplement(int(f["sid"]), user["id"])
                msg = f"补充资料补发包裹 #{nsid} 已生成"; back = "/plan"
            elif path == "/withdraw":
                svc.withdraw(int(f["id"]), user["id"], f["reason"])
                msg = "退课完成：未交运包裹已取消，已拣图书回库，后续发放已拦截"
                back = "/approvals"
            elif path == "/restock":
                svc.adjust_stock(int(f["book"]), int(f["qty"]), "补货入库")
                msg = f"入库 {f['qty']} 本"; back = "/inventory"
            elif path == "/request_address" and user["role"] == "student":
                svc.request_address_change(int(f["eid"]), f["recipient"],
                                           f["phone"], f["address"], f.get("note", ""))
                msg = "改址申请已提交，等待审批"; back = "/address"
            elif path == "/request_exception" and user["role"] == "student":
                svc.submit_exception(int(f["eid"]), f["kind"], f["reason"], user["id"])
                msg = "补发申请已提交，等待核对审批"; back = "/resend"
            else:
                raise DomainError("不支持的操作")
            self._redirect(back + (f"?u={u}" if "?" not in back else f"&u={u}"),
                           "o", msg)
        except DomainError as e:
            self._redirect(back + (f"?u={u}" if "?" not in back else f"&u={u}"),
                           "e", str(e))
        except Exception:
            import traceback
            traceback.print_exc()
            self._send("<h2>服务器错误</h2><pre>"
                       + esc(traceback.format_exc()) + "</pre>", 500)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reset", action="store_true", help="重置并重新写入种子数据")
    args = ap.parse_args()

    DATA_DIR.mkdir(exist_ok=True)
    if args.reset or not DB_PATH.exists():
        print("写入种子数据（含错版案例）…")
        reset_and_seed(DB_PATH, DATA_DIR)
    Handler.app = App(DB_PATH)
    srv = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"教材履约系统已启动：http://localhost:{args.port}")
    print("默认身份 王仓（仓库）；右上角可切换 前台/学员 身份。")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
