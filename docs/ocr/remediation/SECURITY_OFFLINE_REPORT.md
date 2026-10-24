# امنیت و اجرای آفلاین

وضعیت: کنترل‌های محلی W آزموده شده‌اند؛ **اثبات امنیت کامل یا OS egress deny صادر نمی‌شود**. نسخه فعال تغییر نکرده است. شواهد: final_tests، final_diagnostics، production_integrity و change_inventory در evidence/.

## اصلاحات و مرز آزمون

| کنترل / ریسک | رفتار پس از اصلاح و شاهد | حد نتیجه |
|---|---|---|
| P0 خروج تصویر از Google Vision | کلاس inert، نگهداری کلید حذف، ocr_image همیشه EXTERNAL_OCR_DISABLED؛ مسیر انتخاب و خواندن api_key.txt حذف؛ env ناامن و available=True آزموده شد | PASS در مسیرهای سورس W؛ P تغییری نکرد |
| P0 registry/Docker fallback | available فقط native؛ entrypoint غیرnative پیش از نوشتن تصویر خطا می‌دهد؛ هیچ docker info/run/pull اجرا نمی‌شود | PASS محلی |
| artifact مدل | local_models.py فقط hashهای مشاهده‌شده fas/eng/Arabic را می‌پذیرد؛ مدل مفقود/دست‌کاری‌شده/زبان path traversal رد می‌شود؛ readiness نیز hash را بررسی می‌کند | PASS آزمون؛ دایرکتوری مدل باید immutable و ACLدار باشد؛ جلوگیری OS از تعویض فایل پس از hash آزموده نشده |
| خطای subprocess | stderr خام و traceback سند وارد پاسخ/لاگ نمی‌شود؛ پیام ثابت و دسته خطا؛ CLI با env allowlist و حافظه/مهلت محدود اجرا می‌شود | PASS مسیرهای تغییرکرده؛ audit کامل همه کتابخانه‌های native انجام نشده |
| storage/TEMP | serve.configure_storage مسیر مطلق خارج release، reserve دیسک و probe نوشتن را پیش از import اعمال می‌کند | PASS توسعه؛ بررسی ACL و همه webrootهای واقعی سرور NOT_TESTED |
| session/CSRF/مالکیت/preview/download | آزمون‌های موجود W بدون حذف موفق؛ مسیر جدید endpoint ایجاد نشده | PASS آزمون W؛ سازگاری routes متفاوت P هنوز BLOCKED |
| CSV / HTML / Excel | escape فرمول و HTML موجود حفظ؛ سلول XLSX صریحاً text؛ فقدان exporter خروجی‌های دیگر را حذف نمی‌کند | آزمون‌های mock و بدون وابستگی موفق؛ فایل XLSX واقعی BLOCKED_DEPENDENCY؛ CSV ذاتاً تضمین نوع در همه نرم‌افزارهای spreadsheet ندارد |
| review نسخه‌دار | status XLSX در table/page/document/state/API هم‌راستا؛ UI هشدار availability را می‌گیرد؛ publish ساختاریافته انسانی انجام نشده | عدم نمایش snapshot خودکار به‌عنوان current با metadata صریح |
| CPU/RAM | CLI سقف ۷۶۸MiB/۹۰s؛ pool حداکثر چهار worker؛ deadline مشترک OCR | سقف حافظه سخت pool، timeout سخت render، multi-process admission و quota سراسری کامل نشده |
| نسخه عملیاتی | hash هر ۵۲ فایل شناخته‌شده P بدون تغییر؛ health فقط GET loopback، release ثابت | integrity سورس؛ ممیزی کل دیسک/DB یا داده کاربران انجام نشده |

## مدل‌ها و وابستگی‌ها

fas: `99e420969b5ddd2cb135b416316a7ed417c59c4faf9e0d28941348f6448114df`

eng: `8280aed0782fe27257a68ea10fe7ef324ca0f8d85bd2fd145d1c7dce4c35493902e`

Arabic: `a80454f72df35c6f52550551dd69b8521ea6a62d834396d1c7dce4c35493902e`

منبع اعتماد این مرحله artifact محلی نصب‌شده و ثبت‌شده پیش از تغییر است؛ این hashها اجازه دانلود یا نصب نسخه دیگری نیستند. Paddle و openpyxl و کتابخانه معتبر Jalali در runtime آزمون حاضر نیستند؛ wheel آفلاین تأییدشده در مخزن پیدا نشد. تعداد نصب/دانلود مدل، wheel یا image: صفر. مقایسه Paddle **NOT_TESTED**؛ هیچ بهبود نظری به‌جای benchmark اعلام نمی‌شود.

## حدود محرمانگی و شبکه

آزمون‌ها فقط چهار PDF مصنوعی مشخص با fingerprint/ساختار کنترل‌شده و fixtureهای مصنوعی موقت را خواندند؛ هیچ جست‌وجو یا OCR روی اسناد مشتری انجام نشد. خروجی‌های مرجع و مثال‌ها مصنوعی هستند. snapshot توسعه از allowlist سورس، بدون secrets و uploads، در محل خصوصی خارج webroot ثبت شد. هیچ commit، push، deploy، restart، تغییر IIS، تنظیم عملیاتی، firewall یا DB اجرا نشد.

socket-block در harness فقط Python را محدود می‌کند. Windows Job Object حافظه و مالکیت فرایند است و firewall نیست. **OS egress deny، native DLL telemetry، dependency CVE audit آنلاین و test VM کاملاً بدون شبکه NOT_TESTED** هستند. اجرای محلی با مدل نصب‌شده در آزمون موفق است؛ تضمین اجرای کل پشته با شبکه OS مسدود نیازمند VM ایزوله است.

لاگ‌های عمومی تغییرکرده از متن استثنا پاک شده‌اند؛ manifest خصوصی job و snapshot OCR طبق قابلیت موجود متن سند را نگه می‌دارند. سیاست retention، حذف امن، backup encryption و ACL حساب سرویس باید با مالک داده تعیین شوند؛ هیچ پاک‌سازی فایل کاربری انجام نشده است. TIFF master در خروجی خصوصی حفظ می‌شود؛ read_region هنوز کل TIFF را decode می‌کند. direct developer entrypoint و استقرار IIS آینده باید تنها از storage خصوصی پیکربندی‌شده استفاده کنند؛ سخت‌سازی serve جای احراز ACL همه entrypointها نیست.

Paddle worker موجود دارای مسیر مدل محلی و verify_models است؛ نصب نبودن آن مانع تست واقعی inference/telemetry آن است. دو موتور هم‌نظر و regex معتبر جای تأیید انسانی نیستند. بازبینی مستقل دو agent فقط‌خواندنی انجام شد؛ اشکال‌های Decimal، مقایسه واحد، تاریخچه، manifest Excel و منشأ متن منتخب با regression test اصلاح شدند. این بازبینی جای تست نفوذ یا ممیزی جامع امنیت همه اجزای سامانه نیست.
