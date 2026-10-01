"""Local configuration UI. Inference goes directly to CLIProxyAPI, never through here."""
import copy, hashlib, hmac, json, os, re, shutil, sys, threading, time, tomllib
import urllib.error, urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 8318
LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
LOCK = threading.Lock()
SECTIONS = ('codex-api-key', 'openai-compatibility')
AUTH_FILE = 'codex-official.json'

class RouteError(Exception): pass

def read_json(path): return json.loads(path.read_text(encoding='utf-8-sig'))
def write_json(path, data):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)

def get_proxy_url():
    try:
        text = (ROOT / 'config.yaml').read_text(encoding='utf-8')
        m = re.search(r'(?m)^proxy-url:\s*["\']?([^"\'\r\n#]+)', text)
        return m.group(1).strip() if m else None
    except Exception:
        return None

def get_opener(entry=None):
    p = entry.get('proxy-url') if entry else None
    proxy = p if (p is not None and p != '') else get_proxy_url()
    if proxy:
        return urllib.request.build_opener(urllib.request.ProxyHandler({'http': proxy, 'https': proxy}))
    return urllib.request.build_opener()

def secrets(): return read_json(ROOT / '.local-secrets.json')
def api(path, method='GET', data=None):
    req = urllib.request.Request('http://127.0.0.1:8317' + path,
        data=json.dumps(data).encode() if data is not None else None, method=method,
        headers={'Authorization': 'Bearer ' + secrets()['management_key' if path.startswith('/v0/') else 'api_key'], 'Content-Type': 'application/json'})
    try:
        with LOCAL_OPENER.open(req, timeout=15) as r: return json.load(r)
    except urllib.error.HTTPError as e:
        raise RouteError('网关接口 HTTP ' + str(e.code)) from None
    except (OSError, ValueError): raise RouteError('无法连接本地网关，请先启动 CLIProxyAPI') from None

def revision(config):
    return hashlib.sha256(json.dumps({k:config.get(k,[]) for k in SECTIONS},sort_keys=True).encode()).hexdigest()

def find_entry(config, provider):
    if provider.get('section')=='auth-file':return {'name':AUTH_FILE}
    items = config.get(provider['section'], [])
    base = provider['base_url'].rstrip('/')
    # Same base-url may host two credentials (e.g. KKAPI GPT + CN copy); disambiguate via tag/name.
    if provider.get('tag'):matches=[v for v in items if v.get('base-url','').rstrip('/')==base and v.get('headers',{}).get('X-Route-Tag')==provider['tag']]
    elif provider.get('name'):matches=[v for v in items if v.get('base-url','').rstrip('/')==base and v.get('name')==provider['name']]
    else:matches=[v for v in items if v.get('base-url','').rstrip('/')==base]
    if len(matches) != 1: raise RouteError('供应商配置缺失或重复：' + provider['label'])
    return matches[0]

def plan_identity(p):return (p['section'],p['base_url'].rstrip('/'),p.get('tag'))
def entry_identity(section,entry):return (section,entry.get('base-url','').rstrip('/'),entry.get('headers',{}).get('X-Route-Tag'))

def auth_active():
    return json.loads((ROOT/'auth'/AUTH_FILE).read_text(encoding='utf-8-sig')).get('excluded_models') != ['*']

def active(entry, section):
    if section=='auth-file':return auth_active()
    return not entry.get('disabled',False) if section=='openai-compatibility' else '*' not in entry.get('excluded-models',[])

GROUPS=('gpt','deepseek','glm')
OFFICIAL_MODELS_URL='https://chatgpt.com/backend-api/codex/models?client_version=1.0.0'
# Image models are not in the Codex /models payload and cannot serve /v1/responses.
OFFICIAL_IMAGE_MODELS=['gpt-image-1.5','gpt-image-2','gpt-image-2.5','gpt-image-2.5-flare','gpt-image-2.5-sunburst']
UA='curl/8.4.0'

def leaf(upstream):return upstream.rsplit('/',1)[-1]

# 渠道头（head）：同一分组同时启用多家来源时，给客户端可见的模型 ID 加来源前缀，
# 让客户端和网关都能分辨"这个模型走哪一家"。完整设计与实测证据见
# app/core/sources.py 的 HEAD_SEP 段与 docs/evidence-p0-head-routing.txt。
#
# 为什么这里又写一份而不是 import sources：sources.py 依赖 core.bridge，bridge 又
# import 本文件，反向 import 会成环；而且本文件要能单独 python route_selector.py 跑起来
# 当独立选择页用，不能依赖 app/ 包。所以只复制这三行纯函数，**不要"去重"**。
HEAD_SEP='/'
def _client_id(head,alias):
    h=(head or '').strip()
    return h+HEAD_SEP+alias if h else alias

def belongs(leafname, upstream, group):
    low=leafname.lower()
    if group=='gpt':return low.startswith('gpt') or low.startswith('codex')
    if group=='glm':return low.startswith('glm')
    return low.startswith('deepseek')

def credentials(entry):
    if 'api-key' in entry:return entry['api-key'],entry.get('headers',{})
    first=entry.get('api-key-entries',[{}])[0]
    return first.get('api-key',''),entry.get('headers',{})

