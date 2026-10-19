# -*- coding: utf-8 -*-
"""tests/diag_environment.py — 「换个盘就起不来 / 临时文件建不出来」现场取证

用法（在项目根目录，用打包用的同一个 Python 跑）：
    python tests/diag_environment.py
    python tests/diag_environment.py "W:\\path\\to\\ANGEL GAME v3.exe"

这个脚本只读探测 + 一次性写探针文件（自己删），不改任何现有文件、
不启动 GUI、不碰数据库。目的是把「到底哪一步建不了临时文件」钉死。

输出分四段：
  A. 本进程视角的临时目录候选（GetTempPathW / TMP / TEMP / tempfile）逐个试写
  B. 程序所在盘的物理属性（盘类型 / 文件系统 / 剩余空间 / subst·net use 映射）
  C. scratchdir 统一解析出的候选目录逐一试写 + TEMP/TMP 自愈结果
     + winforms 补丁目录 + .NET WindowsDesktop 运行时 / WebView2 程序集候选
  D. 结论摘要（scratch 落点、是否自愈、哪些候选不可写）

退出码：0 = 全部候选可写；1 = 有候选不可写（把输出发我即可定位）。
"""
from __future__ import annotations

import ctypes
import os
import sys
import tempfile
from ctypes import wintypes

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

APP_NAME = "ANGEL GAME"

LINE = "=" * 74
_probe_results: list[tuple[str, bool, str]] = []


def _hr(title: str) -> None:
    print("\n" + LINE)
    print(title)
    print(LINE)


def _probe_writable(d: str) -> tuple[bool, str]:
    """在 d 下创建→写入→删除一个探针文件。返回 (可写?, 失败原因)。"""
    if not d:
        return False, "路径为空"
    try:
        os.makedirs(d, exist_ok=True)
    except Exception as e:  # noqa: BLE001
        return False, f"makedirs 失败 {type(e).__name__}: {e}"
    probe = os.path.join(d, "__angel_probe__.tmp")
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write("x")
        os.remove(probe)
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, f"write 失败 {type(e).__name__}: {e}"


def _fmt_gb(n: int) -> str:
    return f"{n / 1024 ** 3:.2f} GB"


# --------------------------------------------------------------------------- #
# A. 临时目录候选
# --------------------------------------------------------------------------- #
def section_temp() -> None:
    _hr("A. 本进程视角的临时目录候选（哪个不可写，哪条链路就会断）")

    # 1) Windows 自己的 GetTempPathW：PyInstaller 引导程序走的就是这个
    buf = ctypes.create_unicode_buffer(ctypes.wintypes.MAX_PATH + 1)
    try:
        n = ctypes.windll.kernel32.GetTempPathW(MAX_PATH := wintypes.MAX_PATH + 1, buf)
        gettemppath = buf.value if n else f"<失败 err={ctypes.get_last_error()}>"
    except Exception as e:  # noqa: BLE001
        gettemppath = f"<异常 {e}>"
    print(f"  GetTempPathW()      : {gettemppath}")

    # 2) 环境变量（按 GetTempPathW 的真实优先级 TMP -> TEMP -> USERPROFILE）
    for name in ("TMP", "TEMP", "TMPDIR", "USERPROFILE"):
        v = os.environ.get(name)
        print(f"  %{name}%".ljust(22) + f": {v or '<未设置>'}")

    # 3) Python 的 tempfile.gettempdir()
    try:
        ptd = tempfile.gettempdir()
    except Exception as e:  # noqa: BLE001
        ptd = f"<异常 {e}>"
    print(f"  tempfile.gettempdir(): {ptd}")

    print("\n  -- 逐个试写（这是关键）--")
    cands: list[tuple[str, str]] = [
        ("GetTempPathW", gettemppath),
        ("%TMP%", os.environ.get("TMP") or ""),
        ("%TEMP%", os.environ.get("TEMP") or ""),
        ("tempfile", ptd if isinstance(ptd, str) else ""),
        ("USERPROFILE", os.environ.get("USERPROFILE") or ""),
    ]
    seen = set()
    for label, d in cands:
        if not d or d in seen or d.startswith("<"):
            continue
        seen.add(d)
        ok, why = _probe_writable(d)
        _probe_results.append((f"A/{label}", ok, why))
        print(f"    [{'OK ' if ok else 'BAD'}] {label:<12} {d}  {why}")

    # 4) PyInstaller 单文件版的解压残留：数量多 = 反复解压失败
    mei = []
    for base in {gettemppath, os.environ.get("TMP") or "", os.environ.get("TEMP") or ""}:
        if not base or not os.path.isdir(base):
            continue
        try:
            mei += [os.path.join(base, x) for x in os.listdir(base) if x.startswith("_MEI")]
        except Exception:  # noqa: BLE001
            pass
    if mei:
        total = 0
        for d in mei:
            try:
                total += sum(
                    os.path.getsize(os.path.join(r, f))
                    for r, _, fs in os.walk(d) for f in fs
                    if os.path.isfile(os.path.join(r, f))
                )
            except Exception:  # noqa: BLE001
                pass
        print(f"\n  残留 _MEI* 解压目录: {len(mei)} 个，合计 {_fmt_gb(total)}")
        print("    （数量很多通常说明单文件版每次启动都重新解压，且上次没清干净）")
    else:
        print("\n  残留 _MEI* 解压目录: 0 个")


