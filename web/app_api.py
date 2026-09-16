"""web/app_api.py — pywebview 后端 API (js_api)

暴露给前端 ``window.pywebview.api`` 调用的方法，封装 database / launcher /
icon_extractor。所有数据库写操作由 ``self._lock`` 串行化，保证多线程（pywebview
的 JS bridge 在独立线程执行）下 SQLite 连接安全。
"""
from __future__ import annotations

import base64
import datetime
import functools
import os
import threading
import traceback

import webview

import config as cfg
import database
import launcher
import icon_extractor


def debug_log(msg: str) -> None:
    """追加一行到 %APPDATA%/ANGEL GAME/angel_debug.log（与旧版同路径、同格式）。"""
    try:
        d = cfg._user_data_dir()
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "angel_debug.log")
        now = datetime.datetime.now()
        ts = now.strftime("%H:%M:%S.") + f"{now.microsecond // 1000:03d}"
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:  # noqa: BLE001
        pass


def log_api_errors(func):
    """装饰器：把 API 方法异常写进 debug 日志，再原样抛出（前端已有 toast 兜底）。"""
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        try:
            return func(self, *args, **kwargs)
        except Exception as e:  # noqa: BLE001
            debug_log(f"[API ERROR] {func.__name__}: {e}\n{traceback.format_exc()}")
            raise
    return wrapper


def _b64_data_url(path: str | None) -> str | None:
    """把本地 png 文件转成 data url；失败返回 None。"""
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as f:
            b = base64.b64encode(f.read()).decode("ascii")
        return f"data:image/png;base64,{b}"
    except Exception:  # noqa: BLE001
        return None


def _decode_cover(cover: str) -> bytes | None:
    """把前端传回的封面（data url 或本地路径）解码成二进制 png。

    文件选择返回的可能是绝对路径 / file:// 路径，需要按文件读取。
    """
    if not cover:
        return None
    if cover.startswith("data:image"):
        try:
            _, b64 = cover.split(",", 1)
            return base64.b64decode(b64)
        except Exception:  # noqa: BLE001
            return None
    p = cover
    if p.startswith("file:///"):
        p = p[len("file:///"):]
    elif p.startswith("file://"):
        p = p[len("file://"):]
    p = p.replace("/", os.sep)
    if os.path.isfile(p):
        try:
            with open(p, "rb") as f:
                return f.read()
        except Exception:  # noqa: BLE001
            return None
    return None


