# -*- mode: python ; coding: utf-8 -*-
"""angel_game.spec — PyInstaller 打包配置（onedir 目录形式、无控制台）

用法：
    .venv\\Scripts\\pyinstaller.exe --noconfirm --clean angel_game.spec
    实际可执行文件在 W:/ai_01/.venvs/angel_game/Scripts/pyinstaller.exe
    （用正斜杠写，Windows 同样认；请在 W:/ai_01/angel_game 目录下执行）

产物补充（本机 PyInstaller 版本 6.22.3）：依赖与数据都收在
    dist/ANGEL GAME v3/_internal/ 下，发布时必须整份 dist/ANGEL GAME v3/
    一起拷贝 —— 只拷那个 exe 起不来。之前 build/ 里那份老产物没有
    _internal/ 目录，是用 PyInstaller 5.x 打的，这点本身就是它已过期的旁证。

产物：dist\\ANGEL GAME v3\\ANGEL GAME v3.exe（双击 dist 目录里的 exe 即用，
      用户数据仍写在 %APPDATA%\\ANGEL GAME）

为什么用 onedir 而不是 onefile：
- onefile 启动时要把自身解压到 %TEMP%，一旦 TEMP 被指向不可写位置
  （例如 C:\\Windows\\TEMP，或 TEMP/TMP 不一致）就报
  "Could not create temporary directory!"，程序根本起不来。
- onedir 把依赖平铺在 dist\\ANGEL GAME v3\\ 里，运行时零解压、零临时目录依赖，
  对环境不挑，启动也更快。代价是发布时要带整个文件夹而不是单个 exe。

几个刻意保留的细节：
- datas 里带上 web/index.html 与 assets：config._bundle_dir() 在打包后取 sys._MEIPASS，
  main.py 也按 _MEIPASS/web/index.html 找界面，漏了就白屏。
  （onedir 下 sys._MEIPASS 就是 dist\\ANGEL GAME v3 本身，路径解析与 onefile 一致。）
- hiddenimports 补齐 pywebview 的 Windows 后端（EdgeChromium / WinForms）以及
  pythonnet(pythonnet + clr_loader)。这些是运行时动态加载的，PyInstaller 静态分析看不到。
- excludes 剔掉 tkinter / numpy / pandas / matplotlib / PyQt 系：本项目一个都不用，
  留着只是让体积和杀软误报都变难看。

依赖版本（务必照装，版本错了会出诡异问题）：
    pip install "pywebview==6.2.1" pillow pywin32 pyinstaller
- 必须用 pywebview 4.x+：main.py 用到了 create_window(maximized=) 与 win.events.*
  （3.x 两者都没有，会在 create_window 直接 TypeError，表现为双击无反应）。
  main.py 里已加能力探测做兜底，但仍建议锁定 6.2.1。
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
# 目标子目录刻意不叫 webview/platforms，免得在 onedir 下被 sys.path
# 当成真模块抢在 PYZ 前面命中。
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
    print("[angel_game.spec] 警告：找不到 webview/platforms/winforms.py 源码，"
          "未随包分发。冻结后 winforms 补丁将无法安装，"
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

# onedir 结构：EXE 只含纯 Python 代码（exclude_binaries=True），
# 二进制与数据文件由下面的 COLLECT 平铺到 dist\\ANGEL GAME v3\\ 里。
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    console=False,          # 无控制台窗口（调试时可临时改 True）
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/icon.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name=APP_NAME,
)
