# -*- coding: utf-8 -*-
"""教材履约 Web 服务（仅标准库）。

三视图：
  /warehouse  仓库：待发备货、拣包单、交运、库存、模拟物流
  /frontdesk  前台：班级发放计划、异常补发审批、改地址审批、退课、物流跟踪
  /student    学员：只看自己的包裹、申请改地址/补发；不展示仓库内部备注
"""
from __future__ import annotations

import html
import http.cookies
import os
import secrets
import sqlite3
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app import core, db as dbmod, logistics

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))

SESSIONS: dict[str, dict] = {}

STATUS_LABEL = {
    "packed": ("已打包待交运", "b-gray"),
    "shipped": ("运输中", "b-blue"),
    "delivered": ("已签收", "b-green"),
    "rejected": ("拒收处理中", "b-amber"),
    "returned": ("已退回入库", "b-amber"),
    "cancelled": ("已取消", "b-gray"),
}
TYPE_LABEL = {"wrong_sent": "错寄", "rejected": "物流拒收", "damaged": "包裹破损"}
KIND_LABEL = {"first": "首次发放", "resend": "补发", "supplement": "补充资料"}
BOOK_COLORS = {}  # 启动时从库载入教材封面色


def h(s) -> str:
    return html.escape("" if s is None else str(s), quote=True)


def qs(url: str) -> dict:
    return {k: v[0] for k, v in urllib.parse.parse_qs(
        urllib.parse.urlsplit(url).query, keep_blank_values=True).items()}


def badge(status: str) -> str:
    label, cls = STATUS_LABEL.get(status, (status, "b-gray"))
    return f'<span class="badge {cls}">{h(label)}</span>'


def book_dot(code: str) -> str:
    color = BOOK_COLORS.get(code, "#8c3b2e")
    return f'<span class="book-dot" style="background:{color}"></span>'


# ---------------------------------------------------------------- 页面框架

NAV = {
    "warehouse": [("/warehouse/pending", "待发备货"),
                  ("/warehouse/inventory", "教材库存"),
                  ("/frontdesk/tracking", "物流跟踪")],
    "frontdesk": [("/frontdesk/plan", "班级发放计划"),
                  ("/frontdesk/exceptions", "补发审批"),
                  ("/frontdesk/address", "改地址审批"),
                  ("/frontdesk/tracking", "物流跟踪")],
    "student": [("/student", "我的包裹")],
}


def layout(user: dict | None, active: str, title: str, body: str,
           flash: tuple | None = None) -> str:
    if user:
        role_cn = {"warehouse": "仓库", "frontdesk": "前台", "student": "学员"}[user["role"]]
        links = "".join(
            f'<a href="{u}" class="{"on" if u == active else ""}">{h(n)}</a>'
            for u, n in NAV[user["role"]])
        who = f'{h(user["name"])}（{role_cn}）'
        top = (f'<div class="brand">教材履约台<small>发放 · 拣包 · 物流 · 补发</small></div>'
               f'<div class="nav">{links}</div><div class="spacer"></div>'
               f'<div class="who">当前：<b>{who}</b>　'
               f'<a href="/logout" style="border:none">切换身份</a></div>')
    else:
        top = '<div class="brand">教材履约台<small>请选择身份进入</small></div>'
    flash_html = ""
    if flash:
        kind, msg = flash
        flash_html = f'<div class="flash flash-{kind}">{h(msg)}</div>'
    return f"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{h(title)} · 教材履约台</title>
<link rel="stylesheet" href="/static/style.css"></head>
<body>
<div class="topbar">{top}</div>
<div class="wrap"><h1>{h(title)}</h1>{flash_html}{body}</div>
</body></html>"""


# ---------------------------------------------------------------- 登录

def page_login() -> str:
    staff = []
    student_rows = []
    conn = dbmod.get_conn()
    try:
        staff = conn.execute(
            "SELECT * FROM users WHERE role IN ('warehouse','frontdesk')").fetchall()
        student_rows = conn.execute(
            "SELECT s.*, c.name AS class_name FROM students s "
            "JOIN enrollments e ON e.student_id=s.id JOIN classes c ON c.id=e.class_id "
            "WHERE e.status='enrolled' ORDER BY s.id").fetchall()
    finally:
        conn.close()
    s_btns = "".join(
        f'<form class="inline" method="post" action="/login">'
        f'<input type="hidden" name="uid" value="{r["id"]}">'
        f'<button class="btn-ghost" type="submit">仓库 / 前台：{h(r["name"])}</button></form>'
        for r in staff)
    opts = "".join(
        f'<option value="{r["id"]}">{r["id"]} {h(r["name"])}（{h(r["class_name"])}）</option>'
        for r in student_rows)
    body = f"""
<div class="card"><h2 style="margin-top:0">工作人员入口</h2>
<p class="small muted">演示环境：仓库提交异常，前台审批；仓库不能批准自己提交的异常。</p>
{s_btns}</div>
<div class="card"><h2>学员入口</h2>
<form method="post" action="/login" class="form-row" style="align-items:flex-end">
<div><label>选择学员</label><select name="uid">{opts}</select></div>
<div><button type="submit">进入我的包裹</button></div>
</form>
<p class="student-scope-note">学员端只展示本人包裹，不显示仓库内部备注。</p>
</div>"""
    return layout(None, "", "登录", body)


# ---------------------------------------------------------------- 仓库视图

def page_pending(user, flash):
    conn = dbmod.get_conn()
    try:
        rows = core.pending_list(conn)
        trs = []
        for r in rows:
            books = "".join(
                f'<div>{book_dot(m["textbook_code"])}{h(m["title"])}'
                f'<span class="muted">（{h(m["edition"])}版 ×{m["qty"]}）</span></div>'
                for m in r["materials"])
            miss_cls = "zero" if r["missing"] == 0 else "miss"
            cd_cls = "countdown urgent" if r["countdown"] <= 7 else "countdown"
            if r["countdown"] < 0:
                cd_txt = f'<span class="{cd_cls}">已开课 {-r["countdown"]} 天</span>'
            else:
                cd_txt = f'<span class="{cd_cls}">{r["countdown"]} 天</span>'
            if not r["address_ready"]:
                action = '<span class="badge b-red">地址缺失</span>'
            elif r["missing"]:
                action = (f'<span class="miss">缺 {r["missing"]} 本</span> '
                          '<span class="small muted">补齐库存后可成包</span>')
            else:
                action = (f'<form class="inline" method="post" action="/act/first">'
                          f'<input type="hidden" name="enrollment_id" value="{h(r["enrollment_id"])}">'
                          f'<button type="submit">拣书打包（首次发放）</button></form>')
            trs.append(f"""<tr>
