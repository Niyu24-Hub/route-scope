"""Evidence-based route selection and response admission, not compute attestation."""
import asyncio
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import time
import weakref
import uuid
from urllib.parse import urlsplit

from aiohttp import ClientTimeout, web
from yarl import URL

from .evidence import Observation, SECRET_KEYS, decode_request, effort, fingerprint, obj, safe_headers, verdict
from .runtime import atomic_json, read_json

DEFAULT_ORDER=['none','minimal','low','medium','high','xhigh','max']


class PolicyError(ValueError):
    pass


@dataclass
class Route:
    name: str
    upstream: str
    key_env: str | None = None


def stateless_text(payload, headers):
    if headers.get('Cookie') or any(payload.get(k) for k in ('tools','previous_response_id','conversation','background')):
        return False
    if isinstance(payload.get('input'),str):
        return True
    messages=payload.get('messages')
    return bool(isinstance(messages,list) and messages and all(
        isinstance(m,dict) and m.get('role') in ('user','system','developer') and isinstance(m.get('content'),str) for m in messages))


def origin(url):
    p=urlsplit(url)
    return (p.scheme,p.hostname,p.port or (443 if p.scheme=='https' else 80))


def opaque_context(payload):
    if payload.get('previous_response_id') or payload.get('conversation'):return True
    stack=[payload]
    while stack:
        value=stack.pop()
        if isinstance(value,dict):
            if (value.get('encrypted_content') or value.get('type') in ('function_call_output','tool_result')
                    or value.get('role') in ('tool','function') or value.get('tool_calls') or value.get('function_call')):
                return True
            stack.extend(value.values())
        elif isinstance(value,list):stack.extend(value)
    return False


