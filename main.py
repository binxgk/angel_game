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

# ---------------------------------------------------------------------------
# 单实例锁（必须放在最前面，早于一切重活）
# ---------------------------------------------------------------------------
# 为什么放在这里而不是 main() 里：单文件版每次双击都要先把整个包解压到临时
# 目录、加载 CLR、初始化 pywebview —— 这些开销在"检查是否已有实例"之前就
# 已经烧掉了。重复双击 N 次就白白解压/初始化 N 次，正是"启动溢出"的来源。
# 把锁提到模块导入阶段最顶部，重复启动在付出任何代价之前就被拦下。
#
# 代价：此时 app_api 还没导入，日志系统不可用，所以拦截时只能静默退出
# （仅激活已有窗口）。这是有意接受的取舍。
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

    实现要点：用 use_last_error=True 取 kernel32，再配合
    ctypes.get_last_error() 读取错误码。直接调 k32.GetLastError() 是不安全的
    —— 两次 ctypes 调用之间可能夹进别的 Win32 调用，把 last-error 覆盖掉，
    导致 ERROR_ALREADY_EXISTS 判断偶发失效（多开漏网）。
    """
    if sys.platform != "win32":
        return True
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        mutex = k32.CreateMutexW(None, 0, _MUTEX_NAME)
        if not mutex:
            return True  # 创建失败：放行
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            try:
                k32.CloseHandle(ctypes.c_void_p(mutex))
            except Exception:  # noqa: BLE001
                pass
            _activate_existing_window()
            return False
        global _mutex_handle
        _mutex_handle = mutex  # 首个实例：保留句柄直到进程退出
        return True
    except Exception:  # noqa: BLE001
        return True


_IS_FIRST_INSTANCE = _ensure_single_instance()
if not _IS_FIRST_INSTANCE:
    # 已有实例：不做任何初始化，立即退出
    raise SystemExit(0)

# ---------------------------------------------------------------------------
# 可写目录 / 临时目录自愈（必须在 import dotnet_bootstrap 之前）
# ---------------------------------------------------------------------------
# 为什么放在这里：dotnet_bootstrap 有两个分支用 %TMP%/%TEMP% 找可写目录
# （runtimeconfig 与 winforms 补丁），SQLite / icon_extractor / 各种第三方库
# 也都会往临时目录写文件。一旦 %TEMP% 指向不可写位置，这些全部静默失败，
# 表现就是「换到某个盘就起不来」——因为最后的兜底是程序目录，
# 而程序目录写在哪个盘是不受我们控制的。
#
# scratchdir.bootstrap() 做两件事：
#   1. %TMP%/%TEMP% 不可用时，在本进程内把它们改写到确定可写的位置；
#   2. 把整条解析链写进 angel_debug.log，下次出问题一个文件定位。
# 它只依赖标准库、不抛异常、耗时约 1ms，放在这里是安全的。
def _early_log(msg: str) -> None:
    """在 config / app_api 的日志系统就绪之前，直接往 angel_debug.log 写一行。

    这个函数的存在是为了消灭一种最恶心的故障：程序在 import 阶段就死掉，
    日志系统还没初始化，于是【一行日志都不留】，用户只看到「双击没反应」。
    本函数只依赖标准库，自身绝不抛异常。
    """
    try:
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA") or ""
        if not base:
            return
        d = os.path.join(base, "ANGEL GAME")
        os.makedirs(d, exist_ok=True)
        line = time.strftime("[%H:%M:%S") + ".000] [main] " + msg + "\n"
        with open(os.path.join(d, "angel_debug.log"), "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:  # noqa: BLE001
        pass


# 注意：这里【不能】让 import 失败把整个程序带走。
# 历史故障（已在 angel_debug.log 与 PYZ-00.toc 上双向坐实）：某个构建是从
# 旧目录（W:\AI\）打的，PYZ 里没有 scratchdir 模块，于是一启动就
# ModuleNotFoundError —— 死在 dotnet / webview / app_api 全部之前，
# 日志里一行都没有，现象就是「双击无反应」，极易被误判成「这个盘不能跑」。
# 自愈能力很重要，但它不该成为新的崩溃点：拿不到就降级，并留下日志证据。
try:
    import scratchdir as _scratch
except Exception as _exc:  # noqa: BLE001
    _scratch = None
    _early_log(
        f"import scratchdir 失败（已降级继续启动）: "
        f"{type(_exc).__name__}: {_exc} —— 多半是构建过期（缺 scratchdir 模块），"
        f"请用当前 angel_game.spec 重新打包"
    )

_SCRATCH_INFO: dict = {}
if _scratch is not None:
    try:
        _SCRATCH_INFO = _scratch.bootstrap()
    except Exception as _exc:  # noqa: BLE001
        _early_log(
            f"scratchdir.bootstrap() 失败（已降级继续启动）: "
            f"{type(_exc).__name__}: {_exc}"
        )

# ---------------------------------------------------------------------------
# .NET 运行时引导
# ---------------------------------------------------------------------------
# 必须在 import webview 之前：把 .NET 运行时定成 coreclr 并预热 clr，
# 否则 pywebview 在 Windows 上会走坏掉的 netfx 路径（详见 dotnet_bootstrap.py）
import dotnet_bootstrap

_DOTNET_STATUS = dotnet_bootstrap.prepare()

import webview

from web import app_api


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


def _on_move_resize(window, *args) -> None:
    """窗口尺寸/位置变化时记录几何。

    pywebview 各事件给回调的参数个数不一样：
      resized -> (window, width, height)
      moved   -> (window, x, y)
    旧签名只收 window，于是每次拖动都抛
    `_on_move_resize() takes 1 positional argument but 3 were given`，
    几何记忆直接失效、日志被刷屏。这里用 *args 吞掉多余参数。
    """
    global _just_max
    if _just_max:
        return  # 最大化过程中的全屏 resize 不记录为正常尺寸
    if _read_geom(window):
        _save_snapshot(window, immediate=False)


def _on_maximized(window, *args) -> None:
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


def _on_restored(window, *args) -> None:
    global _just_max
    _just_max = False
    _on_move_resize(window)


def _on_closed(window, *args) -> None:
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
    app_api.debug_log(f"dotnet 引导状态: {_DOTNET_STATUS}")
    api = app_api.Api()
    app_api.debug_log(f"Api 初始化完成，数据库={cfg.DB_PATH}")

    # 资源定位：打包后在 _MEIPASS/web，开发时为项目目录 web/
    bundle = cfg._bundle_dir()
    html = os.path.join(bundle, "web", "index.html")
    if not os.path.isfile(html):
        html = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "index.html")
    app_api.debug_log(f"加载界面: {html} (存在={os.path.isfile(html)})")

    # ---- pywebview 版本兼容层 ----
    # create_window() 的参数与事件 API 在不同大版本间有差异：
    #   * 4.x+ ：支持 maximized= 参数、win.events.* 事件容器（实例属性）、win.maximize()
    #   * 3.x  ：无 maximized、无 events 容器（事件直接挂在 window 上）、无 maximize()
    # 之前直接写死 4.x 的用法，一旦打包环境装到 3.x 就会在 create_window 抛
    # TypeError（表现为「双击没反应/弹 Unhandled exception」）。这里探测后择路。
    try:
        import inspect as _inspect
        _cw_params = set(_inspect.signature(webview.create_window).parameters)
    except Exception:  # noqa: BLE001
        _cw_params = set()
    _supports_maximized = "maximized" in _cw_params
    try:
        # pywebview 多数版本没有 webview.__version__，走包元数据更可靠
        from importlib.metadata import version as _pkg_version
        _wv_ver = _pkg_version("pywebview")
    except Exception:  # noqa: BLE001
        _wv_ver = getattr(webview, "__version__", "unknown")
    app_api.debug_log(f"pywebview 能力探测: 版本={_wv_ver} maximized参数={_supports_maximized}")

    _win_kwargs = {
        "js_api": api,
        "width": win_w,
        "height": win_h,
        "x": win_x,
        "y": win_y,
        "min_size": (820, 600),
    }
    if _supports_maximized:
        _win_kwargs["maximized"] = win_max  # 4.x+：创建时即可指定最大化
    win = webview.create_window("ANGEL GAME", html, **_win_kwargs)

    # 事件容器是「实例属性」（self.events = EventContainer()），必须在实例上探测，
    # 不能在 Window 类上探测（类上没有该属性，会误判成 3.x）。
    _has_events_container = hasattr(win, "events")
    _has_maximize = hasattr(win, "maximize")
    app_api.debug_log(
        f"pywebview 实例能力: events容器={_has_events_container} maximize方法={_has_maximize}"
    )

    def _restore_maximized(*args) -> None:
        """窗口显示后还原最大化（仅旧版走这条路；4.x+ 已在创建时指定）。"""
        try:
            win.maximize()
        except Exception:  # noqa: BLE001
            pass

    # ---- 事件订阅：兼容 events 容器(4.x+) 与直接挂载(3.x) ----
    def _on(event_name: str, handler) -> None:
        """把 handler 挂到指定窗口事件上，两代 API 都尝试，失败静默忽略。"""
        if _has_events_container:
            try:
                getattr(win.events, event_name).__iadd__(handler)
                return
            except Exception:  # noqa: BLE001
                pass
        try:
            getattr(win, event_name).__iadd__(handler)
        except Exception:  # noqa: BLE001
            pass  # 该事件在此平台/版本不存在：跳过

    if win_max and _has_maximize and not _supports_maximized:
        _on("shown", _restore_maximized)

    # 订阅几何变化，自动记住上次窗口状态
    _on("resized", _on_move_resize)
    _on("moved", _on_move_resize)
    _on("maximized", _on_maximized)
    _on("restored", _on_restored)
    _on("closed", _on_closed)

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

        _tb_text = _tb.format_exc()
        logged = False
        try:
            app_api.debug_log("FATAL main: " + _tb_text)
            logged = True
        except Exception:  # noqa: BLE001
            pass
        if not logged:
            # 走到这里说明连 app_api 都没导入成功（例如 import dotnet_bootstrap
            # 或 import webview 阶段就炸了）。此时 app_api.debug_log 本身会抛
            # NameError，被上面的 except 吞掉 → 结果又是【一行日志都没有】的
            # 「双击无反应」，正是这次故障里最难查的那一类。
            # 退回只依赖标准库的 _early_log，保证一定留下现场。
            _early_log("FATAL main (日志系统未就绪): " + _tb_text.replace("\n", " | "))
        raise
