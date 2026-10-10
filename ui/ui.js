"use strict";
// Read-only UI. Every value is rendered with textContent (no HTML injection).
var PAGES = ["Dashboard","Streams","Lineup","Devices","Aliases","Logs"];
var cur = "Dashboard", timer = null, logCursor = 0, lastEv = null, busy = false, ticks = 0;
function el(tag, text, cls){var e=document.createElement(tag); if(text!==undefined&&text!==null)e.textContent=String(text); if(cls)e.className=cls; return e;}
function get(u){return fetch(u,{cache:"no-store"}).then(function(r){if(!r.ok)throw new Error(u+" "+r.status);return r.json();});}
function table(cols, rows){
  var t=el("table"), h=el("tr"); cols.forEach(function(c){h.appendChild(el("th",c[0]));}); t.appendChild(h);
  rows.forEach(function(r){var tr=el("tr"); cols.forEach(function(c){var v=c[1](r); tr.appendChild(el("td",v===undefined||v===null?"-":v));}); t.appendChild(tr);});
  return t;
}
function kv(obj){var rows=Object.keys(obj).map(function(k){var v=obj[k]; return [k, (v!==null&&typeof v==="object")?JSON.stringify(v):v];});
  return table([["key",function(r){return r[0];}],["value",function(r){return r[1];}]],rows);}
function f(k){return function(r){return r[k];};}
var R = {
 Dashboard: function(m){return get("/api/ui/state").then(function(s){
  var b=document.getElementById("badge"); b.textContent=s.health.ok?"healthy":"attention"; b.className="badge "+(s.health.ok?"ok":"bad");
  m.appendChild(el("h2","Health")); m.appendChild(kv({m1:s.m1.ip,m1_dev:s.m1.dev,api8_reachable:s.health.api8_reachable,api8_age_sec:s.health.api8_last_ok_age_sec,api8_source:s.health.api8_source,epg_present:s.health.epg_present,lineup_channels:s.lineup_count,tuner_count:s.tuner_count,uptime_sec:s.uptime_sec,version:s.version}));
  s.warnings.forEach(function(w){m.appendChild(el("p",w,"warn"));});
  m.appendChild(el("h2","Tuner slots"));
  m.appendChild(table([["slot",f("slot")],["kind",f("kind")],["dev",f("dev")],["state",f("state")],["channel",f("chan_id")],["client",f("client_ip")],["started",f("started")],["bytes",f("bytes")]],s.slots));
  if(s.brp){m.appendChild(el("h2","BRP counters"));m.appendChild(kv(s.brp));}
  m.appendChild(el("h2","Config (non-secret)"));m.appendChild(kv(s.config));});},
 Streams: function(m){return get("/api/ui/streams").then(function(s){
  m.appendChild(el("h2","Live ("+s.live.length+(s.hls_live?", +"+s.hls_live+" HLS":"")+")"));
  m.appendChild(table([["channel",f("chan_id")],["client",f("client_ip")],["user-agent",f("ua")],["start",f("start")],["age s",f("age_sec")],["bytes",f("bytes")],["qdrops",f("qdrops")],["pacer",function(r){return r.pacer?JSON.stringify(r.pacer):null;}]],s.live));
  m.appendChild(el("h2","Recently closed"));
  m.appendChild(table([["channel",f("chan_id")],["client",f("client_ip")],["user-agent",f("ua")],["start",f("start")],["dur s",f("dur_sec")],["reason",f("reason")],["bytes",f("bytes")],["wgap",function(r){return r.wgap?JSON.stringify(r.wgap):null;}],["pacer",function(r){return r.pacer?JSON.stringify(r.pacer):null;}]],s.recent_closed));});},
 Lineup: function(m){return get("/api/ui/lineup").then(function(s){
  m.appendChild(el("p",s.count+" channels ("+(s.cached_age_sec==null?"lineup not cached yet":"cached "+s.cached_age_sec+" s ago")+"; this page never refreshes it)"));
  m.appendChild(table([["#",f("number")],["name",f("name")],["chan_id",f("chan_id")],["proto",f("protocol")],["HD",f("hd")],["DRM",f("drm")]],s.channels));});},
 Devices: function(m){return get("/api/ui/devices").then(function(s){
  s.configured_m1s.forEach(function(d){m.appendChild(el("h2","Configured M1 "+d.ip));m.appendChild(kv({spec:d.spec,m1_dev:d.dev,source:d.source,hdhr_device_id:d.hdhr_device_id,hls_tuner:d.hls_tuner}));
   m.appendChild(table([["BRP dev",f("dev")],["push IP",f("push_ip")],["alias present",f("alias_present")]],d.brp_devs));});
  m.appendChild(el("h2","Protected real Minis (never used as push IPs)"));m.appendChild(el("p",s.protected_minis.join(", ")||"-"));
  m.appendChild(el("h2","Passive discovery (read-only)"));
  if(s.passive_discovery===null){m.appendChild(el("p","off (set ZUI_DISCO=1 for a receive-only UDP 31064 listener)"));}
  else m.appendChild(table([["dev",f("dev")],["ip",f("ip")],["kind",f("kind")],["model",f("model")],["name",f("name")],["tuners",f("tuners")],["share",f("share")],["protected",f("protected")]],s.passive_discovery));});},
 Aliases: function(m){return get("/api/ui/aliases").then(function(s){
  m.appendChild(table([["dev",f("dev")],["push IP",f("push_ip")],["status",f("status")]],s.rows));
  m.appendChild(el("p","Reserved (never use): "+(s.reserved_ips.join(", ")||"-")));
  m.appendChild(el("h2","Commands to copy (the bridge never runs these)"));
  m.appendChild(el("p",s.commands.note));
  m.appendChild(el("h2","macOS"));m.appendChild(el("pre",s.commands.macos.join("\n")||"(nothing missing)","mono"));
  m.appendChild(el("h2","Linux"));m.appendChild(el("pre",s.commands.linux.join("\n")||"(nothing missing)","mono"));});},
 Logs: function(m){return get("/api/ui/logs?limit=300").then(function(s){
  if(!s.ring_on)m.appendChild(el("p","log ring is off","warn"));
  logCursor=s.cursor||0;
  var pre=el("pre",s.lines.map(function(l){return l.line;}).reverse().join("\n"),"mono");pre.id="logpre";m.appendChild(pre);});}
};
function render(){var m=document.getElementById("main");var tmp=document.createElement("div");
 return R[cur](tmp).then(function(){m.textContent="";while(tmp.firstChild)m.appendChild(tmp.firstChild);}).catch(function(e){m.textContent="";m.appendChild(el("p",String(e),"warn"));});}
