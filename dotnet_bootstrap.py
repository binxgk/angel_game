"""dotnet_bootstrap.py — 让 pywebview 的 Windows 后端能正常用上 .NET

背景（踩坑记录）
----------------
pywebview 在 Windows 上的窗口后端（winforms / edgechromium / mshtml）依赖
pythonnet 通过 clr_loader 加载 CLR。clr_loader 在 Windows 上默认选 **netfx**
（.NET Framework），但 pythonnet 3.x 的 `Python.Runtime.dll` 是
**netstandard2.0** 目标程序集，在 .NET Framework 下加载时缺少一整套门面程序集
（netstandard.dll 等约 90 个），导致 `Assembly::LoadFrom` 之后
`GetType("Python.Runtime.Loader")` 拿不到类型，最终抛：

    RuntimeError: Failed to resolve Python.Runtime.Loader.Initialize
    from ...\\pythonnet\\runtime\\Python.Runtime.dll

pywebview 自己写了一个回退：

    try:
        import clr
    except Exception:
        os.environ['PYTHONNET_RUNTIME'] = 'coreclr'
        import clr

但**这个回退是失效的**：第一次 `import clr` 失败时，pythonnet 已经把
`_RUNTIME` 设成了一个坏掉的 NetFx 实例（`set_runtime_from_env()` 只负责
"创建 runtime 对象"，真正失败发生在后面取函数那一步）。第二次 `import clr`
时 `pythonnet.load()` 看到 `_RUNTIME is not None`，**不会重新读取
`PYTHONNET_RUNTIME`**，于是继续用那个坏的 NetFx，照样失败。

修复思路
--------
在 `import webview` **之前**就把运行时定好，让 pywebview 的第一次
`import clr` 直接走对的路：

1. 选运行时：优先 coreclr（只要机器上有 `Microsoft.WindowsDesktop.App`，
   因为 WinForms / WebView2 都在这个共享框架里）。
2. 写一份 runtimeconfig.json，framework 指名
   `Microsoft.WindowsDesktop.App`。**不能**用 clr_loader 自动生成的那份——
   它只写 `Microsoft.NETCore.App`，那样 `clr.AddReference('System.Windows.Forms')`
   会报 FileNotFoundException。
3. 预先 `import clr` 并补上 `Microsoft.Win32.SystemEvents` 引用
   （pywebview 的 winforms.py 会 `from Microsoft.Win32 import SystemEvents`，
   而该程序集在 coreclr 下不会自动进入可见范围）。预先 import 之后
   `sys.modules['clr']` 已存在，pywebview 自己的 `import clr` 直接命中缓存。

任何一步失败都安静放过：交给 pywebview 原本的逻辑去尝试，
不让引导代码本身成为新的崩溃点。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile

# 只做一次，避免重复探测
_DONE = False

# 最近一次 prepare() 的真实结果与失败原因，供 --selfcheck 诊断读取
_LAST_RESULT: str | None = None
_LAST_ERROR: str | None = None

# 我们的 runtimeconfig.json 放在用户数据目录，不污染程序目录
_RC_NAME = "pywebview.runtimeconfig.json"

# 随包分发的 winforms.py 源码所在子目录（相对 sys._MEIPASS）。
# 两个 .spec 的 datas 必须带上这一项，否则冻结后拿不到源码，
# OpenFolderDialog 补丁装不上（详见 _install_winforms_patch）。
# 刻意不用 "webview/platforms" 这个真实包路径作目标，避免在 onedir 下
# 被 sys.path 当成真模块抢在 PYZ 前面命中。
_WINFORMS_SRC_SUBDIR = "_winforms_src"

# 首选目标框架版本；实际会按机器上装了的版本向下兼容挑选
_MIN_MAJOR = 6


def _app_dir() -> str:
    """返回程序所在目录：冻结时是 exe 所在目录，开发时是项目根目录。

    这是 runtimeconfig 的最后写入候选位置——用户既然能运行程序，
    就一定能往这里写文件，所以它是最可靠的兜底。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _log(msg: str) -> None:
    """尽力记一行调试日志（app_api 的 debug_log 此时可能还没就绪）。"""
    try:
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA") or ""
        if not base:
            return
        d = os.path.join(base, "ANGEL GAME")
        os.makedirs(d, exist_ok=True)
        import time

        line = time.strftime("[%H:%M:%S") + ".000] [dotnet] " + msg + "\n"
        with open(os.path.join(d, "angel_debug.log"), "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:  # noqa: BLE001
        pass


def _find_windowsdesktop_runtime() -> tuple[str, str] | None:
    """找一个可用的 Microsoft.WindowsDesktop.App，返回 (version, root)。

    直接扫 dotnet 安装目录，不依赖 clr_loader（它可能还没被 import）。
    """
    roots = []
    for env_name in ("DOTNET_ROOT", "DOTNET_ROOT_X64"):
        v = os.environ.get(env_name)
        if v:
            roots.append(v)
    roots.append(os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "dotnet"))
    roots.append(os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "dotnet"))

    best: tuple[tuple[int, ...], str, str] | None = None
    for root in roots:
        shared = os.path.join(root, "shared", "Microsoft.WindowsDesktop.App")
        if not os.path.isdir(shared):
            continue
        for name in os.listdir(shared):
            full = os.path.join(shared, name)
            if not os.path.isdir(full):
                continue
            # 只接受形如 8.0.13 的稳定版版本号（跳过 preview/rc）
            base = name.split("-")[0]
            parts = base.split(".")
            if len(parts) < 2:
                continue
            try:
                ver = tuple(int(p) for p in parts)
            except ValueError:
                continue
            if ver[0] < _MIN_MAJOR:
                continue
            if "-" in name:  # preview / rc 不选
                continue
            if best is None or ver > best[0]:
                best = (ver, name, root)
    if best is None:
        return None
    _, version, root = best
    return version, root


