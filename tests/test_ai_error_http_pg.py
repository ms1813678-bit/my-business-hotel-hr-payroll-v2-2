"""Real migrated PG, authenticated/CSRF AI HTTP, no actual model or dependency bypass."""
from datetime import date
from decimal import Decimal
from html import unescape
import re
from pathlib import Path
import uuid

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text, func
from sqlalchemy.orm import sessionmaker

from app import main, ai_gateway, ai_agent, ai_assistant, ai_smart_scan
from app.db import engine as configured_engine, get_db
from app.models import AdminAccount, Department, Employee, LeaveType, LeaveRecord, AICommandApproval, AuditLog
from app.security import hash_password
from app.ai_error_display import known_reason
from tests.test_routes_security import csrf_from


@pytest.fixture
def pg_http(monkeypatch):
    if configured_engine.dialect.name != "postgresql":
        pytest.skip("migrated PostgreSQL required")
    schema = "d8_http_" + uuid.uuid4().hex[:12]
    with configured_engine.begin() as c:
        c.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(configured_engine.url, connect_args={"options": "-csearch_path=" + schema})
    factory = sessionmaker(engine, autoflush=False)
    try:
        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        with engine.begin() as c:
            cfg.attributes["connection"] = c
            command.upgrade(cfg, "head")
        with factory.begin() as db:
            db.add(AdminAccount(username="http-admin", password_hash=hash_password("Synthetic-HTTP-Password-123!"), role="system_admin", active=True))
            db.add(AdminAccount(username="http-finance", password_hash=hash_password("Synthetic-HTTP-Password-123!"), role="finance", active=True))
            d = Department(name="Synthetic HTTP")
            db.add(d);db.flush()
            e = Employee(code="SYN-HTTP",full_name="Synthetic",department_id=d.id,position="p",hire_date=date(2020,1,1),basic_salary=100,daily_hours=8,status="active")
            t = LeaveType(code="synthetic-http",name_ar="Synthetic",name_en="Synthetic",tracks_balance=False,active=True)
            db.add_all([e,t]);db.flush()
            ids = e.id,t.id
        def get():
            with factory() as db:
                yield db
        main.app.dependency_overrides[get_db] = get
        for module in (main, ai_gateway, ai_agent, ai_smart_scan):
            if hasattr(module,"SessionLocal"):
                monkeypatch.setattr(module,"SessionLocal",factory)
        monkeypatch.setattr(main,"engine",engine)
        monkeypatch.setattr(main,"local_model_status",lambda *a,**k:{"available":False,"server":False,"model":"Synthetic","message":""})
        with TestClient(main.app) as client:
            yield client,factory,ids
    finally:
        main.app.dependency_overrides.clear()
        engine.dispose()
        with configured_engine.begin() as c:
            c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


def login(client,username="http-admin"):
    page=client.get("/login")
    response=client.post("/login",data={"username":username,"password":"Synthetic-HTTP-Password-123!","csrf_token":csrf_from(page)},follow_redirects=False)
    assert response.status_code==303,response.text


def proposal(factory,ids,lang,monkeypatch,status="draft",actor="http-admin"):
    with factory.begin() as db:
        row=LeaveRecord(employee_id=ids[0],leave_type_id=ids[1],start_date=date(2027,1,1),end_date=date(2027,1,1),calendar_days=1,charged_days=Decimal(1),reason="Synthetic",status=status,created_by="synthetic")
        db.add(row);db.flush();rid=row.id
    monkeypatch.setattr(ai_gateway,"build_plan",lambda *a,**k:{"action":"confirm_leave","record_id":rid,"reason":"Synthetic"})
    result=ai_gateway.prepare_command("Synthetic confirm",actor,lang)
    return result["token"],rid


def receipt(response):
    match=re.search(r'name="review_receipt" value="([a-f0-9]{64})"',response.text)
    assert match,response.text[-3000:]
    return match.group(1)


@pytest.mark.parametrize("lang",["ar","en","zh"])
def test_pg_http_actionable_business_refusal_no_partial_write(pg_http,lang,monkeypatch):
    client,factory,ids=pg_http;login(client)
    client.cookies.set("my_hr_language",lang)
    token,rid=proposal(factory,ids,lang,monkeypatch,status="confirmed")
    page=client.get("/ai")
    review=client.post("/ai/review",data={"csrf_token":csrf_from(page),"token":token})
    proof=receipt(review)
    with factory() as db:
        before=db.scalar(select(func.count()).select_from(AuditLog))
    result=client.post("/ai/approve",data={"csrf_token":csrf_from(review),"token":token,"review_receipt":proof})
    assert result.status_code==200
    expected=known_reason(ValueError("Only a draft leave record can be confirmed."),lang)
    assert expected in unescape(result.text)
    assert 'action="/ai/approve"' not in result.text
    with factory() as db:
        assert db.get(LeaveRecord,rid).status=="confirmed"
        assert db.scalar(select(AICommandApproval)).status=="pending"
        assert db.scalar(select(func.count()).select_from(AuditLog))==before


@pytest.mark.parametrize("lang",["ar","en","zh"])
def test_pg_http_success_once_replay_and_actor_binding(pg_http,lang,monkeypatch):
    client,factory,ids=pg_http;login(client);client.cookies.set("my_hr_language",lang)
    token,rid=proposal(factory,ids,lang,monkeypatch)
    page=client.get("/ai")
    review=client.post("/ai/review",data={"csrf_token":csrf_from(page),"token":token})
    proof=receipt(review)
    data={"csrf_token":csrf_from(review),"token":token,"review_receipt":proof}
    result=client.post("/ai/approve",data=data)
    assert result.status_code==200
    with factory() as db:
        assert db.get(LeaveRecord,rid).status=="confirmed"
        assert db.scalar(select(AICommandApproval)).status=="consumed"
        before=db.scalar(select(func.count()).select_from(AuditLog))
    again=client.post("/ai/approve",data=data)
    assert again.status_code==200
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(AuditLog))==before
    other,_=proposal(factory,ids,lang,monkeypatch,actor="someone-else")
    response=client.post("/ai/review",data={"csrf_token":csrf_from(again),"token":other})
    assert response.status_code==200 and 'action="/ai/approve"' not in response.text


def test_pg_http_real_csrf_and_role_required(pg_http,monkeypatch):
    client,factory,ids=pg_http
    login(client)
    token,_=proposal(factory,ids,"en",monkeypatch)
    for path in ("/ai","/ai/review","/ai/approve","/ai/reject"):
        response=client.post(path,data={"token":token,"question":"Synthetic"})
        assert response.status_code==403
    logout=client.get("/ai")
    assert client.post("/logout",data={"csrf_token":csrf_from(logout)},follow_redirects=False).status_code==303
    login(client,"http-finance")
    page=client.get("/")
    assert client.get("/ai").status_code==403
    for path in ("/ai","/ai/review","/ai/approve","/ai/reject"):
        response=client.post(path,data={"token":token,"question":"Synthetic","csrf_token":csrf_from(page)})
        assert response.status_code==403
    with factory() as db:
        assert db.scalar(select(AICommandApproval)).status=="pending"