<td>{h(r['student_name'])}<div class="small muted">{h(r['enrollment_id'])}</div></td>
<td>{h(r['class_name'])}</td><td>{cd_txt}</td>
<td>{books}</td>
<td><span class="{miss_cls}">缺书 {r['missing']}</span></td>
<td class="addr">{h(r['receiver_name'])}　{h(r['receiver_phone'])}<br>{h(r['receiver_address'])}</td>
<td>{action}</td></tr>""")
        table = "\n".join(trs) or '<tr><td colspan="7" class="muted">暂无待发报名</td></tr>'

        # 已打包待交运
        packed = conn.execute(
            """SELECT p.*, s.name AS student_name, c.name AS class_name FROM parcels p
               JOIN enrollments e ON e.id=p.enrollment_id
               JOIN students s ON s.id=e.student_id
               JOIN classes c ON c.id=e.class_id
               WHERE p.status='packed' ORDER BY p.id""").fetchall()
        ptrs = []
        for p in packed:
            ptrs.append(
                f'<tr><td>#{p["id"]}</td><td>{h(p["student_name"])}</td>'
                f'<td>{h(p["class_name"])}</td><td>{KIND_LABEL.get(p["kind"],p["kind"])}</td>'
                f'<td class="addr">{h(p["receiver_address"])}</td>'
                f'<td><a href="/warehouse/parcel?id={p["id"]}">拣包单</a>　'
                f'<form class="inline" method="post" action="/act/ship">'
                f'<input type="hidden" name="parcel_id" value="{p["id"]}">'
                f'<button class="btn-stamp" type="submit">交运</button></form></td></tr>')
        ptable = "\n".join(ptrs) or '<tr><td colspan="6" class="muted">无待交运包裹</td></tr>'
    finally:
        conn.close()
    body = f"""
<p class="subtitle">按开课倒计时升序、缺书数量降序排列；首次发放每个报名记录仅可创建一次。</p>
<div class="card"><h2 style="margin-top:0">待发列表（开课倒计时 / 缺书）</h2>
<table><thead><tr><th>学员</th><th>班级</th><th>距开课</th><th>计划教材</th>
<th>缺书</th><th>收货地址</th><th>操作</th></tr></thead><tbody>{table}</tbody></table></div>
<div class="card"><h2>已打包 · 待交运</h2>
<table><thead><tr><th>包裹</th><th>学员</th><th>班级</th><th>类型</th><th>面单地址</th>
<th>操作</th></tr></thead><tbody>{ptable}</tbody></table></div>"""
    return layout(user, "/warehouse/pending", "待发备货", body, flash)


def page_parcel(user, parcel_id: int, flash):
    conn = dbmod.get_conn()
    try:
        data = core.packing_list(conn, parcel_id)
        p, items = data["parcel"], data["items"]
        stu = conn.execute(
            "SELECT s.name FROM students s JOIN enrollments e ON e.student_id=s.id "
            "WHERE e.id=?", (p["enrollment_id"],)).fetchone()
        rows = "".join(
            f'<tr><td>{book_dot(i["textbook_code"])}{h(i["item_title"] or i["textbook_code"])}</td>'
            f'<td>{h(i["edition"])}版</td><td>{i["qty"]}</td>'
            f'<td>{"是" if i["is_supplement"] else "—"}</td></tr>'
            for i in items)
        events = conn.execute(
            "SELECT * FROM tracking_events WHERE parcel_id=? ORDER BY id",
            (parcel_id,)).fetchall()
        tl = "".join(
            f'<li><span class="t-at">{h(ev["at"][5:16].replace("T"," "))}</span>'
            f'{h(ev["detail"] or ev["event"])}</li>' for ev in events)
        proof = None
        for ev in events:
            if ev["detail"] and "签收凭证：" in ev["detail"]:
                proof = ev["detail"].split("签收凭证：", 1)[1]
        proof_html = (f'<div class="proof"><p>签收凭证：</p>'
                      f'<img src="/proof?name={h(urllib.parse.quote(os.path.basename(proof)))}" '
                      f'alt="签收图"></div>') if proof else ""
        if p["status"] == "packed":
            actions = f"""
<form class="inline" method="post" action="/act/ship">
<input type="hidden" name="parcel_id" value="{p["id"]}">
<button class="btn-stamp" type="submit">确认交运（生成运单）</button></form>"""
        else:
            actions = f"""<div class="small">运单号：<span class="trackno">{h(p['tracking_no'])}</span></div>
<form class="inline" method="post" action="/act/sim/transit">
<input type="hidden" name="parcel_id" value="{p["id"]}">
<button class="btn-ghost" type="submit">模拟：派送中</button></form>
<form class="inline" method="post" action="/act/sim/sign">
<input type="hidden" name="parcel_id" value="{p["id"]}">
<button type="submit">模拟：学员签收（回传签收图）</button></form>
<form class="inline" method="post" action="/act/sim/reject">
<input type="hidden" name="parcel_id" value="{p["id"]}">
<button class="btn-stamp" type="submit">模拟：物流拒收退回</button></form>"""
    finally:
        conn.close()
    body = f"""
