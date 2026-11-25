"""Normal named migrated database, real auth, no synthetic operation flags."""
import os,subprocess,sys
from pathlib import Path
from uuid import uuid4
import pytest
from sqlalchemy import create_engine,text
from app.db import engine


@pytest.mark.parametrize('lang',['ar','en','zh'])
def test_normal_monthly_earning_http(tmp_path,lang):
    name='test_month_earning_'+uuid4().hex[:12]
    admin=create_engine(engine.url.set(database='postgres'),isolation_level='AUTOCOMMIT')
    root=Path(__file__).resolve().parents[1]
    try:
        with admin.connect() as c:c.execute(text(f'CREATE DATABASE "{name}"'))
        env={**os.environ,'DATABASE_URL':engine.url.set(database=name).render_as_string(hide_password=False),'APP_ENV':'test','MONTH_EARNING_ACCEPTANCE_LANG':lang,'MONTH_EARNING_CAPTURE_DIR':str(tmp_path/'pixels')}
        for args in [['-m','alembic','upgrade','head'],['-m','pytest','tests/month_earning_http_acceptance.py','-q','--tb=short','--junitxml='+str(tmp_path/'http.xml')]]:
            result=subprocess.run([sys.executable,*args],cwd=root,env=env,capture_output=True,text=True,timeout=100)
            assert result.returncode==0,result.stdout+result.stderr
        from xml.etree import ElementTree
        suites=list(ElementTree.parse(tmp_path/'http.xml').getroot().iter('testsuite'))
        assert suites and sum(int(x.attrib['tests']) for x in suites)==1
        assert all(int(x.attrib.get(k,'0'))==0 for x in suites for k in ('errors','failures','skipped'))
        if os.environ.get('MONTH_EARNING_COPY_PIXELS'):
            import shutil
            output=Path(os.environ['MONTH_EARNING_COPY_PIXELS']);output.mkdir(parents=True,exist_ok=True)
            for path in (tmp_path/'pixels').glob('*.html'):shutil.copyfile(path,output/path.name)
    finally:
        with admin.connect() as c:
            c.execute(text('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=:n AND pid<>pg_backend_pid()'),dict(n=name))
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()