_OFFICIAL_CACHE=[0.0,None]
def official_meta():
    """Full Codex model entries from the official backend. Returns ({slug:entry}, error); 30s cache."""
    now=time.time()
    if _OFFICIAL_CACHE[1] is not None and now-_OFFICIAL_CACHE[0]<30:
        return _OFFICIAL_CACHE[1],None
    token=json.loads((ROOT/'auth'/AUTH_FILE).read_text(encoding='utf-8-sig')).get('access_token')
    if not token:return {},'官方凭据缺少 access_token'
    # config.yaml proxy-url exists because chatgpt.com is unreachable directly from this machine.
    opener=get_opener()
    req=urllib.request.Request(OFFICIAL_MODELS_URL,
        headers={'Authorization':'Bearer '+token,'User-Agent':'codex_cli_rs/1.0.0 (Windows 11; x86_64)',
                 'Accept':'application/json','originator':'codex_cli_rs'})
    last=None
    for _ in range(2):
        try:
            with opener.open(req,timeout=12) as r:
                payload=json.load(r)
            rows=payload.get('models') if isinstance(payload,dict) else None
            if not isinstance(rows,list):return {},'官方模型列表格式无效'
            meta={}
            for item in rows:
                if isinstance(item,dict) and isinstance(item.get('slug'),str) and item['slug']:meta[item['slug']]=item
                elif isinstance(item,str) and item:meta[item]={}
            _OFFICIAL_CACHE[:]=[now,meta]
            return meta,None
        except urllib.error.HTTPError as e:
            return {},'官方拉取失败：HTTP '+str(e.code)
        except (OSError,ValueError) as e:
            last=e;time.sleep(1)
    return {},'官方拉取失败：'+type(last).__name__

def fetch_official_models():
    meta,error=official_meta()
    if error:return [],error
    return list(meta),None

def fetch_upstream(provider, entry, opener=None):
    """Raw upstream /models fetch for one credential. Returns (rows, error); rows are unfiltered."""
    if provider['section']=='auth-file':
        slugs,error=fetch_official_models()
        if error:return [],error
        return [{'id':n,'context_length':None} for n in slugs],None
    key,headers=credentials(entry)
    if not key:return [],'缺少密钥'
    req=urllib.request.Request(provider['base_url'].rstrip('/')+'/models',
        headers={**headers,'Authorization':'Bearer '+key,'User-Agent':UA,'Accept':'application/json'})
    
    openers = [opener] if opener else [get_opener(entry), LOCAL_OPENER]
    payload = None
    for op in openers:
        try:
            with op.open(req, timeout=12) as r:
                payload = json.load(r)
                break
        except urllib.error.HTTPError as e:
            return [], '上游拉取失败：HTTP ' + str(e.code)
        except (OSError, ValueError):
            continue
    if payload is None:
        return [], '上游拉取失败：超时或无法连接'
    data=payload.get('data') if isinstance(payload,dict) else None
    if not isinstance(data,list):return [],'上游拉取失败：模型列表格式无效'
    rows=[]
    for item in data:
        if not isinstance(item,dict):continue
        upstream=item.get('id')
        if not isinstance(upstream,str) or not upstream:continue
        rows.append({'id':upstream,'context_length':item.get('context_length') or item.get('context_window')})
    return rows,None

def select_candidates(provider, rows):
    """Filter raw rows down to this row's column, deduping channel-prefixed names to one leaf entry."""
    known={m['name'] for m in provider.get('models',[])}
    merged={}
    for item in rows:
        upstream=item['id'];name=leaf(upstream)
        if not belongs(name,upstream,provider['group']):continue
        score=(upstream in known,'/' not in upstream,upstream)
        prev=merged.get(name)
        if prev is None or score>prev[0]:
            merged[name]=(score,{'name':upstream,'alias':name,'context_length':item.get('context_length')})
    return [merged[k][1] for k in sorted(merged)]

def upstream_models(provider, entry, opener=None):
    """Live /models fetch already filtered for one row. Failure yields an error, never a stale list."""
    rows,error=fetch_upstream(provider,entry,opener)
    if error:return [],error
    return select_candidates(provider,rows),None

def legacy_models(provider):
    """Hidden aliases kept for tasks started before this UI existed; never offered as checkboxes."""
    return [dict(m) for m in provider.get('models',[]) if m.get('alias','').startswith('A/')]

def codex_usable(alias):
    # Image models run only on /v1/images/*; expired aliases are historical provider
    # inventory. Neither can serve a current Codex /v1/responses request.
    # 空/非字符串先挡掉：''.lower() 能跑，于是空串会被判成"可用"，进而混进 expose、
    # 影响"候选为空但有勾选"那条守卫的判据（审查 Q-3）。别名一律应当是非空字符串。
    if not isinstance(alias,str) or not alias:return False
    low=alias.lower()
    return 'image' not in low and '-expires-on-' not in low

def fetch_all(plan, config):
    # Rows sharing one credential (Goat: DeepSeek + GLM) must share a single fetch, otherwise a transient
    # timeout on one row silently drops that row's ticks and models from the gateway.
    by_cred={}
    for p in plan['providers']:
        if p['section']=='auth-file':by_cred.setdefault(('auth',p['id']),[]).append(p)
        else:by_cred.setdefault(plan_identity(p),[]).append(p)
    jobs=[]
    for rows in by_cred.values():
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        jobs.append((rows,opener))
    def run(job):
        rows,opener=job;p=rows[0]
        if p['section']=='auth-file':raw,error=fetch_upstream(p,{'name':AUTH_FILE})
        else:
            try:entry=find_entry(config,p)
            except RouteError as e:return rows,None,str(e)
            raw,error=fetch_upstream(p,entry,opener)
        return rows,raw,error
    # Upstream /models calls are independent and slow; fetch them concurrently so the page is not ~9x serial.
    from concurrent.futures import ThreadPoolExecutor
    # max(1, ...)：jobs 为空（providers: [] —— 全新/空配置是**合法状态**）时
    # max_workers=0 会直接 ValueError，把 snapshot 变成一条误导性的 unreadable、
    # 把 apply_selection 变成整次保存被补偿后拒绝（复查轮 7 的 #4）。
    with ThreadPoolExecutor(max_workers=max(1,min(8,len(jobs)))) as pool:
        results=list(pool.map(run,jobs))
    out={}
    for rows,raw,error in results:
        for p in rows:
            available=[] if raw is None else select_candidates(p,raw)
            out[p['id']]={'available':available,'fetch_error':error,'expose':p.get('expose',[])}
    return out

