> Current verification and source identity: [CURRENT_SOURCE_STATUS.md](CURRENT_SOURCE_STATUS.md). This procedure does not certify a Windows release or hotel-device acceptance. Check current source and actual completed test outcomes before applying it.

# Maintenance routine

## Library updates

- Check for dependency updates monthly. `pyproject.toml` pins compatible
  ranges; run `pip install -U` within those ranges on the demo/test setup
  first, run the full suite (see docs/TESTING.md), then update the hotel
  machine.
- Review security advisories for FastAPI, SQLAlchemy, cryptography, and
  psycopg as they appear; apply security fixes promptly after a suite run.

## Windows updates

- Keep Windows Update enabled on the hotel machine. Apply cumulative updates
  monthly, outside working hours, and confirm the app, PostgreSQL service,
  and the nightly backup scheduled task all start afterwards.

## PostgreSQL version

- The tested target during development was PostgreSQL 16. After acceptance on
  the hotel machine, record the exact installed version here and treat it as
  the supported version: upgrade only deliberately, after a backup and a
  restore drill, not casually.

## Backups

- Encrypted nightly backups to USB remain manual-deletion only: the operator
  deletes old archives deliberately, the app never auto-deletes.
- Keep `backup_encryption_key.json` with the backups and a second copy on a
  separate drive; keep the passphrase (12+ characters) in a separate safe
  place. An old copy of the key file stays readable by its old passphrase, so
  account for it in the same protection plan.

## Arabic summary / ملخص عربي

- تحديث المكتبات كل شهر على نسخة التجربة الأول، وبعد نجاح الاختبارات على جهاز
  الفندق.
- تحديثات ويندوز شهرية خارج ساعات العمل، والتأكد بعدها إن التطبيق وقاعدة
  البيانات والمهمة الليلية شغالين.
- إصدار PostgreSQL المجرب هو 16؛ بعد تجربة جهاز الفندق سجّل الإصدار الفعلي
  هنا واعتبره المعتمد، والترقية تبقى قرار مدروس بعد نسخة احتياطية وتجربة
  استعادة.
- النسخ الاحتياطية مش بتتحذف تلقائيًا أبدًا؛ الحذف بقرارك أنت. ملف المفتاح
  `backup_encryption_key.json` مع النسخ وله نسخة تانية في مكان منفصل، وكلمة
  السر (12 حرفًا على الأقل) في مكان ثالث آمن.
