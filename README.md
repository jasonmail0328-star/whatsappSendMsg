# WhatsApp 多账号 管理器（模块化版本）

概述
----
这个项目是将单文件原型拆分为模块化结构的实现。包含：
- app/db.py：数据库初始化与 CRUD
- app/worker.py：Playwright 操作（添加账号、发送消息）
- app/tasks.py：任务调度（添加账号、发送消息）
- app/ui.py：简单 HTML 渲染
- app/server.py：FastAPI 路由
- run.py：启动入口（可选桌面窗口）

快速开始（Windows / Desktop）
1. 建议创建并激活虚拟环境：
   python -m venv .venv
   .venv\Scripts\Activate.ps1   # PowerShell（注意 ExecutionPolicy）

2. 安装依赖并下载 Playwright 浏览器：
   pip install -r requirements.txt
   python -m playwright install chromium

3. 运行（HTTP only）：
   python run.py

   或运行并打开桌面窗口（需要 pywebview 和 WebView2 runtime）：
   python run.py --desktop

4. 在 UI 中：
   - 点击 “添加账号（扫码）” -> 在弹出的 Chromium 窗口扫码登录 -> 注册成功后 profile 保存在 `accounts/`
   - 在消息框中输入消息，点击某个账号的 “发送” 按钮，程序会选择一个未被系统触达的联系人并发送（或模拟发送）

数据库
----
SQLite 数据库位于 `data/bot.db`。初始表结构见 `migrations/001_create_tables.sql`。

注意事项与限制
----
- 这个版本为模块化原型，适合本地桌面或开发使用。
- 发送任务会以 headful Playwright 浏览器运行；同一 profile 不可并发使用（避免同时触发同一账号多个发送任务）。
- 联系人抓取目前仅从可见聊天列表抓取，后续可扩展为滚动抓取或打开聊天以获取更多 metadata。
- 请遵守 WhatsApp 使用条款与当地法规。

下一步建议
----
- 在 tasks 中加入 account-level locking（写入 DB in_use 标志）以避免 profile 并发访问。
- 增强联系人抓取（滚动与更多属性）。
- 添加模板管理、发送计划、监控/告警与导出功能。
- 如需，我可把该项目打包为 Windows 可执行文件（PyInstaller）并提供打包脚本。
