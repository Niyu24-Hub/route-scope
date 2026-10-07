# 尽量获得满足请求强度的返回：路由选择与完整回显核验

版本 0.3.0 增加一个**显式启用**的应用层网关。自动旁路 `watch` 保持原有行为，不能在原链路外强制换号。

## 实际能做什么

1. **保留原始请求，明确记录出站策略。** 请求中的原始 effort 不覆盖。启用 `raise_effort=true` 后，低于 `minimum_effort` 的请求在出站时提高到门槛；更高的原请求保持不变。例如原请求 low、最低 high，则实际发送 high；原请求 xhigh 则仍发 xhigh，不能接受 high 冒充达标。
2. **按近期回显选择已配置的路由。** 路由必须由用户明确配置，只使用传入认证或指定环境变量里的授权 Key。观察按路由、凭据指纹、模型和目标强度分别统计；近期回显合格的路由优先，不合格路由进入冷却期。不同 Key 不等于不同的上游账号，不假设能控制号池绑定。
3. **完整返回通过核验才交付。** 原始模型名必须匹配，响应成功完成、字段存在且不低于要求，才原样交付响应 body。首包 high、最终 low 会被拒绝；缺失字段、解析不完整、模型名称不匹配均不能作为通过证据。返回的强度字段不能用于证明真实算力。
4. **不在原有工具会话里偷偷换号。** 成功选择的路由会与会话/响应标识绑定。已有 `previous_response_id`、加密上下文等若无法确认既有绑定，直接拒绝，不猜测账号。绑定路由冷却时返回明确错误；不会把同一上下文送给不明账号尝试解密。
5. **可选的有限重试。** 默认仅一次请求。只有开启 `retry_stateless=true` 且 `max_attempts=2`，并且是无工具、无 Cookie、无已有响应状态的简单文本请求，才允许尝试第二条已配置路由。不重复同一条候选路由。401/403/429 原样返回，不轮换 Key 绕过它们。

这能减少“低回显结果被直接交付”的情况；不能保证任何一次请求都成功，也不能保证 anyrouter 真正提供所声称的算力。**只有一个不可控号池入口时，没有客户端技巧能保证指定内部账号。** 新实现会宁可明确失败，也不把 low 回显改写成 high 给客户端。

## 如何启动

先复制 `guard.example.toml` 为 `guard.local.toml`，填写实际上游支持的模型名并检查路由。默认监听 **15725**，面板 **15726**，与已有 15722/15721/15927 分开。

```powershell
cd route-scope
.\start-guard.ps1 -ConfigPath guard.local.toml
# 如果 anyrouter 需要经过 mihomo：
.\start-guard.ps1 -ConfigPath guard.local.toml -OutboundProxy http://127.0.0.1:7890
```

首次接入时使用新会话，并在 CC Switch 中将**为新会话准备的供应商**的上游地址设为 `http://127.0.0.1:15725/v1`。保存原地址，恢复时改回 `https://anyrouter.top/v1`。程序不会自动改动正在运行的 CC Switch 或 Codex 会话。

链路为：`Codex → CC Switch → Route Scope 15725 → mihomo（如配置）→ anyrouter`。同时获得原始入站请求、实际交给 HTTP 客户端的出站载荷及其哈希、真实上游返回、每次尝试的路由、拒绝原因和客户端最终状态。

WSL 可用相同命令：`python -m route_scope serve --config guard.local.toml`，使用已有安装好依赖的 Python 环境；本机地址必须指向运行网关的同一个系统。

## 配置示例

```toml
[guard]
models = ["your-model-name"]
minimum_effort = "high"
raise_effort = true
max_attempts = 1
retry_stateless = false
cooldown_seconds = 60
total_timeout = 600
buffer_limit = 16777216
max_parallel = 4
session_ttl = 3600
history_window = 20
health_ttl = 900

[[routes]]
name = "anyrouter-primary"
upstream = "https://anyrouter.top"

[[routes]]
name = "anyrouter-alternative"
upstream = "https://anyrouter.top"
key_env = "ROUTE_SCOPE_ANYROUTER_ALTERNATIVE_KEY"
```

只有配置并提供第二条路由的 Key 后才存在备用选择。密钥放在启动网关进程的环境变量中，不放进 TOML、浏览器表单或命令行参数；环境变量变化需要由新进程读取，保持同一会话时不应直接更换凭据。跨 origin 的路由必须提供独立凭据，网关不会将原始认证、Cookie 或账号头泄漏给备用 origin。

面板可选择“新会话路由偏好”。偏好不能绕过冷却、缺失凭据或已有绑定限制，不会把正在运行的上下文突然转到另一个 Key。响应门槛缺失时返回本地 422；上游原本的 400/401/403/429 保留真实状态。

新工具会话可以在首次发送前选择健康的授权路由，但不会在这次请求失败后自动重放工具请求。评分默认使用最近 20 次结果，旧证据 900 秒后不再主导选择；最近一次不合格会额外降低优先级，上游显式账号提示变化时重置近期窗口。首包和最终事件会及时写入面板，但模型输出保持缓冲，直到完成核验。

一个客户端请求的多次尝试用 `request_group` 关联。被拒绝的中间尝试没有客户端返回状态；只有真正结束该客户端请求的记录才标注 HTTP 状态，避免把“主路由拒绝、备用成功”错写成先给客户端返回 422。审计同时保留原始请求对比和实际出站对比；面板统计以实际出站对比为准。

`effort_order` 是显式比较顺序，默认包含常见字段值，不是任何模型的能力清单。未知档位可以在纯观察模式原样记录；主动门槛比较需要用户明确给出顺序。按 [OpenAI 官方 reasoning 文档](https://developers.openai.com/api/docs/guides/reasoning)，具体支持哪些档位取决于模型。别名和实际返回模型名必须匹配；本版本不会自行猜测两个不同模型名等价。

## 必须理解的代价

- 为防止首包合格、终包变低，严格模式缓冲完整响应再交付。首个可见内容会推迟到整个模型响应完成，客户端和 CC Switch 的首包/读取超时都需要足够长。特别要检查 CC Switch 的 streaming_first_byte_timeout；本项目不会自动修改它。
- 拒绝交付不能撤销上游已经消耗的 tokens，也不能撤销上游已执行的托管工具。因此工具请求不会自动重放；可选的第二次简单文本请求仍可能产生第二次费用。
- 当前支持 HTTP POST Responses / Chat 入口；WebSocket、后台响应、其他接口在此专用端口被明确拒绝。Chat 接口往往不回显 effort，严格核验可能无法放行；Codex Responses 是主要接入路径。
- 本网关选择的是用户配置的 API 路由，不是 anyrouter 内部账号；未集成号池内部账号选择或绑定接口。
- 冷却、偏好、成功率均依据近期**返回字段**，不能识别上游伪造的模型名或强度。

## 本地演示，不产生真实 API 费用

```powershell
.\.venv\Scripts\python.exe -m route_scope guard-demo
```

打开 [http://127.0.0.1:15834](http://127.0.0.1:15834)。模拟主路由首包 high、最终 low；网关拦截它，再对无状态文本请求尝试明确配置的模拟备用路由。面板展示原始值、实际出站值、attempt 1/2 与放行/拒绝记录。所有模型名称为 demo-model，所有上游为本机地址。

本地强度门槛失败采用 HTTP 422，而不是临时网络错误或冲突状态，以减少客户端不必要的自动重试；具体客户端是否重试仍由其配置决定。上游原生错误状态不改写。
