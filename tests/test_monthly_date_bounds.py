from datetime import date
import pytest
from app.report_builder import monthly_date_bounds,build_monthly_pdf
from app import main
from fastapi import HTTPException
from types import SimpleNamespace

@pytest.mark.parametrize('options',[{1},{2},{3},{4},{8},{9},{1,7,11}])
def test_unrepresentable_predecessor_refuses_before_db(options):
 with pytest.raises(ValueError,match='اختر شهرًا'):build_monthly_pdf(None,date(1,1,1),options)

@pytest.mark.parametrize('options',[{1},{5},{6},{7},{8},{9}])
def test_exclusive_end_refuses_before_db(options):
 with pytest.raises(ValueError,match='اختر شهرًا'):build_monthly_pdf(None,date(9999,12,1),options)

@pytest.mark.parametrize('start,options,expected',[(date(1,1,1),{7},date(1,1,31)),(date(1,2,1),{1},date(1,2,28)),(date(1,6,1),{8},date(1,6,30)),(date(9999,11,1),{9},date(9999,11,30)),(date(2024,2,20),{1,3},date(2024,2,29))])
def test_actual_arithmetic_neighbors_no_arbitrary_cutoff(start,options,expected):
 assert monthly_date_bounds(start,options)==(start.replace(day=1),expected)

@pytest.mark.parametrize('month,options',[('0001-01',[1]),('0001-05',[9]),('9999-12',[7])])
def test_route_refuses_before_renderer_or_save(monkeypatch,month,options):
 def forbidden(*a,**kw):raise AssertionError('must not render or save')
 monkeypatch.setattr(main,'build_monthly_pdf',forbidden)
 monkeypatch.setattr(main,'_save_report_copy',forbidden)
 request=SimpleNamespace(state=SimpleNamespace(language='en'))
 with pytest.raises(HTTPException) as err:main.reports_monthly_pdf(request,month,options,SimpleNamespace(role='system_admin'))
 assert err.value.status_code==400

from tests.test_monthly_report import client_factory

@pytest.mark.parametrize('language,expected',[
 ('ar','اختر شهرًا صالحًا للتقرير الشهري.'),
 ('en','Choose a valid month for the monthly report.'),
 ('zh','请为月度报表选择有效月份。'),
])
@pytest.mark.parametrize('month,option',[('0001-01',1),('0001-05',9),('9999-12',7)])
def test_real_http_localized_rejection(client_factory,monkeypatch,language,expected,month,option):
 def forbidden(*args,**kwargs):
  raise AssertionError('invalid date must not render or save')
 monkeypatch.setattr(main,'build_monthly_pdf',forbidden)
 monkeypatch.setattr(main,'_save_report_copy',forbidden)
 client=client_factory('system_admin')
 client.cookies.set('my_hr_language',language)
 response=client.get('/reports/monthly.pdf',params={'month':month,'opt':option})
 assert response.status_code==400
 assert response.json()=={'detail':expected}

@pytest.mark.parametrize('month,option',[('0001-01',7),('0001-02',1),('0001-06',8),('9999-11',9),('2024-02',1)])
def test_real_http_neighbors_still_render(client_factory,monkeypatch,month,option):
 saved=[]
 monkeypatch.setattr(main,'_save_report_copy',lambda *a,**kw:saved.append((a,kw)))
 response=client_factory('system_admin').get('/reports/monthly.pdf',params={'month':month,'opt':option})
 assert response.status_code==200
 assert response.content.startswith(b'%PDF')
 assert len(saved)==1
 assert saved[0][0][2:4]==monthly_date_bounds(date.fromisoformat(month+'-01'),{option})


@pytest.mark.parametrize('options',[[10],[11],[10,11]])
@pytest.mark.parametrize('month',['9999-12','0001-01','2026-10'])
def test_modifier_only_retains_no_data_rejection_order(client_factory,monkeypatch,month,options):
 def forbidden(*a,**kw):
  raise AssertionError('modifier-only requests must not reach date preflight')
 monkeypatch.setattr(main,'monthly_date_bounds',forbidden)
 response=client_factory('system_admin').get('/reports/monthly.pdf',params=[('month',month)]+[('opt',o) for o in options])
 assert response.status_code==403
