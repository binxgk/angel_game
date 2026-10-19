"""tests/selfcheck.py — 启动参数 / 快捷方式功能的回归自检 (v2.9)

为什么这么写：项目是「开发态数据放项目目录」的设计（config.py 里 _DATA = 脚本目录），
直接 import 就会操作真实的 data/angel_game.db。所以这里先复制一份最小副本到临时目录，
在副本里跑完全部检查，最后连临时目录一起删掉——绝不碰真实库与真实图标缓存。

用法：python tests/selfcheck.py
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COPY_FILES = ["config.py", "database.py", "launcher.py", "shortcut.py", "icon_extractor.py"]
COPY_DIRS = ["web", "assets"]

_ok = True
_failed: list[str] = []


def check(label: str, cond: bool, extra: object = "") -> None:
    global _ok
    if cond:
        print(f"[PASS] {label}")
    else:
        _ok = False
        _failed.append(label)
        print(f"[FAIL] {label} | {extra}")


def build_sandbox() -> str:
    """复制一份最小可运行副本，并塞一个「旧版 schema」的库用来测迁移。"""
    box = tempfile.mkdtemp(prefix="angel_selfcheck_")
    for f in COPY_FILES:
        shutil.copy2(os.path.join(REPO, f), box)
    for d in COPY_DIRS:
        shutil.copytree(os.path.join(REPO, d), os.path.join(box, d),
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    os.makedirs(os.path.join(box, "data"), exist_ok=True)
    # 旧库：games 表里没有 launch_args 列（验证 _ensure_columns 的迁移路径）
    conn = sqlite3.connect(os.path.join(box, "data", "angel_game.db"))
    conn.executescript(
        """
        CREATE TABLE games (
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
        CREATE TABLE launch_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id INTEGER NOT NULL,
            launched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            launch_mode TEXT DEFAULT 'direct',
            FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE
        );
        INSERT INTO games (name, exe_path) VALUES ('老游戏', 'C:/Windows/System32/notepad.exe');
        """
    )
    conn.commit()
    conn.close()
    return box


def make_lnk(path: str, target: str, args: str, workdir: str) -> None:
    """用 WScript.Shell 造一个 .lnk；失败则跳过相关检查（CI 无桌面也能跑）。"""
    script = (
        f"$w=New-Object -ComObject WScript.Shell;"
        f"$s=$w.CreateShortcut('{path}');"
        f"$s.TargetPath='{target}';$s.Arguments='{args}';"
        f"$s.WorkingDirectory='{workdir}';$s.Save()"
    )
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                   capture_output=True, timeout=30)


def main() -> int:
    box = build_sandbox()
    try:
        sys.path.insert(0, box)
        os.chdir(box)
        # pywebview 只在文件对话框里用到，自检不需要真窗口
        stub = types.ModuleType("webview")
        stub.OPEN_DIALOG = 0
        stub.windows = []
        sys.modules.setdefault("webview", stub)

        import database
        import launcher
        import shortcut
        from web import app_api

        py = sys.executable

        # 1. 旧库迁移
        db = database.connect()
        cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(games)").fetchall()}
        check("旧库自动补出 launch_args 列", "launch_args" in cols, sorted(cols))
        old = db.get_active_games_with_today()
        check("迁移后老数据 launch_args 为空串",
              all(not r.get("launch_args") for r in old), old)

        # 2. 写入 / 读取 / 编辑
        gid = db.add_game(name="自检", exe_path=py, direct_exe_path=py,
                          launch_args="-krqlv=hd -dx11")
        check("add_game 写入 launch_args", bool(gid), gid)
        check("get_game 读回 launch_args",
              dict(db.get_game(gid)).get("launch_args") == "-krqlv=hd -dx11")
        db.update_game(gid, launch_args="-dx12")
        check("update_game 可改 launch_args", db.get_game(gid)["launch_args"] == "-dx12")

        api = app_api.Api()
        ser = [x for x in api.load()["games"] if x["id"] == gid][0]
        check("load() 序列化带 launch_args", ser.get("launch_args") == "-dx12", ser.get("launch_args"))

        api.edit_game({"id": gid, "name": "自检", "args": "  -windowed  "})
        check("edit_game 覆盖 args 并去首尾空白",
              db.get_game(gid)["launch_args"] == "-windowed", db.get_game(gid)["launch_args"])
        api.edit_game({"id": gid, "name": "自检", "args": ""})
        check("edit_game 传空串可清空 args", db.get_game(gid)["launch_args"] == "")

        # 3. 参数真的进了子进程（"A B" 必须保持一个 token）
        child = os.path.join(box, "data", "_argv_child.py")
        with open(child, "w", encoding="utf-8") as f:
            f.write("import json,os,sys\n"
                    "p=os.path.join(os.path.dirname(os.path.abspath(__file__)),'_argv_out.json')\n"
                    "json.dump(sys.argv, open(p,'w',encoding='utf-8'), ensure_ascii=False)\n")
        api.edit_game({"id": gid, "name": "自检", "body": py,
                       "args": f'"{child}" -krqlv=hd -name="A B"'})
        game = dict(db.get_game(gid))
        before = db.conn.execute("SELECT COUNT(*) c FROM launch_logs WHERE game_id=?",
                                 (gid,)).fetchone()["c"]
        ok, detail = launcher.launch_game(game, db, poll_seconds=1)
        after = db.conn.execute("SELECT COUNT(*) c FROM launch_logs WHERE game_id=?",
                                (gid,)).fetchone()["c"]
        got = json.load(open(os.path.join(box, "data", "_argv_out.json"), encoding="utf-8"))
        check("带参数启动成功", ok, detail)
        check("参数逐个传达到子进程", got[1:] == ["-krqlv=hd", "-name=A B"], got)
        check("本次启动计入日志", after == before + 1, f"{before}->{after}")

        # 4. 走启动器时不透传游戏参数（桩掉 execute_launch，不真起进程）
        recorded: list = []
        orig = launcher.execute_launch

        def fake(exe_path, cwd=None, poll_seconds=8, args=None):
            recorded.append((exe_path, list(args or [])))
            return (True, "已启动: stub")

        launcher.execute_launch = fake
        launcher.launch_game({**game, "launcher_path": r"C:\Windows\System32\notepad.exe"},
                             db, mode="launcher", log=False)
        launcher.launch_game(game, db, poll_seconds=1)
        launcher.execute_launch = orig
        check("mode=launcher 时不透传游戏参数",
              recorded[0] == (r"C:\Windows\System32\notepad.exe", []), recorded[0])
        check("mode=direct 时带着参数启动",
              recorded[1][1] == [child, "-krqlv=hd", "-name=A B"], recorded[1])

        # 5. 快捷方式解析
        lnk = os.path.join(box, "data", "_test.lnk")
        make_lnk(lnk, r"C:\Windows\System32\notepad.exe", "-krqlv=hd -dx11", r"C:\Windows")
        if os.path.isfile(lnk):
            info = shortcut.read_shortcut(lnk)
            check("解析 .lnk 目标", bool(info["ok"]) and info["target"].lower().endswith("notepad.exe"), info)
            check("解析 .lnk 参数", info["args"] == "-krqlv=hd -dx11", info["args"])
            check("解析 .lnk 工作目录", info["workdir"] == r"C:\Windows", info["workdir"])
            r = api.resolve_shortcut(lnk)
            check("API resolve_shortcut 返回目标与参数",
                  bool(r.get("ok")) and bool(r.get("target")) and bool(r.get("args")), r)
        else:
            print("[SKIP] 无法创建 .lnk（当前环境无 WScript.Shell）")
        r2 = api.resolve_shortcut(os.path.join(box, "data", "nope.lnk"))
        check("无效路径返回错误而非崩溃", (not r2.get("ok")) and bool(r2.get("error")), r2)

        db.delete_game(gid)
        db.close()
    finally:
        os.chdir(REPO)
        shutil.rmtree(box, ignore_errors=True)

    print()
    if _ok:
        print("RESULT: ALL PASS")
        return 0
    print("RESULT: FAILED ->", ", ".join(_failed))
    return 1


if __name__ == "__main__":
    sys.exit(main())