def clean_plan(plan):
    """Plan file keeps only durable fields; live fetch results stay in memory."""
    out=copy.deepcopy(plan)
    for p in out['providers']:
        p.pop('available',None);p.pop('fetch_error',None)
    return out

def snapshot():
    config=api('/v0/management/config'); plan=read_json(ROOT/'routing-plan.json'); selected={}
    # **逐行容错**：坏行（config 里那条来源缺失或重复、auth 文件读不出来……）只应该让
    # **那一行**算不出启用状态，不该让整页 `/api/state` 抛 RouteError → 409 → 首页一个
    # 字节的数据都拿不到。health 那边的 _routing/_sources 早就是逐行 try 并降级成
    # "不知道"，这里曾是唯一的例外。
    # 被跳过的行**不静默**：收进 unreadable，前端可以据此提示，而不是把它显示成"未启用"。
    # 注意这只放宽**读**路径；写路径（build_config）仍然严格报错 —— 坏行绝不能写进配置。
    #
    # 连"行本身就不成形"（不是 dict、没有 id、plan 根本不是个对象）也要兜住：
    # 原写法 `plan['providers']` / `p['group']` 是直取，畸形 plan 仍会整页 409。
    raw=plan.get('providers') if isinstance(plan,dict) else None
    rows=list(raw) if isinstance(raw,list) else []
    unreadable=[]
    if not isinstance(raw,list):
        unreadable.append({'id':None,'label':'routing-plan.json 的 providers 不是列表','group':None,
                           'error':'routing-plan.json 结构不对，一个来源都读不出来'})
    # 先点名"连归属都读不出来"的行：不这样做，一个缺 group 的行会被下面的分组循环静默跳过、
    # 在界面上完全隐形（既不在任何分组里，也不在 unreadable 里）。
    for p in rows:
        if not isinstance(p,dict):
            unreadable.append({'id':None,'label':'（不是对象的行）','group':None,
                               'error':'providers 里有一项不是对象'})
        elif p.get('group') not in GROUPS:
            unreadable.append({'id':p.get('id'),'label':p.get('label') or p.get('id') or '（缺 group 的行）',
                               'group':None,'error':'这一行的 group 不是 %s 之一，无法归组'
                               % '/'.join(GROUPS)})
    for group in GROUPS:
        ids=[]
        for p in rows:
            if not isinstance(p,dict) or p.get('group')!=group:continue
            pid=p.get('id')
            if not isinstance(pid,str) or not pid:
                unreadable.append({'id':None,'label':p.get('label') or '（缺 id 的行）','group':group,
                                   'error':'这一行没有 id，无法判断它是否启用'})
                continue
            try:
                is_on=active(find_entry(config,p),p['section'])
            except Exception as e:          # noqa: BLE001 - 同 health._routing 的口径
                unreadable.append({'id':pid,'label':p.get('label') or pid,
                                   'group':group,'error':type(e).__name__+': '+str(e)})
                continue
            if is_on:ids.append(pid)
        # 一个分组可以同时启用多家来源。这里给**列表**：旧版那个 'conflict' 哨兵值随之下线——
        # "多家同时启用"从错误状态变成正常状态，前端不再需要"先解决冲突再保存"那条路。
        selected[group]=ids
    # 上游拉取整段也要兜住：fetch_all 内部会读 p['section'] 之类，畸形行同样会让它抛。
    try:
        live=fetch_all({'providers':rows},config)
    except Exception as e:                  # noqa: BLE001
        live={}
        unreadable.append({'id':None,'label':'（上游候选列表整批拉取失败）','group':None,
                           'error':type(e).__name__+': '+str(e)[:160]})
    providers=[]
    for p in rows:
        merged=dict(p) if isinstance(p,dict) else {'raw':p}
        if isinstance(p,dict) and p.get('id') in live:
            merged.update(live[p['id']])
        providers.append(merged)
    return {'revision':revision(config),'selected':selected,'providers':providers,
            'models':api('/v1/models').get('data',[]), 'client_connected':client_connected(),
            'unreadable':unreadable}

def client_connected():
    p=Path.home()/'.codex'/'config.toml'
    if not p.exists(): return False
    try:
        c=tomllib.loads(p.read_text(encoding='utf-8-sig'))
        return c.get('model_providers',{}).get(c.get('model_provider'),{}).get('base_url')=='http://127.0.0.1:8317/v1'
    except ValueError:return False

def _selected_ids(selected, group):
    """取某个分组当前选中的来源 id 列表。兼容老的单个 id（字符串）与新的列表。"""
    v=(selected or {}).get(group)
    if v is None:return []
    if isinstance(v,str):return [v] if v else []
    if isinstance(v,(list,tuple)):return [x for x in v if isinstance(x,str)]
    raise RouteError('供应商选择无效：%s 的选择项格式不对'%group)

