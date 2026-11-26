"""No authored Arabic template text node may stay Arabic under EN or ZH.

Uses the real catalog lookup on every template text/attribute node (Jinja
expressions replaced by a neutral token, dynamic stored data never judged).
Read-only guard; adds no business rule.
"""
import re
from pathlib import Path

import pytest

from app.i18n import _translate_value, translate_html
from scripts.i18n_audit import AR, Extractor, TEMPLATES


def _nodes():
    for tpl in sorted(TEMPLATES.glob('*.html')):
        s = tpl.read_text(encoding='utf-8')
        s = re.sub(r'\{#.*?#\}', '', s, flags=re.S)
        s = re.sub(r'<[^>]*>', lambda m: re.sub(r'\{%.*?%\}', '', m.group(0)), s, flags=re.S)
        s = re.sub(r'\{%.*?%\}', '\n<br>\n', s, flags=re.S)
        s = re.sub(r'\{\{.*?\}\}', '12345', s, flags=re.S)
        ex = Extractor()
        ex.feed(s)
        for _kind, raw in ex.items:
            if AR.search(raw):
                yield tpl.name, raw


# Block-title defaults are split by the extractor's block-to-<br> rewrite; they translate whole at runtime.
SPLIT_ARTIFACT = '<br>'


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_every_authored_arabic_template_node_translates(language):
    leaks = sorted({(name, raw.strip()) for name, raw in _nodes()
                    if SPLIT_ARTIFACT not in raw and AR.search(_translate_value(raw, language))})
    assert leaks == [], leaks


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_new_gap_strings_render_without_arabic(language):
    html = ('<html lang="ar"><title>استعادة كلمة المرور — نظام الموارد البشرية</title>'
            '<th>الحساب</th><dt>الصرف</dt><span>اسم المستخدم: <bdi>u</bdi></span></html>')
    assert AR.search(translate_html(html, language)) is None


def test_duplicate_employee_value_messages_translate():
    """main.py builds these 409 details from _DUPLICATE_FIELD_AR; each must localize."""
    import re
    from app.i18n import translate_text
    from app.main import _DUPLICATE_FIELD_AR
    assert set(_DUPLICATE_FIELD_AR) == {"national_id", "fingerprint_id", "passport_number"}
    for label in _DUPLICATE_FIELD_AR.values():
        message = f"القيمة المدخلة في {label} مستخدمة لموظف آخر."
        for lang in ("en", "zh"):
            assert not re.search("[\u0600-\u06ff]", translate_text(message, lang)), (lang, message)
