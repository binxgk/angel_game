"""scratchdir.py — 唯一权威的「可写目录 / 临时目录」解析器（P0）

为什么需要这个模块
---------------
项目里有三处各自为政地找「能写文件的目录」，每处的候选列表还不一样：

  1. PyInstaller 单文件引导程序解压 _MEIxxxx —— 走 GetTempPathW()，
     失败则退到 exe 同目录（这段是 C 代码，Python 管不到）。
  2. dotnet_bootstrap._write_runtime_config ——
     %APPDATA% → %LOCALAPPDATA% → %TEMP% → **程序目录**
  3. dotnet_bootstrap._install_winforms_patch ——
     %TMP% → %TEMP% → **程序目录**

问题就出在「程序目录」这个兜底：它让程序的行为**取决于 exe 被放在哪个盘**。

【2026-10 复核修正 —— 请以此为准】
现场取证（angel_debug.log + build\angel_game\PYZ-00.toc）表明，本机「W: 起不来」
的已证实主因【不是】这里，而是：
  (1) dotnet_bootstrap 的 winforms 补丁在冻结包里拿不到源码 → import webview
      必崩（webview/platforms/winforms.py:680，AttributeError）；
  (2) W: 上那个 exe 是缺 scratchdir 模块的过期构建 → ModuleNotFoundError，
      死在写日志之前，表现为「双击无反应」。
实测 `W:\ai_01\angel_game\` 可写、`%TEMP%` 也可写，所以「W: 不可写」没被证实。

因此本模块的准确定位是：**防御性加固**，针对「兜底落到程序目录 → 行为随盘符
变化」这一类真实存在、但本次故障中并未证实命中的失败模式。它值得保留
（成本约 1ms，且能把整条解析链写进日志），但它不是本次故障的根因，
不要再拿它去解释「换盘就起不来」。

本模块做三件事，把「换盘就起不来」这一类问题一次性关掉：

  A. `resolve()` 给出一份**按可靠性排序**的候选，用户私有目录优先，
     程序目录**永远排最后**且只在无路可走时用。
  B. `repair_temp_env()` 在**最早期的启动阶段**检查 %TMP%/%TEMP%：
     如果不存在、不是目录、或根本写不进去，就把它们**在本进程内**改写到
     一个确定可写的位置。这一下修好的是全链路——不只是本模块，
     还包括 SQLite 的临时文件、tempfile、icon_extractor、
     dotnet_bootstrap 的 TEMP 候选，以及任何第三方库。
     （注意：只改 os.environ 是不够的，tempfile 会把首次结果缓存进
      `tempfile.tempdir`，所以这里显式把它也改掉。）
  C. 每一步都写进 angel_debug.log，下次出问题一个文件就能定位，
     不用再猜。

设计约束
--------
- 本模块**只依赖标准库**（os / sys / ctypes / tempfile / time），
  且不得在 import 期做任何 IO——它要在 main.py 最早期被调用，
  那时日志系统还没起来。
- 任何单个候选的探测失败都静默跳过，绝不让「找目录」本身成为崩溃点。
- 探测一律用真实读写（建文件→写→删），不看权限位——
  Windows 上 ACL / Controlled Folder Access / 只读位的组合太乱，
  只有真写一次才算数。
"""
from __future__ import annotations

import ctypes
import os
import sys
import tempfile
import time

# 显式导入 ctypes.wintypes —— 不要依赖「import ctypes 之后 ctypes.wintypes
# 一定存在」这个隐式副作用（Windows 上通常成立，但属于实现细节）。
# 少了它，_get_temp_path_w() 里的 ctypes.wintypes.MAX_PATH 会抛
# AttributeError，而那个调用点是 try/except 吞异常的：
# 结果是 GetTempPathW 这条候选【静默消失】——正是最不该发生的那种失败。
try:
    import ctypes.wintypes  # noqa: F401
except Exception:  # noqa: BLE001  # 非 Windows 上 wintypes 不可用，忽略
    pass

# 应用名，与 config.py / dotnet_bootstrap.py 保持一致
APP_NAME = "ANGEL GAME"

# 探测结果缓存：进程内只认真探一次，避免启动路径上反复做磁盘 IO
_CACHE: dict[str, str | None] = {}

# repair_temp_env() 实际做了什么，供 main.py 记日志
_ACTIONS: list[str] = []


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def is_writable(path: str) -> bool:
    """真实探测：能在 path 下建文件→写→删，才算可写。

    只看 os.access() 是不够的：Windows 上 FACL 允许创建但 Controlled
    Folder Access / 杀软会拦；反过来只读位有时又只是显示属性。
    """
    if not path:
        return False
    try:
        os.makedirs(path, exist_ok=True)
        if not os.path.isdir(path):
            return False
        probe = os.path.join(path, ".__angel_scratch_probe__.tmp")
        try:
            with open(probe, "w", encoding="utf-8") as f:
                f.write("probe")
            return True
        finally:
            try:
                os.remove(probe)
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        return False