def build_config(config, plan, selected):
    if set(selected)!= set(GROUPS): raise RouteError('选择项不完整')
    # selected[group] 兼容两种形状：老的单个 id（字符串）与新的 id 列表。
    # 老形状必须继续吃——备份脚本、外部调用、以及旧前端都可能发它。
    sel={group:_selected_ids(selected,group) for group in GROUPS}
    new=copy.deepcopy(config)
    known={plan_identity(p) for p in plan['providers']}
    for section in SECTIONS:
        for entry in config.get(section,[]):
            if entry_identity(section,entry) not in known and active(entry,section):
                raise RouteError('检测到额外已启用供应商，请先在原管理页停用，避免混用')
    for group in GROUPS:
        options=[p['id'] for p in plan['providers'] if p['group']==group]
        bad=[x for x in sel[group] if x not in options]
        if bad:raise RouteError('供应商选择无效：'+'、'.join(bad))
    # One credential can back several plan rows (Goat serves both DeepSeek and GLM): apply it once and
    # union every selected row's picks, so the same key is never written twice or raced against itself.
    buckets={}
    for p in plan['providers']:
        if p['section']=='auth-file':continue
        buckets.setdefault(plan_identity(p),[]).append(p)
    for entries in buckets.values():
        enabled=False;picked=[];blocked=None
        unknown=False
        heads=set()
        for p in entries:
            if p['id'] not in sel.get(p['group'],[]):continue
            enabled=True
            if p.get('blocked'):blocked=p
            # Fetch failed -> candidate list unknown. Never rewrite this credential's models from partial data.
            if p.get('fetch_error'):unknown=True
            heads.add((p.get('head') or '').strip())
            picked+=[m for m in p.get('available',[]) if m['alias'] in p.get('expose',[]) and codex_usable(m['alias'])]
            picked+=legacy_models(p)
        if blocked:raise RouteError(blocked['label']+'：'+blocked['blocked'])
        entry=find_entry(new,entries[0])
        if enabled and unknown:
            raise RouteError('上游模型列表拉取失败，请刷新后重试；未改动该供应商配置')
        # 拉取"成功"、候选为空，而**这一行本来是有勾选的**：写下去就是把这个凭据的 models
        # 清空（网关于是不再注册它，等于静默停用），而界面报的是保存成功。
        # 与上一条 unknown 守卫同理：宁可拒绝，也不静默改坏配置。
        # 判据只看**真正会被写进去的那些别名**（codex_usable 过滤后），所以：
        #   · "启用了但一个模型都没勾" → 列表为空 → 不拦（这是受支持的状态）；
        #   · "只勾了图片/过期别名（界面本来就把这些复选框禁用了）" → 列表也为空 → 不拦
        #     （否则会误拒一个本来就只能写出 models: [] 的配置，见审查 NC-1）。
        exposed_usable=[n for p in entries if p['id'] in sel.get(p['group'],[])
                        for n in (p.get('expose') or []) if codex_usable(n)]
        if enabled and not picked and exposed_usable:
            raise RouteError('%s 的上游返回了空的模型列表，而这一行本来有勾选；写下去会把该凭据的'
                             '模型清空（等于停用它）。为避免静默改坏配置，本次未保存，请刷新后重试。'
                             % (entries[0].get('label') or entries[0].get('id')))
        # 一个凭据只能有一个渠道头：同一个 base-url+tag 被两行不同头的来源共用时，
        # 写进 config 的 models 只能有一个前缀，猜哪个都是错的，所以直接拒绝。
        if len(heads)>1:
            raise RouteError('%s 这一个凭据被多个渠道头共用（%s）；请让共用一个端点和标签的'
                             '来源使用同一个渠道头'%(entries[0].get('label'),
                                                '、'.join(sorted(h or '（无头）' for h in heads))))
        head=next(iter(heads)) if heads else ''
        if enabled:
            # Empty picks are allowed: an empty models list means this credential registers nothing.
            seen=set();models=[]
            for m in picked:
                alias=m['alias']
                if alias in seen:continue
                seen.add(alias)
                # 遗留 A/ 别名是老任务钉死的模型 ID，**永不加渠道头**——加了那些会话就打不开。
                # 见 HANDOFF §6.5.1（旧线程按 model 名钉住）。
                client=alias if alias.startswith('A/') else _client_id(head,alias)
                models.append({'name':m['name'],'alias':client})
            # Config entries take name/alias only; context_length is UI/catalog metadata.
            entry['models']=models
        # Disabled credentials keep their saved model list; excluded-models/disabled already block them.
        entry['request-retry']=0
        entry.pop('prefix',None)
        if entries[0]['section']=='codex-api-key':
            if enabled:entry.pop('excluded-models',None)
            else:entry['excluded-models']=['*']
        else:
            if enabled:entry.pop('disabled',None)
            else:entry['disabled']=True
    return new

def wait_models(expected):
    # ponytail: subset check; management GET normalizes lists, exact equality false-alarms.
    for _ in range(40):
        actual={v['id'] for v in api('/v1/models').get('data',[])}
        if expected<=actual:return
        time.sleep(.15)
    raise RouteError('配置已保存，但运行时模型目录尚未匹配')

def backup(label):
    p=ROOT/'backups'/(label+'-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f'));p.mkdir(parents=True)
    shutil.copy2(ROOT/'config.yaml',p/'config.yaml')
    return p

