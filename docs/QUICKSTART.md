# 从零开始使用 Route Scope

## 1. 下载和环境

安装 Python 3.11 或更高版本，并确认 `python --version`（Windows 也可用 `py -3 --version`）可用。Windows 启动器支持 `py` 或 PATH 中的 `python`。旁路自动抓取主要面向 Windows / WSL；macOS 未验证。

```bash
git clone https://github.com/Niyu24-Hub/route-scope.git
cd route-scope
```

也可以从仓库 Code → Download ZIP 下载并解压。Release 的 wheel 适合命令行安装；需要双击启动器时使用源码 ZIP。

## 2. 先运行不需要 Key 的演示

Windows 双击 `start-demo.cmd`，等待出现“已生成 18 条本地协议演示记录”，打开 <http://127.0.0.1:15824>。首次启动会创建 `.venv` 并下载依赖，需要网络；按 Ctrl+C 退出。

Linux / WSL：

```bash
python3 -m venv .venv-linux
source .venv-linux/bin/activate
python -m pip install -e .
python -m route_scope demo
```

Ubuntu 若缺少 venv 支持，先安装系统提供的 `python3-venv`，或使用 `bash start-wsl.sh --demo` 的用户环境引导流程。Linux 环境不要复用 Windows 创建的 `.venv`。

面板中的数据全部来自本地模拟上游。检查请求强度、首包回显和最终回显三列，点击一条记录查看报文，并尝试导出 JSONL。重复运行演示会向演示目录追加记录，因此数量可能超过 18。

## 3. 选择真实使用方式

| 需求 | 方式 | 是否改变链路 |
|---|---|---|
| 保持已有会话，只观察客户端与 CC Switch 之间的流量 | `watch` 自动旁路 | 否；Windows 需要 Npcap，Linux 需要抓包权限 |
| 观察 CC Switch 发给上游的请求 | `serve` 显式代理 | 是；需要修改供应商 base_url |
| 明确提高出站门槛并拒绝最终回显不合格的响应 | `serve --config guard.local.toml` | 是；完整缓冲后交付，首字更晚 |

### A. 自动旁路

Windows 双击 `start-auto.cmd`，打开 <http://127.0.0.1:15927>。正常使用 CC Switch 管理的客户端，发送一条新消息。来源状态必须显示可用，并且新请求出现在列表中，才说明观察成功。

无 Windows 回环接口时运行 `.venv\Scripts\python.exe -m route_scope capture-doctor`；根据 [Windows 诊断说明](WINDOWS-MODERN-CAPTURE.md)检查 Npcap，或使用显式代理。没有 WSL 时无需安装 WSL；已有 WSL 可用 `-Distro Ubuntu` 限定来源。

WSL 独立运行：安装好本项目后，在已激活环境执行 `sudo "$(command -v python)" -m route_scope watch --data-dir "$HOME/.local/share/route-scope/auto"`。这里需要 root 读取报文副本。结束使用 Ctrl+C。更详细的发现机制与数据位置见 [自动抓取](AUTO-CAPTURE.md)。

Windows 停止使用 `stop-auto.cmd`。如果自定义过数据目录，使用相同目录执行 `python -m route_scope watch-stop --data-dir <目录>`。

### B. 显式代理

先在独立终端启动，保持窗口运行：

```bash
python -m route_scope serve --upstream https://anyrouter.top
```

Windows 未激活环境时用 `.venv\Scripts\python.exe` 替代 `python`，也可以运行 `start-windows.ps1`。

1. 记录 CC Switch 中供应商原有的上游地址。
2. 将该供应商的 base_url 改为 `http://127.0.0.1:15723/v1`，保留模型和密钥。
3. 客户端仍连接 CC Switch。发起新请求后检查 <http://127.0.0.1:15724>。
4. 停止代理前先恢复供应商原地址，再关闭 Route Scope。

`--upstream` 是实际上游根地址，通常不带 `/v1`，因为客户端路径会原样追加。不要指回 CC Switch 或本项目自身端口，以免循环。需要网络代理时明确添加 `--outbound-proxy http://127.0.0.1:7890`，请替换为实际地址。

也可通过 `doctor` 查看供应商 ID，用 `connect --provider <ID>` 预览变更；退出 CC Switch 后加 `--apply` 执行。恢复同样先退出 CC Switch，再运行 `restore --apply`。默认恢复凭据在 `data/route-receipt.json`，请保留到恢复完成。

### C. 强度回显网关

复制 `guard.example.toml` 为 `guard.local.toml`，把 `models` 改为供应商实际支持的模型名，检查上游地址。密钥继承原请求或通过 `key_env` 指定的环境变量提供。

```bash
python -m route_scope serve --config guard.local.toml
```

Windows 也可运行 `powershell -NoProfile -ExecutionPolicy Bypass -File start-guard.ps1 -ConfigPath guard.local.toml`。新会话接入代理 `http://127.0.0.1:15725/v1`，面板 <http://127.0.0.1:15726>。默认只尝试一次；缺失最终回显也会拒绝返回。先使用 `python -m route_scope guard-demo` 理解行为，详细配置见 [路由网关](ROUTING-GUARD.md)。

## 4. 读懂结果

- 请求强度来自实际请求 body，未声明与 null 分开显示。
- 首包值和最终值分别记录；首包 high、最终 low 仍可能被判为较低。
- 未知强度字符串原样保留，不强行排序；tokens 为 0 与未返回不同。
- 捕获不完整、上游错误和回显缺失不能视为成功。
- 模型名称与强度都是协议字段，不能证明内部真实模型、账号或算力。

## 5. 隐私、排错与升级

正文可能包含代码和提示词。`watch` / `serve` 可加 `--metadata-only` 避免保存新请求正文，旧记录不会自动清除。不要上传 `data/`、截图或导出文件中的真实内容。

常见问题见 [TROUBLESHOOTING.md](TROUBLESHOOTING.md)。升级前先按对应模式正常停止本项目，保留本地配置、数据及恢复记录，然后 `git pull --ff-only` 和 `python -m pip install -e .`。检查 `python -m route_scope --version` 后再启动。