def app_dir() -> str:
    """程序所在目录：冻结时是 exe 所在目录，开发时是本模块所在目录。

    ⚠️ 这是**最不可靠**的候选：用户把 exe 放哪儿、那个盘/目录给不给他写，
    完全不受我们控制。只在无路可走时才允许用它。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _get_temp_path_w() -> str:
    """Windows 的 GetTempPathW()——PyInstaller 引导程序走的就是这个。"""
    try:
        buf = ctypes.create_unicode_buffer(ctypes.wintypes.MAX_PATH + 1)
        n = ctypes.windll.kernel32.GetTempPathW(ctypes.wintypes.MAX_PATH + 1, buf)
        if n:
            return buf.value
    except Exception:  # noqa: BLE001
        pass
    return ""


def _user_bases() -> list[str]:
    """用户私有目录候选，按可靠性从高到低。"""
    out: list[str] = []
    for name in ("LOCALAPPDATA", "APPDATA"):
        v = os.environ.get(name)
        if v and v not in out:
            out.append(v)
    # USERPROFILE 兜底：某些精简环境两个 AppData 变量都没设
    up = os.environ.get("USERPROFILE")
    if up and up not in out:
        out.append(up)
    return out


# --------------------------------------------------------------------------- #
# A. 解析可写目录
# --------------------------------------------------------------------------- #
def candidates(sub: str = "") -> list[str]:
    """按可靠性排序返回 `ANGEL GAME[\\sub]` 的所有候选目录。

    顺序（越靠前越可靠）：
      1. %LOCALAPPDATA%\\ANGEL GAME[\\sub]   用户私有、系统盘、不随漫游同步
      2. %APPDATA%\\ANGEL GAME[\\sub]        用户私有（历史默认位置）
      3. %TEMP%\\ANGEL GAME[\\sub]           仅当它可写
      4. %TMP%\\ANGEL GAME[\\sub]            仅当它可写
      5. %USERPROFILE%\\ANGEL GAME[\\sub]
      6. %ProgramData%\\ANGEL GAME[\\sub]    机器级，通常需管理员，基本会失败
      7. 程序目录\\ANGEL GAME[\\sub]        ⚠️ 最后防线，换盘行为就变

    注意第 3/4 条的顺序与 dotnet_bootstrap 原先的实现相反（原先是 TMP 先）。
    这里不是要改变谁先谁后——`resolve()` 会依次探测、取第一个真能写的，
    顺序只影响「谁被先试」。TEMP 与 TMP 在绝大多数机器上指向同一个地方。
    """
    bases: list[str] = []

    def _add(b: str | None) -> None:
        if b and b not in bases:
            bases.append(b)

    for b in _user_bases():
        _add(b)
    for name in ("TEMP", "TMP"):
        _add(os.environ.get(name))
    _add(_get_temp_path_w())
    _add(os.environ.get("USERPROFILE"))
    _add(os.environ.get("ProgramData") or os.environ.get("ALLUSERSPROFILE"))

    tail = f"{APP_NAME}\\{sub}" if sub else APP_NAME
    out = [os.path.join(b, tail) for b in bases if b]
    # 程序目录兜底：换盘行为变化的根源，放最后
    out.append(os.path.join(app_dir(), tail))
    # 去重（保留顺序）
    seen: set[str] = set()
    return [p for p in out if not (p.lower() in seen or seen.add(p.lower()))]


def resolve(sub: str = "", *, create: bool = True) -> str | None:
    """返回第一个确定可写的 `ANGEL GAME[\\sub]` 目录；全不可写返回 None。"""
    key = f"resolve::{sub}"
    if key in _CACHE:
        return _CACHE[key]
    found: str | None = None
    for d in candidates(sub):
        if is_writable(d):
            found = d
            break
    if create and found is None:
        # 连 create 都失败的极端情况：至少把用户目录建出来再试一次
        # （某些场景 makedirs 会因父目录不存在而失败，而不是权限问题）
        try:
            for b in _user_bases():
                d = os.path.join(b, f"{APP_NAME}\\{sub}" if sub else APP_NAME)
                os.makedirs(d, exist_ok=True)
                if is_writable(d):
                    found = d
                    break
        except Exception:  # noqa: BLE001
            pass
    _CACHE[key] = found
    return found


def log_dir() -> str:
    """angel_debug.log 所在目录。优先用户私有目录，绝不优先程序目录。"""
    for b in _user_bases():
        d = os.path.join(b, APP_NAME)
        if is_writable(d):
            return d
    d = resolve()
    return d or app_dir()


# --------------------------------------------------------------------------- #
# B. 修复 %TMP% / %TEMP%
# --------------------------------------------------------------------------- #
def repair_temp_env() -> list[str]:
    """检查并（必要时）在本进程内修复 %TMP% / %TEMP%。

    修好这一项的收益是**全链路**的：PyInstaller 的 Python 侧临时文件、
    SQLite 的临时表/排序文件、icon_extractor、以及 dotnet_bootstrap 里
    所有以 TEMP/TMP 为候选的分支，全部同时恢复正常。

    只改 `os.environ` 不够：`tempfile` 首次调用 `gettempdir()` 后会把结果
    缓存进模块级 `tempfile.tempdir`，之后就不再读环境变量了。所以这里
    必须显式把 `tempfile.tempdir` 也指过去。

    返回本次实际做的修正（人类可读），供 main.py 写进 angel_debug.log。
    """
    global _ACTIONS
    actions: list[str] = []
    if sys.platform != "win32":
        return actions

    raw = {
        "TMP": os.environ.get("TMP", ""),
        "TEMP": os.environ.get("TEMP", ""),
        "GetTempPathW": _get_temp_path_w(),
    }

    bad = [name for name, v in raw.items() if not v or not os.path.isdir(v) or not is_writable(v)]
    if not bad:
        # 全部正常：什么都不搬。刻意【不】把 %TEMP% 改指到用户目录——
        # 健康的环境保持原样，避免无谓地改变行为（外部依赖 %TEMP% 的程序
        # 也会跟着我们的进程环境走，能不动就不动）。
        #
        # 但有一处残留要清：tempfile 可能已经把 gettempdir() 的结果缓存进
        # tempfile.tempdir，而那个目录此刻已不可用（运行期被删、权限变了）。
        # 这时 os.environ 是对的、tempfile 却是错的，症状很隐蔽。
        try:
            cached = getattr(tempfile, "tempdir", None)
            if cached and not is_writable(cached):
                tempfile.tempdir = None  # 置空 → 下次 gettempdir() 重新解析
                actions.append(
                    f"tempfile.tempdir 缓存指向不可写的 {cached!r} → 已清空重新解析"
                )
        except Exception:  # noqa: BLE001
            pass
        _ACTIONS = actions
        return actions

    # 找一个确定可写的位置放新的 temp
    target = None
    for sub in ("temp", ""):
        target = resolve(sub)
        if target:
            break
    if not target:
        actions.append(
            "TEMP/TMP 不可写，且找不到任何可写目录（"
            + "; ".join(f"{k}={v!r}" for k, v in raw.items())
            + "）——无法自动修复"
        )
        _ACTIONS = actions
        return actions

    for name, v in raw.items():
        if name in bad:
            actions.append(f"{name}={v!r} 不可用 → 改指向 {target}")

    # 写回环境变量（本进程 + 后续子进程都会继承）
    os.environ["TMP"] = target
    os.environ["TEMP"] = target
    os.environ["TMPDIR"] = target

    # 关键：清掉 tempfile 的缓存，否则 gettempdir() 仍旧返回旧值
    try:
        tempfile.tempdir = target
    except Exception:  # noqa: BLE001
        pass

    _ACTIONS = actions
    return actions


def last_actions() -> list[str]:
    return list(_ACTIONS)


# --------------------------------------------------------------------------- #
# C. 日志
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    """尽力往 angel_debug.log 追加一行；失败就静默（日志不是必需品）。"""
    try:
        d = log_dir()
        os.makedirs(d, exist_ok=True)
        line = time.strftime("[%Y-%m-%d %H:%M:%S") + ".000] [scratch] " + msg + "\n"
        with open(os.path.join(d, "angel_debug.log"), "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:  # noqa: BLE001
        pass


def bootstrap(verbose: bool = True) -> dict:
    """启动早期调用一次：修 TEMP/TMP + 记录整条解析链。返回诊断字典。

    设计成幂等且绝不抛异常——它自己在启动路径上，崩了比不修更糟。
    """
    info: dict = {
        "app_dir": app_dir(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "temp_before": {
            "TMP": os.environ.get("TMP", ""),
            "TEMP": os.environ.get("TEMP", ""),
            "GetTempPathW": _get_temp_path_w(),
        },
        "repaired": [],
        "scratch": None,
        "log_dir": None,
        "candidates": [],
    }
    try:
        info["repaired"] = repair_temp_env()
        info["scratch"] = resolve()
        info["log_dir"] = log_dir()
        info["candidates"] = candidates()
        if verbose:
            for a in info["repaired"]:
                log("TEMP/TMP 修复: " + a)
            log(f"app_dir={info['app_dir']} frozen={info['frozen']}")
            log(f"scratch={info['scratch']}")
            log(f"log_dir={info['log_dir']}")
            log("候选目录顺序: " + " | ".join(info["candidates"]))
            log(
                "temp 现状: TMP=%s TEMP=%s GetTempPathW=%s"
                % (
                    os.environ.get("TMP", ""),
                    os.environ.get("TEMP", ""),
                    info["temp_before"]["GetTempPathW"],
                )
            )
    except Exception as exc:  # noqa: BLE001
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


if __name__ == "__main__":
    import json

    print(json.dumps(bootstrap(), ensure_ascii=False, indent=2))