def apply_selection(data):
    with LOCK:
        old=api('/v0/management/config')
        if data.get('revision') != revision(old): raise RouteError('配置已变化，请刷新后再保存')
        plan=read_json(ROOT/'routing-plan.json');old_plan=copy.deepcopy(plan)
        live=fetch_all(plan,old)
        for p in plan['providers']:
            p['available']=live[p['id']]['available'];p['fetch_error']=live[p['id']]['fetch_error']
        picks=data.get('picks',{})
        for pid,names in picks.items():
            p=next((x for x in plan['providers'] if x['id']==pid),None)
            if p is None:raise RouteError('未知供应商勾选')
            # A failed fetch means the candidate list is unknown, not empty. Keep the submitted ticks so a
            # transient upstream timeout cannot silently drop models the user asked for.
            if p.get('fetch_error'):
                p['expose']=[n for n in dict.fromkeys(names)]
                continue
            avail={m['alias'] for m in p.get('available',[])}
            submitted=[n for n in dict.fromkeys(names)]
            # 上游**拉取成功但候选为空**：这不是"用户取消了勾选"，而是"我们无从判断这些勾选
            # 还算不算数"。旧写法在这里求交得空 → p['expose']=[] 落盘、整条链路还报保存成功，
            # 用户的勾选被**静默清空且不抛异常** —— 正是 ADR-0008 §4 列为本设计最需要守住的
            # 那一条（"静默清空用户勾选的模型"）的另一个面。
            # 现在明确拒绝：宁可整次不保存，也不静默丢用户的勾选。范围很窄 —— picks 里只有
            # **用户本次改过**的来源（home.js 的 saveRouting 只放 draft≠origin 的项），
            # 所以不会因为某个没动过的来源拉空而误拦整次保存。
            if submitted and not avail:
                raise RouteError('%s 的上游返回了空的模型列表（HTTP 成功但没有任何候选），'
                                 '无法确认你勾选的模型是否还有效；为避免静默清空你的勾选，'
                                 '本次未保存。请稍后重试，或先取消该来源的勾选再保存。'
                                 % (p.get('label') or pid))
            kept=[n for n in submitted if n in avail]
            # 上游返回了**非空**候选、但用户勾的一个都不在其中：与"候选整体为空"是同一类
            # 事故（模型被改名/退役/换了命名法），旧写法会把 expose 清空落盘、还报保存成功。
            # 只拦"全军覆没"：**部分**消失仍然放行 —— 那是原设计的 ponytail 取舍
            # （"upstream can retire a model and must not wedge saving"）想保住的情形，
            # 退役个别模型不该把整次保存卡死；但一个都不剩更像是上游或配置出了事。
            if submitted and avail and not kept:
                raise RouteError('%s 的候选里有 %d 个模型，但你勾的 %d 个一个都不在其中'
                                 '（可能是上游改名或退役了它们）；为避免静默清空你的勾选，'
                                 '本次未保存。请刷新后重新勾选。'
                                 % (p.get('label') or pid, len(avail), len(submitted)))
            p['expose']=kept
        new=build_config(old,plan,data.get('selected',{}))
        changed=[s for s in SECTIONS if old.get(s,[])!=new.get(s,[])]
        auth_before={};auth_changes={}
        for p in plan['providers']:
            if p['section']!='auth-file':continue
            pid=p['id'];auth_before[pid]=auth_current()
            enabled=p['id'] in _selected_ids(data.get('selected'),p['group'])
            # Picks can change while the provider stays enabled, so compare desired vs actual exclusions.
            #
            # **启用状态与勾选都没变时不要重算，也就不必去连 chatgpt.com。**
            # auth_excluded() 在 enabled 时要 fetch_official_models()（打官方接口），失败即抛
            # "无法从官方获取模型列表，不修改排除项" —— 于是官方不可达时，**哪怕用户只改了一家
            # 无关的中转商**，整次保存也被拒（审查 HI-12）。
            # 判据只用本地信息：启用状态可以从 auth 文件的现状反推（`['*']` 就是停用），
            # 上一次的勾选在 old_plan 里（picks 循环只改了 plan 的那一份）。
            # 代价：官方新上架的模型在用户没动过 GPT 勾选时不会自动进排除项 —— 它会被网关注册
            # 但不在目录里，属"网关有、目录无"的无害一侧，比"整次保存被拒"划算。
            prev_row=next((x for x in old_plan['providers'] if x.get('id')==pid),None)
            prev_enabled=auth_before[pid] != ['*']
            keep={n for n in (p.get('expose') or []) if codex_usable(n)}
            prev_keep={n for n in ((prev_row or {}).get('expose') or []) if codex_usable(n)}
            if auth_before[pid] and enabled == prev_enabled and keep == prev_keep:
                desired=auth_before[pid]
            else:
                desired=auth_excluded(enabled,p.get('expose'))
            if auth_before[pid]!=desired:auth_changes[pid]=desired
        if not changed and not auth_changes:
            if plan!=old_plan:
                write_json(ROOT/'routing-plan.json',clean_plan(plan));regen_catalog(plan,data['selected'])
            return snapshot()
        b=backup('route-switch');attempted=[]
        try:
            # One concurrency check before any write: a PUT triggers a full config reload that
            # normalizes the other section, so re-checking mid-loop false-alarms.
            current=api('/v0/management/config')
            for s in changed:
                if current[s]!=old[s]:raise RouteError('保存期间检测到其他配置修改')
            for section in changed:
                # ponytail: one local writer; external management edits must not run concurrently.
                attempted.append(section)
                api('/v0/management/'+section,'PUT',new[section])
            for pid,desired in auth_changes.items():
                patch_auth_models(desired); attempted.append('auth:'+pid)
            # 期望的运行时模型 ID 必须用**客户端可见 ID**（带渠道头）。用干净别名去
            # wait_models 会永远等不到，报"配置已保存，但运行时模型目录尚未匹配"，
            # 而配置其实已经正确写进去了 —— 一个纯粹由这里口径不一致造成的假失败。
            expected={_client_id(p.get('head'),m['alias'])
                      for p in plan['providers'] if p['id'] in _selected_ids(data.get('selected'),p['group'])
                      for m in p.get('available',[]) if m['alias'] in p.get('expose',[]) and codex_usable(m['alias'])}
            # 遗留 A/ 别名不加头，单独并进来（build_config 里也是这么写的）
            expected|={m['alias'] for p in plan['providers']
                       if p['id'] in _selected_ids(data.get('selected'),p['group'])
                       for m in legacy_models(p)}
            wait_models(expected)
        except Exception:
            restored=True
            for section in reversed(attempted):
                if section.startswith('auth:'):continue
                try:
                    api('/v0/management/'+section,'PUT',old[section])
                    if api('/v0/management/config')[section]!=old[section]:restored=False
                except Exception:restored=False
            for pid,prev in auth_before.items():
                try:
                    if auth_current()!=prev:patch_auth_models(prev)
                except Exception:restored=False
            if not restored:raise RouteError('回滚失败；请暂停网关并从备份恢复：'+str(b)) from None
            raise
        regen_catalog(plan,data['selected'])
        write_json(ROOT/'routing-plan.json',clean_plan(plan))
        write_json(b/'switch.json',{'selected':data['selected'],'picks':{k:list(v) for k,v in picks.items()},'time':datetime.now().isoformat(),'scope':'next request; existing streams are not interrupted'})
        return snapshot()