<p class="subtitle"><a href="/warehouse/pending">← 返回待发备货</a></p>
<div class="sheet">
<div class="sheet-head"><div><h1 class="big">拣包单 / 包裹详情 #{p["id"]}</h1>
<div class="small muted">报名记录 {h(p["enrollment_id"])} · 学员 {h(stu["name"])} ·
{KIND_LABEL.get(p["kind"], p["kind"])}</div></div>
<div>{badge(p["status"])}</div></div>
<div class="grid2">
<div><b>收件人</b><div class="addr">{h(p["receiver_name"])}　{h(p["receiver_phone"])}</div>
<div class="addr">{h(p["receiver_address"])}</div></div>
<div>{actions}</div></div>
<table style="margin-top:14px"><thead><tr><th>教材</th><th>版次（寄出快照）</th>
<th>数量</th><th>补充件</th></tr></thead><tbody>{rows}</tbody></table>
<h2>物流轨迹</h2><ul class="timeline">{tl}</ul>{proof_html}</div>"""
    return layout(user, "/warehouse/pending", f"拣包单 #{p['id']}", body, flash)


def page_inventory(user, flash):
    conn = dbmod.get_conn()
    try:
        inv = conn.execute(
            """SELECT i.*, t.title, t.cover_color FROM inventory i
               JOIN textbooks t ON t.code=i.textbook_code
               ORDER BY i.textbook_code, i.edition""").fetchall()
        trs = "".join(
            f'<tr><td>{book_dot(r["textbook_code"])}{h(r["title"])}</td>'
            f'<td>{h(r["edition"])}版</td><td><b>{r["qty"]}</b></td></tr>'
            for r in inv)
        classes = conn.execute("SELECT * FROM classes ORDER BY id").fetchall()
        plan_blocks = []
        for c in classes:
            mats = conn.execute(
                """SELECT cm.*, t.title FROM class_materials cm
                   JOIN textbooks t ON t.code=cm.textbook_code
                   WHERE cm.class_id=? ORDER BY cm.textbook_code""", (c["id"],)).fetchall()
            lines = []
            for m in mats:
                eds = conn.execute(
                    "SELECT edition,label FROM textbook_editions WHERE textbook_code=? "
                    "ORDER BY released_on", (m["textbook_code"],)).fetchall()
                opts = "".join(
                    f'<option value="{e["edition"]}" {"selected" if e["edition"]==m["edition"] else ""}>'
                    f'{h(e["label"])}</option>' for e in eds)
                lines.append(f"""<tr><td>{book_dot(m['textbook_code'])}{h(m["title"])}</td>
<td>{h(m["edition"])}版</td>
<td><form class="inline form-row" method="post" action="/act/upgrade">
<input type="hidden" name="class_id" value="{c["id"]}">
<input type="hidden" name="textbook_code" value="{m["textbook_code"]}">
<select name="new_edition" style="width:auto">{opts}</select>
<button class="btn-ghost" type="submit">版次升级</button></form></td></tr>""")
            plan_blocks.append(
                f'<div class="card"><h2 style="margin-top:0">{h(c["name"])}'
                f'<span class="small muted">（{c["id"]} · 开课 {c["start_date"]}）</span></h2>'
                f'<table><thead><tr><th>教材</th><th>当前计划版次</th><th>升级</th></tr></thead>'
                f'<tbody>{"".join(lines)}</tbody></table>'
                f'<p class="small muted">升级时：未交运包裹自动换版回库；已寄旧版逐人生成补充资料，'
                f'历史寄出书与版次照原样保留。</p></div>')
        moves = conn.execute(
            "SELECT * FROM stock_movements ORDER BY id DESC LIMIT 12").fetchall()
        mrows = "".join(
            f'<tr><td class="small">{h(r["at"][5:16].replace("T"," "))}</td>'
            f'<td>{h(r["textbook_code"])} {h(r["edition"])}版</td>'
            f'<td style="color:{"var(--ok)" if r["change"]>0 else "var(--bad)"}">'
            f'{r["change"]:+d}</td><td class="small">{h(r["reason"])} {h(r["ref"])}</td>'
            f'<td class="small">{h(r["actor"])}</td></tr>' for r in moves)
    finally:
        conn.close()
    body = f"""
<div class="grid2"><div class="card"><h2 style="margin-top:0">库存余量</h2>
<table><thead><tr><th>教材</th><th>版次</th><th>可拣数量</th></tr></thead>
<tbody>{trs}</tbody></table>
<h2>入库登记</h2>
<form method="post" action="/act/stockin" class="form-row" style="align-items:flex-end">
<div><label>教材</label><input name="textbook_code" placeholder="如 MATH"></div>
<div><label>版次</label><input name="edition" placeholder="如 2" style="width:90px"></div>
<div><label>数量</label><input name="qty" value="1" style="width:90px"></div>
<div><button type="submit">入库</button></div></form></div>
<div class="card"><h2 style="margin-top:0">最近库存流水</h2>
<table><thead><tr><th>时间</th><th>教材</th><th>变动</th><th>原因</th><th>操作人</th></tr>
</thead><tbody>{mrows}</tbody></table></div></div>
<h1 style="font-size:18px">班级教材版次</h1>{"".join(plan_blocks)}"""
    return layout(user, "/warehouse/inventory", "教材库存与版次", body, flash)


# ---------------------------------------------------------------- 前台视图

def page_plan(user, class_id: str | None, flash):
    conn = dbmod.get_conn()
    try:
        classes = conn.execute("SELECT * FROM classes ORDER BY start_date").fetchall()
        if not class_id:
            class_id = classes[0]["id"]
        plan = core.class_plan(conn, class_id)
        is_fd = user and user["role"] == "frontdesk"
        tag_label = {"current": ("已按当前版次发放", "b-green"),
                     "old_with_supp": ("旧版已寄 · 补充资料已安排", "b-blue"),
                     "old_pending_supp": ("旧版已寄 · 待发补充资料", "b-amber"),
                     "partial": ("部分发放", "b-amber"),
                     "unissued": ("未发放", "b-gray")}
        people_html = []
        for person in plan["people"]:
            e = person["enrollment"]
            line_bits = []
            for ls in person["lines"]:
                m = ls["material"]
                lab, cls = tag_label[ls["tag"]]
                issued = f'，寄出版次：{h(ls["issued_edition"])}版' if ls["issued_edition"] else ""
                line_bits.append(
                    f'<div>{book_dot(m["textbook_code"])}{h(m["title"])}'
                    f'<span class="muted">（计划 {h(m["edition"])}版{issued}）</span> '
                    f'<span class="badge {cls}">{lab}</span></div>')
            sup_html = ""
            if person["supplements"]:
                notes = "".join(
                    f'<div class="edition-note"><b>补充资料</b>：{h(s["old_edition"])}版→'
                    f'{h(s["new_edition"])}版。{h(s["note"])}</div>'
                    for s in person["supplements"])
                if user["role"] == "warehouse":
                    btn = (f'<form class="inline" method="post" action="/act/supplement">'
                           f'<input type="hidden" name="enrollment_id" value="{e["id"]}">'
                           f'<button type="submit">拣包发出补充资料</button></form>')
                else:
                    btn = '<span class="small muted">仓库拣包后发出</span>'
                sup_html = notes + btn
            p = person["parcel"]
            parcel_html = (f'<a href="/frontdesk/tracking?id={p["id"]}">首包 #{p["id"]}</a> '
                           f'{badge(p["status"])}') if p else '<span class="muted">未创建</span>'
            withdraw = ""
            if is_fd and e["status"] == "enrolled":
                withdraw = (f'<form class="inline" method="post" action="/act/withdraw" '
                            f'onsubmit="return confirm(\'确认该学员退课？在途/已签收包裹将拦截退课。\')">'
                            f'<input type="hidden" name="enrollment_id" value="{e["id"]}">'
                            f'<button class="btn-ghost" type="submit">办理退课</button></form>')
            people_html.append(f"""<tr>
