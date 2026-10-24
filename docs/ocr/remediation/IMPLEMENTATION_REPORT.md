# گزارش اجرای مرحله دوم اصلاح OCR گمرک

## نتیجه اجرایی

**حکم نهایی: PARTIAL — اصلاحات واقعی محلی انجام شد؛ همه گیت‌ها کامل نشده‌اند؛ استقرار انجام نشده و نسخه آماده استقرار اعلام نمی‌شود.**

۲۰۵ آزمون نهایی موفق، صفر failure/error/skip؛ Ruff محدوده تغییر، syntax پایتون و JavaScript و mypy هفت ماژول جدید موفق‌اند. چهار PDF مصنوعی با موتور واقعی CPU پردازش شدند. متن/اعداد سه نمونه متنی به مبنای قبل بدون پسرفت معیار برگشتند؛ شکست end-to-end صفحه جدول از ۱ به ۰ رسید، اما کیفیت سلول/ردیف آن بهبود اثبات‌شده ندارد. دقت اسناد تجاری واقعی، false-accept عملیاتی و SLA تولید NOT_MEASURED هستند.

هیچ سند مشتری، DB عملیاتی، تنظیم IIS، سرویس P یا release فعال تغییر نکرد. هیچ مدل/وابستگی/image نصب یا دانلود نشد؛ هیچ commit/push/deploy/restart انجام نشد. تغییرات قبلی کاربر با snapshot خصوصی قبل از تغییر حفظ و تفکیک شدند. مقایسه فایل‌های این مرحله با **snapshot پیش از تغییر** است، نه با HEAD که از قبل dirty بود.

## تثبیت واقعیت و منشأ

- W: شاخه main، HEAD `6fbe11cf260f56a44741b78c46ba43727081cb99` با تغییرات قبلی ثبت‌نشده. fingerprint نهایی سورس و مدل‌های application: `87b28f274b6d8042d0ea4cab3d5b0e226c73b220487a24dc64c8930519bed448`.
- P فعال: `52e15ac8ac125993cba5974cdf3d3249bf9524c1` در `E:\GomrokOCR\releases\52e15ac8ac125993cba5974cdf3d3249bf9524c1`. health فقط GET loopback بود. ۵۲ فایل سورس شناخته‌شده P با ابتدای کار hash یکسان داشتند؛ این کنترل به معنی خواندن/ممیزی داده کاربران نیست.
- موتور فعلی عوض نشده: Tesseract بومی 5.5.3.20260724، CPU، مدل‌های محلی fas/eng. hashها در local_models.py و SECURITY_OFFLINE_REPORT آمده‌اند. Paddle نصب نیست.
- baseline اولیه واقعی: ۱۵۴ آزمون؛ ۱۴۶ pass، هفت error و یک failure، عمدتاً فقدان openpyxl. artifact missing از اشکال محصولِ وابسته‌کردن کل صفحه به exporter جدا شد.
- داده مجاز: فقط چهار PDF مصنوعی مشخص مخزن و fixture موقت. snapshot خارج webroot فقط allowlist سورس/assets را دارد؛ secret، uploads و settings عملیاتی کپی نشده‌اند.

## پیشرفت واقعی گیت‌ها

پیشرفت با «تحویل انجام‌شده / مانع باقیمانده» ثبت می‌شود؛ درصد ذهنی تکمیل پروژه یا دقت ساختگی اعلام نمی‌شود. محدودیت یک گیت مانع ادامه بخش مستقل و ایمن گیت بعدی نشد.