# --------------------------------------------------------------------------- #
# B. 程序所在盘
# --------------------------------------------------------------------------- #
def section_drive(app_dir: str) -> None:
    _hr("B. 程序所在盘的物理属性（为什么 W: 和别的盘不一样）")

    if not app_dir or not os.path.isdir(app_dir):
        print(f"  目录不存在: {app_dir}")
        return

    drive = os.path.splitdrive(os.path.abspath(app_dir))[0] or "?"
    root = drive + os.sep
    print(f"  程序目录 : {app_dir}")
    print(f"  所在盘   : {drive}")

    # GetDriveTypeW: 2=可移动 3=固定 4=网络 5=光驱 6=RAM磁盘
    try:
        dt = ctypes.windll.kernel32.GetDriveTypeW(root)
        kinds = {0: "未知", 1: "不存在", 2: "可移动磁盘(U盘/SD)",
                 3: "固定磁盘", 4: "网络驱动器(映射盘)", 5: "光驱", 6: "RAM 磁盘"}
        print(f"  盘类型   : {dt} = {kinds.get(dt, '?')}")
    except Exception as e:  # noqa: BLE001
        print(f"  盘类型   : <异常 {e}>")

    # GetVolumeInformationW: 文件系统名 + 卷标
    try:
        fsname = ctypes.create_unicode_buffer(ctypes.wintypes.MAX_PATH + 1)
        volname = ctypes.create_unicode_buffer(ctypes.wintypes.MAX_PATH + 1)
        fsflags = wintypes.DWORD(0)
        maxcomp = wintypes.DWORD(0)
        okv = ctypes.windll.kernel32.GetVolumeInformationW(
            root, volname, ctypes.wintypes.MAX_PATH + 1,
            None, ctypes.byref(maxcomp), ctypes.byref(fsflags),
            fsname, ctypes.wintypes.MAX_PATH + 1,
        )
        if okv:
            print(f"  卷标     : {volname.value or '<空>'}")
            print(f"  文件系统 : {fsname.value}")
            if fsname.value.upper() in ("FAT32", "EXFAT", "FAT", "FAT16"):
                print("    !! 非 NTFS：SQLite WAL / 文件锁 / 长路径都可能受限，"
                      "很多程序在这类盘上就是起不来")
        else:
            print(f"  文件系统 : <失败 err={ctypes.get_last_error()}>")
    except Exception as e:  # noqa: BLE001
        print(f"  文件系统 : <异常 {e}>")

    # 剩余空间
    try:
        free_bytes = ctypes.c_ulonglong(0)
        total_bytes = ctypes.c_ulonglong(0)
        ctypes.windll.kernel32.GetDiskFreeSpaceExW(
            root, ctypes.byref(free_bytes), ctypes.byref(total_bytes), None)
        print(f"  剩余空间 : {_fmt_gb(free_bytes.value)} / 共 {_fmt_gb(total_bytes.value)}")
        if free_bytes.value < 1024 ** 3:
            print("    !! 剩余空间不足 1GB，单文件版解压/临时文件很可能写不下")
    except Exception as e:  # noqa: BLE001
        print(f"  剩余空间 : <异常 {e}>")

    # 是否是 subst 虚拟盘 / 映射盘
    try:
        out = os.popen("subst").read().strip()
        if out:
            print(f"  subst    :\n    " + out.replace("\n", "\n    "))
    except Exception:  # noqa: BLE001
        pass
    try:
        out = os.popen("net use").read().strip()
        if out and len(out.splitlines()) > 4:
            print("  net use  : " + out.splitlines()[2].strip())
    except Exception:  # noqa: BLE001
        pass

    # 目录本身能不能写（程序目录要写 winforms_patched.py / runtimeconfig）
    ok, why = _probe_writable(app_dir)
    _probe_results.append(("B/程序目录", ok, why))
    print(f"  程序目录可写: {'是' if ok else '否'} {why}")


