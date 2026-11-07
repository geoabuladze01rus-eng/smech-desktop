'use strict';
/* Смеч 0.5. Дополнительные экраны: сверка материалов, просмотр источника и двухэтапное согласование. */
window.smechAdvanced = (() => {
  let specState = null, previewUrl = null, reviewCache = null;
  const STAT = {matched:'Совпадает',shortage:'Недостаток по спецификации',surplus:'Превышение по спецификации',unmatched_spec:'Нет в потребности',unmatched_demand:'Нет в спецификации'};
  const REVIEW = {awaiting_engineer:'Ожидает инженера',awaiting_manager:'Ожидает руководителя',approved:'Утверждено',rejected:'Отклонено',superseded:'Устаревшая редакция'};
  const csrfWorkspace = () => window.smechWorkspace?.active;

  function specView(){
    const specs=project().specifications||[], check=calc?.specificationCheck||{rows:[],stats:{}};
    const total=check.rows.length;
    return head('Спецификации материалов', 'Загрузите спецификацию отдельно от ВОР и сравните её с расчётной потребностью. Система не угадывает аналоги и не переводит единицы.',
      `<button class="btn secondary" data-action="specCsv">↓ CSV сверки</button><button class="btn primary" data-action="specImport">↑ Импорт спецификации</button>`)+alertBox()+
      `<div class="alert info">Сопоставление строгое: наименование + единица измерения. Наличие строки в спецификации не подтверждает поставку или складской остаток. При отличающихся обозначениях требуется ручное уточнение.</div>`+
      panel(`Расхождения · ${total} групп`, `${specs.length} строк импортировано, совпадений: ${check.stats?.matched||0}`,
      total?`<div class="table-scroll"><table><thead><tr><th>Материал</th><th>Ед.</th><th class="num">Потребность</th><th class="num">Спецификация</th><th class="num">Разница</th><th>Статус</th></tr></thead><tbody>${check.rows.map(r=>`<tr><td>${esc(r.name)}</td><td>${esc(r.unit)}</td><td class="num">${esc(r.demand)}</td><td class="num">${esc(r.specified)}</td><td class="num">${esc(r.difference)}</td><td><span class="tag ${r.status==='matched'?'good':'warn'}">${esc(STAT[r.status]||r.status)}</span></td></tr>`).join('')}</tbody></table></div>`:empty('Загрузите спецификацию и задайте материалы на работах ВОР.'))+
      panel('Исходная спецификация', 'Данные сохраняются отдельно от справочника цен и норм расхода; цены из спецификации не подставляются',
      `<div class="panel-body">${specs.length?`<div class="table-scroll"><table><thead><tr><th>Материал</th><th>Код</th><th>Ед.</th><th class="num">Количество</th><th>Источник</th><th></th></tr></thead><tbody>${specs.map(s=>`<tr><td>${esc(s.name)}</td><td>${esc(s.code)}</td><td>${esc(s.unit)}</td><td class="num">${esc(s.qty)}</td><td class="small">${esc(s.source)}</td><td><button class="btn tiny danger" data-action="specDelete" data-id="${esc(s.id)}">×</button></td></tr>`).join('')}</tbody></table></div>`:'<p>Спецификации пока не загружены.</p>'}</div>`,
      `<button class="btn tiny secondary" data-action="specClear" ${specs.length?'':'disabled'}>Очистить спецификацию</button>`);
  }
  const specColumns=[['name','Наименование материала'],['unit','Единица измерения'],['qty','Количество'],['code','Код / марка'],['source','Источник / примечание']];
  function guessSpecColumn(header,k){const rules={name:/наим|материал|ресурс|оборудование|изделие/i,unit:/ед(\.|иница)?\s*изм|единица измерения/i,qty:/кол-?во|количество|об[ъь][её]м|потребность/i,code:/шифр|марка|артикул|код/i,source:/источник|примечание|лист|документ/i};return header.findIndex(x=>rules[k].test(String(x||'')));}
  function specImport(){specState=null;
    openModal('Импорт спецификации материалов','Поддерживаются XLSX, CSV, TSV и PDF. Перед добавлением пользователь выбирает строки и сверяет единицы/количества.',
      `<div class="field"><label>Спецификация до 15 МБ</label><input type="file" id="specFile" accept=".xlsx,.csv,.tsv,.pdf" required></div><label class="help"><input type="checkbox" id="specOCR" style="width:auto"> Локальное OCR PDF-скана (только с установленным Tesseract)</label><div id="specPreview"><p class="help">Выберите файл. Он читается на вашем сервере и не передаётся внешним сервисам.</p></div>`,
      'Добавить выбранные строки',async()=>{if(!specState){toast('Сначала загрузите файл');return false;}
        const sh=specState.sheets[specState.sheet],mapping=specState.mapping,lines=[];
        for(const index of [...specState.selected].sort((a,b)=>a-b)){
          const v=specRow(sh,index,mapping);
          if(!validSpecRow(v)){toast(`Строка ${index+1}: исправьте наименование, единицу и количество`);return false;}
          const origin=sh.origins?.[index];
          const where=specState.pdf?`PDF ${specState.filename}, стр. ${origin?.page??'?'}, таблица ${origin?.table??'?'}, строка ${origin?.row??index+1}; SHA-256 ${specState.sha256}`: `${specState.filename}, лист «${sh.name}», строка ${index+1}`;
          lines.push({id:uid(),name:v.name.trim(),unit:v.unit.trim(),qty:inputNum(v.qty),code:v.code,source:(where+(v.source?'; '+v.source:'')).slice(0,500),note:''});
        }
        if(!lines.length){toast('Отметьте строки для добавления');return false;}
        const p=project();if((p.specifications||[]).length+lines.length>10000){toast('Лимит: 10 000 позиций спецификации');return false;}
        if(!confirm(`Добавить ${lines.length} проверенных строк в спецификацию объекта «${p.name}»? Повторы сохранятся, цены и нормы не будут изменены.`))return false;
        p.specifications=p.specifications||[];p.specifications.push(...lines);p.version++;
        view='spec';toast('Добавлено строк спецификации: '+lines.length);
      });
  }
  async function specFile(file){if(!file)return;
    try{
      const fd=new FormData();fd.append('file',file);
      const ocr=document.getElementById('specOCR')?.checked && file.name.toLowerCase().endsWith('.pdf');
      const res=await fetch('/api/import'+(ocr?'?ocr=true':''),{method:'POST',body:fd});const parsed=await res.json();
      if(!res.ok)throw Error(parsed.detail||'Не удалось прочитать файл');
      specState={sheets:parsed.sheets||[],filename:parsed.filename||file.name,pdf:parsed.sourceType==='pdf',sha256:parsed.sha256||'',sheet:0,header:0,mapping:{},page:0,selected:new Set(),edits:{},warnings:parsed.warnings||[]};
      initSpecMapping();renderSpecPreview();
    }catch(e){toast('Спецификация: '+e.message);}
  }
  function initSpecMapping(){if(!specState?.sheets.length)return;
    const sh=specState.sheets[specState.sheet],head=sh.rows?.[specState.header]||[];
    specState.mapping=Object.fromEntries(specColumns.map(([k])=>[k,guessSpecColumn(head,k)]));specState.selected.clear();specState.edits={};specState.page=0;
  }
  function specRow(sheet,index,map){const row=sheet.rows?.[index]||[];const get=k=>map[k]===undefined||map[k]<0?'':String(row[map[k]]??'').trim();const changes=specState?.edits?.[`${specState.sheet}:${index}`]||{};return {name:changes.name??get('name'),qty:changes.qty??get('qty'),unit:changes.unit??get('unit'),code:get('code'),source:get('source')};}
  function validSpecRow(r){return r.name.length>0&&r.name.length<=500&&r.unit.length>0&&r.unit.length<=40&&isNumber(r.qty)&&Number(inputNum(r.qty))>0&&!/^(?:итого|всего|сумма\s*:)/i.test(r.name);}
  function renderSpecPreview(){const host=document.getElementById('specPreview');if(!host||!specState)return;
    if(!specState.sheets.length){host.innerHTML=`<div class="alert">Не найдено таблиц. ${esc(specState.warnings.join(' '))}</div>`;return;}
    const sh=specState.sheets[specState.sheet],rows=sh.rows||[],hi=specState.header,header=rows[hi]||[];
    const maxcols=Math.min(80,Math.max(5,...rows.slice(0,20).map(r=>r.length)));
    const choices=Array.from({length:maxcols},(_,i)=>`<option value="${i}">${i+1}: ${esc(header[i]||'Без названия')}</option>`).join('');
    const total=Math.max(0,rows.length-hi-1),pages=Math.max(1,Math.ceil(total/15));specState.page=Math.min(pages-1,Math.max(0,specState.page));const start=hi+1+specState.page*15;
    host.innerHTML=`<div class="alert info">${esc(specState.filename)} · ${rows.length} строк. Неопределённые позиции и несовместимые единицы не объединяются. ${esc((specState.warnings||[]).join(' '))}</div>`+
      `<div class="form-grid"><div class="field"><label>Лист / таблица</label><select id="specSheet">${specState.sheets.map((x,i)=>`<option value="${i}" ${i===specState.sheet?'selected':''}>${esc(x.name)}</option>`).join('')}</select></div><div class="field"><label>Строка заголовков</label><select id="specHeader">${rows.slice(0,20).map((_,i)=>`<option value="${i}" ${i===hi?'selected':''}>Строка ${i+1}</option>`).join('')}</select></div></div>`+
      `<div class="mapping">${specColumns.map(([k,label])=>`<div class="field"><label>${label}</label><select id="specMap_${k}"><option value="-1">Не импортировать</option>${choices}</select></div>`).join('')}</div>`+
      `<div class="actions"><button type="button" class="btn tiny secondary" data-action="specSelectAll">Отметить корректные строки</button><button type="button" class="btn tiny secondary" data-action="specDeselect">Снять все отметки</button><span class="help">Выбрано: ${specState.selected.size}</span></div>`+
      `<div class="preview-table"><table><thead><tr><th>Добавить</th><th>Строка</th><th>Наименование</th><th>Ед.</th><th>Количество</th><th>Проверка</th></tr></thead><tbody>${rows.slice(start,start+15).map((r,offset)=>{const i=start+offset,v=specRow(sh,i,specState.mapping),valid=validSpecRow(v);return `<tr><td><input type="checkbox" data-spec-select="${i}" ${specState.selected.has(i)?'checked':''}></td><td>${i+1}</td><td><input class="pdf-cell" aria-label="Материал" data-spec-edit="name" data-spec-row="${i}" value="${esc(v.name)}"></td><td><input class="pdf-cell" aria-label="Единица" data-spec-edit="unit" data-spec-row="${i}" value="${esc(v.unit)}"></td><td><input class="pdf-cell" aria-label="Количество" data-spec-edit="qty" data-spec-row="${i}" value="${esc(v.qty)}"></td><td>${valid?'<span class="tag good">Проверить</span>':'<span class="tag warn">Не заполнено</span>'}</td></tr>`;}).join('')}</tbody></table></div>`+
      `<div class="actions"><button type="button" class="btn tiny secondary" data-action="specPrev" ${specState.page===0?'disabled':''}>←</button><span class="help">${specState.page+1} / ${pages}</span><button type="button" class="btn tiny secondary" data-action="specNext" ${specState.page===pages-1?'disabled':''}>→</button></div>`;
    for(const [k] of specColumns)document.getElementById('specMap_'+k).value=String(specState.mapping[k]??-1);
  }

  function reviewView(){
    if(!csrfWorkspace())return head('Согласование сметы','Два независимых решения по зафиксированной архивной версии.')+`<div class="alert">Согласование доступно только в серверном рабочем пространстве. Локальные снимки не имеют проверяемой подписи сотрудника и не считаются утверждёнными.</div><button class="btn primary" data-action="wsOpen">Войти в рабочее пространство</button>`;
    return head('Согласование расчётов','Инженер проверяет объёмы и исходные документы; руководитель утверждает ту же неизменяемую архивную версию.',
      `<button class="btn secondary" data-action="reviewRefresh">Обновить</button><button class="btn primary" data-action="reviewSnapshot">Зафиксировать расчёт</button>`)+
      `<div class="alert info">Решения привязаны к SHA-256 и редакции снимка. После изменения ведомости старое согласование становится устаревшим. Инженер и руководитель должны быть разными пользователями.</div><div id="reviewsLive"><p class="help">Нажмите «Обновить», чтобы загрузить решения.</p></div>`;
  }
  async function refreshReviews(){if(view!=='approvals'||!csrfWorkspace())return;
    const wrap=document.getElementById('reviewsLive');if(!wrap)return;
    try{const response=await window.smechWorkspace.reviewApi(`/projects/${encodeURIComponent(data.active)}/reviews`);
      reviewCache=response;
      const role=response.role, items=response.items||[];
      wrap.innerHTML=panel('Архивные версии на согласовании','Каждый этап подписывается через авторизованную учётную запись',items.length?
        `<div class="table-scroll"><table><thead><tr><th>Версия</th><th>Проверка</th><th>Решения</th><th>Действие</th></tr></thead><tbody>${items.map(x=>{const canReview=(role==='editor'&&x.state==='awaiting_engineer')||(role==='owner'&&x.state==='awaiting_manager');const allowed= x.state!=='superseded';return `<tr><td><b>№ ${x.snapshotRevision}</b><div class="help">SHA ${esc(x.snapshotHash.slice(0,12))}…</div>${x.blockers.length?`<span class="tag warn">${x.blockers.length} замечаний</span>`:''}</td><td><span class="tag ${x.state==='approved'?'good':'warn'}">${esc(REVIEW[x.state]||x.state)}</span></td><td class="small">${x.decisions.map(d=>`${d.stage==='engineering'?'Инженер':'Руководитель'}: ${d.decision==='approved'?'согласовано':'отклонено'} · ${esc(d.actor)}${d.note?` · ${esc(d.note)}`:''}`).join('<hr>')||'Решений нет'}</td><td>${canReview&&allowed?`<div class="table-actions"><button class="btn tiny primary" data-action="reviewApprove" data-id="${esc(x.snapshotId)}" ${x.blockers.length?'disabled':''}>Согласовать</button><button class="btn tiny secondary" data-action="reviewReject" data-id="${esc(x.snapshotId)}">Отклонить</button></div>`:'—'}</td></tr>`;}).join('')}</tbody></table></div>`:
        empty('Зафиксируйте коммерческий расчёт, чтобы направить его на инженерную проверку.'));
    }catch(e){wrap.innerHTML=`<div class="alert">Не удалось загрузить согласование: ${esc(e.message)}</div>`;}
  }
  async function decide(id,approval){
    if(!await window.smechWorkspace.flush()){toast('Сначала дождитесь сохранения всех изменений');return;}
    await refreshReviews();const row=reviewCache?.items?.find(x=>x.snapshotId===id);if(!row){toast('Архивная версия не найдена');return;}
    const stage=reviewCache.role==='editor'?'engineering':'management';
    const note=prompt(approval?'Комментарий к согласованию (необязательно)':'Причина отклонения (обязательно, от 5 символов):','');
    if(note===null)return;if(!approval&&note.trim().length<5){toast('Укажите мотивировку отклонения');return;}
    if(!confirm(`${approval?'Согласовать':'Отклонить'} неизменяемую версию № ${row.snapshotRevision} (SHA ${row.snapshotHash.slice(0,12)}…)? Решение нельзя изменить.`))return;
    try{await window.smechWorkspace.reviewApi(`/projects/${encodeURIComponent(data.active)}/snapshots/${encodeURIComponent(id)}/review`,{method:'POST',body:JSON.stringify({stage,decision:approval?'approved':'rejected',note,expectedProjectRevision:row.currentRevision,expectedSnapshotHash:row.snapshotHash})});
      await refreshReviews();toast('Решение зафиксировано в журнале');
    }catch(e){toast('Согласование: '+e.message);}
  }
  function clearPreview(){if(previewUrl){URL.revokeObjectURL(previewUrl);previewUrl=null;}}
  async function viewPdfSource(file,origin,container){if(!file)return;
    const host=document.getElementById(container);if(!host)return;
    host.innerHTML='<p class="help">Подготавливается изображение исходной страницы…</p>';
    try{const fd=new FormData();fd.append('file',file);
      const coord=Array.isArray(origin.rowBbox)?origin.rowBbox.join(','):'';
      const url=`/api/pdf/source-page?page=${encodeURIComponent(origin.page)}${coord?'&bbox='+encodeURIComponent(coord):''}`;
      const resp=await fetch(url,{method:'POST',body:fd});if(!resp.ok){let r=await resp.json();throw Error(r.detail||'Не удалось показать PDF');}
      if(resp.headers.get('X-Smech-SHA256')!==origin.sha256)throw Error('Этот файл не совпадает с исходным PDF по SHA-256. Сверка запрещена.');
      clearPreview();previewUrl=URL.createObjectURL(await resp.blob());
      host.innerHTML=`<p class="help">Исходная страница ${esc(origin.page)}, строка ${esc(origin.row||'—')}. Подсветка обозначает границы, найденные при распознавании; проверить текст и единицы нужно вручную.</p><img class="pdf-source-image" alt="Страница оригинального PDF с выделенной строкой" src="${previewUrl}">`;
    }catch(e){host.innerHTML=`<div class="alert">${esc(e.message)}</div>`;}
  }
  async function showSourceFromImport(i){if(!importData?.pdf||!importData.file)return;
    const origin=importData.sheets[importData.sheetIndex].origins?.[i];if(!origin)return;
    const host=document.getElementById('sourcePreview');
    await viewPdfSource(importData.file,{...origin,sha256:importData.sha256},'sourcePreview');
    host?.scrollIntoView({behavior:'smooth',block:'nearest'});
  }
  function showSourceFromSaved(id){const line=lineById(id);if(!line?.sourceRef)return;
    const origin=line.sourceRef;
    openModal('Проверка исходной страницы','Выберите тот же PDF, из которого была импортирована строка. Документ сравнивается по SHA-256 и не хранится на сервере.',
      `<p class="help">${esc(origin.file)} · стр. ${esc(origin.page)}, строка ${esc(origin.row)} · SHA ${esc(origin.sha256.slice(0,14))}…</p><input type="file" id="sourcePdfFile" accept=".pdf"><div id="sourceStandalone"></div>`,
      'Закрыть',()=>true);
    document.getElementById('sourcePdfFile').dataset.sourceLine=id;
  }
  async function action(act,id,b){
    switch(act){
      case 'specImport':specImport();return true;
      case 'specCsv':csvSave('comparison');return true;
      case 'specDelete':if(confirm('Удалить позицию спецификации?')){project().specifications=(project().specifications||[]).filter(s=>s.id!==id);project().version++;await refresh();}return true;
      case 'specClear':if(confirm('Удалить все строки спецификации текущего объекта?')){project().specifications=[];project().version++;await refresh();}return true;
      case 'specSelectAll':if(specState){const sh=specState.sheets[specState.sheet];if(specState.pdf){toast('PDF: проверяйте строки и отмечайте их по одной');return true;}sh.rows.forEach((_,i)=>{if(i>specState.header&&validSpecRow(specRow(sh,i,specState.mapping)))specState.selected.add(i);});renderSpecPreview();}return true;
      case 'specDeselect':specState?.selected.clear();renderSpecPreview();return true;
      case 'specPrev':if(specState){specState.page--;renderSpecPreview();}return true;
      case 'specNext':if(specState){specState.page++;renderSpecPreview();}return true;
      case 'pdfSource':await showSourceFromImport(Number(id));return true;
      case 'viewSource':showSourceFromSaved(id);return true;
      case 'reviewRefresh':await refreshReviews();return true;
      case 'reviewSnapshot':if(!csrfWorkspace()){toast('Нужно войти в рабочее пространство');return true;}
        await window.smechWorkspace.snapshot();view='approvals';paint();await refreshReviews();return true;
      case 'reviewApprove':await decide(id,true);return true;
      case 'reviewReject':await decide(id,false);return true;
    }
    return false;
  }
  document.addEventListener('change',async e=>{
    const el=e.target;
    if(el.id==='specFile'){await specFile(el.files?.[0]);return;}
    if(el.id==='specSheet'&&specState){specState.sheet=Number(el.value);specState.header=0;initSpecMapping();renderSpecPreview();return;}
    if(el.id==='specHeader'&&specState){specState.header=Number(el.value);initSpecMapping();renderSpecPreview();return;}
    if(el.id?.startsWith('specMap_')&&specState){specState.mapping[el.id.replace('specMap_','')]=Number(el.value);specState.selected.clear();specState.edits={};renderSpecPreview();return;}
    if(el.dataset.specEdit&&specState){const key=`${specState.sheet}:${Number(el.dataset.specRow)}`;specState.edits[key] ||= {};specState.edits[key][el.dataset.specEdit]=el.value;return;}
    if(el.dataset.specSelect!==undefined&&specState){const i=Number(el.dataset.specSelect);if(el.checked)specState.selected.add(i);else specState.selected.delete(i);return;}
    if(el.id==='sourcePdfFile'){const src=lineById(el.dataset.sourceLine)?.sourceRef;if(src)await viewPdfSource(el.files?.[0],src,'sourceStandalone');}
  });
  document.addEventListener('click',e=>{
    const el=e.target.closest('[data-view]');if(el?.dataset.view==='approvals')Promise.resolve().then(refreshReviews);
    if(e.target.closest('[data-action="closeModal"]'))clearPreview();
  });
  return {specView,reviewView,refreshReviews,action};
})();
