# ممیزی فنی OCR سامانه گمرک

تاریخ: ۲۰۲۶-۱۰-۲۴، منطقهٔ زمانی America/Los_Angeles.

این بررسی فقط شامل خواندن سورس و تنظیمات مجاز، خواندن وضعیت محلی سرویس، آزمون‌های ایزوله با دادهٔ مصنوعی و تولید مستندات است. هیچ سند کاربر، دیتابیس، تنظیم عملیاتی، سرویس، مدل یا نسخهٔ مستقر تغییر نکرد. هیچ مدل یا بسته‌ای نصب یا دریافت نشد. هیچ commit، push یا استقراری انجام نشد. درخواست‌های اینترنتی ممیزی صرفاً برای مستندات عمومی موتورهای OCR بود؛ هیچ محتوا، تصویر، خروجی سند، رمز یا تنظیم خصوصی به آن درخواست‌ها اضافه نشد.

### A. Executive Summary

**نتیجهٔ قابل دفاع: `REPAIR_CURRENT_ENGINE`.** خطاهای قابل بازتولید در معیارهای ارزیابی، انتقال هشدار، تبدیل خروجی، وابستگی Excel و مسیرهای پردازش وجود دارد. شواهد فعلی برای تعویض موتور کافی نیست. چهار نمونهٔ مصنوعی برای تشخیص نقص مفیدند، اما نمایندهٔ اسناد واقعی گمرکی نیستند.

مهم‌ترین یافته، **ناهمسانی نسخهٔ کاری و نسخهٔ واقعاً فعال** است:

| مرجع | هویت و وضعیت مشاهده‌شده |
|---|---|
| `W`، پوشهٔ کاری این مخزن | HEAD برابر `6fbe11cf260f56a44741b78c46ba43727081cb99`؛ از ابتدا دارای تغییرات ثبت‌نشده و فایل‌های untracked بود. |
| اثرانگشت کد و مدل‌های W | `2bd6bd7de2efabcfd93b7e6c92861eac0b54c56cc5416799a34f1d6f25a36e68` از `ocr_pipeline/provenance.py:source_digest`؛ این شناسه جای commit تغییرات ثبت‌نشده را نمی‌گیرد. |
| `P`، نسخهٔ مستقر | `52e15ac8ac125993cba5974cdf3d3249bf9524c1`؛ پاسخ `127.0.0.1:15101/healthz` و مسیر کدِ مدل در تنظیمات خصوصی هر دو این نسخه را نشان دادند. |
| مسیر کد P | `E:\GomrokOCR\releases\52e15ac8ac125993cba5974cdf3d3249bf9524c1\`؛ از تنظیم مسیر مدل، cwd و مسیر serve.py فرایند شنونده تأیید شد؛ در ادامه `P/...` به این ریشه اشاره می‌کند. مسیر پیش‌فرض deploy.ps1 با محل فعال یکی نیست. |
| موتور فعال P | Tesseract محلی، مسیر `legacy`؛ متغیر محیطی فرایند فعال `OCR_PIPELINE_MODE` تنظیم نشده و مقدار پیش‌فرض `legacy` است. `OMP_THREAD_LIMIT=2` و `OCR_TESSERACT_ONLY=1`. |
| موتور دوم | W پیاده‌سازی اختیاری PaddleOCR دارد؛ P چنین موتور نصبی یا تنظیم‌شده‌ای ندارد. `P/ocr_pipeline/precision/` یک مسیر آزمایشی **Tesseract** است و با `W/ocr_pipeline/precision.py` مبتنی بر موتور دوم تفاوت دارد. |

درصدهای confidence موتور، دقت اندازه‌گیری‌شده نیستند. وجود «شناسه» در JSON هم به معنای اعتبارسنجی شناسه نیست. در مسیر فعال، طبقه‌بندی اسناد گمرکی، استخراج فیلدهای دارای معنای تجاری و ثبت آن‌ها در دیتابیس پیاده‌سازی نشده است.

سطح اطمینان: برای مسیرهای سورس، نصب محلی، اختلاف نسخه‌ها و آزمون‌های مصنوعی **بالا**؛ برای دقت روی پرونده‌های واقعی، رفتار تحت بار عملیاتی و برتری موتور جایگزین **ناکافی / NOT_MEASURED**. نتایج عملکرد، اجرای ایزولهٔ کد روی همین میزبان ویندوز هستند؛ اندازه‌گیری latency درخواست واقعی کاربر یا SLA سرویس نیستند.

شواهد قابل بازتولید در [پوشهٔ evidence](evidence/) و ابزار [audit_diagnostics.py](audit_diagnostics.py) قرار دارند. فقط مقادیر تجمیعی، نام تست، اثرانگشت کد و دادهٔ مصنوعی ذخیره شده‌اند.

نام `adjacent_venv_versions` در evidence مربوط به محیط مجاور C است؛ نصب cold فرایند فعال جدا ثبت شده و نسخهٔ کتابخانهٔ ازقبلبارشدهٔ نامعلوم، نامعلوم باقی مانده است. هیچ probe یا patchی به فرایند زنده تزریق نشد.

### B. Current Architecture

#### مسیر فعال P

```text
IIS / HTTPS
  → Waitress، loopback:15101، شش thread
  → Flask: session مالک + CSRF + محدودیت فایل، نرخ و صف
  → POST /upload: ذخیرهٔ PDF با نام UUID؛ بررسی امضا، بازشدن و تعداد صفحات
  → daemon Thread: _run_job → _process_job → RUN_LOCK
  → process_pdf(mode=legacy)
  → render_master: رندر خاکستری قطعه‌ای، مستر TIFF lossless تا ۲۰۰۰ DPI
  → ocr_page: تحلیل تا ۵۰۰ DPI → پیش‌پردازش مشروط → تحلیل ناحیه و جدول
  → Tesseract: چهار worker بومی حداکثر؛ چند مقیاس، PSM و زبان
  → ترمیم محدود مبتنی بر پیکسل، بازسازی سطر و BiDi، متن و جدول
  → page_quality و هشدار تأییدنشدن انسانی
  → TXT / JSON / quality.json / CSV / HTML / تلاش برای XLSX / ZIP
  → وضعیت در حافظهٔ JOBS؛ download فقط برای session مالک
