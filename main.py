"""main.py — 入口

初始化配置与数据库，启动 pywebview 窗口加载 web/index.html（HTML/CSS/JS 界面），
后端逻辑通过 web/app_api.py 暴露给前端。

运行方式（在 angel_game/ 目录下）：
    python main.py            # 正常模式
    python main.py --demo     # 演示模式（注入模拟游戏，用 notepad.exe 作启动目标）
"""
from __future__ import annotations

import os
import sys
import threading
import time
import ctypes

import config as cfg
import webview

from web import app_api


# ---- 单实例锁：避免重复启动产生多个进程/窗口互相干扰 ----
_MUTEX_NAME = "ANGEL_GAME_SINGLE_INSTANCE_MUTEX"
_mutex_handle = None  # 首个实例持有的内核互斥锁句柄，进程退出时自动释放


def _activate_existing_window() -> None:
    """把已存在的 ANGEL GAME 窗口提到最前（最小化则先还原）。"""
    try:
        import win32gui
        import win32con
        hwnd = win32gui.FindWindow(None, "ANGEL GAME")
        if not hwnd:
            return
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
    except Exception:  # noqa: BLE001
        pass


def _ensure_single_instance() -> bool:
    """返回 True 表示本进程是首个实例（继续启动）；

    False 表示已有实例在运行（已尝试把它提到最前，本进程应直接退出）。
    锁失败时（如非 Windows / API 不可用）放行，不阻断正常启动。
    """
    try:
        k32 = ctypes.windll.kernel32
        # 仅设置返回值类型为指针（64 位 HANDLE 安全）；参数走默认调用约定，
        # 避免在某些环境下设置 argtypes 反而导致 CreateMutexW 抛 ArgumentError。
        k32.CreateMutexW.restype = ctypes.c_void_p
        mutex = k32.CreateMutexW(0, 0, _MUTEX_NAME)
        if not mutex:
            return True  # 创建失败：放行
        if k32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            try:
                k32.CloseHandle(mutex)
            except Exception:  # noqa: BLE001
                pass
            _activate_existing_window()
            return False
        global _mutex_handle
        _mutex_handle = mutex  # 首个实例：保留句柄直到进程退出
        return True
    except Exception:  # noqa: BLE001
        return True


# ---- 窗口几何记忆（记住上次关闭时的尺寸/最大化状态；每次启动强制居中，不记住位置） ----
DEFAULT_W, DEFAULT_H = 960, 640  # 默认窗口尺寸（比旧 1100x740 更小，启动更紧凑）
_geom_lock = threading.Lock()
_geom: dict = {"width": DEFAULT_W, "height": DEFAULT_H}
_last_geom_save = 0.0
_just_max = False  # 最大化瞬间屏蔽由 maximize 触发的 resize 全屏写入


def _save_snapshot(window, immediate: bool) -> None:
    global _last_geom_save
    now = time.time()
    with _geom_lock:
        if not immediate and (now - _last_geom_save) < 0.4:
            return  # 节流：拖动过程中每 0.4s 最多写一次
        # 只保存尺寸与最大化，不保存位置（每次启动强制居中，避免被拉回角落）
        snapshot = {
            "width": _geom["width"],
            "height": _geom["height"],
            "maximized": _geom.get("maximized", False),
        }
        _last_geom_save = now
    cfg.save_window_geometry(snapshot)


def _read_geom(window) -> bool:
    """读取当前几何；成功返回 True 并写入 _geom。

    各字段独立读取：位置(x/y)在窗口销毁瞬间可能取不到，此时保留 _geom 中已有值，
    避免把已记住的窗口位置丢掉（否则关闭即丢失位置）。
    """
    try:
        w = int(window.width)
    except Exception:  # noqa: BLE001
        w = None
    try:
        h = int(window.height)
    except Exception:  # noqa: BLE001
        h = None
    try:
        x = int(window.x)
    except Exception:  # noqa: BLE001
        x = None
    try:
        y = int(window.y)
    except Exception:  # noqa: BLE001
        y = None
    if w is None or h is None or w < 200 or h < 150:
        return False
    with _geom_lock:
        _geom["width"] = w
        _geom["height"] = h
        if x is not None:
            _geom["x"] = x
        if y is not None:
            _geom["y"] = y
        _geom["maximized"] = False
    return True