| گیت | حکم | تحویل اثبات‌شده | باقی‌مانده / مانع |
|---|---|---|---|
| G0 | PARTIAL | provenance W/P، snapshot خصوصی، baseline و ۱۵۴ تست قبل تغییر؛ تطبیق storage و محافظ renderer مستقل | ادغام quality/candidate/precision و همه routes متفاوت P/W و تست سازگاری آن‌ها BLOCKED_INTEGRATION |
| G1 | PASS_LOCAL | CER فاصله‌حساس، WER واژه، strict/normalized، اعداد multiset/order، Field EM مکانی، grid و status مفقود/خالی؛ چهار نمونه و regression | این PASS کیفیت recognizer یا پذیرش تجاری نیست؛ corpus واقعی موجود نیست |
| G2 | PARTIAL | خروجی‌های TXT/JSON/CSV/HTML با نبود Excel حفظ؛ manifest/table/page/revision/API/UI صریح؛ readiness/admission الزامی آزموده شد | فایل XLSX واقعی و قرارداد کاملِ تبلیغ‌شده محصول: BLOCKED_DEPENDENCY؛ wheel تأییدشده نداریم |
| G3 | PARTIAL | اختلاف .99 تا JSON/review store، صفر confidence، history/candidates، inherited uncertainty و Latin repair اصلاح و آزموده شد | browser/DOM واقعی و همه مسیرهای بازبینی P آزموده نشده؛ numeric_verified قدیمی فقط برچسب reread است، تأیید انسانی نیست |
| G4 | PARTIAL | cloud غیرقابل فعال‌سازی، Docker غیرقابل انتخاب، مدل pinشده، private TEMP، log امن و آزمون‌های owner/CSRF W موفق | OS egress deny/native telemetry/CVE scan و ACL عملیاتی NOT_TESTED؛ امنیت کامل ادعا نمی‌شود |
| G5 | PARTIAL | schema_v2 افزوده/پشت flag، raw/logical/display/canonical، منشأ و bbox معکوس، منبع نهایی precision و حالت‌های missing/unreadable/uncertain | انتشار structured field متناظر با revision انسانی NOT_IMPLEMENTED؛ snapshot قدیمی صریحاً stale است |
| G6 | PARTIAL | قواعد عمومی کانتینر، HS shape/context، Gregorian، Decimal دقیق، وزن، total با قرارداد، شناسه ثبت‌شده و مقایسه scopeدار با آزمون مثبت/منفی | Jalali BLOCKED_DEPENDENCY؛ nomenclature ملی، rule واقعی بارنامه/اظهارنامه و اتصال مجوز پرونده UNKNOWN/NOT_IMPLEMENTED |
| G7 | PARTIAL | registry و schema common_labeled_v1 برای هشت فیلد؛ label تکراری نامطمئن، bbox و provenance؛ هیچ DB جدیدی ساخته نشد | هشت خانواده تخصصی UNSUPPORTED_DOCUMENT_SCHEMA؛ corpus/template معتبر غایب |
| G8 | PARTIAL | نقطه/اعشار چندمقیاسی و حروف معنایی حفظ؛ deskew با خطر clipping رد؛ پسرفت renderer با ablation ریشه‌یابی و رفع؛ چهار نمونه بدون افت معیار نسبت به مبنا | strip-grid/faint/عکس/merged و gridless به‌طور کامل benchmark نشده‌اند؛ جدول فعلی هنوز خطا دارد |
| G9 | PARTIAL | detector native PDF پشت flag خاموش؛ mixed/RTL/rotation مشکوک fallback؛ CLI bounded و deadline مشترک OCR، reserve دیسک و ثبت resource | deadline سخت render/job، سقف حافظه کل pool، queue پایدار/چندprocess، cancellation/retention/quota و cold/warm/P95 کامل نشده؛ بهبود سرعت اثبات نشد |
| P2 | NOT_TESTED | مسیر اصلی به مدل دوم وابسته نشده است | runtime/model/corpus آفلاین تأییدشده موجود نیست؛ نصب/دانلود انجام نشد |

## اصلاحات و ریشه خطاها

