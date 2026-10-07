# 发布流程

1. 更新 `route_scope/__init__.py`、`pyproject.toml` 和 `CHANGELOG.md` 中的版本与变更，执行 `uv lock`。
2. 运行 `python -m pytest -q`；检查当前版本的公开文档与截图只使用模拟数据。
3. 执行 `uv build`，检查 wheel / sdist 内容，并在独立环境安装 wheel，运行 `route-scope --version` 与演示。
4. 检查 `git status`、忽略规则与提交文件；不要提交本地会话、密钥、数据库或构建缓存。
5. 推送 main 并等待 Windows / Ubuntu CI 通过，然后创建对应 `vX.Y.Z` 标签。
6. 创建 GitHub Release，附上 wheel、sdist 与 SHA256SUMS。GitHub 自动提供的源码 ZIP 包含启动脚本及教程。

此流程不会发布到 PyPI。依赖漏洞和真实驱动兼容性需要另行评估；单元测试通过不能替代这些检查。