<td>{h(e['id']).replace('E-','')} {h(e['student_name'])}<div class="small muted">{h(e['id'])}</div></td>
<td>{''.join(line_bits)}{sup_html}</td>
<td>{parcel_html}<div class="small">{withdraw}</div></td></tr>""")
        tabs = "".join(
            f'<a href="/frontdesk/plan?class_id={c["id"]}" '
            f'class="btn btn-{"ghost" if c["id"]!=class_id else ""}" style="margin-right:8px">'
            f'{h(c["name"])}（{c["start_date"]}）</a>' for c in classes)
        # 版次差异文字说明（当前计划各教材的最新 diff_note）
        diffs = []
        for m in plan["materials"]:
            te = conn.execute(
                "SELECT * FROM textbook_editions WHERE textbook_code=? AND edition=?",
                (m["textbook_code"], m["edition"])).fetchone()
            if te and te["diff_note"]:
                diffs.append(f'<div class="edition-note"><b>{book_dot(m["textbook_code"])}'
                             f'{h(m["title"])} {h(te["label"])}版次说明：</b>{h(te["diff_note"])}</div>')
    finally:
        conn.close()
    body = f"""
<p class="subtitle">发放计划 = 班级 × 教材当前版次；已寄旧版不做覆盖，只追加补充资料。</p>
<div style="margin-bottom:14px">{tabs}</div>
<div class="card">{''.join(diffs) or '<p class="small muted">当前计划版次暂无升级说明。</p>'}</div>
<div class="card"><table><thead><tr><th>学员</th><th>版次与补充资料</th><th>首次发放 / 退课</th></tr>
</thead><tbody>{''.join(people_html)}</tbody></table></div>"""
    return layout(user, "/frontdesk/plan", "班级发放计划", body, flash)


def page_exceptions(user, flash):
    conn = dbmod.get_conn()
    try:
        rows = conn.execute(
            """SELECT x.*, s.name AS student_name FROM exceptions x
               JOIN enrollments e ON e.id=x.enrollment_id
               JOIN students s ON s.id=e.student_id ORDER BY
               CASE x.status WHEN 'submitted' THEN 0 ELSE 1 END, x.id""").fetchall()
        trs = []
        for r in rows:
            st = {"submitted": ("待审批", "b-amber"), "approved": ("已批准补发", "b-green"),
                  "rejected": ("已驳回", "b-gray")}[r["status"]]
            sub = conn.execute("SELECT name,role FROM users WHERE id=?",
                               (r["submitted_by"],)).fetchone()
            appr = conn.execute("SELECT name FROM users WHERE id=?",
                                (r["approved_by"],)).fetchone() if r["approved_by"] else None
            act = ""
            if r["status"] == "submitted":
                if user["role"] == "frontdesk":
                    act = (f'<form class="inline" method="post" action="/act/exc/approve">'
                           f'<input type="hidden" name="exception_id" value="{r["id"]}">'
                           f'<button type="submit">核对理由并批准补发</button></form>'
                           f'<form class="inline" method="post" action="/act/exc/reject">'
                           f'<input type="hidden" name="exception_id" value="{r["id"]}">'
                           f'<button class="btn-ghost" type="submit">驳回</button></form>')
                else:
                    act = '<span class="small muted">须由前台审批（提交人不可审批）</span>'
            else:
                act = f'<span class="small">{h(r["resolution"])}</span>'
            trs.append(f"""<tr>
<td>#{r["id"]}</td><td>{h(r["student_name"])}<div class="small muted">{h(r["enrollment_id"])}</div></td>
<td><span class="badge b-red">{TYPE_LABEL[r["type"]]}</span><div class="small">关联包裹 #{r["parcel_id"]}</div></td>
<td>{h(r["reason"])}</td>
<td class="small">{h(sub["name"])}（{"仓库" if sub["role"]=="warehouse" else "学员"}）</td>
<td><span class="badge {st[1]}">{st[0]}</span>{f'<div class="small muted">{h(appr["name"])}</div>' if appr else ''}</td>
<td>{act}</td></tr>""")
    finally:
        conn.close()
    body = f"""
