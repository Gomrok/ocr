# مبنای عملکرد CPU

**این اعداد benchmark محلی W هستند، نه latency سرویس تولید و نه SLA.** دستگاه آزمون Windows Server 2025 / x64، Xeon E5-2699 v3، ۲۴ CPU منطقی، RAM حدود ۴۶٫۸۸GiB است. accelerator محاسباتی قابل استفاده مشاهده نشد. Python 3.11.9، numpy 2.4.6، OpenCV headless 5.0.0.93، PyMuPDF 1.28.2 و psutil 7.2.2 ثبت شده‌اند. محیط Python و TEMP آزمون روی C با تنظیمات خصوصی P روی E یکسان فرض نمی‌شود. محدودیت cgroup/VM میزبان و رزرو منابع سایر سرویس‌ها UNKNOWN است.

## روش

چهار PDF مصنوعی مشخص؛ هر کدام یک صفحه اسکن، یک اجرای مستقل پیش از تغییر و یک اجرای نهایی. ۲۰۰۰ DPI، legacy، OMP_THREAD_LIMIT=2، موتور بومی با pool حداکثر چهار فرایند. هر document در child تازه و تحت supervisor با سقف ۸GiB و مهلت ۶۰۰ ثانیه؛ RSS درخت هر ۰٫۱ ثانیه sample می‌شود. source_hash مدل و corpus در evidence هستند. همه source_unchangedها true هستند. warm/cold کنترل‌شده، cache دیسک خالی، affinity ثابت و میزبان اختصاصی نداریم؛ P50/P95 **NOT_MEASURED**. P با fixture جدید اجرا نشده است.

## زمان و حافظه

| نمونه مصنوعی | قبل، ثانیه سند | بعد، ثانیه سند | بعد با startup | peak tree RSS MiB، نمونه‌برداری | CPU seconds درخت، کران پایین | صفحات ناموفق قبل→بعد |
|---|---:|---:|---:|---:|---:|---|
| bench_persian | 17.633 | 18.773 | 21.540 | 716.4 | 39.688 | 0 → 0 |
| bench_mixed | 17.723 | 21.690 | 23.667 | 776.4 | 43.906 | 0 → 0 |
| bench_lowq | 42.190 | 44.434 | 48.793 | 879.4 | 91.281 | 0 → 0 |
| bench_table | 19.185 | 25.017 | 26.736 | 720.3 | 53.297 | 1 → 0 |

افزایش latency ثبت شده است؛ **هیچ ادعای بهبود CPU وجود ندارد**. chunked rendering برای جلوگیری از سطح رندر ناسالم P حفظ شد؛ هزینه آن و تغییرات کنترل/ثبت شواهد باید در ablation جدا شوند. زمان‌های متفاوت دو اجرای پس از اصلاح، دلیل کافی برای اعلام نسبت سرعت یا صدک سرویس نیستند. فایل final_baseline اجرای پیش از اصلاح نهایی type/metadata و final_sealed_baseline اجرای fingerprint نهایی را جدا ثبت می‌کنند؛ nها ادغام نشده‌اند.

| نمونه | render | master_read | preprocess | layout | OCR متن | OCR جدول | numeric verification |
|---|---:|---:|---:|---:|---:|---:|---:|
| bench_persian | 6.026 | 4.02 | 0.213 | 1.536 | 5.821 | NOT_APPLICABLE | 0.548 |
| bench_mixed | 5.962 | 4.316 | 0.271 | 1.516 | 8.888 | NOT_APPLICABLE | 0.006 |
| bench_lowq | 15.741 | 4.018 | 0.167 | 1.271 | 19.206 | NOT_APPLICABLE | 3.215 |
| bench_table | 5.641 | 4.357 | 0.206 | 1.507 | 0.392 | 12.253 | 0.002 |

زمان مرحله و document تقریباً wall time هستند و مراحل موازی قابل جمع ساده به CPU seconds نیستند. sampler صفحه در metrics.py کل درخت application را می‌بیند؛ counterهای CPU/I/O آن **lifetime** هستند و per-job delta یا زمان بارگذاری مدل نیستند. برای benchmark ایزوله از CPU کران پایین supervisor استفاده شده؛ فرایندهای کوتاه ممکن است بین sampleها از دست بروند. peak parent با peak tree متفاوت است. زمان load مدل، warm reuse latency، I/O دقیق یک job و مصرف هم‌زمان چند job NOT_MEASURED هستند؛ شمارش starts/calls جای آن‌ها نیست.

## کنترل‌های اجرا و حدود باقی‌مانده

- render.py محافظ قطعه‌ای P را دارد؛ pixel-equivalence روی PDF مصنوعی کوچک و مرز tile آزموده شده است. این آزمون جای corpus اسکن و عکس نیست.
- سقف CLI برابر ۷۶۸MiB و ۹۰s، stdout محدود و stderr سرکوب‌شده است؛ fallback نامحدود ۹۰۰ثانیه‌ای حذف شد.
- deadline مشترک OCR document=۱۸۰۰s و page=۳۰۰s **cooperative** است؛ pool call از زمان باقی‌مانده استفاده می‌کند. render و کل Python job hard-kill budget ندارند. حافظه سخت مجموع pool هنوز NOT_ENFORCED است.
- سقف raster حدود ۴۰۰MP باقی است. reserve دیسک در legacy هم پیش از رندر کنترل می‌شود. read_region هنوز کل TIFF را decode می‌کند؛ cache ناحیه‌ای یا کاهش DPI خودکار تازه‌ای ادعا نمی‌شود.
- W در همین process با RUN_LOCK اجرای job را serial می‌کند و QUEUE_SLOTS=2 ظرفیت admission دارد؛ Waitress entrypoint شش thread دارد. این قفل بین چند process سرویس مشترک نیست. صف پایدار، quota سراسری، cancellation و retention خودکار کامل نشده‌اند.
- load_job کار نیمه‌تمام را پس از restart به error می‌برد؛ نمونه خروجی تکمیل‌شده قابل دریافت می‌ماند. هیچ job ناقص به‌عنوان ثبت تجاری تأیید نمی‌شود.
- text-first پیش‌فرض خاموش است؛ detector مشکوک به مسیر اصلی برمی‌گردد. آزمون native لاتین مصنوعی عبور کرده، اما مزیت عددی کارایی آن در corpus نماینده NOT_MEASURED است.

جزئیات قابل بازتولید: `evidence/sealed_baseline.json`؛ فرمان `python -B -X utf8 docs/ocr/audit_diagnostics.py baseline --output <new-evidence.json>`. اجرای همان فرمان روی P یا داده مشتری در این مأموریت مجاز نیست.

رندر قطعه‌ای clip شده روی تصویر اسکن یک پسرفت کیفیت ایجاد کرد؛ ablation_single_render آن را جدا کرد و ablation_alias_consensus آن را رفع نکرد. سیاست نهایی sampling کامل برای تک‌تصویر با fallback قطعه‌ای، معیارهای مبنا را برگرداند. final_sealed_baseline و repaired_baseline تاریخچه آزمایش‌های ردشده‌اند و برای اعلام پذیرش نسخه نهایی استفاده نمی‌شوند. اعداد جدول بالا فقط sealed_baseline با source_digest نهایی هستند. هیچ نسبت سرعت معنادار یا کاهش RAM تولیدی از این n=1 استنتاج نمی‌شود.
