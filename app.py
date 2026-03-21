from __future__ import annotations

import argparse
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Iterable

try:
    from authlib.integrations.flask_client import OAuth
except ModuleNotFoundError:  # pragma: no cover - fallback for environments without Authlib
    OAuth = None
import xlrd
from flask import Flask, g, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix


BASE_DIR = Path(__file__).resolve().parent


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file(BASE_DIR / ".env")
DB_PATH = Path(os.environ.get("HDO_DB_PATH", str(BASE_DIR / "data" / "hdo.sqlite3")))
DEFAULT_IMPORT_PATH = Path(
    os.environ.get(
        "HDO_IMPORT_PATH",
        "/Users/fanatik/Downloads/aktualni-program-hdo-ke-stazeni-3.xls",
    )
)
DAY_LABELS = {
    0: "Pondělí",
    1: "Úterý",
    2: "Středa",
    3: "Čtvrtek",
    4: "Pátek",
    5: "Sobota",
    6: "Neděle",
    7: "Svátek",
}
DAY_NAME_TO_KEY = {
    "pondělí": 0,
    "úterý": 1,
    "středa": 2,
    "čtvrtek": 3,
    "pátek": 4,
    "sobota": 5,
    "neděle": 6,
    "svátek": 7,
}
OAUTH_PROVIDER_CONFIG = {
    "google": {
        "display_name": "Google",
        "metadata_url": "https://accounts.google.com/.well-known/openid-configuration",
        "scope": "openid email profile",
        "icon": "G",
    },
    "apple": {
        "display_name": "Apple",
        "metadata_url": "https://appleid.apple.com/.well-known/openid-configuration",
        "scope": "openid email name",
        "icon": "A",
    },
}


@dataclass
class Interval:
    start: str
    end: str


class PrefixMiddleware:
    def __init__(self, app, prefix: str):
        self.app = app
        self.prefix = normalize_prefix(prefix)

    def __call__(self, environ, start_response):
        if not self.prefix:
            return self.app(environ, start_response)
        if environ.get("SCRIPT_NAME"):
            return self.app(environ, start_response)

        path_info = environ.get("PATH_INFO", "") or ""
        if path_info.startswith(self.prefix):
            environ["SCRIPT_NAME"] = self.prefix
            stripped = path_info[len(self.prefix) :]
            environ["PATH_INFO"] = stripped if stripped else "/"
        else:
            # Proxy can strip prefix before forwarding. Keep routes matching
            # and still generate prefixed URLs via url_for.
            environ["SCRIPT_NAME"] = self.prefix
        return self.app(environ, start_response)


