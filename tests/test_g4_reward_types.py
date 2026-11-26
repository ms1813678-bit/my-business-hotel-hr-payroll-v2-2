"""Reward types: the 19-item list relayed via Main (this docstring is not an approval record; 13 leave-day reward and 18 real
promotion/raise are NOT reward categories). 1-11 are financial (money reaches payroll when approved);
12 gift/voucher and 19 custom are non-financial by default with an optional financial flag. SQLite route fixture."""
import pytest
from sqlalchemy import select

from app.models import StaffAction
from app.staff_actions_workflow import (CATEGORY_LABELS_AR, FINANCIAL_REWARD_CATEGORIES,
                                        NONFINANCIAL_REWARD_CATEGORIES, REWARD_CATEGORIES)
from tests.test_staff_actions_routes import csrf, login, staff_action_client  # noqa: F401

FIN = sorted(FINANCIAL_REWARD_CATEGORIES)
NONFIN = sorted(NONFINANCIAL_REWARD_CATEGORIES)


def _post(client, category, **extra):
    token = csrf(client.get("/staff-actions"))
    data = {"csrf_token": token, "employee_id": client.test_employee_id, "kind": "reward",
            "category": category, "title": f"t-{category}", "reason": "Synthetic reason"}
    data.update(extra)
    return client.post("/staff-actions", data=data, follow_redirects=False)


def test_the_list_has_exactly_the_approved_types():
    assert len(FIN) == 11 and len(NONFIN) == 6 and len(REWARD_CATEGORIES) == 17
    assert not {"leave_reward", "promotion_raise", "salary_raise"} & REWARD_CATEGORIES
    assert all(CATEGORY_LABELS_AR.get(c) for c in REWARD_CATEGORIES)


@pytest.mark.parametrize("category", FIN)
def test_financial_types_need_amount_and_month_then_create(staff_action_client, category):
    c = staff_action_client; login(c)
    assert "error=create" in _post(c, category).headers["location"]
    r = _post(c, category, is_financial="yes", amount="150.00", payroll_month="2026-10-01")
    assert "error" not in r.headers["location"]
    with c.test_session_factory() as db:
        a = db.scalar(select(StaffAction).where(StaffAction.category == category))
        assert a.is_financial is True and str(a.amount) == "150.00"


@pytest.mark.parametrize("category", NONFIN)
def test_nonfinancial_types_create_without_money_by_default(staff_action_client, category):
    c = staff_action_client; login(c)
    r = _post(c, category)
    assert "error" not in r.headers["location"]
    with c.test_session_factory() as db:
        a = db.scalar(select(StaffAction).where(StaffAction.category == category))
        assert a.is_financial is False and a.amount is None


def test_gift_and_custom_can_be_flagged_financial_but_certificate_form_has_no_money_field_needed(staff_action_client):
    c = staff_action_client; login(c)
    for cat in ("gift_voucher", "custom"):
        r = _post(c, cat, is_financial="yes", amount="75.00", payroll_month="2026-10-01")
        assert "error" not in r.headers["location"]


@pytest.mark.parametrize("category", ["leave_reward", "promotion_raise", "nonsense"])
def test_excluded_and_unknown_types_are_refused(staff_action_client, category):
    c = staff_action_client; login(c)
    assert "error=create" in _post(c, category).headers["location"]


def test_page_offers_every_type_with_its_arabic_label(staff_action_client):
    c = staff_action_client; login(c)
    page = c.get("/staff-actions").text
    for cat in REWARD_CATEGORIES:
        assert f'data-category="{cat}"' in page and f'value="{cat}"' in page
        assert CATEGORY_LABELS_AR[cat] in page


@pytest.mark.parametrize("lang,cookie", [("en", "en"), ("zh", "zh")])
def test_every_category_label_is_pure_in_english_and_chinese(staff_action_client, lang, cookie):
    import re
    c = staff_action_client; login(c)
    # a stored action of every category, so the table path (category_labels) is checked too
    for cat in sorted(REWARD_CATEGORIES):
        extra = {"is_financial": "yes", "amount": "10.00", "payroll_month": "2026-10-01"} if cat in FINANCIAL_REWARD_CATEGORIES else {}
        assert "error" not in _post(c, cat, **extra).headers["location"]
    page = c.get("/staff-actions", cookies={"my_hr_language": cookie}).text
    from app.i18n import EN, ZH
    table = EN if lang == "en" else ZH
    for cat in REWARD_CATEGORIES:
        label = CATEGORY_LABELS_AR[cat]
        assert table.get(label), (cat, label)
        assert label not in page, (lang, cat, "Arabic label leaked")
        assert table[label] in page, (lang, cat)
        assert not re.search("[\u0600-\u06ff]", table[label])


def test_arabic_page_keeps_arabic_labels_only(staff_action_client):
    c = staff_action_client; login(c)
    page = c.get("/staff-actions").text
    from app.i18n import EN
    for cat in REWARD_CATEGORIES:
        assert CATEGORY_LABELS_AR[cat] in page
        assert EN[CATEGORY_LABELS_AR[cat]] not in page
