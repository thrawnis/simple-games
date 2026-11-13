"""Simple Games Engine - a tiny, hardened host for single-file HTML games."""

from __future__ import annotations

import hmac
import os
import secrets
import sys
import threading
import time
from functools import wraps

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from storage import Storage, StorageError, slugify, validate_html

# ---------------------------------------------------------------------------
# Configuration (from settings.env via docker compose `env_file`)
# ---------------------------------------------------------------------------
PLACEHOLDER_PASSWORDS = {"change-me", "changeme", "password", "replace-with-a-strong-password"}


def _env_bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        sys.exit(f"FATAL: {name} must be an integer")


def password_problems(pw: str) -> list[str]:
    problems = []
    if pw.lower() in PLACEHOLDER_PASSWORDS:
        problems.append("it is still the example placeholder")
    if len(pw) < 16:
        problems.append("it must be at least 16 characters long")
    classes = sum(
        [
            any(c.islower() for c in pw),
            any(c.isupper() for c in pw),
            any(c.isdigit() for c in pw),
            any(not c.isalnum() for c in pw),
        ]
    )
    if len(pw) < 24 and classes < 3:
        problems.append(
            "passwords shorter than 24 characters need at least 3 of: "
            "lowercase, uppercase, digits, symbols"
        )
    if len(set(pw)) < 8:
        problems.append("it has too few distinct characters")
    return problems


ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
_problems = password_problems(ADMIN_PASSWORD)
if _problems:
    sys.exit(
        "FATAL: ADMIN_PASSWORD in settings.env is not strong enough: "
        + "; ".join(_problems)
        + ". Run ./setup.sh to generate one."
    )

DATA_DIR = os.environ.get("DATA_DIR", "/data")
MAX_UPLOAD_KB = _env_int("MAX_UPLOAD_KB", 5120)
KEEP_VERSIONS = _env_int("KEEP_VERSIONS", 50)
SITE_TITLE = os.environ.get("SITE_TITLE", "Simple Games")
SESSION_HOURS = _env_int("ADMIN_SESSION_HOURS", 8)
COOKIE_SECURE = _env_bool("COOKIE_SECURE", True)
# Requests through Cloudflare (Tunnel) always carry Cloudflare's headers,
# which visitors cannot strip. By default the admin area is refused for them,
# so it is only reachable on the local network (e.g. via Nginx Proxy Manager).
ADMIN_VIA_CLOUDFLARE = _env_bool("ADMIN_VIA_CLOUDFLARE", False)
CLOUDFLARE_HEADERS = ("Cf-Ray", "Cf-Connecting-Ip", "Cf-Visitor")
ALLOW_EXTERNAL = _env_bool("GAMES_ALLOW_EXTERNAL_RESOURCES", False)
LOGIN_MAX_FAILS = _env_int("LOGIN_MAX_FAILURES", 5)
LOGIN_WINDOW = _env_int("LOGIN_LOCKOUT_MINUTES", 15) * 60

# Secret for signing session cookies. If not provided, a random one is made
# at start-up (admins are simply logged out when the container restarts).
SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

try:
    storage = Storage(DATA_DIR, keep_versions=KEEP_VERSIONS)
except PermissionError:
    sys.exit(
        f"FATAL: cannot write to {DATA_DIR}. On the host run: "
        "sudo chown -R 10001:10001 ./data   (or run ./setup.sh)"
    )

app = Flask(__name__)
app.config.update(
    SECRET_KEY=SECRET_KEY,
    MAX_CONTENT_LENGTH=MAX_UPLOAD_KB * 1024 + 64 * 1024,  # file + form overhead
    SESSION_COOKIE_NAME="sg_admin",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=COOKIE_SECURE,
    SESSION_COOKIE_PATH="/admin",
    PERMANENT_SESSION_LIFETIME=SESSION_HOURS * 3600,
)

