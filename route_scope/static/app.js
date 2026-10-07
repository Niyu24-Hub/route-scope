"use strict";
const $ = id => document.getElementById(id);
let items = [], filter = "all", paused = false, detail = null, watch = null, readErrors=[];
const fmt = new Intl.DateTimeFormat("zh-CN", {timeZone:"Asia/Shanghai",hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false});
const fmtDate = new Intl.DateTimeFormat("zh-CN", {timeZone:"Asia/Shanghai",year:"numeric",month:"2-digit",day:"2-digit"});
const el = (tag, text, cls) => { const n=document.createElement(tag); if(text!==undefined)n.textContent=text; if(cls)n.className=cls; return n; };
const effort = (x, missing="未返回") => !x?.present ? missing : x.value === null ? "null" : typeof x.value === "string" ? (x.value || '""（空值）') : "无效字段";
function badge(x, missing){const v=effort(x,missing);return el("span",v,"pill "+(["none","minimal","low","medium","high","xhigh","max"].includes(v)?v:""));}
const audit = r => r.outbound_verdict || r.verdict;
const needsReview = r => !["match","lower","changed","no_echo"].includes(audit(r));
function clientType(record){
  const agent=record.client||record.request_headers?.["user-agent"]||"";
  const claude=/\bclaude(?:[-_ ]cli)?(?:[\/\s;]|$)/i.test(agent);
  const codex=/\bcodex(?:[-_ ](?:desktop|tui|cli(?:_rs)?))?(?:[\/\s;]|$)/i.test(agent);
  return claude!==codex?(claude?"claude":"codex"):"unknown";
}
const clientNames={claude:"Claude",codex:"Codex",unknown:"未识别"};
function render(){
  $("total").textContent=items.length;
  $("matches").textContent=items.filter(x=>audit(x)==="match").length;
  $("changes").textContent=items.filter(x=>["lower","changed"].includes(audit(x))||x.model_changed).length;
  $("no-echo").textContent=items.filter(x=>audit(x)==="no_echo").length;
  $("unknown").textContent=items.filter(needsReview).length;
  const session=$("sessions").value, source=$("sources").value, client=$("clients").value, selectedEffort=$("efforts").value, q=$("search").value.toLowerCase();
  const visible=items.filter(x=>(!client||clientType(x)===client)&&(!source||(x.capture_host||x.source)===source)&&(!session||x.session===session)&&(!selectedEffort||JSON.stringify(x.requested?.value)===selectedEffort)&&(!q||JSON.stringify(x).toLowerCase().includes(q))&&(filter==="all"||(filter==="changed"?(["lower","changed"].includes(audit(x))||x.model_changed):filter==="no_echo"?audit(x)==="no_echo":needsReview(x))));
  $("rows").replaceChildren();
  for(const r of visible){
    const row=el("tr",undefined,audit(r));
    const when=el("td",fmt.format(new Date(r.started_at)),"time");when.append(el("small",fmtDate.format(new Date(r.started_at))),el("small",r.capture_host|| (r.source==="websocket"?"WebSocket":r.source==="wsl-passive"?"WSL 旁路 / HTTP":r.source==="windows-passive"?"Windows 旁路 / HTTP":"HTTP / SSE")));row.append(when);
    const clientCell=el("td",clientNames[clientType(r)],"client-name");clientCell.title="User-Agent: "+(r.client||"未提供");row.append(clientCell);
    const model=el("td",undefined,"model-cell");model.append(el("span",r.requested_model||"未声明","model"),el("small",r.capture_boundary==="client_to_ccswitch"?"客户端 → CC Switch":"观测点请求 body.model"));row.append(model);
    const returned=el("td",undefined,"model-cell");
    returned.append(el("span",r.returned_model||r.first_model||"未返回模型字段","model"));
    if(r.returned_model){
      returned.append(el("small",r.first_event==="message_start"?"message_start.message.model":"响应 model 字段"));
      if(r.first_model&&r.first_model!==r.returned_model)returned.append(el("small","首包："+r.first_model));
    }else if(r.first_model){returned.append(el("small","仅首包 · 最终模型未返回"));}
    if(r.model_changed)returned.append(el("small","与请求模型名称不同","amber"));
    row.append(returned);
    for(const key of ["requested","first","final"]){
      const cell=el("td");
      const missing=key==="requested"?"未声明":r.protocol==="anthropic_messages"?"标准协议无回显":"未返回";
      cell.append(badge(r[key],missing));
      if(key==="requested"){
        cell.append(el("small",r.requested?.path||"请求中没有该字段"));
        if(r.protocol==="anthropic_messages"){
          if(r.request_thinking){for(const [name,value] of Object.entries(r.request_thinking))cell.append(el("small",`thinking.${name}: ${JSON.stringify(value)}`));}
          else cell.append(el("small","旧记录未提取 thinking 设置"));
          if(r.message_efforts?.length)cell.append(el("small",`另有 ${r.message_efforts.length} 处消息级 effort · 详见证据`,"amber"));
        }
      }else if(r.protocol==="anthropic_messages"&&r[key]?.present){cell.append(el("small","供应商扩展回显"));}
      row.append(cell);
    }
    if(r.forwarded_requested)row.children[4].append(el("small","实际出站："+effort(r.forwarded_requested,"未声明")));
    const inProgress=["sending","streaming"].includes(r.state);
    const tokens=el("td",r.reasoning_tokens==null?"—":Number(r.reasoning_tokens).toLocaleString());
    tokens.title=r.reasoning_tokens_source||"接口未报告推理用量";
    tokens.append(el("small",r.reasoning_tokens_source?.endsWith("thinking_tokens")?"thinking · 接口报告":"reasoning · 接口报告"),el("small",r.duration_ms===undefined?(inProgress?"进行中":"耗时未知"):(r.duration_ms/1000).toFixed(1)+" 秒"));row.append(tokens);
    const result=el("td");result.append(el("span",r.outbound_verdict_label||r.verdict_label,"result "+audit(r)),el("small",(r.http_status?"HTTP "+r.http_status:inProgress?"等待响应":"未捕获响应头")+(r.model_changed?" · 模型名称不同":"")));row.append(result);
    if(r.routing){const labels={admitted:"回显合格 · 已放行",rejected:"未通过核验 · 未交付",withheld:"完整响应核验中",cancelled:"客户端断开",blocked:"本地拦截 · 未请求上游",upstream_error_forwarded:"上游错误已返回"};result.append(el("small",`${r.routing.route||'本地策略'} · 尝试 ${r.routing.attempt} · ${labels[r.routing.delivery]||r.routing.delivery}${r.routing.client_status?' · 客户端 HTTP '+r.routing.client_status:''}`));}
    const end=el("td"),button=el("button","查看 ↗","detail-button");button.addEventListener("click",()=>openDetail(r.id));end.append(button,el("small",r.session?r.session.slice(0,8):"无会话标识"));row.append(end);$("rows").append(row);
  }
  $("empty").classList.toggle("hidden",visible.length>0);
  if(readErrors.length&&!visible.length){$("empty").querySelector("h2").textContent="采集数据读取异常";$("empty").querySelector("p").textContent="后端可能已抓到请求，但面板未能读取。请查看上方错误信息，不能据此认定请求数为 0。";}else if(items.length && !visible.length){$("empty").querySelector("h2").textContent="没有符合筛选条件的请求";}else{$("empty").querySelector("h2").textContent=watch?.paused?"抓取已暂停":"等待第一条请求";}
  $("shown").textContent=visible.length+" / "+items.length+" 条";
}
function renderWatch(value){
  watch=value;$("watch-panel").classList.toggle("hidden",!watch);
  if(!watch)return;
  const stale=watch.stale||watch.state!=="running";
  $("capture-toggle").disabled=stale;$("capture-toggle").textContent=watch.paused?"继续抓取":"暂停抓取";
  $("watch-message").textContent=stale?"抓取服务已停止或心跳中断；下方保留历史记录。":watch.paused?"已暂停抓取。原应用和代理继续正常运行。":"持续自动发现监听端口，新请求会自动记录。无需改动现有代理配置。";
  const labels={starting:"正在启动",capturing:"正在抓取",waiting_traffic:"已就绪 · 等待流量",waiting_listener:"等待 CC Switch 启动",paused:"已暂停",unavailable:"此来源不可用",recovering:"正在恢复捕获接口",stopped:"已停止"};
  $("workers").replaceChildren();
  for(const worker of watch.workers||[]){
    const card=el("article",undefined,"worker-card"),state=worker.stale&&worker.state!=="stopped"?"心跳中断":labels[worker.state]||worker.state;
    card.append(el("strong",worker.name),el("span",state,"worker-state "+(["capturing","waiting_traffic"].includes(worker.state)&&!worker.stale?"green":"amber")));
    card.append(el("p",(worker.ports?.length?"端口 "+worker.ports.join(" / "):"尚未发现监听端口")+" · "+(Object.keys(worker.observed_interfaces||{}).join(", ")||"尚无实际流量")));
    if(worker.error)card.append(el("p",worker.error,"worker-error"));
    if(worker.snapshot_error)card.append(el("p","快照发布失败："+worker.snapshot_error+"；原记录仍保存在采集端数据库。","worker-error"));
    const errors=Object.entries(worker.errors||{}).map(([k,v])=>`${k}: ${v}`).join(" · ");
    if(errors)card.append(el("p","解析提示："+errors,"worker-error"));
    card.append(el("small",`本次请求 ${worker.stats?.requests||0} · 完整 HTTP 返回 ${worker.stats?.responses||0} · 内核丢包 ${worker.kernel_drops??"未知"} · 自动恢复 ${worker.restart_count||0}`));
    if(worker.last_request_epoch)card.append(el("small","最近请求 "+fmt.format(new Date(worker.last_request_epoch*1000))));
    $("workers").append(card);
  }
  for(const error of watch.setup_errors||[])$("workers").append(el("p",error,"worker-error"));
  $("empty").querySelector("p").textContent=watch.paused?"点击继续抓取后，会从新的请求开始观察。":"正常使用 Codex 或 Claude 即可。已有会话不必重启；请先查看上方各来源的覆盖状态。";
}
function renderRouting(routing){
  $("routing-panel").classList.toggle("hidden",!routing);if(!routing)return;
  $("routing-message").textContent=`最低门槛 ${routing.minimum_effort} · 更高的原始请求保持不变 · 最多 ${routing.max_attempts} 次尝试 · ${routing.raise_effort?'已启用明确的出站强度提升':'保持原始请求强度'}。`;
  $("route-preference").replaceChildren(el("option","自动选择"));$("route-preference").firstChild.value="";
  $("routing-routes").replaceChildren();
  for(const route of routing.routes){
    const option=el("option",route.name);option.value=route.name;$("route-preference").append(option);
    const card=el("article",undefined,"worker-card");card.append(el("strong",route.name),el("p",route.upstream));
    card.append(el("small",route.auth_source==='incoming'?'使用 CC Switch 传入的认证':route.key_configured?'独立 Key 环境变量已配置':'独立 Key 环境变量未配置'));
    const samples=routing.health.filter(h=>h.route===route.name);
    if(!samples.length)card.append(el("small","暂无观察记录；不认定为高算力账号"));
    for(const h of samples){const seconds=Math.max(0,Math.ceil((h.cooldown_until||0)-Date.now()/1000));const recent=h.recent||[];card.append(el("p",`${h.model} / ${h.target}：近期 ${recent.filter(Boolean).length}/${recent.length} 次回显合格 · 累计 ${h.accepted}/${h.observations}${seconds?' · 冷却 '+seconds+' 秒':''}`));}
    $("routing-routes").append(card);
  }
  $("route-preference").value=routing.preferred_route||"";
}
async function refresh(){
  if(paused)return;
  try{
    const [a,b]=await Promise.all([fetch("/api/captures"),fetch("/api/status")]);if(!a.ok||!b.ok)throw Error("HTTP error");
    const captures=await a.json();items=captures.items;readErrors=captures.errors||[];const s=await b.json();
    $("capture-read-errors").classList.toggle("hidden",!readErrors.length);
    $("capture-read-errors").textContent=readErrors.map(e=>`${e.source}: ${e.message} (${e.error})${e.using_cached_records?' · 当前显示上次成功读取的数据':''}`).join('；');
    renderWatch(s.watch);
    renderRouting(s.routing);
    const passive=!!watch||items.some(r=>r.capture_boundary==="client_to_ccswitch");document.querySelector(".route").classList.toggle("hidden",passive);$("passive-boundary").classList.toggle("hidden",!passive);
    $("proxy").textContent=s.viewer?"外部抓取器 / 只读面板":s.proxy;$("connect-url").textContent=watch?"自动监听 CC Switch · 无需修改 base_url":s.viewer?"由 mitmproxy 或外部抓取器接入":s.proxy+"/v1";$("upstream").textContent=s.viewer?"见报文详情":s.upstream;$("active").textContent=s.viewer?items.filter(r=>["sending","streaming"].includes(r.state)).length:s.active;$("retention").textContent="最近 "+s.retention+" 条 · "+(s.capture_bodies?"保留正文":"仅元数据");
    const selected=$("sessions").value;$("sessions").replaceChildren(el("option","所有会话"));$("sessions").firstChild.value="";
    for(const value of new Set(items.map(x=>x.session).filter(Boolean))){const option=el("option",value.slice(0,12));option.value=value;$("sessions").append(option);}$("sessions").value=selected;
    const selectedEffort=$("efforts").value;$("efforts").replaceChildren(el("option","所有请求强度"));$("efforts").firstChild.value="";
    for(const value of [...new Set(items.map(x=>x.requested?.value).filter(x=>typeof x==="string"))].sort()){const option=el("option",value||'""（空值）');option.value=JSON.stringify(value);$("efforts").append(option);}$("efforts").value=selectedEffort;
    const selectedSource=$("sources").value;$("sources").replaceChildren(el("option","所有来源"));$("sources").firstChild.value="";
    for(const value of [...new Set(items.map(x=>x.capture_host||x.source).filter(Boolean))]){const option=el("option",value);option.value=value;$("sources").append(option);}$("sources").value=selectedSource;
    $("connection").textContent="观测服务在线";$("updated").textContent="更新于 "+fmt.format(new Date());render();
  }catch{$("connection").textContent="服务未连接 · 正在重试";}
}
async function openDetail(id){try{const resp=await fetch("/api/captures/"+encodeURIComponent(id));if(!resp.ok)throw Error();detail=await resp.json();$("detail-summary").textContent=clientNames[clientType(detail)]+" · "+(detail.capture_host||detail.source)+" · 观测点请求模型："+(detail.requested_model||"未声明")+" · 返回模型："+(detail.returned_model||detail.first_model||"未返回")+" · "+detail.verdict_label;showDetail("evidence");$("detail").showModal();}catch{$("connection").textContent="记录已过期或服务未连接";}}
function showDetail(view){
  let value;
  if(view==="request")value={headers:detail.request_headers,body:detail.request_body,bytes:detail.request_bytes,sha256:detail.request_sha256,truncated:detail.request_truncated,actual_outbound:detail.forwarded_requested?{effort:detail.forwarded_requested,headers:detail.forwarded_request_headers,body:detail.forwarded_request_body,sha256:detail.forwarded_sha256}:undefined};
  else if(view==="response")value={headers:detail.response_headers,body:detail.response_body,output_text:detail.output_text,wire_sha256:detail.response_sha256,wire_bytes:detail.response_wire_bytes,truncated:detail.response_truncated};
  else {value={client_type:clientType(detail),client_type_origin:"captured_user_agent",...detail};for(const k of ["request_headers","response_headers","request_body","response_body","output_text"])delete value[k];}
  $("detail-content").textContent=JSON.stringify(value,null,2);document.querySelectorAll("[data-view]").forEach(n=>n.classList.toggle("selected",n.dataset.view===view));
}
$("pause").onclick=()=>{paused=!paused;$("pause").textContent=paused?"继续刷新":"暂停刷新";if(!paused)refresh();};
$("search").oninput=render;$("sessions").onchange=render;$("efforts").onchange=render;$("sources").onchange=render;$("clients").onchange=render;
$("capture-toggle").onclick=async()=>{
  $("capture-toggle").disabled=true;
  try{const r=await fetch('/api/watch/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:watch?.paused?'resume':'pause'})});if(!r.ok)throw Error();paused=false;$("pause").textContent="暂停刷新";await refresh();}
  catch{$("watch-message").textContent="未能改变抓取状态，请确认抓取服务仍在运行。";}
  finally{$("capture-toggle").disabled=false;}
};
document.querySelectorAll("[data-filter]").forEach(n=>n.onclick=()=>{filter=n.dataset.filter;document.querySelectorAll("[data-filter]").forEach(b=>b.classList.toggle("selected",b===n));render();});
document.querySelectorAll("[data-view]").forEach(n=>n.onclick=()=>showDetail(n.dataset.view));
$("detail-close").onclick=()=>$("detail").close();
$("route-preference").onchange=async()=>{
  try{const r=await fetch('/api/routing/preference',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({route:$("route-preference").value||null})});if(!r.ok)throw Error();renderRouting(await r.json());}
  catch{$("routing-message").textContent="无法更新路由偏好，请检查网关状态。";}
};
refresh();setInterval(refresh,1000);
