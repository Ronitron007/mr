"use strict";
(function(){
let DATA = window.MRTRACE_DATA;
const $ = (s, el=document) => el.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const TZ = "Asia/Kolkata";
const fmtClock = t => new Intl.DateTimeFormat("en-GB",{timeZone:TZ,hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false}).format(new Date(t*1000));
const fmtDay = t => new Intl.DateTimeFormat("en-GB",{timeZone:TZ,weekday:"short",day:"numeric",month:"short"}).format(new Date(t*1000));
const fmtWhen = t => new Intl.DateTimeFormat("en-GB",{timeZone:TZ,day:"numeric",month:"short",hour:"2-digit",minute:"2-digit",hour12:false}).format(new Date(t*1000));
function dur(s){ if(s==null||isNaN(s)) return "–"; s=Math.round(s); if(s<60) return s+"s"; if(s<3600) return Math.floor(s/60)+"m "+String(s%60).padStart(2,"0")+"s"; return Math.floor(s/3600)+"h "+String(Math.floor(s%3600/60)).padStart(2,"0")+"m"; }
function ago(t){ const d=Date.now()/1000-t; if(d<90) return "just now"; if(d<3600) return Math.round(d/60)+"m ago"; if(d<86400) return Math.round(d/3600)+"h ago"; return Math.round(d/86400)+"d ago"; }
const units = u => u==null ? "–" : (+u).toFixed(2).replace(/\.?0+$/,"");
const kfmt = n => n==null ? "–" : n>=1e6 ? (n/1e6).toFixed(1)+"M" : n>=1e3 ? Math.round(n/1e3)+"k" : String(n);
function costText(t){
  const c=t.cost||{};
  if(c.units!=null && (c.units>0 || c.claude_pts>0)) return {txt:units(c.units)+"u", tip:`${units(c.units)} mr cost units${c.claude_pts?` · ${c.claude_pts} Claude pts`:""}`};
  if(t.cost_measured && !c.claude_pts && !(c.usd_equiv>0)) return {txt:"free", tip:"mr measured this job and recorded zero cost"};
  if(c.tok_in||c.tok_out) return {txt:kfmt((c.tok_in||0)+(c.tok_out||0))+" tok", tip:"mr recorded no cost for this job; showing tokens instead"};
  return {txt:"n/a", tip:"cost not measured (mr wrote no cost for this job)", na:true};
}
function maxRung(t){ let best=null; for(const s of t.steps){ if(s.rung && s.rung!=="?" && (best===null||RANK(s.rung)>RANK(best))) best=s.rung; } return best; }
function timeBar(t, W=120){
  const tm=t.time||{}, w=tm.wall_s||0; if(!w) return "";
  const m=(tm.model_s||0)/w*W, o=(tm.mr_s||0)/w*W, u=(tm.unlogged_s||0)/w*W;
  return `<svg width="${W}" height="5" viewBox="0 0 ${W} 5" class="tb"><title>model ${dur(tm.model_s)} · mr checks ${dur(tm.mr_s)}${tm.unlogged_s?` · unlogged ${dur(tm.unlogged_s)}`:""}</title>
   <rect x="0" y="0" width="${m}" height="5" fill="var(--ink-2)"/><rect x="${m}" y="0" width="${o}" height="5" fill="var(--warn)" opacity=".75"/><rect x="${m+o}" y="0" width="${u}" height="5" fill="var(--idle)" opacity=".5"/></svg>`;
}
const RANK = r => { const i = DATA.rungOrder.indexOf(r); return i<0 ? DATA.rungOrder.length : i; };
const stepColor = s => s.live ? "var(--plan)" : !s.ok ? "var(--bad)" : s.role==="planner" ? "var(--plan)" : s.role==="reviewer" ? "var(--rev)" : "var(--ok)";
const VERB = {planner:"Plan",implementer:"Implement",reviewer:"Review"};

let state = {sel: decodeURIComponent(location.hash.slice(1)) || null, filter:"all", repo:"", q:""};

function endOf(t){ return t.t1 || (t.status==="running" ? Date.now()/1000 : Math.max(t.t0||0, ...t.steps.map(s=>s.t1||0))); }

/* ---------- board ---------- */
function glyph(t, W=132, H=26){
  const t0=t.t0, span=Math.max(endOf(t)-t0,1), n=DATA.rungOrder.length;
  let out=`<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" aria-hidden="true"><line x1="0" x2="${W}" y1="${H-.5}" y2="${H-.5}" stroke="var(--rule)"/>`;
  for(const s of t.steps){
    if(s.t0==null) continue;
    const x=(s.t0-t0)/span*W, w=Math.max(((s.t1||endOf(t))-s.t0)/span*W,2.5);
    const y=H-4-(RANK(s.rung)/n)*(H-8);
    out+=`<rect x="${x.toFixed(1)}" y="${(y-3).toFixed(1)}" width="${w.toFixed(1)}" height="5" rx="1.5" fill="${stepColor(s)}"/>`;
  }
  return out+"</svg>";
}
function visible(){
  const q=state.q.toLowerCase();
  return DATA.traces.filter(t=>{
    if(state.repo && t.repo!==state.repo) return false;
    if(state.filter==="running" && t.status!=="running") return false;
    if(state.filter==="trouble" && (t.status==="accepted"||t.status==="running")) return false;
    if(state.filter==="escalated" && !(t.steps.filter(s=>s.role==="implementer").length>1 || t.steps.some(s=>!s.ok))) return false;
    if(q && !(`${t.task} ${t.title} ${t.repo} ${t.steps.map(s=>s.model+" "+s.rung).join(" ")}`.toLowerCase().includes(q))) return false;
    return true;
  });
}
function renderStats(list){
  const n=list.length, acc=list.filter(t=>t.status==="accepted").length;
  const esc=list.filter(t=>t.steps.filter(s=>s.role==="implementer").length>1).length;
  const hum=list.filter(t=>t.status==="needs_human").length, run=list.filter(t=>t.status==="running").length;
  const walls=list.map(t=>t.wall_s).filter(x=>x!=null).sort((a,b)=>a-b);
  const med=walls.length?walls[Math.floor(walls.length/2)]:null;
  const u=list.reduce((a,t)=>a+(t.cost.units||0),0);
  $("#stats").innerHTML=`<span><strong>${n}</strong> jobs</span><span><strong>${acc}</strong> accepted</span><span><strong>${esc}</strong> escalated</span><span><strong>${hum}</strong> need a human</span>${run?`<span><strong>${run}</strong> running</span>`:""}<span>median <strong>${dur(med)}</strong></span><span><strong>${units(u)}</strong> units</span>`;
}
function renderSummary(list){
  const att={}, fails={};
  let direct=0, climbed=0;
  for(const t of list){
    for(const s of t.steps){ if(!s.rung||s.rung==="?"||s.live) continue;
      const a=att[s.rung]||(att[s.rung]={ok:0,bad:0,plan:0,rev:0});
      if(s.role==="planner") a.plan++; else if(s.role==="reviewer") a.rev++; else if(s.ok===false) a.bad++; else a.ok++;
      if(s.ok===false && s.failure_class) fails[s.failure_class]=(fails[s.failure_class]||0)+1; }
    if(t.status==="accepted" && t.task.startsWith("t-")){ (t.steps.filter(s=>s.role==="implementer").length>1)?climbed++:direct++; }
  }
  const rungs=Object.keys(att).sort((a,b)=>RANK(a)-RANK(b));
  const max=Math.max(1,...rungs.map(r=>{const a=att[r];return a.ok+a.bad+a.plan+a.rev;}));
  const bw=Math.max(22,Math.min(34,Math.floor(400/Math.max(rungs.length,1)))), H=40;
  let svg=`<svg width="${rungs.length*bw}" height="${H+14}" role="img" aria-label="attempts per rung">`;
  rungs.forEach((r,i)=>{ const a=att[r]; let y=H; const x=i*bw+2, w=bw-6;
    for(const [k,c] of [["ok","var(--ok)"],["bad","var(--bad)"],["plan","var(--plan)"],["rev","var(--rev)"]]){ const h=a[k]/max*(H-4); if(!h) continue; y-=h; svg+=`<rect x="${x}" y="${y}" width="${w}" height="${h}" fill="${c}"><title>${r}: ${a[k]} ${({ok:"passed attempts",bad:"failed attempts",plan:"plans",rev:"reviews"})[k]}</title></rect>`; }
    svg+=`<text x="${x+w/2}" y="${H+11}" font-size="9.5" text-anchor="middle" fill="var(--ink-3)" font-family="var(--mono)">${esc(r)}</text>`; });
  svg+="</svg>";
  const fl=Object.entries(fails).sort((a,b)=>b[1]-a[1]);
  const fmax=Math.max(1,...fl.map(x=>x[1]));
  const tot=direct+climbed;
  $("#summary").innerHTML=`<div class="sm"><h4>Model sessions per rung</h4>${rungs.length?svg:"<span class=na>none</span>"}</div>
   <div class="sm"><h4>Failures by class</h4>${fl.length?fl.map(([k,v])=>`<div class="fb" title="${esc(DATA.failureMeaning[k]||k)}"><span>${esc(k)}</span><span><i style="width:${v/fmax*100}%"></i></span><b class="num">${v}</b></div>`).join(""):"<span class=na>none</span>"}</div>
   <div class="sm"><h4>Accepted jobs</h4>${tot?`<div class="split"><i style="flex:${direct};background:var(--ok)"></i><i style="flex:${climbed};background:var(--warn)"></i></div><div class="lg"><span><b class="num">${direct}</b> on the first rung</span><span><b class="num">${climbed}</b> after escalating</span></div>`:"<span class=na>none</span>"}</div>`;
}
function renderBoard(){
  const list=visible(); renderStats(list); renderSummary(list);
  let html="", day=null;
  for(const t of list){
    const d=t.t0?fmtDay(t.t0):"unknown";
    if(d!==day){ html+=`<div class="day">${esc(d)}</div>`; day=d; }
    const ct=costText(t), mr=maxRung(t);
    html+=`<div class="row st-${esc(t.status)} ${t.task===state.sel?"sel":""}" data-t="${esc(t.task)}" tabindex="0">
      <div class="pip" title="${esc(t.status)}"></div>
      <div class="t">${t.title?esc(t.title):`<em class="untitled">${esc(t.type||"task")} task in ${esc(t.repo||"?")}</em>`}</div>
      <div class="g"><div class="gl">${glyph(t,120)}<span class="mx" title="highest rung reached">${esc(mr||"")}</span></div>${timeBar(t)}
        <span class="num">${dur(t.status==="running"?endOf(t)-t.t0:t.wall_s)}</span> · <span class="num ${ct.na?"na":""}" title="${esc(ct.tip)}">${esc(ct.txt)}</span></div>
      <div class="m"><span class="num">${esc(t.task)}</span> · ${esc(t.repo||"?")} · ${esc(t.route||"?")}/${esc(t.difficulty||"?")} · ${t.t0?ago(t.t0):""}${t.shape?` <span class="tag">${esc(t.shape)}</span>`:""}</div></div>`;
  }
  $("#list").innerHTML = html || `<div class="empty">No jobs match.</div>`;
}

/* ---------- ladder chart ---------- */
function ladderChart(t){
  const steps=t.steps.filter(s=>s.t0!=null);
  const used=new Set(steps.map(s=>s.rung));
  const rows=[...new Set([...(t.ladder||[]), ...steps.map(s=>s.rung)])].sort((a,b)=>RANK(b)-RANK(a));
  const model={}; for(const s of steps) model[s.rung]=model[s.rung]||s.model;
  const W=1000, L=170, R=24, top=34, rh=40, H=top+rows.length*rh+62;
  const t0=t.t0||(steps[0]&&steps[0].t0)||0, t1=Math.max(endOf(t), ...steps.map(s=>s.t1||0)), span=Math.max(t1-t0,1);
  const X=v=>L+(v-t0)/span*(W-L-R), Y=r=>top+rows.indexOf(r)*rh;
  let g=`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="ladder chart">
  <defs><pattern id="hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="6" stroke="var(--rule)" stroke-width="3"/></pattern>
  <marker id="ah" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L8 4L0 8z" fill="var(--ink-2)"/></marker></defs>`;
  g+=`<text x="12" y="${top-12}" font-size="10.5" fill="var(--ink-3)" letter-spacing=".08em">↑ STRONGER / PRICIER</text><text x="12" y="${top+rows.length*rh+30}" font-size="10.5" fill="var(--ink-3)" letter-spacing=".08em">↓ CHEAPER</text>`;
  rows.forEach((r,i)=>{
    const y=Y(r), u=used.has(r);
    if(i%2===0) g+=`<rect x="0" y="${y}" width="${W}" height="${rh}" fill="var(--paper-2)" opacity=".6"/>`;
    g+=`<text x="12" y="${y+17}" font-family="var(--mono)" font-size="13" font-weight="600" fill="${u?"var(--ink)":"var(--idle)"}">${esc(r)}</text>`;
    g+=`<text x="12" y="${y+32}" font-size="11" fill="var(--ink-3)">${esc(u?(model[r]||""):"not reached")}</text>`;
  });
  // ticks
  for(let k=0;k<=4;k++){ const v=t0+span*k/4, x=X(v);
    g+=`<line x1="${x}" x2="${x}" y1="${top}" y2="${top+rows.length*rh}" stroke="var(--rule)" stroke-dasharray="2 4"/>`;
    g+=`<text x="${x}" y="${H-8}" font-size="11" text-anchor="${k===0?"start":k===4?"end":"middle"}" fill="var(--ink-3)" font-family="var(--mono)">${fmtClock(v)}${k?` (+${dur(v-t0)})`:""}</text>`; }
  // where time went: model time vs mr's own checks
  const ay=top+rows.length*rh+8;
  g+=`<rect x="${L}" y="${ay}" width="${W-L-R}" height="6" fill="url(#hatch)"/>`;
  for(const s of steps) g+=`<rect x="${X(s.t0)}" y="${ay}" width="${Math.max(X(s.t1||t1)-X(s.t0),1)}" height="6" fill="var(--ink-3)"/>`;
  g+=`<text x="${W-R}" y="${ay+20}" font-size="10.5" text-anchor="end" fill="var(--ink-3)">bottom strip: ▮ a model was working · ▨ mr's own checks, tests, git</text>`;
  // path: every hand-off between consecutive steps (dashed = the previous step failed)
  for(let i=1;i<steps.length;i++){ const a=steps[i-1], b=steps[i];
    const x1=X(a.t1||a.t0), y1=Y(a.rung)+rh/2, x2=X(b.t0), y2=Y(b.rung)+rh/2;
    if(a.rung===b.rung && x2-x1<4) continue;
    const mx=Math.max(x1,x2)+0, c=(y1===y2)?`M${x1} ${y1} L${x2} ${y2}`:`M${x1} ${y1} C ${x1+18} ${y1}, ${x2-18} ${y2}, ${x2} ${y2}`;
    g+=`<path d="${c}" fill="none" stroke="${a.ok===false?"var(--bad)":"var(--ink-3)"}" stroke-width="1.4" stroke-dasharray="${a.ok===false?"4 3":"0"}" opacity=".8"/>`; }
  // bars
  for(const s of steps){
    const x=X(s.t0), w=Math.max(X(s.t1||t1)-x,3), y=Y(s.rung)+8, h=rh-16, col=stepColor(s);
    const lab=`${VERB[s.role]||s.role} · ${dur((s.t1||t1)-s.t0)}`;
    g+=`<g class="bar" data-i="${s.idx}"><title>${esc(s.line)}</title><rect x="${x}" y="${y}" width="${w}" height="${h}" rx="4" fill="${col}" ${s.live?'opacity=".75"':""}/>`;
    if(w>lab.length*6.6+16) g+=`<text x="${x+8}" y="${y+h/2+4}" font-size="12" font-weight="500" fill="var(--paper)">${esc(lab)}</text>`;
    else { const right = x+w+130 > W; g+=`<text x="${right?x-5:x+w+5}" y="${y+h/2+4}" font-size="11.5" text-anchor="${right?"end":"start"}" fill="var(--ink-2)">${esc(lab)}</text>`; }
    if(!s.ok && !s.live){ const f=`✕ ${s.role==="reviewer"?"review said no":(s.failure_class||s.decision||"failed")}`, right = x+w+140 > W;
      g+=`<text x="${right?x+w:x}" y="${y-2}" font-size="11" font-weight="600" text-anchor="${right?"end":"start"}" fill="var(--bad)">${esc(f)}</text>`; }
    g+=`</g>`;
  }
  // gate / advisor events
  for(const e of t.events||[]){ if(!e.t) continue; const x=X(e.t);
    const c=e.kind==="guard"?(e.allow?"var(--ok)":"var(--bad)"):"var(--rev)";
    g+=`<g><title>${esc(fmtClock(e.t)+" "+e.label)}</title><rect x="${x-5}" y="${top-14}" width="10" height="10" transform="rotate(45 ${x} ${top-9})" fill="${c}"/></g>`; }
  return g+"</svg>";
}

/* ---------- detail ---------- */
function sig(v){ if(v===true||v==="pass") return "y"; if(v===false||(typeof v==="string"&&v.startsWith("fail"))) return "n"; return ""; }
const SIGNAME={H:"Hygiene (typecheck/lint/existing tests)",D:"Diff stayed in scope and size",A:"Acceptance tests",C:"Cross-family review"};
function toolStrip(tools){
  if(!tools||!tools.length) return "";
  const c={}; for(const x of tools) if(x.kind!=="say") c[x.name]=(c[x.name]||0)+1;
  const counts=Object.entries(c).sort((a,b)=>b[1]-a[1]).map(([k,v])=>`${esc(k)} ${v}`).join(" · ");
  return `<div class="tools" aria-label="tool calls in order">${tools.map(x=>`<i class="k-${esc(x.kind)}" title="${esc(x.name+(x.hint?": "+x.hint:""))}"></i>`).join("")}<span class="tcount">${counts}</span></div>
  <details><summary>${tools.filter(x=>x.kind!=="say").length} tool calls, in order</summary><div class="toollist">${tools.map(x=>x.kind==="say"?`<div style="color:var(--ink-3)">“${esc(x.hint)}”</div>`:`<div><b>${esc(x.name)}</b> ${esc(x.hint||"")}</div>`).join("")}</div></details>`;
}
function stepCard(s){
  const sg=["H","D","A","C"].map(k=>`<i class="${sig(s.signals[k])}" title="${esc(SIGNAME[k]+": "+(s.signals[k]??"not run"))}">${k}${sig(s.signals[k])==="y"?"✓":sig(s.signals[k])==="n"?"✕":"·"}</i>`).join("");
  const tok=s.tokens||{};
  let h=`<li class="step ${s.live?"":s.ok?"ok":"bad"}"><div><div class="sl"><span class="role role-${esc(s.role)}">${esc(VERB[s.role]||s.role)}</span>${esc(s.rung)} · ${esc(s.model||"")} ${s.live?"<em>(running now)</em>":""}</div>
    <div class="sub"><span class="num">${s.t0?fmtClock(s.t0):""}</span><span class="num">${dur(s.wall_s ?? (s.t1&&s.t0?s.t1-s.t0:null))}</span>
    ${Object.keys(s.signals||{}).length?`<span class="sig">${sg}</span>`:""}
    ${tok.in!=null?`<span class="num">${kfmt(tok.in)} in · ${kfmt(tok.out)} out${tok.cache?` · ${kfmt(tok.cache)} cached`:""}</span>`:""}
    ${s.usd?`<span class="num">$${(+s.usd).toFixed(2)} API-equiv</span>`:""}
    ${s.diff&&s.diff.files?`<span class="num">+${s.diff.add} −${s.diff.del} in ${s.diff.files} file${s.diff.files==1?"":"s"}</span>`:""}
    ${s.mr_result?`<span>model said: <b>${esc(s.mr_result)}</b>${s.confidence!=null?` (${Math.round(s.confidence*100)}% sure)`:""}</span>`:""}
    ${s.escalated_from?`<span>escalated from ${esc(s.escalated_from)}</span>`:""}</div>`;
  if(!s.ok && !s.live){ const m=DATA.failureMeaning[s.failure_class]; h+=`<div class="fail"><b>✕ ${esc(s.failure_class||s.decision||"failed")}</b>${m?` — ${esc(m)}`:""}${s.is_error?" (process error)":""}</div>`; }
  if(s.pushback){ const p=s.pushback; h+=`<div class="push"><b>Pushback (${esc(p.type||"")}):</b> ${esc(p.evidence||"")}${p.proposal?`<br><b>Proposal:</b> ${esc(p.proposal)}`:""}</div>`; }
  h+=toolStrip(s.tools);
  if(s.summary) h+=`<details><summary>Model's last words</summary><div class="said">${esc(s.summary)}</div></details>`;
  if(s.patch||s.raw) h+=`<div class="paths">${s.patch?`patch ${esc(s.patch)}<br>`:""}${s.raw?`raw ${esc(s.raw)}`:""}</div>`;
  return h+"</div></li>";
}
function renderDetail(){
  const t=DATA.traces.find(x=>x.task===state.sel);
  if(!t){ $("#detail").innerHTML=`<div class="empty">Pick a job on the left. Each row's little chart is the job's climb up the model ladder: left to right is time, higher means a more expensive model, red means that attempt failed.</div>`; return; }
  t.steps.forEach((s,i)=>s.idx=i);
  const ladder=(t.ladder||[]).map(r=>`<span class="rg ${t.steps.some(s=>s.rung===r)?"used":""} ${r===t.final_rung&&t.status==="accepted"?"final":""}">${esc(r)}</span>`).join('<span class="arrow">→</span>');
  const fails=[...new Set(t.steps.filter(s=>!s.ok&&!s.live&&s.failure_class).map(s=>s.failure_class))];
  const chrono=[...t.steps.map(s=>({t:s.t0||0,h:stepCard(s)})), ...(t.events||[]).map(e=>({t:e.t||0,h:`<li class="step ev"><div><span class="num">${e.t?fmtClock(e.t):""}</span> ${esc(e.label)}</div></li>`}))].sort((a,b)=>a.t-b.t);
  const src=t.route_sources||{};
  $("#detail").innerHTML=`<div class="wrap">
   <div class="crumb"><span class="num">${esc(t.task)}</span><span>${esc(t.repo||"")}</span><span>${t.t0?esc(fmtWhen(t.t0))+" IST":""}</span>${t.caller?`<span>called by ${esc(t.caller)}</span>`:`<span title="mr does not record the calling agent yet">caller not recorded</span>`}<span>exit ${t.exit??"–"}</span></div>
   <h2 class="title">${t.title?esc(t.title):`<em class="untitled">${esc(t.type||"task")} task in ${esc(t.repo||"?")} <small>(mr kept no title for this task)</small></em>`}</h2>
   <div class="headline h-${esc(t.status)}">${esc(t.headline)}${t.why&&t.status!=="accepted"?`<div class="whyline">${esc(t.why)}</div>`:""}</div>
   <div class="facts"><span>route <b>${esc(t.route||"?")}</b>${src.route?` (${esc(src.route)})`:""}</span><span>type <b>${esc(t.type||"?")}</b></span><span>difficulty <b>${esc(t.difficulty||"?")}</b>${src.difficulty?` (${esc(src.difficulty)})`:""}</span><span>risk <b>${esc(t.risk||"?")}</b></span>
    <span>wall <b class="num">${dur(t.status==="running"?endOf(t)-t.t0:t.wall_s)}</b>${t.time&&t.time.wall_s?` = model <b class="num">${dur(t.time.model_s)}</b> + mr checks <b class="num">${dur(t.time.mr_s)}</b>${t.time.unlogged_s?` + unlogged <b class="num">${dur(t.time.unlogged_s)}</b>`:""}`:""}</span><span title="${esc(costText(t).tip)}">cost <b class="num">${esc(costText(t).txt)}</b></span>${t.cost.claude_pts?`<span>Claude pts <b class="num">${t.cost.claude_pts}</b></span>`:""}<span>tokens <b class="num">${kfmt(t.cost.tok_in)} in / ${kfmt(t.cost.tok_out)} out</b></span>${t.cost.usd_equiv?`<span>API-equiv <b class="num">$${(+t.cost.usd_equiv).toFixed(2)}</b></span>`:""}</div>
   <div class="ladder-legend">ladder ${ladder||"–"}</div>
   <section><h3>The climb</h3><div class="chart">${ladderChart(t)}</div>
   ${fails.length?`<div class="why">${fails.map(f=>`<b>${esc(f)}</b>: ${esc(DATA.failureMeaning[f]||"")}`).join(" · ")}</div>`:""}</section>
   <section><h3>Step by step</h3><ol class="steps">${chrono.map(x=>x.h).join("")}</ol></section></div>`;
}
function select(id){ state.sel=id; history.replaceState(null,"","#"+encodeURIComponent(id)); renderBoard(); renderDetail(); $("#detail").scrollTop=0; if(matchMedia("(max-width:900px)").matches) $("#detail").scrollIntoView({behavior:"smooth"}); }

/* ---------- wiring ---------- */
function init(){
  const repos=[...new Set(DATA.traces.map(t=>t.repo).filter(Boolean))].sort();
  $("#repo").innerHTML=`<option value="">all repos</option>`+repos.map(r=>`<option>${esc(r)}</option>`).join("");
  $("#list").addEventListener("click",e=>{const r=e.target.closest(".row"); if(r) select(r.dataset.t);});
  $("#list").addEventListener("keydown",e=>{const r=e.target.closest(".row"); if(r&&e.key==="Enter") select(r.dataset.t);});
  document.querySelectorAll("[data-f]").forEach(b=>b.addEventListener("click",()=>{state.filter=b.dataset.f;document.querySelectorAll("[data-f]").forEach(x=>x.setAttribute("aria-pressed",x===b));renderBoard();}));
  $("#repo").addEventListener("change",e=>{state.repo=e.target.value;renderBoard();});
  $("#q").addEventListener("input",e=>{state.q=e.target.value;renderBoard();});
  document.addEventListener("keydown",e=>{
    if(e.target.tagName==="INPUT") { if(e.key==="Escape") e.target.blur(); return; }
    if(e.key==="/"){e.preventDefault();$("#q").focus();return;}
    if(e.key!=="j"&&e.key!=="k") return;
    const l=visible(), i=l.findIndex(t=>t.task===state.sel), n=e.key==="j"?Math.min(i+1,l.length-1):Math.max(i-1,0);
    if(l[n]) { select(l[n].task); const el=document.querySelector(`.row[data-t="${CSS.escape(l[n].task)}"]`); el&&el.scrollIntoView({block:"nearest"}); }
  });
  if(!state.sel && DATA.traces.length) state.sel=DATA.traces[0].task;
  renderBoard(); renderDetail();
  const lv=$("#live");
  lv.innerHTML = DATA.live ? `<b></b>live · updated ${fmtClock(DATA.generated)} IST` : `<b></b>snapshot ${esc(fmtWhen(DATA.generated))} IST`;
  lv.classList.toggle("off",!DATA.live);
  if(DATA.live) setInterval(async()=>{
    try{ const r=await fetch("data.json",{cache:"no-store"}); const d=await r.json();
      const changed=d.sig!==DATA.sig; DATA=d; lv.innerHTML=`<b></b>live · updated ${fmtClock(d.generated)} IST`;
      if(changed||d.traces.some(t=>t.status==="running")){ const sc=$("#detail").scrollTop; renderBoard(); renderDetail(); $("#detail").scrollTop=sc; }
    }catch(e){ lv.classList.add("off"); lv.innerHTML="<b></b>live (server unreachable)"; }
  }, 4000);
}
document.readyState==="loading"?document.addEventListener("DOMContentLoaded",init):init();
})();