if _env_bool("TRUST_PROXY", False):
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------
APP_CSP = (
    "default-src 'none'; style-src 'self'; img-src 'self' data:; "
    "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)

if ALLOW_EXTERNAL:
    _ext = " https:"
else:
    _ext = ""

# Games run in a sandbox with an opaque ("null") origin. They can execute
# their own scripts but cannot read this site's cookies, call the admin
# area as the logged-in admin, or (by default) talk to any server at all.
GAME_CSP = (
    "sandbox allow-scripts allow-pointer-lock allow-modals allow-downloads; "
    "default-src 'none'; "
    f"script-src 'unsafe-inline' 'unsafe-eval' data: blob:{_ext}; "
    f"style-src 'unsafe-inline' data:{_ext}; "
    f"img-src data: blob:{_ext}; "
    f"media-src data: blob:{_ext}; "
    f"font-src data:{_ext}; "
    f"connect-src data: blob:{_ext}; "
    "worker-src blob: data:; "
    "form-action 'none'; base-uri 'none'; frame-ancestors 'none'"
)


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("Content-Security-Policy", APP_CSP)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    resp.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    resp.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=(), usb=(), payment=(), "
        "clipboard-read=(), display-capture=()"
    )
    if request.path.startswith("/admin"):
        resp.headers["Cache-Control"] = "no-store"
    resp.headers.pop("Server", None)
    return resp


@app.before_request
def block_admin_from_internet():
    if (
        not ADMIN_VIA_CLOUDFLARE
        and request.path.startswith("/admin")
        and any(h in request.headers for h in CLOUDFLARE_HEADERS)
    ):
        abort(404)


# ---------------------------------------------------------------------------
# Auth, CSRF and login throttling
# ---------------------------------------------------------------------------
_fail_lock = threading.Lock()
_failures: dict[str, list[float]] = {}


def _client_ip() -> str:
    return request.remote_addr or "unknown"


def _recent_failures(ip: str) -> list[float]:
    cutoff = time.monotonic() - LOGIN_WINDOW
    with _fail_lock:
        times = [t for t in _failures.get(ip, []) if t > cutoff]
        if times:
            _failures[ip] = times
        else:
            _failures.pop(ip, None)
        return times


def _record_failure(ip: str) -> None:
    with _fail_lock:
        _failures.setdefault(ip, []).append(time.monotonic())
        if len(_failures) > 10000:  # bound memory
            _failures.pop(next(iter(_failures)))


def csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token
app.jinja_env.globals["site_title"] = SITE_TITLE


def check_csrf() -> None:
    sent = request.form.get("csrf", "")
    expected = session.get("csrf", "")
    if not expected or not hmac.compare_digest(sent, expected):
        abort(
            400,
            "Invalid or expired form. Reload the page and try again. "
            "(If you opened the site with http:// instead of https://, the "
            "secure login cookie is not sent - use the https address, or set "
            "COOKIE_SECURE=false in settings.env.)",
        )