def normalize_prefix(prefix: str) -> str:
    normalized = (prefix or "").strip()
    if not normalized:
        return ""
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    return normalized.rstrip("/")


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["DATABASE"] = str(DB_PATH)
    app.config["SECRET_KEY"] = os.environ.get("HDO_SECRET_KEY", "dev-secret-change-me")
    app.config["AUTH_ENABLED"] = os.environ.get("HDO_AUTH_ENABLED", "1") != "0"
    app.config["AUTH_SESSION_KEY"] = "auth_user_id"
    app.config["EXTERNAL_BASE_URL"] = os.environ.get("HDO_EXTERNAL_BASE_URL", "").rstrip("/")
    app.config["URL_PREFIX"] = normalize_prefix(os.environ.get("HDO_URL_PREFIX", ""))
    app.config["APPLICATION_ROOT"] = app.config["URL_PREFIX"] or "/"
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)
    app.wsgi_app = PrefixMiddleware(app.wsgi_app, app.config["URL_PREFIX"])
    oauth = OAuth(app) if OAuth else None
    if oauth is None:
        app.config["AUTH_ENABLED"] = False

    configured_providers: dict[str, dict[str, str]] = {}
    for provider_key, provider in OAUTH_PROVIDER_CONFIG.items():
        if oauth is None:
            continue
        client_id = os.environ.get(f"HDO_OAUTH_{provider_key.upper()}_CLIENT_ID", "").strip()
        client_secret = os.environ.get(f"HDO_OAUTH_{provider_key.upper()}_CLIENT_SECRET", "").strip()
        if not client_id or not client_secret:
            continue
        oauth.register(
            name=provider_key,
            client_id=client_id,
            client_secret=client_secret,
            server_metadata_url=provider["metadata_url"],
            client_kwargs={"scope": provider["scope"]},
        )
        configured_providers[provider_key] = provider

    @app.after_request
    def add_noindex_headers(response):
        response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive, nosnippet, noimageindex"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    @app.before_request
    def load_current_user() -> None:
        g.current_user = None
        if not app.config["AUTH_ENABLED"]:
            return
        user_id = session.get(app.config["AUTH_SESSION_KEY"])
        if not user_id:
            return
        g.current_user = get_user_by_id(get_db(), int(user_id))

    @app.context_processor
    def inject_template_context() -> dict[str, object]:
        return {
            "auth_enabled": app.config["AUTH_ENABLED"],
            "auth_library_available": oauth is not None,
            "current_user": getattr(g, "current_user", None),
        }

    @app.get("/login")
    def login():
        if not app.config["AUTH_ENABLED"]:
            return redirect(url_for("index"))
        if g.current_user:
            return redirect(url_for("index"))
        providers = [{"key": key, **provider} for key, provider in configured_providers.items()]
        return render_template("login.html", providers=providers)

    @app.get("/auth/<provider>/start")
    def auth_start(provider: str):
        if not app.config["AUTH_ENABLED"]:
            return redirect(url_for("index"))
        if provider not in configured_providers:
            return redirect(url_for("login"))
        callback_path = url_for("auth_callback", provider=provider)
        redirect_uri = build_external_url(app, callback_path)
        return oauth.create_client(provider).authorize_redirect(redirect_uri)

    @app.get("/auth/<provider>/callback")
    def auth_callback(provider: str):
        if not app.config["AUTH_ENABLED"]:
            return redirect(url_for("index"))
        if provider not in configured_providers:
            return redirect(url_for("login"))
        client = oauth.create_client(provider)
        token = client.authorize_access_token()
        user_info = token.get("userinfo")
        if not user_info:
            user_info = client.userinfo(token=token)
        user = upsert_user_from_oauth(get_db(), provider, user_info)
        if not user or not int(user["is_active"]):
            session.pop(app.config["AUTH_SESSION_KEY"], None)
            return redirect(url_for("login"))
        session[app.config["AUTH_SESSION_KEY"]] = int(user["id"])
        return redirect(url_for("index"))

    @app.post("/logout")
    def logout():
        session.pop(app.config["AUTH_SESSION_KEY"], None)
        return redirect(url_for("login") if app.config["AUTH_ENABLED"] else url_for("index"))

    @app.route("/")
    @login_required(app)
    def index():
        db = get_db()
        commands = list_commands(db)
        if not commands:
            return render_template("empty.html", import_path=str(DEFAULT_IMPORT_PATH))

        selected_id = request.args.get("command_id", type=int)
        if selected_id is None:
            selected_id = resolve_default_command_id(db, commands)
        command = get_command_by_id(db, selected_id) or commands[0]
        today = datetime.now().date()
        current_state = build_current_state(db, command["id"], today, datetime.now())
        upcoming_days = build_upcoming_days(db, command["id"], today, 7)
        import_meta = get_latest_import(db)
        return render_template(
            "index.html",
            commands=commands,
            command=command,
            current_state=current_state,
            upcoming_days=upcoming_days,
            import_meta=import_meta,
            today=today,
        )

    @app.get("/settings")
    @login_required(app)
    def settings():
        db = get_db()
        commands = list_commands(db)
        import_meta = get_latest_import(db)
        default_code = get_setting(db, "default_numeric_code") or ""
        selected_default_command = None
        if commands:
            selected_default_command = (
                next((item for item in commands if item["numeric_code"] == default_code), None) or commands[0]
            )
        users = list_users(db)
        return render_template(
            "settings.html",
            commands=commands,
            import_meta=import_meta,
            default_code=default_code,
            selected_default_command=selected_default_command,
            import_path=str(DEFAULT_IMPORT_PATH),
            users=users,
            providers=[{"key": key, **provider} for key, provider in configured_providers.items()],
        )

    @app.get("/setting")
    @login_required(app)
    def settings_alias():
        return redirect(url_for("settings"))

    @app.post("/import")
    @admin_required(app)
    def import_default():
        source = request.form.get("source") or str(DEFAULT_IMPORT_PATH)
        import_workbook(Path(source), get_db())
        next_endpoint = request.form.get("next")
        if next_endpoint in {"index", "settings"}:
            return redirect(url_for(next_endpoint))
        return redirect(url_for("index"))

    @app.post("/settings/default-code")
    @login_required(app)
    def update_default_code():
        command_id = request.form.get("command_id", type=int)
        db = get_db()
        command = get_command_by_id(db, command_id) if command_id else None
        default_code = command["numeric_code"] if command else ""
        set_setting(db, "default_numeric_code", default_code)
        return redirect(url_for("settings"))

    @app.post("/settings/users/<int:user_id>/toggle-active")
    @admin_required(app)
    def toggle_user_active(user_id: int):
        db = get_db()
        current_user = g.current_user
        if current_user and int(current_user["id"]) == user_id:
            return redirect(url_for("settings"))
        user = get_user_by_id(db, user_id)
        if user:
            update_user_active(db, user_id, not bool(user["is_active"]))
        return redirect(url_for("settings"))

    @app.post("/settings/users/<int:user_id>/toggle-admin")
    @admin_required(app)
    def toggle_user_admin(user_id: int):
        db = get_db()
        current_user = g.current_user
        user = get_user_by_id(db, user_id)
        if not user:
            return redirect(url_for("settings"))
        if current_user and int(current_user["id"]) == user_id:
            return redirect(url_for("settings"))
        update_user_admin(db, user_id, not bool(user["is_admin"]))
        return redirect(url_for("settings"))

    @app.get("/robots.txt")
    def robots_txt():
        return (
            "User-agent: *\n"
            "Disallow: /\n"
            "Noindex: /\n",
            200,
            {"Content-Type": "text/plain; charset=utf-8"},
        )

    @app.teardown_appcontext
    def close_db(_: object) -> None:
        db = g.pop("db", None)
        if db is not None:
            db.close()

    init_db()
    return app


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as db:
        row = db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'commands'"
        ).fetchone()
        if row and row[0] and "UNIQUE(import_id, readable_code)" in row[0]:
            db.executescript(
                """
                DROP TABLE IF EXISTS schedules;
                DROP TABLE IF EXISTS commands;
                DROP TABLE IF EXISTS imports;
                """
            )
        db.executescript(
            """
            PRAGMA foreign_keys = ON;

            CREATE TABLE IF NOT EXISTS imports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_path TEXT NOT NULL,
                validity_from TEXT,
                validity_to TEXT,
                imported_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS commands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                import_id INTEGER NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
                numeric_code TEXT NOT NULL,
                readable_code TEXT NOT NULL,
                description TEXT
            );

            CREATE TABLE IF NOT EXISTS schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                command_id INTEGER NOT NULL REFERENCES commands(id) ON DELETE CASCADE,
                day_key INTEGER NOT NULL,
                interval_order INTEGER NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                name TEXT,
                is_admin INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                last_login_at TEXT
            );

            CREATE TABLE IF NOT EXISTS oauth_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                provider TEXT NOT NULL,
                provider_sub TEXT NOT NULL,
                email TEXT,
                UNIQUE(provider, provider_sub)
            );
            """
        )


