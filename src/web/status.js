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
    loadSection("active","/alerts/active"),
    loadSection("inspections","/inspections/recent?limit=20",x=>x.inspections),
    loadSection("approvals","/approvals"),
    loadSection("remediations","/alerts/history?limit=20",x=>x.remediations),
    loadSection("advice","/alerts/history?limit=20",x=>x.advice),
  ]);
  $("state").textContent=`最近刷新 ${new Date().toLocaleTimeString()}`;
}
$("connect").addEventListener("click",()=>{const token=$("token").value.trim();if(token)sessionStorage.setItem(TOKEN_KEY,token);$("token").value="";refresh()});
$("clear").addEventListener("click",()=>{sessionStorage.removeItem(TOKEN_KEY);$("token").value="";$("state").textContent="Token 已清除"});
$("refresh").addEventListener("click",refresh);
refresh();setInterval(()=>{if(sessionStorage.getItem(TOKEN_KEY))refresh()},30000);
