"""Group 8أ boundary oracle: /reports/monthly.pdf at representable-date edges.

Isolated addition from the group-8 lane (2026-11-26), base commit
e60b73f269f3cf203c1cf7d86e437359970aa233. No shared file changed; no new
policy. Expectations below are the ACTUAL behavior of the pinned commit,
verified live through the HTTP route before this module was written, so the
retained "extreme month date overflow" lead in
docs/MONTHLY_REPORT_FINDINGS_1697.md gets an executable oracle instead of a
note:

- Option sets that compute predecessor months (comparison sections {1,2,3,4}
  and six-month charts {8,9}) are refused with 400 and the Arabic detail at
  both date edges, for every role whose scope admits them.
- Sections without predecessor arithmetic ({5,6,7}, modifiers 10/11) render
  200 even at year 1, including on an empty database.
- Modifiers alone ({10}, {11}, {10,11}) carry no data and are refused 403 at
  any month, before any date math.
- The 400 detail is exactly the user-facing Arabic message; no internal
  (English) arithmetic error leaks into the response.
"""
from datetime import date
import re
from urllib.parse import quote

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.dependencies import get_current_admin
from app.main import app
from app.models import AdminAccount

ARABIC_INVALID_MONTH = "اختر شهرًا صالحًا للتقرير الشهري."

# Options whose sections compute predecessor months inside the build path.
PREDECESSOR_OPTIONS = {1, 2, 3, 4, 8, 9}
# Options whose sections never compute predecessors (verified in source: the
# only two monthly_calendar_comparison call sites are payroll and attendance;
# the charts are options 8/9). Modifiers 10/11 add no section of their own.
NON_PREDECESSOR_DATA_OPTIONS = {5, 6, 7}
MODIFIER_ONLY_SETS = [{10}, {11}, {10, 11}]


