let key='',state=null,picks={};const GROUPS=['gpt','deepseek','glm'];const $=id=>document.getElementById(id);
async function api(path,data){const response=await fetch(path,{method:data?'POST':'GET',headers:{Authorization:'Bearer '+key,'Content-Type':'application/json'},body:data?JSON.stringify(data):undefined});const value=await response.json();if(!response.ok)throw Error(value.error||'请求失败');return value;}
function renderPicker(group){
  const box=$('picks-'+group);const id=$(group).value;box.replaceChildren();
  const p=state.providers.find(x=>x.id===id);
  if(!p){box.textContent='暂不启用';return;}
  if(p.fetch_error){const e=document.createElement('p');e.className='err';e.textContent=p.fetch_error;box.append(e);return;}
  if(!p.available||!p.available.length){const e=document.createElement('p');e.className='err';e.textContent='上游没有可用模型';box.append(e);return;}
  if(!picks[id])picks[id]=new Set(p.expose||[]);
  const chosen=picks[id];
  for(const m of p.available.filter(m=>!/image|-expires-on-/i.test(m.alias))){
    const row=document.createElement('label');row.className='opt';
    const cb=document.createElement('input');cb.type='checkbox';cb.value=m.alias;cb.checked=chosen.has(m.alias);
    cb.onchange=()=>{cb.checked?chosen.add(m.alias):chosen.delete(m.alias);};
    row.append(cb,document.createTextNode(m.alias));
    if(/image/i.test(m.alias)){const tag=document.createElement('span');tag.className='flag';tag.textContent='生图专用，Codex 不可用';row.append(tag);}
    box.append(row);
  }
  const tools=document.createElement('div');tools.className='tools';
  const all=document.createElement('button');all.type='button';all.className='secondary tiny';all.textContent='全选';
  all.onclick=()=>{p.available.filter(m=>!/image|-expires-on-/i.test(m.alias)).forEach(m=>chosen.add(m.alias));renderPicker(group);};
  const none=document.createElement('button');none.type='button';none.className='secondary tiny';none.textContent='全不选';
  none.onclick=()=>{chosen.clear();renderPicker(group);};
  tools.append(all,none);box.append(tools);
}
function render(s){
  state=s;$('login').hidden=true;$('settings').hidden=false;
  for(const group of GROUPS){
    const select=$(group);const keep=select.value;select.replaceChildren(new Option('暂不启用',''));
    for(const p of s.providers.filter(p=>p.group===group)){const option=new Option(p.label+(p.blocked?' · 认证未通过':'')+(p.fetch_error?' · 拉取失败':''),p.id);option.disabled=!!p.blocked;select.add(option);}
    select.value=s.selected[group]||keep||'';
    renderPicker(group);
  }
  for(const group of GROUPS)$(group).onchange=()=>renderPicker(group);
  $('warnings').textContent=s.providers.filter(p=>p.blocked||p.warning).map(p=>p.label+'：'+(p.blocked||p.warning)).join('\n');
  $('recheck').hidden=!s.providers.some(p=>p.blocked);
  if(Object.values(s.selected).includes('conflict'))$('warnings').textContent+='\n检测到多来源同时启用，请重新选择后保存。';
  $('models').replaceChildren(...s.models.filter(m=>!m.id.includes('/')).map(m=>{const el=document.createElement('span');el.className='model';el.textContent=m.id;return el;}));
  if(!s.models.length)$('models').textContent='尚未暴露任何模型；请勾选上面的模型后保存。';
  $('client').textContent=s.client_connected?'客户端配置已指向本地网关；当前运行实例是否已加载需单独验收。':'Codex 尚未接入此网关。选定供应商后再接入；不会自动重启客户端。';
}
function payload(){const selected={},out={};for(const group of GROUPS){const id=$(group).value||null;selected[group]=id;if(id)out[id]=Array.from(picks[id]||[]);}return {revision:state.revision,selected,picks:out};}
$('login').onsubmit=async e=>{e.preventDefault();key=$('password').value;try{render(await api('/api/state'));$('password').value='';$('status').textContent='已读取当前路由';}catch(e){$('status').textContent=e.message;}};
$('refresh').onclick=async()=>{try{render(await api('/api/state'));$('status').textContent='已刷新';}catch(e){$('status').textContent=e.message;}};
$('save').onclick=async()=>{const b=$('save');b.disabled=true;$('status').textContent='正在保存并核验路由…';try{render(await api('/api/select',payload()));$('status').textContent='已保存，后续请求使用新路由。新增模型或能力需重启 Codex 刷新菜单。';}catch(e){$('status').textContent=e.message;}finally{b.disabled=false;}};
$('connect').onclick=async()=>{const b=$('connect');b.disabled=true;try{render(await api('/api/connect',{revision:state.revision}));$('status').textContent='客户端配置已接入，官方登录与历史未改。首次需要退出并重新打开客户端一次。';}catch(e){$('status').textContent=e.message;}finally{b.disabled=false;}};
$('recheck').onclick=async()=>{const b=$('recheck');b.disabled=true;try{render(await api('/api/recheck',{}));$('status').textContent='验证完成';}catch(e){$('status').textContent=e.message;}finally{b.disabled=false;}};
(async()=>{try{render(await api('/api/state'));$('status').textContent='已就绪';}catch(e){$('status').textContent=e.message;}})();
