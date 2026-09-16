# ANGEL GAME

> 一个**本地优先**的 Windows 游戏启动与管理工具：管理你的游戏库、一键启动、记录游玩轨迹、按周期提醒检查更新。
> 前端用 [pywebview](https://github.com/r0x0r/pywebview) 跑原生 HTML/CSS/JS，数据全部存在本地 SQLite，**不上云、不联网**。

[English](README.en.md)

---

## ✨ 功能特性

| 模块 | 说明 |
|---|---|
| **游戏库管理** | 添加 / 编辑 / 隐藏 / 删除；添加时自动从 `.exe` 提取图标作封面，也可自定义封面图 |
| **一键启动** | 两种模式：`direct`（游戏本体）与 `launcher`（Steam / Epic / 官方启动器等平台启动器） |
| **今日进度** | 底部显示「今日已启动 / 活跃总数」；已启动的卡片显示「✓ 今天」角标 |
| **游玩统计** | 当前连续游玩天数（streak）、历史最长连续、累计启动次数、活跃天数；**日历热力图**（按月聚合每日启动的不同游戏数，点某天就地展开当天明细） |
| **标签与筛选** | 按标签分类、搜索框实时过滤 |
| **更新检查** | 每个游戏可配「启动器路径」+ 提醒周期（天）；超期卡片显示 muted「待更新」弱角标；卡片右键「🔄 检查启动器更新」用启动器模式启动并标记；底部「更新检查 (N)」汇总待检查项，支持单查 / 全部检查（**不污染游玩统计**） |
| **界面与体验** | 亮 / 暗主题、网格列数可调、窗口置顶、**单实例锁**、高 DPI 适配、窗口尺寸 / 最大化记忆 |
| **数据安全** | SQLite 启用 WAL 模式；数据库损坏时自动备份为 `.bak` 并重建；用户数据落到稳定目录 |

---

## 🖼 截图

> 截图已存入 [`docs/`](docs/) 目录，提交后下方图片自动显示：
>
> - `docs/preview-main.png` —— 主界面（游戏卡片网格 + 今日进度 + 底部栏 + 更新检查）
> - `docs/preview-stats.png` —— 统计面板（连续游玩 / 总览 + 日历热力图）

![主界面](docs/preview-main.png)
![统计面板](docs/preview-stats.png)

---

## 🛠 技术栈

- **语言**：Python 3.11+
- **界面**：[pywebview](https://github.com/r0x0r/pywebview)（Windows 下基于 Edge WebView2 内核）+ 原生 HTML / CSS / JS
- **存储**：SQLite（标准库 `sqlite3`）
- **平台 API**：[pywin32](https://github.com/mhammond/pywin32)（窗口置顶、单实例互斥、资源管理器打开目录）
- **打包**：[PyInstaller](https://pyinstaller.org/)（单文件 `.exe`，UPX 压缩）

> ⚠️ **主要支持平台：Windows 10 / 11**。代码对单实例锁、窗口置顶、DPI、打开目录等做了 Windows 专用实现，并保留非 Windows 兜底（不阻断启动），但未在其他平台验证。

---

## 📁 目录结构

```
angel_game/
├── main.py                 # 入口：初始化配置/数据库，创建 pywebview 窗口，处理单实例与窗口几何记忆
├── config.py               # 配置读写（config.json）、路径解析（开发/打包两套）、窗口几何记忆
├── database.py             # SQLite 层：建表、增删改查、启动日志、统计聚合、更新检查查询
├── launcher.py             # 启动逻辑：subprocess.Popen 启动 + 存活检测 + 启动/更新记录
├── icon_extractor.py       # 从 .exe 提取图标 / 生成默认图标
├── ANGEL GAME v2.spec      # PyInstaller 打包规格（单文件 exe）
├── web/
│   ├── __init__.py         # 包标记
│   ├── app_api.py          # pywebview 后端 API（暴露给 window.pywebview.api 的 js_api）
│   └── index.html          # 前端界面（HTML/CSS/JS，卡片网格 + 统计面板 + 设置）
├── assets/
│   ├── icon.ico            # 程序图标（打包用）
│   └── default_icon.png    # 默认封面模板
├── data/                   # 运行时数据（开发模式；打包后落到 %APPDATA%/ANGEL GAME/）— 不提交
│   ├── angel_game.db       # SQLite 数据库（games + launch_logs）
│   └── icons/              # 提取/缓存的封面与图标
├── history/                # 统计面板月份 JSON 缓存 — 不提交
├── config.json             # 用户配置（运行时生成/补全）— 不提交
├── window_geometry.json    # 窗口尺寸/最大化记录 — 不提交
└── dist/                   # 打包产物 ANGEL GAME v2.exe — 不提交
```

---

## 🚀 快速开始

### 环境要求

- Windows 10 / 11
- Python 3.11+
- Microsoft Edge **WebView2 Runtime**（Win10/11 通常已预装；缺失时运行会报错，需单独安装）

### 安装依赖

```bash
pip install -r requirements.txt
```

### 运行（开发模式）

```bash
python main.py          # 正常模式，加载本地游戏库
python main.py --demo   # 演示模式：自动注入示例游戏（用 notepad.exe 作启动目标），无需真实游戏即可体验
```

### 打包为可执行文件

```bash
pyinstaller "ANGEL GAME v2.spec" --noconfirm
```

产物位于 `dist/ANGEL GAME v2.exe`（单文件，双击即运行，无需 Python 环境）。
打包时会自动收集 `web/`、`assets/` 与 `webview` 运行时依赖。

---

## 📖 使用说明

1. **添加游戏**：点「添加」，填游戏名称与启动程序（`.exe`）；可选填「游戏本体」(direct 目标) 与「启动器路径」(launcher 目标)，上传封面，打标签。
2. **启动**：双击卡片或点启动按钮。默认 `direct` 模式跑游戏本体；若游戏需在平台启动器内更新/验证，请在编辑里填「启动器路径」，改用右键「检查启动器更新」或该模式启动。
3. **检查更新**：编辑游戏并填入「启动器路径」+ 提醒周期（天，默认 3）。超期后卡片左上角出现 muted「待更新」角标，右键「🔄 检查启动器更新」会拉起启动器并标记已检查（不计入今日进度）；底部「更新检查 (N)」可一次查看全部待检查项并单查 / 全查。
4. **统计面板**：查看连续游玩、总览与日历热力图；点某天展开当天各游戏的启动明细。
5. **设置**：切换亮 / 暗主题、调整网格列数、开关窗口置顶。窗口尺寸与最大化状态会自动记忆。

---

## ⚙️ 配置（`config.json`）

运行时生成于用户数据目录，缺失或损坏会自动用默认值重建并补全字段。生效配置项：

| 键 | 说明 | 默认 |
|---|---|---|
| `update_check.default_remind_days` | 更新检查默认提醒周期（天） | `3` |
| `update_check.default_launch_mode` | 默认启动模式 | `"direct"` |
| `window_behavior.always_on_top` | 窗口是否置顶 | `false` |
| `ui.theme` | 主题：`"light"` / `"dark"` | `"dark"` |
| `ui.grid_columns` | 卡片网格列数（2–10） | `5` |
| `ui.tile_width` / `ui.tile_height` | 卡片尺寸（px） | `180` / `220` |

> 注：配置文件中可能还存在 `scan_directories`、`exclude_patterns`、`launcher_detection` 等字段，为后续「目录扫描自动入库」功能的预留，**当前版本未读取**，可忽略。

---

## 💾 数据存储

- **开发模式**：用户数据位于项目根目录 —— `data/angel_game.db`、`data/icons/`、`config.json`、`window_geometry.json`、`history/`、运行时生成的 `assets/default_icon.png`。
- **打包后**：数据落到稳定的 `%APPDATA%/ANGEL GAME/`（跨运行持久），只读资源模板取自 exe 内部。
- **数据库表**：
  - `games`：游戏信息（名称、exe 路径、启动器路径、封面/图标、最后启动、更新检查时间、提醒周期、标签、隐藏标记）。
  - `launch_logs`：启动日志（`game_id`、`launched_at`、`launch_mode`），外键级联，删除游戏时一并清除。

---

## ❓ 常见问题

- **`ModuleNotFoundError: No module named 'webview'`** → 未安装依赖，执行 `pip install -r requirements.txt`。
- **窗口白屏 / 报错 WebView2 相关** → 系统缺 Edge WebView2 Runtime，到微软官网安装后重试。
- **数据库损坏** → 程序会自动备份原库为 `angel_game.db.bak` 并重建（重建后数据清空，旧备份可手动恢复）。
- **重复双击只弹出一个窗口** → 单实例锁生效，重复启动会激活已存在的窗口而非新开。
- **想重新打包却找不到入口** → 使用 `ANGEL GAME v2.spec`，不要使用旧版 `ANGEL GAME.spec`（已移除）。

---

## 📌 版本

- **v2.x（当前）**：接入「检查启动器更新」完整链路（启动器路径录入 → 超期判定 → 卡片弱角标 → 右键检查 → 全局待检查面板），清理旧版遗留的打包/原型/测试残留。
- 早期版本为纯游戏库 + 启动统计（P0–P9 渐进式迭代）。

---

## 📄 许可证

[MIT License](LICENSE) — 可自由使用、修改、分发。如需其他协议，替换为对应 `LICENSE` 文件即可。

---

## 🙏 致谢

- [pywebview](https://github.com/r0x0r/pywebview) — 把 Web 前端装进桌面窗口
- [PyInstaller](https://pyinstaller.org/) — 单文件打包
- [pywin32](https://github.com/mhammond/pywin32) — Windows 平台 API 桥接
