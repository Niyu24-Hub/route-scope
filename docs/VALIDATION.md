# 0.3.2 发布验证

日期：2026-10-07（北京时间）。验证使用本地模拟上游，不调用付费模型。

- Windows 基础回归：**99 passed, 2 skipped**。跳过项为需要显式启用的真实 mitmproxy 引擎和实时 Npcap 捕获。
- CLI：`python -m route_scope --version` 输出 `route-scope 0.3.2`。
- Windows 首次启动：从干净源码副本调用共用启动器，成功创建 Python 3.14 环境、安装依赖并输出版本。
- 安装包：构建 wheel 与源码分发包；独立虚拟环境安装 wheel，使用隔离导入验证页面资源、14 条模拟记录、JSONL 导出与密钥脱敏。
- 文档与分发：只保留模拟截图，真实会话截图、数据和本机历史资料排除在 Git 与源码分发包之外。
- GitHub Actions：Windows / Ubuntu × Python 3.11 / 3.12；每项执行基础测试、构建、安装 wheel 和隔离演示验证。每次实际结果以 [Actions](https://github.com/Niyu24-Hub/route-scope/actions/workflows/tests.yml) 为准。

测试覆盖 SSE 分块、压缩、Unicode、缺失/null/未知字段、WebSocket 多轮、错误与截断、配置恢复、认证与会话脱敏、路由拒绝与有限重试、自动发现、来源快照与数据库恢复。

本次发布没有重新验证真实上游、当前 Npcap 驱动或各 WSL 网络模式；基础测试和模拟截图不能证明号池能力或你的机器已具备抓包权限。旧版本的本机实测报告不作为公开发布的验收依据。