<p class="subtitle">补发须核对理由和审批；仓库账号与提交本人均不能审批；每张异常单最多产生一个补发包裹。</p>
<div class="card"><table><thead><tr><th>单号</th><th>学员</th><th>异常类型</th>
<th>对外理由</th><th>提交人</th><th>状态</th><th>审批</th></tr></thead>
<tbody>{''.join(trs) or '<tr><td colspan="7" class="muted">暂无异常单</td></tr>'}</tbody></table></div>
<p class="small muted">仓库内部备注仅在仓库/前台侧于物流详情可见，学员端不出现。</p>"""
    return layout(user, "/frontdesk/exceptions", "异常与补发审批", body, flash)


def page_address(user, flash):
    conn = dbmod.get_conn()
    try:
        rows = conn.execute(
            """SELECT a.*, s.name AS student_name FROM address_requests a
               JOIN enrollments e ON e.id=a.enrollment_id
               JOIN students s ON s.id=e.student_id ORDER BY
               CASE a.status WHEN 'pending' THEN 0 ELSE 1 END, a.id""").fetchall()
        trs = []
        for r in rows:
            st = {"pending": ("待处理", "b-amber"), "approved": ("已生效", "b-green"),
                  "rejected": ("未生效", "b-gray")}[r["status"]]
            cur = conn.execute("SELECT * FROM enrollments WHERE id=?",
                               (r["enrollment_id"],)).fetchone()
            act = ""
            if r["status"] == "pending" and user["role"] == "frontdesk":
                act = (f'<form class="inline" method="post" action="/act/addr/approve">'
                       f'<input type="hidden" name="request_id" value="{r["id"]}">'
                       f'<button type="submit">批准（未交运才生效）</button></form>'
                       f'<form class="inline" method="post" action="/act/addr/reject">'
                       f'<input type="hidden" name="request_id" value="{r["id"]}">'
                       f'<button class="btn-ghost" type="submit">驳回</button></form>')
            trs.append(f"""<tr>
<td>#{r["id"]}</td><td>{h(r["student_name"])}</td>
<td class="addr">{h(cur["receiver_name"])} {h(cur["receiver_phone"])}<br>{h(cur["receiver_address"])}</td>
<td class="addr">{h(r["new_receiver"] or "")} {h(r["new_phone"] or "")}<br>{h(r["new_address"])}</td>
<td><span class="badge {st[1]}">{st[0]}</span>{f'<div class="small muted">{h(r["note"])}</div>' if r["note"] else ''}</td>
<td>{act}</td></tr>""")
    finally:
        conn.close()
    body = """
<p class="subtitle">改地址仅在交运前生效：批准瞬间若包裹已交运，系统自动驳回并提示走异常补发。</p>
<div class="card"><table><thead><tr><th>申请</th><th>学员</th><th>现地址</th>
<th>申请改为</th><th>状态</th><th>处理</th></tr></thead>
<tbody>%s</tbody></table></div>""" % ("".join(trs) or '<tr><td colspan="6" class="muted">暂无改地址申请</td></tr>')
    return layout(user, "/frontdesk/address", "改地址审批", body, flash)


def page_tracking(user, parcel_id: int | None, flash):
    conn = dbmod.get_conn()
    try:
        if parcel_id:
            p = conn.execute(
                """SELECT p.*, s.name AS student_name, c.name AS class_name FROM parcels p
                   JOIN enrollments e ON e.id=p.enrollment_id
                   JOIN students s ON s.id=e.student_id
                   JOIN classes c ON c.id=e.class_id WHERE p.id=?""",
                (parcel_id,)).fetchone()
            if not p:
                raise core.BusinessError("包裹不存在")
            events = conn.execute(
                "SELECT * FROM tracking_events WHERE parcel_id=? ORDER BY id",
                (parcel_id,)).fetchall()
            tl = "".join(
                f'<li><span class="t-at">{h(ev["at"][5:16].replace("T"," "))}</span>'
                f'{h(ev["detail"] or ev["event"])}</li>' for ev in events)
            items = conn.execute("SELECT * FROM parcel_items WHERE parcel_id=?",
                                 (parcel_id,)).fetchall()
            ihtml = "".join(
                f'<div>{book_dot(i["textbook_code"])}{h(i["item_title"] or i["textbook_code"])}'
                f'<span class="muted">（{h(i["edition"])}版 ×{i["qty"]}）</span></div>'
                for i in items)
            intnotes = conn.execute(
                "SELECT internal_note FROM exceptions WHERE parcel_id=? AND internal_note!=''",
                (parcel_id,)).fetchall()
            note_html = "".join(
                f'<div class="internal-note"><span class="tag-staff">仅内部</span>{h(n["internal_note"])}</div>'
                for n in intnotes)
            body = f"""
<p class="subtitle"><a href="/frontdesk/tracking">← 返回全部包裹</a></p>
<div class="card"><h2 style="margin-top:0">包裹 #{p["id"]} · {h(p["student_name"])} ·
{h(p["class_name"])} {badge(p["status"])} <span class="small muted">
（{KIND_LABEL.get(p["kind"],p["kind"])}）</span></h2>
<div class="trackno">{h(p["tracking_no"] or "未交运")}</div>
<div class="addr">{h(p["receiver_name"])}　{h(p["receiver_phone"])}<br>{h(p["receiver_address"])}</div>
<h2>内件</h2>{ihtml}{note_html}
<h2>物流轨迹（模拟接口回传）</h2><ul class="timeline">{tl}</ul></div>"""
            return layout(user, "/frontdesk/tracking", f"物流跟踪 #{p['id']}", body, flash)

        rows = conn.execute(
            """SELECT p.*, s.name AS student_name, c.name AS class_name FROM parcels p
               JOIN enrollments e ON e.id=p.enrollment_id
               JOIN students s ON s.id=e.student_id
               JOIN classes c ON c.id=e.class_id
               ORDER BY p.id DESC""").fetchall()
        trs = "".join(
            f'<tr><td>#{r["id"]}</td><td>{h(r["student_name"])}</td>'
            f'<td>{h(r["class_name"])}</td><td>{KIND_LABEL.get(r["kind"],r["kind"])}</td>'
            f'<td class="small trackno" style="font-size:13px">{h(r["tracking_no"] or "—")}</td>'
            f'<td>{badge(r["status"])}</td>'
            f'<td><a href="/frontdesk/tracking?id={r["id"]}">轨迹/签收</a></td></tr>'
            for r in rows)
    finally:
        conn.close()
    body = f"""
