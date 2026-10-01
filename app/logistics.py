# -*- coding: utf-8 -*-
"""模拟物流接口：交运后按运单号回传轨迹与签收。
不依赖任何外部服务；通过 core.logistics_callback 回写本地 SQLite。"""
from __future__ import annotations

import sqlite3

from . import core

# 1x1 PNG，用作模拟签收图（真实环境由快递员回传照片）
PROOF_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082"
)

STAGE_DETAIL = {
    "picked": "快递员已揽收",
    "transit": "运输中，到达转运中心",
    "delivering": "快递员正在派送，请保持电话畅通",
}


def push_transit(conn: sqlite3.Connection, tracking_no: str,
                stage: str = "transit") -> None:
    """回传一条非终态轨迹（不改变包裹状态）。"""
    p = conn.execute("SELECT * FROM parcels WHERE tracking_no=?",
                     (tracking_no,)).fetchone()
    if not p:
        raise core.BusinessError("运单号不存在")
    conn.execute(
        "INSERT INTO tracking_events(parcel_id,event,detail,at) VALUES(?,?,?,?)",
        (p["id"], stage, STAGE_DETAIL.get(stage, stage), core.now_iso()),
    )


def sign(conn: sqlite3.Connection, tracking_no: str,
         with_proof: bool = True) -> dict:
    """模拟正常签收；默认附带本地签收图。"""
    p = conn.execute("SELECT * FROM parcels WHERE tracking_no=?",
                     (tracking_no,)).fetchone()
    proof = None
    if with_proof:
        proof = core.save_proof(PROOF_PNG, p["id"])
    return core.logistics_callback(conn, tracking_no, "signed",
                                   proof_path=proof)


def reject(conn: sqlite3.Connection, tracking_no: str,
           reason: str = "收件人拒收") -> dict:
    """模拟物流拒收：先回传 rejected，再安排退回入库。"""
    core.logistics_callback(conn, tracking_no, "rejected", reason)
    return return_back(conn, tracking_no)


def return_back(conn: sqlite3.Connection, tracking_no: str) -> dict:
    """拒收包裹退回仓库并回库。"""
    return core.logistics_callback(conn, tracking_no, "returning",
                                   "拒收退回，已入库")
