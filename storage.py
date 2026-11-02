"""
SQLite storage for the Expense Tracker.

Tables are read and written as whole pandas DataFrames, which keeps the
Streamlit code simple: load a table, let the user edit it, save it back.
Every table has an integer `id` so other tables can refer to its rows.
"""

import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import uuid
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

import pandas as pd

# Column name -> SQLite type. New columns added here are created on the next start.
SCHEMA = {
    # Amount is always in rupees; for an expense paid in another currency, Currency and
    # Original hold what was actually paid (Currency is "" for rupees)
    "expenses": {
        "Date": "TEXT", "Category": "TEXT", "Amount": "REAL", "Payment": "TEXT", "Note": "TEXT",
        "Account": "TEXT", "Receipt": "TEXT", "Currency": "TEXT", "Original": "REAL",
    },
    "income": {"Date": "TEXT", "Source": "TEXT", "Amount": "REAL", "Note": "TEXT", "Account": "TEXT"},
    "transfers": {"Date": "TEXT", "From": "TEXT", "To": "TEXT", "Amount": "REAL", "Note": "TEXT"},
    # Amount > 0: the person owes you; < 0: you owe them
    "splits": {
        "Date": "TEXT", "Person": "TEXT", "Amount": "REAL", "Note": "TEXT", "Settled": "INTEGER",
        "ExpenseId": "INTEGER",
    },
    # Money put towards (or taken out of, if negative) a savings goal
    "contributions": {"Date": "TEXT", "Goal": "TEXT", "Amount": "REAL", "Note": "TEXT"},
}
# Values used for rows saved before a column existed
COLUMN_DEFAULTS = {"Payment": "Other", "Note": "", "Account": "", "Settled": 0, "Receipt": "", "Currency": ""}
RECEIPT_TYPES = (".jpg", ".jpeg", ".png", ".webp", ".pdf")
KEEP_BACKUPS = 14