```

مراجع P: `serve.py:configure_storage/main`؛ `web_app.py:242 _process_job`، `310 _run_job`، `346 upload`، `460 status`، `625 download`؛ `ocr_pipeline/pipeline.py:967 process_pdf`، `1018 _process_document`، `51 ocr_page`؛ `render.py:70 render_master`؛ `quality.py:word_needs_review/page_quality`.

سرویس مدلِ شبکه‌ای، broker صف، worker مستقل پایدار و دیتابیسِ فیلدهای گمرکی در این مسیر وجود ندارد. اجرای OCR از پاسخ آپلود جداست، ولی هماهنگ‌کننده و queue در همان فرایند وب هستند. restart، کار نیمه‌تمام و وضعیت حافظه‌ای P را بازیابی خودکار نمی‌کند.

مسیرهای `compare` و `candidate` در P وجود دارند ولی فعال نیستند. `P/ocr_pipeline/precision/extraction.py:process_document/process_page` با `LocalRecognizer` و پروفایل‌های فارسی/انگلیسی/مختلط، رندر ۳۰۰ یا ۴۰۰ DPI، snapshot نسخه‌دار، مختصات و بازبینی کار می‌کند. این مسیر موتور دیگری نیست؛ دست‌نویس فقط declaration کاربر است، نه مدل تشخیص دست‌نویس. گردش `/reviews` در P برای extraction همین مسیرها ثبت می‌شود؛ `_process_job` آن را تنها با وجود `extraction_revision` ثبت می‌کند. بنابراین وجود صفحهٔ بازبینی را نباید به معنی ثبت خودکار همهٔ اسناد legacy در آن دانست.

#### تفاوت‌های مهم W

مراجع W: [web_app.py](../../web_app.py)، [pipeline.py](../../ocr_pipeline/pipeline.py)، [precision.py](../../ocr_pipeline/precision.py)، [review.py](../../ocr_pipeline/review.py).

W علاوه بر legacy، `quality_mode=maximum_local` دارد: Tesseract اولیه، سپس `PaddleEngine` در فرایند جدا، تشخیص قطعه‌ای ۱۰۲۴ پیکسل با هم‌پوشانی ۱۲۸، تطبیق سطر/سلول و بازخوانی عددی. `review_units` و `numeric_checks` حاصل این مرحله‌اند. W وضعیت job را در manifestهای خصوصی JSON ذخیره می‌کند و بازبینی نسخه‌دار سطر/سلول دارد. این قابلیت‌ها **به نسخهٔ فعال نسبت داده نمی‌شوند**.

W برخلاف P هنوز کلاس Google Vision را دارد، ولی `_process_document:1002` آن را فقط با `OCR_LOCAL_ONLY=0`، نبود force_tesseract، حالت legacy و وجود کلید وارد مسیر می‌کند. مقدار پیش‌فرض محلی است و maximum_local این شاخه را وارد نمی‌کند.

### C. Answers Q01–Q23

#### Q01. موتور واقعی

P فعال: **Tesseract 5 بومی، LSTM با مدل‌های محلی**؛ نه PaddleOCR، نه EasyOCR، نه LLM. DLL و CLI دو backend اجرای همان موتورند. P مسیر candidate هم Tesseract است. W به‌صورت اختیاری Tesseract + PaddleOCR دارد؛ Google Vision نیز فقط در شاخهٔ اختیاری غیرمحلی W باقی مانده است. شواهد: P `pipeline.py:967–1027` و `precision/engine.py:49`؛ W `pipeline.py:963`، `paddle_engine.py:MODELS`، `engines.py:554/618`.

#### Q02. زبان، فریم‌ورک و نسخه‌ها

Python 3.11.9، Windows x64، Flask 3.1.3، NumPy 2.4.6، OpenCV headless 5.0.0.93، PyMuPDF 1.28.2، psutil 7.2.2 در نصب cold بررسی‌شده یافت شدند. محیط مجاور `C:\Websites\ocr.gomrok.org\runtime\.venv` دارای Waitress 3.0.2 است؛ **فرایند واقعاً فعال** با Python عمومی `C:\Program Files\Python311\python.exe` و serve.py روی E اجرا می‌شود. metadata نصب Waitress در اجرای تازهٔ همین Python پیدا نشد؛ با وجود سرویسِ زنده، نسخهٔ ماژول ازقبلبارشده در حافظه **UNKNOWN** است و 3.0.2 را نمی‌توان قطعاً به آن نسبت داد. `openpyxl` در نصب cold عمومی و محیط مجاور یافت نشد. فهرست requirements اصلی عمدتاً بازهٔ حداقلی است، نه lock کامل. W نیاز `openpyxl>=3.1.5,<4` را افزوده، اما requirements نسخهٔ P آن را ندارد. نسخه‌های Paddle در lock W، **مقادیر مطلوب نصب** هستند: paddleocr 3.7.0، paddlex 3.7.2، paddlepaddle 3.3.1؛ هیچ‌کدام در runtimeهای بررسی‌شده نصب نیستند. شواهد: `requirements.txt`، `requirements-paddle.lock`، P `requirements.txt` و [environment.json](evidence/environment.json).

#### Q03. محل اجرا

هماهنگ‌کننده داخل Flask، daemon thread و `RUN_LOCK`؛ حداکثر یک job در حال اجرای OCR و یک job منتظر، در هر فرایند وب. پردازش‌های بومی Tesseract فرزند همین کار هستند. صف پایدار، Celery/RQ یا سرویس OCR مستقل وجود ندارد. Waitress شش thread به معنی شش OCR موازی نیست. شواهد: W `web_app.py:55–63,978,1038,1137` و `serve.py:main`؛ P `web_app.py:64–65,242,310,444`.

#### Q04. مسیر آپلود تا دیتابیس

مسیر B کامل است. اعتبارسنجی HTTP، ذخیرهٔ فایل، `process_pdf`، render، preprocess، layout، OCR متن/سلول، بازخوانی، normalization، خروجی و download انجام می‌شود. **ثبت فیلد در دیتابیس: NOT_IMPLEMENTED**؛ ORM، schema تجاری، migration و repository دادهٔ پرونده در این سورس یافت نشد. JSONهای OCR، وضعیت و بازبینی، دیتابیس عملیاتی فیلدها نیستند. مراجع W: `web_app.py:1072,978,956,970,1163`؛ `pipeline.py:963,987,55,838,1131`؛ `tables.py:182`؛ `review.py:122,155,282`.

#### Q05. تفاوت بر اساس نوع سند

طبقه‌بندی Invoice/CMR/اظهارنامه وجود ندارد. تفکیک فقط بر اساس **نوع ناحیه** متن، سلول، عدد، glyph، line و sparse است. P candidate پروفایل زبانی و کیفیت/هندسهٔ قابل تنظیم دارد؛ آن هم classifier نوع سند نیست. W `TesseractEngine.PSM_FOR:832`؛ P `precision/profiles.py:14,18`.

#### Q06. CPU، مدل، RAM و thread

Tesseract نصب‌شده: `v5.5.3.20260724`، Leptonica 1.87.0، پشتیبانی AVX2/AVX/FMA/SSE4.1. مسیر exe: `C:\Tools\GomrokOCR\Tesseract\tesseract.exe`. مدل W در `tessdata/` و مدل P در `P/tessdata/`؛ SHA-256 هر سه در W و P برابر است. فعال GPU وجود ندارد. pool حداکثر چهار فرایند و سه handle زبان در هر فرایند دارد؛ `OMP_THREAD_LIMIT=2` در فرایند فعال مشاهده شد. RAM واقعی در G آمده است. W Paddle detector صریحاً `device=cpu,cpu_threads=8,enable_mkldnn=False` و recognizer با چهار thread/oneDNN تعریف شده، ولی بارگذاری واقعی آن **NOT_TESTED** است. شواهد: `native_pool.py:52,126`، W `paddle_worker.py:53–72` و `deploy.ps1:147`.

#### Q07. شبکه و خروج ناخواسته

در مسیر فعال P کلید خارجی به `process_pdf` داده شود رد می‌شود و engine_list فقط محلی است. در W، `GoogleVisionEngine.ocr_image:561`، PNG ناحیه را base64 کرده به `vision.googleapis.com/v1/images:annotate` می‌فرستد؛ default خاموش بودن، حذف قابلیت خروج نیست. نصب‌گر W آگاهانه از Hugging Face و PyPI دانلود می‌کند؛ در مسیر درخواست OCR فراخوانی نشده و در این ممیزی اجرا نشد. Docker fallback ممکن است image ناموجود را خودکار pull کند؛ `--network=none` یا `--pull=never` ندارد. SDKهای downloader در lock Paddle دیده می‌شوند؛ worker آفلاین، مسیر مدل صریح و guard پایتونی دارد. telemetry/error-tracking خارجیِ صریح در کد برنامه یافت نشد؛ تضمین برای همهٔ کد native یا وابستگی‌ها صادر نمی‌شود. جزئیات F.

#### Q08. PDF متن‌دار، اسکن، ترکیبی و تصویر

legacy در W/P همهٔ صفحات PDF را raster می‌کند؛ تشخیص و routing متن‌دار/اسکن/ترکیبی ندارد. W آپلود مستقل تصویر را رد می‌کند. P فعال legacy نیز فقط PDF می‌پذیرد؛ **در P candidate غیرفعال** پشتیبانی PNG/JPEG/TIFF/BMP و TIFF چندصفحه‌ای وجود دارد. W `precision.detail_dpi:28` صرفاً برای انتخاب DPI وجود text را نگاه می‌کند؛ استخراج متن نیست. P `precision/imaging.py:16–83` و `web_app.py:371–379`.

#### Q09. استخراج مستقیم متن و معیار کیفیت

**NOT_IMPLEMENTED** در مسیر خروجی legacy و candidate P. `get_text` در W فقط تصمیم DPI کمکی است. هیچ معیار معتبری مانند پوشش متن/پیکسل، glyph خراب، mapping فونت یا تطبیق شماره با تصویر برای پذیرفتن لایهٔ متن PDF وجود ندارد. شواهد: `pipeline._process_document` و `render_master` در هر دو نسخه؛ P `precision/imaging.py:72` صریحاً `embedded_text_used=False` ثبت می‌کند.

#### Q10. لایهٔ متن مخدوش

در مسیر فعال، لایهٔ متن مخدوش **OCR را skip نمی‌کند**؛ متن PDF جای خروجی OCR قرار نمی‌گیرد. در W maximum_local، هر متن غیرخالی می‌تواند شاخهٔ تشخیص native scan DPI را عوض کند (`precision.detail_dpi`)؛ این اثر انتخاب وضوح است، نه حذف OCR. اثر تجربی بر PDF دارای لایهٔ مخدوش **NOT_MEASURED**.

#### Q11. رندر، DPI و کوچک‌سازی

PyMuPDF، خاکستری ۸بیتی، پیش‌فرض ۲۰۰۰ DPI و سقف ۴۰۰ مگاپیکسل؛ بالاتر از سقف effective_dpi کاهش و ثبت می‌شود. TIFF Deflate lossless است. P رندر مستر را به قطعات ۳۰۰۰ پیکسلی محدود و سفیدشدن غیرواقعی را با probe کنترل می‌کند؛ W تک‌رندر دارد و آن محافظ را ندارد. تحلیل تا ۵۰۰ DPI؛ تحلیل متن دوباره تا لبهٔ ۱۸۰۰؛ OCR ناحیه با هدف ارتفاع glyph حدود ۳۸ و معمولاً سقف لبهٔ ۳۵۰۰ کوچک می‌شود. بنابراین موتور OCR مستقیماً مستر ۲۰۰۰ DPI را نمی‌خواند. W maximum_local رندر جزئیات جدا تا ۶۰۰ DPI/۸۰ مگاپیکسل و detector tile دارد. P candidate فقط ۳۰۰/۴۰۰ DPI با سقف ۲۵ مگاپیکسل پیش‌فرض است. مراجع: `render.py:59,68`، `preprocess.py:149`، `layout.py:88`، `pipeline.py:37,140`؛ P `render.py:26,70` و W `precision.py:397`.

#### Q12. هندسه و پیش‌پردازش

legacy: برآورد skew با minAreaRect و بازهٔ حدود ±۷ درجه؛ چرخش با همان اندازهٔ بوم؛ روشنایی پس‌زمینه/کنتراست/حذف CC ریز به‌صورت مشروط. چرخش خودکار ۹۰/۱۸۰ درجه، OSD، dewarp صفحه و تشخیص خودکار چهارگوش عکس **NOT_IMPLEMENTED** در مسیر فعال. توابع `binarize_adaptive/remove_quiet_borders/preprocess_master` وجود دارند ولی با داشتن تابع، فعال‌بودن در `ocr_page` اثبات نمی‌شود. P candidate چرخش و perspective صریح کاربر، بوم گسترش‌یافته و variantهای grayscale/adaptive دارد؛ این‌ها فعال عمومی نیستند. W `_local_read` برش سطر را براساس polygon با بوم بزرگ‌تر می‌چرخاند. مراجع: `preprocess.py:21–143`، `pipeline.py:72–98`؛ P `precision/imaging.py:110,141`.

#### Q13. خطر حذف نقطه، اعشار و خط تیره

بله. آزمایش مصنوعی چهار پیکسل جوهر جدا، پس از `denoise_preserve_dots` صفر شد؛ آستانهٔ مساحت ≤۶ به فاصله از حروف یا DPI ورودی وابسته نیست. این شاهدِ مکانیزم حذف است، نه اثبات حذف حرف مشخص در سند کاربر. `strip_grid_lines` لبه‌های دارای پوشش >۴۰٪ و mask خطوط را سفید می‌کند؛ خط تیره یا علامت مماس با حاشیه ممکن است همراه خط حذف شود. `_glyph_digit_ocr` نیز علامت‌های کوچک را با قواعد هندسی تفسیر می‌کند. بازنمونه‌برداری و deskew با بوم ثابت هم خطر افت جزئیات/بریدن حاشیه دارند. تست‌های حفظ نقطه بخشی از حالات را می‌پوشانند، نه تضمین همهٔ DPIها. مراجع: `preprocess.py:93,63`، `tables.py:36`، `pipeline.py:526`؛ `evidence/diagnostics.json`.

#### Q14. جدول، ستون و فیلد کوتاه

جدول خط‌دار با morphology، شبکه و OCR سلول‌به‌سلول پردازش می‌شود؛ PSM و مقیاس متفاوت است. فیلد کوتاه عددی/glyph هم PSM جدا دارد. تشخیص معنایی فیلد، جدول بدون خط و تشخیص span واقعی وجود ندارد؛ همهٔ spanها ۱ ذخیره می‌شوند. خطوط شبکهٔ حداقل سه مرز افقی و عمودی شرط تشخیص فعلی‌اند؛ جدول سادهٔ یک‌ردیفه/یک‌ستونه ممکن است از مسیر جدول خارج شود. ترتیب چندستونه legacy heuristic است، نه layout model آموخته‌شده. `layout.detect_tables/analyze_layout`، `tables.cell_bboxes/build_table_model`، `pipeline._cell_ocr_best/_ocr_table`.

#### Q15. مختصات

شماره صفحه و word/cell bbox در مختصات مستر موجود است. `lines` در legacy فقط رشته است و polygon جهت‌دار کلمه نگهداری نمی‌شود. W maximum_local، `review_units.source_bbox` را با بازگردانی deskew به تصویر اصلی نگاشت می‌کند. P candidate مختصات reference_pixels و transformهای variant را ذخیره می‌کند. **مختصات فیلد معنایی مانند gross_weight: NOT_IMPLEMENTED**، چون خود فیلد وجود ندارد. W `pipeline.py:252–286`، `precision.source_box`، `review.create_document`؛ P `precision/imaging.py:97` و `extraction.py:316`.

#### Q16. هویت دقیق مدل فارسی

مدل واقعی `fas.traineddata` است، با اندازهٔ ۳٬۳۲۵٬۹۵۵ بایت و SHA-256 برابر `99e420969b5ddd2cb135b416316a7ed417c59c4faf9e0d28941348f6448114df`. خروجی محلی `combine_tessdata -d/-l`:

```text
Version: 4.00.00alpha:fas:synth20170629
network: [1,48,0,1Ct3,3,16Mp3,3Lfys64Lfx96Lrx96Lfx192O1c1]
int_mode=0; iteration=896400; sample_iteration=897843
```

عدد 4 در version داده، نسخهٔ exe نیست؛ exe واقعی 5.5.3 است. `eng.traineddata`: ۱۵٬۴۰۰٬۶۰۱ بایت، SHA-256 `8280aed0782fe27257a68ea10fe7ef324ca0f8d85bd2fd145d1c2b560bcb66ba`، LSTM float. `Arabic.traineddata`: ۱۷٬۰۹۵٬۲۷۹ بایت، SHA-256 `a80454f72df35c6f52550551dd69b8521ea6a62d834396d1c7dce4c35493902e`؛ فقط برای بازخوانی محدود رقم. منبع مستند Arabic در `tessdata/SOURCES.md` commit `e12c65a915945e4c28e237a9b52bc4a8f39a0cec` است. P candidate همین commit و hashهای fas/eng را pin می‌کند؛ تاریخ و زنجیرهٔ دانلود تاریخی آن‌ها **UNKNOWN** است. نام فایل به‌تنهایی گواه مدل best/fast نیست؛ مشخصات و hash فوق هویت محلیِ قابل اثبات‌اند.

مدل‌های تعریف‌شده، ولی نصب‌نشدهٔ W: `PP-OCRv5_server_det` و `arabic_PP-OCRv5_mobile_rec`، با revision و hash در `model_artifacts.py`. این نام‌ها با `PP-OCRv5_server_rec` یکی نیستند.

#### Q17. انتخاب زبان

پیش‌فرض فعال `fas` است؛ انگلیسی به‌طور هم‌زمان در تمام متن روشن نیست. در شرایط Latin context، garbage یا confidence پایین، گذر جداگانهٔ `eng` و ادغام مکانی اجرا می‌شود. `--langs fas+eng` در CLI قابل انتخاب است. P candidate پروفایل `mixed=fas+eng` دارد، اما غیرفعال است. W precision براساس خوانش برش بین fas و eng انتخاب می‌کند. `engines.TesseractEngine.__init__`، `pipeline.py:171–213`، P `precision/profiles.py:14`.

#### Q18. Unicode

ی/ي و ک/ك همسان می‌شوند، ولی mapping از این دو فراتر می‌رود: `ة/ۀ→ه`، `أ/إ→ا`، `ؤ→و`؛ این‌ها همیشه صرفاً presentation-equivalent نیستند. نیم‌فاصله در normalization عمومی حفظ می‌شود؛ در canonical/ارزیابی W ممکن است به فاصله تبدیل شود. NFC در evaluator جدید وجود دارد، نه لزوماً هر خروجی legacy. P candidate normalization جست‌وجو جدا و محدودتر دارد. W `engines.py:50–85`؛ P `engines.py:37–72`؛ W `precision.canonical`؛ P `precision/extraction.py:25`.

#### Q19. ارقام

ارقام عربی و لاتین در `display_text` به فارسی تبدیل می‌شوند؛ نسخهٔ لاتین با `normalized_en_digits` جدا نگه داشته می‌شود. این تبدیل شامل رشته‌های شناسه و انگلیسی هم هست؛ مقدار عددی عوض نمی‌شود، اما Exact Match رشته‌ای، مصرف downstream و تشخیص token LTR ممکن است تحت تأثیر قرار بگیرد. `U+066C` به comma تبدیل می‌شود؛ parser معتبر Decimal/locale/واحد وجود ندارد. رشته نباید برای شناسه به float/int تبدیل شود؛ حفظ صفر ابتدایی فعلاً از رشته‌بودن ناشی است. مراجع: `engines.normalize_text/display_text/en_digits_only`.

#### Q20. OCR یا BiDi

هر دو لایه می‌توانند خطا ایجاد کنند. `words_to_lines` خط‌های HOCR/مرکز y را گروه‌بندی و x را RTL مرتب می‌کند؛ `_order_bidi_runs` جزیره‌های دارای ASCII لاتین/رقم را معکوس و اجزای دارای hyphen را متصل می‌کند. این پیاده‌سازی، الگوریتم کامل Unicode BiDi نیست. `display_text` سپس ارقام را فارسی می‌کند و مرورگر `dir=rtl` نمایش می‌دهد؛ تفاوت خروجی خام، رشتهٔ منطقی JSON و نمایش UI باید جدا سنجیده شود. آزمون‌های synthetic line grouping و Latin islands گذشتند، ولی دادهٔ کافی برای نسبت‌دادن تمام خطای واقعی به یکی از لایه‌ها نداریم. اختلاف CER سخت‌گیرانه و normalized در E اثر normalization را نشان می‌دهد؛ آن را به‌تنهایی خطای بصری نمی‌نامیم. مراجع: `engines.py:257–382`، `pipeline.py:238–249`؛ `tests/test_accuracy.py`.

#### Q21. فیلدهای گمرکی اختصاصی

**NOT_IMPLEMENTED** برای شماره کانتینر، بارنامه، HS، وزن، مبلغ و تاریخ با معنای تجاری. `IDENTIFIER_RE` فقط candidateهای شبیه کد/تاریخ/عدد را برمی‌گرداند. `_recover_header_numbers` فقط کنار label عمومی تاریخ/شماره، در بالای صفحه، برش را از تصویر بازخوانی می‌کند؛ این schema فیلد یا validator تقویم نیست. `engines.py:186–197`، `pipeline.py:720,792`.

#### Q22. تمام خانواده‌های تصحیح و جایگزینی

| محل | تغییر یا انتخاب واقعی | مراجعه به پیکسل / حفظ منشأ |
|---|---|---|
| `engines.normalize_text/display_text/en_digits_only` | mapping حروف فوق، ارقام، comma و فشرده‌سازی فاصلهٔ افقی | بدون تصویر؛ raw باید جدا بماند. |
| `engines.select_consensus` | انتخاب خوانش براساس متن normalized، تعداد رأی و confidence؛ اختلاف، flag سطح نتیجه | همان موتور در مقیاس‌های متفاوت؛ استقلال آماری اثبات نشده است. |
| `engines.merge_word_lists` | حذف/انتخاب کلمات هم‌پوشان | هندسه و متن؛ ممکن است یکی از خوانش‌ها از متن نهایی حذف شود. |
| `_order_bidi_runs/words_to_lines` | ترتیب مکانی، برعکس‌کردن جزیرهٔ LTR، اتصال hyphen | بدون بازخوانی تصویر؛ تبدیل ترتیب/فاصله است. |
| `_garbage_token/_replaceable_token/merge_latin_secondary/prefer_latin_region` | صفرهای تکراری `[0۰]{3,}`، حروف لام/الف، اختلاط `[۰-۹][0-9]`، punctuation میانی و خوانش eng | گذر eng از همان crop؛ برخی branchها `needs_review` را حذف می‌کنند؛ raw اغلب ثبت می‌شود، نه همیشه همهٔ alternatives. |
| `_repair_suspicious_words/_confirmed_crop_read` | پرکردن `*` یا U+FFFD با الگوی سازگار و دو خوانش/گذر glyph | از تصویر؛ raw_ocr_text و needs_review نگه داشته می‌شود. |
| `_glyph_digit_ocr/_script_digit_read/_is_slash` | whitelist، تحلیل CC و شکل صفر، تفکیک ۳/۴، slash و خوانش مدل Arabic | از پیکسل؛ قاعدهٔ هندسی همچنان می‌تواند خطا کند. |
| `_verify_text_numbers` | فقط برخی رشته‌های فارسی ۲/۳ یا سال‌مانند، بودجهٔ ۱۶ مورد | بازخوانی crop؛ تغییر مقدار و raw_ocr_text و flag؛ همهٔ عددها پوشش ندارند. |
| `_recover_header_numbers` | شش مورد نزدیک label، متن شبیه عدد یا ۱–۴ حرف فارسی، بازسازی رشتهٔ slashدار | بازخوانی تصویر؛ بدون تقویم یا مقدار مورد انتظار. |
| `_cell_ocr_best/_ocr_table` | scale voting، split خطوط، انتخاب eng عددی، جایگزینی کامل/بخشی رقم در مواضع confirmed | از crop؛ raw_text جدول ممکن است خوانش منتخب باشد؛ raw_ocr_text جدا مهم است. |
| W `precision.canonical/reconcile/_local_read/enhance_page` | همسان‌سازی مقایسه، توافق دو موتور، جایگزینی یک numeric substring در متن سطر | از تصویر؛ candidates، pre_precision_text و review_units؛ آزمون واقعی موتور دوم انجام نشد. |
| W `review.save_document` | اعمال متن انسانی پس از display_text و وضعیت confirmed | ورودی انسان، نه تصحیح زبانی؛ اصل و نسخه‌ها حفظ می‌شوند. |
| P candidate `choose_observation/normalize_search` | انتخاب observation کامل براساس پوشش، اعداد و confidence؛ نسخهٔ جست‌وجو جدا | alternatives و review_required؛ مسیر فعال نیست. |

Regexهای `validate_text` تنها `arabic_variants_present`، `suspicious_punctuation`، `trailing_separator` و `possible_isolated_letters` را تشخیص می‌دهند؛ چیزی را اصلاح نمی‌کنند. Regexهای identifier نیز validator نیستند. **لغت‌نامهٔ تصحیح معنایی، fuzzy spell correction یا حدس LLM یافت نشد.** انتخاب نزدیک‌ترین متن در محاسبهٔ distance/line coverage evaluator فقط سنجش است و متن OCR را عوض نمی‌کند. قواعد کامل قابل اجرا در فایل‌های نام‌برده حفظ شده‌اند؛ هیچ شناسه‌ای در این ممیزی اصلاح نشده است.

#### Q23. LLM و JSON

خیر. JSON با `json.dumps` از کلمات، جدول و metadata ساخته می‌شود. SDK یا inference مدل زبانی برای OCR→JSON در مسیرهای بررسی‌شده یافت نشد. ریسک حدس LLM در این پیاده‌سازی مطرح نیست؛ خطای انتخاب خوانش، normalization و قواعد پیکسلی همچنان وجود دارد.

#### پوشش واقعی انواع سند

در همهٔ ردیف‌های زیر، تشخیص نوع و **استخراج معنایی فیلدهای اختصاصی `NOT_IMPLEMENTED`** است. فهرست فیلدها نیاز پیشنهادی برای مرحلهٔ بعد است، نه ادعای قابلیت موجود.

| نوع سند | فیلدهای لازم برای تعریف schema در آینده | وضعیت فعلی |
|---|---|---|
| اظهارنامه و ترخیص | نوع و شماره اظهارنامه، گمرک، تاریخ، رویه، طرف‌ها، اقلام و HS | OCR عمومی و جدول خط‌دار؛ schema/classifier ندارد. |
| Commercial / Proforma Invoice | نوع Invoice، شماره، تاریخ، فروشنده/خریدار، ارز، شرح/تعداد/واحد/قیمت/تخفیف/مبلغ | تفکیک Proforma از Invoice، محاسبه و نگاشت ردیف ندارد. |
| Packing List | تعداد بسته، نوع بسته، اقلام، وزن خالص/ناخالص، واحد، ابعاد | ارتباط معنایی ردیف و جمع وزن ندارد. |
| CMR و سایر بارنامه‌ها | نوع بارنامه، شماره، حمل‌کننده، فرستنده/گیرنده، مسیر، بسته، وزن | parser مخصوص نوع شناسه یا قالب حمل ندارد. |
| Certificate of Origin | شماره، تاریخ، مرجع صادرکننده، کشور مبدأ، اقلام | استخراج عمومی؛ تطبیق با Invoice ندارد. |
| کانتینر و حمل | شماره، ISO category/check digit، seal، سفر/وسیله، بارنامه | فقط regex candidate عمومی؛ اعتبارسنجی ISO ندارد. |
| بانکی، بیمه، پرداخت | شماره/تاریخ مرجع، بانک/بیمه‌گر، بیمه‌نامه، ارز/مبلغ/ذی‌نفع | schema، validator و reconciliation ندارد. |
| قانونی و بخشنامهٔ چندصفحه‌ای | شماره، تاریخ، مرجع، موضوع، بند/تبصره، ارجاع و ترتیب صفحات | حلقهٔ چندصفحه‌ای هست؛ استخراج سلسله‌مراتب حقوقی و اتصال بندها نیست. |

برای هر هشت نوع: روش موجود morphology + OCR + Regex عمومی است؛ template مختصاتی یا ML classifier اختصاصی ندارد. شبکهٔ جدول row/column را نگه می‌دارد، اما صحت پیوند ردیف با دادهٔ واقعی **NOT_MEASURED**؛ merged cell و جدول بدون خط حل نشده‌اند. تطبیق چند سند یک پرونده و مختصات فیلد معنایی **NOT_IMPLEMENTED**. مختصات کلمه/سلول جای مختصات فیلد نیست. W وضعیت needs_review/confirmed در واحد بازبینی دارد؛ P candidate verified_text/review_status دارد؛ ماشین حالت `missing/uncertain/validated` برای فیلد تجاری وجود ندارد. P legacy به‌صراحت human_verified=False می‌دهد.

#### اعتبارسنجی مقادیر حساس

| موضوع | وضعیت واقعی | رد / هشدار |
|---|---|---|
| ISO 6346 و check digit | `NOT_IMPLEMENTED`؛ regex حتی طول‌های گسترده‌تر و حروف category نامعتبر را می‌پذیرد. | عدد کنترلی محاسبه یا رد نمی‌شود. |
| HS و طول متناسب کشور/سند | `NOT_IMPLEMENTED`؛ country/context schema ندارد. | هیچ رد دامنه‌ای ندارد. |
| بارنامه و شناسهٔ گمرکی | `NOT_IMPLEMENTED`؛ فقط regex شبیه شناسه. | candidate متن، نه اعتبار. |
| شمسی/میلادی | `NOT_IMPLEMENTED` برای تشخیص تقویم، leap day، range و تبدیل معتبر. | slashداربودن تاریخ را معتبر نمی‌کند. |
| وزن خالص/ناخالص و واحد | `NOT_IMPLEMENTED`؛ حتی net≤gross بررسی نمی‌شود. | هشدار قاعدهٔ تجاری ندارد. |
| بسته/تعداد/واحد | `NOT_IMPLEMENTED` برای integer/range/unit معنایی. | خروجی رشته‌ای است. |
| ارز/اعشار/مبلغ/جمع | `NOT_IMPLEMENTED`؛ normalization رقم و comma معادل Decimal parsing نیست. | مبلغ ناسازگار رد نمی‌شود. |
| quantity×unit_price، تخفیف و هزینه | `NOT_IMPLEMENTED` | حسابداری یا tolerance وجود ندارد. |
| تطبیق Invoice/Packing/بارنامه | `NOT_IMPLEMENTED` | batch چندفایلی پروندهٔ تطبیق‌پذیر نیست. |

اعتبارسنجی فعلی عمدتاً confidence، اختلاف خوانش، برخی ارقام از پیکسل و punctuation است؛ اغلب فقط هشدار می‌دهد و download مسدود نمی‌شود. raw_text ناحیه و raw_ocr_text اصلاحات و raw سلول‌ها وجود دارند، ولی تمام خوانش‌های تمام scaleها با منشأ کامل ذخیره نمی‌شوند؛ raw_text هم متن کامل جدول و همهٔ خطاهای حذف را پوشش نمی‌دهد. W UI بازبینی اختیاری دارد؛ P review اصلی به snapshot candidate/compare وابسته است. gate الزامی تأیید فیلد حساس پیش از مصرف تجاری وجود ندارد. **ذخیره یا دریافت خروجی غلط بدون هشدار خاصِ آن فیلد ممکن است؛ ثبت خودکار فیلد در دیتابیس از این سورس قابل اثبات نیست و اصلاً این لایه پیاده نشده است.**

### D. Root Cause Analysis

| شناسه / اولویت | علت ریشه‌ای و اثر | شواهد و میزان قطعیت |
|---|---|---|
| R01 / P0 | معیار قدیمی WER روی فاصلهٔ نویسه‌ای رشتهٔ joinشده تقسیم بر تعداد واژه است؛ CER فاصله‌ها را حذف می‌کند؛ Numeric فقط عضویت token است. نتیجهٔ quality baseline قدیمی قابل اتکا نیست. | `benchmark.py:20,28,42` در W/P؛ diagnostics: یک جایگزینی واژه WER=2 به‌جای 0.5؛ حذف فاصله CER=0؛ حذف تکرار و وارونگی اعداد Numeric=1. قطعی. |
| R02 / P0 | مصرف mandatory `openpyxl` بدون نصب؛ W پس از OCR جدول، کل صفحه را failed می‌کند؛ P فقدان XLSX را می‌بلعد. | W `tables.py:170,182`، P `tables.py:195–200` و requirements؛ ۸ شکست تست W و اجرای واقعی جدول. قطعی. |
| R03 / P0 | `select_consensus` اختلاف را روی result می‌گذارد ولی caller متن آن را به word منتقل نمی‌کند؛ برخی Latin repairها flag قبلی را حذف می‌کنند. | `engines.py:111,470`، `pipeline.py:158,230`؛ diagnostics caller با دو عدد متفاوت و confidence .99، zero flagged word می‌دهد؛ raw حفظ می‌شود اما هشدار از دست می‌رود. قطعی در W، منطق مشابه در P. |
| R04 / P0 | validator تجاری و gate فیلد حساس وجود ندارد؛ `numeric_verified` تنها نام یک بازخوانی OCR است. | Q21 و جدول اعتبارسنجی؛ عدد اشتباهِ پرconfidence یا دو موتور هم‌خطا می‌تواند عبور کند. فقدان قابلیت قطعی؛ نرخ وقوع واقعی NOT_MEASURED. |
| R05 / P1 | انتخاب preprocessing سراسری با سفیدی میانگین صفحه و تحلیل کوچک، کیفیت ناحیهٔ متن کم‌رنگ را تضمین نمی‌کند؛ OSD/dewarp فعال نیست. | lowq مصنوعی، CER نزدیک ۵۰٪، با `preprocess_ops=[]`؛ اصل scan قبلاً کاهش نمونه‌برداری یافته و بزرگ‌سازی جزئیات تولید نمی‌کند. دخالت هر عامل نیاز به ablation دارد؛ نسبت‌دادن همهٔ خطا به مدل یا فیلتر مجاز نیست. |
| R06 / P1 | قاعدهٔ اندازهٔ ثابت CC و پاک‌سازی حاشیهٔ جدول، علامت‌های کوچک را با noise/rule اشتباه می‌گیرد؛ deskew legacy بوم ثابت دارد. | Q13 و تست چهارپیکسلی؛ مکانیزم قطعی، سهم در خطای واقعی UNKNOWN. |
| R07 / P1 | فراخوانی OCR برای تمام PDFها، master بسیار بزرگ و چندین scale/retry؛ بعداً همان تصویر کوچک می‌شود. | render/master_read بخش بزرگی از زمان؛ G. نبود text-first routing قطعی؛ سود کمی گزینه D روی اسناد واقعی NOT_TESTED. |
| R08 / P1 | بازسازی BiDi و normalization جهانی به متن، ارقام و شناسه اعمال می‌شود؛ raw/نمایش/مقدار canonical قرارداد جدا ندارند. | mapping حروف دارای همزه، Persianization شناسه، اتصال hyphen؛ Q18–Q22. عامل رشته‌ای قطعی، نسبت خطای نموداری/معنایی نیازمند GT مکانی است. |
| R09 / P1 | baseline/README/ابزارهای قدیمی از ساختار جدید خروجی عقب‌اند؛ `table_001.json` می‌خوانند ولی خروجی جدید `page_001_table_001.json` است. | `run_benchmark.py:report` و `ocr_bot.py:run_benchmark`؛ همچنین report قدیمی VERIFIEDهای ثابت، all روی مجموعهٔ خالی و میانگین با مخرج ثابت دارد. قطعی؛ مسیر قدیمی برای تأیید انتشار استفاده نشد. |
| R10 / P1 | شبکهٔ جدول فقط خط‌دار و span=1؛ ترتیب/همبستگی تجاری ردیف‌ها فهمیده نمی‌شود. | table model و baseline مصنوعی ۲۰ سلولی؛ C/E. محدودیت طراحی قطعی. |
| R11 / P1 | release P از W جداست؛ P اصلاحات render/quality/storage دارد که W ندارد؛ W موتور دوم و review دیگری دارد که P ندارد. | hashهای source و inspection دو ریشه؛ جایگزینی مستقیم W می‌تواند قابلیت‌های فعال را از بین ببرد. قطعی. |
| R12 / P1 | حافظه/CPU واقعی کل درخت و هزینهٔ مرحلهٔ precision در متریک استاندارد کامل ثبت نمی‌شود؛ total counter با actual native calls یکی نیست. | `metrics.py` فقط process جاری را می‌سنجد؛ W `m.report()` پیش از enhance_page monitor را متوقف می‌کند؛ G. قطعی. |

هشدار مکمل W: `tables.cell_confidence` confidence صفر را با truthiness حذف می‌کند؛ ورودی `[0,1]` نتیجهٔ `1` می‌دهد. `precision.reconcile` برای دو موتورِ هم‌متن حتی با confidence .01 می‌تواند needs_review=False بدهد؛ توافق، اثبات صحت یا calibration نیست. هر دو در diagnostics بازتولید شدند. هیچ‌کدام نرخ دقت مدل دوم را اندازه نمی‌گیرند.

### E. OCR Quality Baseline

#### داده و روش

چهار PDFِ موجود `input/bench_{persian,mixed,lowq,table}.pdf`، هرکدام یک صفحه، با `input/ground_truth.json` استفاده شدند. متن مرجع با constantهای `make_bench_docs.py` و جدول ۵×۴ تطبیق داده شد. این اسناد **مصنوعی‌اند**؛ عنوان «سند واقعی» در متن بعضی ابزارها طبقه‌بندی درستِ این دیتاست نیست. ابعاد تصویر و ساختار هر PDF مطابق generator، بدون لایهٔ متن و پیوست بودند. اسناد اصلی فقط read شدند و SHA-256 پیش/پس برابر ماند. lowq همان متن فارسی با degradation است؛ چهار نمونه مستقل جمعیتی نیستند.

آزمون‌ها مستقیماً روی API فایل محلیِ همان pipeline، `legacy`، ۲۰۰۰ DPI، `force_tesseract=True`، `OMP_THREAD_LIMIT=2` و بدون debug image اجرا شدند؛ pipeline اصلاح یا mock نشد. تمام خروجی‌های سند در TemporaryDirectory جدید ساخته و حذف شدند. supervise فقط فرایندهای همین آزمایش را با سقف ۸ GiB/۶۰۰ ثانیه برای هر سند محدود کرد. رندر ۲۰۰۰ DPI و گذرهای OCR واقعی اجرا شدند؛ exporter جدول W نیز عمداً دور زده نشد. کد P در فرایند تازه با root همان release import شد؛ سرویس فعال فراخوانی پردازشی نشد.

محاسبات مستقل ابزار ممیزی:

- CER سخت‌گیرانه = فاصلهٔ ویرایشی نویسه‌ها / تعداد نویسهٔ مرجع، با احتساب فاصله؛ WER = فاصلهٔ ویرایشی **دنبالهٔ واژه‌ها** / تعداد واژهٔ مرجع.
- normalized فقط NFC، تبدیل‌های `display_text`، حذف کشیده و همسان‌کردن فاصله/نیم‌فاصله است؛ spelling، ترتیب، مقدار عدد و صفر ابتدایی اصلاح نمی‌شود. این نمایه برای شکل نگارش است؛ **معیار سخت‌گیرانه جدا باقی می‌ماند**، چون mapping حروف همیشه بی‌اثر معنایی نیست.
- Numeric در این گزارش multiset token با همسان‌سازی شکل ارقام است و تکرار حذف‌شده را پنهان نمی‌کند؛ جای جداکننده‌ها و نوع comma/اعشار حفظ می‌شود. بنابراین عدم تطبیق token الزاماً تغییر مقدار حسابی نیست و می‌تواند ناشی از separator باشد. این معیار Field Exact Match یا سنجش اتصال عدد به label نیست. `identifier_exact_matches` فقط Regex candidates مرجع و حضور contiguous همان رشته با مرز کنترل‌شده است؛ validator نوع شناسه نیست.
- نتیجهٔ جدول positional cell/row/column/shape است؛ تشخیص span و صحت معنایی ردیف را اندازه نمی‌گیرد.

نتیجهٔ نهاییِ اجرای کنترل‌شده، از [baseline_working_verified.json](evidence/baseline_working_verified.json) و [baseline_deployed_verified.json](evidence/baseline_deployed_verified.json):

| نسخه / نمونه / زبان و کیفیت | CER سخت‌گیرانه | WER سخت‌گیرانه | CER normalized | WER normalized | زمان pipeline، ثانیه | failed page |
|---|---:|---:|---:|---:|---:|---:|
| W / persian / فارسی، تمیز | ۴٫۹۰٪ | ۸٫۷۵٪ | ۴٫۶۸٪ | ۷٫۳۲٪ | ۲۴٫۸۰۰ | ۰ |
| W / mixed / فارسی و انگلیسی، تمیز | ۱۸٫۸۱٪ | ۴۳٫۳۳٪ | ۴٫۵۹٪ | ۲۹٫۰۳٪ | ۲۰٫۸۱۹ | ۰ |
| W / lowq / فارسی، degraded | ۵۲٫۱۲٪ | ۶۵٫۰۰٪ | ۵۱٫۶۷٪ | ۶۴٫۶۳٪ | ۴۴٫۷۲۰ | ۰ |
| W / table / عنوان فارسی و جدول | ۱۰۰٪، شکست export | ۱۰۰٪، شکست export | ۱۰۰٪ | ۱۰۰٪ | ۲۳٫۵۸۵ | ۱ |
| P / persian / فارسی، تمیز | ۵٫۳۵٪ | ۸٫۷۵٪ | ۵٫۱۲٪ | ۷٫۳۲٪ | ۲۵٫۴۱۹ | ۰ |
| P / mixed / فارسی و انگلیسی، تمیز | ۲۶٫۶۱٪ | ۵۰٫۰۰٪ | ۱۳٫۳۰٪ | ۳۸٫۷۱٪ | ۲۲٫۷۹۵ | ۰ |
| P / lowq / فارسی، degraded | ۴۹٫۸۹٪ | ۶۸٫۷۵٪ | ۴۹٫۲۲٪ | ۶۷٫۰۷٪ | ۴۳٫۹۸۰ | ۰ |
| P / table / فقط متن عنوان خارج جدول | ۰٪ | ۰٪ | ۰٪ | ۰٪ | ۲۸٫۴۰۲ | ۰ |

صفر CER/WER ردیف آخر فقط برای **عنوان ۲۳نویسه‌ای خارج جدول** است؛ متن جدول در سنجهٔ سلول جدا ارزیابی شده و کاملاً صحیح نیست. هر ردیف n=1 صفحه دارد. CER/WER می‌تواند با درج‌های زیاد از ۱ بیشتر شود؛ clamp به ۱ برای متن موفق انجام نشد.

| نسخه / نمونه | token عدد صحیح / مرجع | token عدد اضافی | Regex identifier EM / مرجع | واژهٔ flagged / خروجی |
|---|---:|---:|---:|---:|
| W / persian | ۱/۵ | ۵ | ۱/۳ | ۷/۸۰، ۸٫۷۵٪ |
| W / mixed | ۵/۵ | ۱ | ۵/۵ | ۵/۳۰، ۱۶٫۶۷٪ |
| W / lowq | ۱/۵ | ۸ | ۱/۳ | ۱۸/۹۶، ۱۸٫۷۵٪ |
| P / persian | ۱/۵ | ۳ | ۱/۳ | ۶/۸۰، ۷٫۵۰٪ |
| P / mixed | ۵/۵ | ۳ | ۵/۵ | ۵/۳۰، ۱۶٫۶۷٪ |
| P / lowq | ۱/۵ | ۱۰ | ۱/۳ | ۱۴/۹۱، ۱۵٫۳۸٪ |

نمونهٔ mixed نشان می‌دهد «همهٔ tokenهای مرجع پیدا شدند» با نبود false positive یکی نیست. سنجهٔ identifiers ممکن است تاریخ/عدد هم داشته باشد؛ ۵/۵ اثبات پنج فیلد گمرکی معتبر نیست. برای عنوان جدول token عددی مرجع وجود ندارد و Exact Match عددی `NOT_APPLICABLE` است. در P table، quality هشت flagged item و صفر flagged word از چهار واژهٔ عنوان نشان می‌دهد؛ این هشدارهای جدول با word rate مخلوط نشدند.

| جدول ۵×۴ | Cell EM | Row EM | Column EM | شکل شبکه | دامنهٔ نتیجه |
|---|---:|---:|---:|---:|---|
| W | ۱۶/۲۰، ۸۰٪ | ۲/۵، ۴۰٪ | ۱/۴، ۲۵٪ | ۵×۴ صحیح | فقط artifact پیش از شکست export؛ نتیجهٔ نهایی صفحه ناموفق |
| P | ۱۶/۲۰، ۸۰٪ | ۲/۵، ۴۰٪ | ۲/۴، ۵۰٪ | ۵×۴ صحیح | export JSON/CSV/HTML؛ XLSX به علت وابستگی غایب ساخته نشد |

مقایسهٔ مرحله‌ایِ متن خام ناحیه با متن نهایی: روی persian، CER normalized خام W حدود ۱٫۷۸٪ و نهایی ۴٫۶۸٪؛ P خام ۲٫۲۳٪ و نهایی ۵٫۱۲٪ است. پس تمام خطا را نمی‌توان به recognizer اولیه نسبت داد؛ انتخاب، بازخوانی، ترکیب و ترتیب نیز باید بررسی شوند. در mixed و lowq همین مراحل مجموعاً CER را بهبود می‌دهند. raw و نهایی از نظر قطعه‌بندی یکسان نیستند؛ این سنجه به‌تنهایی تعیین نمی‌کند کدام تابع مقصر هر کاراکتر است.

یک بازاجرای تشخیصی Persian در [stage_changes_synthetic.json](evidence/stage_changes_synthetic.json) همان CER را بازتولید کرد و تغییر مقدار عددیِ دارای تفاوت `raw_ocr_text→text` در واژه‌های نهایی نداشت. بنابراین افت متن نهایی این نمونه را نمی‌توان با شواهد حاضر به «اصلاح عددی مشخص» نسبت داد؛ تفاوت TXT موتور، HOCR، ترتیب و ترکیب باید در ablation مرحله‌ای جدا شود.

برای جدول W، خطای export به خروجی نهایی صفحه منجر نمی‌شود؛ CER/WER=1 اگر متن خالیِ نتیجهٔ failed سنجیده شود، **خطای end-to-end** است، نه ۱۰۰٪ خطای recognizer. امتیاز جدولِ artifact پیش از شکست export با scope=pre_export_only جدا گزارش می‌شود و موفقیت pipeline نیست. برای P، اگر metadata صفحه cells را درون خود نداشت، JSON واقعی export جدول خوانده شد؛ سند به‌صورت مصنوعی تکمیل نشد.

#### معیارهای اندازه‌گیری‌نشده

| معیار | وضعیت و علت |
|---|---|
| CER/WER واقعی هر خانواده سند گمرکی | `NOT_MEASURED`؛ GT نمایندهٔ واقعی/بی‌نام‌سازی‌شده در اختیار این آزمون نبود. |
| Field Exact Match | `NOT_MEASURED`؛ extractor معنایی و label/GT فیلد وجود ندارد. |
| دقت شماره کانتینر/HS/وزن/مبلغ به تفکیک فیلد | `NOT_MEASURED`؛ tokenهای مصنوعی جای dataset فیلد نیستند. |
| نرخ فیلدهای حساس نیازمند بازبینی | `NOT_MEASURED`؛ فقط نرخ word flag قابل شمارش است، نه field rate. |
| خطای حساس تأییدشده بدون هشدار | `NOT_MEASURED`؛ تعریف confirmation تجاری و GT فیلد نداریم؛ نقص R03 اثبات امکان است، نه نرخ. |
| دقت table detection روی مجموعهٔ نماینده | `NOT_MEASURED`؛ یک grid نمونه برای precision/recall کافی نیست. |
| PDF متن‌دار/ترکیبی/خراب، عکس زاویه‌دار، انگلیسی تنها | `NOT_MEASURED` در baseline؛ unit test با benchmark جمعیتی فرق دارد. |
| P50/P95 معتبر برای هر نوع/زبان/کیفیت | `NOT_MEASURED`؛ هر دسته n=1 و نمونه‌ها تکرارپذیری/بار نماینده ندارند. |
| Paddle، EasyOCR و معماری D/E | `NOT_TESTED` به‌عنوان benchmark موتور/معماری مستقل. |

هیچ عدد از README به‌عنوان نتیجهٔ این اجرا کپی نشده است. `tools/evaluate_accuracy.py` جدید W، WER صحیح token-level، provenance، پنج صفحه یکتا، exact critical و جدول دارد، ولی **اجرای acceptance پنج‌صفحه‌ای انجام نشد**: مدل دوم نصب نیست و مرجع خصوصیِ مجاز و مسیرهای آن در این مأموریت تحویل نشده‌اند. fixtureهای خود `tests/test_acceptance.py` آزمونِ evaluator هستند، نه تأیید کیفیت OCR واقعی.

#### آزمون‌های موجود و نتیجهٔ validation

۱۵۴ تست W اجرا شد: **۱۴۶ موفق، ۷ error و ۱ failure، بدون skip**. هفت error از نبود `openpyxl` و failure route بازبینی از 500 ناشی از همان وابستگی است؛ نام همه در [tests.json](evidence/tests.json) ثبت است. شکست‌ها از قبلِ ممیزی وجود داشتند؛ کد محصول تغییر نکرد. تست‌های موتور بومی واقعی، ارقام فارسی/Arabic، ownership/CSRF/path traversal، process guard، cache، geometry، budget، جلوگیری از formula/HTML و provenance در موارد مستقل از Excel گذشتند. این موفقیت‌ها مساوی امنیت مطلق یا پذیرش تولید نیستند.

AST برای ۲۷ فایل Python برنامه/ابزار ممیزی معتبر بود. Ruff روی ابزار افزوده‌شده موفق شد؛ روی سورس/تست‌های موجود **۳۸ مورد قبلی** گزارش کرد. `python -m pip check` موفق بود؛ این ابزار وابستگی declared اما نصب‌نشدهٔ یک پروژهٔ غیرنصب‌شده را الزاماً تشخیص نمی‌دهد. typechecker یا build مستقل در مخزن پیکربندی نشده؛ static typecheck و build بسته `NOT_RUN / NOT_CONFIGURED`، نه «موفق». بسته/مدل برای سبزکردن آزمون نصب نشد. suite نسخهٔ P جداگانه اجرا نشد؛ benchmark P و بررسی سورس آن از نتیجهٔ suite W تفکیک شده‌اند.

برای بازتولید از ریشهٔ مخزن، با نام خروجی تازه و بدون تغییر محیط عملیاتی:

```powershell
python -X utf8 docs/ocr/audit_diagnostics.py diagnostics --output docs/ocr/evidence/reproduced-diagnostics.json
python -X utf8 docs/ocr/audit_diagnostics.py tests --output docs/ocr/evidence/reproduced-tests.json
python -X utf8 docs/ocr/audit_diagnostics.py baseline --output docs/ocr/evidence/reproduced-working.json
python -X utf8 docs/ocr/audit_diagnostics.py baseline --release-root "E:\GomrokOCR\releases\52e15ac8ac125993cba5974cdf3d3249bf9524c1" --output docs/ocr/evidence/reproduced-deployed.json
python -m ruff check docs/ocr/audit_diagnostics.py
python -m pip check
```

حالت `benchmark` ابزار، worker داخلیِ supervisor است و در Windows عمداً بدون containment مستقل اجرا نمی‌شود؛ برای تک‌نمونه از `baseline --case bench_persian` استفاده شود. harness، engine محلی و اسناد synthetic موجود را لازم دارد؛ دانلود یا نصب انجام نمی‌دهد. اجرای اولیهٔ ابزار ممیزی یک race در مشاهدهٔ خروج PID و ناسازگاری خواندن metadata جدول P را آشکار کرد؛ ابزار اصلاح و هر هشت مورد به‌صورت متوالی از ابتدا بازاجرا شدند. evidenceهای ناموفق اولیه کنار گذاشته شدند؛ اعداد جدول‌ها فقط اجرای `verified` نهایی‌اند. این پسوند بیانگر کامل‌شدن شواهد اجراست، نه قبولی کیفیت یا exporter.

#### دیتاست لازم برای تصمیم بعدی

یک corpus محلی و مجاز با دست‌کم یک pilot حدود ۲۰۰ صفحه از هشت خانواده، حدود ۲۰–۳۰ صفحه در هر خانواده و پوشش فارسی، انگلیسی و mixed پیشنهاد می‌شود؛ این **پیشنهاد طراحی** است، نه حد نمونهٔ آماری تضمین‌شده. native PDF، scan تمیز/کم‌کیفیت، متن ریز، چندستون، جدول بدون خط/ادغام‌شده، چرخش/سایه و لایهٔ متن مخدوش جدا label شوند. فایل‌های یک پرونده و نسخهٔ degraded همان صفحه در splitهای train/tune/held-out جدا نیفتند. حداقل دو annotator و داوری اختلاف برای متن منطقی، glyph حساس، bbox، label، واحد، تقویم، row/column/span، خواناناپذیری و ارتباط cross-document لازم است. دادهٔ قابل‌خواندن باید از ناموجود و ناخوانا جدا باشد؛ مقدار ناشناخته نباید با حدس تکمیل شود.

CER/WER micro و macro همراه تعداد نمونه، Field EM شامل صفر/اعشار/علامت، جدول positional، دقت تشخیص uncertainty، false-accept حساس و review workload با denominator روشن سنجیده شوند. برای performance، cold/warm start و سند چندصفحه‌ای و concurrency هدف با نمونه‌های کافی و بار کنترل‌شده لازم است؛ P50/P95 فقط با اعلام n و توزیع/بازهٔ عدم قطعیت گزارش شوند. corpus محرمانه و GT آن خارج Git و web root بماند؛ گزارش قابل‌اشتراک فقط aggregate و مثال مصنوعی داشته باشد.

### F. Security & Offline Execution

#### مسیرهای خروج، اعتماد و ریسک

| شناسه / شدت | دامنه و محل | شواهد / اثر | اصلاح پیشنهادی، بدون اعمال در این ممیزی |
|---|---|---|---|
| S01 / بالا، مشروط به config | W `engines.GoogleVisionEngine` و `pipeline._process_document` | امکان فرستادن تصویر سند با روشن‌شدن گزینهٔ غیرمحلی؛ default خاموش و P فاقد این کلاس است. ارسال واقعی در این ممیزی انجام نشد. | ممنوعیت سخت egress برای مسیر سند، حذف/رد شاخهٔ cloud در محصول و تست fail-closed؛ از بازگرداندن این قابلیت هنگام reconcile نسخه‌ها جلوگیری شود. |
| S02 / متوسط | W/P `TesseractEngine._ocr_subprocess` Docker fallback | `docker run` بدون no-network/pull policy/سقف منابع؛ Docker daemon/DOCKER_HOST قابل انتقال به زیرساخت دیگر است. در این میزبان native استفاده شد. | فقط image از پیش نصب با digest، daemon محلی تأییدشده، `--pull=never --network=none` و budget؛ یا نبود backend را صریحاً fail کند. |
| S03 / متوسط تا بالا در ورودی مهاجم | W/P مسیر render/layout وب و fallback CLI | PDF هنگام upload در فرایند وب parse می‌شود؛ raster سقف دارد اما زمان کل legacy، CC labelهای بزرگ، تعداد cellها و fallbackهای ۹۰۰ ثانیه‌ای سقف job واحد ندارند. چهارفرایندی‌بودن جای محدودیت حافظه/زمان کل نیست. | parse و OCR محصور، سقف تعداد/پیکسل/زمان/خروجی و deadline سند؛ queue پایدار و cancel امن. بدون exploit عملیاتی. |
| S04 / متوسط | W `pipeline.py:1068`، `engines.py:808`؛ P exception logger مشابه | exception traceback می‌تواند مسیر خصوصی یا stderr ابزار native را وارد log کند؛ تضمین sanitization همهٔ parserها نیست. log واقعی کاربران خوانده نشد. | logger با category و correlation id، حذف متن/filename خارجی، ACL/retention و تست redaction. |
| S05 / متوسط | مسترها، upload، JSON/TXT/CSV/XLSX، ZIP و reviewها | فاقد retention/حذف زمان‌بندی‌شدهٔ upload/master/export در مسیر اصلی؛ نسخه‌ها عمداً حفظ می‌شوند. خطای فرایند ممکن است tempfile باقی بگذارد. اثر امنیتی: تکثیر متن محرمانه و پرشدن دیسک. | سیاست مصوب retention/quota/backup و cleanup فقط artifactهای منقضی و متعلق به job؛ اصل کاربر بدون مجوز حذف نشود. |
| S06 / متوسط، رگرسیون محتمل W | W `web_app.py:48` و `serve.py` | default داده داخل checkout و tempfile سیستم؛ P برعکس `configure_storage` مسیر خصوصی صریح و TEMP خصوصی enforce می‌کند. تنظیم P فعلی خصوصی است. | پیش‌شرط P برای storage در reconcile حفظ شود؛ پوشهٔ اشتراکی و backup خارج IIS/Git و ACL حداقل. |
| S07 / متوسط | W `PaddleEngine` و lock آن | مدل‌ها نصب نیستند؛ downloader SDKها در dependency graph تعریف شده‌اند. local model_dir+hash+guard خوب است، اما monkeypatch socket فایروال native نیست. | نصب کنترل‌شدهٔ کاملاً offline، manifest و hash ثابت، OS egress deny و runtime smoke مستقل با فایل مصنوعی؛ مدل ناقص باید fail شود. |
| S08 / متوسط، پایداری/محرمانگی نشست | session و JOBS | session مالک، login/نقش هویتی گمرکی نیست؛ possession cookie اختیار فایل است. W کلید تصادفی default و secure-cookie به config وابسته است؛ P با کلید پایدار و Secure=1 فعال است. TTL نشست ۲۴ ساعت جای retention فایل نیست. | ثبت نیاز هویتی/SSO در صورت الزام، کلید پایدار خصوصی، revoke/retention، بازیابی مالکیت مصوب؛ عدم افزودن auth حدسی در این ممیزی. |
| S09 / پایین تا متوسط | HTML/CSV/XLSX/UI | W/P HTML escape و XLSX متن‌بودن سلول و CSV guard دارند؛ UI از textContent/textarea استفاده می‌کند. تست‌های مستقل از exporter گذشتند. متن خام TXT/JSON هنگام مصرف downstream هنوز untrusted است. | این escapeها و string typing حفظ؛ downstream هم از اجرای فرمول/HTML خودداری کند؛ Unicode controlهای نمایشی بررسی شوند. |
| S10 / UNKNOWN | vulnerabilityهای dependency، backup، فایروال و ACL جاری | SBOM/CVE audit همهٔ DLLها و پکیج‌ها، inventory backup و آزمون OS egress اجرا نشد. deploy script قصد ACL را نشان می‌دهد، نه وضعیت بالفعل همهٔ نسخه‌های backup. | inventory/version lock، CVE review آفلاین/با metadata عمومی، ممیزی ACL و فایروال بدون دسترسی به محتوای اسناد. |

ownership: W روی status/download/review/preview مالک را بررسی می‌کند؛ allowlist خروجی، resolve و containment مسیر دارد؛ CSRF همهٔ POSTها، version conflict و قفل بین‌فرایندی review وجود دارند. P روی status/download/preview/reviews مالک را چک می‌کند و preview/review semaphore دارد. شواهد P `web_app.py:475,625,652` و `review.py:53,177`؛ W `web_app.py:68,1163,1183,1206,1243` و `review.py:57,282`. IDOR قابل اثباتی در مسیرهای بررسی‌شده پیدا نشد؛ این عبارت گواه ممیزی نفوذ جامع یا مجوز دسترسی به فایل کاربر دیگر نیست.

IIS قواعد HTTPS و reverse proxy دارد؛ Waitress فقط loopback، trusted_proxy مشخص، Host validation، CSP nonce، nosniff، frame-ancestors و no-store برقرارند. Secure cookie و HSTS در W شرط config دارند؛ P Secure=1 مشاهده شد. سلامت واقعی TLS عمومی و upload/download روی دامنه در این مأموریت اجرا نشد، چون استقراری رخ نداد؛ health محلی read-only کافی برای شناسایی release بود. هیچ فایل واقعی یا مصنوعی از طریق دامنه upload نشد.

#### حکم آفلاین

- **P فعال / Tesseract بومی:** از نظر معماری اجرای کامل OCR بدون سرویس بیرونی ممکن است؛ مدل و binary حاضرند و baseline محلی واقعی اجرا شد. هیچ HTTP OCR در آن مسیر وجود ندارد. ماژول native دارای libcurl بودن، به معنی استفادهٔ شبکه در این ورودی‌های محلی نیست.
- **W legacy محلی:** `OCR_LOCAL_ONLY=1` یا force_tesseract، مدل از پیش موجود و native backend برای مسیر محلی کافی‌اند؛ fallback Docker نامحدود باید کنترل شود.
- **W maximum_local:** قابلیت طراحی برای local-only دارد، اما روی محیط حاضر **آماده نیست / NOT_TESTED**؛ بدون نصب و verify مدل/runtime، claim اجرای موفق ممنوع است.
- **آزمون با فایروال واقعی مسدودکنندهٔ خروجی:** `NOT_TESTED`؛ تنظیمات فایروال سرور تغییر نکرد. harness اتصال‌های socket پایتون را رد کرد؛ این guard شامل syscall مستقل DLL و هر subprocess نیست. پس «بدون مشاهدهٔ نیاز شبکه در اجرای محلی» با «اثبات تمام egressها در OS» فرق دارد. برای آزمون بعدی از VM/حساب ایزوله با deny outbound و حفظ دسترسی لازم UI استفاده شود.

### G. CPU Performance

#### محیط مشاهده‌شده و حدود تعمیم

Windows Server 2025 Standard، x64، hypervisor حاضر؛ پردازندهٔ نمایان Intel Xeon E5-2699 v3 @2.30GHz، ۲۴ core/logical CPU گزارش‌شده و affinity برابر ۲۴. این تعداد، تخصیص قابل مشاهدهٔ ماشین مجازی است؛ توپولوژی/فرکانس/اشتراک میزبان فیزیکی **UNKNOWN**. RAM فیزیکی نمایان ۵۰٬۳۳۱٬۰۸۶٬۸۴۸ بایت، حدود ۴۶٫۸۸ GiB؛ حافظهٔ آزاد در snapshot سیستم حدود ۱۹ GiB و فضای آزاد C حدود ۸ GiB بود و مقدار لحظه‌ای است. GPU محاسباتی به OS نمایان نبود؛ تنها Remote/Basic Display Adapter دیده شد. کانتینر برای سرویس فعال مشاهده نشد؛ quota container در این استقرار `NOT_APPLICABLE`، محدودیت hypervisor **UNKNOWN**.

فرایند فعال وب هنگام بررسی idle، health با active_jobs=0، حدود ۴۳۰ MiB RSS، ۳۰ thread سیستم‌عامل و صفر child داشت؛ این baseline idle با شش thread تنظیم‌شدهٔ Waitress متناقض نیست، چون کتابخانه‌های native thread اضافی می‌سازند. داده، temp و log فعال روی `E:\GomrokOCR\{data,temp,logs}` قرار دارند؛ فضای آزاد E هنگام بررسی نهایی حدود ۳۳٫۹ GiB بود. مسیرهای پیش‌فرض C در deploy.ps1 را نباید به استقرار فعال تعمیم داد.

benchmarkها از مسیر HTTP عملیاتی اجرا نشدند؛ از Python عمومی با نصب cold و کد P/W در فرایندهای جدا استفاده شد. فایل‌های موقت آزمون در TEMP حساب ممیزی روی C بودند، درحالی‌که temp سرویس روی E است؛ اختلاف volume و بار/حساب اجرایی، مقایسهٔ مستقیم latency عملیاتی را نامعتبر می‌کند. نسخهٔ پکیج واقعاً ازقبلبارشده در سرویس می‌تواند با metadata نصب cold فرق داشته باشد؛ Waitress نمونهٔ مشاهده‌شدهٔ این عدم قطعیت است. نتایج را SLA تولید نمی‌نامیم.

| نسخه / نمونه | wall شامل startup، ثانیه | peak RSS درخت، MiB | peak RSS داخلی والد، MiB | CPU seconds، کران پایین | core-equivalent، کران پایین |
|---|---:|---:|---:|---:|---:|
| W / persian | ۲۸٫۱۷۵ | ۸۲۷٫۲ | ۸۱۹٫۳ | ۳۸٫۴۸۴ | ۱٫۳۶۶ |
| W / mixed | ۲۲٫۹۴۵ | ۷۴۴٫۹ | ۷۸۰٫۱ | ۳۶٫۰۳۱ | ۱٫۵۷۰ |
| W / lowq | ۴۷٫۶۴۳ | ۸۵۲٫۴ | ۷۹۸٫۱ | ۷۵٫۶۵۶ | ۱٫۵۸۸ |
| W / table، export ناموفق | ۲۵٫۵۶۶ | ۷۲۰٫۶ | ۸۰۷٫۶ | ۵۳٫۵۴۷ | ۲٫۰۹۴ |
| P / persian | ۲۹٫۰۳۴ | ۸۲۲٫۹ | ۸۱۸٫۴ | ۴۴٫۰۴۷ | ۱٫۵۱۷ |
| P / mixed | ۲۴٫۹۵۰ | ۷۷۵٫۹ | ۸۱۶٫۴ | ۴۲٫۰۶۲ | ۱٫۶۸۶ |
| P / lowq | ۴۷٫۰۶۹ | ۸۴۶٫۴ | ۸۰۶٫۸ | ۷۶٫۱۲۵ | ۱٫۶۱۷ |
| P / table | ۳۱٫۰۷۴ | ۶۷۲٫۶ | ۷۷۷٫۷ | ۵۴٫۵۶۲ | ۱٫۷۵۶ |

در هر مورد حداکثر پنج child مشاهده شد: چهار worker OCR به‌علاوهٔ فرایند کمکی multiprocessing؛ این «پنج موتور OCR» نیست. فایل‌های evidence، زمان تک‌تک stageها را هم دارند؛ مثلاً render+master_read در W persian حدود ۱۶٫۴۴۵ ثانیه از total داخلی ۲۴٫۶۳ ثانیه است. W lowq با mean=238.1 و low_contrast=True و skew_angle=0 همچنان preprocessing خالی داشت؛ guard میانگین >۲۲۵ فیلترهای contrast/background را رد کرده است. این مشاهده، نیاز به تحلیل ناحیه‌ای و آزمون ablation را نشان می‌دهد، نه تضمین بهبود با روشن‌کردن یک فیلتر.

تعریف CPU ستون جدول: مجموع حداکثر زمان user+system مشاهده‌شده برای PID/create_timeهای متعلق به آزمایش، تقسیم بر wall time؛ **کران پایین نمونه‌برداری‌شدهٔ core-equivalent** است. فرایند خیلی کوتاه ممکن است دیده نشود. RSS درخت با دورهٔ ۰٫۱ ثانیه سنجیده شد؛ متریک داخلی process با دورهٔ ۰٫۰۵، بنابراین peakهای نمونه‌برداری‌شده لزوماً هم‌زمان/قابل جمع نیستند. این اعداد peak تمام سیستم، commit حافظه یا هزینهٔ همهٔ requestها نیستند.

گلوگاه‌های مستند:

1. render و read دوبارهٔ TIFF سهم قابل‌توجهی از زمان هر صفحه دارند. مستر A4 تقریباً ۳۸۷ مگاپیکسل خاکستری است، حدود ۳۶۹ MiB فقط برای pixel buffer؛ comment قدیمی ۱٫۱GB برای RGB است، درحالی‌که مسیر فعلی grayscale است. arrays، resize، labels، TIFF decoder و workerها سربار دیگری دارند.
2. `MasterPage.read_region` برخلاف docstring **کل TIFF را read و decode می‌کند** و بعد slice می‌گیرد؛ region streaming واقعی نیست. مسیر اصلی معمولاً read_full و crop از RAM دارد، ولی استفادهٔ آینده از read_region بدون اصلاح، مصرف را تشدید می‌کند.
3. هر ناحیه چند scale، PSM retry، eng pass و گاهی glyph pass دارد. `ocr_calls` شمارندهٔ همهٔ actual native invocations نیست؛ اختلاف آن با `ocr_native_calls` در evidence مشخص است. مدل‌ها برای هر سند با pool جدا دوباره بار می‌شوند؛ cache سندی و محدود به ۱۲۸ ورودی/۴MiB است.
4. `Metrics` فقط RSS فرایند میزبان OCR را می‌سنجد. مدل‌های child و هزینهٔ W precision خارج peak قابل اتکای آن هستند؛ از این عدد ظرفیت concurrent استخراج نشود.
5. native monitor حدود ۶۰ ثانیه، انتظار pool حدود ۹۰ ثانیه، fallback CLI هر بار ۹۰۰ ثانیه؛ deadline کلی سند legacy تعریف نشده. W Paddle: initialize=۱۸۰، detect=۶۰۰، recognize=۱۲۰ ثانیه، pipe=۳۰ و سقف worker=۸GiB؛ page precision budget=۲۴۰۰ ثانیه بین calls بررسی می‌شود و زمان OCR اولیه را پوشش نمی‌دهد.
6. سقف upload ۵۰۰MiB، حداکثر ۱۰۰ فایل و ۳۰۰ صفحه در batch؛ queue slot=۲ و RUN_LOCK=۱ فقط در هر process. چند process آینده این تضمین را سراسری نمی‌کند. `JOBS` به ۱۰۲۴ ورودی محدود می‌شود ولی eviction عمرمحور ندارد. با گذشت زمان، حتی jobهای پایان‌یافته می‌توانند پذیرش را ببندند.
7. temp CLI در finally حذف می‌شود؛ در P startup مسیر TEMP خصوصی تنظیم می‌شود، در W چنین enforceای نیست. master/upload/output عمدتاً ماندگارند. زمان retention آن‌ها **NOT_IMPLEMENTED** در کد برنامه؛ cleanup زمان‌بندی‌شدهٔ بیرونی **UNKNOWN**.

زمان startup سردِ مدل به‌تنهایی، peak RAM برای چند OCR هم‌زمان، throughput تحت بار و P50/P95 عملیاتی **NOT_MEASURED**. اولین فراخوانی‌های native unit test و startup-inclusive wall زمان load+inference+spawn را مخلوط دارند؛ از آن‌ها عدد ساختگی «زمان بارگذاری مدل» استخراج نشده است. آزمون فشار روی سرویس فعال انجام نشد.

### H. Recommended Target Architecture

ابتدا اصلاح محافظه‌کارانهٔ سیستم موجود و همسان‌سازی منبع نسخه‌ها لازم است. تعویض مستقیم پوشهٔ W با P مجاز نیست: ممکن است محافظ رندر، quality metadata، templateها، private-temp startup یا review فعال حذف شود. نسخهٔ هدف باید از سورس دقیق release فعال، با حفظ قابلیت‌ها، و cherry-pick/ادغام هدفمند ساخته شود؛ در این مأموریت این کار انجام نشده است.

معماری پیشنهادی پس از baseline معتبر، استنتاج مهندسی است و benchmark اثبات‌شده نیست:

```text
ورودی محصور و budgetدار، فایل تغییرناپذیر و شناسهٔ job
  → تحلیل نوع PDF و شواهد کیفیت text layer، در سطح صفحه/ناحیه
  → text extraction محلی فقط برای نواحی معتبر؛ OCR برای سایر نواحی
  → reference image با DPI متناسبِ کیفیت منبع + transform قابل برگشت
  → OCR ناحیه‌ای CPU با profile زبان/ناحیه و بودجهٔ مشترک
  → observation خام و تمام alternatives مهم، بدون بازنویسی اصل
  → parser فیلد/جدول با provenance صفحه/box/row/column
  → validator دامنه‌ای بدون حدس + تطبیق بین اسناد
  → unknown / missing / uncertain / valid / human_confirmed
  → بازبینی الزامی فیلد حساس در صورت تعارض/نامعتبری
  → انتشار اتمیک خروجی نسخه‌دار و مصرف downstream با قرارداد روشن
