"""launcher.py — 游戏启动与日志 (P4 基础版)

P4 范围：基础点击启动（subprocess.Popen，设置 cwd，失败兜底检测）并写入日志。
更新提醒弹窗与失败兜底对话框属于 P5，此处保留可扩展的返回结构。
"""
from __future__ import annotations

import os
import subprocess
import time
from typing import Tuple


def execute_launch(exe_path: str, cwd: str = None, poll_seconds: int = 8) -> Tuple[bool, str]:
    """启动 exe 并检测是否存活。
    返回 (是否成功存活, 描述)。进程在 poll_seconds 内退出视为启动失败。
    """
    if not os.path.isfile(exe_path):
        return (False, f"文件不存在: {exe_path}")
    workdir = cwd or os.path.dirname(exe_path)
    try:
        proc = subprocess.Popen(
            [exe_path], cwd=workdir,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:  # noqa: BLE001
        return (False, f"启动异常: {e}")
    # 轮询检测进程状态（每 1 秒，持续 poll_seconds 秒）
    for _ in range(poll_seconds):
        if proc.poll() is not None:
            return (False, "进程已退出，可能启动失败")
        time.sleep(1)
    return (True, f"已启动: {os.path.basename(exe_path)}")


def launch_game(game: dict, db, mode: str = "direct", log: bool = True) -> Tuple[bool, str]:
    """根据 mode 选择目标并启动；写入启动日志与最后启动时间。

    关键修复（v2.6）：log_launch 不再依赖 8 秒存活检测结果。
    很多游戏启动器（Steam/平台启动器）启动完子进程后立即退出，
    若以「8 秒内退出=失败」来判断是否记录，会导致大量正常启动不被日历记录。
    现在的策略：Popen 成功 = 已启动 → 立即记录；存活检测仅影响 UI 反馈。

    log 参数（v2.8）：检查更新时调用方传 log=False，只通过 db.update_last_update_check
    标记已检查，避免「检查更新」被计入游玩统计 / 今日进度 / 热力图。
    """
    target = game.get("direct_exe_path") or game.get("exe_path")
    if mode == "launcher" and game.get("launcher_path"):
        target = game["launcher_path"]
    ok, detail = execute_launch(target)
    # 只要 exe 文件存在且 Popen 成功创建进程，就视为一次有效启动并记录
    # （不再要求进程必须存活 8 秒，适配「启动器→退出→子进程继续」的常见模式）
    launched = not detail.startswith("文件不存在") and not detail.startswith("启动异常")
    if launched and db is not None and game.get("id") is not None and log:
        try:
            db.log_launch(game["id"], mode)
            db.update_last_launched(game["id"])
        except Exception:
            pass  # 记录失败不影响启动本身
    return (ok, detail)