class Store:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.backup_dir = self.db_path.parent / "backups"
        self.receipts_dir = self.db_path.parent / "receipts"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _ensure_schema(self) -> None:
        with closing(self._connect()) as con, con:
            con.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
            for table, columns in SCHEMA.items():
                con.execute(f"CREATE TABLE IF NOT EXISTS {table} (id INTEGER PRIMARY KEY)")
                existing = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
                for col, sql_type in columns.items():
                    if col not in existing:
                        con.execute(f'ALTER TABLE {table} ADD COLUMN "{col}" {sql_type}')

    # ---------- Tables ----------
    def load(self, table: str) -> pd.DataFrame:
        quoted = ", ".join(f'"{c}"' for c in ["id", *SCHEMA[table]])
        with closing(self._connect()) as con:
            df = pd.read_sql_query(f"SELECT {quoted} FROM {table} ORDER BY Date DESC, id DESC", con)
        if "Date" in df:
            df["Date"] = pd.to_datetime(df["Date"]).dt.date
        for col, default in COLUMN_DEFAULTS.items():
            if col in df:
                df[col] = df[col].fillna(default)
        return df

    def save(self, table: str, df: pd.DataFrame) -> None:
        """Replace the whole table with `df`. Rows without an id get a new one."""
        columns = list(SCHEMA[table])
        df = df.dropna(subset=[c for c in ("Date", "Amount") if c in columns])
        if "id" not in df:
            df = df.assign(id=None)
        for col in columns:
            if col not in df:
                df = df.assign(**{col: COLUMN_DEFAULTS.get(col)})
        rows = []
        for r in df.itertuples(index=False):
            values = []
            for col in columns:
                v = getattr(r, col)
                if col == "Date":
                    v = pd.Timestamp(v).date().isoformat()
                elif pd.isna(v):
                    v = None
                elif hasattr(v, "item"):  # numpy scalar -> plain Python value
                    v = v.item()
                values.append(v)
            rows.append((None if pd.isna(r.id) else int(r.id), *values))

        placeholders = ", ".join("?" * (len(columns) + 1))
        quoted = ", ".join(f'"{c}"' for c in columns)
        with closing(self._connect()) as con, con:
            con.execute(f"DELETE FROM {table}")
            con.executemany(f"INSERT INTO {table} (id, {quoted}) VALUES ({placeholders})", rows)

    def add(self, table: str, row: dict) -> int:
        """Insert one row and return its new id."""
        columns = [c for c in SCHEMA[table] if c in row]
        values = [
            pd.Timestamp(row[c]).date().isoformat() if c == "Date" else row[c] for c in columns
        ]
        quoted = ", ".join(f'"{c}"' for c in columns)
        with closing(self._connect()) as con, con:
            cursor = con.execute(
                f"INSERT INTO {table} ({quoted}) VALUES ({', '.join('?' * len(columns))})", values
            )
            return cursor.lastrowid

    # ---------- Receipts (files next to the database, named in expenses.Receipt) ----------
    def save_receipt(self, expense_id: int, filename: str, data: bytes) -> str:
        suffix = Path(filename).suffix.lower()
        if suffix not in RECEIPT_TYPES:
            raise ValueError(f"Receipts can be {', '.join(RECEIPT_TYPES)} files.")
        self.receipts_dir.mkdir(exist_ok=True)
        name = f"{expense_id}_{uuid.uuid4().hex[:8]}{suffix}"
        (self.receipts_dir / name).write_bytes(data)
        with closing(self._connect()) as con, con:
            con.execute("UPDATE expenses SET Receipt = ? WHERE id = ?", (name, expense_id))
        self.prune_receipts()
        return name

    def receipt_path(self, name: str) -> Path:
        return self.receipts_dir / Path(name).name  # never leave the receipts folder

    def remove_receipt(self, expense_id: int) -> None:
        with closing(self._connect()) as con, con:
            con.execute("UPDATE expenses SET Receipt = '' WHERE id = ?", (expense_id,))
        self.prune_receipts()

    def prune_receipts(self) -> None:
        """Delete receipt files that no expense refers to any more."""
        if not self.receipts_dir.exists():
            return
        with closing(self._connect()) as con:
            in_use = {row[0] for row in con.execute("SELECT Receipt FROM expenses WHERE Receipt != ''")}
        for path in self.receipts_dir.iterdir():
            if path.name not in in_use:
                path.unlink()

    # ---------- Settings (JSON values by key) ----------
    def load_settings(self, defaults: dict) -> dict:
        settings = json.loads(json.dumps(defaults))  # deep copy
        with closing(self._connect()) as con:
            for key, value in con.execute("SELECT key, value FROM settings"):
                settings[key] = json.loads(value)
        return settings

    def save_settings(self, settings: dict) -> None:
        with closing(self._connect()) as con, con:
            con.executemany(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                [(k, json.dumps(v, ensure_ascii=False)) for k, v in settings.items()],
            )

    # ---------- Moving from the old CSV / JSON files ----------
    def import_legacy_files(self, folder: Path) -> tuple[list[str], list[str]]:
        """Copy expenses.csv, income.csv and settings.json into an empty database, once.

        The old files are left where they are. Returns (done, problems) messages for the user.
        """
        with closing(self._connect()) as con:
            if con.execute("SELECT 1 FROM settings WHERE key = '_imported_legacy'").fetchone():
                return [], []
        messages, problems = [], []
        for table, name in (("expenses", "expenses.csv"), ("income", "income.csv")):
            path = folder / name
            if path.exists() and self.load(table).empty:
                old = pd.read_csv(path, dtype=str)
                old["Amount"] = pd.to_numeric(old["Amount"])
                for col in SCHEMA[table]:
                    if col not in old:
                        old[col] = COLUMN_DEFAULTS.get(col)
                self.save(table, old)
                messages.append(f"Moved {len(old)} rows from {name} into the database.")

        path = folder / "settings.json"
        legacy = {}
        if path.exists():
            try:
                legacy = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                problems.append("settings.json could not be read, so defaults are being used.")
        self.save_settings({**legacy, "_imported_legacy": True})
        return messages, problems

    # ---------- Backups ----------
    def backup(self, label: str = "") -> Path:
        self.backup_dir.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        target = self.backup_dir / f"{self.db_path.stem}_{stamp}{'_' + label if label else ''}.db"
        with closing(self._connect()) as src, closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
        self._prune_backups()
        return target

    def daily_backup(self) -> Path | None:
        """Back up once per day, the first time the app is opened that day."""
        today = date.today().isoformat()
        if any(p.name.startswith(f"{self.db_path.stem}_{today}") for p in self.list_backups()):
            return None
        return self.backup("auto")

    def list_backups(self) -> list[Path]:
        if not self.backup_dir.exists():
            return []
        return sorted(self.backup_dir.glob(f"{self.db_path.stem}_*.db"), reverse=True)

    def _prune_backups(self) -> None:
        # Only automatic backups are pruned; ones made by hand are kept
        for old in [p for p in self.list_backups() if p.stem.endswith("_auto")][KEEP_BACKUPS:]:
            old.unlink()

    def restore(self, backup: Path) -> None:
        """Replace the database with a backup, saving the current state first."""
        self.backup("before-restore")
        with closing(sqlite3.connect(backup)) as src, closing(self._connect()) as dst:
            src.backup(dst)
        self._ensure_schema()