function show2(name){cur=name;var nav=document.getElementById("nav");nav.textContent="";
 PAGES.forEach(function(p){var b=el("button",p,p===cur?"on":"");b.onclick=function(){show2(p);lastEv=null;render();};nav.appendChild(b);});}
// Live updates: poll the tiny /api/ui/events cursor every 1.5 s and refetch
// only the Streams / Logs data that changed. Other pages refresh every ~5 s.
function setLive(ok){var b=document.getElementById("live");if(b){b.textContent=ok?"live":"offline";b.className="badge "+(ok?"ok":"bad");}}
function tick(){
 if(busy||document.hidden)return; ticks++;
 if(cur!=="Streams"&&cur!=="Logs"){if(ticks%3===0)render();return;}
 busy=true;
 get("/api/ui/events").then(function(ev){setLive(true);
  if(cur==="Streams"){var chg=!lastEv||ev.seq!==lastEv.seq||ev.live>0||ev.hls_live>0||ev.log_seq!==lastEv.log_seq;lastEv=ev;
   if(chg)return render();}
  else if(cur==="Logs"){lastEv=ev;
   if(ev.log_seq>logCursor){return get("/api/ui/logs?limit=300&since="+logCursor).then(function(s){
    var pre=document.getElementById("logpre");if(!pre||!s.lines.length){logCursor=s.cursor||logCursor;return;}
    var add=s.lines.map(function(l){return l.line;}).reverse().join("\n");
    var all=(add+"\n"+pre.textContent).split("\n").slice(0,600);pre.textContent=all.join("\n");logCursor=s.cursor;});}}
 }).catch(function(){setLive(false);}).then(function(){busy=false;});
}
show2("Dashboard");render();timer=setInterval(tick,1500);
