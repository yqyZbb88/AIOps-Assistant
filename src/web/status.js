"use strict";
const TOKEN_KEY="aiops_status_token";
const $=id=>document.getElementById(id);

function setContent(id,node){const box=$(id).querySelector(".content");box.className="content";box.replaceChildren(node)}
function message(text,cls="muted"){const p=document.createElement("p");p.className=cls;p.textContent=text;return p}
function article(item){
  const el=document.createElement("article");
  const title=document.createElement("strong");
  title.textContent=item.alertname||item.metric_id||item.command||item.approval_id||item.level||"记录";
  const body=document.createElement("div");
  body.textContent=JSON.stringify(item,null,2);
  el.append(title,body);return el;
}
function renderList(id,items,empty="暂无记录"){
  if(!Array.isArray(items)||!items.length){setContent(id,message(empty));return}
  const frag=document.createDocumentFragment();items.forEach(item=>frag.append(article(item)));setContent(id,frag);
}
function renderHosts(hosts,statuses){
  if(!hosts.length){setContent("hosts",message("主机清单为空"));return}
  const grid=document.createElement("div");grid.className="host-grid";
  hosts.forEach(host=>{
    const card=document.createElement("article");card.className="host-card";
    const title=document.createElement("strong");
    title.textContent=`${host.name} · ${host.host_id}`;
    const badges=document.createElement("div");
    const status=statuses.get(host.host_id);
    const exporter=status?.exporter?.state||(host.enabled?"unknown":"disabled");
    [`exporter: ${exporter}`,`ssh: ${host.ssh_configured?"configured":"off"}`].forEach(text=>{
      const badge=document.createElement("span");
      badge.className=`badge ${text.includes("reachable")||text.includes("configured")?"ok":text.includes("disabled")||text.includes("unknown")||text.includes("off")?"warn":"bad"}`;
      badge.textContent=text;badges.append(badge);
    });
    const body=document.createElement("pre");
    body.textContent=status?JSON.stringify({metrics:status.metrics,active_alerts:status.active_alerts},null,2):"未查询（主机已禁用）";
    card.append(title,badges,body);grid.append(card);
  });
  setContent("hosts",grid);
}
async function loadHosts(){
  try{
    const hosts=await request("/hosts?include_disabled=true",true);
    const statuses=new Map();
    await Promise.all(hosts.filter(h=>h.enabled).map(async host=>{
      try{statuses.set(host.host_id,await request(`/hosts/${encodeURIComponent(host.host_id)}/status`,true))}
      catch(error){statuses.set(host.host_id,{exporter:{state:"query_error"},error:error.message})}
    }));
    renderHosts(hosts,statuses);
  }catch(error){setContent("hosts",message(error.message,"error"))}
}
async function request(path,authenticated=true){
  const headers={"Accept":"application/json"};
  const token=sessionStorage.getItem(TOKEN_KEY);
  if(authenticated&&token)headers.Authorization=`Bearer ${token}`;
  const response=await fetch(path,{headers,cache:"no-store"});
  let data;try{data=await response.json()}catch{data={detail:"响应不是 JSON"}}
  if(!response.ok)throw new Error(`${response.status} ${data.detail||response.statusText}`);
  return data;
}
function renderHealth(data){
  const wrap=document.createElement("div");
  const components=data.components||{};
  Object.entries(components).forEach(([name,value])=>{
    const badge=document.createElement("span");const status=value.status||"unknown";
    badge.className=`badge ${status==="ok"||status==="running"?"ok":status==="unknown"||status==="disabled"?"warn":"bad"}`;
    badge.textContent=`${name}: ${status}`;wrap.append(badge);
  });
  const meta=document.createElement("p");meta.className="muted";meta.textContent=`version ${data.version||"?"} · ready ${Boolean(data.ready)}`;wrap.append(meta);setContent("health",wrap);
}
async function loadSection(id,path,selector=value=>value){
  try{renderList(id,selector(await request(path,true)))}catch(error){setContent(id,message(error.message,"error"));if(error.message.startsWith("401"))$("state").textContent="Token 无效或已失效"}
}
async function refresh(){
  try{renderHealth(await request("/ready",false))}catch(error){setContent("health",message(error.message,"error"))}
  const token=sessionStorage.getItem(TOKEN_KEY);
  if(!token){$("state").textContent="请输入 Token 后连接";return}
  $("state").textContent="刷新中…";
  await Promise.all([
    loadHosts(),
    loadSection("inspections","/inspections/recent?limit=20",x=>x.inspections),
    loadSection("approvals","/approvals"),
    loadSection("executions","/executions?limit=20",x=>x.executions),
    loadSection("remediations","/alerts/history?limit=20",x=>x.remediations),
    loadSection("advice","/alerts/history?limit=20",x=>x.advice),
  ]);
  $("state").textContent=`最近刷新 ${new Date().toLocaleTimeString()}`;
}
$("connect").addEventListener("click",()=>{const token=$("token").value.trim();if(token)sessionStorage.setItem(TOKEN_KEY,token);$("token").value="";refresh()});
$("clear").addEventListener("click",()=>{sessionStorage.removeItem(TOKEN_KEY);$("token").value="";$("state").textContent="Token 已清除"});
$("refresh").addEventListener("click",refresh);
refresh();setInterval(()=>{if(sessionStorage.getItem(TOKEN_KEY))refresh()},30000);