# ---------- User accounts ----------
USERNAME_PATTERN = re.compile(r"^[a-z0-9_.-]{3,30}$")
MIN_PASSWORD_LENGTH = 8
HASH_ITERATIONS = 200_000


def _hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, HASH_ITERATIONS)


class Users:
    """Usernames and salted password hashes. Each user's data lives in its own database."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "users.db"
        with closing(sqlite3.connect(self.db_path)) as con, con:
            con.execute(
                "CREATE TABLE IF NOT EXISTS users "
                "(username TEXT PRIMARY KEY, salt BLOB, password_hash BLOB, created TEXT)"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def count(self) -> int:
        with closing(self._connect()) as con:
            return con.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def store_for(self, username: str) -> Store:
        return Store(self.data_dir / "users" / username / "expenses.db")

    def create(self, username: str, password: str) -> str | None:
        """Add a user. Returns an error message, or None on success."""
        if not USERNAME_PATTERN.match(username):
            return "Usernames are 3–30 characters: lowercase letters, numbers, dots, dashes or underscores."
        if len(password) < MIN_PASSWORD_LENGTH:
            return f"Passwords need at least {MIN_PASSWORD_LENGTH} characters."
        salt = os.urandom(16)
        try:
            with closing(self._connect()) as con, con:
                con.execute(
                    "INSERT INTO users VALUES (?, ?, ?, ?)",
                    (username, salt, _hash_password(password, salt), datetime.now().isoformat()),
                )
        except sqlite3.IntegrityError:
            return "That username is taken."
        return None

    def check(self, username: str, password: str) -> bool:
        with closing(self._connect()) as con:
            row = con.execute(
                "SELECT salt, password_hash FROM users WHERE username = ?", (username,)
            ).fetchone()
        if row is None:
            _hash_password(password, b"timing-equaliser")  # don't reveal which usernames exist
            return False
        return hmac.compare_digest(_hash_password(password, row[0]), row[1])

    def change_password(self, username: str, old: str, new: str) -> str | None:
        if not self.check(username, old):
            return "Your current password is wrong."
        if len(new) < MIN_PASSWORD_LENGTH:
            return f"Passwords need at least {MIN_PASSWORD_LENGTH} characters."
        salt = os.urandom(16)
        with closing(self._connect()) as con, con:
            con.execute(
                "UPDATE users SET salt = ?, password_hash = ? WHERE username = ?",
                (salt, _hash_password(new, salt), username),
            )
        return None

    def adopt_unowned_data(self, username: str, legacy_dir: Path) -> tuple[list[str], list[str]]:
        """Give the data from before accounts existed to the first user.

        That's data/expenses.db (with its backups) if it exists, else the old CSV files.
        """
        old_db = self.data_dir / "expenses.db"
        store = self.store_for(username)
        if old_db.exists():
            store.db_path.unlink(missing_ok=True)
            shutil.move(old_db, store.db_path)
            if (self.data_dir / "backups").exists():
                shutil.move(self.data_dir / "backups", store.backup_dir)
            return ["Your existing data was moved into this account."], []
        return store.import_legacy_files(legacy_dir)