def _write_runtime_config(version: str) -> str | None:
    """写 runtimeconfig.json，指名 WindowsDesktop 框架。返回路径。

    依次尝试多个候选目录，全部失败才返回 None（顺序由 scratchdir.candidates()
    统一给出，详见 scratchdir.py 顶部说明）：
      1. %LOCALAPPDATA%\\ANGEL GAME       首选：用户私有、系统盘、不随漫游同步
      2. %APPDATA%\\ANGEL GAME            历史默认位置
      3. %TEMP% / %TMP%\\ANGEL GAME       仅当它们可写
      4. %USERPROFILE%\\ANGEL GAME
      5. %ProgramData%\\ANGEL GAME        机器级，通常需管理员，基本会失败
      6. 程序目录\\ANGEL GAME              最后防线

    候选列表刻意**不**再把程序目录放在显眼位置。历史上这里手写过一份
    「APPDATA → LOCALAPPDATA → TEMP → 程序目录」的列表，兜底是程序目录，
    于是**程序行为取决于 exe 被放在哪个盘**：前面候选全挂时，C:/D: 还有
    程序目录可写、W: 可能连兜底都没有，表现为「换个盘就起不来」。

    ⚠️ 复核修正（2026-10）：现场证据表明，本机那次「W: 起不来」的已证实主因
    是 winforms 补丁在冻结包里失效 + W: 上那份 exe 是缺 scratchdir 的过期构建，
    【不是】这里。而实测 W:\ai_01\angel_game\ 与 %TEMP% 都是可写的。
    所以本处改动应如实看作**防御性加固**（针对「兜底落到程序目录 → 行为随
    盘符变化」这一真实但本次未证实命中的失败模式），而非已证实的根因。
    统一走 scratchdir 依然值得保留：用户私有目录永远优先、程序目录永远最后。
    """
    major, minor = version.split(".")[0], version.split(".")[1]
    cfg = {
        "runtimeOptions": {
            "tfm": f"net{major}.{minor}",
            "framework": {
                "name": "Microsoft.WindowsDesktop.App",
                "version": f"{major}.{minor}.0",
            },
            "rollForward": "LatestMinor",
        }
    }

    # 统一的候选列表（按可靠性排序，程序目录永远排最后）。
    # import 失败时退回旧的手写列表，保证本模块脱离 main.py 单独被 import
    # 时也能工作。
    try:
        import scratchdir

        candidates = scratchdir.candidates()
    except Exception:  # noqa: BLE001
        candidates = []
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(os.path.join(appdata, "ANGEL GAME"))
        localappdata = os.environ.get("LOCALAPPDATA")
        if localappdata:
            candidates.append(os.path.join(localappdata, "ANGEL GAME"))
        tmp = os.environ.get("TEMP") or os.environ.get("TMP")
        if tmp:
            candidates.append(os.path.join(tmp, "ANGEL GAME"))
        # 最后防线：程序所在目录。冻结时用 exe 所在目录，开发时用项目目录。
        candidates.append(_app_dir())

    for d in candidates:
        try:
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, _RC_NAME)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
            _log(f"runtimeconfig 已写入 {path}")
            return path
        except Exception as exc:  # noqa: BLE001
            _log(f"runtimeconfig 写入 {d} 失败: {type(exc).__name__}: {exc}")
            continue

    return None


