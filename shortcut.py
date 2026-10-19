"""shortcut.py — Windows 快捷方式（.lnk）解析 (v2.9)

用途：把桌面快捷方式拖进启动器时，直接拿到「真正的目标 exe + 启动参数 + 工作目录」。
很多游戏（如《鸣潮》）本体藏在 `...\\Client\\Binaries\\Win64\\Client-Win64-Shipping.exe`，
桌面上只有启动器发的 .lnk，靠用户手翻目录很容易填错——这一步替用户做完。

解析策略（两级，不引入第三方依赖）：
  1. 纯 Python 按 MS-SHLLINK 规范直接读二进制（快，无子进程）。绝大多数 .lnk 都能覆盖。
  2. 二进制里只拿到 IDList（没有 LinkInfo 的本地路径）时，退回调用 WScript.Shell COM。
     走 PowerShell 而不是 pywin32，是因为项目其它模块都没依赖 pywin32，
     不能为了读一个快捷方式就让打包体积和安装要求多一项。
"""
from __future__ import annotations

import os
import struct
import subprocess
from typing import Dict, Optional

# ---- LinkFlags 位 -------------------------------------------------------- #
_HAS_LINK_TARGET_IDLIST = 0x00000001
_HAS_LINK_INFO = 0x00000002
_HAS_NAME = 0x00000004
_HAS_RELATIVE_PATH = 0x00000008
_HAS_WORKING_DIR = 0x00000010
_HAS_ARGUMENTS = 0x00000020
_HAS_ICON_LOCATION = 0x00000040
_IS_UNICODE = 0x00000080

_LNK_MAGIC = 0x0000004C
_CREATE_NO_WINDOW = 0x08000000


def is_shortcut(path: str) -> bool:
    return bool(path) and path.lower().endswith(".lnk")


def _read_cstring(data: bytes, offset: int, encoding: str = "mbcs") -> str:
    """读 null 结尾字符串；offset 为 0 或越界时返回空串。"""
    if offset <= 0 or offset >= len(data):
        return ""
    end = data.find(b"\x00", offset)
    raw = data[offset:] if end < 0 else data[offset:end]
    try:
        return raw.decode(encoding, errors="replace")
    except LookupError:  # 极少数环境没有 mbcs
        return raw.decode("latin-1", errors="replace")


def _read_unicode_cstring(data: bytes, offset: int) -> str:
    if offset <= 0 or offset >= len(data):
        return ""
    end = offset
    while end + 1 < len(data) and data[end:end + 2] != b"\x00\x00":
        end += 2
    return data[offset:end].decode("utf-16-le", errors="replace")


def _parse_binary(path: str) -> Dict[str, str]:
    """按 MS-SHLLINK 规范解析。失败时返回空 dict（由上层决定是否退化）。"""
    out: Dict[str, str] = {}
    with open(path, "rb") as f:
        data = f.read()
    if len(data) < 0x4C:
        return out
    header_size = struct.unpack_from("<I", data, 0x00)[0]
    if header_size != _LNK_MAGIC:
        return out
    flags = struct.unpack_from("<I", data, 0x14)[0]
    pos = 0x4C

    # 1) LinkTargetIDList：只用来定位下一个结构的位置
    if flags & _HAS_LINK_TARGET_IDLIST:
        if pos + 2 > len(data):
            return out
        idlist_size = struct.unpack_from("<H", data, pos)[0]
        pos += 2 + idlist_size

    # 2) LinkInfo：本地路径在这里（ANSI 与 Unicode 两套偏移，优先 Unicode）
    base_path = ""
    suffix = ""
    if flags & _HAS_LINK_INFO and pos + 0x1C <= len(data):
        info_start = pos
        info_size, info_header_size = struct.unpack_from("<II", data, info_start)
        if info_size <= 0 or info_start + info_size > len(data):
            return out
        (link_info_flags,) = struct.unpack_from("<I", data, info_start + 0x08)
        if link_info_flags & 0x1:
            local_ansi, local_uni = struct.unpack_from("<II", data, info_start + 0x10)
            uni_base = suffix_base = 0
            if info_header_size >= 0x24 and info_start + 0x24 <= len(data):
                uni_base, suffix_uni = struct.unpack_from("<II", data, info_start + 0x1C)
                if suffix_uni:
                    suffix_base = suffix_uni
            if uni_base:
                base_path = _read_unicode_cstring(data, info_start + uni_base)
                if suffix_base:
                    suffix = _read_unicode_cstring(data, info_start + suffix_base)
            if not base_path and local_uni:
                base_path = _read_unicode_cstring(data, info_start + local_uni)
                if suffix_base:
                    suffix = _read_unicode_cstring(data, info_start + suffix_base)
            if not base_path:
                base_path = _read_cstring(data, info_start + local_ansi)
                (suffix_off,) = struct.unpack_from("<I", data, info_start + 0x18)
                if suffix_off:
                    suffix = _read_cstring(data, info_start + suffix_off)
        pos = info_start + info_size

    # 3) StringData：NAME / RELATIVE_PATH / WORKDIR / ARGUMENTS / ICON，按 flag 顺序依次排列
    strings = {}
    if flags & _IS_UNICODE:
        decode = lambda off, n: data[off:off + n * 2].decode("utf-16-le", errors="replace")  # noqa: E731
        unit = 2
    else:
        decode = lambda off, n: data[off:off + n].decode("mbcs", errors="replace")  # noqa: E731
        unit = 1
    for bit in (_HAS_NAME, _HAS_RELATIVE_PATH, _HAS_WORKING_DIR, _HAS_ARGUMENTS, _HAS_ICON_LOCATION):
        if flags & bit:
            if pos + 2 > len(data):
                break
            count = struct.unpack_from("<H", data, pos)[0]
            pos += 2
            end = pos + count * unit
            if end > len(data):
                break
            key = {_HAS_NAME: "name", _HAS_RELATIVE_PATH: "relative",
                   _HAS_WORKING_DIR: "workdir", _HAS_ARGUMENTS: "args",
                   _HAS_ICON_LOCATION: "icon"}[bit]
            strings[key] = decode(pos, count)
            pos = end

    target = base_path
    if suffix and target and not target.endswith(("\\", "/")):
        target = target + "\\" + suffix
    elif suffix and not target:
        target = suffix
    if target:
        out["target"] = target
    out["args"] = strings.get("args", "")
    out["workdir"] = strings.get("workdir", "")
    out["name"] = strings.get("name", "")
    return out


