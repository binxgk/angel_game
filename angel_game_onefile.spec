# -*- mode: python ; coding: utf-8 -*-
"""angel_game_onefile.spec — 额外产物：单文件 exe 版

用法：
    .venv\\Scripts\\pyinstaller.exe --noconfirm --clean angel_game_onefile.spec

产物：dist\\ANGEL GAME v3 (单文件).exe

与主编译（angel_game.spec，onedir）的关系：
- **angel_game.spec（onedir）是主编译**，日常发布/自用走它：零解压、零临时目录依赖、
  启动更快、对环境不挑。
- 本 spec 只是**额外**编一个单文件版，方便「只想要一个 exe」的场合。

单文件版天生的代价（用之前请知情）：
- 启动时 PyInstaller 引导程序要把自身解压到临时目录。若 %TEMP% / %TMP% 指向
  不可写位置（例如被指向 C:\\Windows\\TEMP，或两个变量不一致指向无效路径），
  会直接报 "Could not create temporary directory!" 而起不来。
- 单文件版每次启动都要解压一遍，冷启动明显慢于 onedir。
- 本 spec 刻意保留以下设置以尽量降低风险：
    * runtime_tmpdir 不指定（沿用系统 %TEMP%），因为指定固定目录反而更容易踩权限坑；
    * 保持与主编译完全一致的 hiddenimports / excludes / datas，
      保证两个产物在功能上等价（含 dotnet_bootstrap 的 .NET 运行时引导）。

依赖版本（务必照装，版本错了会出诡异问题）：
    pip install "pywebview==6.2.1" pillow pywin32 pyinstaller
- 必须用 pywebview 4.x+：main.py 用到了 create_window(maximized=) 与 win.events.*
  （3.x 两者都没有，会在 create_window 直接 TypeError，表现为双击无反应）。
- 装 pywebview 时【不要】加 --only-binary :all:：它依赖的 proxy_tools 只有 sdist，
  加了该参数会让 pip 找不到依赖、回退去逐个尝试全部历史版本，CPU 空转十几分钟。
- pillow / pywin32 是硬依赖：icon_extractor.py 的 PIL 导入没有保护，缺了会在
  ensure_default_icon 处 ImportError；win32gui 用于提取 exe 图标与激活已有窗口。
"""
from PyInstaller.utils.hooks import collect_submodules

import sys

APP_NAME = "ANGEL GAME v3"

# ---------------------------------------------------------------------------
# 随包分发 winforms.py 的【源码】
# ---------------------------------------------------------------------------
# 为什么必须带这一项（这是「换个盘就起不来」真正的根因之一，与盘符无关）：
#
#   dotnet_bootstrap._install_winforms_patch() 要在 import webview 之前，
#   把 `class OpenFolderDialog` 的类体反射换成惰性 stub。那个类在 .NET Core
#   下 import 时就炸：
#       AttributeError: 'NoneType' object has no attribute 'GetMethod'
#       （webview/platforms/winforms.py:680，FileDialogNative+IFileDialog
#         是 .NET Framework 私有类型，coreclr 下 GetType 返回 None）
#
#   补丁源码本来取自 winforms.py 本身，但【冻结后】拿不到：
#       - loader.get_source() 返回 None（PyFrozenLoader 不提供源码）
#       - spec.origin 指向的 .py 在磁盘上不存在（已被编进 PYZ）
#   于是补丁永远装不上 → coreclr 体检必败 → 回退 netfx 也起不来 →
#   双击无反应、angel_debug.log 里只有一行「拿不到 winforms 源码，跳过补丁」。
#   C:/D:/E: 上放同样的构建，崩法一模一样。
#
# 解法：把构建机上那份 winforms.py 源码当数据文件带进包，运行时从
#   sys._MEIPASS/_winforms_src/winforms.py 读出来再打补丁。
#   目标子目录刻意不叫 webview/platforms，免得在 onedir 下被 sys.path
#   当成真模块抢在 PYZ 前面命中。
def _winforms_source():
    import importlib.util
    import os

    # 1) 直接按「执行 pyinstaller 的那个 Python」猜 site-packages 位置。
    #    不走 import，避免 import webview 带来任何构建期副作用。
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    for sp in (
        os.path.join(exe_dir, "Lib", "site-packages"),   # venv / Windows
        os.path.join(exe_dir, "..", "Lib", "site-packages"),
    ):
        cand = os.path.join(sp, "webview", "platforms", "winforms.py")
        if os.path.isfile(cand):
            return cand

    # 2) 退路：让 importlib 自己找（能找到就一定有 origin）
    try:
        spec = importlib.util.find_spec("webview.platforms.winforms")
        if spec and spec.origin and os.path.isfile(spec.origin):
            return spec.origin
    except Exception:  # noqa: BLE001
        pass
    return None


datas = [
    ("web/index.html", "web"),
    ("assets", "assets"),
]

_winforms_src = _winforms_source()
if _winforms_src:
    datas.append((_winforms_src, "_winforms_src"))
else:
    print("[angel_game_onefile.spec] 警告：找不到 webview/platforms/winforms.py "
          "源码，未随包分发。冻结后 winforms 补丁将无法安装，"
          "import webview 会在 winforms.py:680 抛 AttributeError。")

hidden = [
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    "webview.platforms.mshtml",
    "clr_loader",
    "pythonnet",
    "bottle",
]
hidden += collect_submodules("webview")

excludes = [
    "tkinter", "numpy", "pandas", "matplotlib", "scipy",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "cefpython3",
    "pytest", "pip",
]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

# onefile 结构：binaries 与 datas 全部塞进这一个 EXE（exclude_binaries 不设 True），
# 没有 COLLECT。产物名字里带「(单文件)」以免与 onedir 目录同名、误点。
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=f"{APP_NAME} (单文件)",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,    # 沿用系统 %TEMP%；不指定固定目录，避免额外的权限坑
    console=False,          # 无控制台窗口（调试时可临时改 True）
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/icon.ico",
)