def prepare() -> str:
    """在 import webview 之前调用。返回一句状态描述（供日志）。

    幂等：重复调用只返回首次的结果，不重复探测。
    """
    global _DONE, _LAST_RESULT
    if _DONE:
        return _LAST_RESULT or "already prepared"
    _DONE = True
    _LAST_RESULT = _prepare_impl()
    return _LAST_RESULT


def _prepare_impl() -> str:
    """prepare() 的实际实现（_DONE 由外层管理，这里可以安全重入）。"""
    if sys.platform != "win32":
        return "skipped (not windows)"

    # 用户已经显式指定了运行时，尊重它，不插手
    if os.environ.get("PYTHONNET_RUNTIME"):
        return f"skipped (PYTHONNET_RUNTIME={os.environ['PYTHONNET_RUNTIME']} preset)"

    found = _find_windowsdesktop_runtime()
    if not found:
        # 没有 .NET Desktop 运行时：保持原样，让 pywebview 自己试 netfx
        _log("未找到 Microsoft.WindowsDesktop.App，交给 pywebview 自行处理")
        return "no desktop runtime found; left to pywebview"

    version, root = found
    rc = _write_runtime_config(version)
    if not rc:
        # 写不出 runtimeconfig 就没法给 coreclr 指定 WindowsDesktop 框架，
        # 强行上 coreclr 只会得到 "Could not load System.Windows.Forms"。
        # 直接放弃、交给 netfx，不要赌。
        _log("runtimeconfig 写入失败，放弃 coreclr，交给 netfx")
        return "could not write runtimeconfig; left to netfx"

    os.environ["PYTHONNET_RUNTIME"] = "coreclr"
    os.environ["PYTHONNET_CORECLR_RUNTIME_CONFIG"] = rc
    _log(f"已选 coreclr (WindowsDesktop {version} @ {root}), config={rc}")

    # 预热 clr 并【验证到真正能用】为止。
    #
    # 注意：只验证 `import clr` 成功是不够的——那只是把 CLR 拉起来，不代表
    # pywebview 的后端能加载。coreclr(.NET 8) 下有一个已知的硬伤：
    # pywebview 随包分发的 Microsoft.Web.WebView2.WinForms.dll 是针对
    # .NET Framework 编译的，它引用 System.Windows.Forms.ContextMenu，
    # 而该类型在 .NET Core 3.0+ 已被移除，于是加载 WebView2 时报
    #   System.TypeLoadException: Could not load type
    #   'System.Windows.Forms.ContextMenu' from assembly 'System.Windows.Forms, Version=8.0.0.0'
    # 所以这里必须把 webview 的 winforms 后端真的 import 一遍做体检。
    # 体检不过就如实报告（见下方 except），【不回退 netfx】——理由见那里。
    try:
        import clr  # noqa: F401  (pythonnet 的 legacy shim，内部会 load())

        # pywebview 的 winforms.py 会 `from Microsoft.Win32 import SystemEvents`。
        # 该程序集在 .NET Framework 下属于 System.dll 自动可见，但在 coreclr 下
        # 必须显式引用，否则报 ImportError。
        clr.AddReference("Microsoft.Win32.SystemEvents")

        # 换掉 pywebview 自带的 .NET Framework 版 WebView2 程序集。
        _swap_webview2_assemblies()

        # 给 winforms 后端打补丁，绕开它依赖的 .NET Framework 私有 API。
        _install_winforms_patch()

        # 体检：真正把后端加载起来，确认 WebView2 / WinForms 都可用。
        # 这一步不通过，coreclr 就是走不通的。
        from webview.platforms import winforms as _wf  # noqa: F401

        _log(f"clr 预热成功，后端体检通过 (renderer={getattr(_wf, 'renderer', '?')})")
        return f"coreclr prepared (WindowsDesktop {version}, renderer={getattr(_wf, 'renderer', '?')})"
    except Exception as exc:  # noqa: BLE001
        # coreclr 这条路走不通。
        #
        # 【重要】这里**不再回退 netfx**。原因：pythonnet 3.x 的
        # Python.Runtime.dll 是 netstandard2.0 目标程序集，在 .NET Framework
        # 下加载会缺少一整套门面程序集（netstandard.dll 等），
        # `Failed to resolve Python.Runtime.Loader.Initialize` 是必然结果。
        # 换句话说 netfx 回退【不是保险，是死路】：它只会把"补丁没装上"这个
        # 清晰可诊断的错误，掩盖成晦涩的 pythonnet 加载失败，让人误以为是
        # 环境问题而非代码问题（这个坑实际浪费了大量排查时间）。
        #
        # 所以这里保留 coreclr 的选择（哪怕体检没过），让真正的错误暴露出来。
        import traceback as _tb

        global _LAST_ERROR
        _LAST_ERROR = _tb.format_exc()
        _log(f"coreclr 体检未通过: {type(exc).__name__}: {exc}")
        _log("体检失败详细堆栈:\n" + _LAST_ERROR)
        # 注意：环境变量保持 coreclr 不变，不回退。
        return f"coreclr unusable ({type(exc).__name__}: {exc})"


