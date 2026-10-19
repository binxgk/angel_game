"""launcher.py — 游戏启动与日志 (P4 基础版)

P4 范围：基础点击启动（subprocess.Popen，设置 cwd，失败兜底检测）并写入日志。
更新提醒弹窗与失败兜底对话框属于 P5，此处保留可扩展的返回结构。

v2.9：路径与启动参数分离。
  每个游戏可在库里存一串原始命令行参数（如 "-krqlv=hd -dx11"），启动时拆成
  argv 列表交给 Popen，而不是拼成一个字符串——避免路径/参数里的空格、引号
  被二次解释，也让包含空格的参数（-name="A B"）能正确作为一个 token 传下去。
"""
from __future__ import annotations

import os
import shlex
import subprocess
import time
from typing import List, Optional, Tuple


def _split_windows(text: str) -> Optional[List[str]]:
    """用 Windows 自己的解析器拆参数（CommandLineToArgvW）。

    为什么不自己写切分：Windows 的引号规则很反直觉（`-name="A B"` 这种
    「引号嵌在 token 中间」的写法，朴素切分会拆成两半）。交给系统解析器，
    再加一层 Popen 的 list2cmdline 反向拼装，子进程拿到的就与用户手敲一致。
    """
    try:
        import ctypes
        from ctypes import wintypes

        shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
        shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
        argc = ctypes.c_int(0)
        # 前面垫一个 dummy：CommandLineToArgvW 对 argv[0] 的引号处理有特例，垫掉更稳
        argv = shell32.CommandLineToArgvW("dummy " + text, ctypes.byref(argc))
        if not argv:
            return None
        try:
            return [argv[i] for i in range(1, argc.value)]
        finally:
            kernel32.LocalFree(ctypes.cast(argv, ctypes.c_void_p))
    except Exception:  # noqa: BLE001
        return None


def _split_fallback(text: str) -> List[str]:
    """非 Windows / ctypes 不可用时的兜底：shlex 非 posix 模式 + 手工剥引号。

    用 posix=False 是为了不把 Windows 路径里的反斜杠当转义符
    （posix=True 时 "-logdir=C:\\Games\\X" 会退化成 "C:GamesX"）。
    """
    try:
        lexer = shlex.shlex(text, posix=False)
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return text.split()
    args: List[str] = []
    for tok in tokens:
        if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'":
            tok = tok[1:-1]
        if tok:
            args.append(tok)
    return args


def split_arguments(raw: Optional[str]) -> List[str]:
    """把用户填写的参数串拆成 argv 列表；空串返回空列表，绝不抛异常。"""
    text = (raw or "").strip()
    if not text:
        return []
    if os.name == "nt":
        parsed = _split_windows(text)
        if parsed is not None:
            return parsed
    return _split_fallback(text)


def execute_launch(
    exe_path: str,
    cwd: str = None,
    poll_seconds: int = 8,
    args: Optional[List[str]] = None,
) -> Tuple[bool, str]:
    """启动 exe 并检测是否存活。
    返回 (是否成功存活, 描述)。进程在 poll_seconds 内退出视为启动失败。

    args：已拆好的 argv 列表（不含 exe 本身），会原样追加到命令行尾部。
    """
    if not os.path.isfile(exe_path):
        return (False, f"文件不存在: {exe_path}")
    workdir = cwd or os.path.dirname(exe_path)
    argv = [exe_path] + list(args or [])
    try:
        proc = subprocess.Popen(
            argv, cwd=workdir,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:  # noqa: BLE001
        return (False, f"启动异常: {e}")
    # 轮询检测进程状态（每 1 秒，持续 poll_seconds 秒）
    for _ in range(poll_seconds):
        if proc.poll() is not None:
            return (False, "进程已退出，可能启动失败")
        time.sleep(1)
    detail = f"已启动: {os.path.basename(exe_path)}"
    if args:
        detail += f" ({' '.join(args)})"
    return (True, detail)


def launch_game(
    game: dict,
    db,
    mode: str = "direct",
    log: bool = True,
    poll_seconds: int = 8,
) -> Tuple[bool, str]:
    """根据 mode 选择目标并启动；写入启动日志与最后启动时间。

    关键修复（v2.6）：log_launch 不再依赖 8 秒存活检测结果。
    很多游戏启动器（Steam/平台启动器）启动完子进程后立即退出，
    若以「8 秒内退出=失败」来判断是否记录，会导致大量正常启动不被日历记录。
    现在的策略：Popen 成功 = 已启动 → 立即记录；存活检测仅影响 UI 反馈。

    log 参数（v2.8）：检查更新时调用方传 log=False，只通过 db.update_last_update_check
    标记已检查，避免「检查更新」被计入游玩统计 / 今日进度 / 热力图。

    启动参数（v2.9）：只作用于「游戏本体」这条路径。
    mode="launcher"（走启动器检查更新）时不透传参数——参数是给游戏客户端用的，
    硬塞给 launcher.exe 轻则被忽略，重则让启动器报错退出。
    """
    target = game.get("direct_exe_path") or game.get("exe_path")
    args: List[str] = []
    if mode == "launcher" and game.get("launcher_path"):
        target = game["launcher_path"]
    else:
        args = split_arguments(game.get("launch_args"))
    ok, detail = execute_launch(target, args=args, poll_seconds=poll_seconds)
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
