from fastapi.testclient import TestClient

from app.main import app


def test_language_switch_persists_simplified_chinese_and_redirects_back():
    with TestClient(app) as client:
        page = client.get("/login")
        assert page.status_code == 200
        assert page.headers["content-language"] == "ar"
        assert '<html lang="ar" dir="rtl">' in page.text
        assert "fonts.googleapis.com" not in page.text
        assert "fonts.gstatic.com" not in page.text

        switched = client.get(
            "/language/zh",
            headers={"referer": "http://testserver/login"},
            follow_redirects=False,
        )
        assert switched.status_code == 303
        assert switched.headers["location"] == "/login"
        cookie = switched.headers["set-cookie"].lower()
        assert "my_hr_language=zh" in cookie
        assert "httponly" in cookie
        assert "samesite=lax" in cookie

        chinese_page = client.get("/login")
        assert chinese_page.status_code == 200
        assert chinese_page.headers["content-language"] == "zh-CN"
        assert '<html lang="zh-CN" dir="ltr">' in chinese_page.text
        assert "登录" in chinese_page.text


def test_language_switch_persists_english_and_keeps_login_post_available():
    with TestClient(app) as client:
        switched = client.get("/language/en", follow_redirects=False)
        assert switched.status_code == 303
        assert switched.headers["location"] == "/"

        page = client.get("/login")
        assert page.status_code == 200
        assert page.headers["content-language"] == "en"
        assert '<html lang="en" dir="ltr">' in page.text
        assert 'name="csrf_token"' in page.text
        assert 'name="username"' in page.text
        assert 'name="password"' in page.text


def test_invalid_language_cookie_falls_back_to_arabic():
    with TestClient(app) as client:
        client.cookies.set("my_hr_language", "unsupported")
        page = client.get("/login")
        assert page.status_code == 200
        assert page.headers["content-language"] == "ar"
        assert '<html lang="ar" dir="rtl">' in page.text


def test_language_switch_rejects_external_referer_as_redirect_target():
    with TestClient(app) as client:
        switched = client.get(
            "/language/en",
            headers={"referer": "https://example.invalid/phishing"},
            follow_redirects=False,
        )
        assert switched.status_code == 303
        assert switched.headers["location"] == "/"
