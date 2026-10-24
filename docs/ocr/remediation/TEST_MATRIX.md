# ماتریس آزمون و کنترل کیفیت

آخرین suite پس از اصلاح رندر: **۲۰۵ آزمون، ۲۰۵ موفق، صفر failure/error/skip، ۴۴٫۹۶ ثانیه**. فرمان اجرا `python -B -X utf8 docs/ocr/audit_diagnostics.py tests --output docs/ocr/remediation/evidence/sealed_tests.json` است. همه آزمون‌ها در محیط موقت با داده مصنوعی و منع Python socket اجرا شدند؛ P وارد یا اجرا نشد. موفقیت suite معادل پذیرش OCR روی corpus واقعی نیست.

## تاریخچه واقعی، بدون حذف شکست‌ها

| evidence | n آزمون | failure | error | skip | ثانیه |
|---|---:|---:|---:|---:|---:|
| g0_tests.json | 154 | 1 | 7 | 0 | 28.771 |
| g2_tests.json | 163 | 1 | 0 | 0 | 30.82 |
| g3_tests.json | 167 | 1 | 0 | 0 | 29.476 |
| g4_tests.json | 172 | 1 | 0 | 0 | 29.128 |
| g5_tests.json | 174 | 1 | 0 | 0 | 29.233 |
| g6_tests.json | 180 | 0 | 0 | 0 | 33.158 |
| g8_tests.json | 186 | 0 | 0 | 0 | 31.621 |
| g9_tests.json | 189 | 0 | 1 | 0 | 37.844 |
| final_tests_1.json | 197 | 0 | 0 | 0 | 35.447 |
| final_tests_2.json | 200 | 0 | 0 | 0 | 41.373 |
| final_tests_3.json | 200 | 0 | 0 | 0 | 42.037 |
| final_tests.json | 202 | 0 | 0 | 0 | 43.543 |
| final_after_render_tests.json | 204 | 0 | 0 | 0 | 43.9 |
| sealed_tests.json | 205 | 0 | 0 | 0 | 44.96 |

علت شکست‌های ثبت‌شده:

- G0: هفت error و یک failure مرتبط با نبود exporter openpyxl؛ جدا از دقت recognizer.
- G2: یک expectation قدیمی درباره نوع رقم در normalizer ارزیابی؛ قرارداد جدید NFC_digits_v1 به ASCII یکسان‌سازی می‌کند. آزمون با برابری ارقام فارسی/عربی و آزمون مستقل عدم ادغام حروف معنایی اصلاح شد.
- G3: expectation نبود کلید needs_review در توافق، به مقدار false صریح قرارداد تبدیل شد؛ اختلاف واقعی همچنان true آزموده می‌شود.
- G4: مقایسه مسیر Windows کوتاه 8.3 با مسیر resolveشده شکست خورد؛ fixture مسیر مطلق canonical را مقایسه می‌کند.
- G5: ثبت دوباره candidate بدون نام موتور کنار همان متن با نام موتور؛ dedup صرفاً همین تکرار شاهد را حذف کرد و هر دو متن متفاوت حفظ شدند.
- G9: mock موتور model_hashes واقعی نداشت؛ metadata آن به dict تست تبدیل شد. اجرای اولیه هدفمند NativePool نیز خطای نام seconds داشت؛ signature درست شد و suite کامل دوباره اجرا شد.
- یک اجرای مجزای acceptance با filler=۱۲۰۰ به علت هزینه شدید edit-distance پس از تکرار موفق suite متوقف شد؛ PASS محسوب نشده است. fixture نهایی با filler=۲۰۰ همان تهدید جابه‌جایی label در متن با CER کمتر از آستانه را آزموده و موفق است.
- mypy اولیه هفت خطای نوع در چهار فایل جدید/بازنویسی‌شده داشت؛ annotation و کنترل schema اصلاح شدند؛ اجرای نهایی هفت ماژول موفق است.

هیچ تستی skip نشده و هیچ شرط اصلی مالکیت، تزریق، داده خام، خطای واقعی disk یا لزوم هشدار برای سبزکردن suite حذف نشده است. تست Docker طبق الزام مستقیم مأموریت، از «fallback فعال» به «عدم انتخاب حتی با daemon آماده» تغییر کرد. تست حذف نویز اکنون حفظ تک‌پیکسل نامشخص را الزام می‌کند؛ variant قدیمی فقط با پارامتر صریح برای ablation باقی است. آزمون low-confidence جدید در کنار نمونه توافق واقعاً پر‌اطمینان قرار گرفت. تست strict Excel همچنان خطای dependency را می‌خواهد؛ تست‌های مستقل عدم وابستگی TXT/JSON/CSV/HTML را پوشش می‌دهند.

## پوشش کنترل‌ها