def force_image_generation_off(text):
    """Turn the hosted image tool off; upstream relays reject requests that carry it.

    Codex injects an `image_gen` tool whenever the feature is on, and relays without an image
    endpoint answer every such request with 403 "Image generation is not enabled for this group"
    before producing a token. The switch lives in [features], which may be absent.
    """
    match=re.search(r'(?m)^\[features\][ \t]*$',text)
    line='image_generation = false'
    if not match:
        first=re.search(r'(?m)^\[',text)
        block='[features]\n'+line+'\n\n'
        return (text[:first.start()]+block+text[first.start():]) if first else text.rstrip()+'\n\n'+block
    end=re.search(r'(?m)^\[',text[match.end():])
    body=text[match.end():match.end()+end.start()] if end else text[match.end():]
    if re.search(r'(?m)^image_generation\s*=',body):
        body=re.sub(r'(?m)^image_generation\s*=.*$',line,body)
    else:
        body=body.rstrip('\n')+'\n'+line+'\n'
    return text[:match.end()]+body+text[match.end()+(end.start() if end else len(text)):]

def patch_client_text(text, local_key):
    parsed=tomllib.loads(text)
    if parsed.get('model_provider')!='custom':raise RouteError('当前客户端不是 custom 供应商；停止自动修改')
    # Preserve the provider identifier used by saved tasks and every unrelated config section.
    sections=list(re.finditer(r'(?m)^\[([^\r\n]+)\]\s*$',text))
    target=next((m for m in sections if m.group(1)=='model_providers.custom'),None)
    if target is None:raise RouteError('找不到现有 custom 配置段')
    end=next((m.start() for m in sections if m.start()>target.start()),len(text))
    body=text[target.end():end]
    def set_value(body,key,value):
        line=key+' = '+json.dumps(value,ensure_ascii=False)
        pattern=r'(?m)^'+re.escape(key)+r'\s*=.*$'
        return re.sub(pattern,lambda _:line,body) if re.search(pattern,body) else body.rstrip()+'\n'+line+'\n'
    # A relay is not the OpenAI backend: that name enables its proprietary compaction protocol.
    for key,value in {'name':'Local Gateway','base_url':'http://127.0.0.1:8317/v1','wire_api':'responses','experimental_bearer_token':local_key,'requires_openai_auth':True,'supports_websockets':False,'request_max_retries':0,'stream_max_retries':0}.items():
        body=set_value(body,key,value)
    text=text[:target.end()]+body+text[end:]
    first=re.search(r'(?m)^\[',text);top=text[:first.start()];rest=text[first.start():]
    for key in ('model_context_window','model_auto_compact_token_limit'):
        top=re.sub(r'(?m)^'+key+r'\s*=.*\n?','',top)
    top=set_value(top,'model_catalog_json',str(ROOT/'codex-model-catalog.json'))
    top=set_value(top,'service_tier','default')
    result=force_image_generation_off(top.rstrip()+'\n\n'+rest)
    check=tomllib.loads(result)
    if check.get('model')!=parsed.get('model') or check.get('model_provider')!=parsed.get('model_provider'):raise RouteError('客户端模型身份校验失败')
    return result

def connect_client(data):
    with LOCK:
        current=api('/v0/management/config')
        if data.get('revision')!=revision(current):raise RouteError('路由已变化，请刷新后接入')
        home=Path.home()/'.codex';path=home/'config.toml';auth=home/'auth.json'
        text=path.read_text(encoding='utf-8-sig');parsed=tomllib.loads(text)
        available={m['id'] for m in api('/v1/models').get('data',[])}
        if parsed.get('model') not in available:raise RouteError('请先选择支持当前模型 '+str(parsed.get('model'))+' 的 GPT 中转站')
        if client_connected() and parsed.get('model_catalog_json')==str(ROOT/'codex-model-catalog.json') and parsed.get('model_providers',{}).get('custom',{}).get('name')=='Local Gateway':return snapshot()
        if not auth.exists() or read_json(auth).get('auth_mode')!='chatgpt':raise RouteError('未检测到原有 ChatGPT 登录，停止修改')
        auth_before=auth.read_bytes();modified=patch_client_text(text,secrets()['api_key'])
        b=backup('client-connect');shutil.copy2(path,b/'codex-config.toml');shutil.copy2(auth,b/'codex-auth.json')
        if path.read_text(encoding='utf-8-sig')!=text:raise RouteError('客户端配置正在被其他程序修改')
        temp=path.with_suffix('.toml.gateway.tmp');temp.write_text(modified,encoding='utf-8');os.replace(temp,path)
        if auth.read_bytes()!=auth_before:
            shutil.copy2(b/'codex-config.toml',path)
            raise RouteError('登录文件在接入期间发生变化；已撤销客户端配置修改')
        write_json(b/'verification.json',{'auth_unchanged':True,'provider_id_unchanged':True,'client_restart_required_for_first_load':True})
        return snapshot()

