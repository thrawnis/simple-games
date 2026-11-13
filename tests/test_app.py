"""Run with:  pip install -r app/requirements.txt pytest && pytest tests"""
import importlib
import io
import re
import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))
PW = "Correct-Horse-Battery-9"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", PW)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KEEP_VERSIONS", "3")
    monkeypatch.setenv("MAX_UPLOAD_KB", "10")
    sys.modules.pop("app", None)
    mod = importlib.import_module("app")
    mod.app.config["TESTING"] = True
    c = mod.app.test_client()
    c.data_dir = tmp_path
    return c


def csrf(c, path):
    html = c.get(path).get_data(as_text=True)
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def login(c):
    r = c.post("/admin/login", data={"password": PW, "csrf": csrf(c, "/admin/login")})
    assert r.status_code == 302


def upload(c, slug, body, name="g.html"):
    return c.post(
        f"/admin/games/{slug}/upload",
        data={"csrf": csrf(c, "/admin"), "file": (io.BytesIO(body), name)},
        content_type="multipart/form-data",
    )


def create(c, name, slug="", body=b"<h1>v1</h1>"):
    return c.post(
        "/admin/games",
        data={"csrf": csrf(c, "/admin"), "name": name, "slug": slug,
              "file": (io.BytesIO(body), "game.html")},
        content_type="multipart/form-data",
    )


def test_weak_password_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "short")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    sys.modules.pop("app", None)
    with pytest.raises(SystemExit):
        importlib.import_module("app")


def test_admin_requires_login(client):
    assert client.get("/admin").status_code == 302
    assert client.post("/admin/games", data={}).status_code == 302


def test_wrong_password_and_lockout(client, monkeypatch):
    import app as mod
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    for _ in range(5):
        r = client.post("/admin/login", data={"password": "nope", "csrf": csrf(client, "/admin/login")})
        assert r.status_code == 401
    r = client.post("/admin/login", data={"password": PW, "csrf": csrf(client, "/admin/login")})
    assert r.status_code == 429


def test_csrf_required(client):
    login(client)
    r = client.post("/admin/games", data={"name": "x", "csrf": "bad"})
    assert r.status_code == 400


def test_create_list_play_and_history(client):
    login(client)
    assert create(client, "zeta", body=b"<p>Z</p>").status_code == 302
    assert create(client, "Alpha Game", body=b"<p>A1</p>").status_code == 302
    for i in range(2, 6):
        upload(client, "alpha-game", f"<p>A{i}</p>".encode())

    html = client.get("/").get_data(as_text=True)
    assert html.index("Alpha Game") < html.index("zeta")

    r = client.get("/play/alpha-game")
    assert r.data == b"<p>A5</p>"
    assert "sandbox allow-scripts" in r.headers["Content-Security-Policy"]
    assert "allow-same-origin" not in r.headers["Content-Security-Policy"]

    versions = sorted(p.name for p in (client.data_dir / "games/alpha-game/versions").iterdir())
    assert versions == ["000003.html", "000004.html", "000005.html"]  # KEEP_VERSIONS=3
    assert client.get("/admin/games/alpha-game/download/1").status_code == 404
    d = client.get("/admin/games/alpha-game/download/3")
    assert d.data == b"<p>A3</p>" and "attachment" in d.headers["Content-Disposition"]

    client.post("/admin/games/alpha-game/restore/3", data={"csrf": csrf(client, "/admin")})
    assert client.get("/play/alpha-game").data == b"<p>A3</p>"


def test_old_versions_not_public(client):
    login(client)
    create(client, "Game", "game")
    upload(client, "game", b"<p>2</p>")
    client.post("/admin/logout", data={"csrf": csrf(client, "/admin")})
    assert client.get("/admin/games/game/download/1").status_code == 302
    assert client.get("/play/game").data == b"<p>2</p>"


def test_rejects_bad_uploads(client):
    login(client)
    create(client, "Game", "game")
    upload(client, "game", b"\x89PNG\x00\x00", "x.html")
    upload(client, "game", b"<p>x</p>", "x.exe")
    upload(client, "game", b"\xff\xfe bad utf8", "x.html")
    upload(client, "game", b"a" * 20000, "x.html")
    assert len(list((client.data_dir / "games/game/versions").iterdir())) == 1


@pytest.mark.parametrize("slug", ["../etc", "..", "a/b", "a b", "-x", "x-", "x" * 70, "%2e%2e"])
def test_bad_slugs(client, slug):
    login(client)
    create(client, "Name", slug)
    assert [p.name for p in (client.data_dir / "games").iterdir()] == []
    assert client.get(f"/play/{slug}").status_code == 404


def test_delete(client):
    login(client)
    create(client, "Game", "game")
    client.post("/admin/games/game/delete", data={"csrf": csrf(client, "/admin"), "confirm": "nope"})
    assert client.get("/play/game").status_code == 200
    client.post("/admin/games/game/delete", data={"csrf": csrf(client, "/admin"), "confirm": "game"})
    assert client.get("/play/game").status_code == 404


def test_names_escaped(client):
    login(client)
    create(client, "<script>alert(1)</script>", "xss")
    html = client.get("/").get_data(as_text=True)
    assert "<script>alert(1)" not in html and "&lt;script&gt;" in html