def _parse_with_powershell(path: str) -> Dict[str, str]:
    """退化方案：借 WScript.Shell COM 读快捷方式（无 pywin32 依赖）。"""
    script = (
        "$ErrorActionPreference='Stop';"
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:DSH_LNK_PATH);"
        "$o=[Console]::OutputEncoding;"
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
        "Write-Output ('TARGET='+$s.TargetPath);"
        "Write-Output ('ARGS='+$s.Arguments);"
        "Write-Output ('CWD='+$s.WorkingDirectory)"
    )
    env = dict(os.environ, DSH_LNK_PATH=path)
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", script],
            capture_output=True, timeout=15, env=env,
            creationflags=_CREATE_NO_WINDOW,
        )
    except Exception:  # noqa: BLE001
        return {}
    text = (proc.stdout or b"").decode("utf-8", errors="replace")
    out: Dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("TARGET="):
            out["target"] = line[len("TARGET="):].strip()
        elif line.startswith("ARGS="):
            out["args"] = line[len("ARGS="):].strip()
        elif line.startswith("CWD="):
            out["workdir"] = line[len("CWD="):].strip()
    return out


def read_shortcut(path: str) -> Dict[str, object]:
    """解析 .lnk，返回统一结构（永不抛异常，失败时 ok=False + error 说明）。

    {
      "ok": bool, "target": str, "args": str, "workdir": str,
      "name": str, "error": str
    }
    """
    result: Dict[str, object] = {"ok": False, "target": "", "args": "", "workdir": "",
                                 "name": "", "error": ""}
    if not path or not os.path.isfile(path):
        result["error"] = "快捷方式文件不存在"
        return result
    if not is_shortcut(path):
        result["error"] = "不是 .lnk 快捷方式"
        return result
    try:
        info = _parse_binary(path)
    except Exception:  # noqa: BLE001
        info = {}
    if not info.get("target"):
        try:
            info = _parse_with_powershell(path) or info
        except Exception:  # noqa: BLE001
            pass
    target = str(info.get("target") or "").strip().strip('"')
    if not target:
        result["error"] = "无法从快捷方式中读出目标路径"
        return result
    # 有些 .lnk 的目标是 dll/exe 的短路径或带环境变量，尽量还原成绝对路径
    target = os.path.expandvars(target)
    workdir = str(info.get("workdir") or "").strip().strip('"')
    if not workdir or not os.path.isdir(workdir):
        workdir = os.path.dirname(target)
    result.update({
        "ok": True,
        "target": target,
        "args": str(info.get("args") or "").strip(),
        "workdir": workdir,
        "name": suggest_name(path, target),
        "error": "",
    })
    return result


def suggest_name(lnk_path: str, target: str) -> str:
    """猜游戏名：优先用快捷方式文件名（桌面上通常就是游戏名），去掉「- 快捷方式」尾巴。"""
    stem = os.path.splitext(os.path.basename(lnk_path))[0]
    for tail in (" - 快捷方式", " - Shortcut", " - 副本"):
        if stem.endswith(tail):
            stem = stem[: -len(tail)]
            break
    if stem.strip():
        return stem.strip()
    return os.path.splitext(os.path.basename(target))[0]