@pytest.fixture()
def empty_client(monkeypatch):
    """HTTP client over an EMPTY database (no seed): boundary behavior must
    not depend on data presence."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    import app.main as main_mod
    monkeypatch.setattr(main_mod, "SessionLocal", factory)
    client = TestClient(app, raise_server_exceptions=False)

    def as_role(role: str):
        account = AdminAccount(
            username=f"g8a-{role}", role=role, active=True,
            display_name="حدود", password_hash="x",
        )
        app.dependency_overrides[get_current_admin] = lambda: account
        return client

    try:
        yield as_role
    finally:
        client.close()
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _get(client, month, options):
    return client.get(
        "/reports/monthly.pdf",
        params=[("month", month)] + [("opt", str(o)) for o in sorted(options)],
    )


def _assert_arabic_400(response):
    assert response.status_code == 400
    assert response.json()["detail"] == ARABIC_INVALID_MONTH


# --- lower edge: year 1 -----------------------------------------------------

@pytest.mark.parametrize("option", sorted(PREDECESSOR_OPTIONS))
def test_lower_edge_single_predecessor_option_refused(empty_client, option):
    client = empty_client("system_admin")
    _assert_arabic_400(_get(client, "0001-01", {option}))


@pytest.mark.parametrize(
    "options", [{1, 10}, {2, 10}, {3, 10}, {4, 10}, {8, 10}, {9, 11}, {1, 3, 8, 10, 11}],
)
def test_lower_edge_predecessor_combinations_refused(empty_client, options):
    client = empty_client("system_admin")
    _assert_arabic_400(_get(client, "0001-01", options))


@pytest.mark.parametrize(
    "options", [{5}, {6}, {7}, {5, 10}, {6, 10}, {7, 10}, {5, 6, 7, 10, 11}],
)
def test_lower_edge_non_predecessor_sections_render(empty_client, options):
    client = empty_client("system_admin")
    response = _get(client, "0001-01", options)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")
    # The route uses the host's strftime, whose year-1 padding differs by OS.
    expected_name=f"MY_HR_Monthly_{date(1,1,1).strftime('%Y-%m')}.pdf"
    header=response.headers["content-disposition"]
    assert header==f"attachment; filename*=UTF-8''{quote(expected_name)}"
    _assert_lower_edge_pdf_disposition(header)


def test_chart_minimum_lower_bound_is_five_months_back(empty_client):
    client = empty_client("system_admin")
    # 0001-05 with a six-month chart needs months before year 1: refused.
    _assert_arabic_400(_get(client, "0001-05", {8}))
    # 0001-06 reaches exactly 0001-01: representable, renders.
    response = _get(client, "0001-06", {8})
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")


# --- upper edge: year 9999 --------------------------------------------------

@pytest.mark.parametrize("option", [1, 2, 3, 4, 5, 6, 7, 8, 9])
def test_upper_edge_every_data_option_refused(empty_client, option):
    """The exclusive end of 9999-12 is not representable, so every data
    option (even leave-only, which needs no predecessor) is refused."""
    client = empty_client("system_admin")
    _assert_arabic_400(_get(client, "9999-12", {option}))


def test_upper_edge_all_options_refused(empty_client):
    client = empty_client("system_admin")
    _assert_arabic_400(_get(client, "9999-12", set(range(1, 12))))


# --- modifiers alone: 403 at any month, before date math --------------------

@pytest.mark.parametrize("options", MODIFIER_ONLY_SETS)
@pytest.mark.parametrize("month", ["0001-01", "2026-10", "9999-12"])
def test_modifier_only_sets_refused_403_before_date_math(empty_client, options, month):
    client = empty_client("system_admin")
    response = _get(client, month, options)
    assert response.status_code == 403
    assert response.json()["detail"] == "هذا الجزء غير متاح لصلاحية حسابك."


def test_all_options_render_in_all_three_languages():
    """8أ/8ت overlap: the full 11-option monthly PDF builds in ar/en/zh on
    an empty database (CJK font present at app/static/fonts/)."""
    from datetime import date as _date
    from sqlalchemy.orm import Session
    import app.models  # noqa: F401  (register every table before create_all)

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            from app.report_builder import MONTHLY_ALL_OPTIONS, build_monthly_pdf
            for language in ("ar", "en", "zh"):
                content = build_monthly_pdf(
                    db, _date(2026, 10, 1), set(MONTHLY_ALL_OPTIONS), language=language,
                )
                assert content.startswith(b"%PDF"), language
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


# --- role-scoped edges -------------------------------------------------------

def test_assistant_edge_behavior(empty_client):
    client = empty_client("assistant_manager")
    # Assistant scope {3,4,7,9,10,11}: charts/comparison need predecessors.
    _assert_arabic_400(_get(client, "0001-01", {3, 10}))
    _assert_arabic_400(_get(client, "0001-01", {9}))
    _assert_arabic_400(_get(client, "9999-12", {3, 7}))
    # Leave-only at year 1 renders (aggregate path, no predecessors).
    response = _get(client, "0001-01", {7})
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")


def test_finance_edge_behavior(empty_client):
    client = empty_client("finance")
    # Finance payroll comparison at year 1: refused with the Arabic message.
    _assert_arabic_400(_get(client, "0001-01", {1, 10}))
    _assert_arabic_400(_get(client, "0001-01", {2}))
    # Pinned actual: _REPORT_TYPES_BY_ROLE[finance] covers all five monthly
    # data types (only the assistant is restricted), so leave renders.
    response = _get(client, "0001-01", {7})
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")


def _assert_lower_edge_pdf_disposition(header):
    """Exact attachment/encoding/name/month, allowing only observed year padding."""
    assert re.fullmatch(r"attachment; filename\*=UTF-8''MY_HR_Monthly_(?:1|0001)-01\.pdf",header)


@pytest.mark.parametrize('year',['1','0001'],ids=['unpadded-posix','padded-windows'])
def test_lower_edge_filename_observed_platform_oracles(year):
    _assert_lower_edge_pdf_disposition(f"attachment; filename*=UTF-8''MY_HR_Monthly_{year}-01.pdf")


@pytest.mark.parametrize('header',[
    "attachment; filename*=UTF-8''MY_HR_Monthly_0001-02.pdf",
    "attachment; filename*=UTF-8''MY_HR_Monthly_0002-01.pdf",
    "attachment; filename*=UTF-8''MY_HR_Monthly_01-01.pdf",
    "attachment; filename*=UTF-8''OTHER_0001-01.pdf",
    "inline; filename*=UTF-8''MY_HR_Monthly_0001-01.pdf",
    "attachment; filename=MY_HR_Monthly_0001-01.pdf",
    "attachment; filename*=UTF-8''MY_HR_Monthly_0001-01.pdf.extra",
],ids=['wrong-month','wrong-year','unobserved-padding','wrong-prefix','inline','missing-encoding','suffix'])
def test_lower_edge_filename_contract_rejects_mutations(header):
    with pytest.raises(AssertionError):_assert_lower_edge_pdf_disposition(header)