def _webview2_candidates() -> list[str]:
    """找出机器上所有 .NET Core 版的 Microsoft.Web.WebView2.WinForms.dll。"""
    import glob as _glob

    roots: list[str] = []
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    la = os.environ.get("LOCALAPPDATA") or ""

    # WebView2 SDK / 各种带 WebView2 的应用都可能带 .NET Core 版程序集
    for base in (pf, pf86, la):
        if not base:
            continue
        roots += [
            os.path.join(base, "Microsoft", "EdgeWebView", "Application", "*"),
            os.path.join(base, "Microsoft", "EdgeWebView", "*", "*"),
            os.path.join(base, "Microsoft", "*", "*WebView2*", "*"),
            os.path.join(base, "*", "*WebView2*", "*"),
        ]

    found: list[str] = []
    for pattern in roots:
        for hit in _glob.glob(pattern, recursive=False):
            if os.path.isdir(hit):
                candidate = os.path.join(hit, "Microsoft.Web.WebView2.WinForms.dll")
                if os.path.isfile(candidate):
                    found.append(candidate)
    return found


def _is_dotnet_core_assembly(path: str) -> bool:
    """粗略判断一个托管程序集是否为 .NET Core 目标。

    .NET Core 程序集的 PE 里会引用 System.Runtime，而 .NET Framework 版会引用
    mscorlib。这里只做字符串扫描，够用且不依赖任何第三方库。
    """
    try:
        with open(path, "rb") as f:
            blob = f.read()
    except Exception:  # noqa: BLE001
        return False
    return b"System.Runtime" in blob and b"mscorlib" not in blob


