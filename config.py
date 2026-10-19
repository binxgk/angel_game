"""config.py — 配置文件读写 (P0)

负责 config.json 的加载、保存与默认重建。配置缺失或损坏时回退到内置默认配置，
并补全缺失字段后写回，保证后续模块拿到的始终是结构完整的配置。
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

# 路径解析：
#  - 开发/普通运行：脚本所在目录（angel_game/），用户数据也放在这里。
#  - PyInstaller 单文件打包后（frozen）：程序在临时目录 _MEIxxxx 解包运行，
#    sys.executable 指向临时路径、退出/重启会被清空，不能把用户数据放在 exe 旁。
#    因此打包后：用户数据（config.json / 数据库 / 图标）落到稳定的
#    %APPDATA%/ANGEL GAME/；只读资源模板优先取自打包目录(_MEIPASS)，缺失则用代码生成。
def _is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def _bundle_dir() -> str:
    """只读资源所在目录：打包后在 _MEIPASS，未打包时为脚本目录。"""
    mp = getattr(sys, "_MEIPASS", None)
    if mp:
        return mp
    return os.path.dirname(os.path.abspath(__file__))


def _user_data_dir() -> str:
    """稳定的用户数据目录（跨运行持久化），首次调用即创建好子目录。"""
    base = (
        os.environ.get("APPDATA")
        or os.environ.get("LOCALAPPDATA")
        or os.path.expanduser("~")
    )
    d = os.path.join(base, "ANGEL GAME")
    os.makedirs(os.path.join(d, "data", "icons"), exist_ok=True)
    os.makedirs(os.path.join(d, "assets"), exist_ok=True)
    return d


if _is_frozen():
    _DATA = _user_data_dir()
    _BUNDLE = _bundle_dir()
else:
    _HERE = os.path.dirname(os.path.abspath(__file__))
    _DATA = _HERE
    _BUNDLE = _HERE

CONFIG_PATH = os.path.join(_DATA, "config.json")


DB_PATH = os.path.join(_DATA, "data", "angel_game.db")
ICON_DIR = os.path.join(_DATA, "data", "icons")
ASSETS_DIR = os.path.join(_DATA, "assets")
WINDOW_GEOMETRY_PATH = os.path.join(_DATA, "window_geometry.json")

# 默认占位图标：优先用打包内的模板；缺失时由 icon_extractor 在可写目录生成。
_TPL = os.path.join(_BUNDLE, "assets", "default_icon.png")
DEFAULT_ICON = _TPL if os.path.isfile(_TPL) else os.path.join(ASSETS_DIR, "default_icon.png")

DEFAULT_CONFIG: dict[str, Any] = {
    "update_check": {
        "default_remind_days": 3,
        "default_launch_mode": "direct",
    },
    "window_behavior": {
        "always_on_top": False,
    },
    "ui": {
        "theme": "dark",
        "grid_columns": 5,
        "tile_width": 180,
        "tile_height": 220,
    },
}


def get_default_config() -> dict[str, Any]:
    """返回默认配置的深拷贝，避免外部修改污染常量。"""
    return json.loads(json.dumps(DEFAULT_CONFIG))


def _deep_merge(base: dict, override: dict) -> dict:
    """将 override 合并进 base 的副本，仅补全缺失键，不删除已有键。"""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: str = CONFIG_PATH) -> dict[str, Any]:
    """读取配置；缺失或无法解析时返回默认配置（不写文件）。"""
    if not os.path.exists(path):
        return get_default_config()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("config 根节点不是对象")
        return _deep_merge(get_default_config(), data)
    except (json.JSONDecodeError, ValueError, OSError) as e:
        print(f"[config] 配置文件损坏，使用默认配置: {e}")
        return get_default_config()


def save_config(config: dict[str, Any], path: str = CONFIG_PATH) -> bool:
    """保存配置到文件；成功返回 True。"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        return True
    except OSError as e:
        print(f"[config] 保存配置失败: {e}")
        return False


def ensure_config(path: str = CONFIG_PATH) -> dict[str, Any]:
    """确保配置文件存在且结构完整：缺失则写默认，有缺项则补全并写回。"""
    if not os.path.exists(path):
        save_config(get_default_config(), path)
        return get_default_config()
    cfg = load_config(path)
    merged = _deep_merge(get_default_config(), cfg)
    if merged != cfg:
        save_config(merged, path)
    return merged


def get_db_path() -> str:
    return DB_PATH


def get_icon_dir() -> str:
    return ICON_DIR


def load_window_geometry() -> dict[str, Any] | None:
    """读取上次保存的窗口几何（width/height/x/y/maximized）；缺失或无效返回 None。"""
    try:
        if not os.path.exists(WINDOW_GEOMETRY_PATH):
            return None
        with open(WINDOW_GEOMETRY_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            return None
        # 至少要有合理的宽高才认为是有效记录
        if not isinstance(d.get("width"), int) or not isinstance(d.get("height"), int):
            return None
        if d["width"] < 200 or d["height"] < 150:
            return None
        return d
    except (json.JSONDecodeError, ValueError, OSError):
        return None


def save_window_geometry(geom: dict[str, Any]) -> None:
    """保存窗口几何到独立文件（与 config.json 分离，避免频繁写入触发深合并）。"""
    try:
        os.makedirs(os.path.dirname(WINDOW_GEOMETRY_PATH), exist_ok=True)
        with open(WINDOW_GEOMETRY_PATH, "w", encoding="utf-8") as f:
            json.dump(geom, f, ensure_ascii=False)
    except OSError:
        pass


if __name__ == "__main__":
    print(json.dumps(ensure_config(), ensure_ascii=False, indent=2))