1. **معیارهای غلط:** WER جایگزینی یک واژه ۲٫۰ گزارش می‌شد، اکنون ۰٫۵؛ حذف فاصله CER صفر می‌داد، اکنون ۰٫۲؛ حذف عدد تکراری دقت ۱ می‌داد، اکنون recall ۰٫۵. خروجی مفقود، مجموعه خالی و پذیرش بدون provenance موفق محسوب نمی‌شوند. adapter versioned فایل `page_001_table_001.json` و schema_v2.fields را می‌خواند.
2. **coupling exporter و صفحه:** فقدان openpyxl دیگر OCR صفحه جدول را از دست نمی‌دهد؛ status unavailable و دلیل BLOCKED_DEPENDENCY ثبت می‌شود. API سطح پایین strict می‌ماند؛ pipeline و review آگاهانه degraded export را درخواست می‌کنند. اگر OCR_REQUIRE_XLSX=1 باشد job پیش از ذخیره upload رد می‌شود. خطای disk با ImportError یکی گرفته نمی‌شود.
3. **گم‌شدن عدم‌اطمینان:** select_consensus، word merge، Latin repair، numeric/page repair، table و precision.reconcile شواهد و هشدار را منتقل می‌کنند. توافق scaleهای یک موتور و توافق کم‌اطمینان تأیید انسانی نیست. صفر از میانگین confidence حذف نمی‌شود. تاریخچه منابع قبلی هم ادغام می‌شود.
4. **خروج بالقوه اطلاعات:** GoogleVisionEngine به interface سازگار ولی inert تبدیل شد؛ کلید ذخیره/خوانده نمی‌شود و مسیر REST حذف شد. Docker پیش از دسترسی به تصویر غیرقابل استفاده است. مدل مفقود/نامعتبر fail-closed می‌شود. stderr خام و traceback سند وارد لاگ/پاسخ نمی‌شود. این اصلاح فقط W است.
5. **تصحیح معنایی بی‌ردپا:** تبدیل ؤ/أ/إ/ة/ۀ حذف شد؛ ی/ک و display digit policy مشخص باقی است. canonical ارزیابی فقط NFC و همسان‌سازی رقم دارد. اصل تصویر/متن خام حفظ می‌شود؛ داده نامعلوم با مقدار حدسی پر نمی‌شود.
6. **محاسبه و تطبیق نامعتبر:** Decimal با precision پیش‌فرض می‌توانست مبلغ بسیار بزرگ غلط را معتبر اعلام کند؛ precision محلیِ کافی و سقف ورودی اضافه شد. گروه‌های بدون سند مشترک یا واحد/ارز متفاوت دیگر consistent نمی‌شوند. هیچ validator مقدار OCR را تغییر نمی‌دهد یا human_confirmed تولید نمی‌کند.
7. **پسرفت هنگام انتقال محافظ P:** رندر قطعه‌ای تصویر تک‌اسکن sampling متفاوت ایجاد کرد و WER mixed از ۰٫۴۳۳۳ به ۰٫۵۰ رفت. ablation single_render معیار قدیم را برگرداند؛ alias_consensus تغییری نداد. شرط اولیه پوشش ۹۸٪ به علت حاشیه سفید روی fixture اعمال نشد؛ آزمون واقعی این نقص را آشکار کرد. سیاست نهایی «تک‌تصویر بدون متن/بردار، با sampling کامل در بودجه پیکسل و fallback tiled برای رندر سفید» اجرا شد؛ صفحات vector/mixed بزرگ همچنان tiled هستند. آخرین اجرا WER mixed=۰٫۴۳۳۳ و numeric precision=۵/۶ را بازگرداند. شواهد اجرای ردشده باقی‌اند.

## مبنای کیفیت نهایی

n=۱ صفحه برای هر ردیف؛ معیارهای اعشاری زیر فقط همین fixtureها هستند. normalized دقیق NFC_digits_v1 است؛ ستون normalized تاریخیِ harness که display policy قدیمی داشت، مبنای پذیرش نیست. ID occurrence، Field EM تجاری نیست. شمارش بازبینی این جدول فقط wordهاست؛ cellها جدا هستند.

| نمونه مصنوعی | CER strict | WER strict | CER normalized | WER normalized | numeric precision / recall | ID EM تعداد | word نیازمند بررسی | صفحه ناموفق قبل→بعد |
|---|---:|---:|---:|---:|---|---|---|---|
| bench_persian | 0.048998 | 0.087500 | 0.048998 | 0.087500 | 0.1667 / 0.2000 | 1/3 | 45/80 | 0→0 |
| bench_mixed | 0.188073 | 0.433333 | 0.050459 | 0.300000 | 0.8333 / 1.0000 | 5/5 | 25/30 | 0→0 |
| bench_lowq | 0.521158 | 0.650000 | 0.521158 | 0.650000 | 0.1111 / 0.2000 | 1/3 | 95/96 | 0→0 |
| bench_table | 0.000000 | 0.000000 | 0.000000 | 0.000000 | NOT_MEASURED / NOT_MEASURED | 0/0 | 0/4 | 1→0 |

جدول خط‌دار: ۲۰ سلول، ۱۶ مطابق؛ **Cell EM=۰٫۸۰، Row EM=۰٫۴۰، Column EM=۰٫۲۵، grid shape=۱**. این بهبود کیفیت جدول نیست؛ قبل فقط artifact پیش از export بود و کل صفحه fail می‌شد، اکنون خروجی end-to-end موجود است. CER صفر نمونه جدول فقط عنوان بیرون جدول را می‌سنجد. متن/اعداد سه نمونه با معیارهای اصلاح‌شده G1 یکسان‌اند؛ fingerprint هر چهار اجرای نهایی با سورس نهایی یکسان و source_unchanged=true است.

