"""Local presentation-only invariants for the owner's selected theme and logos."""
from pathlib import Path
import hashlib
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / 'app/static'


def test_selected_hony_variant_geometry_matches():
    dark = ET.parse(STATIC / 'hony-logo.svg').getroot()
    light = ET.parse(STATIC / 'hony-logo-light.svg').getroot()
    assert dark.attrib['viewBox'] == light.attrib['viewBox'] == '0 0 160 60'
    assert [e.attrib.get('d') for e in dark.iter()] == [e.attrib.get('d') for e in light.iter()]
    circles = list(dark.iter('{http://www.w3.org/2000/svg}circle'))
    assert len(circles) == 2
    assert circles[1].attrib['r'] == '3.4'
    assert any(e.attrib.get('d') == 'M45 11Q58 2 71 11' for e in dark.iter())


def test_shared_brand_surfaces_use_correct_variants():
    base = (ROOT / 'app/templates/base.html').read_text()
    login = (ROOT / 'app/templates/login.html').read_text()
    assert 'sidebar-product-logo' in base
    assert '/static/hony-logo-light.svg' in base
    assert '/static/hony-logo.svg' in base and '/static/hony-logo.svg' in login
    assert 'src="/static/logo-badge.png"' in base
    assert 'src="/static/logo.png"' in login


def test_decorations_are_not_inputs_or_print_content():
    css = (STATIC / 'app.css').read_text()
    assert 'pointer-events: none' in css
    assert '.theme-corners, .theme-lotus { display: none !important; }' in css
    for file in ['base.html', 'login.html']:
        text = (ROOT / 'app/templates' / file).read_text()
        assert text.count('class="theme-corners" aria-hidden="true"') == 1
    assert 'theme-lotus' not in (ROOT / 'app/templates/base.html').read_text()


def test_approved_hotel_print_asset_is_original():
    assert hashlib.sha256((STATIC / 'hotel-logo-print-approved.png').read_bytes()).hexdigest() == '29f48c7b17551b330e9722d3465abc1b964ea0872bb5ac5a5007cf23a4f0e3a8'


def test_desktop_icon_is_valid_multiresolution_ico():
    import struct
    data = (STATIC / 'hony.ico').read_bytes()
    reserved, kind, count = struct.unpack('<HHH', data[:6])
    assert reserved == 0 and kind == 1 and count == 7
    assert all((STATIC / p).exists() for p in ['theme/corner.svg', 'theme/lotus.svg'])
