"""Translator skip blocks and the attendance department-history continuity check (Mido leads, verified)."""
from datetime import date
from types import SimpleNamespace

import pytest

from app.i18n import translate_html


def test_nested_same_tag_inside_a_no_translate_block_stays_untranslated():
    html = '<div data-no-translate><div>الفرق</div>الفرق</div><p>الفرق</p>'
    out = translate_html(html, "en")
    assert out.startswith('<div data-no-translate><div>الفرق</div>الفرق</div>')
    assert out.endswith("<p>Difference</p>")


def test_void_tag_with_no_translate_does_not_stop_translation_of_the_rest():
    out = translate_html('<br data-no-translate><p>الفرق</p><input data-no-translate><p>الفرق</p>', "en")
    assert out.count("Difference") == 2


def test_ordinary_no_translate_block_still_works():
    out = translate_html('<span data-no-translate>الفرق</span><p>الفرق</p><script>var a="الفرق";</script>', "en")
    assert '<span data-no-translate>الفرق</span>' in out and '"الفرق"' in out and "<p>Difference</p>" in out