def auth_excluded(enabled,picks=None):
    if not enabled:return ['*']
    # Exclude every live-known text model except what the user ticked, plus the static image block.
    slugs,error=fetch_official_models()
    if error:raise RouteError('无法从官方获取模型列表，不修改排除项：'+error)
    keep={n for n in (picks or []) if codex_usable(n)}
    return sorted(set(slugs)-keep)+sorted(OFFICIAL_IMAGE_MODELS)

def auth_current():
    return json.loads((ROOT/'auth'/AUTH_FILE).read_text(encoding='utf-8-sig')).get('excluded_models')

def patch_auth_models(excluded):
    api('/v0/management/auth-files/fields','PATCH',{'name':AUTH_FILE,'excluded_models':excluded})

def regen_catalog(plan,selected):
    # picked 的键是**客户端可见 ID**（srapi/gpt-5.6-sol），值里额外带上干净别名 clean——
    # 因为官方 meta 与模板池都是按**上游模型名**索引的，拿带头的 ID 去查会全部落空，
    # 表现是"有头的模型丢掉官方元数据、档位和上下文窗口"，不报错，很难发现。
    picked={}
    for p in plan['providers']:
        # Codex reads this catalog only at startup. Keep every explicitly exposed
        # model here, even if its provider is not the current selection, so a
        # later provider switch never requires a Codex restart just to see it.
        head=(p.get('head') or '').strip();label=p.get('label') or p.get('id')
        for alias in p.get('expose',[]):
            if not codex_usable(alias):continue
            live=next((m for m in p.get('available',[]) if m.get('alias')==alias),None)
            size=live.get('context_length') if live else None
            cid=_client_id(head,alias)
            previous=picked.get(cid,(p['group'],None))[1]
            # Missing metadata from an earlier relay must not hide a later explicit window.
            sizes=[n for n in (previous,size) if isinstance(n,int) and not isinstance(n,bool) and n>0]
            picked[cid]=(p['group'],min(sizes) if sizes else None,head,label,alias)
        # 遗留 A/ 别名不加头（老任务钉死的 ID），键就是它自己，clean 也是它自己。
        for m in legacy_models(p):
            picked.setdefault(m['alias'],(p['group'],None,'',label,m['alias']))
    # Template pool is stable; the live catalog shrinks to the picked set, so it cannot seed itself.
    pool=ROOT/'codex-model-catalog-templates.json'
    if not pool.exists():pool=ROOT/'codex-model-catalog.json'
    if not pool.exists():return
    entries=read_json(pool)['models'];out=[]
    meta,meta_err=official_meta()
    # Official metadata wins when reachable: its reasoning ladders and Fast tiers are
    # authoritative, while the template pool only knows models that existed when it was written.
    added=[]
    # Codex only offers the thinking-depth control when an entry lists reasoning levels. Borrow the
    # widest ladder in the pool for entries with none, not merely the first: the narrowest would drop
    # max/ultra and quietly take away depths the client already uses.
    donor_levels=max((m['supported_reasoning_levels'] for m in entries if m.get('supported_reasoning_levels')),key=len,default=None)
    for alias,(group,size,head,label,clean) in sorted(picked.items()):
        if not meta_err and clean in meta:
            om=meta[clean]
            e=next((copy.deepcopy(m) for m in entries if m['slug']==clean),None) or {}
            e.update({k:v for k,v in om.items() if k not in ('slug',) and v is not None})
            e['slug']=alias
            # 需求 5 的"供应商头"：slug 是带头的客户端 ID（路由用），display_name 给人看。
            # 无头的官方行沿用官方显示名，行为一位不变。
            e['display_name']=(label+' · '+clean) if head else (om.get('display_name') or alias)
            e['description']=om.get('description') or alias
            if not e.get('base_instructions'):e['base_instructions']=next((m.get('base_instructions') for m in entries if m.get('base_instructions')),'')
            e.setdefault('input_modalities',['text'])
            e.setdefault('supports_parallel_tool_calls',False)
            e.setdefault('supports_reasoning_summaries',False)
            e.setdefault('supports_search_tool',False)
            e.setdefault('shell_type','shell_command')
            e.setdefault('support_verbosity',False)
            e.setdefault('default_reasoning_summary','none')
            e.setdefault('effective_context_window_percent',95)
            e.setdefault('supported_in_api',True)
            e.setdefault('experimental_supported_tools',[])
            e.setdefault('truncation_policy',{'limit':10000,'mode':'bytes'})
            e.setdefault('availability_nux',None)
            e.setdefault('upgrade',None)
            if not e.get('supported_reasoning_levels'):
                e['supported_reasoning_levels']=copy.deepcopy(donor_levels) if donor_levels else []
                names=[l['effort'] for l in e['supported_reasoning_levels']]
                e['default_reasoning_level']='max' if 'max' in names else (names[0] if names else None)
            # Established sizing policy for relays; official numbers (max 872000) are not bill-safe here.
            if group=='gpt':
                e['context_window']=e['max_context_window']=272000;e['auto_compact_token_limit']=230000
            elif isinstance(size,int) and size>0:
                e['context_window']=e['max_context_window']=size
                e['auto_compact_token_limit']=int(size*0.9)
            e['effective_context_window_percent']=95
            # 只有遗留 A/ 别名隐藏。带渠道头的模型必须可见，否则需求 5"在模型列表里区分"
            # 就白做了——这一行最容易顺手改错。
            e['visibility']='hide' if alias.startswith('A/') else 'list'
            out.append(e)
            continue
        # 池子按干净别名索引：否则同一个模型每换一次头就会在池里多出一条，越滚越大。
        e=next((copy.deepcopy(m) for m in entries if m['slug']==clean),None)
        if e is None:
            tpl={'gpt':'gpt-5.5','glm':'glm-5.3-flash'}.get(group,'deepseek-v4-pro')
            seed=copy.deepcopy(next(m for m in entries if m['slug']==tpl))
            seed['slug']=clean;seed['display_name']=clean;seed['description']=clean
            # Unknown models must not inherit another model's paid Fast capability.
            seed['service_tiers']=[];seed['additional_speed_tiers']=[]
            added.append(seed)
            e=copy.deepcopy(seed)          # 输出用副本，下面还要把 slug 改成客户端 ID
        if group=='gpt':
            e['context_window']=e['max_context_window']=272000;e['auto_compact_token_limit']=230000
        else:
            # Non-GPT: use the upstream-advertised window verbatim, else keep whatever the entry already had.
            if isinstance(size,int) and size>0:
                e['context_window']=e['max_context_window']=size
                e['auto_compact_token_limit']=int(size*0.9)
        if not e.get('supported_reasoning_levels') and donor_levels:
            e['supported_reasoning_levels']=copy.deepcopy(donor_levels)
            names=[l['effort'] for l in e['supported_reasoning_levels']]
            # Keep the depth the client is configured to request, so nothing it already uses disappears.
            e['default_reasoning_level']='max' if 'max' in names else names[0]
        e['slug']=alias
        if head:e['display_name']=label+' · '+clean
        e['effective_context_window_percent']=95
        e['visibility']='hide' if alias.startswith('A/') else 'list'
        out.append(e)
    b=backup('catalog-regen')
    current=ROOT/'codex-model-catalog.json'
    if current.exists():shutil.copy2(current,b/'codex-model-catalog.json')
    write_json(ROOT/'codex-model-catalog.json',{'models':out})
    if added:write_json(ROOT/'codex-model-catalog-templates.json',{'models':entries+added})

