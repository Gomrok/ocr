/* Offline document review. OCR text is always rendered as text, never HTML. */
(() => {
  'use strict';
  const el = id => document.getElementById(id);
  const number = value => new Intl.NumberFormat('fa-IR').format(value);
  const workspace = el('reviewWorkspace');
  let job = '', documentId = '', documents = [], data = null, selected = '';
  let edits = new Map(), requestController = null, generation = 0, saving = false;
  const dirty = () => edits.size > 0;
  const page = () => data?.pages.find(p => p.page === Number(el('reviewPage').value));
  const endpoint = () => '/review/' + encodeURIComponent(job) + '/' + encodeURIComponent(documentId);
  const current = unit => ({...unit, ...edits.get(unit.id)});
  function message(text = '', error = false) {
    el('reviewMessage').textContent = text;
    el('reviewMessage').hidden = !text;
    el('reviewMessage').dataset.error = String(error);
  }
  function buttons() {
    el('reviewSave').disabled = !dirty() || saving || !data;
    el('reviewDocument').disabled = saving;
    el('reviewReload').disabled = saving;
    el('reviewUnits').querySelectorAll('textarea,input').forEach(field => { field.disabled = saving; });
    el('reviewPage').disabled = saving;
    el('reviewOnlyUncertain').disabled = saving;
    el('reviewNext').disabled = saving;
    el('reviewSaveState').textContent = dirty()
      ? number(edits.size) + ' مورد تغییر کرده است؛ اصلاحات هنوز ذخیره نشده‌اند.'
      : 'اصل سند و متن خام OCR بدون تغییر نگهداری می‌شوند.';
  }
  function pane(which) {
    workspace.dataset.mobilePane = which;
    el('reviewShowText').setAttribute('aria-pressed', String(which === 'text'));
    el('reviewShowSource').setAttribute('aria-pressed', String(which === 'source'));
  }
  function locate(unit, focusImage = false) {
    selected = unit.id;
    el('reviewUnits').querySelectorAll('.review-unit').forEach(node =>
      node.classList.toggle('selected', node.dataset.id === selected));
    const bounds = unit.source_bbox || unit.bbox;
    const p = page(), box = el('reviewSelection');
    if (!p || !bounds || bounds.length !== 4) { box.hidden = true; return; }
    const [x0,y0,x1,y1] = bounds;
    box.style.left = Math.max(0, 100 * x0 / p.width) + '%';
    box.style.top = Math.max(0, 100 * y0 / p.height) + '%';
    box.style.width = Math.min(100, 100 * Math.max(1, x1-x0) / p.width) + '%';
    box.style.height = Math.min(100, 100 * Math.max(1, y1-y0) / p.height) + '%';
    box.hidden = false;
    if (focusImage) {
      pane('source');
      box.scrollIntoView({block:'center', inline:'center', behavior:'auto'});
      el('reviewSourcePane').focus({preventScroll:true});
    }
  }
  function record(unit, text, confirmed) {
    const saved = data.pages.flatMap(p => p.units).find(item => item.id === unit.id);
    if (text === saved.text && confirmed === Boolean(saved.confirmed)) edits.delete(unit.id);
    else edits.set(unit.id, {id:unit.id, text, confirmed});
    const unresolved = data.pages.flatMap(p => p.units).map(current)
      .filter(u => u.needs_review && !u.confirmed).length;
    el('reviewUnresolved').textContent = '(' + number(unresolved) + ')';
    const card = Array.from(el('reviewUnits').children).find(node => node.dataset.id === unit.id);
    if (card) {
      card.dataset.uncertain = String(Boolean(unit.needs_review && !confirmed));
      card.querySelector('.review-unit-status').textContent = confirmed ? 'با تصویر تطبیق داده شد'
        : unit.needs_review ? 'نیازمند بررسی' : 'خروجی خودکار';
    }
    buttons();
  }
  function renderUnits() {
    const p = page(), host = el('reviewUnits');
    host.replaceChildren();
    if (!p) return;
    const units = p.units.map(current);
    const unresolved = data.pages.flatMap(p => p.units).map(current)
      .filter(u => u.needs_review && !u.confirmed).length;
    el('reviewUnresolved').textContent = '(' + number(unresolved) + ')';
    const shown = units.filter(u => !el('reviewOnlyUncertain').checked || (u.needs_review && !u.confirmed));
    if (!shown.length) {
      const empty = document.createElement('p');
      empty.className = 'review-empty';
      empty.textContent = p.error ? 'استخراج این صفحه انجام نشده است.'
        : el('reviewOnlyUncertain').checked ? 'مورد علامت‌گذاری‌شده‌ای در این صفحه وجود ندارد.'
        : 'متنی برای بازبینی در این صفحه موجود نیست.';
      host.append(empty);
    }
    shown.forEach((unit,index) => {
      const card = document.createElement('article');
      card.className = 'review-unit';
      card.dataset.id = unit.id;
      card.dataset.uncertain = String(Boolean(unit.needs_review && !unit.confirmed));
      const heading = document.createElement('div');
      heading.className = 'review-unit-heading';
      const label = document.createElement('span');
      label.textContent = unit.kind === 'cell'
        ? 'جدول ' + number(unit.table_id) + ' · سطر ' + number(unit.row + 1) + ' · ستون ' + number(unit.column + 1)
        : (unit.reason === 'unrecognized_ink' ? 'ناحیهٔ فاقد خوانش' : 'سطر متن') + ' ' + number(index+1);
      const status = document.createElement('span');
      status.className = 'review-unit-status';
      status.textContent = unit.confirmed ? 'با تصویر تطبیق داده شد'
        : unit.needs_review ? 'نیازمند بررسی' : 'خروجی خودکار';
      label.append(' · ', status);
      const find = document.createElement('button');
      find.type = 'button'; find.className = 'review-locate'; find.textContent = 'نمایش در سند';
      find.addEventListener('click', () => locate(unit, true));
      heading.append(label, find);
      const text = document.createElement('textarea');
      text.value = unit.text; text.maxLength = 8192;
      text.placeholder = unit.reason === 'unrecognized_ink' ? 'در صورت خوانا بودن، متن این ناحیه را وارد کنید.' : '';
      text.setAttribute('aria-label', label.textContent); text.spellcheck = false;
      text.addEventListener('focus', () => locate(unit));
      const confirm = document.createElement('label');
      confirm.className = 'review-confirm';
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox'; checkbox.checked = Boolean(unit.confirmed);
      const confirmText = document.createElement('span');
      confirmText.textContent = 'با تصویر سند تطبیق داده شد';
      confirm.append(checkbox, confirmText);
      text.addEventListener('input', () => { checkbox.checked = false; record(unit,text.value,false); });
      checkbox.addEventListener('change', () => record(unit,text.value,checkbox.checked));
      card.append(heading,text,confirm);
      const alternatives = document.createElement('details');
      alternatives.className = 'review-alternatives';
      const summary = document.createElement('summary'); summary.textContent = 'متن اولیه و خوانش‌های پیشنهادی';
      alternatives.append(summary);
      const choices = [{text:unit.original_text, engine:'متن اولیه'}, ...(unit.candidates || [])];
      choices.slice(0,8).forEach(choice => {
        const reading = document.createElement('p');
        reading.textContent = String(choice.engine || 'خوانش جایگزین') + ': ' + String(choice.text || '');
        alternatives.append(reading);
      });
      card.append(alternatives); host.append(card);
    });
    const active = units.find(u => u.id === selected);
    if (active) locate(active);
  }
  function renderPage() {
    const p = page();
    if (!p) return;
    const warnings = p.warnings || [];
    el('reviewPageNotice').hidden = !warnings.length;
    el('reviewPageNotice').textContent = warnings.some(w => w.startsWith('second_engine_'))
      ? 'موتور دوم نتوانسته است این صفحه را بررسی کند. خروجی خودکار قابل دریافت است و نیاز به تطبیق با سند دارد.'
      : 'بررسی خودکار این صفحه کامل نیست. نواحی فاقد خوانش یا بررسی‌نشده در نتیجه مشخص شده‌اند.';
    el('reviewSelection').hidden = true;
    el('reviewImage').src = '/preview/' + encodeURIComponent(job) + '/' +
      encodeURIComponent(documentId) + '/' + p.page;
    el('reviewImage').alt = 'تصویر اصلی صفحهٔ ' + number(p.page);
    renderUnits();
  }
  function renderDownloads() {
    const host = el('reviewDownloads'); host.replaceChildren();
    (data?.files || []).filter(path => /\.(zip|txt|json|xlsx|csv)$/i.test(path)).forEach(path => {
      const link = document.createElement('a');
      link.href = '/download/' + encodeURIComponent(job) + '/' + encodeURIComponent(path);
      link.textContent = 'نسخهٔ ' + number(data.revision) + ' · ' + path.split('.').pop().toUpperCase();
      link.title = path.split('/').pop(); host.append(link);
    });
  }
  async function load() {
    requestController?.abort(); requestController = new AbortController();
    const token = ++generation;
    data = null; edits.clear(); buttons();
    el('reviewGrid').setAttribute('aria-busy','true');
    el('reviewUnits').replaceChildren(); el('reviewSelection').hidden = true;
    message('در حال دریافت متن و پیش‌نمایش سند…');
    try {
      const response = await fetch(endpoint(), {signal:requestController.signal});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'دریافت بازبینی انجام نشد.');
      if (token !== generation) return;
      data = result;
      el('reviewPage').replaceChildren();
      data.pages.forEach(p => {
        const option = document.createElement('option');
        option.value = String(p.page); option.textContent = 'صفحهٔ ' + number(p.page);
        el('reviewPage').append(option);
      });
      el('reviewRevision').textContent = data.revision ? 'نسخهٔ ' + number(data.revision) : 'خروجی خودکار';
      message(data.exports?.xlsx?.status === 'unavailable' ? 'خروجی Excel در این نسخه در دسترس نیست؛ سایر خروجی‌ها قابل دریافت هستند.' : ''); renderPage(); renderDownloads(); buttons();
    } catch (error) {
      if (error.name !== 'AbortError') message(error.message, true);
    } finally {
      if (token === generation) el('reviewGrid').setAttribute('aria-busy','false');
    }
  }
  document.addEventListener('ocr-job', event => {
    const incoming = event.detail;
    if (incoming.id !== job) {
      requestController?.abort(); ++generation;
      job = incoming.id; documentId = ''; data = null; edits.clear(); selected = '';
      workspace.hidden = true;
    }
    const next = incoming.documents;
    if (!Array.isArray(next) || !next.length) return;
    const changed = JSON.stringify(documents.map(d=>d.id)) !== JSON.stringify(next.map(d=>d.id));
    documents = next; workspace.hidden = false;
    if (changed || !documentId) {
      el('reviewDocument').replaceChildren();
      documents.forEach(doc => {
        const option = document.createElement('option');
        option.value = doc.id; option.textContent = doc.name;
        el('reviewDocument').append(option);
      });
      if (!documents.some(doc=>doc.id === documentId)) {
        documentId = documents[0].id; load();
      }
      el('reviewDocument').value = documentId;
    }
  });
  el('reviewDocument').addEventListener('change', () => {
    if (dirty() && !window.confirm('اصلاحات ذخیره نشده‌اند. از آن‌ها صرف‌نظر شود؟')) {
      el('reviewDocument').value = documentId; return;
    }
    documentId = el('reviewDocument').value; load();
  });
  el('reviewReload').addEventListener('click', () => {
    if (!dirty() || window.confirm('با دریافت آخرین نسخه، اصلاحات ذخیره‌نشده کنار گذاشته شوند؟')) load();
  });
  el('reviewPage').addEventListener('change', renderPage);
  el('reviewOnlyUncertain').addEventListener('change', renderUnits);
  el('reviewZoom').addEventListener('change', () => {
    el('reviewImageWrap').style.width = el('reviewZoom').value + '%';
  });
  el('reviewShowText').addEventListener('click', () => pane('text'));
  el('reviewShowSource').addEventListener('click', () => pane('source'));
  el('reviewImage').addEventListener('error', () => message('پیش‌نمایش دریافت نشد. دریافت آخرین نسخه را انتخاب کنید.',true));
  el('reviewNext').addEventListener('click', () => {
    if (!data) return;
    const pending = data.pages.flatMap(p=>p.units.map(u=>({...current(u),page:p.page})))
      .filter(u=>u.needs_review && !u.confirmed);
    if (!pending.length) { message('مورد علامت‌گذاری‌شدهٔ دیگری وجود ندارد؛ این به معنی تضمین صحت تمام متن نیست.'); return; }
    const unit = pending[(pending.findIndex(u=>u.id === selected)+1) % pending.length];
    el('reviewPage').value = String(unit.page); selected = unit.id;
    if (el('reviewImage').getAttribute('src')?.endsWith('/' + unit.page)) renderUnits();
    else renderPage();
    pane('text');
    const node = Array.from(el('reviewUnits').children).find(n=>n.dataset.id === selected);
    node?.querySelector('textarea')?.focus();
  });
  el('reviewSave').addEventListener('click', async () => {
    if (!data || !dirty() || saving) return;
    saving = true; buttons(); message('در حال ساخت نسخهٔ اصلاح‌شده…');
    try {
      const response = await fetch(endpoint(), {method:'POST',
        headers:{'Content-Type':'application/json','X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content},
        body:JSON.stringify({revision:data.revision,edits:Array.from(edits.values())})});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'ذخیره انجام نشد؛ اصلاحات شما در صفحه باقی مانده‌اند.');
      data = result; edits.clear();
      el('reviewRevision').textContent = 'نسخهٔ ' + number(data.revision);
      renderUnits(); renderDownloads(); message('نسخهٔ اصلاح‌شده ذخیره شد. خروجی اولیه تغییر نکرده است.' + (data.exports?.xlsx?.status === 'unavailable' ? ' خروجی Excel در این نسخه در دسترس نیست.' : ''));
    } catch (error) { message(error.message,true); }
    finally { saving = false; buttons(); }
  });
  el('f').addEventListener('submit', event => {
    if (saving || (dirty() && !window.confirm('اصلاحات ذخیره نشده‌اند. پردازش سند جدید شروع شود؟'))) {
      event.preventDefault(); event.stopImmediatePropagation();
    }
  }, true);
  window.addEventListener('beforeunload', event => { if (dirty()) { event.preventDefault(); event.returnValue = ''; } });
  window.addEventListener('pagehide', () => requestController?.abort());
})();