افزایش پرچم بازبینی، افزایش دقت recognition نیست؛ اکنون اختلاف‌هایی که قبلاً پنهان می‌شدند آشکارند. اعداد/شناسه‌های فارسی و اسکن کم‌کیفیت هنوز خطای زیاد دارند. **Field EM نماینده، false-accept عملیاتی، کیفیت خانواده سند، دقت جدول merged/gridless و صحت اسناد مشتری NOT_MEASURED** هستند. هیچ معیار نهایی، مجوز ثبت تجاری صادر نمی‌کند.

## آزمون و کارایی

- نهایی: `evidence/sealed_tests.json` — ۲۰۵ موفق، صفر failure/error/skip، ۴۴٫۹۶s. شکست‌های میانی در TEST_MATRIX و evidence قبلی باقی‌اند.
- `evidence/sealed_static_validation.json` — Ruff محدوده جدید، fatal lint runtime، AST بدون pyc، node --check و mypy هفت ماژول موفق؛ typecheck strict کل پروژه و build بسته انجام نشده‌اند.
- `evidence/sealed_baseline.json` — چهار اجرای واقعی CPU؛ هیچ مدل جایگزین نصب/آزموده نشده. `sealed_quality_comparison.json` فقط non-regression همان چهار نمونه را تأیید می‌کند.
- زمان سند نهایی به ترتیب فارسی/مختلط/کم‌کیفیت/جدول: ۱۸٫۷۷۳، ۲۱٫۶۹۰، ۴۴٫۴۳۴، ۲۵٫۰۱۷ ثانیه. این اعداد بهبود سرعت را ثابت نمی‌کنند؛ جدول قبل fail می‌شد و مقایسه زمانش هم‌ارز موفقیت کامل نیست. زمان P تولید، cold/warm و P50/P95 اندازه‌گیری نشده‌اند.
- peak درخت benchmark به روش sampled و CPU seconds کران پایین ثبت شده؛ counterهای داخل Metrics lifetime/application-tree هستند و نباید per-job delta جا زده شوند. جزئیات در PERFORMANCE_BASELINE.md.

## فایل‌های همین مرحله

۴۱ فایل سورس/تست/harness نسبت به snapshot خصوصی تغییر/اضافه شده‌اند؛ فهرست دقیق و hash قبل/بعد در sealed_change_inventory.json است. README.md، deploy.ps1، requirements.txt، .gitignore و تغییرات قبلی tests/test_web.py نسبت به snapshot این مرحله دست‌نخورده‌اند؛ diff نسبت به HEAD آن‌ها را اشتباهاً به این مرحله نسبت ندهید.

| مسیر نسبی W | نسبت به snapshot پیش از تغییر |
|---|---|
| `docs/ocr/audit_diagnostics.py` | MODIFIED |
| `docs/ocr/remediation/ablation_diagnostics.py` | ADDED |
| `ocr_bot.py` | MODIFIED |
| `ocr_pipeline/benchmark.py` | MODIFIED |
| `ocr_pipeline/contracts.py` | ADDED |
| `ocr_pipeline/engines.py` | MODIFIED |
| `ocr_pipeline/fields.py` | ADDED |
| `ocr_pipeline/local_models.py` | ADDED |
| `ocr_pipeline/metrics.py` | MODIFIED |
| `ocr_pipeline/native_pool.py` | MODIFIED |
| `ocr_pipeline/observations.py` | ADDED |
| `ocr_pipeline/pdf_text.py` | ADDED |
| `ocr_pipeline/pipeline.py` | MODIFIED |
| `ocr_pipeline/precision.py` | MODIFIED |
| `ocr_pipeline/preprocess.py` | MODIFIED |
| `ocr_pipeline/render.py` | MODIFIED |
| `ocr_pipeline/review.py` | MODIFIED |
| `ocr_pipeline/tables.py` | MODIFIED |
| `ocr_pipeline/validators.py` | ADDED |
| `run_benchmark.py` | MODIFIED |
| `serve.py` | MODIFIED |
| `static/review.js` | MODIFIED |
| `tests/test_acceptance.py` | MODIFIED |
| `tests/test_accuracy.py` | MODIFIED |
| `tests/test_benchmark_contract.py` | ADDED |
| `tests/test_data_contract.py` | ADDED |
| `tests/test_engines.py` | MODIFIED |
| `tests/test_export_contract.py` | ADDED |
| `tests/test_fields.py` | ADDED |
| `tests/test_glyph_safety.py` | ADDED |
| `tests/test_offline_contract.py` | ADDED |
| `tests/test_pdf_text.py` | ADDED |
| `tests/test_performance.py` | MODIFIED |
| `tests/test_precision.py` | MODIFIED |
| `tests/test_readiness_contract.py` | ADDED |
| `tests/test_remediation_regressions.py` | ADDED |
| `tests/test_review.py` | MODIFIED |
| `tests/test_uncertainty_contract.py` | ADDED |
| `tests/test_validators.py` | ADDED |
| `tools/evaluate_accuracy.py` | MODIFIED |
| `web_app.py` | MODIFIED |

