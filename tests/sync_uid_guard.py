"""Behavioral UID suite guard, rolled back after every probe."""
import uuid

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

TABLES = ("departments", "employees", "leave_types", "leave_records")


def verify_sync_uid_enforcement(engine):
    """Check defaults, canonical format, uniqueness and immutability in this schema."""
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            suffix = uuid.uuid4().hex[:12]
            department = connection.execute(text(
                "INSERT INTO departments(name,active) VALUES (:name,true) RETURNING id"
            ), {"name": "uid_guard_" + suffix}).scalar_one()
            employee = connection.execute(text(
                "INSERT INTO employees(code,full_name,department_id,position,hire_date,status,"
                "basic_salary,daily_hours,created_at) VALUES (:code,'Synthetic UID guard',:d,"
                "'guard','2026-01-01','active',1,8,now()) RETURNING id"
            ), {"code": "G" + suffix, "d": department}).scalar_one()
            leave_type = connection.execute(text(
                "INSERT INTO leave_types(code,name_ar,name_en,is_paid,tracks_balance,active) "
                "VALUES (:code,'guard','guard',true,true,true) RETURNING id"
            ), {"code": "uid_guard_" + suffix}).scalar_one()
            leave = connection.execute(text(
                "INSERT INTO leave_records(employee_id,leave_type_id,start_date,end_date,"
                "calendar_days,charged_days,reason,status,created_by,created_at) "
                "VALUES (:e,:t,'2026-01-01','2026-01-01',1,1,'guard','draft','guard',now()) RETURNING id"
            ), {"e": employee, "t": leave_type}).scalar_one()
            ids = dict(zip(TABLES, (department, employee, leave_type, leave)))
            for table, row_id in ids.items():
                value = connection.execute(text(
                    f"SELECT sync_uid FROM {table} WHERE id=:id"
                ), {"id": row_id}).scalar_one()
                parsed = uuid.UUID(value)
                if parsed.version != 4 or str(parsed) != value:
                    raise RuntimeError(f"sync_uid default is not canonical UUID4: {table}")
                connection.execute(text(
                    f"UPDATE {table} SET sync_uid=sync_uid WHERE id=:id"
                ), {"id": row_id})
                with connection.begin_nested() as probe:
                    try:
                        connection.execute(text(
                            f"UPDATE {table} SET sync_uid=:u WHERE id=:id"
                        ), {"id": row_id, "u": str(uuid.uuid4())})
                    except DBAPIError as error:
                        probe.rollback()
                        if "sync_uid is immutable" not in str(error):
                            raise
                    else:
                        raise RuntimeError(f"sync_uid guard did not block an UPDATE: {table}")
                # Clone a synthetic row with a new integer key and bad/duplicate UID.
                # Other unique business keys need distinct values to avoid false passes.
                clone = {"departments": {"name": "clone_" + suffix},
                         "employees": {"code": "C" + suffix},
                         "leave_types": {"code": "clone_" + suffix},
                         "leave_records": {}}[table]
                columns = connection.execute(text(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() "
                    "AND table_name=:t AND column_name<>'id' ORDER BY ordinal_position"
                ), {"t": table}).scalars().all()
                expressions = [":u" if c == "sync_uid" else ":" + c if c in clone else '"' + c + '"'
                               for c in columns]
                quoted = ",".join('"' + c + '"' for c in columns)
                sql = text(f"INSERT INTO {table} ({quoted}) SELECT {','.join(expressions)} "
                           f"FROM {table} WHERE id=:id")
                for bad, expected in ((None, "23502"), ("not-a-uuid", "23514"), (value, "23505")):
                    with connection.begin_nested() as probe:
                        try:
                            connection.execute(sql, {"id": row_id, "u": bad, **clone})
                        except DBAPIError as error:
                            probe.rollback()
                            if getattr(error.orig, "sqlstate", None) != expected:
                                raise
                        else:
                            raise RuntimeError(f"sync_uid guard did not block invalid INSERT: {table}")
        finally:
            transaction.rollback()