def login_required(app: Flask):
    def decorator(func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            if not app.config["AUTH_ENABLED"]:
                return func(*args, **kwargs)
            user = getattr(g, "current_user", None)
            if not user or not bool(user["is_active"]):
                session.pop(app.config["AUTH_SESSION_KEY"], None)
                return redirect(url_for("login"))
            return func(*args, **kwargs)

        return wrapped

    return decorator


def admin_required(app: Flask):
    def decorator(func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            if not app.config["AUTH_ENABLED"]:
                return func(*args, **kwargs)
            user = getattr(g, "current_user", None)
            if not user or not bool(user["is_active"]):
                session.pop(app.config["AUTH_SESSION_KEY"], None)
                return redirect(url_for("login"))
            if not bool(user["is_admin"]):
                return redirect(url_for("index"))
            return func(*args, **kwargs)

        return wrapped

    return decorator


def build_external_url(app: Flask, path: str) -> str:
    base = app.config.get("EXTERNAL_BASE_URL", "")
    if not base:
        return url_for("index", _external=True).rstrip("/") + path
    if path.startswith("/"):
        return f"{base}{path}"
    return f"{base}/{path}"


def get_user_by_id(db: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def list_users(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.execute("SELECT * FROM users ORDER BY email").fetchall()


def update_user_active(db: sqlite3.Connection, user_id: int, is_active: bool) -> None:
    with db:
        db.execute("UPDATE users SET is_active = ? WHERE id = ?", (1 if is_active else 0, user_id))


def update_user_admin(db: sqlite3.Connection, user_id: int, is_admin: bool) -> None:
    with db:
        db.execute("UPDATE users SET is_admin = ? WHERE id = ?", (1 if is_admin else 0, user_id))


def upsert_user_from_oauth(db: sqlite3.Connection, provider: str, user_info: dict[str, object]) -> sqlite3.Row | None:
    provider_sub = clean_string(user_info.get("sub"))
    email = clean_string(user_info.get("email")).lower()
    name = clean_string(user_info.get("name") or user_info.get("preferred_username") or email)
    if not provider_sub or not email:
        return None
    now = datetime.now().isoformat(timespec="seconds")

    with db:
        account = db.execute(
            """
            SELECT u.*
            FROM oauth_accounts oa
            JOIN users u ON u.id = oa.user_id
            WHERE oa.provider = ? AND oa.provider_sub = ?
            """,
            (provider, provider_sub),
        ).fetchone()
        if account:
            db.execute(
                """
                UPDATE users
                SET email = ?, name = ?, last_login_at = ?
                WHERE id = ?
                """,
                (email, name, now, account["id"]),
            )
            db.execute(
                """
                UPDATE oauth_accounts
                SET email = ?
                WHERE provider = ? AND provider_sub = ?
                """,
                (email, provider, provider_sub),
            )
            return get_user_by_id(db, int(account["id"]))

        existing = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if existing:
            user_id = int(existing["id"])
            db.execute(
                """
                UPDATE users
                SET name = ?, last_login_at = ?
                WHERE id = ?
                """,
                (name, now, user_id),
            )
        else:
            is_first_user = db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None
            cursor = db.execute(
                """
                INSERT INTO users (email, name, is_admin, is_active, created_at, last_login_at)
                VALUES (?, ?, ?, 1, ?, ?)
                """,
                (email, name, 1 if is_first_user else 0, now, now),
            )
            user_id = int(cursor.lastrowid)

        db.execute(
            """
            INSERT OR IGNORE INTO oauth_accounts (user_id, provider, provider_sub, email)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, provider, provider_sub, email),
        )
        return get_user_by_id(db, user_id)


def import_workbook(path: Path, db: sqlite3.Connection) -> None:
    book = xlrd.open_workbook(path)
    sheet = book.sheet_by_index(0)

    validity_from = sheet.cell_value(0, 12)
    validity_to = sheet.cell_value(1, 12)
    imported_at = datetime.now().isoformat(timespec="seconds")

    with db:
        db.execute("DELETE FROM schedules")
        db.execute("DELETE FROM commands")
        db.execute("DELETE FROM imports")
        cursor = db.execute(
            """
            INSERT INTO imports (source_path, validity_from, validity_to, imported_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                str(path),
                normalize_excel_date(validity_from, book.datemode),
                normalize_excel_date(validity_to, book.datemode),
                imported_at,
            ),
        )
        import_id = cursor.lastrowid
        current_command_id = None
        interval_order = 0

        for row_index in range(3, sheet.nrows):
            numeric_raw = sheet.cell_value(row_index, 0)
            readable_code = clean_string(sheet.cell_value(row_index, 1))
            description = clean_string(sheet.cell_value(row_index, 2))
            day_name = clean_string(sheet.cell_value(row_index, 3)).lower()

            if numeric_raw not in ("", None):
                numeric_code = stringify_numeric_code(numeric_raw)
                command_cursor = db.execute(
                    """
                    INSERT INTO commands (import_id, numeric_code, readable_code, description)
                    VALUES (?, ?, ?, ?)
                    """,
                    (import_id, numeric_code, readable_code, description),
                )
                current_command_id = command_cursor.lastrowid

            if current_command_id is None or day_name not in DAY_NAME_TO_KEY:
                continue

            interval_order = 0
            for start_col in range(4, sheet.ncols, 2):
                start_value = sheet.cell_value(row_index, start_col)
                end_value = sheet.cell_value(row_index, start_col + 1) if start_col + 1 < sheet.ncols else ""
                if start_value in ("", None) or end_value in ("", None):
                    continue
                start_time = normalize_excel_time(start_value)
                end_time = normalize_excel_time(end_value)
                if not start_time or not end_time:
                    continue
                db.execute(
                    """
                    INSERT INTO schedules (command_id, day_key, interval_order, start_time, end_time)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (current_command_id, DAY_NAME_TO_KEY[day_name], interval_order, start_time, end_time),
                )
                interval_order += 1


def clean_string(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip().replace("\n", " ")


def normalize_excel_date(value: object, datemode: int) -> str | None:
    if value in ("", None):
        return None
    if isinstance(value, float):
        parts = xlrd.xldate_as_tuple(value, datemode)
        return date(parts[0], parts[1], parts[2]).isoformat()
    return clean_string(value)


def normalize_excel_time(value: object) -> str | None:
    if value in ("", None):
        return None
    if isinstance(value, float):
        total_seconds = int(round(value * 24 * 60 * 60))
        hours = (total_seconds // 3600) % 24
        minutes = (total_seconds % 3600) // 60
        return f"{hours:02d}:{minutes:02d}"
    text = clean_string(value)
    if not text:
        return None
    return text[:5]


def stringify_numeric_code(value: object) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return clean_string(value)


def list_commands(db: sqlite3.Connection) -> Iterable[sqlite3.Row]:
    return db.execute(
        """
        SELECT c.*
        FROM commands c
        JOIN imports i ON i.id = c.import_id
        ORDER BY CASE WHEN trim(c.readable_code) = '' THEN 1 ELSE 0 END, c.readable_code, c.numeric_code
        """
    ).fetchall()


def get_command_by_id(db: sqlite3.Connection, command_id: int) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM commands WHERE id = ?", (command_id,)).fetchone()


def get_latest_import(db: sqlite3.Connection) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM imports ORDER BY id DESC LIMIT 1").fetchone()


def get_setting(db: sqlite3.Connection, key: str) -> str | None:
    row = db.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    if not row:
        return None
    return row["value"]


def set_setting(db: sqlite3.Connection, key: str, value: str) -> None:
    with db:
        db.execute(
            """
            INSERT INTO app_settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )


def resolve_default_command_id(db: sqlite3.Connection, commands: Iterable[sqlite3.Row]) -> int:
    default_code = get_setting(db, "default_numeric_code")
    if default_code:
        command = db.execute(
            """
            SELECT id
            FROM commands
            WHERE numeric_code = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (default_code,),
        ).fetchone()
        if command:
            return int(command["id"])
    first_command = next(iter(commands), None)
    if first_command:
        return int(first_command["id"])
    return 0


def get_intervals_for_date(db: sqlite3.Connection, command_id: int, day: date) -> list[Interval]:
    day_key = 7 if is_czech_public_holiday(day) else day.weekday()
    rows = db.execute(
        """
        SELECT start_time, end_time
        FROM schedules
        WHERE command_id = ? AND day_key = ?
        ORDER BY interval_order
        """,
        (command_id, day_key),
    ).fetchall()
    return [Interval(row["start_time"], row["end_time"]) for row in rows]


def build_current_state(
    db: sqlite3.Connection, command_id: int, today: date, now: datetime
) -> dict[str, object]:
    intervals = get_intervals_for_date(db, command_id, today)
    current_minutes = now.hour * 60 + now.minute
    active = False
    next_change = None

    for interval in intervals:
        start_minutes = to_minutes(interval.start)
        end_minutes = to_minutes(interval.end)
        if end_minutes == 0 and interval.end == "00:00":
            end_minutes = 24 * 60
        if start_minutes <= current_minutes < end_minutes:
            active = True
            next_change = interval.end
            break
        if current_minutes < start_minutes and next_change is None:
            next_change = interval.start

    if next_change is None and intervals:
        next_change = intervals[0].start

    return {
        "active": active,
        "label": "Zapnuto" if active else "Vypnuto",
        "next_change": next_change,
        "intervals": intervals,
    }


def build_upcoming_days(
    db: sqlite3.Connection, command_id: int, start_day: date, count: int
) -> list[dict[str, object]]:
    items = []
    for offset in range(count):
        current_day = start_day + timedelta(days=offset)
        intervals = get_intervals_for_date(db, command_id, current_day)
        items.append(
            {
                "date": current_day,
                "label": DAY_LABELS[7] if is_czech_public_holiday(current_day) else DAY_LABELS[current_day.weekday()],
                "is_holiday": is_czech_public_holiday(current_day),
                "intervals": intervals,
                "segments": build_day_segments(intervals),
            }
        )
    return items


def build_day_segments(intervals: list[Interval]) -> list[dict[str, str | bool]]:
    segments: list[dict[str, str | bool]] = []
    cursor = 0

    for interval in intervals:
        start_minutes = to_minutes(interval.start)
        end_minutes = to_minutes(interval.end)
        if interval.end == "00:00":
            end_minutes = 24 * 60

        if cursor < start_minutes:
            segments.append(
                {
                    "start": minutes_to_time(cursor),
                    "end": minutes_to_time(start_minutes),
                    "active": False,
                    "label": "Vypnuto",
                }
            )

        segments.append(
            {
                "start": interval.start,
                "end": minutes_to_time(end_minutes),
                "active": True,
                "label": "Zapnuto",
            }
        )
        cursor = end_minutes

    if cursor < 24 * 60:
        segments.append(
            {
                "start": minutes_to_time(cursor),
                "end": "24:00",
                "active": False,
                "label": "Vypnuto",
            }
        )

    return segments


def to_minutes(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def minutes_to_time(value: int) -> str:
    if value >= 24 * 60:
        return "24:00"
    hours = value // 60
    minutes = value % 60
    return f"{hours:02d}:{minutes:02d}"


def easter_sunday(year: int) -> date:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def is_czech_public_holiday(day: date) -> bool:
    fixed_holidays = {
        (1, 1),
        (5, 1),
        (5, 8),
        (7, 5),
        (7, 6),
        (9, 28),
        (10, 28),
        (11, 17),
        (12, 24),
        (12, 25),
        (12, 26),
    }
    if (day.month, day.day) in fixed_holidays:
        return True
    easter = easter_sunday(day.year)
    return day in {easter - timedelta(days=2), easter + timedelta(days=1)}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="HDO harmonogram")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "import"])
    parser.add_argument("--source", default=str(DEFAULT_IMPORT_PATH))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    init_db()
    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row
        if args.command == "import":
            import_workbook(Path(args.source), db)
            return
        if not get_latest_import(db) and Path(args.source).exists():
            import_workbook(Path(args.source), db)
    app = create_app()
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