def _on_move_resize(window) -> None:
    global _just_max
    if _just_max:
        return  # 最大化过程中的全屏 resize 不记录为正常尺寸
    if _read_geom(window):
        _save_snapshot(window, immediate=False)


def _on_maximized(window) -> None:
    global _just_max
    _just_max = True
    # 250ms 后解除屏蔽（覆盖 maximize 事件与后续全屏 resize 的时序不确定性）
    threading.Timer(0.25, _clear_just_max).start()
    with _geom_lock:
        _geom["maximized"] = True
    _save_snapshot(window, immediate=True)


def _clear_just_max() -> None:
    global _just_max
    _just_max = False


def _on_restored(window) -> None:
    global _just_max
    _just_max = False
    _on_move_resize(window)


def _on_closed(window) -> None:
    _read_geom(window)
    with _geom_lock:
        snapshot = {
            "width": _geom["width"],
            "height": _geom["height"],
            "maximized": _geom.get("maximized", False),
        }
    cfg.save_window_geometry(snapshot)
    app_api.debug_log(f"窗口关闭，已保存几何: {snapshot}")


def main() -> None:
    # 单实例锁：若已有 ANGEL GAME 在运行，激活已有窗口并直接退出，避免多开冲突
    if not _ensure_single_instance():
        try:
            app_api.debug_log("检测到已有实例在运行，激活已有窗口并退出（重复启动被拦截）")
        except Exception:  # noqa: BLE001
            pass
        return

    # 高 DPI 感知：避免高分屏/系统缩放下窗口尺寸或内容错位、模糊
    try:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        except Exception:  # noqa: BLE001
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass

    # 演示模式：注入模拟游戏（保持简单，用 notepad 作启动目标）
    if "--demo" in sys.argv:
        import database
        gdb = database.connect()
        if gdb.count_active_games() == 0:
            gdb.seed_demo_games(
                r"C:\Windows\system32\notepad.exe",
                ["巫师3", "星露谷物语", "艾尔登法环", "赛博朋克2077", "黑神话悟空"],
            )
        gdb.close()

    # 读取上次窗口几何：记住尺寸与最大化，但每次启动强制居中（忽略记住的位置）
    global _geom
    saved = cfg.load_window_geometry()
    if saved:
        win_w = int(saved.get("width", DEFAULT_W))
        win_h = int(saved.get("height", DEFAULT_H))
        win_max = bool(saved.get("maximized"))
    else:
        win_w, win_h, win_max = DEFAULT_W, DEFAULT_H, False
    win_x = win_y = None  # 强制居中
    with _geom_lock:
        _geom = {"width": win_w, "height": win_h, "x": None, "y": None, "maximized": win_max}

    app_api.debug_log("启动 main (pywebview 版) ...")
    api = app_api.Api()
    app_api.debug_log(f"Api 初始化完成，数据库={cfg.DB_PATH}")

    # 资源定位：打包后在 _MEIPASS/web，开发时为项目目录 web/
    bundle = cfg._bundle_dir()
    html = os.path.join(bundle, "web", "index.html")
    if not os.path.isfile(html):
        html = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "index.html")
    app_api.debug_log(f"加载界面: {html} (存在={os.path.isfile(html)})")

    win = webview.create_window(
        "ANGEL GAME",
        html,
        js_api=api,
        width=win_w,
        height=win_h,
        x=win_x,
        y=win_y,
        maximized=win_max,
        min_size=(820, 600),
    )
    # 订阅几何变化，自动记住上次窗口状态
    win.events.resized += _on_move_resize
    win.events.moved += _on_move_resize
    win.events.maximized += _on_maximized
    win.events.restored += _on_restored
    win.events.closed += _on_closed

    app_api.debug_log(
        f"webview.create_window 完成 (还原几何 {win_w}x{win_h} @ {win_x},{win_y} "
        f"max={win_max})，进入 webview.start()"
    )
    webview.start()
    app_api.debug_log("webview.start() 返回（窗口已关闭）")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback as _tb
        try:
            app_api.debug_log("FATAL main: " + _tb.format_exc())
        except Exception:  # noqa: BLE001
            pass
        raise