def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("login"))
        if request.method == "POST":
            check_csrf()
        return view(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# Public routes
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    games = [g for g in storage.list_games() if g.latest is not None]
    return render_template("index.html", games=games)


@app.get("/play/<slug>")
def play(slug: str):
    path = storage.latest_path(slug)
    if path is None:
        abort(404)
    resp = send_file(path, mimetype="text/html; charset=utf-8", max_age=0, conditional=True)
    resp.headers["Content-Security-Policy"] = GAME_CSP
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/healthz")
def healthz():
    return "ok", 200, {"Content-Type": "text/plain"}


# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------
@app.route("/admin/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        check_csrf()
        ip = _client_ip()
        if len(_recent_failures(ip)) >= LOGIN_MAX_FAILS:
            flash(
                f"Too many failed attempts. Try again in {LOGIN_WINDOW // 60} minutes.",
                "error",
            )
            return render_template("login.html"), 429
        supplied = request.form.get("password", "")
        if hmac.compare_digest(supplied.encode(), ADMIN_PASSWORD.encode()):
            _failures.pop(ip, None)
            session.clear()
            session.permanent = True
            session["admin"] = True
            session["csrf"] = secrets.token_urlsafe(32)
            return redirect(url_for("admin_home"))
        _record_failure(ip)
        time.sleep(1)  # slow down guessing
        flash("Incorrect password.", "error")
        return render_template("login.html"), 401
    return render_template("login.html")


@app.post("/admin/logout")
@admin_required
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.get("/admin")
@admin_required
def admin_home():
    return render_template(
        "admin.html", games=storage.list_games(), max_kb=MAX_UPLOAD_KB, keep=KEEP_VERSIONS
    )


def _uploaded_bytes() -> tuple[bytes, str]:
    f = request.files.get("file")
    if f is None or not f.filename:
        raise StorageError("Choose an .html file to upload.")
    if not f.filename.lower().endswith((".html", ".htm")):
        raise StorageError("Only .html or .htm files can be uploaded.")
    return f.read(MAX_UPLOAD_KB * 1024 + 1), f.filename


@app.post("/admin/games")
@admin_required
def create_game():
    name = request.form.get("name", "")
    slug = request.form.get("slug", "")
    try:
        data, filename = _uploaded_bytes()
        validate_html(data, MAX_UPLOAD_KB * 1024)
        game = storage.create_game(name, slug)
        storage.add_version(game.slug, data, MAX_UPLOAD_KB * 1024, filename, "Initial upload")
    except StorageError as e:
        flash(str(e), "error")
        return redirect(url_for("admin_home"))
    flash(f"Created '{game.name}'.", "ok")
    return redirect(url_for("admin_game", slug=game.slug))


@app.get("/admin/games/<slug>")
@admin_required
def admin_game(slug: str):
    game = storage.get(slug)
    if game is None:
        abort(404)
    return render_template(
        "game.html", game=game, max_kb=MAX_UPLOAD_KB, keep=KEEP_VERSIONS
    )


@app.post("/admin/games/<slug>/upload")
@admin_required
def upload_version(slug: str):
    try:
        data, filename = _uploaded_bytes()
        v = storage.add_version(
            slug, data, MAX_UPLOAD_KB * 1024, filename, request.form.get("note", "")
        )
        flash(f"Uploaded version {v.number}. It is now live.", "ok")
    except StorageError as e:
        flash(str(e), "error")
    return redirect(url_for("admin_game", slug=slug))


@app.post("/admin/games/<slug>/rename")
@admin_required
def rename_game(slug: str):
    try:
        storage.rename_game(slug, request.form.get("name", ""))
        flash("Name updated.", "ok")
    except StorageError as e:
        flash(str(e), "error")
    return redirect(url_for("admin_game", slug=slug))


@app.post("/admin/games/<slug>/restore/<int:number>")
@admin_required
def restore_version(slug: str, number: int):
    try:
        v = storage.restore_version(slug, number, MAX_UPLOAD_KB * 1024)
        flash(f"Version {number} restored as new version {v.number}. It is now live.", "ok")
    except StorageError as e:
        flash(str(e), "error")
    return redirect(url_for("admin_game", slug=slug))


@app.get("/admin/games/<slug>/download/<int:number>")
@admin_required
def download_version(slug: str, number: int):
    path = storage.version_path(slug, number)
    if path is None:
        abort(404)
    # Always a download (never rendered) so old versions can't be "played".
    resp = send_file(
        path,
        mimetype="application/octet-stream",
        as_attachment=True,
        download_name=f"{slug}-v{number}.html",
    )
    resp.headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
    return resp


@app.post("/admin/games/<slug>/delete")
@admin_required
def delete_game(slug: str):
    if request.form.get("confirm", "").strip() != slug:
        flash("Type the game id exactly to confirm deletion.", "error")
        return redirect(url_for("admin_game", slug=slug))
    try:
        storage.delete_game(slug)
        flash(f"Deleted '{slug}' and all of its versions.", "ok")
    except StorageError as e:
        flash(str(e), "error")
    return redirect(url_for("admin_home"))


@app.errorhandler(413)
def too_large(_e):
    if session.get("admin"):
        flash(f"File is larger than the {MAX_UPLOAD_KB} KB limit.", "error")
        return redirect(url_for("admin_home"))
    return "File too large", 413


app.jinja_env.filters["slugify"] = slugify
