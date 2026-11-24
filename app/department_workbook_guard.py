"""Bounded workbook transaction operations, without changing caller policy."""
from functools import wraps

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError


def bounded_workbook_operation(function):
    @wraps(function)
    def call(db, *args, **kwargs):
        pg = db.get_bind().dialect.name == 'postgresql'
        previous = None
        failed_transaction = False
        if pg:
            previous = db.scalar(text('SHOW lock_timeout'))
            # Never lengthen an already stricter caller timeout.
            milliseconds = db.scalar(text("SELECT setting::bigint FROM pg_settings WHERE name='lock_timeout'"))
            if milliseconds == 0 or milliseconds > 3000:
                db.execute(text("SELECT set_config('lock_timeout', '3s', true)"))
        try:
            return function(db, *args, **kwargs)
        except HTTPException as exc:
            if exc.status_code != 409:
                raise
            raise ValueError('Workbook restore epoch changed; retry from current session.') from exc
        except SQLAlchemyError:
            # PostgreSQL aborts on timeout/error: caller MUST rollback. Attempting
            # SET here would mask the original error with InFailedSqlTransaction.
            failed_transaction = True
            raise
        finally:
            if pg and not failed_transaction:
                db.execute(text("SELECT set_config('lock_timeout', :value, true)"), {'value': previous})
    return call