def recheck_blocked():
    with LOCK:
        plan=read_json(ROOT/'routing-plan.json');config=api('/v0/management/config')
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):return None
        opener=urllib.request.build_opener(NoRedirect())
        for p in plan['providers']:
            if not p.get('blocked'):continue
            if p['section']=='auth-file':p['blocked']=None;continue
            entry=find_entry(config,p)
            key=entry.get('api-key') or entry['api-key-entries'][0]['api-key']
            req=urllib.request.Request(p['base_url'].rstrip('/')+'/models',headers={**entry.get('headers',{}),'Authorization':'Bearer '+key})
            try:
                with opener.open(req,timeout=15) as response:
                    payload=json.load(response)
                    if not isinstance(payload.get('data'),list):raise RouteError('模型列表格式无效')
                p['blocked']=None
            except urllib.error.HTTPError as e:p['blocked']='密钥验证 HTTP '+str(e.code)+'；请在高级配置中修正后重新验证'
            except (OSError,ValueError):p['blocked']='模型列表验证失败；未启用'
        write_json(ROOT/'routing-plan.json',plan)
        return snapshot()

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def send(self,code,data,content_type='application/json; charset=utf-8'):
        raw=data if isinstance(data,bytes) else json.dumps(data,ensure_ascii=False).encode()
        self.send_response(code);self.send_header('Content-Type',content_type);self.send_header('Content-Length',str(len(raw)))
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers();self.wfile.write(raw)
    def check(self,auth=False):
        if self.headers.get('Host')!=f'127.0.0.1:{PORT}':raise RouteError('仅允许本机地址访问')
        origin=self.headers.get('Origin')
        if origin and origin!=f'http://127.0.0.1:{PORT}':raise RouteError('拒绝跨站请求')
        return True
    def do_GET(self):
        try:
            if not self.check(self.path.startswith('/api/')):return
            static={'/':('selector.html','text/html; charset=utf-8'),'/selector.js':('selector.js','text/javascript; charset=utf-8'),'/selector.css':('selector.css','text/css; charset=utf-8')}
            if self.path in static:
                name,mime=static[self.path];return self.send(200,(ROOT/'static'/name).read_bytes(),mime)
            if self.path=='/api/state':return self.send(200,snapshot())
            self.send(404,{'error':'Not found'})
        except RouteError as e:self.send(409,{'error':str(e)})
        except Exception:self.send(500,{'error':'本地配置读取失败；未返回敏感详情'})
    def do_POST(self):
        try:
            if not self.check(True):return
            if self.path not in ('/api/select','/api/connect','/api/recheck'):return self.send(404,{'error':'Not found'})
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<4096:raise RouteError('请求大小无效')
            data=json.loads(self.rfile.read(length))
            action={'/api/select':lambda:apply_selection(data),'/api/connect':lambda:connect_client(data),'/api/recheck':recheck_blocked}
            self.send(200,action[self.path]())
        except (ValueError,KeyError,TypeError):self.send(400,{'error':'请求格式无效'})
        except RouteError as e:self.send(409,{'error':str(e)})
        except Exception:
            import traceback;traceback.print_exc()
            self.send(500,{'error':'保存失败；请检查本地网关，勿重复提交'})

if __name__=='__main__':
    ThreadingHTTPServer(('127.0.0.1',PORT),Handler).serve_forever()