# --------------------------------------------------------------------------- #
# C. dotnet_bootstrap 的四个候选目录
# --------------------------------------------------------------------------- #
def section_bootstrap(app_dir: str) -> None:
    _hr("C. 临时文件链路探测（scratchdir / dotnet_bootstrap 实际会用的目录）")

    try:
        import scratchdir
    except Exception as e:  # noqa: BLE001
        print(f"  import scratchdir 失败: {type(e).__name__}: {e}")
        return

    print("  scratchdir.candidates() 给出的候选顺序（越靠前越可靠）：")
    for i, d in enumerate(scratchdir.candidates(), 1):
        print(f"    {i}. {d}")
    print("\n  实际探测（第一个 OK 的就是程序真正会用的）：")
    chosen = None
    for i, d in enumerate(scratchdir.candidates(), 1):
        ok, why = _probe_writable(d)
        _probe_results.append((f"C/scratch#{i}", ok, why))
        print(f"    [{'OK ' if ok else 'BAD'}] {i}. {d}  {why}")
        if ok and chosen is None:
            chosen = d
    print(f"\n  => scratchdir.resolve() = {chosen or '<全部不可写!>'}")

    print("\n  winforms 补丁目录（scratchdir.resolve('patch')）：")
    pd = scratchdir.resolve("patch")
    ok, why = _probe_writable(pd or "")
    _probe_results.append(("C/patch", ok, why))
    print(f"    [{'OK ' if ok else 'BAD'}] {pd}  {why}")
    if ok:
        p = os.path.join(pd, "winforms_patched.py")
        try:
            with open(p, "w", encoding="utf-8") as f:
                f.write("# probe\n")
            os.remove(p)
            print("         └─ winforms_patched.py 可创建 ✓")
        except Exception as e:  # noqa: BLE001
            print(f"         └─ winforms_patched.py 创建失败: {e}")

    print("\n  TEMP/TMP 自愈结果（scratchdir.repair_temp_env()）：")
    before = {
        "TMP": os.environ.get("TMP", ""),
        "TEMP": os.environ.get("TEMP", ""),
    }
    actions = scratchdir.repair_temp_env()
    print(f"    修复前: TMP={before['TMP']!r} TEMP={before['TEMP']!r}")
    if actions:
        for a in actions:
            print(f"    修复动作: {a}")
        print(f"    修复后: TMP={os.environ.get('TMP')!r} TEMP={os.environ.get('TEMP')!r}")
    else:
        print("    （无需修复，TMP/TEMP 均可用）")

    print("\n  现存的补丁与 runtimeconfig 落点：")
    hits = []
    for base in {os.environ.get("TMP"), os.environ.get("TEMP"),
                 os.environ.get("APPDATA"), os.environ.get("LOCALAPPDATA"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), APP_NAME),
                 app_dir}:
        if not base or not os.path.isdir(base):
            continue
        for rel in ("angel_game_patch/winforms_patched.py",
                    "ANGEL GAME/pywebview.runtimeconfig.json",
                    "ANGEL GAME/patch/winforms_patched.py",
                    "pywebview.runtimeconfig.json"):
            p = os.path.join(base, rel)
            if os.path.exists(p):
                hits.append(p)
    for p in sorted(set(hits)) or ["（没找到 —— 说明一次都没写成功过）"]:
        print(f"    {p}")

    # .NET Desktop 运行时找不找得到
    print("\n  .NET WindowsDesktop 运行时探测：")
    try:
        import dotnet_bootstrap as db

        found = db._find_windowsdesktop_runtime()
        if found:
            print(f"    {found}")
        else:
            # 别写成「会退回 netfx」：prepare() 的设计是【不回退 netfx】，
            # 而是保留 coreclr 把真实错误暴露出来（否则只会把
            # 「winforms 补丁没装上」这个真因盖住）。详见 dotnet_bootstrap.prepare。
            print("    <未找到> —— coreclr 走不通。注意 prepare() 不会回退 netfx，"
                  "会直接把真实错误抛出来，请往上找第一条 AttributeError/WebViewException。")
        cands = db._webview2_candidates()
        print(f"    机器上 .NET Core 版 WebView2 程序集候选: {len(cands)} 个")
        for c in cands[:3]:
            print(f"      {c}")
    except Exception as e:  # noqa: BLE001
        print(f"    <异常 {e}>")


