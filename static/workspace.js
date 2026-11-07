'use strict';
/* Optional multi-user workspace. The original local-only mode works unchanged. */
window.smechWorkspace=(()=>{
 const ROOT='/api/workspace';
 let active=false, enabled=false, initialized=false, username='', isAdmin=false, csrf='';
 let baseline=null, busy=false, blocked=false, dirty=false, timer=null, status='';
 let pendingBackup=null;
 const clone=x=>JSON.parse(JSON.stringify(x));
 const stripProject=p=>{const v=clone(p);delete v.snapshots;return v;};
 const same=(a,b)=>JSON.stringify(a)===JSON.stringify(b);
 const showStatus=msg=>{status=msg;const el=document.getElementById('workspaceSync');if(el)el.textContent=msg;};
 async function api(path,options={}){
  const headers={...(options.body?{'Content-Type':'application/json'}:{}),...(options.headers||{})};
  if(options.method&&!['GET','HEAD'].includes(options.method))headers['X-CSRF-Token']=csrf;
  const response=await fetch(ROOT+path,{...options,headers,credentials:'same-origin'});
  let result;try{result=await response.json();}catch{result={};}
  if(!response.ok){const detail=result.detail||result.message||`HTTP ${response.status}`;
    const error=new Error(typeof detail==='string'?detail:JSON.stringify(detail));error.status=response.status;throw error;}
  return result;
 }
 async function queryInfo(){try{const result=await api('/info');enabled=!!result.enabled;initialized=!!result.initialized;}catch{enabled=false;}}
 async function getIdentity(){const me=await api('/me');username=me.username;isAdmin=me.is_admin;csrf=me.csrf;}
 function capture(){return {projects:data.projects.map(stripProject),active:data.active,catalog:clone(data.catalog)};}
 function changes(current){
  const old=new Map((baseline?.projects||[]).map(p=>[p.id,stripProject(p)]));
  const now=new Map(current.projects.map(p=>[p.id,p]));
  const upserts=[],deletes=[];
  for(const [id,p] of now)if(!old.has(id)||!same(old.get(id),p))upserts.push({project:p,expectedRevision:baseline?.revisions?.[id]??null});
  for(const [id] of old)if(!now.has(id))deletes.push({id,expectedRevision:baseline.revisions[id]});
  const catalog=!same(current.catalog,baseline?.catalog)?{items:current.catalog,expectedRevision:baseline.catalogRevision}:null;
  return {upserts,deletes,catalog,active:current.active};
 }
 function queue(){
  if(!active)return;
  dirty=true;
  if(blocked){showStatus('Изменения не сохранены: требуется разрешить конфликт');return;}
  showStatus('Сохранение...');
  if(timer)clearTimeout(timer);
  timer=setTimeout(()=>flush(),450);
 }
 async function flush(){
  if(!active||blocked)return false;
  if(busy){dirty=true;return false;}
  if(timer){clearTimeout(timer);timer=null;}
  const current=capture(),payload=changes(current);
  if(!payload.upserts.length&&!payload.deletes.length&&!payload.catalog&&current.active===baseline.active){dirty=false;showStatus('Сохранено на сервере');return true;}
  busy=true;dirty=false;
  try{
   const updated=await api('/sync',{method:'POST',body:JSON.stringify(payload)});
   baseline=updated;
   showStatus('Сохранено на сервере');
   if(dirty||!same(current,capture())){dirty=false;queue();}
   return true;
  }catch(error){
   blocked=true;
   showStatus(error.status===409?'Конфликт редакций — сохранение приостановлено':'Сервер недоступен — изменения пока не сохранены');
   pendingBackup=clone(data);
   // A local recovery file remains available even if the cloud write fails.
   try{sessionStorage.setItem('smech_unsaved_workspace',JSON.stringify({username,at:new Date().toISOString(),data:pendingBackup}));}catch{}
   toast('Облачное сохранение: '+error.message+'; скачайте резервную копию перед перезагрузкой');
   return false;
  }finally{busy=false;}
 }
 async function loadCloud(){
  if(busy){await new Promise(r=>setTimeout(r,500));if(busy)throw Error('Дождитесь сохранения');}
  const state=await api('/state');
  if(!state.projects?.length)throw Error('В учётной записи нет объектов. Обратитесь к администратору.');
  baseline=state;
  data=normalizeData(state);
  active=true;blocked=false;dirty=false;pendingBackup=null;
  try{sessionStorage.setItem('smech_current_mode','workspace');sessionStorage.removeItem('smech_unsaved_workspace');}catch{}
  view='dashboard';
  showStatus('Сохранено на сервере');
  await refresh();
 }
 async function open(){
  await queryInfo();
  if(!enabled){toast('Серверное пространство отключено. Включите SMECH_ENABLE_WORKSPACE=1 в локальной конфигурации.');return;}
  if(!initialized)return loginDialog(true);
  return loginDialog(false);
 }
 function loginDialog(first){
  const title=first?'Создать первую учётную запись':'Вход в рабочее пространство';
  const explanation=first?'Первого администратора можно создать только на локальном компьютере или по предварительно установленному ключу. Данные обычного локального режима не импортируются автоматически.':'Для совместной работы нужна учётная запись. Локальные данные сохраняются отдельно и не передаются при входе.';
  const fields=`<div class="form-grid">${field('Логин (латиница)','username','','text','required autocomplete="username" pattern="[A-Za-z0-9_.-]{3,60}"')}${field('Пароль (12–128 символов)','password','','password','required minlength="12" maxlength="128" autocomplete="'+(first?'new-password':'current-password')+'"')}${first?field('Ключ первичной настройки (если задан)','setupKey','','password'):''}</div>`;
  openModal(title,explanation,fields,first?'Создать администратора':'Войти',async fd=>{
   const entered=await api(first?'/setup':'/login',{method:'POST',body:JSON.stringify({username:String(fd.get('username')||''),password:String(fd.get('password')||''),setupKey:String(fd.get('setupKey')||'')})});
   username=entered.username;csrf=entered.csrf;isAdmin=entered.is_admin;
   await loadCloud();toast('Рабочее пространство: '+username);
  });
 }
 async function switchLocal(){
  if(busy||blocked||dirty){toast('Сначала сохраните данные на сервере или скачайте резервную копию при ошибке');return;}
  active=false;baseline=null;username='';csrf='';
  try{sessionStorage.removeItem('smech_current_mode');}catch{}
  data=load();view='dashboard';await refresh();
 }
 function storageLabel(){
  return active?`<span class="green-dot"></span><div><b>Рабочее пространство</b><small id="workspaceSync">${esc(status||'Сохранено на сервере')}</small></div>`:
   `<span class="green-dot"></span><div><b>Локальный режим</b><small>Данные в текущем браузере</small></div>`;
 }
 function updateHeader(){const e=document.getElementById('storageStatus');if(e)e.innerHTML=storageLabel();}
 function noticeHtml(){
  if(!active)return '';
  let html=`<div class="alert info"><b>Серверный режим: ${esc(username)}.</b> ${esc(status||'Сохранено')}. Объекты доступны только назначенным пользователям. Автономная локальная копия не обновляется.</div>`;
  if(blocked)html+=`<div class="alert"><b>Изменения не сохранены.</b> Не закрывайте вкладку. Сначала скачайте резервную копию, затем можно повторить сохранение или загрузить серверную версию. <button class="btn tiny" data-action="backup">Скачать JSON</button> <button class="btn tiny" data-action="wsRetry">Повторить</button> <button class="btn tiny danger" data-action="wsReload">Отменить мои изменения</button></div>`;
  const role=baseline?.roles?.[data.active];
  if(role==='viewer')html+=`<div class="alert"><b>Только просмотр.</b> Сервер отклонит редактирование этого объекта. Для изменения запросите роль редактора у владельца.</div>`;
  return html;
 }
 function settingsHtml(){
  if(!active)return panel('Рабочее пространство (версия 0.3)','Дополнительный режим с авторизацией и историей изменений',
   `<div class="panel-body"><p>${enabled?'Подключена база рабочего пространства. Доступ по личным учётным записям.':'По умолчанию отключено. Для локального испытания запустите приложение с SMECH_ENABLE_WORKSPACE=1. Для PostgreSQL укажите SMECH_DATABASE_URL.'}</p><div class="settings-option"><div><b>Войти в серверное пространство</b><div class="help">Локальные проекты останутся в текущем браузере. Перенос возможен после входа и отдельного подтверждения.</div></div><button class="btn primary" data-action="wsOpen">${initialized?'Войти':'Настроить'}</button></div></div>`);
  const role=baseline?.roles?.[data.active];
  return panel('Серверное пространство',`${esc(username)} · роль на объекте: ${esc(role||'не определена')}`,`<div class="panel-body"><div class="settings-option"><div><b>Состояние данных</b><div class="help" id="workspaceSync">${esc(status)}</div></div><button class="btn secondary" data-action="wsRetry">Сохранить сейчас</button></div>
   <div class="settings-option"><div><b>Вернуться в локальный режим</b><div class="help">Локальные проекты будут загружены отдельно, данные серверного режима не сотрутся.</div></div><button class="btn secondary" data-action="wsLocal">Локальный режим</button></div>
   <div class="settings-option"><div><b>Перенести местные объекты в базу</b><div class="help">Не перезаписывает существующие объекты, любые совпадения блокируют перенос.</div></div><button class="btn secondary" data-action="wsImport">Импортировать</button></div>
   <div class="settings-option"><div><b>Сотрудники и доступы</b><div class="help">Владелец назначает редакторов и наблюдателей; администратор создаёт учётные записи.</div></div><button class="btn secondary" data-action="wsAccess">Управлять доступом</button></div>
   <div class="settings-option"><div><b>История действий и версии</b><div class="help">Кто менял ВОР, когда и какие позиции</div></div><div class="actions"><button class="btn secondary" data-action="wsAudit">Журнал</button><button class="btn secondary" data-action="wsDiff">Различия версий</button></div></div>
   <div class="settings-option"><div><b>Удалённые объекты</b><div class="help">Можно восстановить объект, который был удалён владельцем.</div></div><button class="btn secondary" data-action="wsTrash">Корзина</button></div>
   <div class="settings-option"><div><b>Завершение сеанса</b><div class="help">После выхода можно вернуться к локальным объектам.</div></div><button class="btn secondary" data-action="wsLogout">Выйти</button></div></div>`);
 }
 async function snapshot(){
  if(!await flush()){toast('Сначала сохраните несинхронизированные изменения');return;}
  try{
   await api(`/projects/${encodeURIComponent(data.active)}/snapshot`,{method:'POST',body:'{}'});
   await loadCloud();view='history';paint();toast('Архивный снимок создан на сервере');
  }catch(e){toast('Архив: '+e.message);}
 }
 async function downloadSnapshot(id){
  try{
   const body=await api(`/projects/${encodeURIComponent(data.active)}/snapshots/${encodeURIComponent(id)}`);
   download(new Blob([JSON.stringify(body,null,2)],{type:'application/json'}),`Смеч серверный снимок ${id}.json`);
  }catch(e){toast('Архив: '+e.message);}
 }
 async function audit(){
  try{
   const logs=await api(`/projects/${encodeURIComponent(data.active)}/history`);
   const names={created:'Создан',updated:'Изменён',deleted:'Удалён',access:'Выдан доступ',access_revoked:'Отозван доступ',snapshot:'Снимок'};
   const rows=logs.slice(0,70).map(x=>`<tr><td>${esc(new Date(x.at).toLocaleString('ru-RU'))}</td><td>${esc(x.actor)}</td><td>${esc(names[x.action]||x.action)}</td><td>№ ${esc(x.revision)}</td><td>${esc(x.details?.added||0)} новых; ${esc(x.details?.changed||0)} изменено; ${esc(x.details?.removed||0)} удалено</td></tr>`).join('');
   openModal('Журнал действий',`Объект: ${project().name}. Последние ${logs.length} событий. Для аналитического сравнения используйте API /diff.`,`<div class="preview-table"><table><thead><tr><th>Дата</th><th>Пользователь</th><th>Действие</th><th>Версия</th><th>Позиции</th></tr></thead><tbody>${rows}</tbody></table></div>`,'Закрыть',()=>true);
  }catch(e){toast('История: '+e.message);}
 }
 async function accessDialog(){
  try{
   const access=await api(`/projects/${encodeURIComponent(data.active)}/access`);
   const roles={owner:'Владелец',editor:'Редактор',viewer:'Просмотр'};
   const canShare=baseline.roles[data.active]==='owner';
   const roster=access.map(a=>`<p>${esc(a.username)} · ${esc(roles[a.role])}${canShare&&a.role!=='owner'?` <button class="btn tiny danger" type="button" data-action="wsRevoke" data-id="${esc(a.username)}">Отозвать доступ</button>`:''}</p>`).join('');
   openModal('Доступ к объекту','Владелец назначает роли существующим пользователям. Администратор может создавать учётные записи.',
    `<div class="panel-body">${roster}</div>`+(canShare?`<div class="form-grid">${field('Логин сотрудника','username')}<div class="field"><label>Роль</label><select name="role"><option value="viewer">Просмотр</option><option value="editor">Редактор</option></select></div></div>`:'')+(isAdmin?'<p class="help">Для создания сотрудника используйте кнопку «Добавить пользователя» ниже.</p>':''),
    canShare?'Назначить роль':'Закрыть',async fd=>{
     if(canShare){await api(`/projects/${encodeURIComponent(data.active)}/access`,{method:'POST',body:JSON.stringify({username:String(fd.get('username')||''),role:String(fd.get('role')||'viewer')})});toast('Доступ обновлён');}
    });
   if(isAdmin){const button=document.createElement('button');button.className='btn secondary';button.textContent='Добавить пользователя';button.type='button';button.addEventListener('click',()=>{document.getElementById('modal').close();addUserDialog();});document.getElementById('modalInner').querySelector('.modal-footer')?.prepend(button);}
  }catch(e){toast('Доступы: '+e.message);}
 }
 function addUserDialog(){
  openModal('Новый сотрудник','Создание логина для входа в Смеч. Пароль сообщается сотруднику по защищённому каналу.',
   `<div class="form-grid">${field('Логин (латиница)','username','','text','required pattern="[A-Za-z0-9_.-]{3,60}"')}${field('Пароль от 12 символов','password','','password','required minlength="12" maxlength="128"')}</div>`,
   'Создать',async fd=>{await api('/users',{method:'POST',body:JSON.stringify({username:fd.get('username'),password:fd.get('password')})});toast('Сотрудник создан');});
 }
 async function importLocal(){
  if(!await flush())return;
  let local;
  try{local=normalizeData(load());}catch(e){toast('Не удаётся открыть локальную копию');return;}
  const ok=confirm(`Добавить в серверное пространство ${local.projects.length} локальных объектов и ${local.catalog.length} материалов? Существующие данные не будут заменены; при совпадении идентификаторов импорт будет отменён.`);
  if(!ok)return;
  try{const imported=await api('/import-local',{method:'POST',body:JSON.stringify({projects:local.projects,catalog:local.catalog})});
   await loadCloud();toast(`Импортировано объектов: ${imported.imported}`);
  }catch(e){toast('Перенос данных: '+e.message);}
 }
 async function logout(){
  if(!await flush()){toast('Сохраните резервную копию несохранённых изменений перед выходом');return;}
  await api('/logout',{method:'POST',body:'{}'});
  active=false;username='';csrf='';baseline=null;
  try{sessionStorage.removeItem('smech_current_mode');}catch{}
  data=load();view='dashboard';await refresh();toast('Вы вышли из рабочего пространства');
 }
 async function showDiff(){
  try{const revision=baseline.revisions[data.active];if(revision<2){toast('Для сравнения нужны минимум две редакции объекта');return;}
   const body=await api(`/projects/${encodeURIComponent(data.active)}/diff?before=${revision-1}&after=${revision}`);
   const diff=body.diff;const items=diff.changedLines.map(x=>`<li>${esc(x.name||x.id)}: ${esc(x.fields.join(', '))}</li>`).join('');
   const prices=diff.priceChanges.map(x=>`<li>${esc(x.material)}: ${esc(x.before.price??'—')} → ${esc(x.after.price??'—')} ₽; ${esc(x.before.source||'нет источника')} → ${esc(x.after.source||'нет источника')}</li>`).join('');
   openModal('Сравнение редакций',`Редакции № ${body.before} и № ${body.after}. Добавлено: ${diff.added}, удалено: ${diff.removed}, изменено: ${diff.changed}.`,
    `<div class="panel-body"><p><b>Изменения полей объекта:</b> ${esc(diff.projectFields.join(', ')||'нет')}</p><p><b>Изменения расходов:</b> ${esc(diff.changedExtras.join(', ')||'нет')}</p><p><b>Изменённые работы:</b></p><ul>${items||'<li>нет</li>'}</ul><p><b>Изменения цен материалов:</b></p><ul>${prices||'<li>нет</li>'}</ul></div>`,
    'Закрыть',()=>true);
  }catch(e){toast('Сравнение версий: '+e.message);}
 }
 async function showTrash(){
  try{const items=await api('/trash');
   const rows=items.map(x=>`<p><b>${esc(x.name)}</b> · редакция ${esc(x.revision)} <button class="btn tiny" data-action="wsRestore" data-id="${esc(x.id)}" data-revision="${esc(x.revision)}">Восстановить</button></p>`).join('');
   openModal('Корзина объектов','Восстановить может только владелец. Архивные редакции и события остаются в базе.',`<div class="panel-body">${rows||'<p>Удалённых объектов нет.</p>'}</div>`,'Закрыть',()=>true);
  }catch(e){toast('Корзина: '+e.message);}
 }
 async function action(name,id,button){
  try{
   switch(name){
    case 'wsOpen':await open();break;
    case 'wsLocal':await switchLocal();break;
    case 'wsRetry':if(blocked){blocked=false;}await flush();paint();break;
    case 'wsReload':if(!confirm('Отменить все изменения, не сохранённые на сервере? Сначала скачайте резервную копию JSON.'))return;await loadCloud();break;
    case 'wsImport':await importLocal();break;
    case 'wsAccess':await accessDialog();break;
    case 'wsAudit':await audit();break;
    case 'wsDiff':await showDiff();break;
    case 'wsTrash':await showTrash();break;
    case 'wsRestore':if(confirm('Восстановить этот объект?')){await api(`/projects/${encodeURIComponent(id)}/restore`,{method:'POST',body:JSON.stringify({expectedRevision:Number(button.dataset.revision)})});document.getElementById('modal').close();await loadCloud();toast('Объект восстановлен');}break;
    case 'wsRevoke':if(confirm('Отозвать доступ пользователя '+id+' к объекту?')){await api(`/projects/${encodeURIComponent(data.active)}/access/${encodeURIComponent(id)}`,{method:'DELETE',body:'{}'});document.getElementById('modal').close();toast('Доступ отозван');}break;
    case 'wsLogout':await logout();break;
   }
  }catch(e){toast('Рабочее пространство: '+e.message);}
 }
 async function bootstrap(){
  await queryInfo();
  if(!enabled)return;
  try{await getIdentity();}catch{return;}
  try{if(sessionStorage.getItem('smech_current_mode')==='workspace')await loadCloud();}catch(e){toast('Не удалось загрузить серверное пространство: '+e.message);}
 }
 bootstrap();
 return {get active(){return active;},queue,flush,snapshot,downloadSnapshot,reviewApi:api,settingsHtml,noticeHtml,updateHeader,action};
})();