def _swap_webview2_assemblies() -> None:
    """把 pywebview 自带的 .NET Framework 版 WebView2 程序集换成 .NET Core 版。

    为什么必须换：pywebview 随包分发的 Microsoft.Web.WebView2.WinForms.dll
    是针对 .NET Framework 编译的，它引用 System.Windows.Forms.ContextMenu，
    而该类型在 .NET Core 3.0+ 已被移除。coreclr 下加载它就报
      System.TypeLoadException: Could not load type 'System.Windows.Forms.ContextMenu'
    换成 .NET Core 版的同名程序集即可（接口一致，只是目标框架不同）。

    找不到可替换的版本就安静跳过，交给后面的体检去判定 coreclr 是否可用。
    """
    try:
        import webview  # noqa: F401

        # 注意：冻结后不能用 __file__ 定位数据目录 ——
        # `webview/lib` 是随包分发的【数据目录】而非 Python 包，打包后没有 __file__；
        # 正确做法是从 sys._MEIPASS（onedir/onefile 下都指向解包根）拼出来。
        base = getattr(sys, "_MEIPASS", None)
        if not base:
            import webview as _wv

            base = os.path.dirname(os.path.dirname(os.path.abspath(_wv.__file__)))
        target_dir = os.path.join(base, "webview", "lib")
        if not os.path.isdir(target_dir):
            _log(f"未找到 webview/lib 目录: {target_dir}")
            return
    except Exception as exc:  # noqa: BLE001
        _log(f"定位 webview/lib 失败: {type(exc).__name__}: {exc}")
        return

    names = (
        "Microsoft.Web.WebView2.WinForms.dll",
        "Microsoft.Web.WebView2.Core.dll",
    )

    # 先看现有的是不是已经是 .NET Core 版，是就不动
    existing = os.path.join(target_dir, names[0])
    if os.path.isfile(existing) and _is_dotnet_core_assembly(existing):
        _log("WebView2 程序集已是 .NET Core 版，无需替换")
        return

    src_dir = None
    for candidate in _webview2_candidates():
        if _is_dotnet_core_assembly(candidate):
            src_dir = os.path.dirname(candidate)
            break

    if not src_dir:
        _log("未找到 .NET Core 版 WebView2 程序集，保持原样")
        return

    import shutil

    replaced = []
    copy_failed: list[str] = []
    for name in names:
        src = os.path.join(src_dir, name)
        if not os.path.isfile(src):
            continue
        dst = os.path.join(target_dir, name)
        try:
            if os.path.isfile(dst):
                backup = dst + ".netfx.bak"
                if not os.path.isfile(backup):
                    shutil.copy2(dst, backup)
            shutil.copy2(src, dst)
            replaced.append(name)
        except Exception as exc:  # noqa: BLE001
            copy_failed.append(name)
            _log(f"替换 {name} 失败: {type(exc).__name__}: {exc}")

    if not replaced and copy_failed:
        # 一个都没换成，而且是因为写不进而失败的——典型场景：exe 放在
        # 只读盘 / 网络盘 / 没有写权限的目录（onedir 下 target_dir 就在
        # 程序目录里，sys._MEIPASS 就是 exe 所在目录）。
        #
        # 退路：不复制，直接把 .NET Core 版程序集 clr.AddReference 进 CLR。
        # 程序集一旦进入默认 AssemblyLoadContext，pywebview 后面那句
        # `from Microsoft.Web.WebView2.WinForms import WebView2` 就会解析到
        # 已加载的这一个，不需要磁盘上那份被换过的文件。
        # 这是纯增益：失败了也不过是回到「一个都没换成」的原状。
        _log(
            "webview/lib 不可写（程序目录只读？），改用 clr.AddReference "
            "直接加载 .NET Core 版 WebView2 程序集"
        )
        try:
            import clr

            for name in copy_failed:
                src = os.path.join(src_dir, name)
                if os.path.isfile(src):
                    clr.AddReference(src)
                    replaced.append(name)
                    _log(f"已通过 clr.AddReference 加载 {src}")
        except Exception as exc:  # noqa: BLE001
            _log(f"clr.AddReference 退路也失败: {type(exc).__name__}: {exc}")

    if replaced:
        _log(f"已从 {src_dir} 替换 WebView2 程序集: {', '.join(replaced)}")
    elif copy_failed:
        _log(
            "WebView2 程序集一个都没换成，且程序目录不可写。"
            "这会直接导致 coreclr 加载 .NET Framework 版 WebView2 时抛 "
            "System.TypeLoadException: Could not load type "
            "'System.Windows.Forms.ContextMenu'，窗口起不来。"
            "解决办法：把 exe 移到一个可写目录（如桌面、文档、D:\\ 根目录），"
            "或改用 angel_game.spec 编出的 onedir 版并放到可写位置。"
        )
    else:
        _log("WebView2 程序集一个都没换成")