# --------------------------------------------------------------------------- #
# D. 摘要
# --------------------------------------------------------------------------- #
def section_summary() -> None:
    _hr("D. 结论摘要")
    bad = [(k, why) for k, ok, why in _probe_results if not ok]
    try:
        import scratchdir

        scratch = scratchdir.resolve()
        actions = scratchdir.repair_temp_env()
    except Exception:  # noqa: BLE001
        scratch, actions = None, []

    print(f"  程序目录      : {app_dir_value()}")
    print(f"  scratch 落点  : {scratch or '<全部不可写!>'}")
    if actions:
        print("  TEMP/TMP 已自愈:")
        for a in actions:
            print(f"    - {a}")
    else:
        print("  TEMP/TMP      : 正常，无需自愈")

    if not bad:
        print("\n  所有候选目录都可写 —— 本机临时文件链路正常。")
        print("  若程序仍起不来，请看 C 段 .NET 运行时探测与 WebView2 程序集候选数。")
        return
    print(f"\n  有 {len(bad)} 个候选目录不可写，落在它们上面的临时文件会建不出来：")
    for k, why in bad:
        print(f"    [BAD] {k}: {why}")
    print("\n  提示：把这整段输出发给我即可定位；不要手改环境变量后再测，"
          "要保留原始失败现场。")


_APP_DIR_HOLDER = {"v": ""}


def app_dir_value() -> str:
    return _APP_DIR_HOLDER["v"]


def main() -> int:
    print(f"Python {sys.version.split()[0]}  frozen={getattr(sys, 'frozen', False)}")
    print(f"sys.executable = {sys.executable}")
    print(f"_MEIPASS      = {getattr(sys, '_MEIPASS', '<非打包运行>')}")
    print(f"项目目录      = {REPO}")

    # 定位「程序目录」：优先命令行传入的 exe 所在目录，否则用项目目录
    app_dir = REPO
    if len(sys.argv) > 1 and sys.argv[1]:
        p = os.path.abspath(sys.argv[1])
        app_dir = os.path.dirname(p) if os.path.isfile(p) else p
    _APP_DIR_HOLDER["v"] = app_dir
    section_temp()
    section_drive(app_dir)
    section_bootstrap(app_dir)
    section_summary()
    return 1 if any(not ok for _, ok, _ in _probe_results) else 0


if __name__ == "__main__":
    sys.exit(main())
