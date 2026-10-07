# 参与开发

要求 Python 3.11+，Windows 或 Linux。前端为静态 HTML/CSS/JavaScript，无需 Node 构建。

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux: source .venv/bin/activate
python -m pip install -e ".[dev]"
python -m pytest -q
```

使用 uv 时可运行 `uv sync --locked --extra dev` 与 `uv run --locked pytest -q`。修改依赖后更新 `uv.lock`。

修改报文解析、路由或存储时，为真实故障添加回归测试；优先使用本地模拟上游。不要调用真实付费模型来运行基础测试。测试不能把回显值、tokens 或耗时当成实际模型能力证明。

模块：`evidence.py` 解析，`server.py` 代理和面板，`routing.py` 网关，`watch.py` 生命周期，`autocapture.py` 旁路捕获，`snapshots.py` 跨系统快照。静态资源位于 `route_scope/static/`。

提交前运行测试、检查文档链接，并确认没有真实数据或凭据。Pull Request 请说明用户可见的变化、测试结果与未覆盖边界。发布流程见 [RELEASING.md](docs/RELEASING.md)。
