# 可选 mitmproxy 适配器

适用于已有 mitmproxy 工作流的用户。此可选依赖要求 Python 3.12+；主程序仍支持 Python 3.11+。

```bash
python -m pip install -e ".[mitm]"
mitmdump --mode reverse:https://anyrouter.top@15923 --listen-host 127.0.0.1 -s addons/mitm_capture.py --set route_scope_data=data/mitm --set route_scope_hosts=anyrouter.top
```

供应商 base_url 指向 `http://127.0.0.1:15923/v1`。在同一目录的另一个终端运行 `python -m route_scope view --data-dir data/mitm`，打开 <http://127.0.0.1:15924>。

addon 只捕获指定域名，流式回调返回原字节；不支持 WebSocket 捕获。不要让两个写入进程共用一个目录，`view` 只读面板除外。这里使用反向代理，不需要安装 CA。

恢复供应商原地址后再关闭代理。可选端到端测试：设置环境变量 `ROUTE_SCOPE_MITMDUMP` 为 mitmdump 可执行文件路径，再执行 `python -m pytest tests/test_mitm_optional.py -q`。
