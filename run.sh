#!/usr/bin/env bash
# 单条本地命令启动教材履约系统（自动建库、写入含错版案例的种子数据）。
set -e
cd "$(dirname "$0")"
PORT="${1:-8000}"
exec python3 app.py --port "$PORT" --reset
