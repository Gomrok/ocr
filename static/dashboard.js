(() => {
  'use strict';

  const byId = (id) => document.getElementById(id);
  const workspace = byId('workspace');
  const candidateMode = workspace.dataset.pipelineMode === 'candidate';
  const dpiLevels = workspace.dataset.dpiLevels.split(',').map(Number);
  const number = (value) => new Intl.NumberFormat('fa-IR').format(value);
  const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
  const files = [];
  let activeJob = null;
  let latestStatus = null;
  let pollingTimer = null;
  let selectedDocument = 1;
  let selectedPage = 1;
  let previewSequence = 0;
  let zoom = 100;
  let uploadBusy = false;
  let statusFailures = 0;
  let lastPreviewKey = '';

  function showError(message) {
    byId('errorText').textContent = message;
    byId('formError').hidden = false;
  }
  function clearError() { byId('formError').hidden = true; }
  function setSteps(current) {
    for (const [index, id] of ['stepUpload', 'stepSettings', 'stepProcess', 'stepDownload'].entries()) {
      const element = byId(id);
      element.classList.toggle('is-current', index === current);
      element.classList.toggle('is-complete', index < current);
      if (index === current) element.setAttribute('aria-current', 'step');
      else element.removeAttribute('aria-current');
    }
  }
  function stateLabel(state) {
    return {selected: 'انتخاب‌شده', waiting: 'در انتظار', running: 'در حال پردازش',
      done: 'آماده', partial: 'بخشی با خطا', error: 'ناموفق'}[state] || 'نامشخص';
  }
  function fileSize(bytes) {
    if (bytes < 1048576) return `${number(Math.max(1, Math.ceil(bytes / 1024)))} کیلوبایت`;
    return `${number(Math.round(bytes / 104857.6) / 10)} مگابایت`;
  }
  function fileRow(file, index, submitted) {
    const row = document.createElement('div');
    row.className = 'file-row';
    row.setAttribute('role', 'listitem');
    const name = document.createElement('span');
    name.className = 'file-name';
    const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    icon.setAttribute('class', 'icon');
    icon.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', '#i-document');
    icon.append(use);
    const bidi = document.createElement('bdi');
    bidi.dir = 'auto';
    bidi.title = file.name;
    bidi.textContent = file.name;
    name.append(icon, bidi);
    const size = document.createElement('span');
    size.className = 'file-size';
    size.textContent = fileSize(file.size);
    const state = document.createElement('span');
    state.className = 'file-state';
    const review = file.state === 'done' && file.review_pages > 0;
    state.dataset.state = review ? 'review' : file.state || 'selected';
    state.textContent = `${review ? 'نیازمند بازبینی' : stateLabel(state.dataset.state)}${file.page_count ? ` · ${number(file.pages_done || 0)}/${number(file.page_count)} صفحه` : ''}`;
    const remove = document.createElement('button');
    remove.className = 'remove-file';
    remove.type = 'button';
    remove.textContent = '×';
    remove.setAttribute('aria-label', `حذف ${file.name} از فهرست`);
    remove.disabled = submitted;
    if (!submitted) remove.addEventListener('click', () => { files.splice(index, 1); renderSelection(); });
    row.append(name, size, state, remove);
    return row;
  }
  function renderSelection() {
    const submitted = Boolean(activeJob || uploadBusy);
    const statusFiles = latestStatus && Array.isArray(latestStatus.file_statuses)
      ? latestStatus.file_statuses : null;
    const list = byId('flist');
    list.replaceChildren();
    (statusFiles || files).forEach((file, index) => list.append(fileRow(file, index, submitted)));
    const count = statusFiles ? statusFiles.length : files.length;
    byId('selectionCount').textContent = `${number(count)} فایل`;
    byId('navCount').textContent = number(count);
    byId('selectionEmpty').hidden = count !== 0;
    byId('clearFiles').hidden = submitted || count === 0;
    byId('go').disabled = submitted || count === 0;
    byId('goLabel').textContent = uploadBusy ? 'در حال بارگذاری' : activeJob ? 'پردازش آغاز شد' : 'شروع پردازش';
    byId('drop').setAttribute('aria-disabled', String(submitted));
  }
  function addFiles(incoming) {
    if (activeJob || uploadBusy) return;
    const next = Array.from(incoming);
    if (files.length + next.length > 100) { showError('در هر نوبت حداکثر ۱۰۰ فایل PDF انتخاب کنید.'); return; }
    const allowed = candidateMode ? /\.(pdf|png|jpe?g|tiff?|bmp)$/i : /\.pdf$/i;
    if (next.some((file) => !allowed.test(file.name))) {
      showError(candidateMode ? 'قالب فایل پشتیبانی نمی‌شود.' : 'فقط فایل PDF پذیرفته می‌شود.'); return;
    }
    if (files.reduce((sum, file) => sum + file.size, 0) + next.reduce((sum, file) => sum + file.size, 0) > 500 * 1048576) {
      showError('مجموع حجم فایل‌ها از ۵۰۰ مگابایت بیشتر است.'); return;
    }
    files.push(...next);
    clearError();
    renderSelection();
    if (files.length) setSteps(1);
  }
  byId('file').addEventListener('change', (event) => {
    addFiles(event.target.files);
    event.target.value = '';
  });
  byId('drop').addEventListener('click', () => {
    if (!activeJob && !uploadBusy) byId('file').click();
  });
  byId('drop').addEventListener('keydown', (event) => {
    if ((event.key === 'Enter' || event.key === ' ') && !activeJob && !uploadBusy) {
      event.preventDefault(); byId('file').click();
    }
  });
  for (const type of ['dragenter', 'dragover']) byId('drop').addEventListener(type, (event) => {
    event.preventDefault(); if (!activeJob) byId('drop').classList.add('is-dragover');
  });
  for (const type of ['dragleave', 'drop']) byId('drop').addEventListener(type, (event) => {
    event.preventDefault(); byId('drop').classList.remove('is-dragover');
  });
  byId('drop').addEventListener('drop', (event) => addFiles(event.dataTransfer.files));
  byId('clearFiles').addEventListener('click', () => { files.length = 0; clearError(); renderSelection(); setSteps(0); });

  function updateDpi() {
    const index = Number(byId('dpiControl').value);
    const value = dpiLevels[index];
    byId('dpiValue').textContent = `${number(value)} DPI`;
    byId('dpiLevel').textContent = `سطح ${number(index + 1)} از ${number(dpiLevels.length)}`;
    byId('dpiControl').setAttribute('aria-valuetext', `${number(value)} DPI، سطح ${number(index + 1)} از ${number(dpiLevels.length)}`);
    for (const stop of byId('dpiStops').children) stop.classList.toggle('is-selected', Number(stop.dataset.dpi) === value);
  }
  byId('dpiControl').addEventListener('input', updateDpi);
  updateDpi();

  async function readJson(response) {
    try { return await response.json(); } catch { return {}; }
  }
  function scheduleStatus(delay = 1500) {
    clearTimeout(pollingTimer);
    pollingTimer = window.setTimeout(fetchStatus, delay);
  }
  async function startProcessing(event) {
    event.preventDefault();
    if (!files.length || activeJob || uploadBusy) return;
    uploadBusy = true;
    renderSelection();
    clearError();
    setSteps(2);
    byId('status').hidden = false;
    byId('info').textContent = 'فایل‌ها در حال بارگذاری هستند…';
    byId('status').scrollIntoView({block: 'nearest'});
    const data = new FormData();
    for (const file of files) data.append('pdfs', file);
    data.append('dpi', dpiLevels[Number(byId('dpiControl').value)]);
    data.append('handwriting', byId('candidateHandwriting').checked ? '1' : '0');
    if (byId('candidateProfile')) {
      data.append('profile', byId('candidateProfile').value);
      data.append('candidate_dpi', byId('candidateDpi').value);
    }
    try {
      const response = await fetch('/upload', {method: 'POST', headers: {'X-CSRF-Token': csrfToken}, body: data, credentials: 'same-origin'});
      const body = await readJson(response);
      if (!response.ok || !body.job_id) throw new Error(body.error || 'بارگذاری انجام نشد. دوباره تلاش کنید.');
      activeJob = body.job_id;
      byId('dpiControl').disabled = true;
      byId('candidateHandwriting').disabled = true;
      byId('runDpi').textContent = `${number(candidateMode ? Number(byId('candidateDpi').value) : body.dpi)} DPI`;
      await fetchStatus();
    } catch (error) {
      showError(error.message || 'ارتباط با سرور برقرار نشد.');
      byId('status').hidden = true;
      setSteps(files.length ? 1 : 0);
    } finally {
      uploadBusy = false;
      renderSelection();
    }
  }
  byId('f').addEventListener('submit', startProcessing);

  function renderLog(entries) {
    const log = byId('log');
    log.replaceChildren();
    for (const entry of entries) {
      const line = document.createElement('div');
      line.className = 'log-entry';
      line.textContent = String(entry);
      log.append(line);
    }
  }
  function formatElapsed(totalSeconds) {
    const safe = Math.max(0, Number(totalSeconds) || 0);
    const minutes = Math.floor(safe / 60);
    const seconds = Math.floor(safe % 60);
    return `${number(minutes).padStart(2, '۰')}:${number(seconds).padStart(2, '۰')}`;
  }
  function outputFormat(path) {
    const match = /\.([a-z0-9]+)$/i.exec(path);
    return match ? match[1].toUpperCase() : 'FILE';
  }
  function outputLabel(path) {
    const format = outputFormat(path);
    const parts = path.split('/');
    if (parts.length === 2 && format === 'TXT') return 'متن کامل (TXT)';
    if (parts.length === 2 && format === 'JSON') return 'دادهٔ ساختاریافته (JSON)';
    return `${format} · ${parts.at(-1)}`;
  }
  function downloadHref(path) {
    return `/download/${encodeURIComponent(activeJob)}/${path.split('/').map(encodeURIComponent).join('/')}`;
  }
  function renderDownloads(status) {
    const names = Array.isArray(status.files) ? status.files : [];
    const list = byId('downloadList');
    list.replaceChildren();
    for (const summary of status.file_statuses || []) {
      const documentNames = summary.outputs || [];
      if (!documentNames.length) continue;
      const group = document.createElement('div');
      group.className = 'download-group';
      const heading = document.createElement('div');
      heading.className = 'download-group-title';
      heading.textContent = summary.name;
      const links = document.createElement('div');
      links.className = 'download-links';
      for (const path of documentNames) {
        if (!names.includes(path)) continue;
        const link = document.createElement('a');
        link.href = downloadHref(path);
        link.textContent = outputLabel(path);
        link.setAttribute('download', '');
        links.append(link);
      }
      if (links.childElementCount) { group.append(heading, links); list.append(group); }
    }
    byId('files').hidden = list.childElementCount === 0;
    byId('resultEmpty').hidden = Boolean(names.length);
    byId('resultCount').textContent = number(names.length);
    byId('navResults').textContent = number(names.length);
    for (const badge of byId('formatList').children) {
      badge.classList.toggle('is-ready', badge.dataset.format === 'ZIP' ? Boolean(status.zip) : names.some((path) => outputFormat(path) === badge.dataset.format));
    }
    const zip = byId('zipFile');
    zip.replaceChildren();
    if (status.zip) {
      const link = document.createElement('a');
      link.className = 'zip-link';
      link.href = downloadHref(status.zip);
      link.textContent = 'دریافت همه (ZIP)';
      link.setAttribute('download', '');
      zip.append(link);
    } else {
      const button = document.createElement('button');
      button.className = 'primary-button zip-button';
      button.type = 'button';
      button.disabled = true;
      button.textContent = 'دریافت همه (ZIP)';
      zip.append(button);
    }
  }
  function renderProgress(status) {
    byId('status').hidden = false;
    byId('status').dataset.state = status.state;
    const total = Math.max(0, Number(status.pages_total) || 0);
    const done = Math.max(0, Math.min(total, Number(status.pages_done) || 0));
    const percent = total ? Math.round(done * 100 / total) : 0;
    byId('progressValue').textContent = `${number(percent)}٪`;
    byId('fill').style.width = `${percent}%`;
    byId('progressBar').setAttribute('aria-valuenow', String(percent));
    byId('pageCount').textContent = `${number(done)} از ${number(total)} صفحه بررسی شد`;
    byId('documentCount').textContent = `${number(status.file_count || 0)} سند`;
    byId('elapsedTime').textContent = formatElapsed(status.elapsed_seconds);
    const complete = status.state === 'done' || status.state === 'error';
    const title = status.state === 'done' ? 'پردازش پایان یافت' : status.state === 'error' ? 'پردازش انجام نشد' : 'در حال استخراج متن';
    byId('runTitleText').textContent = title;
    byId('tag').textContent = status.state === 'done'
      ? status.review_pages > 0 ? 'پیش‌نویس نیازمند بازبینی' : 'خروجی خودکار، تأییدنشده'
      : status.state === 'error' ? 'نیازمند بررسی' : 'در حال پردازش';
    byId('info').textContent = complete
      ? `${number(status.failed_pages || 0)} صفحه با خطا، ${number(status.review_pages || 0)} صفحه با هشدار بازبینی`
      : `سند ${number(status.file_index || 0)} از ${number(status.file_count || 0)} در حال بررسی است`;
    renderLog(Array.isArray(status.log) ? status.log : []);
    renderSelection();
    renderDownloads(status);
    if (complete) setSteps(3);
  }
  async function fetchStatus() {
    if (!activeJob) return;
    try {
      const response = await fetch(`/status/${encodeURIComponent(activeJob)}`, {credentials: 'same-origin', cache: 'no-store'});
      const body = await readJson(response);
      if (!response.ok) throw new Error(body.error || 'وضعیت پردازش در دسترس نیست.');
      statusFailures = 0;
      byId('retryStatus').hidden = true;
      latestStatus = body;
      renderProgress(body);
      updateDocumentSelector(body);
      if (body.state === 'running') scheduleStatus();
      else clearTimeout(pollingTimer);
    } catch (error) {
      statusFailures += 1;
      byId('info').textContent = 'دریافت وضعیت موقتاً انجام نشد؛ پردازش سرور ممکن است ادامه داشته باشد.';
      if (statusFailures < 5) scheduleStatus(Math.min(12000, 1500 * statusFailures));
      else byId('retryStatus').hidden = false;
    }
  }
  byId('retryStatus').addEventListener('click', () => { statusFailures = 0; fetchStatus(); });

  function updateDocumentSelector(status) {
    const summaries = status.file_statuses || [];
    const selector = byId('previewDocument');
    const previous = Number(selector.value) || selectedDocument;
    selector.replaceChildren();
    if (!summaries.length) return;
    for (const item of summaries) {
      const option = document.createElement('option');
      option.value = String(item.index);
      option.textContent = item.name;
      selector.append(option);
    }
    selectedDocument = summaries.some((item) => item.index === previous) ? previous : summaries[0].index;
    selector.value = String(selectedDocument);
    selector.disabled = false;
    const maxPage = summaries[selectedDocument - 1].page_count;
    selectedPage = Math.max(1, Math.min(selectedPage, maxPage));
    updatePreviewControls();
    const previewKey = `${activeJob}:${selectedDocument}:${selectedPage}:${summaries[selectedDocument - 1].state}`;
    if (previewKey !== lastPreviewKey) {
      lastPreviewKey = previewKey;
      renderPreview();
    }
  }
  function selectedSummary() { return latestStatus?.file_statuses?.[selectedDocument - 1]; }
  function updatePreviewControls() {
    const summary = selectedSummary();
    const max = summary?.page_count || 0;
    byId('pageIndicator').textContent = `صفحهٔ ${number(max ? selectedPage : 0)} از ${number(max)}`;
    byId('prevPage').disabled = selectedPage <= 1;
    byId('nextPage').disabled = selectedPage >= max;
    byId('zoomOut').disabled = !max || zoom <= 50;
    byId('zoomIn').disabled = !max || zoom >= 200;
    byId('zoomValue').textContent = `${number(zoom)}٪`;
  }
  function showImageEmpty(message) {
    byId('pageImage').hidden = true;
    byId('imageEmpty').textContent = message;
    byId('imageEmpty').hidden = false;
  }
  function showTextEmpty(message) {
    byId('qualityNotice').hidden = true;
    byId('qualityNotice').textContent = '';
    byId('pageText').hidden = true;
    byId('copyPageText').disabled = true;
    byId('textEmpty').textContent = message;
    byId('textEmpty').hidden = false;
  }
  async function renderPreview() {
    const summary = selectedSummary();
    if (!activeJob || !summary) return;
    const sequence = ++previewSequence;
    const image = byId('pageImage');
    showImageEmpty('تصویر صفحه در حال آماده‌سازی است…');
    showTextEmpty('متن صفحه در حال بررسی است…');
    image.onload = () => {
      if (sequence !== previewSequence) return;
      byId('imageEmpty').hidden = true;
      image.hidden = false;
      image.style.width = `${zoom}%`;
      image.alt = `تصویر صفحهٔ ${number(selectedPage)} سند ${summary.name}`;
    };
    image.onerror = () => { if (sequence === previewSequence) showImageEmpty('نمایش تصویر این صفحه انجام نشد.'); };
    image.src = `/preview/${encodeURIComponent(activeJob)}/${summary.index}/${selectedPage}`;
    try {
      const response = await fetch(`/preview-text/${encodeURIComponent(activeJob)}/${summary.index}/${selectedPage}`,
        {credentials: 'same-origin', cache: 'no-store'});
      const body = await readJson(response);
      if (sequence !== previewSequence) return;
      if (!response.ok) throw new Error();
      if (body.state === 'ready') {
        const quality = body.quality || {};
        let notice = 'متن خودکار، تأیید انسانی نشده است.';
        if (quality.status === 'blank') notice = 'این صفحه سفید گزارش شده است.';
        else if (quality.handwriting_declared) notice = 'سند دارای دست‌نویس اعلام شده است. این خوانش قابل اتکا نیست و باید با تصویر و توسط انسان بازبینی شود.';
        else if (quality.review_required && quality.metadata_available) notice = 'پیش‌نویس نیازمند بازبینی؛ متن زیر دارای خوانش مشکوک است و نباید متن تأییدشده تلقی شود.';
        if (Number.isInteger(quality.flagged_items) && quality.flagged_items > 0) {
          notice += ` ${number(quality.flagged_items)} مورد علامت بازبینی دارد؛ این شمارش، تعداد خطاهای قطعی یا درصد دقت نیست.`;
        } else if (!quality.metadata_available) notice += ' جزئیات ابهام این خروجی در دسترس نیست.';
        byId('qualityNotice').textContent = notice;
        byId('qualityNotice').hidden = false;
        byId('pageText').textContent = body.text;
        byId('pageText').hidden = false;
        byId('textEmpty').hidden = true;
        byId('copyPageText').disabled = !body.text;
        byId('previewNote').textContent = body.truncated
          ? 'این پیش‌نمایش کوتاه شده است؛ متن کامل را از خروجی TXT دریافت کنید.'
          : 'خروجی کامل سند را از بخش دریافت خروجی بردارید.';
      } else if (body.state === 'pending') showTextEmpty('متن این سند پس از پایان پردازش نمایش داده می‌شود.');
      else if (body.reason === 'preview_limit') showTextEmpty('این صفحه بیرون از حد پیش‌نمایش است؛ متن کامل را از خروجی TXT دریافت کنید.');
      else showTextEmpty('متن این صفحه در خروجی فعلی موجود نیست؛ گزارش پردازش را بررسی کنید.');
    } catch {
      if (sequence === previewSequence) showTextEmpty('دریافت متن این صفحه انجام نشد.');
    }
  }
  byId('previewDocument').addEventListener('change', (event) => {
    selectedDocument = Number(event.target.value); selectedPage = 1; lastPreviewKey = ''; updatePreviewControls(); renderPreview();
  });
  byId('prevPage').addEventListener('click', () => { selectedPage -= 1; lastPreviewKey = ''; updatePreviewControls(); renderPreview(); });
  byId('nextPage').addEventListener('click', () => { selectedPage += 1; lastPreviewKey = ''; updatePreviewControls(); renderPreview(); });
  for (const [id, direction] of [['zoomIn', 25], ['zoomOut', -25]]) {
    byId(id).addEventListener('click', () => {
      zoom = Math.max(50, Math.min(200, zoom + direction));
      byId('pageImage').style.width = `${zoom}%`;
      updatePreviewControls();
    });
  }
  byId('copyPageText').addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(byId('pageText').textContent);
      byId('copyPageText').textContent = 'کپی شد';
      window.setTimeout(() => { byId('copyPageText').textContent = 'کپی متن صفحه'; }, 1800);
    } catch {
      byId('copyPageText').textContent = 'کپی ممکن نشد';
      window.setTimeout(() => { byId('copyPageText').textContent = 'کپی متن صفحه'; }, 1800);
    }
  });

  function selectMobilePane(kind) {
    const isImage = kind === 'image';
    byId('imageTab').setAttribute('aria-selected', String(isImage));
    byId('textTab').setAttribute('aria-selected', String(!isImage));
    byId('imagePane').classList.toggle('is-active', isImage);
    byId('textPane').classList.toggle('is-active', !isImage);
  }
  byId('imageTab').addEventListener('click', () => selectMobilePane('image'));
  byId('textTab').addEventListener('click', () => selectMobilePane('text'));
  for (const [index, id] of ['imageTab', 'textTab'].entries()) {
    byId(id).addEventListener('keydown', (event) => {
      if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
        event.preventDefault();
        const other = byId(index ? 'imageTab' : 'textTab');
        other.click(); other.focus();
      }
    });
  }
  selectMobilePane('image');

  function closeMenu() {
    byId('sidebar').classList.remove('is-open');
    byId('navOverlay').hidden = true;
    byId('openMenu').setAttribute('aria-expanded', 'false');
  }
  byId('openMenu').addEventListener('click', () => {
    byId('sidebar').classList.add('is-open');
    byId('navOverlay').hidden = false;
    byId('openMenu').setAttribute('aria-expanded', 'true');
    byId('sidebar').querySelector('a').focus();
  });
  byId('navOverlay').addEventListener('click', closeMenu);
  for (const link of byId('sidebar').querySelectorAll('a')) link.addEventListener('click', closeMenu);
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closeMenu(); });

  const help = byId('helpDialog');
  for (const id of ['openHelp', 'openHelpSide']) byId(id).addEventListener('click', () => {
    closeMenu(); help.showModal();
  });
  for (const id of ['closeHelp', 'confirmHelp']) byId(id).addEventListener('click', () => help.close());
  help.addEventListener('click', (event) => { if (event.target === help) help.close(); });
  renderSelection();
})();
