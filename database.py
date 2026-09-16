"""database.py — SQLite 数据库层 (P0)

建表（games / launch_logs）与增删改查，封装规划文档中的关键查询。
所有时间统一用 datetime('now', 'localtime') 基于用户本地时间。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
from datetime import date, datetime
from typing import Any, Optional

import config as cfg


class GameDB:
    """轻量数据库封装，连接按需建立，支持测试时指定路径。"""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or cfg.get_db_path()
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        # check_same_thread=False：pywebview 把每个 JS API 调用放进独立后台线程执行，
        # 而连接在主线程（Api.__init__）创建。关闭线程检查 + 配合 Api 的 self._lock
        # 串行化访问，既安全又避免 "SQLite objects created in a thread..." 报错。
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA foreign_keys = ON")
        # WAL 模式：允许旁路 hook 线程（独立连接）写库的同时，主线程正常读取网格，
        # 避免 SQLite “多连接并发” 在低并发写场景下的锁竞争。
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            pass
        # 读多写少场景：synchronous=NORMAL 在 WAL 下已保证崩溃安全，但大幅降低写盘同步开销；
        # cache_size=-8000 ≈ 8MB 页缓存，加速大量 launch_logs 的聚合查询与首屏载入。
        try:
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("PRAGMA cache_size=-8000")
        except sqlite3.Error:
            pass
        self.conn.row_factory = sqlite3.Row
        self._create_tables()
        self._ensure_columns()

    # ------------------------------------------------------------------ #
    # 建表
    # ------------------------------------------------------------------ #
    def _create_tables(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS games (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                exe_path TEXT NOT NULL UNIQUE,
                direct_exe_path TEXT,
                launcher_path TEXT,
                icon_path TEXT,
                last_launched TIMESTAMP,
                last_update_check TIMESTAMP,
                update_remind_days INTEGER DEFAULT 3,
                default_launch_mode TEXT DEFAULT 'direct',
                is_hidden INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS launch_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id INTEGER NOT NULL,
                launched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                launch_mode TEXT DEFAULT 'direct',
                FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE
            );
            """
        )
        self.conn.commit()

    def _ensure_columns(self) -> None:
        """为兼容旧库补充缺失列（tags / cover_path / 更新检查相关列）。"""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(games)").fetchall()}
        if "tags" not in cols:
            self.conn.execute("ALTER TABLE games ADD COLUMN tags TEXT NOT NULL DEFAULT ''")
            self.conn.commit()
        if "cover_path" not in cols:
            self.conn.execute("ALTER TABLE games ADD COLUMN cover_path TEXT")
            self.conn.commit()
        # 更新检查相关列（兼容早期无这些列的旧库，避免查询报 no such column）
        for col, ddl in (
            ("launcher_path", "ALTER TABLE games ADD COLUMN launcher_path TEXT"),
            ("last_update_check", "ALTER TABLE games ADD COLUMN last_update_check TIMESTAMP"),
            ("update_remind_days", "ALTER TABLE games ADD COLUMN update_remind_days INTEGER DEFAULT 3"),
            ("default_launch_mode", "ALTER TABLE games ADD COLUMN default_launch_mode TEXT DEFAULT 'direct'"),
        ):
            if col not in cols:
                try:
                    self.conn.execute(ddl)
                    self.conn.commit()
                except sqlite3.Error:
                    pass

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ #
    # 游戏库写入
    # ------------------------------------------------------------------ #
    def add_game(
        self,
        name: str,
        exe_path: str,
        direct_exe_path: Optional[str] = None,
        launcher_path: Optional[str] = None,
        icon_path: Optional[str] = None,
        cover_path: Optional[str] = None,
        update_remind_days: int = 3,
        default_launch_mode: str = "direct",
        tags: str = "",
    ) -> Optional[int]:
        """写入一个游戏；exe_path 唯一冲突时恢复显示（取消隐藏）。返回 game id 或 None。

        关键修复：之前用 ON CONFLICT(exe_path) DO NOTHING，导致"重复添加已隐藏的游戏"
        静默返回已有 id 但不取消隐藏，配合 get_active_games_with_today 的 is_hidden=0 过滤，
        表现为"添加成功但磁贴不显示"。改为冲突时 SET is_hidden=0，使"添加 = 让游戏出现在库里"
        符合直觉（无论首次添加还是重新添加被隐藏的游戏）。
        """
        try:
            cur = self.conn.execute(
                """
                INSERT INTO games
                    (name, exe_path, direct_exe_path, launcher_path, icon_path,
                     cover_path, update_remind_days, default_launch_mode, tags)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(exe_path) DO UPDATE SET is_hidden = 0
                """,
                (name, exe_path, direct_exe_path, launcher_path, icon_path, cover_path,
                 update_remind_days, default_launch_mode, tags),
            )
            self.conn.commit()
            if cur.lastrowid:
                return cur.lastrowid
            row = self.conn.execute(
                "SELECT id FROM games WHERE exe_path = ?", (exe_path,)
            ).fetchone()
            return row["id"] if row else None
        except sqlite3.Error as e:
            print(f"[db] add_game 失败: {e}")
            return None

    def update_game(self, game_id: int, **fields: Any) -> bool:
        allowed = {
            "name", "exe_path", "direct_exe_path", "launcher_path",
            "icon_path", "cover_path", "update_remind_days", "default_launch_mode",
            "is_hidden", "last_update_check", "tags",
        }
        sets, values = [], []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k} = ?")
                values.append(v)
        if not sets:
            return False
        values.append(game_id)
        try:
            self.conn.execute(
                f"UPDATE games SET {', '.join(sets)} WHERE id = ?", values
            )
            self.conn.commit()
            return True
        except sqlite3.Error as e:
            print(f"[db] update_game 失败: {e}")
            return False

    def set_hidden(self, game_id: int, hidden: int = 1) -> bool:
        return self.update_game(game_id, is_hidden=hidden)

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def get_game(self, game_id: int) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
        return dict(row) if row else None

    def get_game_by_path(self, exe_path: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM games WHERE exe_path = ?", (exe_path,)
        ).fetchone()
        return dict(row) if row else None

    def get_all_active_games(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM games WHERE is_hidden = 0 ORDER BY name"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_active_games_with_today(self, include_hidden: bool = False) -> list[dict]:
        """查询1：所有活跃游戏及今日是否已启动，按未启动优先、最近活跃排后。

        include_hidden=True 时返回含被隐藏游戏的全部（批量管理视图用）；
        普通视图保持 is_hidden=0。每行带 is_hidden 字段供 UI 区分。
        """
        rows = self.conn.execute(
            """
            SELECT
                g.id, g.name, g.exe_path, g.direct_exe_path, g.launcher_path,
                g.icon_path, g.cover_path, g.last_launched, g.last_update_check,
                g.update_remind_days, g.default_launch_mode, g.tags, g.is_hidden,
                CASE
                    WHEN EXISTS (
                        SELECT 1 FROM launch_logs
                        WHERE game_id = g.id AND date(launched_at) = date('now', 'localtime')
                    ) THEN 1 ELSE 0
                END AS launched_today,
                COALESCE(
                    (SELECT MAX(launched_at) FROM launch_logs WHERE game_id = g.id),
                    g.created_at
                ) AS last_activity
            FROM games g
            WHERE (g.is_hidden = 0 OR ?)
            ORDER BY launched_today ASC, last_activity DESC
            """,
            (include_hidden,),
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_game(self, game_id: int) -> bool:
        """彻底删除一个游戏（含其启动历史，因外键 ON DELETE CASCADE）。

        不删除用户磁盘上的 exe 文件，只清库。不可恢复，调用方需二次确认。
        """
        try:
            self.conn.execute("DELETE FROM games WHERE id = ?", (game_id,))
            self.conn.commit()
            return True
        except sqlite3.Error as e:
            print(f"[db] delete_game 失败: {e}")
            return False

    def get_today_stats(self) -> tuple[int, int]:
        """查询2：返回 (今日已启动游戏数, 活跃游戏总数)。"""
        row = self.conn.execute(
            """
            SELECT
                (SELECT COUNT(DISTINCT game_id) FROM launch_logs
                 WHERE date(launched_at) = date('now', 'localtime')) AS today_count,
                (SELECT COUNT(*) FROM games WHERE is_hidden = 0) AS total_count
            """
        ).fetchone()
        return (row["today_count"], row["total_count"]) if row else (0, 0)

    def get_games_needing_update_check(self) -> list[dict]:
        """查询3：返回需要检查更新的游戏（有启动器、未设从不、且超期）。"""
        rows = self.conn.execute(
            """
            SELECT id, name, launcher_path, last_update_check
            FROM games
            WHERE is_hidden = 0
              AND launcher_path IS NOT NULL
              AND update_remind_days != -1
              AND (last_update_check IS NULL
                   OR date(last_update_check, '+' || update_remind_days || ' days')
                      <= date('now', 'localtime'))
            """
        ).fetchall()
        return [dict(r) for r in rows]

    def is_update_overdue(self, game_id: int) -> bool:
        """查询3的布尔版：该游戏是否超期需要检查更新。"""
        row = self.conn.execute(
            """
            SELECT CASE WHEN (
                launcher_path IS NOT NULL
                AND update_remind_days != -1
                AND (last_update_check IS NULL
                     OR date(last_update_check, '+' || update_remind_days || ' days')
                        <= date('now', 'localtime'))
            ) THEN 1 ELSE 0 END AS overdue
            FROM games WHERE id = ?
            """,
            (game_id,),
        ).fetchone()
        return bool(row["overdue"]) if row else False

    def get_launch_history(self, game_id: int, limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT id, launched_at, launch_mode FROM launch_logs
            WHERE game_id = ? ORDER BY launched_at DESC LIMIT ?
            """,
            (game_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def count_active_games(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM games WHERE is_hidden = 0"
        ).fetchone()
        return row["c"] if row else 0

    # ------------------------------------------------------------------ #
    # 启动日志与状态更新
    # ------------------------------------------------------------------ #
    def log_launch(self, game_id: int, launch_mode: str = "direct") -> bool:
        """查询4：写入一条启动日志。"""
        try:
            self.conn.execute(
                "INSERT INTO launch_logs (game_id, launched_at, launch_mode) "
                "VALUES (?, datetime('now', 'localtime'), ?)",
                (game_id, launch_mode),
            )
            self.conn.commit()
            return True
        except sqlite3.Error as e:
            print(f"[db] log_launch 失败: {e}")
            return False

    def update_last_launched(self, game_id: int) -> bool:
        """查询5：更新最后启动时间。"""
        try:
            self.conn.execute(
                "UPDATE games SET last_launched = datetime('now', 'localtime') WHERE id = ?",
                (game_id,),
            )
            self.conn.commit()
            return True
        except sqlite3.Error as e:
            print(f"[db] update_last_launched 失败: {e}")
            return False

    def update_last_update_check(self, game_id: int) -> bool:
        """查询6：更新最后更新检查时间。"""
        try:
            self.conn.execute(
                "UPDATE games SET last_update_check = datetime('now', 'localtime') WHERE id = ?",
                (game_id,),
            )
            self.conn.commit()
            return True
        except sqlite3.Error as e:
            print(f"[db] update_last_update_check 失败: {e}")
            return False

    # ------------------------------------------------------------------ #
    # 演示数据
    # ------------------------------------------------------------------ #
    def seed_demo_games(self, demo_exe: str, names: list[str]) -> int:
        """演示模式：清空现有演示数据（保持简单）并注入模拟游戏。
        使用同一真实可执行文件作为启动目标，便于无游戏时验证启动流程。
        返回注入数量。"""
        demo_tags = ["动作", "RPG", "休闲", "策略"]
        count = 0
        for i, name in enumerate(names):
            gid = self.add_game(
                name=name,
                exe_path=demo_exe,
                direct_exe_path=demo_exe,
                launcher_path=None,
                icon_path=None,          # 使用默认图标
                update_remind_days=3,
                default_launch_mode="direct",
                tags=demo_tags[i % len(demo_tags)],
            )
            if gid:
                count += 1
        if count:
            self.seed_demo_launches()
        return count

    def seed_demo_launches(self) -> None:
        """演示模式：为演示游戏注入近 40 天的启动记录，便于热力图/统计可见。"""
        ids = [r["id"] for r in self.conn.execute(
            "SELECT id FROM games WHERE is_hidden = 0 ORDER BY id").fetchall()]
        if not ids:
            return
        for i, gid in enumerate(ids):
            for d in range(0, 40):
                if (d + i) % 3 == 0:
                    self.conn.execute(
                        "INSERT INTO launch_logs (game_id, launched_at, launch_mode) "
                        "VALUES (?, datetime('now', 'localtime', ?), 'direct')",
                        (gid, f"-{d} days"),
                    )
        self.conn.commit()

    # ------------------------------------------------------------------ #
    # 标签与统计聚合（P7/P8/P9）
    # ------------------------------------------------------------------ #
    def get_all_tags(self) -> list[str]:
        """返回所有游戏中出现的去重标签，按使用频次降序。"""
        rows = self.conn.execute("SELECT tags FROM games WHERE is_hidden = 0").fetchall()
        freq: dict[str, int] = {}
        for r in rows:
            for t in (r["tags"] or "").split(","):
                t = t.strip()
                if t:
                    freq[t] = freq.get(t, 0) + 1
        return sorted(freq, key=lambda k: (-freq[k], k))

    def set_game_tags(self, game_id: int, tags: list[str]) -> bool:
        """用标签列表覆盖该游戏标签（自动去空白/去重/排序）。"""
        clean = sorted({t.strip() for t in tags if t and t.strip()})
        return self.update_game(game_id, tags=",".join(clean))

    def get_launch_counts_by_game(self) -> dict[int, int]:
        """返回 {game_id: 启动次数}。"""
        rows = self.conn.execute(
            "SELECT game_id, COUNT(*) AS c FROM launch_logs GROUP BY game_id"
        ).fetchall()
        return {r["game_id"]: r["c"] for r in rows}

    def get_daily_launch_counts(self, days: int = 120) -> dict[str, int]:
        """返回近 days 天每日启动的『不同游戏数』: {YYYY-MM-DD: count}。"""
        rows = self.conn.execute(
            """
            SELECT date(launched_at) AS d, COUNT(DISTINCT game_id) AS c
            FROM launch_logs
            WHERE date(launched_at) >= date('now', 'localtime', ?)
            GROUP BY d
            """,
            (f"-{days - 1} days",),
        ).fetchall()
        return {r["d"]: r["c"] for r in rows}

    def get_daily_game_details(self, days: int = 120) -> dict[str, list[dict]]:
        """返回近 days 天每日『按游戏聚合』的启动明细。

        {YYYY-MM-DD: [{game_id, name, count, last_time, tags}, ...]}
        用于热力图点击某天就地展开当天明细（同一游戏合并成一条）。
        """
        rows = self.conn.execute(
            """
            SELECT date(launched_at) AS d, g.id AS gid, g.name AS name,
                   COUNT(*) AS c, MAX(launched_at) AS last_time, g.tags AS tags
            FROM launch_logs l JOIN games g ON g.id = l.game_id
            WHERE date(launched_at) >= date('now', 'localtime', ?)
            GROUP BY d, g.id
            ORDER BY d, c DESC, name
            """,
            (f"-{days - 1} days",),
        ).fetchall()
        out: dict[str, list[dict]] = {}
        for r in rows:
            out.setdefault(r["d"], []).append({
                "game_id": r["gid"], "name": r["name"],
                "count": r["c"], "last_time": r["last_time"],
                "tags": r["tags"] or "",
            })
        return out

    def get_month_data(self, year: int, month: int) -> tuple[dict[str, int], dict[str, list[dict]]]:
        """返回指定月份的每日游戏启动聚合（用于日历视图）。

        v2.7 修复：当查询的是「当前月份」时，始终直接查 SQLite，
        不走 history JSON 缓存。原因：JSON 是上次打开统计面板时导出的快照，
        当月数据仍在持续写入（用户不断启动游戏），JSON 会严重滞后导致日历显示空白。
        历史月份（非当前月）仍优先读 JSON 缓存以加速。
        """
        import json, os
        from datetime import date
        from config import _DATA

        _today = date.today()
        is_current_month = (year == _today.year and month == _today.month)

        # 历史月份：优先从 history JSON 加载
        if not is_current_month:
            hist_path = os.path.join(_DATA, "history", f"{year:04d}-{month:02d}.json")
            if os.path.isfile(hist_path):
                try:
                    with open(hist_path, "r", encoding="utf-8") as f:
                        raw = json.load(f)
                    daily = raw.get("daily", {})
                    counts = {d: v.get("count", 0) for d, v in daily.items()}
                    details: dict[str, list[dict]] = {}
                    for d, v in daily.items():
                        details[d] = [
                            {
                                "game_id": g.get("game_id"),
                                "name": g.get("name", ""),
                                "count": g.get("count", 0),
                                "last_time": g.get("last_time", ""),
                                "tags": g.get("tags", ""),
                            }
                            for g in v.get("games", [])
                        ]
                    return counts, details
                except Exception:
                    pass

        # 当前月份（或历史 JSON 不存在/解析失败）：直接查 SQLite
        prefix = f"{year:04d}-{month:02d}"
        rows = self.conn.execute(
            """
            SELECT date(launched_at) AS d, COUNT(DISTINCT game_id) AS c
            FROM launch_logs
            WHERE strftime('%Y-%m', launched_at) = ?
            GROUP BY d
            """,
            (prefix,),
        ).fetchall()
        counts = {r["d"]: r["c"] for r in rows}
        rows2 = self.conn.execute(
            """
            SELECT date(l.launched_at) AS d,
                   g.id AS gid, g.name AS name,
                   COUNT(*) AS c, MAX(l.launched_at) AS last_time, g.tags AS tags
            FROM launch_logs l
            JOIN games g ON g.id = l.game_id
            WHERE strftime('%Y-%m', l.launched_at) = ?
            GROUP BY d, g.id
            ORDER BY d, c DESC, name
            """,
            (prefix,),
        ).fetchall()
        details = {}
        for r in rows2:
            details.setdefault(r["d"], []).append({
                "game_id": r["gid"], "name": r["name"],
                "count": r["c"], "last_time": r["last_time"],
                "tags": r["tags"] or "",
            })
        return counts, details

    def export_month_json(self, year: int, month: int) -> str | None:
        """导出指定月份数据到 history JSON，返回写入路径；失败返回 None。"""
        try:
            counts, details = self.get_month_data(year, month)
            daily: dict[str, dict] = {}
            all_dates = set(counts) | set(details)
            for d in all_dates:
                daily[d] = {
                    "count": counts.get(d, 0),
                    "games": details.get(d, []),
                }
            from config import _DATA
            d_dir = os.path.join(_DATA, "history")
            os.makedirs(d_dir, exist_ok=True)
            path = os.path.join(d_dir, f"{year:04d}-{month:02d}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"year": year, "month": month, "daily": daily}, f,
                          ensure_ascii=False, indent=2)
            return path
        except Exception as e:
            print(f"[db] export_month_json 失败: {e}")
            return None

    def get_launch_stats(self) -> dict:
        """连续游玩统计与总览。

        current_streak：当前连续天数。规则——最近游玩日为今天或昨天则连续
        有效（今天还没玩不算断），否则为 0（已断更）。
        longest_streak：历史最长连续天数。
        """
        day_rows = self.conn.execute(
            "SELECT DISTINCT date(launched_at) AS d FROM launch_logs ORDER BY d"
        ).fetchall()
        days = sorted({datetime.strptime(r["d"], "%Y-%m-%d").date() for r in day_rows})
        total_active_days = len(days)
        total_launches = self.conn.execute(
            "SELECT COUNT(*) AS c FROM launch_logs"
        ).fetchone()["c"]
        distinct_games = self.conn.execute(
            "SELECT COUNT(DISTINCT game_id) AS c FROM launch_logs"
        ).fetchone()["c"]

        today = date.today()
        if not days:
            current = 0
        else:
            last = days[-1]
            if (today - last).days > 1:
                current = 0
            else:
                current = 1
                for i in range(len(days) - 1, 0, -1):
                    if (days[i] - days[i - 1]).days == 1:
                        current += 1
                    else:
                        break

        longest = 0
        run = 0
        prev = None
        for d in days:
            if prev is None or (d - prev).days == 1:
                run += 1
            else:
                run = 1
            longest = max(longest, run)
            prev = d

        return {
            "current_streak": current,
            "longest_streak": longest,
            "total_active_days": total_active_days,
            "total_launches": total_launches,
            "distinct_games": distinct_games,
        }

    def get_month_active_days(self, year: int, month: int) -> int:
        """返回指定年月的『活跃天数』（有启动记录的不同日期数）。"""
        row = self.conn.execute(
            "SELECT COUNT(DISTINCT date(launched_at)) AS c FROM launch_logs "
            "WHERE strftime('%Y', launched_at) = ? AND strftime('%m', launched_at) = ?",
            (f"{year:04d}", f"{month:02d}"),
        ).fetchone()
        return row["c"] if row else 0

        # ------------------------------------------------------------------ #
    # 容错：数据库损坏时备份并重建
    # ------------------------------------------------------------------ #
    def safe_rebuild(self) -> bool:
        """数据库损坏时的兜底：备份旧文件后删除并重建。返回是否成功。"""
        try:
            self.conn.close()
            backup = self.db_path + ".bak"
            if os.path.exists(self.db_path):
                shutil.move(self.db_path, backup)
            self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self.conn.execute("PRAGMA foreign_keys = ON")
            self.conn.row_factory = sqlite3.Row
            self._create_tables()
            print(f"[db] 数据库已备份至 {backup} 并重建")
            return True
        except (sqlite3.Error, OSError) as e:
            print(f"[db] safe_rebuild 失败: {e}")
            return False


def connect(db_path: str = None) -> GameDB:
    """工厂函数：返回已初始化的 GameDB；若损坏则自动重建。"""
    try:
        return GameDB(db_path)
    except sqlite3.DatabaseError:
        db = GameDB.__new__(GameDB)
        db.db_path = db_path or cfg.get_db_path()
        db.conn = None  # type: ignore
        if db.safe_rebuild():
            return db
        raise


if __name__ == "__main__":
    db = connect()
    print("games:", db.count_active_games())
    db.close()
