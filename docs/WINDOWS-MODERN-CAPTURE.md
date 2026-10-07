# Windows：替代旧 WinPcap 的现代方案

使用 `capture-doctor` 检查本机 DLL、回环接口和权限；驱动状态取决于你的机器。该命令不修改驱动。

| 方案 | 适合本项目的用途 | 取舍 |
|---|---|---|
| **Npcap，推荐用于保持原链路的旁路抓取** | 捕获 Windows 本机 CC Switch 15721 的 HTTP 请求/返回 | 需要安装驱动及相应权限；本项目已优先加载 `System32/Npcap/wpcap.dll` |
| **本项目 HTTP 路由网关，推荐用于控制出站与核验返回** | CC Switch → Route Scope → anyrouter；Windows/WSL 共用 Python 实现 | 无需抓包驱动、管理员权限或 HTTPS 中间人证书；必须显式接入新链路 |
| Windows Pktmon | 排查丢包、网络栈和连接问题 | Windows 内置诊断工具，不能直接替代本项目的 HTTP/SSE 路由控制 |
| WinDivert/WFP | 需要开发额外内核接收适配层时的候选 | 尚未集成；即便采用 sniff-only，也不能凭抓包解密 anyrouter TLS 或选择号池账号 |

## Npcap 共存安装方案

1. 从 [Npcap 官方下载页](https://npcap.com/#download) 获取安装器。
2. 可先执行项目的 `verify-npcap-installer.ps1 -InstallerPath <安装器路径>`。它只检查数字签名、显示签名主体和 SHA256，不运行安装器。
3. 如需保留旧 WinPcap 应用，请按 [Npcap 官方说明](https://npcap.com/guide/npcap-users-guide.html)选择共存安装方式；本项目优先加载独立 Npcap 目录中的 DLL。
4. 驱动安装安排在现有长任务结束后；安装器可能要求重启。免费版不要套用 OEM 专用的静默 `/S` 参数。
5. 仅重启 Route Scope 自己的抓取器，再执行：

```powershell
cd route-scope
.\.venv\Scripts\python.exe -m route_scope capture-doctor --open-interface
.\start-auto.cmd
```

`loopback_interfaces` 非空仅说明接口可枚举；`interface_open_verified=true` 表示本项目成功打开并关闭了自己的句柄。最终仍要以正常应用请求出现在面板里为准。Npcap 若安装为仅管理员访问，需要为运行 Route Scope 的进程提供相应权限。

项目不会自动卸载 WinPcap、更新网卡驱动或重启用户应用。驱动由用户按机器需求自行安装。

## 为什么路由控制优先采用应用层网关

抓包只能观察，无法让 anyrouter 选择特定内部账号。新的[强度回显网关](ROUTING-GUARD.md)在请求被送出前选择已授权路由，并在完整返回后核验字段；它解决的是路由选择与结果准入，无需依赖 Windows 抓包驱动。之前的旁路自动抓取继续用于不改变现有会话的观察。

参考：[微软 Pktmon 文档](https://learn.microsoft.com/en-us/windows-server/networking/technologies/pktmon/pktmon)、[WinDivert 官方文档](https://github.com/basil00/WinDivert/wiki/WinDivert-Documentation)。后两者未作为本次交付的实际捕获后端。