<div class="card"><table><thead><tr><th>包裹</th><th>学员</th><th>班级</th><th>类型</th>
<th>运单号</th><th>状态</th><th></th></tr></thead><tbody>{trs}</tbody></table></div>"""
    return layout(user, "/frontdesk/tracking", "物流跟踪", body, flash)


# ---------------------------------------------------------------- 学员视图

def page_student(user, flash):
    conn = dbmod.get_conn()
    exc_meta = {"submitted": ("b-amber", "前台处理中"),
                "approved": ("b-green", "已批准，补发中"),
                "rejected": ("b-gray", "未通过")}
    try:
        parcels = core.student_parcels(conn, user["student_id"])
        cards = []
        for p in parcels:
            items = "".join(
                f'<div>{book_dot(i["textbook_code"])}{h(i["item_title"] or i["textbook_code"])}'
                f'<span class="muted">（{h(i["edition"])}版 ×{i["qty"]}）</span></div>'
                for i in p["items"])
            tl = "".join(
                f'<li><span class="t-at">{h(ev["at"][5:16].replace("T"," "))}</span>'
                f'{h(ev["detail"] or ev["event"])}</li>' for ev in p["events"])
            exc_parts = []
            for x in p["exceptions"]:
                cls, label = exc_meta[x["status"]]
                resolution = f"<div>{h(x['resolution'])}</div>" if x["resolution"] else ""
                exc_parts.append(
                    f'<div class="edition-note"><b>{TYPE_LABEL[x["type"]]}申请：</b>'
                    f'{h(x["reason"])}　<span class="badge {cls}">{label}</span>'
                    f'{resolution}</div>')
            exc_html = "".join(exc_parts)
            can_apply = p["status"] in ("delivered", "rejected", "returned")
            apply_form = ""
            if can_apply:
                apply_form = f"""
<details style="margin-top:8px"><summary class="small">申请补发 / 反馈异常</summary>
<form method="post" action="/act/student/exc" class="form-row" style="align-items:flex-end;margin-top:6px">
<input type="hidden" name="parcel_id" value="{p["id"]}">
<div style="max-width:150px"><label>类型</label><select name="exc_type">
<option value="wrong_sent">错寄（版次/书目不对）</option>
<option value="damaged">包裹破损</option>
<option value="rejected">物流拒收未收到</option></select></div>
<div><label>理由（必填，前台据此核对）</label><input name="reason" required></div>
<div><button type="submit">提交申请</button></div></form></details>"""
            cards.append(f"""<div class="card">
<div style="display:flex;justify-content:space-between;align-items:center">
<h2 style="margin:0;border:none">包裹 #{p["id"]} · {KIND_LABEL.get(p["kind"],p["kind"])}</h2>
{badge(p["status"])}</div>
<div class="small muted">{h(p["class_name"])}　运单号
<span class="trackno" style="font-size:13px">{h(p["tracking_no"] or "未交运")}</span></div>
<div class="addr" style="margin:4px 0">{h(p["receiver_name"])}　{h(p["receiver_phone"])}<br>{h(p["receiver_address"])}</div>
<h2>内件教材</h2>{items}{exc_html}
<h2>物流轨迹</h2><ul class="timeline">{tl}</ul>{apply_form}</div>""")

        # 改地址申请：仅当没有已交运包裹时开放
        shipped = conn.execute(
            """SELECT 1 FROM parcels p JOIN enrollments e ON e.id=p.enrollment_id
               JOIN students s ON s.id=e.student_id WHERE s.id=?
               AND p.status IN ('shipped','delivered','rejected','returned') LIMIT 1""",
            (user["student_id"],)).fetchone()
        e = conn.execute(
            """SELECT e.* FROM enrollments e JOIN students s ON s.id=e.student_id
               WHERE s.id=? AND e.status='enrolled'""",
            (user["student_id"],)).fetchone()
        if e:
            if shipped:
                addr_form = ('<p class="small muted">已有包裹交运，改地址不再生效；'
                             '如未收到教材，请在对应包裹上申请补发。</p>')
            else:
                addr_form = f"""
<form method="post" action="/act/student/addr" class="form-row" style="align-items:flex-end">
<div><label>收件人</label><input name="new_receiver" value="{h(e["receiver_name"])}"></div>
<div><label>电话</label><input name="new_phone" value="{h(e["receiver_phone"])}"></div>
<div style="flex:2"><label>新收货地址（交运前可改）</label><input name="new_address"
 value="{h(e["receiver_address"])}" required></div>
<div><button type="submit">申请改地址</button></div></form>"""
            addr_card = f'<div class="card"><h2 style="margin-top:0">收货地址</h2>{addr_form}</div>'
        else:
            addr_card = '<div class="card"><p>该报名已退课。</p></div>'
    finally:
        conn.close()
    body = f"""
