# برنامه انتشار و بازگشت؛ اجرا نشده

**حکم فعلی: استقرار مجاز/آماده نیست.** این سند برنامه اقدام بعدی است؛ نه گزارش انجام استقرار. سورس P، IIS، Waitress، DNS، firewall، settings، DB و اسناد کاربران تغییر نکرده‌اند. هیچ commit یا push انجام نشده است؛ تغییرات ثبت‌نشده قبلی حفظ شده‌اند.

## شروط لازم پیش از تهیه بسته قابل استقرار

1. اختلاف‌های P/W شامل quality، precision/، candidate/compare، routes بازبینی و preview، handwriting declaration و قرارداد خروجی به‌صورت مستقل تطبیق و با regression test پذیرش شوند. فایل هم‌نام را معادل فرض نکنید.
2. قرارداد XLSX تعیین و wheelhouse تأییدشده تأمین شود؛ اگر mandatory است `OCR_REQUIRE_XLSX=1` باید readiness و admission را پیش از دریافت job ناموفق کند. حالت degraded فقط با هشدار صریح مجاز است.
3. corpus دارای ground truth مجاز و held-out از خانواده‌های واقعی، اسکن/متن/ترکیبی، زبان و field حساس تهیه شود. نرخ false-accept، CER/WER، Field EM، association جدول و بار هم‌زمان اندازه‌گیری شوند.
4. VM بدون egress و artifactهای local-only؛ سقف سخت حافظه pool/render، deadline job، رفتار cancellation/restart و quotas چندپردازه تأیید شوند. هیچ قاعده ملی/تقویم بدون مرجع وارد acceptance نشود.
5. schema_v2 و text-first تا اثبات کافی خاموش بمانند. publish فیلدهای انسانی و مجوز پرونده تعریف نشده است؛ اتصال DB/API تجاری فعلاً ممنوع است.

## مسیر انتشار آینده، فقط با مأموریت جداگانه

- snapshot خصوصی و diff موجود را برای مرور تغییرات خود این مرحله استفاده کنید. پیش از ادغام، تغییرات قبلی کاربر را جداگانه بررسی کنید؛ git reset/clean یا کپی دسته‌ای W روی P مجاز نیست.
- روی سورس ادغام‌شده تست‌ها، fingerprint، benchmark، QA امنیت و قرارداد همه routes دوباره اجرا شوند. branch/revision بازبینی‌شده طبق AGENTS.md پیش از deployment باید روی origin/main باشد؛ **در این مأموریت commit/push اجرا نشود**.
- بسته immutable باید فقط سورس/assets و manifest hash داشته باشد؛ uploads، outputs، keys، certificates و production settings بیرون Git و webroot بمانند. بسته قابل استقرار فعلاً ساخته نشده؛ change_inventory صرفاً manifest ممیزی است.
- پس از مجوز مستقل استقرار، preflight deploy.ps1، storage/TEMP خصوصی، محیط/مدل pinشده، dependency readiness و release قبلی حفظ شوند. تصمیم rollback و معیار توقف پیش از تغییر سرویس مشخص شود.
- در همان مأموریت مجاز، HTTPS، health، مالکیت، CSRF و upload/download با **سند مصنوعی** آزموده شوند؛ این بررسی‌های production OCR در این مرحله اجرا نشده‌اند.

## rollback

Rollback عملیاتی در این مرحله لازم/اجرا نشده است؛ release فعال تغییر نکرد. برای انتشار آینده، revision قبلی و settings خصوصی همان نسخه حفظ و با روال deploy.ps1 بازگردانده شوند؛ اسناد و uploads مستقل باقی بمانند. هیچ دستور حذف release یا داده ارائه/اجرا نشده است.

Rollback توسعه باید file-by-file از snapshot خصوصی پیش از اصلاح، با مقایسه hash فعلی و بازبینی تغییرات بعدی انجام شود. snapshot اولیه کاربر را با HEAD اشتباه نگیرید. sealed_change_inventory.json تفاوت با snapshot پیش از همین مأموریت را ثبت می‌کند. بازگرداندن خودکار یا بازنویسی تغییرات جدید مجاز نیست.