شش گزارش همین پوشه، evidenceهای مصنوعی و harness ablation نیز تحویل‌اند؛ فایل‌های خروجی OCR مشتری یا API key وارد Git نشده‌اند. فایل‌های untracked از قبل، مانند precision.py و review.py، در inventory «MODIFIED» ثبت می‌شوند چون پیش از این مرحله وجود داشتند.

## backlog باقیمانده و معیار پذیرش

| اولویت | کار / محل | پیش‌نیاز | معیار پذیرش |
|---|---|---|---|
| P0 | تطبیق P/W: web_app، quality، precision/، review، preview، templates | قرارداد routes و fixture مجاز P | تمام قراردادهای owner/session/CSRF/revision/preview و quality دو نسخه بدون حذف قابلیت پاس شوند |
| P0 | بسته offline exporter و سیاست XLSX: tables/serve/web_app | wheelhouse تأییدشده، تصمیم mandatory/degraded | export XLSX واقعیِ text-only، صفر ابتدایی و فرمول/XSS؛ readiness و rollback واقعی محلی |
| P0 | پذیرش corpus: benchmark/evaluate_accuracy/schema registry | مرجع مجاز held-out با label/bbox/خوانایی | Field EM و خطای حساس بی‌هشدار و association جدول با n کافی، از پیش آستانه‌گذاری‌شده |
| P1 | hard containment کل job/pool/render و صف/retention | VM ایزوله و سیاست مالک داده | timeout/crash/cancel/restart چندprocess، سقف درخت/دیسک و عدم موفق‌نمایی کار ناقص |
| P1 | OS egress deny / ACL / dependency audit | محیط ایزوله و artifact list تأییدشده | اجرای بدون شبکه در سطح OS؛ هیچ payload یا secret بیرون نرود |
| P1 | Jalali و rules ملی/شناسه واقعی: validators/fields | کتابخانه/مرجع آفلاین معتبر و schema کشور | مثبت/منفی/کبیسه/مرزی و context ناشناخته بدون حدس |
| P1 | structured human revision و adapter پرونده | API واقعی و authorization مشخص | snapshot خودکار مستقل؛ فیلد انسانی نسخه‌دار با scope و audit event؛ اجرای مجدد تأیید قدیمی را ارث نبرد |
| P1 | gridless/merged/faint/عکس، table association، region I/O | dataset و budget | افت صفر در شناسه/نقطه/اعشار، EM سطر/ستون و RAM/CPU قابل تکرار |
| P2 | مقایسه Paddle/region tuning/native hybrid | artifact و corpus پس از P0/P1 | held-out یکسان، false-accept و منابع؛ عدم برتری اثباتی یعنی عدم ارتقا |

## تصمیم‌های باز، بدون پرسش حین اجرا

UNKNOWN/NEEDS_USER_INPUT برای مرحله بعد: قرارداد نهایی XLSX، API سامانه اصلی و مجوز پرونده، قواعد کشور/edition، دامنه corpus مجاز و GT، سیاست retention/backup/ACL، SLA و بار هم‌زمان مورد انتظار. wheelخانه معتبر openpyxl/Jalali و artifact موتور دوم در این محیط موجود نیستند. این کمبودها با نصب اینترنتی، ساخت داده مرجع یا حدس قانون دور زده نشدند.

مسیر فنی بعدی هنوز **REPAIR_CURRENT_ENGINE** است؛ شواهد کافی برای REPLACE_OCR_ENGINE یا ادعای برتری Paddle نداریم. optional hybrid PDF فعلاً آزمایشی و خاموش است. گزارش مرحله دوم **PARTIAL** است؛ کد و تست تحویل شده‌اند، اما همه گیت‌های پذیرش و آمادگی production کامل نشده‌اند. برنامه انتشار/rollback صرفاً مستند است و اجرا نشده است.