<p class="subtitle">仅展示您本人的教材包裹与公开物流信息。版次差异以文字说明为准，不影响已寄出的记录。</p>
{addr_card}{''.join(cards) or '<div class="card muted">暂无包裹</div>'}"""
    return layout(user, "/student", "我的包裹", body, flash)


# ---------------------------------------------------------------- HTTP 处理器

class Handler(BaseHTTPRequestHandler):
    server_version = "Fulfill/1.0"

    def log_message(self, fmt, *args):
        pass

    # --- 会话 ---
    def _user(self) -> dict | None:
        cookie = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
        sid = cookie.get("sid")
        return SESSIONS.get(sid.value) if sid else None

    def _redirect(self, path: str, flash=None):
        if flash:
            path += ("&" if "?" in path else "?") + "flash=" + urllib.parse.quote(
                f"{flash[0]}|{flash[1]}")
        self.send_response(302)
        self.send_header("Location", path)
        self.end_headers()

    def _send(self, html_text: str, code: int = 200, ctype: str = "text/html"):
        body = html_text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_form(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n).decode("utf-8") if n else ""
        return {k: v[0] for k, v in
                urllib.parse.parse_qs(raw, keep_blank_values=True).items()}

    def _flash(self) -> tuple | None:
        f = qs(self.path).get("flash")
        if not f:
            return None
        kind, _, msg = f.partition("|")
        return kind, msg

    # --- GET ---
    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        user = self._user()
        flash = self._flash()
        try:
            if path.startswith("/static/"):
                return self._static(path)
            if path.startswith("/proof"):
                return self._proof(qs(self.path).get("name", ""))
            if path == "/login":
                if user:
                    return self._redirect(self._home(user))
                return self._send(page_login())
            if not user:
                return self._send(page_login())
            if path == "/logout":
                cookie = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
                sid = cookie.get("sid")
                if sid:
                    SESSIONS.pop(sid.value, None)
                self.send_response(302)
                self.send_header("Location", "/login")
                self.send_header("Set-Cookie", "sid=; Max-Age=0; Path=/")
                self.end_headers()
                return
            if path == "/" or path == "":
                return self._redirect(self._home(user))

            if user["role"] == "warehouse":
                if path == "/warehouse/pending":
                    return self._send(page_pending(user, flash))
                if path == "/warehouse/parcel":
                    return self._send(page_parcel(user, int(qs(self.path)["id"]), flash))
                if path == "/warehouse/inventory":
                    return self._send(page_inventory(user, flash))
                if path == "/frontdesk/tracking":
                    pid = qs(self.path).get("id")
                    return self._send(page_tracking(user, int(pid) if pid else None, flash))
                if path == "/frontdesk/plan":
                    return self._send(page_plan(user, qs(self.path).get("class_id"), flash))
            elif user["role"] == "frontdesk":
                if path == "/frontdesk/plan":
                    return self._send(page_plan(user, qs(self.path).get("class_id"), flash))
                if path == "/frontdesk/exceptions":
                    return self._send(page_exceptions(user, flash))
                if path == "/frontdesk/address":
                    return self._send(page_address(user, flash))
                if path == "/frontdesk/tracking":
                    pid = qs(self.path).get("id")
                    return self._send(page_tracking(user, int(pid) if pid else None, flash))
                if path == "/warehouse/inventory":
                    return self._send(page_inventory(user, flash))
            else:
                if path == "/student":
                    return self._send(page_student(user, flash))
            return self._send(layout(user, "", "无权访问",
                                     '<div class="card">该页面不属于当前身份视图。</div>',
                                     flash), 403)
        except core.BusinessError as e:
            back = self._home(user)
            return self._send(layout(user, "", "操作受限",
                                     f'<div class="card flash flash-err">{h(str(e))}</div>'
                                     f'<p><a href="{back}">返回</a></p>', flash), 400)
        except FileNotFoundError:
            return self._send("not found", 404, "text/plain")

    def _home(self, user) -> str:
        if user["role"] == "warehouse":
            return "/warehouse/pending"
        if user["role"] == "frontdesk":
            return "/frontdesk/plan"
        return "/student"

    def _static(self, path: str):
        fname = os.path.basename(path)
        fp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "static", fname)
        ctype = "text/css" if fname.endswith(".css") else "application/octet-stream"
        with open(os.path.normpath(fp), "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _proof(self, name: str):
        if not name or "/" in name or ".." in name:
            raise FileNotFoundError
        fp = os.path.join(dbmod.DATA_DIR, "proofs", os.path.basename(name))
        with open(fp, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # --- POST ---
    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/login":
            return self.handle_login(self._read_form())
        user = self._user()
        if not user:
            return self._redirect("/login")
        form = self._read_form()
        conn = dbmod.get_conn()
        try:
            back, msg = self._action(conn, path, form, user)
            return self._redirect(back, ("ok", msg) if msg else None)
        except core.BusinessError as e:
            # 回到来源页并以红色闪条提示
            ref = form.get("_back") or self.headers.get("Referer", self._home(user))
            return self._redirect(ref, ("err", str(e)))
        finally:
            conn.close()

    def _action(self, conn, path, f, user) -> tuple[str, str]:
        uid = user["id"]
        wh = user["role"] == "warehouse"
        fd = user["role"] == "frontdesk"
        stu = user["role"] == "student"

        if path == "/login":  # 不会走到，保留
            return "/login", ""

        if path == "/act/first":
            if not wh:
                raise core.BusinessError("仅仓库可创建首次发放")
            pid = core.create_first_issuance(conn, f["enrollment_id"], uid)
            return f"/warehouse/parcel?id={pid}", f"首次发放已创建：拣包单 #{pid}（该报名不可再建）"

        if path == "/act/ship":
            if not wh:
                raise core.BusinessError("仅仓库可交运")
            t = core.ship_parcel(conn, int(f["parcel_id"]), uid)
            return f"/warehouse/parcel?id={f['parcel_id']}", f"已交运，运单号 {t}；此后改地址截止"

        if path == "/act/stockin":
            if not wh:
                raise core.BusinessError("仅仓库可登记入库")
            qty = int(f["qty"])
            if qty <= 0:
                raise core.BusinessError("入库数量须大于 0")
            conn.execute(
                "INSERT INTO inventory(textbook_code,edition,qty) VALUES(?,?,?) "
                "ON CONFLICT(textbook_code,edition) DO UPDATE SET qty=qty+?",
                (f["textbook_code"].strip(), f["edition"].strip(), qty, qty))
            conn.execute(
                "INSERT INTO stock_movements(textbook_code,edition,change,reason,"
                "ref,actor,at) VALUES(?,?,?,?,?,?,?)",
                (f["textbook_code"].strip(), f["edition"].strip(), qty,
                 "manual_stockin", "", uid, core.now_iso()))
            return "/warehouse/inventory", f"入库 {qty} 本"

        if path == "/act/upgrade":
            if not (wh or fd):
                raise core.BusinessError("无权限")
            r = core.upgrade_edition(conn, f["class_id"], f["textbook_code"],
                                     f["new_edition"], uid)
            return ("/warehouse/inventory",
                    f"版次升级完成：{len(r['supplemented'])} 人已寄旧版已列补充资料，"
                    f"{len(r['swapped'])} 个待交运包裹已换版；历史记录保留")

        if path == "/act/supplement":
            if not wh:
                raise core.BusinessError("仅仓库可发出补充资料")
            pid = core.issue_supplements(conn, f["enrollment_id"], uid)
            return f"/warehouse/parcel?id={pid}", f"补充资料包裹 #{pid} 已打包"

        if path == "/act/exc/approve":
            if not fd:
                raise core.BusinessError("仅前台可审批补发；仓库不能审批异常")
            pid = core.approve_exception(conn, int(f["exception_id"]), uid)
            return "/frontdesk/exceptions", f"已批准并生成补发包裹 #{pid}，请到待发备货交运"

        if path == "/act/exc/reject":
            if not fd:
                raise core.BusinessError("仅前台可审批")
            core.reject_exception(conn, int(f["exception_id"]), uid)
            return "/frontdesk/exceptions", "异常申请已驳回"

        if path == "/act/student/exc":
            if not stu:
                raise core.BusinessError("仅学员可提交申请")
            pid = int(f["parcel_id"])
            own = conn.execute(
                """SELECT e.id FROM parcels p JOIN enrollments e ON e.id=p.enrollment_id
                   JOIN students s ON s.id=e.student_id WHERE p.id=? AND s.id=?""",
                (pid, user["student_id"])).fetchone()
            if not own:
                raise core.BusinessError("不能对他人包裹提交申请")
            xid = core.submit_exception(conn, own["id"], pid, f["exc_type"],
                                        f["reason"], uid)
            return "/student", f"异常申请 #{xid} 已提交，等待前台核对审批"

        if path == "/act/student/addr":
            if not stu:
                raise core.BusinessError("仅学员可申请改地址")
            en = conn.execute(
                """SELECT e.id FROM enrollments e JOIN students s ON s.id=e.student_id
                   WHERE s.id=? AND e.status='enrolled'""",
                (user["student_id"],)).fetchone()
            core.request_address_change(conn, en["id"], f["new_address"],
                                        f.get("new_receiver"), f.get("new_phone"), uid)
            return "/student", "改地址申请已提交，前台在交运前审批后生效"

        if path == "/act/addr/approve":
            if not fd:
                raise core.BusinessError("仅前台可处理地址申请")
            core.approve_address_change(conn, int(f["request_id"]), uid)
            return "/frontdesk/address", "新地址已生效，待交运包裹面单已同步"

        if path == "/act/addr/reject":
            if not fd:
                raise core.BusinessError("仅前台可处理地址申请")
            core.reject_address_change(conn, int(f["request_id"]), uid)
            return "/frontdesk/address", "地址申请已驳回"

        if path == "/act/withdraw":
            if not fd:
                raise core.BusinessError("仅前台可办理退课")
            r = core.withdraw_enrollment(conn, f["enrollment_id"], uid)
            return "/frontdesk/plan", (
                f"退课完成，取消未交运包裹 {len(r['cancelled_parcels'])} 个并已回库"
                if r["cancelled_parcels"] else "退课完成")

        # —— 模拟物流回传 ——
        pid = int(f["parcel_id"])
        if path == "/act/sim/sign":
            if not wh:
                raise core.BusinessError("仅仓库可触发模拟回传")
            p = conn.execute("SELECT tracking_no FROM parcels WHERE id=?", (pid,)).fetchone()
            logistics.sign(conn, p["tracking_no"])
            return f"/warehouse/parcel?id={pid}", "模拟接口回传：学员已签收，签收图已存本地"
        if path == "/act/sim/reject":
            if not wh:
                raise core.BusinessError("仅仓库可触发模拟回传")
            p = conn.execute("SELECT tracking_no FROM parcels WHERE id=?", (pid,)).fetchone()
            logistics.reject(conn, p["tracking_no"])
            return f"/warehouse/parcel?id={pid}", "模拟接口回传：拒收并退回入库，可登记异常申请补发"
        if path == "/act/sim/transit":
            if not wh:
                raise core.BusinessError("仅仓库可触发模拟回传")
            p = conn.execute("SELECT tracking_no FROM parcels WHERE id=?", (pid,)).fetchone()
            logistics.push_transit(conn, p["tracking_no"], "delivering")
            return f"/warehouse/parcel?id={pid}", "已回传一条派送中轨迹"

        raise core.BusinessError("未知操作")

    # 登录走 POST /login（无会话）
    def handle_login(self, f):
        conn = dbmod.get_conn()
        try:
            u = conn.execute("SELECT * FROM users WHERE id=?", (f.get("uid"),)).fetchone()
            if not u:
                return self._redirect("/login", ("err", "用户不存在"))
            sid = secrets.token_hex(16)
            SESSIONS[sid] = {"id": u["id"], "name": u["name"],
                             "role": u["role"], "student_id": u["student_id"]}
            self.send_response(302)
            home = ("/warehouse/pending" if u["role"] == "warehouse"
                    else "/frontdesk/plan" if u["role"] == "frontdesk" else "/student")
            self.send_header("Location", home)
            self.send_header("Set-Cookie", f"sid={sid}; Path=/; HttpOnly")
            self.end_headers()
        finally:
            conn.close()


def run():
    # 确保库已初始化（首次启动若无库则自动建空库；正常流程先跑 seed.py）
    conn = dbmod.get_conn()
    dbmod.init_db(conn)
    global BOOK_COLORS
    BOOK_COLORS = {r["code"]: r["cover_color"]
                   for r in conn.execute("SELECT code,cover_color FROM textbooks")}
    conn.close()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"教材履约台已启动：http://{HOST}:{PORT}")
    httpd.serve_forever()


if __name__ == "__main__":
    run()