def _install_winforms_patch() -> None:
    """让 pywebview 的 winforms 后端能在 .NET Core 下加载。

    背景：`webview/platforms/winforms.py` 里有个 `class OpenFolderDialog`，
    它的**类属性**在导入时就会执行反射：

        iFileDialogType = windowsFormsAssembly.GetType(
            'System.Windows.Forms.FileDialogNative+IFileDialog')

    `FileDialogNative` 是 .NET Framework 的私有类型，在 .NET Core 里**已被删除**，
    于是 GetType 返回 None，紧接着 `.GetMethod(...)` 就抛 AttributeError，
    整个 winforms 模块 import 失败 —— 哪怕我们根本不用"选文件夹"这个功能。

    本项目的文件选择走的是 `FileDialog.OPEN`（选文件），用的是标准
    `WinForms.OpenFileDialog` 控件，与这个类无关。所以这里把该类的类属性块
    换成惰性实现：`show()` 在 coreclr 下直接抛出明确异常，选文件夹不可用，
    但不影响选文件、也不影响后端加载。

    补丁方式：在 import 之前，把打好补丁的源码 exec 进一个预先注册到
    sys.modules 的模块对象里，这样后续 `import webview.platforms.winforms`
    直接命中缓存，不会再去执行原始源码。
    """
    mod_name = "webview.platforms.winforms"
    if mod_name in sys.modules:
        return  # 已经加载过了，不必再打

    try:
        # 关键：不要按源码路径去找 winforms.py ——
        # 冻结后它被编进 PYZ，磁盘上根本没有 .py 文件。
        # 必须通过 importlib 找到模块并让 loader 交出源码。
        spec = importlib.util.find_spec(mod_name)
        if spec is None or spec.loader is None:
            _log("找不到 webview.platforms.winforms 模块，跳过补丁")
            return

        src_path = spec.origin or "<winforms>"
        loader = spec.loader

        src = None
        src_origin = None

        # 候选 1：随包分发的 winforms.py 源文件。
        #
        # 为什么必须有它 —— 这是本次「W: 起不来」故障的【已证实根因】
        # （与 exe 放在哪个盘无关：C:/D:/E: 上同一份构建崩法完全一样）：
        # 冻结后 loader.get_source() 返回 None、spec.origin 指向的 .py
        # 在磁盘上也不存在，于是补丁源码拿不到 → 补丁装不上 →
        # `class OpenFolderDialog` 在 coreclr 下 import 时就炸
        # AttributeError: 'NoneType' object has no attribute 'GetMethod'
        # （webview/platforms/winforms.py:680）→ prepare() 的 coreclr 体检不过 →
        # 按设计【不回退 netfx，保留 coreclr】把真实错误暴露出来（见本文件
        # prepare() 里「netfx 回退不是保险，是死路」那段）→
        # 最终 import webview 抛同一个 AttributeError → 窗口起不来。
        #
        # 源文件由 .spec 的 datas 带进包（见 _winforms_source()）。
        # 本机装的是 PyInstaller 6.x，onedir 的数据文件都在 _internal 下，
        # 所以实际落点是：
        #   onedir   dist\ANGEL GAME v3\_internal\_winforms_src\winforms.py
        #   onefile  %TEMP%\_MEIxxxxxx\_winforms_src\winforms.py
        # 两者都靠 sys._MEIPASS 定位，不要按 exe 旁边的相对路径去找。
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            shipped = os.path.join(meipass, _WINFORMS_SRC_SUBDIR, "winforms.py")
            try:
                if os.path.isfile(shipped):
                    with open(shipped, encoding="utf-8") as f:
                        src = f.read()
                    src_origin = shipped
            except Exception as exc:  # noqa: BLE001
                _log(f"读取随包 winforms 源失败: {type(exc).__name__}: {exc}")
                src = None

        # 候选 2：开发模式 / onedir 从源码跑，loader 直接给源码
        get_source = getattr(loader, "get_source", None)
        if not src and get_source is not None:
            try:
                src = get_source(mod_name)
                if src:
                    src_origin = src_path
            except Exception as exc:  # noqa: BLE001
                _log(f"loader.get_source 失败: {type(exc).__name__}: {exc}")

        # 候选 3：源码真的以 .py 形式存在于磁盘上
        if not src and spec.origin and os.path.isfile(spec.origin):
            try:
                with open(spec.origin, encoding="utf-8") as f:
                    src = f.read()
                src_origin = spec.origin
            except Exception as exc:  # noqa: BLE001
                _log(f"读取 {spec.origin} 失败: {type(exc).__name__}: {exc}")

        if not src:
            # 走到这里说明 .spec 的 datas 漏了 _winforms_src 这一项
            # （或者用的是老构建）。如实说明，别静默跳过——
            # 静默跳过的后果就是上面那条 AttributeError，极难定位。
            _log(
                "拿不到 winforms 源码（冻结后 loader 不提供源码，"
                "且随包的 _winforms_src/winforms.py 不存在）→ "
                "OpenFolderDialog 补丁装不上，import webview 会在 "
                "winforms.py:680 抛 AttributeError。"
                "解决办法：用带 datas 的 angel_game.spec / "
                "angel_game_onefile.spec 重新打包。"
            )
            return

        marker = "class OpenFolderDialog:"
        if marker not in src:
            _log("winforms 源码里没有 OpenFolderDialog，跳过补丁")
            return

        start = src.index(marker)

        # 类的结束位置：往后找第一个"行首无缩进"的 class/def，那之前就是类块。
        # 注意：类块与下一个顶层定义之间还可能夹着模块级语句
        # （本文件里就有 `_main_window_created = Event()` 和
        # `_already_set_up_app = False`），这些必须原样保留，
        # 只把类体本身替换掉。
        lines = src[start:].split("\n")
        end_line = len(lines)
        for i, ln in enumerate(lines[1:], 1):
            if ln and not ln[0].isspace() and (ln.startswith("class ") or ln.startswith("def ")):
                end_line = i
                break

        # 从类块末尾往回退：把尾部的空行与「非缩进的模块级赋值」都留在原处
        keep_from = end_line
        while keep_from > 0:
            probe = lines[keep_from - 1]
            if probe.strip() == "" or (probe and not probe[0].isspace()):
                keep_from -= 1
            else:
                break

        end = start + sum(len(l) + 1 for l in lines[:keep_from])

        stub = (
            "class OpenFolderDialog:\n"
            "    # .NET Core 下 System.Windows.Forms.FileDialogNative 已被移除，\n"
            "    # 原实现依赖它。本项目只用「选文件」，此处降级为不可用。\n"
            "    @classmethod\n"
            "    def show(cls, parent=None, initialDirectory=None, allow_multiple=False, title=None):\n"
            "        raise NotImplementedError(\n"
            "            'Folder picker is unavailable under the .NET Core runtime'\n"
            "        )\n"
            "\n\n"
        )

        patched = src[:start] + stub + src[end:]

        # 关键：把补丁后的源码写成一个【真实存在的 .py 文件】，然后用
        # importlib 从它正常加载，而不是手工造 ModuleType 再 exec。
        #
        # 为什么必须这样（踩坑记录）：手工造的模块没有 __spec__ / __loader__，
        # 本地开发时靠 sys.modules 缓存能蒙混过关，但【冻结后】PyInstaller 的
        # pyimod02_importers 不认这种野模块，会重新执行原始源码，于是补丁白打，
        # 又撞回 OpenFolderDialog 的 AttributeError。
        # 写成真实文件后，模块拥有完整的 spec/loader，import 机制正常接管。
        #
        # 目录选择：交给 scratchdir 统一解析，用户私有目录优先、程序目录垫底。
        # 原先这里是手写的「TMP → TEMP → 程序目录」三段列表，一旦 TMP/TEMP
        # 都不可用就落到程序目录，而程序目录写在哪个盘不受我们控制——
        # 这正是「换个盘就起不来」的直接原因之一。
        # 附带好处：补丁文件改落在 %LOCALAPPDATA%\ANGEL GAME\patch 下，
        # 不会被磁盘清理当成临时垃圾扫掉，也不用每次启动重打一遍。
        try:
            import scratchdir

            patch_dir = scratchdir.resolve("patch")
        except Exception:  # noqa: BLE001
            patch_dir = None
            for _base in (os.environ.get("TMP"), os.environ.get("TEMP"), _app_dir()):
                if not _base:
                    continue
                _cand = os.path.join(_base, "angel_game_patch")
                try:
                    os.makedirs(_cand, exist_ok=True)
                    _probe = os.path.join(_cand, ".w")
                    with open(_probe, "w", encoding="utf-8") as _f:
                        _f.write("")
                    os.remove(_probe)
                    patch_dir = _cand
                    break
                except Exception:  # noqa: BLE001
                    continue

        if not patch_dir:
            _log("找不到可写的补丁目录，跳过 winforms 补丁")
            return

        patched_path = os.path.join(patch_dir, "winforms_patched.py")
        with open(patched_path, "w", encoding="utf-8") as f:
            f.write(patched)

        spec2 = importlib.util.spec_from_file_location(mod_name, patched_path)
        if spec2 is None or spec2.loader is None:
            _log("无法为补丁后的 winforms 建立 module spec，跳过补丁")
            return

        module = importlib.util.module_from_spec(spec2)
        sys.modules[mod_name] = module

        # 关键补充：把子模块【同时挂到父包上】。
        #
        # 只写 sys.modules 是不够的。pywebview 的 guilib.import_winforms()
        # 用的是 `import webview.platforms.winforms as guilib`，而
        # `import a.b.c as x` 最终要做一次属性访问（或 IMPORT_FROM），
        # 取的是[父包对象]上的 `winforms` 属性。
        # 预注册进 sys.modules 的模块不会走 _load()，也就不会自动
        # setattr(parent, 'winforms', module)，于是那句 import 会抛
        #   ImportError: cannot import name 'winforms' from 'webview.platforms'
        # → try_import 判定 winforms 后端不可用 → WebViewException
        # → 补丁"装了"但窗口照样起不来，是最难查的一类失败。
        parent_name, _, child_name = mod_name.rpartition(".")
        parent = sys.modules.get(parent_name)
        if parent is not None and child_name:
            try:
                setattr(parent, child_name, module)
            except Exception:  # noqa: BLE001
                pass

        try:
            spec2.loader.exec_module(module)
        except Exception:
            _detach_submodule(mod_name)
            raise
        _log(f"winforms 模块补丁已安装（OpenFolderDialog 降级，来源 {src_origin}）")
    except Exception as exc:  # noqa: BLE001
        _detach_submodule(mod_name)
        _log(f"安装 winforms 补丁失败: {type(exc).__name__}: {exc}")