| حوزه | محل آزمون | نتیجه و حد |
|---|---|---|
| CER فاصله، WER واژه، ارقام تکراری/ترتیب/علامت | test_benchmark_contract.py | PASS؛ strict/normalized جدا؛ مجموعه خالی و خروجی مفقود موفق نیست |
| Field EM با label/context/page/bbox؛ schema_v2 mapping | test_benchmark_contract.py، test_remediation_regressions.py | PASS روی fixture؛ Field EM تجاری NOT_MEASURED |
| acceptance متن تقریباً درست با label حساس جابه‌جا | test_acceptance.py | PASS؛ نبود hash، manual revision و صفحه تکراری نیز رد می‌شوند |
| exporter اختیاری/mandatory، disk error واقعی، status revision | test_export_contract.py، test_readiness_contract.py، test_remediation_regressions.py | PASS؛ Excel واقعی BLOCKED_DEPENDENCY |
| اختلاف ۱۲۳۴/۱۲۸۴ با confidence=.99 تا page JSON و review store | test_uncertainty_contract.py | PASS؛ هر دو candidate و هشدار باقی‌اند؛ DOM/browser عملی NOT_TESTED |
| صفر confidence، توافق کم‌اطمینان، inherited uncertainty | test_uncertainty_contract.py، test_remediation_regressions.py | PASS؛ هیچ تأیید انسانی خودکار |
| raw/logical/display/canonical، leading zero، نیم‌فاصله، slash/hyphen/اعشار | test_data_contract.py، test_glyph_safety.py | PASS؛ RTL تخصصی corpus واقعی NOT_MEASURED |
| کانتینر، HS/context، تقویم، Decimal، وزن، جمع سطر و scope پرونده | test_validators.py، test_remediation_regressions.py | PASS قواعد پیاده‌شده؛ Jalali BLOCKED_DEPENDENCY؛ rules ملی نامعلوم |
| common labelled extractor / bbox / label تکراری / خانواده ناموجود | test_fields.py، test_pdf_text.py | PASS؛ هشت خانواده تخصصی unsupported |
| نقطه و اعشار چندمقیاسی، قطع جوهر در deskew، geometry معکوس | test_glyph_safety.py، test_remediation_regressions.py، test_review.py | PASS fixture؛ ablation کامل grid-line/faint/merged-cell NOT_MEASURED |
| native لاتین، invisible text، image mixed و چرخش، default flag off | test_pdf_text.py | PASS؛ فارسی native عمداً fallback؛ detector جای glyph identity proof نیست |
| رندر vector در مرز tile؛ حفظ sampling اسکن؛ fallback سفید | test_pdf_text.py | PASS؛ دو regression اختصاصی پس از کشف پسرفت اضافه شد |
| cloud config خصمانه، Docker entry، مدل missing/tampered، private TEMP | test_offline_contract.py | PASS کنترل سورس؛ OS firewall NOT_TESTED |
| upload/owner/CSRF/path/XSS/فرمول/preview/revision conflict | test_web.py، test_review.py، test_accuracy.py | PASS قرارداد W؛ تطبیق کل routes P BLOCKED |
| child timeout/crash/memory، model reuse و whitelist reset | test_process_guard.py، test_native_pool.py، test_performance.py | PASS کنترل‌های موجود/تغییرکرده؛ سقف سخت کل pool/render کامل نیست |

## baseline و حدود داده

مرجع OCR فقط چهار PDF مصنوعی، هر کدام یک صفحه، با زبان فارسی/مختلط، کیفیت کم و جدول خط‌دار است. synthetic source hashها و source_unchanged در evidence ثبت‌اند. جدول مرجع ۵×۴ و ۲۰ سلول دارد؛ شکل، Cell/Row/Column EM از end-to-end جدا ثبت شده‌اند. جدول gridless و merged-cell پشتیبانی اثبات‌شده ندارند. معیار متن نمونه جدول فقط عنوان خارج جدول است؛ CER صفر آن به معنی درست‌بودن ۲۰ سلول نیست.

اجرای final_sealed_baseline پسرفت تکرارشونده mixed را آشکار کرد. دو ablation تحت supervisor اجرا شدند: single_render معیار قبلی را برگرداند؛ alias_consensus اثری نداشت. آزمون‌ها داده مصنوعی مشترک داشتند و هم‌زمان اجرا شدند، بنابراین زمانشان benchmark سرعت نیست. اصلاح سیاست رندر پس از این شواهد انجام شد؛ آخرین معیارهای تولیدشده در `evidence/sealed_baseline.json` و PERFORMANCE_BASELINE.md ملاک‌اند. شواهد شکست قبلی حذف/بازنویسی نشده‌اند.

CER/WER و exact matchهای این چهار fixture قابل گزارش‌اند؛ دقت خانواده‌های تجاری، Field EM نماینده، نرخ خطای حساس پذیرفته‌شده، نرخ بازبینی عملیاتی، cold/warm و P50/P95 **NOT_MEASURED**. برای پذیرش، corpus مجازِ version/hashشده با reference کامل متن/field/row/column/page/bbox، تفکیک زبان و کیفیت، خوانایی/ناخوانایی و held-out مستقل لازم است. برای نسبت خطای نادر، چهار صفحه مخرج معتبر تولید نمی‌کند.

## کنترل‌های ایستا و build

Ruff کامل برای ماژول‌های جدید/بازنویسی‌شده و تست‌های قرارداد، Ruff خطاهای بحرانی F821/F822/F823/F811/E9 روی runtime، mypy هفت ماژول جدید با follow-imports=skip و ignore-missing-imports، و AST parse بدون pyc اجرا شدند؛ جزئیات فرمان و شمارش در sealed_static_validation.json ثبت می‌شوند. این typecheck معادل strict typing کل Flask/CV نیست. build بسته/installer/container و استقرار اجرا نشده‌اند؛ وابستگی تازه‌ای نصب نشده است. بررسی امنیت فقط trust boundaryهای همین تغییرات و regressionهای W را پوشش می‌دهد.

اجرای repaired_baseline نشان داد شرط پوشش ۹۸٪ برای اسکن دارای حاشیه سفید بیش از حد محدود است؛ سیاست تک‌تصویری به نبود متن/بردار و وجود تصویر قابل مشاهده وابسته شد و regression حاشیه سفید اضافه شد. sealed_baseline بازگشت متن/اعداد سه صفحه به مبنا و رفع failure جدول را تأیید کرد؛ sealed_quality_comparison فقط همین چهار نمونه را پوشش می‌دهد. lint نهایی پس از اصلاح ترتیب import تست جدید موفق است.
