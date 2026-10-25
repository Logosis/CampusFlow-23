const $ = id => document.getElementById(id);
const statuses = {WAITING_ASSIGNMENT:'待派单',ASSIGNED:'已派单',REPAIRING:'维修中',WAITING_ACCEPTANCE:'待验收',ACCEPTED:'已验收',CANCELLED:'已取消'};
const syncNames = {PENDING:'等待处理',SUCCESS:'已确认成功',REJECTED:'已明确拒绝',UNKNOWN:'结果尚未确认',ACCEPTED:'已受理'};
let auth, identity, activeTab;
const pages = {}, loads = {};
let locationLoad = 0;
function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function notice(text, kind='') {
  $('notice').hidden = false; $('notice').className = 'notice ' + kind; $('notice').textContent = text;
}
function errorText(error) {
  const labels = {FORBIDDEN:'当前账号没有此操作权限或不在负责范围内。',UNAUTHENTICATED:'登录已失效，请返回首页重新登录。',CONFLICT:'工单状态或版本已变化，请刷新后查看。',UNAVAILABLE:'服务暂时无法确认结果。请稍后核实，并保留原请求重试。',NOT_FOUND:'未找到对应记录。',REQUEST_ID_REUSED:'原请求编号已用于其他内容，请先核实原操作。',INVALID_ARGUMENT:'填写内容不符合要求，请检查后重试。'};
  return labels[error.code] || error.message || '操作未完成，请稍后重试。';
}
async function safely(work) { try { await work(); } catch (error) { notice(errorText(error), 'error'); } }
function has(code) { return identity.grants.some(g => g.permissionCode === code); }
function dateText(value) { return value ? new Date(value).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai',hour12:false}) : '—'; }
function range(from, to) {
  if (!from && !to) return {};
  if (!from || !to || from >= to) throw new Error('请选择完整日期范围，结束日期必须晚于开始日期。');
  return {startTime: new Date(from + 'T00:00:00+08:00').toISOString(), endTime: new Date(to + 'T00:00:00+08:00').toISOString()};
}
const pendingKey = () => 'campusflow.m3.pending.' + identity.userId;
function getPending() { try { return JSON.parse(sessionStorage.getItem(pendingKey())) || {}; } catch { return {}; } }
function savePending(value) { sessionStorage.setItem(pendingKey(), JSON.stringify(value)); pendingPanel(); }
function pendingPanel() {
  const rows = Object.values(getPending()); $('pending').replaceChildren(); $('pending').hidden = rows.length === 0;
  for (const row of rows) {
    const label = element('p', '有一项操作的结果尚未确认，请使用原请求核实。');
    const retry = element('button', '核实并重试原请求');
    retry.onclick = () => safely(async () => { await sendMutation(row.functionName, row.body, true); await refresh(); });
    label.append(retry); $('pending').append(label);
  }
}
async function sendMutation(functionName, body, retry=false) {
  const pending = getPending();
  if (pending[functionName] && !retry) throw new Error('此类操作仍有未确认请求，请先在上方核实原请求。');
  const input = retry ? body : {...body, requestId:crypto.randomUUID()};
  savePending({...pending, [functionName]:{functionName, body:input}});
  try {
    const result = await auth.api('m3', functionName, input);
    const latest = getPending(); delete latest[functionName]; savePending(latest);
    const sync = result.seatSync;
    const waiting = result.notifications.some(n => ['UNKNOWN','PENDING'].includes(n.status));
    notice('操作已保存。当前阶段：' + statuses[result.workOrder.status] +
      (sync ? '；维修限制：' + syncNames[sync.status] + '。' : '。') + (waiting ? '通知尚待核实。' : ''), sync?.status === 'UNKNOWN' || sync?.status === 'REJECTED' ? '' : 'good');
    return result;
  } catch (error) {
    if (error.httpStatus && error.httpStatus < 500) {
      const latest = getPending(); delete latest[functionName]; savePending(latest);
    }
    throw error;
  }
}
function option(value, label) { const node = element('option', label); node.value = value; return node; }
function openDialog(title) { $('dialog-title').textContent = title; $('dialog-content').replaceChildren(); if (!$('action-dialog').open) $('action-dialog').showModal(); return $('dialog-content'); }
$('dialog-close').onclick = () => $('action-dialog').close();
function button(label, action, primary=false) { const node = element('button', label, primary ? 'primary' : ''); node.type='button'; node.onclick = () => safely(async () => { node.disabled=true; try { await action(); } finally { node.disabled=false; } }); return node; }
function empty(container) { container.append(element('div','当前没有符合条件的工单。','empty')); }
function syncLine(parent, sync) {
  if (!sync) return;
  const box = element('div', '维修限制：' + syncNames[sync.status], 'sync');
  if (sync.result?.currentLimit) box.append(element('div', '本工单限制：' + (sync.result.currentLimit.state === 'ACTIVE' ? '生效中' : '已解除') + '（核实于 ' + dateText(sync.checkedAt) + '）'));
  box.append(element('div', sync.message));
  if (sync.status === 'UNKNOWN') box.append(element('div','工单进度已保存，限制结果仍需核实。'));
  parent.append(box);
}
function card(work, detail=null) {
  const box = element('article',undefined,'card');
  const top = element('div',undefined,'card-top');
  top.append(element('h3', work.faultLocation), element('span', statuses[work.status], 'badge'));
  box.append(top,element('p',work.description),element('div','教室编号：'+work.spaceId,'metadata'),element('div','工单 ' + work.workOrderId + ' · 上报于 ' + dateText(work.createdAt),'metadata'));
  if (detail) syncLine(box,detail.seatSync);
  return box;
}
async function all(functionName, input) {
  const items = []; let cursor; const seen = new Set();
  do {
    const data = await auth.api('m3',functionName,{...input,limit:100,...(cursor ? {cursor} : {})});
    items.push(...data.items); cursor=data.nextCursor;
    if (cursor && seen.has(cursor)) throw new Error('列表加载异常，请刷新。');
    seen.add(cursor);
  } while(cursor);
  return items;
}
async function loadLocations() {
  const generation = ++locationLoad;
  const spaces = await all('get_report_locations',{});
  if (generation !== locationLoad) return;
  const selected = $('report-space').value;
  $('report-space').replaceChildren(option('','请选择报修教室'));
  for (const space of spaces) $('report-space').append(option(space.spaceId,space.name + ' · ' + space.location));
  if (spaces.some(space => space.spaceId === selected)) $('report-space').value = selected;
  if (!spaces.length) notice('当前账号没有可报修的教室。');
}
async function loadPage(name, fn, input, render, append=false) {
  const generation = loads[name] = (loads[name] || 0) + 1;
  const cursor = append ? pages[name]?.cursor : null;
  const data = await auth.api('m3',fn,{...input,limit:20,...(cursor ? {cursor} : {})});
  if (generation !== loads[name]) return;
  const container=$(name+'-list'); if (!append) container.replaceChildren();
  if (!data.items.length && !append) empty(container);
  for (const item of data.items) container.append(render(item));
  pages[name]={input,cursor:data.nextCursor}; $(name+'-next').hidden=!data.nextCursor;
}
function simpleAction(label, fn, work, extra={}, fields=null) {
  return button(label, async () => {
    const content=openDialog(label);
    content.append(element('p',work.faultLocation + ' · ' + statuses[work.status]));
    const form=element('form'); let control;
    if (fields) { const field=element('label',fields.label); control=element('textarea'); control.required=fields.required; control.maxLength=2000; field.append(control); form.append(field); }
    const submit=element('button','确认'+label,'primary'); submit.type='submit'; form.append(submit);
    form.onsubmit=event => { event.preventDefault(); safely(async () => {
      submit.disabled=true;
      try {
        const body={workOrderId:work.workOrderId,...(fn==='submit_evaluation'?{}:{expectedVersion:work.version}),...extra};
        if (fields) body[fields.name]=control.value;
        await sendMutation(fn,body); $('action-dialog').close(); await refresh();
      } finally {submit.disabled=false;}
    });}; content.append(form);
  });
}
function renderTask(item) {
  const w=item.workOrder, box=card(w,item), actions=element('div',undefined,'actions'), purpose=$('purpose').value;
  if (purpose==='REPAIR' && w.status==='ASSIGNED') actions.append(simpleAction('开始维修','start_repair',w));
  if (purpose==='REPAIR' && w.status==='REPAIRING') actions.append(simpleAction('提交完工','submit_completion',w,{}, {name:'summary',label:'完工说明',required:true}));
  if (purpose==='ACCEPTANCE' && w.status==='WAITING_ACCEPTANCE') {
    const latest=item.completions.at(-1);
    if (latest) {
      box.append(element('p','完工说明：'+latest.summary));
      actions.append(simpleAction('验收通过','submit_acceptance',w,{completionId:latest.completionId,decision:'PASS'},{name:'comment',label:'验收说明（选填）',required:false}),
        simpleAction('退回返修','submit_acceptance',w,{completionId:latest.completionId,decision:'FAIL'},{name:'comment',label:'退回原因（选填）',required:false}));
    }
  }
  if (purpose==='EVALUATION' && w.status==='ACCEPTED' && !item.evaluations.length) actions.append(simpleAction('提交评价','submit_evaluation',w,{}, {name:'content',label:'维修评价',required:true}));
  actions.append(button('处理记录',()=>showRecords(item))); box.append(actions); return box;
}
function showRecords(item, timeline=null) {
  const content=openDialog('工单进度'); content.append(card(item.workOrder,item));
  const events=timeline || [
    ...item.assignments.map(r=>({occurredAt:r.assignedAt,summary:'派单：'+r.comment})),
    ...item.completions.map(r=>({occurredAt:r.submittedAt,summary:'完工：'+r.summary})),
    ...item.acceptances.map(r=>({occurredAt:r.decidedAt,summary:(r.decision==='PASS'?'验收通过':'退回返修')+'：'+r.comment})),
    ...item.evaluations.map(r=>({occurredAt:r.createdAt,summary:'评价：'+r.content})),
    ...item.escalations.map(r=>({occurredAt:r.triggeredAt,summary:'阶段超时升级（第'+r.cycle+'轮）'}))
  ].sort((a,b)=>a.occurredAt.localeCompare(b.occurredAt));
  const list=element('ol',undefined,'timeline'); for (const event of events) list.append(element('li',dateText(event.occurredAt)+' · '+event.summary)); content.append(list);
}
function renderOrder(w) {
  const box=card(w), actions=element('div',undefined,'actions');
  actions.append(button('查看进度',async()=>{const data=await auth.api('m3','get_work_order',{workOrderId:w.workOrderId});showRecords(data.detail,data.timeline);}));
  if (has('m3.work_order.cancel') && identity.userId===w.reporterId && ['WAITING_ASSIGNMENT','ASSIGNED'].includes(w.status)) actions.append(simpleAction('取消工单','cancel_work_order',w,{}, {name:'reason',label:'取消原因（选填）',required:false}));
  box.append(actions); return box;
}
function renderDispatch(item) {
  const w=item.workOrder, box=card(w,item);
  box.append(button('选择承接人',async()=>{
    const candidates=await all('get_dispatch_candidates',{workOrderId:w.workOrderId});
    const content=openDialog('派发维修工单'), form=element('form'), label=element('label','承接人'), select=element('select');
    select.required=true; select.setAttribute('aria-label','承接人'); select.append(option('','请选择维修人员')); candidates.forEach(p=>select.append(option(p.userId,p.displayName))); label.append(select);
    const commentLabel=element('label','派单说明（选填）'), comment=element('textarea'); comment.maxLength=2000; commentLabel.append(comment);
    const submit=element('button','确认派单','primary'); submit.type='submit'; submit.disabled=!candidates.length;
    form.append(label,commentLabel,submit); if (!candidates.length) content.append(element('p','当前没有符合资格的维修人员。'));
    form.onsubmit=event=>{event.preventDefault();safely(async()=>{submit.disabled=true;try{await sendMutation('dispatch_order',{workOrderId:w.workOrderId,assigneeId:select.value,comment:comment.value,expectedVersion:w.version});$('action-dialog').close();await refresh();}finally{submit.disabled=false;}});}; content.append(form);
  },true)); return box;
}
const ordersInput=()=>({view:$('view').value,...($('order-status').value?{status:$('order-status').value}:{}),...range($('order-from').value,$('order-to').value)});
async function refresh() {
  if (activeTab==='report') return loadLocations();
  if (activeTab==='orders') return loadPage('orders','get_work_orders',ordersInput(),renderOrder);
  if (activeTab==='tasks') return loadPage('tasks','get_work_tasks',{purpose:$('purpose').value},renderTask);
  if (activeTab==='dispatch') return loadPage('dispatch','get_dispatch_tasks',{},renderDispatch);
}
async function activate(name) {
  activeTab=name; for (const panel of document.querySelectorAll('section.panel')) panel.hidden=panel.id!==name;
  for (const tab of $('tabs').children) tab.setAttribute('aria-selected',String(tab.dataset.tab===name));
  await refresh();
}
for (const tab of $('tabs').children) tab.onclick=()=>safely(()=>activate(tab.dataset.tab));
for (const [code,label] of Object.entries(statuses)) $('order-status').append(option(code,label));
$('report-form').onsubmit=event=>{event.preventDefault();safely(async()=>{
  const submit=event.target.querySelector('button');submit.disabled=true;
  try{const result=await sendMutation('report_fault',{spaceId:$('report-space').value,faultLocation:$('fault-location').value,description:$('description').value});$('fault-location').value='';$('description').value='';notice('报修已提交，工单编号：'+result.workOrder.workOrderId+'。','good');}finally{submit.disabled=false;}
});};
$('orders-form').onsubmit=event=>{event.preventDefault();safely(refresh);};
$('tasks-form').onsubmit=event=>{event.preventDefault();safely(refresh);};
$('dispatch-refresh').onclick=()=>safely(refresh);
for (const [name,fn,render] of [['orders','get_work_orders',renderOrder],['tasks','get_work_tasks',renderTask],['dispatch','get_dispatch_tasks',renderDispatch]]) $(name+'-next').onclick=()=>safely(()=>loadPage(name,fn,pages[name].input,render,true));
$('stats-form').onsubmit=event=>{event.preventDefault();safely(async()=>{
  const data=await auth.api('m3','get_maintenance_stats',range($('stats-from').value,$('stats-to').value));
  const box=element('div',undefined,'stats');
  for (const [label,value] of [['工单总数',data.totalCount],['超时工单',data.overdueCount],['平均维修时长',data.averageRepairSeconds===null?'暂无样本':(data.averageRepairSeconds/3600).toFixed(1)+' 小时']]) {const tile=element('div',label,'stat');tile.append(element('strong',String(value)));box.append(tile);}
  $('stats-result').replaceChildren(box,element('p','时长样本：'+data.sampleCount+' 单 · 统计于 '+dateText(data.checkedAt),'muted'));
  const stages=element('div',undefined,'cards');for (const [status,count] of Object.entries(data.statusCounts)) stages.append(element('div',statuses[status]+'：'+count));$('stats-result').append(stages);
});};
await safely(async()=>{
  try{auth=await import('/shared/auth.js');}catch{throw new Error('登录服务暂未就绪，请稍后从首页进入报修。');}
  if (!auth.getCredential()){location.assign('/login.html');return;}
  identity=await auth.api('d','get_identity',{});$('identity').textContent=identity.displayName;
  const access={report:has('m3.work_order.report'),orders:has('m3.work_order.read'),dispatch:has('m3.dispatch.manage'),stats:has('m3.statistics.read'),tasks:false};
  for (const [purpose,code,label] of [['REPAIR','m3.repair.manage','维修任务'],['ACCEPTANCE','m3.acceptance.manage','验收任务'],['EVALUATION','m3.evaluation.create','待评价工单']]) if(has(code)) {$('purpose').append(option(purpose,label));access.tasks=true;}
  $('view').querySelector('[value=MANAGED]').hidden=!has('m3.work_order.read_all');
  for (const tab of $('tabs').children) tab.hidden=!access[tab.dataset.tab];$('tabs').hidden=false;pendingPanel();
  const first=Object.keys(access).find(key=>access[key]);if(first)await activate(first);else notice('当前账号暂无报修功能权限，请联系管理员。');
});
