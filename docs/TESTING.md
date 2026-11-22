# Running the test suite

The suite must run against a database built with the project's real migration
chain, not with simplified `create_all` tables.

## Why

The install relies on database objects that SQLAlchemy's
`Base.metadata.create_all` never creates, for example the audit-log
immutability trigger added by migration 0009. A `create_all`-built database
silently lacks those objects, so tests can pass there while the real install
behaves differently (this actually happened during review: a cleanup query
that the trigger rejects on a migrated database passed on a `create_all`
database).

## How

1. Create a dedicated test database, separate from any demo or production
   database:

   ```sql
   CREATE DATABASE hotel_test;
   ```

2. Initialize it with the real migrations:

   ```bash
   DATABASE_URL='postgresql+psycopg://postgres@127.0.0.1/hotel_test' \
     python -m scripts.initialize_database
   ```

   or `alembic upgrade head` with the same `DATABASE_URL`.

3. Run the suite against it:

   ```bash
   DATABASE_URL='postgresql+psycopg://postgres@127.0.0.1/hotel_test' \
   SESSION_SECRET='test-secret-0123456789abcdef0123456789' \
     python3 -m pytest -q
   ```

`tests/conftest.py` fails the whole run immediately when the configured
database is not at the alembic head, with the found and expected revision in
the message. After pulling changes that add a migration, re-run step 2 before
the suite.

## Arabic summary / ملخص عربي

الاختبارات لازم تشتغل على قاعدة بيانات متجهزة بترقيات المشروع الفعلية
(`scripts.initialize_database` أو `alembic upgrade head`)، مش جداول مبسطة من
`create_all`، لأن الجداول المبسطة ناقصها حمايات حقيقية زي حماية سجل المراجعة
من التعديل والحذف. ملف `tests/conftest.py` بيوقف التشغيل فورًا لو القاعدة مش
على آخر ترقية. بعد أي تعديل فيه ترقية جديدة، جهّز قاعدة الاختبار تاني قبل
تشغيل الاختبارات.