class Api:
    def __init__(self) -> None:
        self.db = database.connect()
        self._cfg = cfg.ensure_config()
        self.icon_dir = cfg.ICON_DIR
        icon_extractor.ensure_default_icon(cfg.DEFAULT_ICON)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #
    def _cover(self, g: dict, lazy: bool = True) -> str | None:
        """优先自定义封面，其次缓存的 exe 图标；都没有时：

        - lazy=True（首屏 load 用）：返回 None，不阻塞启动，交给前端 cover_for 异步提取；
        - lazy=False（cover_for 用）：当场提取并缓存到 icon_path，返回 data url。
        """
        for key in ("cover_path", "icon_path"):
            url = _b64_data_url(g.get(key))
            if url:
                return url
        if lazy:
            return None
        exe = g.get("direct_exe_path") or g.get("exe_path")
        if exe and os.path.isfile(exe):
            p = icon_extractor.extract_icon(exe, self.icon_dir, g["id"])
            if p:
                try:
                    self.db.update_game(g["id"], icon_path=p)
                except Exception:  # noqa: BLE001
                    pass
                return _b64_data_url(p)
        return None

    def _serialize_game(self, g: dict, lazy_cover: bool = True) -> dict:
        return {
            "id": g["id"],
            "name": g["name"],
            "cover": self._cover(g, lazy=lazy_cover),
            "launched_today": bool(g.get("launched_today")),
            "tags": [t for t in (g.get("tags") or "").split(",") if t.strip()],
            "date": g.get("last_launched") or g.get("created_at") or "",
            "exe_path": g.get("exe_path"),
            "direct_exe_path": g.get("direct_exe_path"),
            "launcher_path": g.get("launcher_path"),
            "last_update_check": g.get("last_update_check"),
            "update_remind_days": g.get("update_remind_days"),
            "overdue": self.db.is_update_overdue(g["id"]) if g.get("id") else False,
        }

    def _launched_today(self, game_id: int) -> bool:
        row = self.db.conn.execute(
            "SELECT CASE WHEN EXISTS("
            "SELECT 1 FROM launch_logs WHERE game_id=? "
            "AND date(launched_at)=date('now','localtime')) THEN 1 ELSE 0 END AS t",
            (game_id,),
        ).fetchone()
        return bool(row["t"]) if row else False

    def _set_top(self, on: bool) -> None:
        """用 Windows API 设置窗口置顶（仅 Windows，失败静默）。"""
        try:
            import ctypes
            import win32gui
            hwnd = win32gui.FindWindow(None, "ANGEL GAME")
            if not hwnd:
                return
            HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
            user32 = ctypes.windll.user32
            user32.SetWindowPos(
                hwnd,
                HWND_TOPMOST if on else HWND_NOTOPMOST,
                0, 0, 0, 0,
                0x0001 | 0x0002,  # SWP_NOSIZE | SWP_NOMOVE
            )
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ #
    # 暴露给前端的 API
    # ------------------------------------------------------------------ #
    @log_api_errors
    def load(self) -> dict:
        """初始加载：游戏列表 + 今日进度 + 设置。

        封面采用 lazy 模式（lazy_cover=True）：不在此处同步提取 exe 图标，
        未缓存封面的游戏先返回 None（前端显示占位），由 cover_for 在首屏渲染后
        异步逐个提取。这样 load 不阻塞主线程，UI 立即出现，大幅加快启动/加载。
        """
        with self._lock:
            rows = self.db.get_active_games_with_today()
            games = [self._serialize_game(dict(r), lazy_cover=True) for r in rows]
            today = self.db.get_today_stats()
            settings = self._cfg
            update_needed = len(self.db.get_games_needing_update_check())
        return {
            "games": games,
            "today": {"launched": today[0], "total": today[1]},
            "update_needed_count": update_needed,
            "settings": {
                "theme": settings["ui"]["theme"],
                "grid_columns": settings["ui"]["grid_columns"],
                "always_on_top": settings["window_behavior"]["always_on_top"],
            },
        }

    @log_api_errors
    def launch(self, game_id: int) -> dict:
        """启动一个游戏并记录，返回最新状态。"""
        with self._lock:
            g = self.db.get_game(int(game_id))
            if not g:
                return {"ok": False, "detail": "游戏不存在"}
            ok, detail = launcher.launch_game(dict(g), self.db)
            launched = self._launched_today(int(game_id))
            today = self.db.get_today_stats()
        return {
            "ok": ok,
            "detail": detail,
            "game_id": int(game_id),
            "launched_today": launched,
            "today": {"launched": today[0], "total": today[1]},
        }

    @log_api_errors
    def check_update(self, game_id: int) -> dict:
        """通过启动器检查该游戏更新：启动启动器（launcher 模式）并标记已检查。

        仅记录 last_update_check，不写游玩日志（launcher.launch_game(log=False)），
        避免「检查更新」污染今日进度 / 热力图。无有效启动器时返回 need_launcher，
        引导前端提示用户去编辑补充启动器路径。
        """
        with self._lock:
            g = self.db.get_game(int(game_id))
            if not g:
                return {"ok": False, "detail": "游戏不存在"}
            lp = g.get("launcher_path")
            if not lp or not os.path.isfile(lp):
                return {
                    "ok": False,
                    "detail": "未配置有效的启动器路径，请编辑补充",
                    "need_launcher": True,
                }
            ok, detail = launcher.launch_game(dict(g), self.db, mode="launcher", log=False)
            self.db.update_last_update_check(int(game_id))
        return {
            "ok": True,
            "detail": detail,
            "game_id": int(game_id),
            "launched": ok,
        }

    @log_api_errors
    def cover_for(self, game_id: int) -> dict:
        """懒加载单个游戏封面（首屏渲染后由前端异步调用）。

        优先返回已缓存的封面/图标 data url；未缓存则从 exe 提取并写入 icon_path
        缓存，下次 load 直接走缓存、不重复提取。返回 {id, cover}。
        """
        gid = int(game_id)
        with self._lock:
            g = self.db.get_game(gid)
            if not g:
                return {"id": gid, "cover": None}
            cached = _b64_data_url(g.get("cover_path")) or _b64_data_url(g.get("icon_path"))
            if cached:
                return {"id": gid, "cover": cached}
            exe = g.get("direct_exe_path") or g.get("exe_path")
            if exe and os.path.isfile(exe):
                p = icon_extractor.extract_icon(exe, self.icon_dir, gid)
                if p:
                    try:
                        self.db.update_game(gid, icon_path=p)
                    except Exception:  # noqa: BLE001
                        pass
                    return {"id": gid, "cover": _b64_data_url(p)}
        return {"id": gid, "cover": None}

    @log_api_errors
    def add_game(self, payload: dict) -> dict:
        """添加一个游戏。payload: {name, exe, body?, launcher?, tags?, cover?}。"""
        name = (payload.get("name") or "").strip()
        exe = (payload.get("exe") or "").strip()
        body = (payload.get("body") or "").strip() or None
        launcher = (payload.get("launcher") or "").strip() or None
        tags = payload.get("tags") or ""
        cover = payload.get("cover")
        if not exe or not name:
            return {"ok": False, "error": "请填写游戏名称和启动程序路径"}
        if not os.path.isfile(exe):
            return {"ok": False, "error": f"文件不存在: {exe}"}
        if launcher and not os.path.isfile(launcher):
            return {"ok": False, "error": f"启动器文件不存在: {launcher}"}
        with self._lock:
            gid = self.db.add_game(
                name=name,
                exe_path=exe,
                direct_exe_path=body or exe,
                launcher_path=launcher,
                tags=tags,
            )
            if not gid:
                return {"ok": False, "error": "添加失败（路径可能已存在）"}
            # 自定义封面（前端选的图片，data url 或本地路径）
            if cover and isinstance(cover, str):
                raw = _decode_cover(cover)
                if raw:
                    cpath = os.path.join(self.icon_dir, f"{gid}_cover.png")
                    try:
                        with open(cpath, "wb") as f:
                            f.write(raw)
                        self.db.update_game(gid, cover_path=cpath)
                    except Exception:  # noqa: BLE001
                        pass
            # 自动提取 exe 图标作为封面
            icon = icon_extractor.extract_icon(exe, self.icon_dir, gid)
            if icon:
                self.db.update_game(gid, icon_path=icon)
        return {"ok": True, "id": gid}

    @log_api_errors
    def edit_game(self, payload: dict) -> dict:
        """编辑已有游戏。payload: {id, name?, exe?, body?, tags?, cover?}。"""
        try:
            gid = int(payload.get("id"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "无效游戏 id"}
        fields: dict = {}
        if payload.get("name"):
            fields["name"] = payload["name"].strip()
        if payload.get("exe"):
            fields["exe_path"] = payload["exe"].strip()
        if "body" in payload:
            fields["direct_exe_path"] = payload.get("body") or payload.get("exe")
        if "tags" in payload:
            fields["tags"] = payload["tags"]
        if "launcher" in payload:
            lp = (payload.get("launcher") or "").strip()
            if lp and not os.path.isfile(lp):
                return {"ok": False, "error": f"启动器文件不存在: {lp}"}
            fields["launcher_path"] = lp or None
        if payload.get("cover") and isinstance(payload["cover"], str):
            raw = _decode_cover(payload["cover"])
            if raw:
                cpath = os.path.join(self.icon_dir, f"{gid}_cover.png")
                try:
                    with open(cpath, "wb") as f:
                        f.write(raw)
                    fields["cover_path"] = cpath
                except Exception:  # noqa: BLE001
                    pass
        with self._lock:
            self.db.update_game(gid, **fields)
        return {"ok": True, "id": gid}

    @log_api_errors
    def hide_games(self, ids: list) -> dict:
        """批量隐藏游戏（is_hidden=1）。原型文案「已隐藏」。"""
        count = 0
        with self._lock:
            for i in ids:
                try:
                    if self.db.set_hidden(int(i), 1):
                        count += 1
                except Exception:  # noqa: BLE001
                    pass
        return {"ok": True, "count": count}

    @log_api_errors
    def delete_game(self, game_id: int) -> dict:
        """彻底删除一个游戏（含启动历史，外键级联）。不可恢复，调用方需二次确认。"""
        with self._lock:
            ok = self.db.delete_game(int(game_id))
        if ok:
            # 顺手清掉本地图标/封面缓存
            try:
                for ext in ("icon", "cover"):
                    p = os.path.join(self.icon_dir, f"{int(game_id)}_{ext}.png")
                    if os.path.isfile(p):
                        os.remove(p)
            except Exception:  # noqa: BLE001
                pass
        return {"ok": bool(ok), "id": int(game_id)}

    @log_api_errors
    def open_folder(self, game_id: int) -> dict:
        """在资源管理器打开游戏 exe 所在目录（仅 Windows）。"""
        try:
            g = self.db.get_game(int(game_id))
            if g and g.get("exe_path"):
                d = os.path.dirname(g["exe_path"])
                if os.path.isdir(d):
                    os.startfile(d)  # type: ignore[attr-defined]
                    return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
        return {"ok": False, "error": "无法打开目录"}

    @log_api_errors
    def stats(self) -> dict:
        """连续游玩 / 总览统计。"""
        with self._lock:
            return self.db.get_launch_stats()

    @log_api_errors
    def month_data(self, year: int, month: int) -> dict:
        """指定年月的日历聚合（计数 + 每日明细）。"""
        with self._lock:
            counts, details = self.db.get_month_data(int(year), int(month))
        # 明细里的 tags 是逗号串，前端展开成列表
        norm_details = {}
        for d, items in details.items():
            norm_details[d] = [
                {**it, "tags": [t for t in (it.get("tags") or "").split(",") if t.strip()]}
                for it in items
            ]
        return {"counts": counts, "details": norm_details}

    @log_api_errors
    def pick_file(self, kind: str) -> str | None:
        """打开系统文件选择框，返回选中路径。kind: 'exe' | 'img'。"""
        try:
            w = webview.windows[0]
        except Exception:  # noqa: BLE001
            return None
        try:
            if kind == "img":
                files = w.create_file_dialog(
                    webview.OPEN_DIALOG, allow_multiple=False,
                    file_types=("图片 (*.png;*.jpg;*.jpeg)",),
                )
            else:
                files = w.create_file_dialog(
                    webview.OPEN_DIALOG, allow_multiple=False,
                    file_types=("可执行文件 (*.exe)",),
                )
        except Exception:  # noqa: BLE001
            return None
        if files:
            return files[0]
        return None

    @log_api_errors
    def get_settings(self) -> dict:
        s = self._cfg
        return {
            "theme": s["ui"]["theme"],
            "grid_columns": s["ui"]["grid_columns"],
            "always_on_top": s["window_behavior"]["always_on_top"],
        }

    @log_api_errors
    def save_settings(self, payload: dict) -> dict:
        s = cfg.ensure_config()
        if payload.get("theme"):
            s["ui"]["theme"] = payload["theme"]
        if payload.get("grid_columns") is not None:
            try:
                s["ui"]["grid_columns"] = max(2, min(10, int(payload["grid_columns"])))
            except (TypeError, ValueError):
                pass
        if payload.get("always_on_top") is not None:
            s["window_behavior"]["always_on_top"] = bool(payload["always_on_top"])
            self._set_top(bool(payload["always_on_top"]))
        cfg.save_config(s)
        return self.get_settings()
