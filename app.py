from __future__ import annotations

import argparse
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

import xlrd
from flask import Flask, g, redirect, render_template, request, url_for


BASE_DIR = Path(__file__).resolve().parent
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


@dataclass
class Interval:
    start: str
    end: str


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["DATABASE"] = str(DB_PATH)

    @app.after_request
    def add_noindex_headers(response):
        response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive, nosnippet, noimageindex"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    @app.route("/")
    def index():
        db = get_db()
        commands = list_commands(db)
        if not commands:
            return render_template("empty.html", import_path=str(DEFAULT_IMPORT_PATH))

        selected_id = request.args.get("command_id", type=int) or commands[0]["id"]
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

    @app.post("/import")
    def import_default():
        source = request.form.get("source") or str(DEFAULT_IMPORT_PATH)
        import_workbook(Path(source), get_db())
        return redirect(url_for("index"))

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
            """
        )


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