```

raw OCR، متن منطقی، متن نمایش فارسی، مقدار canonical شناسه و مقدار انسانی باید فیلدهای جدا باشند. موتور دوم تنها برای خوانش ناحیهٔ حساس/نامطمئن و با مدل ازپیش‌نصب‌شده ارزیابی شود؛ تکرار scale همان مدل رأی مستقل نیست. توافق دو موتور هم جای check digit، تقویم، واحد و بازبینی را نمی‌گیرد. استفاده از LLM یا cloud در معماری پیشنهادی نیست.

#### مقایسهٔ گزینه‌ها، بدون تعویض موتور

| گزینه | CPU / زبان | منابع و نگهداری | ریسک آفلاین / اعداد | کیفیت قابل اثبات / اتصال به سامانه |
|---|---|---|---|---|
| A: وضع موجود | بله؛ fas و eng با گذر جدا، P فعال Tesseract | اندازه‌گیری synthetic در E/G؛ استقرار ساده‌تر از چند runtime ولی چندگذر و master پرهزینه | local P؛ W cloud مشروط و Docker fallback باید بسته شوند. validator عددی دامنه‌ای ندارد. | **TESTED روی چهار مصنوعی**؛ کیفیت نمایندهٔ گمرک NOT_MEASURED. مسیر فعلی حفظ می‌شود. |
| B: Tesseract 5 تنظیم‌شده برای region | بله؛ fas/eng حاضر، mixed قابل تنظیم | reuse دادهٔ موجود؛ profile و DPI/PSM نیازمند tuning held-out؛ اندازهٔ مدل‌های محلی در Q16 اندازه‌گیری شده | قابل نصب کاملاً محلی؛ whitelist مخصوص نوع فیلد می‌تواند نویز را کم کند ولی احتمال حذف کاراکتر معتبر دارد. | backend و fixtureهای عدد واقعی تست شدند؛ **پروفایل بهینهٔ جدید NOT_TESTED**. P candidate زیرساخت بخشی از این گزینه را دارد ولی غیرفعال و خارج benchmark این ممیزی است. |
| C: PP-OCRv5 چندزبانهٔ فارسی | طراحی CPU صریح؛ مدل درست `arabic_PP-OCRv5_mobile_rec` با detector مجزا، پشتیبانی fa/en براساس مستند رسمی | دو مدل و runtime جدا؛ سقف ۸GiB W **حد پیکربندی است، نه RAM اندازه‌گیری‌شده**؛ مصرف واقعی UNKNOWN. بسته‌های CPU و سازگاری Windows باید تست شوند. | model_dir و hash ثابت؛ SDK دانلودکننده و native runtime نیازمند deny egress. تشخیص بهتر عدد روی این corpus اثبات نشده. | **NOT_TESTED / NOT_INSTALLED**؛ wrapper W هست، P ندارد. هیچ وزن دانلود نشد. |
| D: direct PDF + OCR انتخابی | CPU و Unicode موجود PDF؛ برای نواحی raster همان OCR محلی | تخمین کیفی: برای PDF متن‌دار می‌تواند رندر/OCR را کم کند؛ عدد RAM/سرعت **NOT_MEASURED**. معیار اعتبار متن و dedup پیچیدگی اصلی است. | خطر پذیرفتن لایهٔ خراب یا hidden text و تکرار متن؛ عدد حساس نیازمند evidence مکانی/اعتبارسنجی است. | **NOT_TESTED، routing فعلی NOT_IMPLEMENTED**؛ با orchestration صفحه‌ای قابل افزودن بدون حذف fallback فعلی. |
| E: موتور اصلی + بازخوانی حساس با موتور دوم | هر دو باید local CPU باشند؛ همان language/model pin لازم است | هزینهٔ اضافی فقط روی ناحیهٔ انتخابی؛ budget زمان/RAM/queue باید مشترک باشد؛ مقدار مصرف **NOT_MEASURED** | استقلال نسبی از مدل متفاوت، ولی خطای مشترک ممکن؛ disagreement هرگز بی‌هشدار resolve نشود. | reconcile و budgetهای W با fake engine تست دارند؛ **benchmark دو موتور واقعی NOT_TESTED**؛ پذیرش محتاج GT فیلد و false-accept است. |

پشتیبانی فارسی در نام عمومی «PP-OCRv5» تضمین نمی‌شود؛ مدل `server_rec` پیش‌فرض همان مدل Arabic نیست. مرجع رسمی، فارسی و انگلیسی را برای Arabic multilingual ذکر می‌کند: [جدول مدل‌های چندزبانه PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/algorithm/PP-OCRv5/PP-OCRv5_multi_languages.md). تنظیم CPU و مسیرهای local در [مستند رسمی اجرای PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/pipeline_usage/OCR.en.md) شرح داده شده است؛ هیچ accuracy فروشنده به دادهٔ گمرک تعمیم داده نشد.

برای Tesseract، مدل float best و integer fast انتخاب هزینه/کیفیت متفاوت دارند؛ تغییر مدل تنها با آزمایش روی همین GT مجاز است. مستند [tessdata_fast](https://tesseract-ocr.github.io/tessdoc/Data-Files-in-tessdata_fast.html) و [کنترل thread در Tesseract](https://tesseract-ocr.github.io/tessdoc/FAQ.html) مبنای امکان فنی‌اند، نه benchmark این میزبان. در این ممیزی مدل fast نصب یا جایگزین نشد.

### I. Remediation Backlog

این backlog پیشنهاد است؛ کد محصول در این مرحله تغییر نکرده است. برای فایل‌های P که در W موجود نیستند، ابتدا باید سورس معتبر release فعال وارد مبنای کار شود.

| اولویت / اقدام | فایل‌های مرتبط | پیش‌نیاز | معیار پذیرش و آزمون |
|---|---|---|---|
| P0-01: همسان‌سازی release و منبع | P/W `serve.py,web_app.py,render.py,pipeline.py,quality.py,precision*,templates*,review*` | inventory دقیق P/W و حفظ کار ثبت‌نشدهٔ کاربر | قابلیت‌های P، routes، preview، private-temp و quality بدون حذف باقی بمانند؛ hash release و commit واحد؛ regression روی هر flow. |
| P0-02: اصلاح evaluator و گزارش بنچمارک | `benchmark.py,run_benchmark.py,ocr_bot.py,tools/evaluate_accuracy.py` | تعاریف CER/WER/EM و واحد گزارش | WER token-level؛ فضای گمشده CER غیرصفر؛ duplicate/order/missing/extra عدد جدا؛ پیدا کردن نام جدید جدول؛ نبود خروجی/نمونه=FAIL/NOT_MEASURED، هرگز VERIFIED ثابت. |
| P0-03: قرارداد exporter و نصب offline آن | `requirements.txt,tables.py,review.py,web_app.py` و packaging | تصمیم صریح mandatory یا degraded export؛ wheel از منبع مورداعتماد و محیط ایزوله | `openpyxl` در runtime عرضه شود؛ fail readiness یا failure روشن exporter؛ خروجی OCR معتبر با خطای غیرOCR مبهم نشود؛ همهٔ ۱۵۴ تست و table pipeline واقعی موفق. |
| P0-04: انتشار هشدار و منع تأیید خودکار متعارض | `engines.select_consensus/merge_latin_secondary`, `pipeline.ocr_page`, W `precision.reconcile`, P `quality.py` | تعریف statusهای observed/uncertain/verified | اختلاف عددی با confidence بالا هم needs_review داشته باشد؛ raw/candidates حفظ؛ دو scale یا دو confidence پایین flag را پاک نکنند؛ تست end-to-end caller. |
| P0-05: اعمال سیاست محلی قطعی | W `engines.py,pipeline.py,serve.py`، Docker runner، `paddle_worker.py` | الزام ممنوعیت خارجی موجود همین مأموریت | credential یا config نتواند cloud OCR را فعال کند؛ native و Docker در deny-outbound مصنوعی کار کنند؛ مدل گم‌شده بدون دانلود fail شود. |
| P1-01: schema و validators حساس | ماژول domain جدید متناسب با معماری، parser و contracts export/review | قواعد کشور/نوع سند و استانداردها، بدون حدس شناسه | ISO check digit، HS context، date/calendar، Decimal ارز و units، net/gross، totals و cross-document؛ status ناموجود/ناخوانا؛ هر field با source bbox/raw؛ mutation شناسه ممنوع. |
| P1-02: baseline نماینده و release gate | `tools/run_acceptance.py,evaluate_accuracy.py`، P `evaluation.py` و تست‌های GT | corpus مجاز مستقل و مدل/نسخه pin | نتایج held-out به تفکیک خانواده/زبان/کیفیت/field؛ false-accept حساس و workload؛ پنج صفحهٔ gate فقط smoke، نه معیار عمومی. |
| P1-03: کنترل کیفیت ناحیه و هندسه | `preprocess.py,layout.py,render.py,pipeline.py`، P `precision/imaging.py` | image-level GT و تست نقطه/اعشار/لبه در DPIهای متعدد | original immutable، transform معکوس‌پذیر، fallback اصلی، ablation هر فیلتر؛ orientation/crop نادرست با flag؛ حفاظت رندر قطعه‌ای P حفظ شود. |
| P1-04: بودجهٔ مشترک و observability | `metrics.py,native_pool.py,engines.py,process_guard.py,web_app.py` | SLA، budget سرویس و سیاست abort | CPU/RSS درخت، load time و stage time کامل، deadline سند؛ admission مشترک میان process؛ هیچ fallback بدون سقف؛ تست timeout/crash/restart. |
| P1-05: retention و امنیت log/storage | `serve.py,web_app.py,review.py` و job cleanup خصوصی | سیاست نگهداری/backup و مجوز حذف | upload/master/export خارج IIS/Git، ACL و temp خصوصی، quota، redaction با تست؛ cleanup فقط محدودهٔ job منقضی و هرگز اسناد اصلی بدون مجوز. |
| P1-06: ارزیابی محلی گزینه B/D/E | orchestration و adapter engine، پروفایل‌ها | P0ها، corpus و artifactهای از پیش نصب‌شده | مقایسهٔ A/B/D/E روی همان split، source hash و CPU budget؛ عدد حساس بدون اعتبارسنجی وارد خروجی تأییدشده نشود؛ rollback سازگار. |
| P2-01: مدل دوم و ظرفیت‌سنجی | W `paddle_engine.py,paddle_worker.py,model_artifacts.py,requirements-paddle.lock` | مجوز جدا برای تأمین مدل، محیط نصب آفلاین و فضای کافی | cold/warm، fa/en/mixed، digit EM و memory/concurrency واقعی؛ مقایسه با Tesseract بدون ادعای پیشینی؛ OS offline test. |
| P2-02: جدول پیچیده/چندستون | `layout.py,tables.py` و parser row association | GT شبکه، span و line-item IDs | gridless/merged-cell و ترتیب ستون، row association و عدم جابه‌جایی عدد با label؛ positional و semantic metrics جدا. |
| P2-03: lock و بهداشت ابزارها | requirements اصلی، lock native/DLL، lint/type config | inventory runtime و تصمیم سازگاری Windows | نصب بازتولیدپذیر، SBOM، lint قبلی تفکیک‌شده و typecheck مرحله‌ای؛ ارتقای نامرتبط موتور انجام نشود. |

### J. Open Questions

فقط موارد حل‌نشده با سورس و آزمون این مرحله:

1. `NEEDS_USER_INPUT`: هشت خانوادهٔ سند در مصرف واقعی چه سهمی دارند، فیلدهای اجباری هر نوع/کشور و خطای قابل‌قبول هر فیلد چیست؟
2. `NEEDS_USER_INPUT`: مصرف‌کنندهٔ downstream کدام برنامه است و آیا بعد از download یا JSON، لایه‌ای بیرون این مخزن فیلدها را در دیتابیس ثبت می‌کند؟ وجود چنین لایه‌ای از این سورس اثبات نشد.
3. `NEEDS_USER_INPUT`: corpus محلی مجاز، GT بازبینی‌شده و مرجع خصوصی پنج‌صفحه‌ای دقیقاً کجا و با چه مجوز استفاده‌ای هستند؟ هیچ جست‌وجوی محتوایی در اسناد کاربران انجام نشد.
4. `UNKNOWN`: نسخهٔ کاری ثبت‌نشده چگونه با release فعال 52e15ac... مرتبط است و کدام مسیر precision/review مبنای توسعهٔ موردنظر است؟ اختلاف کد اثبات شد، نیت ادغام از سورس قابل تعیین نیست.
5. `UNKNOWN`: حجم/توزیع صفحات، DPI منبع، تراکم جدول و concurrent requests عملیاتی در بازهٔ نماینده؛ برای محرمانگی log/اسناد کاربران خوانده نشد.
6. `UNKNOWN`: resource contention و quota میزبان hypervisor، فضای رشد، latency ذخیره‌سازی و capacity هدف؛ snapshot OS جای پایش بازه‌ای نیست.
7. `UNKNOWN`: تمام backupها، سیاست retention بیرونی، وضعیت بالفعل ACL همهٔ مسیرها، OS firewall و egress native؛ deploy script گواه اجرای همهٔ آن‌ها نیست.
8. `UNKNOWN`: زمان cold load و کیفیت واقعی مدل دوم روی CPU این نصب؛ runtime/model نصب‌شده‌ای برای آزمایش حاضر نبود.
9. `NEEDS_USER_INPUT`: الزام هویت سازمانی/SSO، مسئول تأیید فیلد حساس، audit trail قابل اتکا و سیاست reuse/انقضای نشست و بازبینی چیست؟
10. `UNKNOWN`: منشأ تاریخی دانلود fas/eng و همهٔ revisionهای DLL؛ هویت byte/hash فعلی مشخص است، زنجیرهٔ تأمین تاریخی کامل نیست.
11. `UNKNOWN`: نسخه و مسیر دقیق Waitress و سایر ماژول‌های ازقبلبارشده در حافظهٔ سرویس؛ metadata اجرای cold با خدمتِ درحال‌اجرا همخوانی کامل ندارد. بدون restart یا introspection تأییدشده نمی‌توان این بخش را قطعی کرد؛ سرویس restart نشد.

در طول مأموریت هیچ سؤال یا درخواست مجوز دانلود/تغییر مطرح نشد؛ این موارد برای تصمیم بعدی ثبت شده‌اند.

### K. Final Verdict

**`REPAIR_CURRENT_ENGINE`**، با اطمینان بالا دربارهٔ ضرورت اصلاح لایه‌های فعلی و اطمینان ناکافی برای تغییر موتور.

شواهد، نقص معیار، گم‌شدن uncertainty، نبود validator تجاری، شکست/پنهان‌سازی خطای exporter و اختلاف release را نشان می‌دهند. هیچ‌یک به‌تنهایی دلیل تعویض Tesseract نیست. افت روی اسکن کم‌کیفیت واقعیِ مصنوعی اندازه‌گیری شد، اما dataset کوچک و دو نسخهٔ متفاوت اجازهٔ تعمیم به تمام اسناد یا اعلام برتری Paddle را نمی‌دهد. رفع R01–R04 و همسان‌سازی منبع P/W باید پیش از تصمیم موتور انجام شود.

`HYBRID_ARCHITECTURE` از نوع direct-PDF + OCR انتخابی و بازخوانی ناحیهٔ حساس یک **هدف مشروط پیشنهادی** است، نه نتیجهٔ benchmark تأییدشده. `REPLACE_OCR_ENGINE` با این شواهد قابل دفاع نیست. کیفیت تولید، Field EM و SLA عملیاتی تا corpus و آزمون مستقل `NOT_MEASURED` باقی می‌مانند.
