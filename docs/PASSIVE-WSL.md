# WSL 现有进程的旁路观测

适用链路：`Codex / Claude → 15722（WSL CC Switch）→ 7890（mihomo）→ anyrouter`。

本模式使用 Linux AF_PACKET 接收网络报文副本，不接管任何已有连接，不修改 CC Switch / Codex 配置，不修改防火墙或路由，不注入进程，不安装 CA，不向原进程发送输入或停止信号。每次观测有时间上限，结束后关闭抓取 socket。

## 启动

WSL 镜像网络的实际接口可能是 `loopback0`，普通 Linux 回环则通常是 `lo`。通过 `ip -br addr` 和只读流量计数确认后选择，不要为了抓取修改网络模式。

在 WSL 内激活已安装本项目的 Python 环境，再运行（端口和接口应替换为实际值）：

```bash
sudo "$(command -v python)" -m route_scope.passive \
  --interface lo --port 15722 --seconds 120 \
  --data-dir "$HOME/.local/share/route-scope/passive"
```

`--pid` 可以重复，用于保存原进程的启动标识和存活快照；**它不是流量过滤器**。真正过滤条件是指定接口、127.0.0.1 地址和指定 TCP 端口。只有能从 socket inode 解析出 PID 时才填入 `client_pids`；镜像网络经 Windows 转发时可能无法解析，必须保留未知。

请求头会话标识的指纹可与本机会话文件匹配；再通过进程打开的会话文件验证归属。不能因为端口上出现了 Codex 流量就假定来自某个 CLI PID。

在同一个 WSL 系统另开只读面板：

```bash
sudo "$(command -v python)" -m route_scope view --data-dir "$HOME/.local/share/route-scope/passive" --dashboard-port 15926
```

## 证据边界

- 抓到的是 **客户端发给 CC Switch 的请求**以及 **CC Switch 返回给客户端的响应**，每条记录标记 `capture_boundary=client_to_ccswitch`、`upstream_request_observed=false`。
- 不能据此确认 CC Switch 转发到 anyrouter 时是否改过参数。7890 上的 HTTPS 流量是密文，本模式不解密或冒充 anyrouter。
- 请求强度只从实际请求正文提取；最终强度只取实际响应字段，不通过答题、tokens 或耗时推断。
- 从一条流的中途开始时，可能缺少请求头或响应头；此类数据不能拼成虚假的完整请求/响应对。
- 只支持 IPv4 回环上的 HTTP/1.x（Content-Length、chunked、连接结束定界），不支持本模式下的 HTTP/2、WebSocket 或 TLS 解密。
- 重传与乱序按 TCP 序号重组、去重。WSL mirrored 接口需要接收两个方向，不能直接丢弃所有 PACKET_OUTGOING 帧。
- `response.completed` 已被完整解析后，客户端立即关闭/重置连接不等于模型失败。`http_message_complete=false` 和 `capture_end_reason` 会保留 HTTP 结束边界未完整观察的事实；抓取窗口结束也不等于请求失败。

输出：`captures.db` 存放脱敏报文，`passive-summary.json` 存放观察统计、内核丢包计数和原进程前后快照。不会保存原始未脱敏 PCAP。未抓到报文或抓包丢失时必须如实记录，不能声称验证通过。
