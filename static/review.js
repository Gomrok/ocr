(() => {
  'use strict';
  const el = (id) => document.getElementById(id);
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  let snapshot = null, activeUnit = null, generation = 0, saving = false;
  const messages = {experimental_profile:'پروفایل هنوز اعتبارسنجی نشده است',numeric_disagreement:'اختلاف در عدد یا شناسه',reading_disagreement:'اختلاف خوانش',no_readable_observation:'متن قابل خواندن پیدا نشد',retry_budget_exhausted:'سقف بازخوانی تکمیل شد',retry_not_run_region_budget:'این ناحیه بازخوانی نشده است',attempt_failed:'یکی از خوانش‌ها ناموفق بود',declared_handwriting_requires_human:'دست‌نویس؛ تأیید انسانی لازم است',overlapping_reading_disagreement:'اختلاف در ناحیهٔ هم‌پوشان',recovered_unverified_text:'متن بازیابی‌شده هنوز تأیید نشده است',greater_spatial_and_text_coverage_candidate:'نامزد دارای پوشش بیشتر؛ نیازمند بررسی'};
  function option(value, text) { const o = document.createElement('option'); o.value = value; o.textContent = text; return o; }
  Object.assign(messages, {
    document_time_budget_exhausted: 'سقف زمان سند رسیده است؛ بازخوانی این ناحیه کامل نشده است',
    evidence_budget_exhausted: 'سقف حجم شواهد رسیده است؛ بازخوانی این ناحیه کامل نشده است',
    low_engine_confidence_signal: 'امتیاز موتور پایین است؛ این امتیاز درصد صحت نیست'
  });
  async function fetchJSON(url, options) {
    const response = await fetch(url, {credentials:'same-origin', ...options});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'دریافت اطلاعات انجام نشد.');
    return data;
  }
  function message(text) { el('reviewMessage').textContent = text; }
  function renderPage() {
    if (!snapshot) return;
    const page = Number(el('reviewPage').value);
    el('reviewImage').src = `/reviews/${snapshot.document_id}/pages/${page}`;
    el('reviewImage').alt = `تصویر مرجع صفحهٔ ${page.toLocaleString('fa-IR')}`;
    const info = snapshot.pages.find(p => p.page === page);
    el('reviewOverlay').setAttribute('viewBox', `0 0 ${info.width || 1} ${info.height || 1}`);
    el('reviewBox').setAttribute('width', '0');
    el('reviewUnits').replaceChildren(); el('reviewForm').hidden = true; activeUnit = null;
    for (const unit of snapshot.units.filter(u => u.page === page)) {
      const button = document.createElement('button'); button.type = 'button'; button.className = 'review-unit';
      button.dataset.span = unit.span_id; button.setAttribute('aria-pressed','false');
      const state = unit.review_status === 'approved' ? 'تأییدشده' : unit.review_status === 'unreadable' ? 'ناخوانا' : 'نیازمند بازبینی';
      button.textContent = `${state} · ${unit.kind === 'cell' ? 'سلول' : 'ناحیه'}\n${unit.verified_text ?? unit.content_text ?? 'بدون متن'}`;
      button.addEventListener('click', () => selectUnit(unit)); el('reviewUnits').append(button);
    }
    if (info.error) message('استخراج این صفحه ناموفق بوده است؛ متن ساخته‌شده‌ای برای آن ارائه نمی‌شود.');
  }
  function selectUnit(unit) {
    activeUnit = unit;
    for (const b of el('reviewUnits').children) b.setAttribute('aria-pressed', String(b.dataset.span === unit.span_id));
    const [x,y,right,bottom] = unit.bbox;
    for (const [name,value] of Object.entries({x,y,width:right-x,height:bottom-y})) el('reviewBox').setAttribute(name,String(value));
    el('observedText').value = unit.engine_text_raw || '';
    el('verifiedText').value = unit.verified_text ?? unit.content_text ?? '';
    el('handwritingFlag').checked = !!unit.handwriting;
    el('unitReasons').textContent = unit.reasons.map(reason => messages[reason] || 'نیازمند بازبینی').join('؛ ');
    el('alternativeText').replaceChildren(option('', 'مشاهدهٔ نامزدهای ثبت‌شده'));
    unit.alternatives.forEach((text,index) => el('alternativeText').append(option(String(index), text || 'بدون متن')));
    el('reviewForm').hidden = false;
  }
  async function loadDocument() {
    const id = el('reviewDocument').value, turn = ++generation;
    snapshot = null; activeUnit = null; el('reviewForm').hidden = true; el('reviewUnits').replaceChildren();
    el('reviewImage').removeAttribute('src');
    for (const name of ['reviewPage','extractionRevision','activateRevision','nextUnreviewed']) el(name).disabled = true;
    el('searchExport').hidden = true;
    if (!id) return;
    try {
      const data = await fetchJSON(`/reviews/${id}`); if (turn !== generation) return;
      snapshot = data;
      el('reviewPage').replaceChildren(...data.pages.map(p => option(String(p.page),p.page.toLocaleString('fa-IR'))));
      el('extractionRevision').replaceChildren(...data.available_revisions.map((r,i) => option(r,`نسخهٔ ${(i+1).toLocaleString('fa-IR')}${r === data.extraction_revision ? ' · فعال' : ''}`)));
      el('extractionRevision').value = data.extraction_revision;
      for (const name of ['reviewPage','extractionRevision','activateRevision','nextUnreviewed']) el(name).disabled = false;
      el('searchExport').href = `/reviews/${id}/search`; el('searchExport').hidden = false;
      const remaining = data.units.filter(u => u.review_status !== 'approved').length;
      el('reviewCount').textContent = `${remaining.toLocaleString('fa-IR')} ناحیه تأییدنشده`;
      message(`نسخهٔ بازبینی ${data.version.toLocaleString('fa-IR')}؛ ${data.search_entries.toLocaleString('fa-IR')} ناحیه در خروجی مخصوص جست‌وجو`);
      renderPage();
    } catch (error) { if (turn === generation) message(error.message); }
  }
  async function save(status, revision) {
    if (saving || !snapshot || (!activeUnit && !revision)) return;
    if (!el('reviewerName').value.trim()) { message('نام بازبین را وارد کنید.'); el('reviewerName').focus(); return; }
    if (status === 'approved' && !el('verifiedText').value.trim()) { message('برای تأیید، متن خوانده‌شده را وارد کنید.'); return; }
    saving = true;
    const id = snapshot.document_id;
    const body = {version:snapshot.version, reviewer:el('reviewerName').value.trim(), changes:revision ? [] : [{span_id:activeUnit.span_id,status,text:status === 'approved' ? el('verifiedText').value : null,handwriting:el('handwritingFlag').checked}]};
    if (revision) body.activate_revision = revision;
    try {
      await fetchJSON(`/reviews/${id}`, {method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(body)});
      await loadDocument();
    } catch (error) { message(error.message); } finally { saving = false; }
  }
  el('reviewForm').addEventListener('submit', e => {e.preventDefault(); save('approved');});
  el('unreadableUnit').addEventListener('click', () => save('unreadable'));
  el('revokeUnit').addEventListener('click', () => save('not_reviewed'));
  el('activateRevision').addEventListener('click', () => save(null, el('extractionRevision').value));
  el('reviewDocument').addEventListener('change', loadDocument);
  el('reloadReview').addEventListener('click', loadDocument);
  el('reviewPage').addEventListener('change', renderPage);
  el('reviewImage').addEventListener('error', () => message('تصویر مرجع این صفحه در دسترس نیست.'));
  el('reviewZoom').addEventListener('change', () => { el('reviewCanvas').style.width = `${el('reviewZoom').value}%`; });
  el('alternativeText').addEventListener('change', () => { if (activeUnit && el('alternativeText').value !== '') el('verifiedText').value = activeUnit.alternatives[Number(el('alternativeText').value)]; });
  el('nextUnreviewed').addEventListener('click', () => {
    if (!snapshot) return;
    const start = activeUnit ? snapshot.units.indexOf(activeUnit)+1 : 0;
    const ordered = [...snapshot.units.slice(start),...snapshot.units.slice(0,start)];
    const unit = ordered.find(u => u.review_status !== 'approved');
    if (unit) {el('reviewPage').value = String(unit.page); renderPage(); selectUnit(unit); if (window.matchMedia('(max-width:760px)').matches) showPane(1); el('verifiedText').focus();}
  });
  const tabs = ['reviewImageTab','reviewTextTab'];
  function showPane(index) { tabs.forEach((name,i) => {el(name).setAttribute('aria-selected',String(i===index));el(name).tabIndex=i===index?0:-1;el(i?'reviewTextPane':'reviewImagePane').classList.toggle('is-active',i===index);}); }
  tabs.forEach((name,index) => {el(name).addEventListener('click',()=>showPane(index));el(name).addEventListener('keydown', e => {if (['ArrowLeft','ArrowRight'].includes(e.key)){e.preventDefault();showPane(1-index);el(tabs[1-index]).focus();}});});
  fetchJSON('/reviews/data').then(data => {for (const item of data.documents) el('reviewDocument').append(option(item.document_id,item.name)); if (!data.documents.length) message('هنوز خروجی مسیر آزمایشی در این نشست وجود ندارد.');}).catch(error=>message(error.message));
})();
