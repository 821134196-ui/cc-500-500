"""模拟物流接口：运单号、物流状态回传、签收图（本地 SVG 文件）。

真实环境此处应调用快递公司 API；本实现全部本地完成，便于离线演示与测试。
"""
from __future__ import annotations

import itertools
from pathlib import Path

from html import escape

WAYBILL_PREFIX = "SF"


class LogisticsError(Exception):
    pass


class LogisticsService:
    def __init__(self, counter_start: int = 10001):
        # 进程内单调递增，itertools.count 本身线程安全
        self._counter = itertools.count(counter_start)

    def create_waybill(self, shipment_id: int, address_text: str) -> str:
        n = next(self._counter)
        # 运单号与包裹关联仅由调用方落库，这里不触网
        return f"{WAYBILL_PREFIX}{n:08d}{shipment_id:03d}"

    # ---------------------------------------------------------- 签收图（本地）

    def make_signature_image(self, shipment_id: int, signer: str,
                             tracking_no: str | None, data_dir: str | Path) -> str:
        """生成一张模拟签收图（SVG，本质是文本文件，无需图像依赖）。"""
        sig_dir = Path(data_dir) / "signatures"
        sig_dir.mkdir(parents=True, exist_ok=True)
        file_name = f"sig_ship{shipment_id}.svg"
        path = sig_dir / file_name
        svg = f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="420" height="220" viewBox="0 0 420 220">
  <rect width="420" height="220" fill="#fbfaf6"/>
  <rect x="10" y="10" width="400" height="200" fill="none" stroke="#5b5750" stroke-width="1.5"/>
  <text x="30" y="46" font-family="serif" font-size="18" fill="#2b2a27">电子签收凭证</text>
  <text x="30" y="86" font-family="sans-serif" font-size="14" fill="#3d3b36">运单号：{escape(tracking_no or '')}</text>
  <text x="30" y="114" font-family="sans-serif" font-size="14" fill="#3d3b36">签收人：{escape(signer)}</text>
  <path d="M30 170 C 80 120, 120 190, 170 150 S 270 120, 330 165"
        fill="none" stroke="#2b2a27" stroke-width="2.4" stroke-linecap="round"/>
  <text x="30" y="196" font-family="sans-serif" font-size="11" fill="#6f6a61">模拟物流回传 · 仅用于教材履约演练</text>
</svg>
"""
        path.write_text(svg, encoding="utf-8")
        return file_name
