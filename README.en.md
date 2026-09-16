# ANGEL GAME

> A **local-first** Windows game launcher and manager: organize your game library, launch with one click, track your play history, and get periodic update-check reminders.
> The front end runs native HTML/CSS/JS via [pywebview](https://github.com/r0x0r/pywebview); all data lives in a local SQLite database — **no cloud, no network**.

[中文](README.md)

---

## ✨ Features

| Module | Description |
|---|---|
| **Game library** | Add / edit / hide / delete; an icon is auto-extracted from the `.exe` as the initial cover, and custom covers are supported |
| **One-click launch** | Two modes: `direct` (the game binary itself) and `launcher` (platform launchers such as Steam / Epic / official launchers) |
| **Today's progress** | The bottom bar shows "launched today / total active"; launched cards get a "✓ today" badge |
| **Play stats** | Current streak, longest streak, total launches and active days; plus a **calendar heatmap** (per-month aggregation of distinct games launched each day — click a day to expand its detail inline) |
| **Tags & filtering** | Categorize by tag, and filter instantly with the search box |
| **Update check** | Each game can store a "launcher path" + a reminder interval (in days); overdue cards show a muted "needs update" badge; right-click "🔄 Check launcher update" launches in launcher mode and marks it checked; the bottom "Update check (N)" panel aggregates pending items with single / bulk check (**without polluting play stats**) |
| **UI & experience** | Light / dark theme, adjustable grid columns, always-on-top window, **single-instance lock**, high-DPI support, window size and maximized-state memory |
| **Data safety** | SQLite runs in WAL mode; a corrupted database is automatically backed up to `.bak` and rebuilt; user data is written to a stable directory |

---

## 🖼 Screenshots

> Screenshots live in [`docs/`](docs/); the images below render once committed:
>
> - `docs/preview-main.png` — Main window (game card grid + today's progress + bottom bar + update check)
> - `docs/preview-stats.png` — Stats panel (streaks / overview + calendar heatmap)

![Main window](docs/preview-main.png)
![Stats panel](docs/preview-stats.png)

---

## 🛠 Tech Stack

- **Language**: Python 3.11+
- **UI**: [pywebview](https://github.com/r0x0r/pywebview) (backed by the Edge WebView2 engine on Windows) + native HTML / CSS / JS
- **Storage**: SQLite (stdlib `sqlite3`)
- **Platform APIs**: [pywin32](https://github.com/mhammond/pywin32) (always-on-top, single-instance mutex, reveal in Explorer)
- **Packaging**: [PyInstaller](https://pyinstaller.org/) (single-file `.exe`, UPX-compressed)

> ⚠️ **Primary platform: Windows 10 / 11**. The single-instance lock, always-on-top, DPI handling and "open folder" have Windows-specific implementations, with non-Windows fallbacks that keep the app starting. Other platforms are untested.

---

## 📁 Project Structure

```
angel_game/
├── main.py                 # Entry point: init config/database, create the pywebview window, handle single instance + window geometry
├── config.py               # Config read/write (config.json), path resolution (dev vs packaged), window geometry memory
├── database.py             # SQLite layer: schema, CRUD, launch logs, stats aggregation, update-check queries
├── launcher.py             # Launch logic: subprocess.Popen + liveness check + launch/update logging
├── icon_extractor.py       # Extract icons from .exe / generate the default icon
├── ANGEL GAME v2.spec      # PyInstaller spec (single-file exe)
├── web/
│   ├── __init__.py         # Package marker
│   ├── app_api.py          # pywebview backend API (the js_api exposed as window.pywebview.api)
│   └── index.html          # Front end (HTML/CSS/JS: card grid + stats panel + settings)
├── assets/
│   ├── icon.ico            # App icon (used for packaging)
│   └── default_icon.png    # Default cover template
├── data/                   # Runtime data (dev mode; %APPDATA%/ANGEL GAME/ when packaged) — not committed
│   ├── angel_game.db       # SQLite database (games + launch_logs)
│   └── icons/              # Extracted/cached covers and icons
├── history/                # Monthly JSON cache for the stats panel — not committed
├── config.json             # User config (generated/backfilled at runtime) — not committed
├── window_geometry.json    # Window size / maximized record — not committed
└── dist/                   # Build output: ANGEL GAME v2.exe — not committed
```

---

## 🚀 Quick Start

### Requirements

- Windows 10 / 11
- Python 3.11+
- Microsoft Edge **WebView2 Runtime** (usually preinstalled on Win10/11; if missing, the app will error until you install it)

### Install dependencies

```bash
pip install -r requirements.txt
```

### Run (development mode)

```bash
python main.py          # Normal mode, loads your local game library
python main.py --demo   # Demo mode: injects sample games (using notepad.exe as the target) so you can try it without real games
```

### Build an executable

```bash
pyinstaller "ANGEL GAME v2.spec" --noconfirm
```

The output is `dist/ANGEL GAME v2.exe` (single file, double-click to run, no Python required).
The build automatically bundles `web/`, `assets/` and the `webview` runtime dependencies.

---

## 📖 Usage

1. **Add a game**: click "Add", then enter a name and the executable (`.exe`); optionally set a "game binary" (the direct target) and a "launcher path" (the launcher target), upload a cover and add tags.
2. **Launch**: double-click a card or click the launch button. The default `direct` mode runs the game binary; if the game must be updated or verified inside a platform launcher, fill in the "launcher path" and use right-click "Check launcher update" or launch in that mode.
3. **Update check**: edit a game and fill in the "launcher path" + reminder interval (days, default 3). Once overdue, a muted "needs update" badge appears in the card's top-left corner; right-click "🔄 Check launcher update" brings up the launcher and marks it checked (it does not count toward today's progress). The bottom "Update check (N)" panel lists every pending item and lets you check one or all.
4. **Stats panel**: see your streaks, the overview and the calendar heatmap; click a day to expand that day's per-game launch detail.
5. **Settings**: switch light / dark theme, adjust grid columns, toggle always-on-top. Window size and maximized state are remembered automatically.

---

## ⚙️ Configuration (`config.json`)

Generated at runtime in the user data directory; if missing or corrupted it is rebuilt from defaults with missing fields backfilled. Active keys:

| Key | Description | Default |
|---|---|---|
| `update_check.default_remind_days` | Default reminder interval for update checks (days) | `3` |
| `update_check.default_launch_mode` | Default launch mode | `"direct"` |
| `window_behavior.always_on_top` | Keep the window on top | `false` |
| `ui.theme` | Theme: `"light"` / `"dark"` | `"dark"` |
| `ui.grid_columns` | Card grid columns (2–10) | `5` |
| `ui.tile_width` / `ui.tile_height` | Card size (px) | `180` / `220` |

> Note: the config file may also contain `scan_directories`, `exclude_patterns`, `launcher_detection`, etc. These are reserved for a future "scan folders and import automatically" feature and are **not read by the current version** — safe to ignore.

---

## 💾 Data Storage

- **Development mode**: user data lives in the project root — `data/angel_game.db`, `data/icons/`, `config.json`, `window_geometry.json`, `history/`, plus the runtime-generated `assets/default_icon.png`.
- **Packaged**: data goes to a stable `%APPDATA%/ANGEL GAME/` (persists across runs); read-only resource templates are read from inside the exe.
- **Tables**:
  - `games`: game info (name, exe path, launcher path, cover/icon, last launched, update-check time, reminder interval, tags, hidden flag).
  - `launch_logs`: launch log (`game_id`, `launched_at`, `launch_mode`), foreign-key cascaded so deleting a game also removes its logs.

---

## ❓ FAQ

- **`ModuleNotFoundError: No module named 'webview'`** → dependencies are missing; run `pip install -r requirements.txt`.
- **Blank window / WebView2-related error** → the system is missing the Edge WebView2 Runtime; install it from Microsoft and retry.
- **Corrupted database** → the app automatically backs up the original as `angel_game.db.bak` and rebuilds it (the rebuilt DB is empty; you can restore the backup manually).
- **Double-clicking repeatedly opens only one window** → the single-instance lock is working; a second launch activates the existing window instead of opening a new one.
- **Can't find the build entry point after a rebuild** → use `ANGEL GAME v2.spec`, not the old `ANGEL GAME.spec` (removed).

---

## 📌 Versions

- **v2.x (current)**: wired up the complete "check launcher update" flow (launcher path entry → overdue detection → muted card badge → right-click check → global pending panel), and cleaned up the leftover packaging / prototype / test artifacts from older versions.
- Earlier versions were a plain game library + launch stats (P0–P9 incremental iterations).

---

## 📄 License

[MIT License](LICENSE) — free to use, modify and distribute. To use a different license, simply replace the `LICENSE` file.

---

## 🙏 Acknowledgements

- [pywebview](https://github.com/r0x0r/pywebview) — puts a web front end inside a desktop window
- [PyInstaller](https://pyinstaller.org/) — single-file packaging
- [pywin32](https://github.com/mhammond/pywin32) — Windows platform API bridge