def _detach_submodule(mod_name: str) -> None:
    """把 mod_name 从 sys.modules 与父包属性上一起摘掉（打补丁失败时的回滚）。

    两边都要清：只清 sys.modules 会在父包上留一个指向坏模块的悬空属性，
    之后 pywebview 的 `import webview.platforms.winforms as guilib`
    会拿到那个坏模块而不是重新加载，错误现场被污染。
    """
    sys.modules.pop(mod_name, None)
    parent_name, _, child_name = mod_name.rpartition(".")
    if not child_name:
        return
    parent = sys.modules.get(parent_name)
    if parent is None:
        return
    try:
        if getattr(parent, child_name, None) is not None:
            delattr(parent, child_name)
    except Exception:  # noqa: BLE001
        pass


def _recover_pythonnet() -> None:
    """把 pythonnet 恢复成"未加载"状态，好让下一次 import clr 重新按环境变量选运行时。

    三个坑，少清一个都会让回退失效：

    1. `pythonnet.load()` 开头就是 `if _LOADED: return`。只清 `_RUNTIME` 而留着
       `_LOADED=True`，下次 `import clr` 会立刻 return，压根不会重新选运行时。
    2. `_RUNTIME` 不清，`load()` 里 `if _RUNTIME is None: set_runtime_from_env()`
       这个分支就进不去，环境变量改了也白改（pywebview 自带的 coreclr 回退
       正是这样失效的）。
    3. **绝对不能 pop sys.modules 里的 "clr" / "pythonnet"**。`clr.py` 在导入时
       就把 `pythonnet` 的对象绑进了自己的模块命名空间；把 sys.modules 里的条目
       删掉后再 import，会得到**全新的模块对象**，与 `clr.py` 里绑定的那个状态分裂，
       结果是两个运行时都起不来。只重置模块内的状态标志，模块对象必须保持同一个。
    """
    try:
        import pythonnet

        runtime = getattr(pythonnet, "_RUNTIME", None)
        if runtime is not None:
            try:
                runtime.shutdown()
            except Exception:  # noqa: BLE001
                pass

        pythonnet._RUNTIME = None
        pythonnet._LOADED = False
        pythonnet._LOADER_ASSEMBLY = None
        _log("已重置 pythonnet 加载状态 (_RUNTIME/_LOADED/_LOADER_ASSEMBLY)")
    except Exception as exc:  # noqa: BLE001
        _log(f"重置 pythonnet 加载状态失败: {type(exc).__name__}: {exc}")

    # 注意：这里【不】动 sys.modules —— clr / pythonnet 必须保持同一个模块对象，
    # 只靠上面的状态标志让它下次被调用时重新走一遍 load()。