class RoutingGuard:
    def __init__(self, config, store):
        self.config,self.store=config,store
        settings=dict(config.guard or {})
        allowed={'minimum_effort','raise_effort','models','effort_order','max_attempts','retry_stateless',
                 'cooldown_seconds','total_timeout','buffer_limit','session_ttl','max_parallel','history_window','health_ttl'}
        if set(settings)-allowed:raise PolicyError('未知 guard 配置项')
        self.order=settings.get('effort_order',DEFAULT_ORDER)
        if not isinstance(self.order,list) or not self.order or not all(isinstance(x,str) and x for x in self.order) or len(set(self.order))!=len(self.order):
            raise PolicyError('effort_order 必须是不重复的非空字符串列表')
        self.minimum=settings.get('minimum_effort','high')
        if self.minimum not in self.order:raise PolicyError('minimum_effort 不在比较顺序中')
        self.raise_effort=settings.get('raise_effort',False)
        self.models=settings.get('models',[])
        if not isinstance(self.models,list) or not self.models or not all(isinstance(x,str) and x for x in self.models):
            raise PolicyError('guard.models 必须明确列出允许控制的模型名')
        self.max_attempts=settings.get('max_attempts',1)
        if type(self.max_attempts) is not int or not 1<=self.max_attempts<=2:raise PolicyError('max_attempts 只允许 1 或 2')
        self.retry_stateless=settings.get('retry_stateless',False)
        parallel=settings.get('max_parallel',4)
        if type(parallel) is not int or not 1<=parallel<=16:raise PolicyError('max_parallel 范围为 1..16')
        self.capacity=asyncio.Semaphore(parallel)
        if type(self.raise_effort) is not bool or type(self.retry_stateless) is not bool:raise PolicyError('guard 开关必须为布尔值')
        self.cooldown=settings.get('cooldown_seconds',60)
        self.timeout=settings.get('total_timeout',600)
        self.limit=settings.get('buffer_limit',16*1024*1024)
        self.ttl=settings.get('session_ttl',3600)
        self.window=settings.get('history_window',20)
        self.health_ttl=settings.get('health_ttl',900)
        if type(self.window) is not int or not 1<=self.window<=100 or type(self.health_ttl) is not int or self.health_ttl<1:
            raise PolicyError('history_window 范围 1..100，health_ttl 必须为正整数')
        if any(type(v) is not int or v<1 for v in (self.cooldown,self.timeout,self.limit,self.ttl)):
            raise PolicyError('guard 时间和字节上限必须是正整数')
        if self.limit>64*1024*1024:raise PolicyError('buffer_limit 不得大于 64 MiB')
        self.routes=[]
        for value in config.routes or [{'name':'primary','upstream':config.upstream}]:
            if set(value)-{'name','upstream','key_env'}:raise PolicyError('未知 routes 配置项')
            route=Route(**value);p=urlsplit(route.upstream)
            if not route.name or any(r.name==route.name for r in self.routes):raise PolicyError('路由名必须唯一')
            if p.scheme not in ('https','http') or not p.hostname or p.username or p.password or p.query or p.fragment:
                raise PolicyError('路由 URL 必须是不含凭据和查询参数的 HTTP(S) 地址')
            if p.scheme=='http' and p.hostname not in ('localhost','127.0.0.1','::1'):
                raise PolicyError('非本机路由必须使用 HTTPS')
            if p.hostname in ('localhost','127.0.0.1','::1') and p.port in (config.proxy_port,config.dashboard_port):
                raise PolicyError('路由不能指向自身')
            if origin(route.upstream)!=origin(config.upstream) and not route.key_env:
                raise PolicyError('不同 origin 的备用路由必须配置独立 key_env，禁止转发原认证信息')
            self.routes.append(route)
        self.state_path=Path(config.data_dir)/'routing-health.json'
        self.state=read_json(self.state_path,{'health':{},'sessions':{},'responses':{}})
        for key in ('health','sessions','responses'):
            if not isinstance(self.state.get(key),dict):self.state[key]={}
        self.locks=weakref.WeakValueDictionary()

    def prepare(self, raw, headers, path):
        try:
            payload=json.loads(decode_request(raw,headers.get('Content-Encoding','').lower()))
        except (ValueError,TypeError):raise PolicyError('无法读取 JSON 请求体') from None
        if not isinstance(payload,dict):raise PolicyError('请求必须是 JSON 对象')
        if payload.get('model') not in self.models:raise PolicyError('该模型未列入 guard.models；未修改请求')
        if payload.get('background'):raise PolicyError('强度准入不支持后台响应')
        original=effort(payload)
        current=original['value']
        nested=obj(payload.get('reasoning')).get('effort')
        flat=payload.get('reasoning_effort')
        if nested is not None and flat is not None and nested!=flat:raise PolicyError('请求有冲突的 effort 字段')
        if current is not None and current not in self.order:raise PolicyError('未知强度顺序：请明确配置 effort_order；未猜测或降档')
        if current is None or self.order.index(current)<self.order.index(self.minimum):
            if not self.raise_effort:raise PolicyError('请求低于门槛或未声明强度，raise_effort 未启用')
            target=self.minimum
        else:target=current
        if target!=current:
            if path.endswith('/responses'):
                if 'reasoning' in payload and not isinstance(payload['reasoning'],dict):raise PolicyError('reasoning 必须是对象')
                payload.setdefault('reasoning',{})['effort']=target
                if 'reasoning_effort' in payload:payload['reasoning_effort']=target
            else:
                payload['reasoning_effort']=target
                if nested is not None:payload['reasoning']['effort']=target
            wire=json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode()
        else:
            wire=decode_request(raw,headers.get('Content-Encoding','').lower())
        return payload,wire,target,original

    def session(self,payload,headers):
        sid=next((headers.get(k) for k in ('session_id','session-id','thread-id','x-session-id') if headers.get(k)),None)
        sid=sid or obj(payload.get('metadata')).get('session_id') or payload.get('prompt_cache_key')
        previous=payload.get('previous_response_id')
        return fingerprint(sid,self.store.salt) if sid else None, fingerprint(previous,self.store.salt) if previous else None

    def key(self,route,headers):
        if route.key_env:
            value=os.environ.get(route.key_env)
            return ('Bearer '+value) if value else None
        return headers.get('Authorization') or headers.get('x-api-key') or headers.get('api-key')

    def bucket(self,route,auth,model,target):
        return fingerprint(json.dumps([route.name,route.upstream,auth,model,target]),self.store.salt)

    def choose(self,payload,headers,target,excluded):
        now=time.time();sid,prev=self.session(payload,headers)
        # Persist only accepted-route affinity. Do not rebind existing opaque contexts.
        pin=self.state['responses'].get(prev) if prev else None
        pin=pin or self.state['sessions'].get(sid)
        if pin and pin.get('expires',0)<now:pin=None
        if pin is None and opaque_context(payload):return None
        candidates=[]
        for i,route in enumerate(self.routes):
            auth=self.key(route,headers)
            if route.key_env and auth is None:continue
            credential=fingerprint(auth or '',self.store.salt)
            if pin and (route.name!=pin['route'] or credential!=pin['credential']):continue
            if not pin and headers.get('Cookie') and i!=0:continue
            bucket=self.bucket(route,auth,payload['model'],target)
            if bucket in excluded:continue
            health=self.state['health'].get(bucket,{})
            if health.get('cooldown_until',0)>now:continue
            recent=health.get('recent',[]) if now-health.get('last_seen',0)<=self.health_ttl else []
            score=(sum(recent)+1)/(len(recent)+2)
            if recent and not recent[-1]:score*=.25  # Recent binding changes outweigh old successes.
            preferred=route.name==self.state.get('preferred_route')
            candidates.append((preferred,score,-i,route,auth,bucket,credential))
        if not candidates:return None
        return max(candidates,key=lambda x:x[:3])[3:]

    def assess(self,record,target,model):
        if record.get('transport_error'):return False,'transport_error'
        if not 200<=(record.get('http_status') or 0)<300:return False,'upstream_http_error'
        if record.get('state')!='completed':return False,'response_not_completed'
        if record.get('upstream_error') or record.get('incomplete_details'):return False,'response_not_completed'
        if record.get('parse_errors'):return False,'response_not_parseable'
        if record.get('returned_model')!=model:return False,'model_name_mismatch'
        actual=obj(record.get('final')).get('value')
        if actual not in self.order:return False,'effort_missing_or_unranked'
        if self.order.index(actual)<self.order.index(target):return False,'effort_below_request'
        return True,'echo_admitted_compute_unverified'

    def remember(self,route,bucket,credential,payload,target,record,accepted,reason,headers):
        now=time.time();h=self.state['health'].setdefault(bucket,{'route':route.name,'model':payload['model'],'target':target,'observations':0,'accepted':0})
        hints=record.get('account_hints') or {}
        old_hints=h.get('last_account_hints') or {}
        if now-h.get('last_seen',0)>self.health_ttl or (hints and old_hints and hints!=old_hints):
            h['recent']=[]
        h['recent']=(h.get('recent',[])+[bool(accepted)])[-self.window:]
        if hints:h['last_account_hints']=hints
        h.update(observations=h['observations']+1,last_reason=reason,last_seen=now)
        if accepted:
            h['accepted']+=1;h['cooldown_until']=0
            sid,_=self.session(payload,headers)
            pin={'route':route.name,'credential':credential,'expires':now+self.ttl}
            if sid:self.state['sessions'][sid]=pin
            if record.get('response_id'):self.state['responses'][fingerprint(record['response_id'],self.store.salt)]=pin
        else:h['cooldown_until']=now+self.cooldown
        for key in ('sessions','responses'):
            self.state[key]={k:v for k,v in self.state[key].items() if v.get('expires',0)>now}
            if len(self.state[key])>2000:
                self.state[key]=dict(sorted(self.state[key].items(),key=lambda kv:kv[1]['expires'])[-2000:])
        if len(self.state['health'])>2000:
            self.state['health']=dict(sorted(self.state['health'].items(),key=lambda kv:kv[1].get('last_seen',0))[-2000:])
        atomic_json(self.state_path,self.state)

    def public_state(self):
        return {'mode':'complete_response_admission','minimum_effort':self.minimum,'raise_effort':self.raise_effort,
                'models':self.models,'max_attempts':self.max_attempts,'retry_stateless':self.retry_stateless,
                'routes':[{'name':r.name,'upstream':r.upstream,'key_configured':not r.key_env or bool(os.environ.get(r.key_env)),
                           'auth_source':'environment' if r.key_env else 'incoming'} for r in self.routes],
                'health':list(self.state['health'].values()),'preferred_route':self.state.get('preferred_route'),'actual_compute':'unverified'}

    def prefer(self,name):
        if name is not None and name not in [r.name for r in self.routes]:raise PolicyError('未知路由')
        self.state['preferred_route']=name
        atomic_json(self.state_path,self.state)
        return self.public_state()

    def blocked(self,request,raw,reason,status,forwarded=None,target=None):
        obs=Observation(request.method,request.path,request.headers,raw,self.store.salt,self.config.capture_bodies,self.config.body_limit,'guard-gateway')
        record=obs.finish()
        record.update(state='blocked',verdict='blocked',verdict_label='本地策略拦截 · 未请求上游',
            capture_boundary='client_to_gateway',upstream_request_observed=False,
            routing={'route':None,'attempt':0,'delivery':'blocked','reason':reason,'client_status':status,'required_effort':target})
        if forwarded is not None:record['planned_requested']=effort(forwarded)
        self.store.save(record)
        return record['id']

    def save(self,obs):
        record=obs.refresh()
        if record.get('forwarded_requested'):
            record['outbound_verdict'],record['outbound_verdict_label']=verdict({**record,'requested':record['forwarded_requested']})
        self.store.save(record)

    async def forward(self,service,request):
        from .server import forward_headers
        host=urlsplit('http://'+request.host).hostname
        if host not in ('127.0.0.1','localhost','::1') or (request.headers.get('Origin') and request.headers['Origin']!=f'{request.scheme}://{request.host}'):
            return web.json_response({'error':{'code':'guard_local_client_required','message':'拒绝跨站点调用本机收费路由'}},status=403)
        if request.method!='POST' or not request.path.endswith(('/responses','/chat/completions')) or request.headers.get('Upgrade'):
            return web.json_response({'error':{'code':'guard_unsupported_endpoint','message':'此端口只接收 HTTP POST Responses/Chat 请求，不能绕过强度核验'}},status=400)
        if request.content_type!='application/json':
            return web.json_response({'error':{'code':'guard_json_required','message':'Content-Type 必须为 application/json'}},status=415)
        raw=await request.read()
        try:payload,wire,target,original=self.prepare(raw,request.headers,request.path)
        except PolicyError as exc:
            capture_id=self.blocked(request,raw,str(exc),400)
            return web.json_response({'error':{'code':'guard_policy','message':str(exc),'capture_ids':[capture_id]}},status=400)
        can_retry=self.retry_stateless and stateless_text(payload,request.headers)
        attempts=self.max_attempts if can_retry else 1
        excluded=set();ids=[];task=asyncio.current_task();group_id=uuid.uuid4().hex
        deadline=time.monotonic()+self.timeout
        sid,prev=self.session(payload,request.headers)
        lock_key=sid or prev or fingerprint(os.urandom(32),self.store.salt)
        lock=self.locks.get(lock_key)
        if lock is None:lock=asyncio.Lock();self.locks[lock_key]=lock
        # Serialize only the same conversation; independent requests keep concurrency.
        async with lock,self.capacity:
            service.active.add(task)
            try:
                for attempt in range(attempts):
                    choice=self.choose(payload,request.headers,target,excluded)
                    if not choice:
                        if not ids:ids.append(self.blocked(request,raw,'no_eligible_route_or_unbound_context',422,payload,target))
                        break
                    route,auth,bucket,credential=choice;excluded.add(bucket)
                    remaining=deadline-time.monotonic()
                    if remaining<=0:break
                    obs=Observation(request.method,request.path,request.headers,raw,self.store.salt,service.config.capture_bodies,service.config.body_limit,'guard-gateway')
                    if auth:obs.secrets.extend([auth,auth.removeprefix('Bearer ')])
                    headers=forward_headers(request.headers)
                    headers.popall('Content-Encoding',None)
                    if route.key_env:
                        for name in list(headers.keys()):
                            if SECRET_KEYS.search(name) or name.lower() in ('chatgpt-account-id','openai-organization','openai-project'):
                                headers.popall(name,None)
                        headers['Authorization']=auth
                    obs.record.update(upstream=route.upstream,capture_boundary='gateway_to_configured_upstream',upstream_request_observed=True,
                        forwarded_requested=effort(payload),forwarded_sha256=hashlib.sha256(wire).hexdigest(),
                        forwarded_request_headers=safe_headers(headers,self.store.salt,obs.secrets),
                        forwarded_request_body=obs.body_view(wire) if obs.capture else None,
                        routing={'route':route.name,'request_group':group_id,'attempt':attempt+1,'required_effort':target,'rewritten':target!=original['value'],'delivery':'withheld','actual_compute':'unverified'})
                    ids.append(obs.record['id']);self.save(obs)
                    chunks=[];size=0;transport=None;status=502;response_headers={}
                    signature=None
                    try:
                        async with service.client.post(URL(route.upstream.rstrip('/')+request.raw_path,encoded=True),data=wire,headers=headers,
                                proxy=service.config.outbound_proxy,allow_redirects=False,timeout=ClientTimeout(total=remaining)) as upstream:
                            status=upstream.status;response_headers=forward_headers(upstream.headers)
                            obs.headers(status,upstream.headers)
                            async for chunk in upstream.content.iter_any():
                                if request.transport is None or request.transport.is_closing():raise asyncio.CancelledError()
                                size+=len(chunk)
                                if size>self.limit:raise PolicyError('response_buffer_limit')
                                chunks.append(chunk);obs.feed(chunk)
                                current=json.dumps([obs.record['first'],obs.record['final'],obs.record['state']])
                                if current!=signature:self.save(obs);signature=current
                    except asyncio.CancelledError:
                        obs.record['routing'].update(delivery='cancelled',reason='client_disconnected')
                        obs.finish('cancelled');self.save(obs);raise
                    except Exception as exc:transport=type(exc).__name__
                    record=obs.finish(transport)
                    accepted,reason=self.assess(record,target,payload['model'])
                    self.remember(route,bucket,credential,payload,target,record,accepted,reason,request.headers)
                    obs.record['routing'].update(delivery='admitted' if accepted else 'rejected',reason=reason,client_status=status if accepted else None)
                    self.save(obs)
                    if accepted or (400<=status<500 and transport is None):
                        # Preserve real 400/401/403/429 and Retry-After; never rotate to evade them.
                        if not accepted:
                            obs.record['routing'].update(delivery='upstream_error_forwarded',client_status=status);self.save(obs)
                        return web.Response(status=status,headers=response_headers,body=b''.join(chunks))
                    if reason not in ('effort_below_request','effort_missing_or_unranked','model_name_mismatch'):
                        break
                if not ids:ids.append(self.blocked(request,raw,'request_time_budget_exhausted',422,payload,target))
                last=self.store.get(ids[-1])
                if last and last.get('routing',{}).get('delivery')=='rejected':
                    last['routing']['client_status']=422;self.store.save(last)
                return web.json_response({'error':{'code':'guard_no_verified_route',
                    'message':'没有完整返回满足所请求强度的可用路由；未交付模型输出。请检查路由状态或等待冷却结束。',
                    'required_effort':target,'capture_ids':ids}},status=422)
            finally:service.active.discard(task)
