import json
import logging
import secrets
import sqlite3
import string
import threading
from datetime import date
from pathlib import Path

from config import DB_PATH

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 640

_db_parent = Path(DB_PATH).expanduser().parent
if str(_db_parent) not in ("", "."):
    _db_parent.mkdir(parents=True, exist_ok=True)

LOCK = threading.RLock()
conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA busy_timeout=30000")
conn.execute("PRAGMA foreign_keys=ON")
try:
    conn.execute("PRAGMA journal_mode=WAL")
except sqlite3.DatabaseError:
    # Read-only/legacy environments may not allow switching journal mode.
    pass
conn.execute("PRAGMA synchronous=NORMAL")


class _ThreadLocalCursor:
    """Expose the old ``db.cur`` API without sharing one cursor across threads.

    SQLite connections can be shared with ``check_same_thread=False``, but a
    cursor cannot be used recursively or by two threads at once.  Several bot
    helpers intentionally expose ``db.cur`` for backwards compatibility, so a
    thread-local cursor plus the shared re-entrant lock is the least disruptive
    safe migration.
    """

    def __init__(self, connection):
        self._connection = connection
        self._local = threading.local()

    def _get(self):
        cursor = getattr(self._local, "cursor", None)
        if cursor is None:
            cursor = self._connection.cursor()
            self._local.cursor = cursor
        return cursor

    def execute(self, *args, **kwargs):
        with LOCK:
            self._get().execute(*args, **kwargs)
        return self

    def executemany(self, *args, **kwargs):
        with LOCK:
            self._get().executemany(*args, **kwargs)
        return self

    def executescript(self, *args, **kwargs):
        with LOCK:
            self._get().executescript(*args, **kwargs)
        return self

    def fetchone(self):
        with LOCK:
            return self._get().fetchone()

    def fetchall(self):
        with LOCK:
            return self._get().fetchall()

    @property
    def lastrowid(self):
        with LOCK:
            return self._get().lastrowid

    @property
    def rowcount(self):
        with LOCK:
            return self._get().rowcount


cur = _ThreadLocalCursor(conn)


class PurchaseError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _columns(table: str):
    cur.execute(f"PRAGMA table_info({table})")
    return {row["name"] for row in cur.fetchall()}


def _add_column_if_missing(table: str, column: str, definition: str):
    if column not in _columns(table):
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _bump_daily_tx(field, amount=1):
    statements = {
        "new_users": "UPDATE daily_stats SET new_users=new_users+? WHERE day=?",
        "sales": "UPDATE daily_stats SET sales=sales+? WHERE day=?",
        "referral_rewards": (
            "UPDATE daily_stats SET referral_rewards=referral_rewards+? WHERE day=?"
        ),
    }
    statement = statements.get(field)
    if statement is None:
        raise ValueError(f"unknown stat field: {field}")
    today = date.today().isoformat()
    cur.execute("INSERT OR IGNORE INTO daily_stats(day) VALUES (?)", (today,))
    cur.execute(statement, (int(amount), today))


def init():
    with LOCK:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users(
                id TEXT PRIMARY KEY,
                username TEXT DEFAULT '',
                ref TEXT,
                balance INTEGER DEFAULT 0,
                purchased INTEGER DEFAULT 0,
                rewarded INTEGER DEFAULT 0,
                rewarded_at TEXT,
                banned INTEGER DEFAULT 0,
                joined_at TEXT DEFAULT (datetime('now')),
                last_active TEXT DEFAULT (datetime('now'))
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS subs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                link TEXT NOT NULL,
                used INTEGER DEFAULT 0,
                owner TEXT,
                added_at TEXT DEFAULT (datetime('now')),
                assigned_at TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_stats(
                day TEXT PRIMARY KEY,
                new_users INTEGER DEFAULT 0,
                sales INTEGER DEFAULT 0,
                referral_rewards INTEGER DEFAULT 0
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS settings(
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS topups(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                amount INTEGER NOT NULL,
                status TEXT DEFAULT 'awaiting_receipt',
                created_at TEXT DEFAULT (datetime('now')),
                reviewed_at TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS receipts(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_unique_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                topup_id INTEGER,
                created_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS tickets(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                status TEXT DEFAULT 'open',
                created_at TEXT DEFAULT (datetime('now')),
                closed_at TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS ticket_messages(
                admin_id TEXT,
                message_id INTEGER,
                ticket_id INTEGER,
                user_id TEXT,
                PRIMARY KEY (admin_id, message_id)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS messages(
                key TEXT PRIMARY KEY,
                text TEXT,
                photo_file_id TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS purchases(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                unit_price INTEGER NOT NULL,
                status TEXT DEFAULT 'completed',
                note TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS purchase_items(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                purchase_id INTEGER NOT NULL,
                sub_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                account_name TEXT,
                link TEXT,
                price_paid INTEGER,
                assigned_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS ledger(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                action TEXT NOT NULL,
                amount INTEGER NOT NULL,
                balance_before INTEGER,
                balance_after INTEGER,
                note TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS custom_buttons(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                button_type TEXT,
                payload TEXT,
                location TEXT DEFAULT 'main',
                sort_order INTEGER DEFAULT 100,
                is_active INTEGER DEFAULT 1,
                audience TEXT DEFAULT 'all',
                starts_at TEXT,
                ends_at TEXT,
                status TEXT DEFAULT 'draft',
                draft_title TEXT,
                draft_button_type TEXT,
                draft_payload TEXT,
                draft_location TEXT,
                draft_sort_order INTEGER,
                draft_is_active INTEGER,
                draft_audience TEXT,
                draft_starts_at TEXT,
                draft_ends_at TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                published_at TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS backup_logs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id TEXT,
                operation_type TEXT NOT NULL,
                backup_file_name TEXT,
                file_size INTEGER,
                status TEXT DEFAULT 'ok',
                note TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS broadcast_logs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id TEXT NOT NULL,
                scope TEXT NOT NULL,
                content_type TEXT NOT NULL,
                preview TEXT,
                total INTEGER DEFAULT 0,
                success INTEGER DEFAULT 0,
                failed INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_logs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id TEXT,
                action_type TEXT NOT NULL,
                target_user_id TEXT,
                details TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
            """
        )


        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS trial_claims(
                user_id TEXT PRIMARY KEY,
                panel_username TEXT UNIQUE,
                provider_key TEXT DEFAULT 'youpanel',
                sub_id INTEGER,
                status TEXT DEFAULT 'pending',
                error TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS plan_categories(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                emoji TEXT DEFAULT '',
                description TEXT DEFAULT '',
                sort_order INTEGER DEFAULT 100,
                is_active INTEGER DEFAULT 1,
                audience TEXT DEFAULT 'all',
                starts_at TEXT,
                ends_at TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_menu_items(
                key TEXT PRIMARY KEY,
                default_title TEXT NOT NULL,
                title TEXT,
                callback_data TEXT NOT NULL,
                sort_order INTEGER DEFAULT 100,
                is_active INTEGER DEFAULT 1,
                updated_at TEXT DEFAULT (datetime('now'))
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS plans(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                volume_label TEXT DEFAULT '',
                duration_label TEXT DEFAULT '',
                price INTEGER NOT NULL DEFAULT 0,
                description TEXT DEFAULT '',
                sort_order INTEGER DEFAULT 100,
                is_active INTEGER DEFAULT 1,
                is_default INTEGER DEFAULT 0,
                max_per_order INTEGER DEFAULT 4,
                cost_price INTEGER DEFAULT 0,
                tag TEXT DEFAULT '',
                show_stock INTEGER DEFAULT 1,
                low_stock_threshold INTEGER DEFAULT 5,
                pre_purchase_text TEXT DEFAULT '',
                post_purchase_text TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS system_buttons(
                key TEXT PRIMARY KEY,
                default_title TEXT NOT NULL,
                title TEXT,
                location TEXT DEFAULT 'main',
                sort_order INTEGER DEFAULT 100,
                is_active INTEGER DEFAULT 1,
                updated_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_messages(
                chat_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                context TEXT DEFAULT '',
                kind TEXT DEFAULT 'menu',
                created_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY(chat_id, message_id)
            )
            """
        )

        _add_column_if_missing("users", "display_name", "TEXT DEFAULT ''")
        _add_column_if_missing("users", "admin_note", "TEXT DEFAULT ''")
        _add_column_if_missing("users", "is_test", "INTEGER DEFAULT 0")
        _add_column_if_missing("subs", "price_paid", "INTEGER")
        _add_column_if_missing("subs", "account_name", "TEXT")
        _add_column_if_missing("subs", "status", "TEXT")
        _add_column_if_missing("subs", "purchase_id", "INTEGER")
        _add_column_if_missing("subs", "plan_id", "INTEGER")
        _add_column_if_missing("purchases", "plan_id", "INTEGER")
        _add_column_if_missing("purchase_items", "plan_id", "INTEGER")
        _add_column_if_missing("topups", "target_quantity", "INTEGER")
        _add_column_if_missing("topups", "target_plan_id", "INTEGER")
        _add_column_if_missing("topups", "target_total", "INTEGER")
        _add_column_if_missing("topups", "target_unit_price", "INTEGER")
        _add_column_if_missing("topups", "purchase_completed_at", "TEXT")
        _add_column_if_missing("purchase_items", "link", "TEXT")
        _add_column_if_missing("purchase_items", "status", "TEXT DEFAULT 'active'")
        _add_column_if_missing("purchase_items", "reverted_at", "TEXT")
        _add_column_if_missing("purchase_items", "reverted_by", "TEXT")
        _add_column_if_missing("purchase_items", "revert_reason", "TEXT")
        _add_column_if_missing("messages", "draft_text", "TEXT")
        _add_column_if_missing("messages", "draft_photo_file_id", "TEXT")
        _add_column_if_missing("messages", "updated_at", "TEXT")
        _add_column_if_missing("messages", "published_at", "TEXT")
        _add_column_if_missing("plans", "pre_purchase_text", "TEXT DEFAULT ''")
        _add_column_if_missing("plans", "post_purchase_text", "TEXT DEFAULT ''")
        _add_column_if_missing("plans", "delivery_type", "TEXT DEFAULT 'pool'")
        _add_column_if_missing("plans", "panel_data_limit_bytes", "INTEGER DEFAULT 0")
        _add_column_if_missing("plans", "panel_duration_days", "INTEGER DEFAULT 0")
        _add_column_if_missing("plans", "panel_start_mode", "TEXT DEFAULT 'on_hold'")
        _add_column_if_missing("plans", "panel_reset_strategy", "TEXT DEFAULT 'no_reset'")
        _add_column_if_missing("plans", "panel_max_devices", "INTEGER")
        _add_column_if_missing("plans", "category_id", "INTEGER")
        _add_column_if_missing("plans", "purchase_mode", "TEXT DEFAULT 'quantity'")
        _add_column_if_missing("plans", "provider_key", "TEXT DEFAULT 'pool'")
        _add_column_if_missing("plans", "provider_options_json", "TEXT DEFAULT '{}'")
        _add_column_if_missing("plans", "unlimited_volume", "INTEGER DEFAULT 0")
        _add_column_if_missing("subs", "source_type", "TEXT DEFAULT 'pool'")
        _add_column_if_missing("subs", "panel_provider", "TEXT")
        _add_column_if_missing("subs", "panel_username", "TEXT")
        _add_column_if_missing("subs", "panel_status", "TEXT")
        _add_column_if_missing("subs", "panel_data_limit", "INTEGER")
        _add_column_if_missing("subs", "panel_used_traffic", "INTEGER DEFAULT 0")
        _add_column_if_missing("subs", "panel_expires_at", "INTEGER")
        _add_column_if_missing("subs", "panel_duration_seconds", "INTEGER")
        _add_column_if_missing("subs", "is_trial", "INTEGER DEFAULT 0")
        _add_column_if_missing("subs", "last_synced_at", "TEXT")
        _add_column_if_missing("purchases", "provider", "TEXT DEFAULT 'pool'")
        _add_column_if_missing("trial_claims", "provider_key", "TEXT DEFAULT 'youpanel'")
        _add_column_if_missing("purchases", "provision_error", "TEXT")
        _add_column_if_missing("purchases", "completed_at", "TEXT")
        _add_column_if_missing("purchases", "refunded_at", "TEXT")
        _add_column_if_missing("bot_messages", "kind", "TEXT DEFAULT 'menu'")
        _add_column_if_missing("purchases", "is_test", "INTEGER DEFAULT 0")
        _add_column_if_missing("topups", "is_test", "INTEGER DEFAULT 0")
        _add_column_if_missing("ledger", "is_test", "INTEGER DEFAULT 0")
        # Duplicate Telegram file IDs must remain recordable so each suspicious
        # topup keeps its own audit row. Detection is done by counting prior
        # uses, therefore this index must never be UNIQUE.
        cur.execute("DROP INDEX IF EXISTS idx_receipts_unique_file")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_receipts_file ON receipts(file_unique_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_custom_buttons_location ON custom_buttons(location, sort_order)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_backup_logs_created ON backup_logs(created_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_subs_plan_used ON subs(plan_id, used)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_subs_owner_used ON subs(owner, used)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_subs_purchase ON subs(purchase_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_purchases_user_created ON purchases(user_id, created_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_purchases_status_created ON purchases(status, created_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_topups_user_status ON topups(user_id, status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_topups_status_reviewed ON topups(status, reviewed_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ledger_user_created ON ledger(user_id, created_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_purchase_items_purchase ON purchase_items(purchase_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_purchase_items_sub ON purchase_items(sub_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_subs_source_owner ON subs(source_type, owner, used)")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_subs_panel_username ON subs(panel_username) WHERE panel_username IS NOT NULL AND TRIM(panel_username)<>''")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_trial_claims_status ON trial_claims(status, updated_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tickets_user_status ON tickets(user_id, status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_bot_messages_user ON bot_messages(user_id, created_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_admin_logs_created ON admin_logs(created_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_users_joined ON users(joined_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_users_last_active ON users(last_active)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_plan_categories_active_order ON plan_categories(is_active, sort_order)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_plans_category_active_order ON plans(category_id, is_active, sort_order)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_plans_provider ON plans(provider_key, is_active)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_admin_menu_order ON admin_menu_items(is_active, sort_order)")

        cur.execute("SELECT link, COUNT(*) AS c FROM subs GROUP BY link HAVING c > 1 LIMIT 1")
        if cur.fetchone() is None:
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_subs_unique_link ON subs(link)")
        else:
            cur.execute("CREATE INDEX IF NOT EXISTS idx_subs_link ON subs(link)")

        # Legacy databases may already contain duplicate links and therefore
        # cannot receive a UNIQUE index. These triggers still block every new
        # duplicate without deleting historical/sold records.
        cur.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_subs_no_duplicate_insert
            BEFORE INSERT ON subs
            WHEN EXISTS (SELECT 1 FROM subs WHERE link=NEW.link)
            BEGIN
                SELECT RAISE(ABORT, 'duplicate sub link');
            END
            """
        )
        cur.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_subs_no_duplicate_update
            BEFORE UPDATE OF link ON subs
            WHEN NEW.link<>OLD.link AND EXISTS (SELECT 1 FROM subs WHERE link=NEW.link)
            BEGIN
                SELECT RAISE(ABORT, 'duplicate sub link');
            END
            """
        )

        _ensure_default_plan()
        _ensure_default_categories()
        _ensure_system_buttons()
        _ensure_admin_menu_items()
        _migrate_catalog_assignments()
        cur.execute("UPDATE system_buttons SET location='buy', updated_at=datetime('now') WHERE key='trial' AND location='main'")
        cur.execute("UPDATE subs SET plan_id=? WHERE plan_id IS NULL", (default_plan_id(),))

        cur.execute("UPDATE subs SET status='available' WHERE status IS NULL AND used=0")
        cur.execute("UPDATE subs SET status='delivered' WHERE status IS NULL AND used=1")
        cur.execute("UPDATE subs SET source_type='pool' WHERE source_type IS NULL OR TRIM(source_type)=''")
        cur.execute("UPDATE plans SET delivery_type='pool' WHERE delivery_type IS NULL OR TRIM(delivery_type)=''")
        cur.execute("UPDATE plans SET provider_key=CASE WHEN COALESCE(delivery_type,'pool')='youpanel' THEN 'youpanel' ELSE 'pool' END WHERE provider_key IS NULL OR TRIM(provider_key)='' OR provider_key='pool'")
        cur.execute("UPDATE plans SET purchase_mode='quantity' WHERE purchase_mode IS NULL OR purchase_mode NOT IN ('direct','quantity','wholesale','disabled')")
        cur.execute("UPDATE plans SET provider_options_json='{}' WHERE provider_options_json IS NULL OR TRIM(provider_options_json)=''")
        cur.execute("UPDATE purchases SET provider='pool' WHERE provider IS NULL OR TRIM(provider)=''")
        cur.execute("UPDATE trial_claims SET provider_key='youpanel' WHERE provider_key IS NULL OR TRIM(provider_key)=''")
        cur.execute("UPDATE purchases SET is_test=1 WHERE user_id IN (SELECT id FROM users WHERE COALESCE(is_test,0)=1)")
        cur.execute("UPDATE topups SET is_test=1 WHERE user_id IN (SELECT id FROM users WHERE COALESCE(is_test,0)=1)")
        cur.execute("UPDATE ledger SET is_test=1 WHERE user_id IN (SELECT id FROM users WHERE COALESCE(is_test,0)=1)")
        cur.execute(
            "INSERT INTO settings(key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        cur.execute("DELETE FROM bot_messages WHERE created_at < datetime('now', '-30 days')")
        _backfill_missing_account_names()
        conn.commit()


def _random_token(length=6):
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def generate_service_code():
    for _ in range(50):
        candidate = f"Berserk {_random_token(6)}"
        cur.execute("SELECT 1 FROM subs WHERE account_name=?", (candidate,))
        if cur.fetchone() is None:
            return candidate
    return f"Berserk {_random_token(8)}"


def _backfill_missing_account_names():
    cur.execute("SELECT id FROM subs WHERE account_name IS NULL OR TRIM(account_name)=''")
    rows = cur.fetchall()
    for row in rows:
        cur.execute(
            "UPDATE subs SET account_name=? WHERE id=?",
            (generate_service_code(), row["id"]),
        )


def get_user(user_id):
    cur.execute("SELECT * FROM users WHERE id=?", (str(user_id),))
    return cur.fetchone()


def get_or_create_user(user_id, username=None, ref=None, display_name=None):
    user_id = str(user_id)
    with LOCK:
        row = get_user(user_id)
        if row:
            return row, False
        if ref is not None:
            ref = str(ref)
            if ref == user_id or not get_user(ref):
                ref = None
        cur.execute(
            "INSERT INTO users(id, username, ref, display_name) VALUES (?, ?, ?, ?)",
            (user_id, username or "", ref, display_name or ""),
        )
        _bump_daily_tx("new_users")
        conn.commit()
        return get_user(user_id), True


def touch_active(user_id, username=None, display_name=None):
    user_id = str(user_id)
    with LOCK:
        if username is not None and display_name is not None:
            cur.execute(
                "UPDATE users SET last_active=datetime('now'), username=?, display_name=? WHERE id=?",
                (username or "", display_name or "", user_id),
            )
        elif username is not None:
            cur.execute(
                "UPDATE users SET last_active=datetime('now'), username=? WHERE id=?",
                (username or "", user_id),
            )
        elif display_name is not None:
            cur.execute(
                "UPDATE users SET last_active=datetime('now'), display_name=? WHERE id=?",
                (display_name or "", user_id),
            )
        else:
            cur.execute("UPDATE users SET last_active=datetime('now') WHERE id=?", (user_id,))
        conn.commit()


def add_balance(user_id, amount, action="balance_adjustment", note=""):
    user_id = str(user_id)
    amount = int(amount)
    with LOCK:
        row = get_user(user_id)
        if not row:
            get_or_create_user(user_id)
            row = get_user(user_id)
        before = int(row["balance"] or 0)
        after = before + amount
        if after < 0:
            raise ValueError("موجودی کیف پول نمی‌تواند منفی شود.")
        is_test = int(row["is_test"] or 0) if "is_test" in row.keys() else 0
        cur.execute("UPDATE users SET balance=? WHERE id=?", (after, user_id))
        cur.execute(
            """
            INSERT INTO ledger(user_id, action, amount, balance_before, balance_after, note, is_test)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, action, amount, before, after, note or "", is_test),
        )
        conn.commit()
        return after


def set_ban(user_id, banned: bool):
    cur.execute("UPDATE users SET banned=? WHERE id=?", (1 if banned else 0, str(user_id)))
    conn.commit()


def mark_rewarded(referred_id):
    cur.execute(
        "UPDATE users SET rewarded=1, rewarded_at=datetime('now') WHERE id=?",
        (str(referred_id),),
    )
    conn.commit()


def increment_purchased(user_id, amount=1):
    cur.execute(
        "UPDATE users SET purchased = purchased + ? WHERE id=?",
        (int(amount), str(user_id)),
    )
    conn.commit()


def search_users(query, limit=10):
    q = f"%{str(query).strip().lstrip('@')}%"
    cur.execute(
        """
        SELECT * FROM users
        WHERE id LIKE ? OR username LIKE ? OR display_name LIKE ?
        ORDER BY joined_at ASC
        LIMIT ?
        """,
        (q, q, q, int(limit)),
    )
    return cur.fetchall()


def list_users(offset=0, limit=10):
    cur.execute(
        "SELECT * FROM users ORDER BY joined_at ASC LIMIT ? OFFSET ?",
        (int(limit), int(offset)),
    )
    return cur.fetchall()


def list_users_with_stats(offset=0, limit=10):
    cur.execute(
        """
        SELECT u.*,
               (SELECT COUNT(*) FROM subs s WHERE s.owner=u.id AND s.used=1) AS delivered_count,
               (SELECT COUNT(*) FROM topups t WHERE t.user_id=u.id AND t.status='approved') AS approved_topup_count,
               (SELECT COUNT(*) FROM tickets tk WHERE tk.user_id=u.id) AS ticket_count,
               (SELECT COUNT(*) FROM users child WHERE child.ref=u.id AND COALESCE(child.is_test,0)=0) AS referral_count,
               (SELECT COUNT(*) FROM users child WHERE child.ref=u.id AND COALESCE(child.is_test,0)=1) AS referral_test_count,
               (SELECT COUNT(*) FROM users child WHERE child.ref=u.id AND child.rewarded=1 AND COALESCE(child.is_test,0)=0) AS rewarded_referral_count
        FROM users u
        ORDER BY u.joined_at ASC, u.id ASC
        LIMIT ? OFFSET ?
        """,
        (int(limit), int(offset)),
    )
    return cur.fetchall()


# گروه‌بندی یکپارچه کاربران برای پنل مدیریت و پیام‌های هدفمند.
# همه شرط‌ها ثابت و داخلی هستند تا هیچ SQL دلخواهی از Callback وارد نشود.
_USER_SEGMENT_WHERE = {
    "all": "1=1",
    "buyers": "u.banned=0 AND COALESCE(u.is_test,0)=0 AND EXISTS (SELECT 1 FROM purchases p WHERE p.user_id=u.id AND p.status='completed' AND COALESCE(p.is_test,0)=0)",
    "no_buy": "u.banned=0 AND COALESCE(u.is_test,0)=0 AND NOT EXISTS (SELECT 1 FROM purchases p WHERE p.user_id=u.id AND p.status='completed' AND COALESCE(p.is_test,0)=0)",
    "has_sub": "u.banned=0 AND COALESCE(u.is_test,0)=0 AND EXISTS (SELECT 1 FROM subs s WHERE s.owner=u.id AND s.used=1 AND COALESCE(s.is_trial,0)=0)",
    "no_sub": "u.banned=0 AND COALESCE(u.is_test,0)=0 AND NOT EXISTS (SELECT 1 FROM subs s WHERE s.owner=u.id AND s.used=1 AND COALESCE(s.is_trial,0)=0)",
    "new7": "u.banned=0 AND COALESCE(u.is_test,0)=0 AND u.joined_at>=datetime('now','-7 days')",
    "new30": "u.banned=0 AND COALESCE(u.is_test,0)=0 AND u.joined_at>=datetime('now','-30 days')",
    "active7": "u.banned=0 AND COALESCE(u.is_test,0)=0 AND u.last_active>=datetime('now','-7 days')",
    "inactive30_buyers": "u.banned=0 AND COALESCE(u.is_test,0)=0 AND (u.last_active<datetime('now','-30 days') OR u.last_active IS NULL) AND EXISTS (SELECT 1 FROM purchases p WHERE p.user_id=u.id AND p.status='completed' AND COALESCE(p.is_test,0)=0)",
    "payment_problem30": "u.banned=0 AND COALESCE(u.is_test,0)=0 AND (EXISTS (SELECT 1 FROM topups t WHERE t.user_id=u.id AND t.status='rejected' AND COALESCE(t.reviewed_at,t.created_at)>=datetime('now','-30 days')) OR EXISTS (SELECT 1 FROM purchases p WHERE p.user_id=u.id AND p.status IN ('failed','retry','admin_review','refunded') AND p.created_at>=datetime('now','-30 days') AND COALESCE(p.is_test,0)=0))",
    "expiring3": "u.banned=0 AND COALESCE(u.is_test,0)=0 AND EXISTS (SELECT 1 FROM subs s WHERE s.owner=u.id AND s.used=1 AND COALESCE(s.is_trial,0)=0 AND s.panel_expires_at IS NOT NULL AND CAST(s.panel_expires_at AS INTEGER)>CAST(strftime('%s','now') AS INTEGER) AND CAST(s.panel_expires_at AS INTEGER)<=CAST(strftime('%s','now','+3 days') AS INTEGER))",
    "low_volume20": "u.banned=0 AND COALESCE(u.is_test,0)=0 AND EXISTS (SELECT 1 FROM subs s WHERE s.owner=u.id AND s.used=1 AND COALESCE(s.is_trial,0)=0 AND COALESCE(s.panel_data_limit,0)>0 AND COALESCE(s.panel_used_traffic,0)>=CAST(COALESCE(s.panel_data_limit,0)*0.8 AS INTEGER) AND COALESCE(s.panel_used_traffic,0)<COALESCE(s.panel_data_limit,0))",
    "zero_usage7": "u.banned=0 AND COALESCE(u.is_test,0)=0 AND EXISTS (SELECT 1 FROM subs s WHERE s.owner=u.id AND s.used=1 AND COALESCE(s.is_trial,0)=0 AND COALESCE(s.source_type,'pool')!='pool' AND COALESCE(s.panel_used_traffic,0)=0 AND s.assigned_at<=datetime('now','-7 days'))",
    "valuable": "u.banned=0 AND COALESCE(u.is_test,0)=0 AND COALESCE((SELECT SUM(p.amount) FROM purchases p WHERE p.user_id=u.id AND p.status='completed' AND COALESCE(p.is_test,0)=0),0)>=1000000",
    "returning": "u.banned=0 AND COALESCE(u.is_test,0)=0 AND (SELECT COUNT(*) FROM purchases p WHERE p.user_id=u.id AND p.status='completed' AND COALESCE(p.is_test,0)=0)>=2",
    "open_ticket": "u.banned=0 AND COALESCE(u.is_test,0)=0 AND EXISTS (SELECT 1 FROM tickets tk WHERE tk.user_id=u.id AND tk.status='open')",
    "positive_balance_no_buy": "u.banned=0 AND COALESCE(u.is_test,0)=0 AND COALESCE(u.balance,0)>0 AND NOT EXISTS (SELECT 1 FROM purchases p WHERE p.user_id=u.id AND p.status='completed' AND COALESCE(p.is_test,0)=0)",
    "attention": "u.banned=0 AND COALESCE(u.is_test,0)=0 AND (EXISTS (SELECT 1 FROM tickets tk WHERE tk.user_id=u.id AND tk.status='open') OR EXISTS (SELECT 1 FROM topups t WHERE t.user_id=u.id AND t.status='rejected' AND COALESCE(t.reviewed_at,t.created_at)>=datetime('now','-30 days')) OR EXISTS (SELECT 1 FROM purchases p WHERE p.user_id=u.id AND p.status IN ('failed','retry','admin_review') AND p.created_at>=datetime('now','-30 days') AND COALESCE(p.is_test,0)=0) OR EXISTS (SELECT 1 FROM subs s WHERE s.owner=u.id AND s.used=1 AND COALESCE(s.is_trial,0)=0 AND s.panel_expires_at IS NOT NULL AND CAST(s.panel_expires_at AS INTEGER)>CAST(strftime('%s','now') AS INTEGER) AND CAST(s.panel_expires_at AS INTEGER)<=CAST(strftime('%s','now','+3 days') AS INTEGER)))",
    "banned": "u.banned=1",
    "test": "COALESCE(u.is_test,0)=1",
}


def _user_segment_where(segment):
    segment = str(segment or "all")
    if segment not in _USER_SEGMENT_WHERE:
        raise ValueError(f"unknown user segment: {segment}")
    return _USER_SEGMENT_WHERE[segment]


def count_user_segment(segment):
    where = _user_segment_where(segment)
    cur.execute(f"SELECT COUNT(*) AS c FROM users u WHERE {where}")
    return int(cur.fetchone()["c"] or 0)


def list_user_segment(segment, offset=0, limit=6):
    where = _user_segment_where(segment)
    cur.execute(
        f"""
        SELECT u.*,
               (SELECT COUNT(*) FROM purchases p WHERE p.user_id=u.id AND p.status='completed' AND COALESCE(p.is_test,0)=0) AS purchase_count,
               COALESCE((SELECT SUM(p.amount) FROM purchases p WHERE p.user_id=u.id AND p.status='completed' AND COALESCE(p.is_test,0)=0),0) AS spent_total,
               (SELECT COUNT(*) FROM subs s WHERE s.owner=u.id AND s.used=1 AND COALESCE(s.is_trial,0)=0) AS delivered_count,
               (SELECT COUNT(*) FROM tickets tk WHERE tk.user_id=u.id AND tk.status='open') AS open_ticket_count,
               (SELECT COUNT(*) FROM users child WHERE child.ref=u.id AND COALESCE(child.is_test,0)=0) AS referral_count,
               (SELECT COUNT(*) FROM users child WHERE child.ref=u.id AND COALESCE(child.is_test,0)=1) AS referral_test_count,
               (SELECT COUNT(*) FROM users child WHERE child.ref=u.id AND child.rewarded=1 AND COALESCE(child.is_test,0)=0) AS rewarded_referral_count
        FROM users u
        WHERE {where}
        ORDER BY u.joined_at DESC, u.id DESC
        LIMIT ? OFFSET ?
        """,
        (max(1, min(20, int(limit))), max(0, int(offset))),
    )
    return cur.fetchall()


def user_segment_counts(segments=None):
    names = list(segments or _USER_SEGMENT_WHERE.keys())
    return {name: count_user_segment(name) for name in names if name in _USER_SEGMENT_WHERE}


def user_insights(days=7):
    days = max(1, min(365, int(days)))
    period = f"-{days} days"
    result = {"days": days}
    cur.execute("SELECT COUNT(*) AS c FROM users u WHERE u.banned=0 AND COALESCE(u.is_test,0)=0 AND u.joined_at>=datetime('now',?)", (period,))
    result["new_users"] = int(cur.fetchone()["c"] or 0)
    cur.execute(
        """
        SELECT COUNT(*) AS c FROM users u
        WHERE u.banned=0 AND COALESCE(u.is_test,0)=0 AND u.joined_at>=datetime('now',?)
          AND EXISTS (SELECT 1 FROM purchases p WHERE p.user_id=u.id AND p.status='completed' AND COALESCE(p.is_test,0)=0)
        """,
        (period,),
    )
    result["new_buyers"] = int(cur.fetchone()["c"] or 0)
    result["new_without_buy"] = max(0, result["new_users"] - result["new_buyers"])
    result["conversion_rate"] = round(result["new_buyers"] * 100 / result["new_users"], 1) if result["new_users"] else 0.0
    cur.execute(
        """
        SELECT COUNT(*) AS c FROM (
            SELECT user_id, MIN(created_at) AS first_purchase
            FROM purchases
            WHERE status='completed' AND COALESCE(is_test,0)=0
            GROUP BY user_id
            HAVING first_purchase>=datetime('now',?)
        ) q
        """,
        (period,),
    )
    result["first_buyers"] = int(cur.fetchone()["c"] or 0)
    cur.execute(
        """
        SELECT COUNT(DISTINCT p.user_id) AS c
        FROM purchases p
        WHERE p.status='completed' AND COALESCE(p.is_test,0)=0 AND p.created_at>=datetime('now',?)
          AND EXISTS (SELECT 1 FROM purchases old WHERE old.user_id=p.user_id AND old.status='completed' AND COALESCE(old.is_test,0)=0 AND old.created_at<datetime('now',?))
        """,
        (period, period),
    )
    result["returning_buyers"] = int(cur.fetchone()["c"] or 0)
    cur.execute(
        """
        SELECT COUNT(DISTINCT u.id) AS c FROM users u
        WHERE u.banned=0 AND COALESCE(u.is_test,0)=0 AND (
          EXISTS (SELECT 1 FROM topups t WHERE t.user_id=u.id AND t.status='rejected' AND COALESCE(t.reviewed_at,t.created_at)>=datetime('now',?))
          OR EXISTS (SELECT 1 FROM purchases p WHERE p.user_id=u.id AND p.status IN ('failed','retry','admin_review','refunded') AND p.created_at>=datetime('now',?) AND COALESCE(p.is_test,0)=0)
        )
        """,
        (period, period),
    )
    result["payment_problems"] = int(cur.fetchone()["c"] or 0)
    result["expiring3"] = count_user_segment("expiring3")
    result["inactive30_buyers"] = count_user_segment("inactive30_buyers")
    result["valuable"] = count_user_segment("valuable")
    result["open_ticket"] = count_user_segment("open_ticket")
    result["positive_balance_no_buy"] = count_user_segment("positive_balance_no_buy")
    result["zero_usage7"] = count_user_segment("zero_usage7")
    return result


def service_report_summary():
    result = {}
    cur.execute("SELECT COUNT(*) AS c FROM subs WHERE used=1 AND COALESCE(is_trial,0)=0")
    result["delivered"] = int(cur.fetchone()["c"] or 0)
    cur.execute("SELECT COUNT(*) AS c FROM subs WHERE used=1 AND COALESCE(is_trial,0)=0 AND COALESCE(source_type,'pool')='pool'")
    result["pool"] = int(cur.fetchone()["c"] or 0)
    cur.execute("SELECT COUNT(*) AS c FROM subs WHERE used=1 AND COALESCE(is_trial,0)=0 AND COALESCE(source_type,'pool')!='pool'")
    result["provider"] = int(cur.fetchone()["c"] or 0)
    cur.execute("SELECT COUNT(*) AS c FROM subs WHERE used=0 AND COALESCE(source_type,'pool')='pool' AND COALESCE(status,'available') IN ('available','returned_to_pool')")
    result["stock"] = int(cur.fetchone()["c"] or 0)
    cur.execute("SELECT COUNT(*) AS c FROM subs WHERE used=1 AND COALESCE(is_trial,0)=0 AND panel_expires_at IS NOT NULL AND CAST(panel_expires_at AS INTEGER)<=CAST(strftime('%s','now') AS INTEGER)")
    result["expired"] = int(cur.fetchone()["c"] or 0)
    result["expiring3_users"] = count_user_segment("expiring3")
    result["low_volume_users"] = count_user_segment("low_volume20")
    result["zero_usage_users"] = count_user_segment("zero_usage7")
    cur.execute("SELECT COALESCE(SUM(panel_data_limit),0) AS total_limit,COALESCE(SUM(panel_used_traffic),0) AS total_used FROM subs WHERE used=1 AND COALESCE(is_trial,0)=0 AND COALESCE(source_type,'pool')!='pool'")
    row = cur.fetchone()
    result["total_limit"] = int(row["total_limit"] or 0)
    result["total_used"] = int(row["total_used"] or 0)
    return result


def payment_report_summary(days=30):
    days = max(1, min(365, int(days)))
    period = f"-{days} days"
    result = {"days": days}
    for status in ("approved", "pending_review", "rejected", "awaiting_receipt"):
        cur.execute("SELECT COUNT(*) AS c,COALESCE(SUM(amount),0) AS amount FROM topups WHERE status=? AND created_at>=datetime('now',?) AND COALESCE(is_test,0)=0", (status, period))
        row = cur.fetchone()
        result[f"topup_{status}_count"] = int(row["c"] or 0)
        result[f"topup_{status}_amount"] = int(row["amount"] or 0)
    for status in ("completed", "paid", "provisioning", "retry", "admin_review", "failed", "refunded"):
        cur.execute("SELECT COUNT(*) AS c,COALESCE(SUM(amount),0) AS amount FROM purchases WHERE status=? AND created_at>=datetime('now',?) AND COALESCE(is_test,0)=0", (status, period))
        row = cur.fetchone()
        result[f"purchase_{status}_count"] = int(row["c"] or 0)
        result[f"purchase_{status}_amount"] = int(row["amount"] or 0)
    return result


def support_report_summary(days=30):
    days = max(1, min(365, int(days)))
    period = f"-{days} days"
    result = {"days": days}
    cur.execute("SELECT COUNT(*) AS c FROM tickets WHERE created_at>=datetime('now',?)", (period,))
    result["new"] = int(cur.fetchone()["c"] or 0)
    cur.execute("SELECT COUNT(*) AS c FROM tickets WHERE status='open'")
    result["open"] = int(cur.fetchone()["c"] or 0)
    cur.execute("SELECT COUNT(*) AS c FROM tickets WHERE status='closed' AND COALESCE(closed_at,created_at)>=datetime('now',?)", (period,))
    result["closed"] = int(cur.fetchone()["c"] or 0)
    cur.execute("SELECT COUNT(DISTINCT user_id) AS c FROM tickets WHERE status='open'")
    result["users_with_open"] = int(cur.fetchone()["c"] or 0)
    cur.execute("SELECT COUNT(*) AS c FROM (SELECT user_id FROM tickets WHERE created_at>=datetime('now',?) GROUP BY user_id HAVING COUNT(*)>=2)", (period,))
    result["repeat_users"] = int(cur.fetchone()["c"] or 0)
    return result

def count_users():
    cur.execute("SELECT COUNT(*) AS c FROM users")
    return int(cur.fetchone()["c"] or 0)


def active_users_count(days=7, include_test=False):
    sql = "SELECT COUNT(*) AS c FROM users WHERE last_active >= datetime('now', ?)"
    if not include_test:
        sql += " AND COALESCE(is_test,0)=0"
    cur.execute(sql, (f"-{int(days)} days",))
    return int(cur.fetchone()["c"] or 0)


def count_test_users():
    cur.execute("SELECT COUNT(*) AS c FROM users WHERE COALESCE(is_test,0)=1")
    return int(cur.fetchone()["c"] or 0)


def count_real_users():
    cur.execute("SELECT COUNT(*) AS c FROM users WHERE COALESCE(is_test,0)=0")
    return int(cur.fetchone()["c"] or 0)


def sum_all_balances(include_test=False):
    sql = "SELECT COALESCE(SUM(balance),0) AS s FROM users"
    if not include_test:
        sql += " WHERE COALESCE(is_test,0)=0"
    cur.execute(sql)
    return int(cur.fetchone()["s"] or 0)


_BROADCAST_COUNT_SQL = {
    "all": "SELECT COUNT(*) AS c FROM users u WHERE u.banned=0",
    "buyers": "SELECT COUNT(*) AS c FROM users u WHERE " + _USER_SEGMENT_WHERE["buyers"],
    "no_buy": "SELECT COUNT(*) AS c FROM users u WHERE " + _USER_SEGMENT_WHERE["no_buy"],
    "has_sub": "SELECT COUNT(*) AS c FROM users u WHERE " + _USER_SEGMENT_WHERE["has_sub"],
    "no_sub": "SELECT COUNT(*) AS c FROM users u WHERE " + _USER_SEGMENT_WHERE["no_sub"],
    "active7": "SELECT COUNT(*) AS c FROM users u WHERE u.banned=0 AND COALESCE(u.is_test,0)=0 AND u.last_active >= datetime('now', '-7 days')",
    "inactive7": "SELECT COUNT(*) AS c FROM users u WHERE u.banned=0 AND COALESCE(u.is_test,0)=0 AND (u.last_active < datetime('now', '-7 days') OR u.last_active IS NULL)",
    "inactive30_buyers": "SELECT COUNT(*) AS c FROM users u WHERE u.banned=0 AND " + _USER_SEGMENT_WHERE["inactive30_buyers"],
    "new7": "SELECT COUNT(*) AS c FROM users u WHERE u.banned=0 AND " + _USER_SEGMENT_WHERE["new7"],
    "payment_problem30": "SELECT COUNT(*) AS c FROM users u WHERE u.banned=0 AND " + _USER_SEGMENT_WHERE["payment_problem30"],
    "expiring3": "SELECT COUNT(*) AS c FROM users u WHERE u.banned=0 AND " + _USER_SEGMENT_WHERE["expiring3"],
    "low_volume20": "SELECT COUNT(*) AS c FROM users u WHERE u.banned=0 AND " + _USER_SEGMENT_WHERE["low_volume20"],
    "zero_usage7": "SELECT COUNT(*) AS c FROM users u WHERE u.banned=0 AND " + _USER_SEGMENT_WHERE["zero_usage7"],
    "valuable": "SELECT COUNT(*) AS c FROM users u WHERE u.banned=0 AND " + _USER_SEGMENT_WHERE["valuable"],
    "returning": "SELECT COUNT(*) AS c FROM users u WHERE u.banned=0 AND " + _USER_SEGMENT_WHERE["returning"],
    "open_ticket": "SELECT COUNT(*) AS c FROM users u WHERE u.banned=0 AND " + _USER_SEGMENT_WHERE["open_ticket"],
    "positive_balance_no_buy": "SELECT COUNT(*) AS c FROM users u WHERE u.banned=0 AND " + _USER_SEGMENT_WHERE["positive_balance_no_buy"],
    "positive_balance": "SELECT COUNT(*) AS c FROM users u WHERE u.banned=0 AND u.balance > 0",
    "low_balance": "SELECT COUNT(*) AS c FROM users u WHERE u.banned=0 AND u.balance > 0 AND u.balance < COALESCE((SELECT MIN(price) FROM plans WHERE is_active=1), 100000)",
    "referred": "SELECT COUNT(*) AS c FROM users u WHERE u.banned=0 AND u.ref IS NOT NULL AND TRIM(u.ref) <> ''",
    "referrers": "SELECT COUNT(*) AS c FROM users u WHERE u.banned=0 AND EXISTS (SELECT 1 FROM users child WHERE child.ref=u.id)",
}

_BROADCAST_LIST_SQL = {
    scope: sql.replace("SELECT COUNT(*) AS c", "SELECT u.id, u.username, u.purchased, u.balance, u.last_active") + " ORDER BY u.joined_at DESC"
    for scope, sql in _BROADCAST_COUNT_SQL.items()
}



def _broadcast_scope(scope):
    if scope not in _BROADCAST_COUNT_SQL:
        raise ValueError(f"unknown broadcast scope: {scope}")
    return scope


def count_broadcast_targets(scope):
    scope = _broadcast_scope(scope)
    cur.execute(_BROADCAST_COUNT_SQL[scope])
    return cur.fetchone()["c"]


def list_broadcast_targets(scope, limit=None):
    scope = _broadcast_scope(scope)
    sql = _BROADCAST_LIST_SQL[scope]
    params = []
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    cur.execute(sql, params)
    return cur.fetchall()


def log_broadcast(admin_id, scope, content_type, preview, total, success, failed):
    cur.execute(
        """
        INSERT INTO broadcast_logs(admin_id, scope, content_type, preview, total, success, failed)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (str(admin_id), scope, content_type, preview, int(total), int(success), int(failed)),
    )
    conn.commit()
    return cur.lastrowid


def list_broadcast_logs(limit=10):
    cur.execute("SELECT * FROM broadcast_logs ORDER BY id DESC LIMIT ?", (int(limit),))
    return cur.fetchall()


def referral_count(user_id, include_test=False):
    sql = "SELECT COUNT(*) AS c FROM users WHERE ref=?"
    if not include_test:
        sql += " AND COALESCE(is_test,0)=0"
    cur.execute(sql, (str(user_id),))
    return int(cur.fetchone()["c"] or 0)


def referrals_rewarded_today(ref_id):
    cur.execute(
        """
        SELECT COUNT(*) AS c
        FROM users
        WHERE ref=? AND rewarded=1 AND COALESCE(is_test,0)=0 AND date(rewarded_at)=date('now')
        """,
        (str(ref_id),),
    )
    return cur.fetchone()["c"]


def referred_users(user_id, limit=20):
    cur.execute(
        """
        SELECT id, username, display_name, purchased, rewarded, joined_at, is_test
        FROM users
        WHERE ref=?
        ORDER BY joined_at ASC
        LIMIT ?
        """,
        (str(user_id), int(limit)),
    )
    return cur.fetchall()


def rewarded_referral_count(user_id, include_test=False):
    sql = "SELECT COUNT(*) AS c FROM users WHERE ref=? AND rewarded=1"
    if not include_test:
        sql += " AND COALESCE(is_test,0)=0"
    cur.execute(sql, (str(user_id),))
    return int(cur.fetchone()["c"] or 0)


def referral_reward_total(user_id):
    cur.execute(
        """
        SELECT SUM(amount) AS s
        FROM ledger
        WHERE user_id=? AND action='referral_reward'
        """,
        (str(user_id),),
    )
    return cur.fetchone()["s"] or 0


def total_referral_rewards(include_test=False):
    sql = "SELECT COALESCE(SUM(amount),0) AS s FROM ledger WHERE action='referral_reward'"
    if not include_test:
        sql += " AND COALESCE(is_test,0)=0"
    cur.execute(sql)
    return int(cur.fetchone()["s"] or 0)


def bump_daily(field, amount=1):
    with LOCK:
        _bump_daily_tx(field, amount)
        conn.commit()


def recent_daily_stats(days=7):
    """Return report rows derived from source tables, not stale counters.

    ``daily_stats`` is retained for backwards compatibility, but test-account
    reclassification can make stored counters inaccurate.  Building the small
    seven/thirty-day report from authoritative tables keeps the admin dashboard
    consistent immediately after a user is marked as test or real.
    """
    days = max(1, min(int(days), 366))
    cur.execute(
        """
        WITH RECURSIVE dates(day, n) AS (
            SELECT date('now'), 1
            UNION ALL
            SELECT date(day, '-1 day'), n + 1 FROM dates WHERE n < ?
        ),
        users_by_day AS (
            SELECT date(joined_at) AS day, COUNT(*) AS value
            FROM users
            GROUP BY date(joined_at)
        ),
        sales_by_day AS (
            SELECT date(created_at) AS day, COUNT(*) AS value
            FROM purchases
            WHERE status='completed' AND COALESCE(is_test,0)=0
            GROUP BY date(created_at)
        ),
        rewards_by_day AS (
            SELECT date(created_at) AS day, COALESCE(SUM(amount),0) AS value
            FROM ledger
            WHERE action='referral_reward' AND COALESCE(is_test,0)=0
            GROUP BY date(created_at)
        )
        SELECT d.day,
               COALESCE(u.value,0) AS new_users,
               COALESCE(s.value,0) AS sales,
               COALESCE(r.value,0) AS referral_rewards
        FROM dates d
        LEFT JOIN users_by_day u ON u.day=d.day
        LEFT JOIN sales_by_day s ON s.day=d.day
        LEFT JOIN rewards_by_day r ON r.day=d.day
        ORDER BY d.day DESC
        """,
        (days,),
    )
    return cur.fetchall()


def get_setting(key, default=None):
    cur.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = cur.fetchone()
    return row["value"] if row else default


def get_setting_int(key, default=0):
    val = get_setting(key)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        return default


def set_setting(key, value):
    cur.execute(
        """
        INSERT INTO settings(key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (key, str(value)),
    )
    conn.commit()


def create_topup(
    user_id,
    amount,
    target_quantity=None,
    target_plan_id=None,
    target_total=None,
    target_unit_price=None,
    request_key=None,
):
    user = get_user(user_id)
    is_test = int(user["is_test"] or 0) if user and "is_test" in user.keys() else 0
    request_key = (request_key or "").strip()[:180] or None
    with LOCK:
        if request_key:
            cur.execute(
                """SELECT id,user_id,amount,target_quantity,target_plan_id,target_total,target_unit_price
                   FROM topups WHERE request_key=?""",
                (request_key,),
            )
            existing = cur.fetchone()
            if existing:
                expected = (
                    str(user_id),
                    int(amount),
                    int(target_quantity) if target_quantity is not None else None,
                    int(target_plan_id) if target_plan_id is not None else None,
                    int(target_total) if target_total is not None else None,
                    int(target_unit_price) if target_unit_price is not None else None,
                )
                actual = (
                    str(existing["user_id"]),
                    int(existing["amount"]),
                    existing["target_quantity"],
                    existing["target_plan_id"],
                    existing["target_total"],
                    existing["target_unit_price"],
                )
                if actual != expected:
                    raise ValueError("idempotency key conflicts with another top-up")
                return int(existing["id"])
        cur.execute(
            """
            INSERT INTO topups(
                user_id, amount, target_quantity, target_plan_id, target_total,
                target_unit_price, is_test, request_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(user_id),
                int(amount),
                int(target_quantity) if target_quantity is not None else None,
                int(target_plan_id) if target_plan_id is not None else None,
                int(target_total) if target_total is not None else None,
                int(target_unit_price) if target_unit_price is not None else None,
                is_test,
                request_key,
            ),
        )
        conn.commit()
        return cur.lastrowid


def get_topup(topup_id):
    cur.execute("SELECT * FROM topups WHERE id=?", (int(topup_id),))
    return cur.fetchone()


def get_pending_receipt_topup(user_id):
    cur.execute(
        """
        SELECT * FROM topups
        WHERE user_id=? AND status='awaiting_receipt'
        ORDER BY id DESC
        LIMIT 1
        """,
        (str(user_id),),
    )
    return cur.fetchone()


def set_topup_status(topup_id, status):
    cur.execute(
        "UPDATE topups SET status=?, reviewed_at=datetime('now') WHERE id=?",
        (status, int(topup_id)),
    )
    conn.commit()


def list_pending_topups(limit=15):
    cur.execute("SELECT * FROM topups WHERE status='pending_review' ORDER BY id LIMIT ?", (int(limit),))
    return cur.fetchall()


def count_pending_topups():
    cur.execute("SELECT COUNT(*) AS c FROM topups WHERE status='pending_review'")
    return cur.fetchone()["c"]


def sum_approved_topups(include_test=False):
    sql = "SELECT COALESCE(SUM(amount),0) AS s FROM topups WHERE status='approved'"
    if not include_test:
        sql += " AND COALESCE(is_test,0)=0"
    cur.execute(sql)
    return int(cur.fetchone()["s"] or 0)


def user_approved_topup_count(user_id):
    cur.execute(
        "SELECT COUNT(*) AS c FROM topups WHERE user_id=? AND status='approved'",
        (str(user_id),),
    )
    return int(cur.fetchone()["c"] or 0)


def list_user_topups(user_id, limit=10):
    cur.execute(
        """
        SELECT *
        FROM topups
        WHERE user_id=?
        ORDER BY id DESC
        LIMIT ?
        """,
        (str(user_id), int(limit)),
    )
    return cur.fetchall()


def find_receipt(file_unique_id):
    cur.execute("SELECT * FROM receipts WHERE file_unique_id=? ORDER BY id", (file_unique_id,))
    return cur.fetchall()


def record_receipt(file_unique_id, user_id, topup_id):
    try:
        cur.execute(
            "INSERT INTO receipts(file_unique_id, user_id, topup_id) VALUES (?, ?, ?)",
            (file_unique_id, str(user_id), int(topup_id)),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        conn.rollback()
        return False


def submit_topup_receipt_atomic(topup_id, user_id, file_unique_id):
    """Attach a receipt and move a topup to pending_review exactly once.

    Returns (ok, reason, topup, previous_uses). Duplicate image IDs are
    intentionally allowed to reach manual review, but are not inserted twice.
    """
    topup_id = int(topup_id)
    user_id = str(user_id)
    with LOCK:
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur.execute("SELECT * FROM topups WHERE id=?", (topup_id,))
            topup = cur.fetchone()
            if not topup:
                conn.rollback()
                return False, "not_found", None, 0
            if str(topup["user_id"]) != user_id:
                conn.rollback()
                return False, "owner_mismatch", topup, 0
            if topup["status"] != "awaiting_receipt":
                conn.rollback()
                return False, "invalid_status", topup, 0

            cur.execute(
                "SELECT COUNT(*) AS c FROM receipts WHERE file_unique_id=?",
                (file_unique_id,),
            )
            previous_uses = int(cur.fetchone()["c"] or 0)
            try:
                cur.execute(
                    "INSERT INTO receipts(file_unique_id, user_id, topup_id) VALUES (?, ?, ?)",
                    (file_unique_id, user_id, topup_id),
                )
            except sqlite3.IntegrityError:
                previous_uses = max(1, previous_uses)

            cur.execute(
                "UPDATE topups SET status='pending_review' "
                "WHERE id=? AND status='awaiting_receipt'",
                (topup_id,),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return False, "invalid_status", topup, previous_uses
            conn.commit()
            cur.execute("SELECT * FROM topups WHERE id=?", (topup_id,))
            return True, "submitted", cur.fetchone(), previous_uses
        except Exception:
            conn.rollback()
            raise


def is_low_stock_alerted():
    return get_setting("low_stock_alerted", "0") == "1"


def set_low_stock_alerted(flag: bool):
    set_setting("low_stock_alerted", "1" if flag else "0")


def create_ticket(user_id):
    cur.execute("INSERT INTO tickets(user_id) VALUES (?)", (str(user_id),))
    conn.commit()
    return cur.lastrowid


def record_ticket_message(admin_id, message_id, ticket_id, user_id):
    cur.execute(
        """
        INSERT OR REPLACE INTO ticket_messages(admin_id, message_id, ticket_id, user_id)
        VALUES (?, ?, ?, ?)
        """,
        (str(admin_id), int(message_id), int(ticket_id), str(user_id)),
    )
    conn.commit()


def get_ticket_message_map(admin_id, message_id):
    cur.execute(
        """
        SELECT ticket_id, user_id
        FROM ticket_messages
        WHERE admin_id=? AND message_id=?
        """,
        (str(admin_id), int(message_id)),
    )
    return cur.fetchone()


def list_open_tickets(limit=15):
    cur.execute("SELECT * FROM tickets WHERE status='open' ORDER BY id DESC LIMIT ?", (int(limit),))
    return cur.fetchall()


def close_ticket(ticket_id):
    cur.execute("UPDATE tickets SET status='closed', closed_at=datetime('now') WHERE id=?", (int(ticket_id),))
    conn.commit()


def count_open_tickets():
    cur.execute("SELECT COUNT(*) AS c FROM tickets WHERE status='open'")
    return cur.fetchone()["c"]


def list_user_tickets(user_id, limit=10):
    cur.execute(
        """
        SELECT *
        FROM tickets
        WHERE user_id=?
        ORDER BY id DESC
        LIMIT ?
        """,
        (str(user_id), int(limit)),
    )
    return cur.fetchall()


def user_ticket_counts(user_id):
    cur.execute(
        """
        SELECT
            SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open_count,
            SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) AS closed_count,
            COUNT(*) AS total_count
        FROM tickets
        WHERE user_id=?
        """,
        (str(user_id),),
    )
    row = cur.fetchone()
    return {
        "open": row["open_count"] or 0,
        "closed": row["closed_count"] or 0,
        "total": row["total_count"] or 0,
    }


def get_message(key):
    cur.execute("SELECT * FROM messages WHERE key=?", (key,))
    return cur.fetchone()


def set_message_text(key, text):
    row = get_message(key)
    if row is None:
        cur.execute("INSERT INTO messages(key, text) VALUES (?, ?)", (key, text))
    else:
        cur.execute("UPDATE messages SET text=? WHERE key=?", (text, key))
    conn.commit()


def set_message_photo(key, photo_file_id):
    row = get_message(key)
    if row is None:
        cur.execute("INSERT INTO messages(key, photo_file_id) VALUES (?, ?)", (key, photo_file_id))
    else:
        cur.execute("UPDATE messages SET photo_file_id=? WHERE key=?", (photo_file_id, key))
    conn.commit()


def clear_message(key):
    cur.execute("DELETE FROM messages WHERE key=?", (key,))
    conn.commit()


def complete_purchase(user_id, quantity, unit_price=None, note="", plan_id=None):
    user_id = str(user_id)
    quantity = int(quantity)
    plan_id = int(plan_id) if plan_id is not None else default_plan_id()
    plan = get_plan(plan_id)
    if unit_price is None:
        unit_price = int(plan["price"] if plan else 0)
    else:
        unit_price = int(unit_price)
    total = quantity * unit_price

    if not plan:
        raise PurchaseError("plan_not_found", "پلن پیدا نشد.")
    if int(plan["is_active"] or 0) != 1:
        raise PurchaseError("plan_inactive", "این پلن در حال حاضر فعال نیست.")
    if plan_provider_key(plan) != "pool":
        raise PurchaseError("wrong_delivery_type", "این پلن توسط تأمین‌کننده به‌صورت خودکار ساخته می‌شود.")
    mode = plan_purchase_mode(plan)
    if mode in {"disabled", "wholesale"}:
        raise PurchaseError("purchase_mode", "خرید مستقیم این پلن فعال نیست.")
    max_per_order = max(1, min(100, int(plan["max_per_order"] or 4)))
    if quantity < 1 or quantity > max_per_order or (mode == "direct" and quantity != 1):
        raise PurchaseError("invalid_quantity", "تعداد انتخاب‌شده برای این پلن معتبر نیست.")

    with LOCK:
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur.execute("SELECT * FROM users WHERE id=?", (user_id,))
            user = cur.fetchone()
            if not user:
                raise PurchaseError("user_not_found", "کاربر پیدا نشد.")
            if int(user["banned"] or 0):
                raise PurchaseError("banned", "حساب شما مسدود است.")

            is_test = int(user["is_test"] or 0) if "is_test" in user.keys() else 0
            balance_before = int(user["balance"] or 0)
            if balance_before < total:
                raise PurchaseError("insufficient_balance", "موجودی کیف پول کافی نیست.")

            cur.execute("SELECT COUNT(*) AS c FROM subs WHERE used=0 AND plan_id=? AND COALESCE(source_type,'pool')='pool'", (plan_id,))
            stock = int(cur.fetchone()["c"])
            if stock < quantity:
                raise PurchaseError("insufficient_stock", "موجودی سرویس کافی نیست.")

            cur.execute(
                """
                INSERT INTO purchases(user_id, quantity, amount, unit_price, status, note, plan_id, is_test, provider)
                VALUES (?, ?, ?, ?, 'completed', ?, ?, ?, 'pool')
                """,
                (user_id, quantity, total, unit_price, note or "", plan_id, is_test),
            )
            purchase_id = cur.lastrowid

            balance_after = balance_before - total
            cur.execute("UPDATE users SET balance=?, purchased=purchased+? WHERE id=?", (balance_after, quantity, user_id))
            cur.execute(
                """
                INSERT INTO ledger(user_id, action, amount, balance_before, balance_after, note, is_test)
                VALUES (?, 'purchase', ?, ?, ?, ?, ?)
                """,
                (user_id, -total, balance_before, balance_after, f"purchase_id={purchase_id}", is_test),
            )

            cur.execute("SELECT * FROM subs WHERE used=0 AND plan_id=? AND COALESCE(source_type,'pool')='pool' ORDER BY id LIMIT ?", (plan_id, quantity))
            available = cur.fetchall()
            items = []

            for sub in available:
                account_name = sub["account_name"] or generate_service_code()
                cur.execute(
                    """
                    UPDATE subs
                    SET used=1,
                        owner=?,
                        assigned_at=datetime('now'),
                        price_paid=?,
                        account_name=?,
                        status='delivered',
                        purchase_id=?
                    WHERE id=? AND used=0
                    """,
                    (user_id, unit_price, account_name, purchase_id, sub["id"]),
                )
                if cur.rowcount != 1:
                    raise PurchaseError("stock_race", "موجودی همزمان تغییر کرد. دوباره تلاش کنید.")

                cur.execute(
                    """
                    SELECT id, link, account_name, assigned_at, price_paid, status, purchase_id, plan_id
                    FROM subs
                    WHERE id=?
                    """,
                    (sub["id"],),
                )
                assigned = cur.fetchone()
                cur.execute(
                    """
                    INSERT INTO purchase_items(purchase_id, sub_id, user_id, account_name, link, price_paid, assigned_at, status, plan_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)
                    """,
                    (
                        purchase_id,
                        assigned["id"],
                        user_id,
                        assigned["account_name"],
                        assigned["link"],
                        unit_price,
                        assigned["assigned_at"],
                        plan_id,
                    ),
                )
                items.append(dict(assigned))

            if not is_test:
                _bump_daily_tx("sales", quantity)
            conn.commit()
            return {
                "purchase_id": purchase_id,
                "quantity": quantity,
                "unit_price": unit_price,
                "amount": total,
                "balance_before": balance_before,
                "balance_after": balance_after,
                "is_test": is_test,
                "items": items,
            }
        except PurchaseError:
            conn.rollback()
            raise
        except Exception as exc:
            conn.rollback()
            raise PurchaseError("unexpected", "خطای داخلی خرید رخ داد.") from exc


def get_purchase(purchase_id):
    cur.execute("SELECT * FROM purchases WHERE id=?", (int(purchase_id),))
    return cur.fetchone()


def list_user_purchases(user_id, limit=20):
    cur.execute(
        """
        SELECT *
        FROM purchases
        WHERE user_id=?
        ORDER BY id DESC
        LIMIT ?
        """,
        (str(user_id), int(limit)),
    )
    return cur.fetchall()


def list_user_ledger(user_id, limit=20):
    cur.execute(
        """
        SELECT *
        FROM ledger
        WHERE user_id=?
        ORDER BY id DESC
        LIMIT ?
        """,
        (str(user_id), int(limit)),
    )
    return cur.fetchall()


def purchase_count_by_user(user_id):
    cur.execute(
        "SELECT COUNT(*) AS c FROM purchases WHERE user_id=? AND status='completed'",
        (str(user_id),),
    )
    return cur.fetchone()["c"]


def delivered_sub_count_by_user(user_id):
    cur.execute("SELECT COUNT(*) AS c FROM subs WHERE owner=? AND used=1", (str(user_id),))
    return int(cur.fetchone()["c"] or 0)


def delivered_sub_counts_by_test_status():
    cur.execute(
        """
        SELECT
            SUM(CASE WHEN COALESCE(u.is_test,0)=0 THEN 1 ELSE 0 END) AS real_count,
            SUM(CASE WHEN COALESCE(u.is_test,0)=1 THEN 1 ELSE 0 END) AS test_count
        FROM subs s
        LEFT JOIN users u ON u.id=s.owner
        WHERE s.used=1
        """
    )
    row = cur.fetchone()
    return {
        "real": int(row["real_count"] or 0),
        "test": int(row["test_count"] or 0),
    }



def approve_topup_atomic(topup_id, admin_id=None):
    """
    تایید شارژ به صورت اتمیک:
    اگر وضعیت هنوز pending_review باشد، وضعیت approved می‌شود و همان داخل تراکنش موجودی اضافه می‌شود.
    خروجی: (ok, reason, topup_row, new_balance)
    """
    topup_id = int(topup_id)
    with LOCK:
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur.execute("SELECT * FROM topups WHERE id=?", (topup_id,))
            topup = cur.fetchone()
            if not topup:
                conn.rollback()
                return False, "not_found", None, None
            if topup["status"] != "pending_review":
                conn.rollback()
                return False, "already_reviewed", topup, None

            user_id = str(topup["user_id"])
            amount = int(topup["amount"])
            is_test = int(topup["is_test"] or 0) if "is_test" in topup.keys() else 0
            cur.execute("SELECT * FROM users WHERE id=?", (user_id,))
            user = cur.fetchone()
            if not user:
                cur.execute("INSERT INTO users(id) VALUES (?)", (user_id,))
                before = 0
            else:
                before = int(user["balance"] or 0)
            after = before + amount
            cur.execute("UPDATE users SET balance=? WHERE id=?", (after, user_id))
            cur.execute(
                """
                INSERT INTO ledger(user_id, action, amount, balance_before, balance_after, note, is_test)
                VALUES (?, 'topup_approved', ?, ?, ?, ?, ?)
                """,
                (user_id, amount, before, after, f"topup_id={topup_id};admin_id={admin_id or '-'}", is_test),
            )
            cur.execute(
                "UPDATE topups SET status='approved', reviewed_at=datetime('now') WHERE id=? AND status='pending_review'",
                (topup_id,),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return False, "already_reviewed", topup, None
            conn.commit()
            cur.execute("SELECT * FROM topups WHERE id=?", (topup_id,))
            return True, "approved", cur.fetchone(), after
        except Exception:
            conn.rollback()
            raise

# --- Text message drafts / publishing ---

def set_message_draft_text(key, text):
    row = get_message(key)
    if row is None:
        cur.execute(
            "INSERT INTO messages(key, draft_text, updated_at) VALUES (?, ?, datetime('now'))",
            (key, text),
        )
    else:
        cur.execute(
            "UPDATE messages SET draft_text=?, updated_at=datetime('now') WHERE key=?",
            (text, key),
        )
    conn.commit()


def set_message_draft_photo(key, photo_file_id):
    row = get_message(key)
    if row is None:
        cur.execute(
            "INSERT INTO messages(key, draft_photo_file_id, updated_at) VALUES (?, ?, datetime('now'))",
            (key, photo_file_id),
        )
    else:
        cur.execute(
            "UPDATE messages SET draft_photo_file_id=?, updated_at=datetime('now') WHERE key=?",
            (photo_file_id, key),
        )
    conn.commit()


def publish_message_draft(key):
    row = get_message(key)
    if row is None:
        return False
    draft_text = row["draft_text"] if "draft_text" in row.keys() else None
    draft_photo = row["draft_photo_file_id"] if "draft_photo_file_id" in row.keys() else None
    if draft_text is None and draft_photo is None:
        return False
    cur.execute(
        """
        UPDATE messages
        SET text=COALESCE(draft_text, text),
            photo_file_id=COALESCE(draft_photo_file_id, photo_file_id),
            draft_text=NULL,
            draft_photo_file_id=NULL,
            published_at=datetime('now'),
            updated_at=datetime('now')
        WHERE key=?
        """,
        (key,),
    )
    conn.commit()
    return cur.rowcount == 1


def clear_message_draft(key):
    cur.execute(
        "UPDATE messages SET draft_text=NULL, draft_photo_file_id=NULL, updated_at=datetime('now') WHERE key=?",
        (key,),
    )
    conn.commit()


# --- Custom buttons (draft first, publish after preview) ---

ALLOWED_CUSTOM_BUTTON_TYPES = {"text", "link", "submenu", "file", "support", "buy_plan", "faq", "guide"}
ALLOWED_CUSTOM_BUTTON_LOCATIONS = {"main", "buy", "my_services", "wallet", "support", "guide", "account"}
ALLOWED_CUSTOM_BUTTON_AUDIENCES = {"all", "buyers", "no_buy", "has_service", "no_service", "normal", "test", "admins"}


def _normalize_custom_button_payload(data):
    data = dict(data or {})
    title = (data.get("title") or "").strip()
    button_type = (data.get("button_type") or "text").strip().lower()
    payload = (data.get("payload") or "").strip()
    location = (data.get("location") or "main").strip().lower()
    audience = (data.get("audience") or "all").strip().lower()

    if not title:
        raise ValueError("button title is required")
    if button_type not in ALLOWED_CUSTOM_BUTTON_TYPES:
        raise ValueError("invalid button type")
    if location not in ALLOWED_CUSTOM_BUTTON_LOCATIONS:
        raise ValueError("invalid button location")
    if audience not in ALLOWED_CUSTOM_BUTTON_AUDIENCES:
        raise ValueError("invalid button audience")

    sort_order = int(data.get("sort_order") if data.get("sort_order") not in (None, "") else 100)
    is_active = 1 if str(data.get("is_active", "1")).lower() in {"1", "true", "active", "yes", "on", "فعال"} else 0
    starts_at = (data.get("starts_at") or "").strip() or None
    ends_at = (data.get("ends_at") or "").strip() or None

    return {
        "title": title,
        "button_type": button_type,
        "payload": payload,
        "location": location,
        "sort_order": sort_order,
        "is_active": is_active,
        "audience": audience,
        "starts_at": starts_at,
        "ends_at": ends_at,
    }


def create_custom_button_draft(data):
    data = _normalize_custom_button_payload(data)
    cur.execute(
        """
        INSERT INTO custom_buttons(
            status,
            draft_title, draft_button_type, draft_payload, draft_location,
            draft_sort_order, draft_is_active, draft_audience, draft_starts_at, draft_ends_at,
            updated_at
        ) VALUES ('draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        (
            data["title"], data["button_type"], data["payload"], data["location"],
            data["sort_order"], data["is_active"], data["audience"], data["starts_at"], data["ends_at"],
        ),
    )
    conn.commit()
    return cur.lastrowid


def save_custom_button_draft(button_id, data):
    data = _normalize_custom_button_payload(data)
    cur.execute(
        """
        UPDATE custom_buttons
        SET draft_title=?, draft_button_type=?, draft_payload=?, draft_location=?,
            draft_sort_order=?, draft_is_active=?, draft_audience=?, draft_starts_at=?, draft_ends_at=?,
            updated_at=datetime('now')
        WHERE id=?
        """,
        (
            data["title"], data["button_type"], data["payload"], data["location"],
            data["sort_order"], data["is_active"], data["audience"], data["starts_at"], data["ends_at"],
            int(button_id),
        ),
    )
    conn.commit()
    return cur.rowcount == 1


def publish_custom_button(button_id):
    row = get_custom_button(button_id)
    if not row:
        return False
    if not row["draft_title"]:
        return False
    cur.execute(
        """
        UPDATE custom_buttons
        SET title=draft_title,
            button_type=draft_button_type,
            payload=draft_payload,
            location=draft_location,
            sort_order=draft_sort_order,
            is_active=draft_is_active,
            audience=draft_audience,
            starts_at=draft_starts_at,
            ends_at=draft_ends_at,
            status='published',
            draft_title=NULL,
            draft_button_type=NULL,
            draft_payload=NULL,
            draft_location=NULL,
            draft_sort_order=NULL,
            draft_is_active=NULL,
            draft_audience=NULL,
            draft_starts_at=NULL,
            draft_ends_at=NULL,
            published_at=datetime('now'),
            updated_at=datetime('now')
        WHERE id=?
        """,
        (int(button_id),),
    )
    conn.commit()
    return cur.rowcount == 1


def get_custom_button(button_id):
    cur.execute("SELECT * FROM custom_buttons WHERE id=?", (int(button_id),))
    return cur.fetchone()


def delete_custom_button(button_id):
    cur.execute("DELETE FROM custom_buttons WHERE id=?", (int(button_id),))
    conn.commit()
    return cur.rowcount == 1


def list_custom_buttons(location=None, include_drafts=True, limit=50):
    sql = "SELECT * FROM custom_buttons"
    params = []
    where = []
    if location:
        where.append("COALESCE(draft_location, location)=?")
        params.append(location)
    if not include_drafts:
        where.append("status='published'")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY COALESCE(draft_sort_order, sort_order, 100), id DESC LIMIT ?"
    params.append(int(limit))
    cur.execute(sql, params)
    return cur.fetchall()


def move_custom_button(button_id, direction):
    """Stage an up/down move for a custom button inside its effective location."""
    if direction not in {"up", "down"}:
        return False
    row = get_custom_button(button_id)
    if not row:
        return False
    current = custom_button_effective_data(row)
    location = current.get("location") or "main"
    rows = list_custom_buttons(location=location, include_drafts=True, limit=500)
    effective = []
    for candidate in rows:
        data = custom_button_effective_data(candidate)
        if (data.get("location") or "main") == location:
            effective.append((candidate, data))
    effective.sort(key=lambda item: (int(item[1].get("sort_order") or 100), int(item[0]["id"])))
    index = next((i for i, item in enumerate(effective) if int(item[0]["id"]) == int(button_id)), None)
    if index is None:
        return False
    other_index = index - 1 if direction == "up" else index + 1
    if other_index < 0 or other_index >= len(effective):
        return False
    other_row, other_data = effective[other_index]
    this_order = int(current.get("sort_order") or 100)
    other_order = int(other_data.get("sort_order") or 100)
    if this_order == other_order:
        # Give the pair deterministic neighboring values before swapping.
        this_order = index * 10 + 10
        other_order = other_index * 10 + 10
    current["sort_order"], other_data["sort_order"] = other_order, this_order
    # Draft helpers validate and persist the complete effective payload for both buttons.
    save_custom_button_draft(button_id, current)
    save_custom_button_draft(other_row["id"], other_data)
    return True


def list_active_custom_buttons(location="main"):
    cur.execute(
        """
        SELECT * FROM custom_buttons
        WHERE status='published'
          AND is_active=1
          AND location=?
          AND (starts_at IS NULL OR starts_at='' OR starts_at <= datetime('now'))
          AND (ends_at IS NULL OR ends_at='' OR ends_at >= datetime('now'))
        ORDER BY sort_order, id
        """,
        (location,),
    )
    return cur.fetchall()


def get_active_custom_button_by_title(title):
    cur.execute(
        """
        SELECT * FROM custom_buttons
        WHERE status='published'
          AND is_active=1
          AND location='main'
          AND title=?
          AND (starts_at IS NULL OR starts_at='' OR starts_at <= datetime('now'))
          AND (ends_at IS NULL OR ends_at='' OR ends_at >= datetime('now'))
        ORDER BY sort_order, id
        LIMIT 1
        """,
        ((title or "").strip(),),
    )
    return cur.fetchone()


def custom_button_has_draft(row):
    return bool(row and row["draft_title"])


def stage_custom_button_toggle(button_id):
    row = get_custom_button(button_id)
    if not row:
        return False
    base = custom_button_effective_data(row)
    base["is_active"] = 0 if int(base.get("is_active") or 0) else 1
    return save_custom_button_draft(button_id, base)


def custom_button_effective_data(row, prefer_draft=True):
    if prefer_draft and row["draft_title"]:
        return {
            "title": row["draft_title"],
            "button_type": row["draft_button_type"],
            "payload": row["draft_payload"],
            "location": row["draft_location"],
            "sort_order": row["draft_sort_order"],
            "is_active": row["draft_is_active"],
            "audience": row["draft_audience"],
            "starts_at": row["draft_starts_at"],
            "ends_at": row["draft_ends_at"],
        }
    return {
        "title": row["title"],
        "button_type": row["button_type"],
        "payload": row["payload"],
        "location": row["location"],
        "sort_order": row["sort_order"],
        "is_active": row["is_active"],
        "audience": row["audience"],
        "starts_at": row["starts_at"],
        "ends_at": row["ends_at"],
    }



# --- Catalog, categories, providers and menus ---

DEFAULT_ADMIN_MENU_ITEMS = [
    ("users", "👥 کاربران", "adm_section_users", 10),
    ("catalog", "📦 کاتالوگ و فروش", "adm_section_services", 20),
    ("finance", "💰 سفارش‌ها و پرداخت‌ها", "adm_section_finance", 30),
    ("tickets", "🎫 پشتیبانی", "adm_tickets", 40),
    ("content", "🎨 محتوا و ظاهر", "adm_section_personalize", 50),
    ("reports", "📊 گزارش‌ها", "adm_section_reports", 60),
    ("backup", "💾 سیستم و بک‌آپ", "adm_backup_menu", 70),
    ("settings", "⚙️ تنظیمات", "adm_settings", 80),
]


def _ensure_default_categories():
    cur.execute("SELECT COUNT(*) AS c FROM plan_categories")
    if int(cur.fetchone()["c"] or 0) == 0:
        cur.execute(
            "INSERT INTO plan_categories(title, emoji, description, sort_order, is_active) VALUES ('سرویس‌ها', '📦', 'پلن‌های قابل خرید', 100, 1)"
        )


def _category_for_legacy_plan(title, tag=""):
    text = f"{title or ''} {tag or ''}".casefold()
    if "vip" in text or "ویژه" in text:
        wanted = ("VIP", "💎", "سرویس‌های ویژه و پریمیوم", 10)
    elif "اقتصادی" in text or "economic" in text or "economy" in text:
        wanted = ("اقتصادی", "🌱", "پلن‌های مقرون‌به‌صرفه", 20)
    else:
        wanted = ("سرویس‌ها", "📦", "پلن‌های قابل خرید", 100)
    cur.execute("SELECT id FROM plan_categories WHERE lower(title)=lower(?) ORDER BY id LIMIT 1", (wanted[0],))
    row = cur.fetchone()
    if row:
        return int(row["id"])
    cur.execute(
        "INSERT INTO plan_categories(title, emoji, description, sort_order, is_active) VALUES (?,?,?,?,1)",
        wanted,
    )
    return int(cur.lastrowid)


def _migrate_catalog_assignments():
    cur.execute("SELECT id,title,tag,category_id,delivery_type,provider_key FROM plans ORDER BY id")
    for row in cur.fetchall():
        category_id = row["category_id"]
        if not category_id:
            category_id = _category_for_legacy_plan(row["title"], row["tag"])
        provider = (row["provider_key"] or "").strip().lower()
        legacy_delivery = (row["delivery_type"] or "pool").strip().lower()
        # Adding provider_key to a legacy DB gives old rows the SQL default "pool".
        # Preserve YouPanel rows by honoring the former delivery_type during migration.
        if not provider or (provider == "pool" and legacy_delivery == "youpanel"):
            provider = "youpanel" if legacy_delivery == "youpanel" else "pool"
        cur.execute(
            "UPDATE plans SET category_id=?, provider_key=?, delivery_type=CASE WHEN ?='youpanel' THEN 'youpanel' ELSE 'pool' END WHERE id=?",
            (int(category_id), provider, provider, int(row["id"])),
        )


def _ensure_admin_menu_items():
    for key, title, callback_data, sort_order in DEFAULT_ADMIN_MENU_ITEMS:
        cur.execute(
            """
            INSERT OR IGNORE INTO admin_menu_items(key, default_title, title, callback_data, sort_order, is_active)
            VALUES (?,?,?,?,?,1)
            """,
            (key, title, title, callback_data, int(sort_order)),
        )


def list_admin_menu_items(active_only=False):
    sql = "SELECT * FROM admin_menu_items"
    if active_only:
        sql += " WHERE is_active=1"
    sql += " ORDER BY sort_order, key"
    cur.execute(sql)
    return cur.fetchall()


def get_admin_menu_item(key):
    cur.execute("SELECT * FROM admin_menu_items WHERE key=?", ((key or "").strip(),))
    return cur.fetchone()


def update_admin_menu_item(key, *, title=None, is_active=None, direction=None):
    row = get_admin_menu_item(key)
    if not row:
        return False
    with LOCK:
        try:
            conn.execute("BEGIN IMMEDIATE")
            if direction in {"up", "down"}:
                operator = "<" if direction == "up" else ">"
                order_by = "DESC" if direction == "up" else "ASC"
                cur.execute(
                    f"SELECT * FROM admin_menu_items WHERE sort_order {operator} ? ORDER BY sort_order {order_by}, key {order_by} LIMIT 1",  # nosec B608
                    (int(row["sort_order"] or 100),),
                )
                other = cur.fetchone()
                if other:
                    cur.execute("UPDATE admin_menu_items SET sort_order=? WHERE key=?", (int(other["sort_order"]), key))
                    cur.execute("UPDATE admin_menu_items SET sort_order=? WHERE key=?", (int(row["sort_order"]), other["key"]))
            if title is not None:
                cur.execute("UPDATE admin_menu_items SET title=?,updated_at=datetime('now') WHERE key=?", ((title or row["default_title"]).strip(), key))
            if is_active is not None:
                cur.execute("UPDATE admin_menu_items SET is_active=?,updated_at=datetime('now') WHERE key=?", (1 if is_active else 0, key))
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise


def reset_admin_menu_items():
    for key, title, callback_data, sort_order in DEFAULT_ADMIN_MENU_ITEMS:
        cur.execute(
            "UPDATE admin_menu_items SET title=?,callback_data=?,sort_order=?,is_active=1,updated_at=datetime('now') WHERE key=?",
            (title, callback_data, int(sort_order), key),
        )
    conn.commit()


def create_plan_category(data):
    title = (data.get("title") or "").strip()
    if not title:
        raise ValueError("عنوان دسته الزامی است")
    audience = (data.get("audience") or "all").strip().lower()
    if audience not in ALLOWED_CUSTOM_BUTTON_AUDIENCES:
        raise ValueError("گروه هدف دسته معتبر نیست")
    cur.execute(
        """
        INSERT INTO plan_categories(title,emoji,description,sort_order,is_active,audience,starts_at,ends_at)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            title,
            (data.get("emoji") or "").strip()[:16],
            (data.get("description") or "").strip(),
            int(data.get("sort_order") or 100),
            1 if int(data.get("is_active", 1) or 0) else 0,
            audience,
            data.get("starts_at") or None,
            data.get("ends_at") or None,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_plan_category(category_id):
    cur.execute("SELECT * FROM plan_categories WHERE id=?", (int(category_id),))
    return cur.fetchone()


def list_plan_categories(active_only=False, include_empty=True, limit=50):
    sql = """
        SELECT c.*, COUNT(p.id) AS plan_count,
               SUM(CASE WHEN p.is_active=1 AND COALESCE(p.purchase_mode,'quantity')!='disabled' THEN 1 ELSE 0 END) AS active_plan_count
        FROM plan_categories c
        LEFT JOIN plans p ON p.category_id=c.id
    """
    params = []
    conditions = []
    if active_only:
        conditions.append("c.is_active=1")
        conditions.append("(c.starts_at IS NULL OR c.starts_at='' OR c.starts_at<=datetime('now'))")
        conditions.append("(c.ends_at IS NULL OR c.ends_at='' OR c.ends_at>=datetime('now'))")
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " GROUP BY c.id"
    if not include_empty:
        sql += " HAVING active_plan_count > 0"
    sql += " ORDER BY c.sort_order,c.id LIMIT ?"
    params.append(int(limit))
    cur.execute(sql, params)
    return cur.fetchall()


def update_plan_category(category_id, data):
    row = get_plan_category(category_id)
    if not row:
        return False
    merged = {k: row[k] for k in row.keys()}
    merged.update(data or {})
    title = (merged.get("title") or "").strip()
    if not title:
        raise ValueError("عنوان دسته الزامی است")
    audience = (merged.get("audience") or "all").strip().lower()
    if audience not in ALLOWED_CUSTOM_BUTTON_AUDIENCES:
        raise ValueError("گروه هدف دسته معتبر نیست")
    cur.execute(
        """
        UPDATE plan_categories SET title=?,emoji=?,description=?,sort_order=?,is_active=?,audience=?,starts_at=?,ends_at=?,updated_at=datetime('now')
        WHERE id=?
        """,
        (
            title,
            (merged.get("emoji") or "").strip()[:16],
            (merged.get("description") or "").strip(),
            int(merged.get("sort_order") or 100),
            1 if int(merged.get("is_active") or 0) else 0,
            audience,
            merged.get("starts_at") or None,
            merged.get("ends_at") or None,
            int(category_id),
        ),
    )
    conn.commit()
    return cur.rowcount == 1


def toggle_plan_category(category_id):
    cur.execute("UPDATE plan_categories SET is_active=CASE WHEN is_active=1 THEN 0 ELSE 1 END,updated_at=datetime('now') WHERE id=?", (int(category_id),))
    conn.commit()
    return cur.rowcount == 1


def delete_plan_category(category_id):
    category_id = int(category_id)
    cur.execute("SELECT COUNT(*) AS c FROM plans WHERE category_id=?", (category_id,))
    if int(cur.fetchone()["c"] or 0) > 0:
        return False, "not_empty"
    cur.execute("DELETE FROM plan_categories WHERE id=?", (category_id,))
    conn.commit()
    return (cur.rowcount == 1, "deleted" if cur.rowcount == 1 else "not_found")


def move_record(table, key_field, key_value, direction, where_sql="", where_params=()):
    """Move one ordered record inside a validated scope.

    The whole scope is normalized to 10, 20, ... before persisting. This keeps
    up/down deterministic even when legacy rows share the same sort_order.
    """
    if direction not in {"up", "down"}:
        return False
    allowed = {
        "plans": {"key": "id", "where": {"", "category_id=?"}},
        "plan_categories": {"key": "id", "where": {""}},
        "system_buttons": {"key": "key", "where": {"", "location=?"}},
        "custom_buttons": {"key": "id", "where": {"", "location=?"}},
    }
    spec = allowed.get(table)
    if not spec or key_field != spec["key"] or where_sql not in spec["where"]:
        raise ValueError("unsupported order scope")

    sql = f"SELECT {key_field}, sort_order FROM {table}"  # nosec B608
    params = list(where_params)
    if where_sql:
        sql += " WHERE " + where_sql
    sql += f" ORDER BY sort_order, {key_field}"  # nosec B608
    cur.execute(sql, params)
    rows = cur.fetchall()
    index = next((i for i, row in enumerate(rows) if str(row[key_field]) == str(key_value)), None)
    if index is None:
        return False
    other_index = index - 1 if direction == "up" else index + 1
    if other_index < 0 or other_index >= len(rows):
        return False
    ordered = list(rows)
    ordered[index], ordered[other_index] = ordered[other_index], ordered[index]

    with LOCK:
        try:
            conn.execute("BEGIN IMMEDIATE")
            update_sql = f"UPDATE {table} SET sort_order=? WHERE {key_field}=?"  # nosec B608
            for position, row in enumerate(ordered, start=1):
                cur.execute(update_sql, (position * 10, row[key_field]))
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise


def plan_provider_key(plan_or_id) -> str:
    plan = get_plan(plan_or_id) if not hasattr(plan_or_id, "keys") else plan_or_id
    if not plan:
        return "pool"
    if "provider_key" in plan.keys() and (plan["provider_key"] or "").strip():
        return (plan["provider_key"] or "pool").strip().lower()
    return "youpanel" if (plan["delivery_type"] if "delivery_type" in plan.keys() else "pool") == "youpanel" else "pool"


def plan_purchase_mode(plan_or_id) -> str:
    plan = get_plan(plan_or_id) if not hasattr(plan_or_id, "keys") else plan_or_id
    value = (plan["purchase_mode"] if plan and "purchase_mode" in plan.keys() else "quantity") or "quantity"
    return value if value in {"direct", "quantity", "wholesale", "disabled"} else "quantity"


def plan_provider_options(plan_or_id):
    plan = get_plan(plan_or_id) if not hasattr(plan_or_id, "keys") else plan_or_id
    raw = (plan["provider_options_json"] if plan and "provider_options_json" in plan.keys() else "{}") or "{}"
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        value = {}
    return value if isinstance(value, dict) else {}

DEFAULT_SYSTEM_BUTTONS = [
    ("buy", "🛒 خرید سرویس", "main", 10),
    ("my_subs", "📦 سرویس‌های من", "main", 20),
    ("wallet", "💳 کیف پول", "main", 30),
    ("guide", "📚 آموزش اتصال", "main", 40),
    ("trial", "🧪 اکانت تست", "buy", 45),
    ("referral", "👥 دعوت دوستان", "main", 50),
    ("ticket", "🎫 پشتیبانی", "main", 60),
    ("admin", "⚙️ مدیریت", "main", 900),
]


def _ensure_default_plan():
    cur.execute("SELECT COUNT(*) AS c FROM plans")
    if int(cur.fetchone()["c"] or 0) == 0:
        title = get_setting("plan_title", "یک ماهه | ۱۰۰ گیگ | ۳ کاربره")
        duration = get_setting("plan_duration_label", "۳۰ روز")
        price = get_setting_int("plan_price", 100000)
        low_stock = get_setting_int("low_stock_threshold", 5)
        cur.execute(
            """
            INSERT INTO plans(title, volume_label, duration_label, price, description, sort_order,
                              is_active, is_default, max_per_order, low_stock_threshold)
            VALUES (?, '', ?, ?, 'پلن پیش‌فرض سازگار با نسخه‌های قبلی', 10, 1, 1, 4, ?)
            """,
            (title, duration, int(price), int(low_stock)),
        )
    cur.execute("SELECT COUNT(*) AS c FROM plans WHERE is_default=1")
    if int(cur.fetchone()["c"] or 0) == 0:
        cur.execute("UPDATE plans SET is_default=1 WHERE id=(SELECT id FROM plans ORDER BY id LIMIT 1)")


def default_plan_id():
    cur.execute("SELECT id FROM plans WHERE is_default=1 ORDER BY id LIMIT 1")
    row = cur.fetchone()
    if row:
        return int(row["id"])
    cur.execute("SELECT id FROM plans ORDER BY id LIMIT 1")
    row = cur.fetchone()
    return int(row["id"]) if row else 1


def duplicate_plan(plan_id):
    """Copy an existing plan into a brand-new row.

    The copy starts DISABLED on purpose: it's meant to save the admin from
    re-running the whole creation wizard for a "same but slightly
    different" plan, not to silently put a second identical plan on sale
    before it's been reviewed/edited.
    """
    plan = get_plan(plan_id)
    if not plan:
        raise ValueError("پلن یافت نشد")
    data = {
        "title": f"{plan['title']} (کپی)",
        "volume_label": plan["volume_label"],
        "duration_label": plan["duration_label"],
        "price": plan["price"],
        "description": plan["description"],
        "sort_order": plan["sort_order"],
        "is_active": 0,
        "max_per_order": plan["max_per_order"],
        "cost_price": plan["cost_price"],
        "tag": plan["tag"],
        "show_stock": plan["show_stock"],
        "low_stock_threshold": plan["low_stock_threshold"],
        "pre_purchase_text": plan["pre_purchase_text"],
        "post_purchase_text": plan["post_purchase_text"],
        "delivery_type": plan["delivery_type"],
        "panel_data_limit_bytes": plan["panel_data_limit_bytes"],
        "panel_duration_days": plan["panel_duration_days"],
        "panel_start_mode": plan["panel_start_mode"],
        "panel_reset_strategy": plan["panel_reset_strategy"],
        "panel_max_devices": plan["panel_max_devices"],
        "category_id": plan["category_id"],
        "purchase_mode": plan["purchase_mode"],
        "provider_key": plan["provider_key"],
        "provider_options_json": plan["provider_options_json"],
        "unlimited_volume": plan["unlimited_volume"] if "unlimited_volume" in plan.keys() else 0,
    }
    return create_plan(data)


def get_plan(plan_id=None):
    if plan_id is None:
        plan_id = default_plan_id()
    cur.execute("SELECT * FROM plans WHERE id=?", (int(plan_id),))
    return cur.fetchone()


def list_plans(active_only=False, limit=50, category_id=None, include_disabled=False):
    sql = "SELECT * FROM plans"
    params = []
    where = []
    if active_only:
        where.append("is_active=1")
    if category_id is not None:
        where.append("category_id=?")
        params.append(int(category_id))
    if not include_disabled:
        where.append("COALESCE(purchase_mode,'quantity')!='disabled'")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY sort_order, id LIMIT ?"
    params.append(int(limit))
    cur.execute(sql, params)
    return cur.fetchall()


def count_active_plans():
    cur.execute("SELECT COUNT(*) AS c FROM plans WHERE is_active=1")
    return int(cur.fetchone()["c"] or 0)


def create_plan(data):
    title = (data.get("title") or "").strip()
    if not title:
        raise ValueError("عنوان پلن الزامی است")
    price = int(str(data.get("price") or 0).replace(",", ""))
    if price <= 0:
        raise ValueError("قیمت پلن باید عدد مثبت باشد")
    max_per_order = max(1, min(100, int(data.get("max_per_order") or 4)))
    purchase_mode = (data.get("purchase_mode") or "quantity").strip().lower()
    if purchase_mode not in {"direct", "quantity", "wholesale", "disabled"}:
        purchase_mode = "quantity"
    provider_key = (data.get("provider_key") or data.get("delivery_type") or "pool").strip().lower()
    if provider_key in {"panel", "provider", "auto"}:
        provider_key = "youpanel"
    category_id = data.get("category_id")
    if not category_id:
        categories = list_plan_categories(active_only=False, limit=1)
        category_id = int(categories[0]["id"]) if categories else _category_for_legacy_plan(title, data.get("tag"))
    options = data.get("provider_options")
    if options is None:
        raw_options = data.get("provider_options_json") or "{}"
        try:
            options = json.loads(raw_options) if isinstance(raw_options, str) else dict(raw_options)
        except (TypeError, ValueError, json.JSONDecodeError):
            options = {}
    if not isinstance(options, dict):
        options = {}
    cur.execute(
        """
        INSERT INTO plans(title, volume_label, duration_label, price, description, sort_order,
                          is_active, max_per_order, cost_price, tag, show_stock, low_stock_threshold,
                          pre_purchase_text, post_purchase_text, delivery_type, panel_data_limit_bytes,
                          panel_duration_days, panel_start_mode, panel_reset_strategy, panel_max_devices,
                          category_id, purchase_mode, provider_key, provider_options_json, unlimited_volume)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            title, (data.get("volume_label") or "").strip(), (data.get("duration_label") or "").strip(), price,
            (data.get("description") or "").strip(), int(data.get("sort_order") or 100),
            1 if int(data.get("is_active", 1) or 0) else 0, max_per_order,
            int(str(data.get("cost_price") or 0).replace(",", "")), (data.get("tag") or "").strip(),
            1 if int(data.get("show_stock", 1) or 0) else 0,
            int(data.get("low_stock_threshold") or get_setting_int("low_stock_threshold", 5)),
            (data.get("pre_purchase_text") or "").strip(), (data.get("post_purchase_text") or "").strip(),
            "pool" if provider_key == "pool" else "youpanel",
            max(0, int(data.get("panel_data_limit_bytes") or 0)), max(0, int(data.get("panel_duration_days") or 0)),
            "active" if (data.get("panel_start_mode") or "on_hold") == "active" else "on_hold",
            (data.get("panel_reset_strategy") or "no_reset").strip() or "no_reset",
            int(data["panel_max_devices"]) if data.get("panel_max_devices") not in (None, "") else None,
            int(category_id), purchase_mode, provider_key,
            json.dumps(options, ensure_ascii=False, separators=(",", ":")),
            1 if int(data.get("unlimited_volume") or 0) else 0,
        ),
    )
    conn.commit()
    return cur.lastrowid


def update_plan(plan_id, data):
    row = get_plan(plan_id)
    if not row:
        return False
    merged = {k: row[k] for k in row.keys()}
    merged.update({k: v for k, v in (data or {}).items() if v is not None})
    title = (merged.get("title") or "").strip()
    price = int(str(merged.get("price") or 0).replace(",", ""))
    if not title or price <= 0:
        raise ValueError("عنوان و قیمت معتبر الزامی است")
    max_per_order = max(1, min(100, int(merged.get("max_per_order") or 4)))
    purchase_mode = (merged.get("purchase_mode") or "quantity").strip().lower()
    if purchase_mode not in {"direct", "quantity", "wholesale", "disabled"}:
        purchase_mode = "quantity"
    provider_key = (merged.get("provider_key") or merged.get("delivery_type") or "pool").strip().lower()
    if provider_key in {"panel", "provider", "auto"}:
        provider_key = "youpanel"
    options = merged.get("provider_options")
    if options is None:
        raw_options = merged.get("provider_options_json") or "{}"
        try:
            options = json.loads(raw_options) if isinstance(raw_options, str) else dict(raw_options)
        except (TypeError, ValueError, json.JSONDecodeError):
            options = {}
    if not isinstance(options, dict):
        options = {}
    cur.execute(
        """
        UPDATE plans
        SET title=?, volume_label=?, duration_label=?, price=?, description=?, sort_order=?,
            is_active=?, max_per_order=?, cost_price=?, tag=?, show_stock=?, low_stock_threshold=?,
            pre_purchase_text=?, post_purchase_text=?, delivery_type=?, panel_data_limit_bytes=?,
            panel_duration_days=?, panel_start_mode=?, panel_reset_strategy=?, panel_max_devices=?,
            category_id=?, purchase_mode=?, provider_key=?, provider_options_json=?, unlimited_volume=?,
            updated_at=datetime('now')
        WHERE id=?
        """,
        (
            title, (merged.get("volume_label") or "").strip(), (merged.get("duration_label") or "").strip(), price,
            (merged.get("description") or "").strip(), int(merged.get("sort_order") or 100),
            1 if int(merged.get("is_active") or 0) else 0, max_per_order,
            int(str(merged.get("cost_price") or 0).replace(",", "")), (merged.get("tag") or "").strip(),
            1 if int(merged.get("show_stock") or 0) else 0,
            int(merged.get("low_stock_threshold") or get_setting_int("low_stock_threshold", 5)),
            (merged.get("pre_purchase_text") or "").strip(), (merged.get("post_purchase_text") or "").strip(),
            "pool" if provider_key == "pool" else "youpanel",
            max(0, int(merged.get("panel_data_limit_bytes") or 0)), max(0, int(merged.get("panel_duration_days") or 0)),
            "active" if (merged.get("panel_start_mode") or "on_hold") == "active" else "on_hold",
            (merged.get("panel_reset_strategy") or "no_reset").strip() or "no_reset",
            int(merged["panel_max_devices"]) if merged.get("panel_max_devices") not in (None, "") else None,
            int(merged.get("category_id") or _category_for_legacy_plan(title, merged.get("tag"))),
            purchase_mode, provider_key, json.dumps(options, ensure_ascii=False, separators=(",", ":")),
            1 if int(merged.get("unlimited_volume") or 0) else 0,
            int(plan_id),
        ),
    )
    conn.commit()
    return cur.rowcount == 1


def toggle_plan(plan_id):
    row = get_plan(plan_id)
    if not row:
        return False
    if int(row["is_default"] or 0) == 1 and int(row["is_active"] or 0) == 1:
        # پلن پیش‌فرض را می‌شود ویرایش کرد، اما غیرفعال کامل کردنش برای سازگاری نسخه‌های قدیمی خطرناک است.
        return False
    cur.execute("UPDATE plans SET is_active=CASE WHEN is_active=1 THEN 0 ELSE 1 END, updated_at=datetime('now') WHERE id=?", (int(plan_id),))
    conn.commit()
    return cur.rowcount == 1


def plan_stock_count(plan_id):
    cur.execute("SELECT COUNT(*) AS c FROM subs WHERE used=0 AND plan_id=? AND COALESCE(source_type,'pool')='pool'", (int(plan_id),))
    return int(cur.fetchone()["c"] or 0)


def plan_sold_count(plan_id):
    cur.execute("SELECT COUNT(*) AS c FROM subs WHERE used=1 AND plan_id=?", (int(plan_id),))
    return int(cur.fetchone()["c"] or 0)


def plan_sales_by_test_status(plan_id):
    cur.execute(
        """
        SELECT
            SUM(CASE WHEN COALESCE(p.is_test,0)=0 THEN 1 ELSE 0 END) AS real_count,
            SUM(CASE WHEN COALESCE(p.is_test,0)=1 THEN 1 ELSE 0 END) AS test_count
        FROM purchase_items pi
        JOIN purchases p ON p.id=pi.purchase_id
        WHERE pi.plan_id=? AND COALESCE(pi.status,'active')='active'
        """,
        (int(plan_id),),
    )
    row = cur.fetchone()
    return {
        "real": int(row["real_count"] or 0),
        "test": int(row["test_count"] or 0),
    }


# --- System buttons ---


def _ensure_system_buttons():
    for key, default_title, location, sort_order in DEFAULT_SYSTEM_BUTTONS:
        cur.execute(
            """
            INSERT OR IGNORE INTO system_buttons(key, default_title, title, location, sort_order, is_active)
            VALUES (?, ?, ?, ?, ?, 1)
            """,
            (key, default_title, default_title, location, int(sort_order)),
        )


def list_system_buttons(location=None, active_only=False):
    sql = "SELECT * FROM system_buttons"
    params = []
    where = []
    if location:
        where.append("location=?")
        params.append(location)
    if active_only:
        where.append("is_active=1")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY sort_order, key"
    cur.execute(sql, params)
    return cur.fetchall()


def get_system_button(key):
    cur.execute("SELECT * FROM system_buttons WHERE key=?", ((key or "").strip(),))
    return cur.fetchone()


def system_button_title(key):
    row = get_system_button(key)
    if row:
        return row["title"] or row["default_title"]
    for k, title, _, _ in DEFAULT_SYSTEM_BUTTONS:
        if k == key:
            return title
    return key


def update_system_button(key, title=None, location=None, sort_order=None, is_active=None):
    row = get_system_button(key)
    if not row:
        return False
    title = row["title"] if title is None else (title or row["default_title"]).strip()
    location = row["location"] if location is None else (location or "main").strip().lower()
    sort_order = row["sort_order"] if sort_order is None else int(sort_order)
    is_active = row["is_active"] if is_active is None else (1 if int(is_active) else 0)
    cur.execute(
        "UPDATE system_buttons SET title=?, location=?, sort_order=?, is_active=?, updated_at=datetime('now') WHERE key=?",
        (title, location, int(sort_order), int(is_active), key),
    )
    conn.commit()
    return cur.rowcount == 1


def reset_system_button(key):
    row = get_system_button(key)
    if not row:
        return False
    default_location = "main"
    default_order = int(row["sort_order"] or 100)
    for item_key, _, location, sort_order in DEFAULT_SYSTEM_BUTTONS:
        if item_key == key:
            default_location = location
            default_order = int(sort_order)
            break
    cur.execute(
        "UPDATE system_buttons SET title=default_title, location=?, sort_order=?, is_active=1, updated_at=datetime('now') WHERE key=?",
        (default_location, default_order, key),
    )
    conn.commit()
    return cur.rowcount == 1


def find_system_button_by_title(title):
    cur.execute(
        "SELECT * FROM system_buttons WHERE is_active=1 AND title=? LIMIT 1",
        ((title or "").strip(),),
    )
    return cur.fetchone()


# --- Bot message cleanup ---


TRACKED_MESSAGE_KINDS = {"menu", "temp", "preview", "list", "important", "delivery", "receipt", "backup"}


def track_bot_message(chat_id, user_id, message_id, context="", kind="menu"):
    kind = (kind or "menu").strip().lower()
    if kind not in TRACKED_MESSAGE_KINDS:
        kind = "temp"
    with LOCK:
        cur.execute(
            """
            INSERT OR REPLACE INTO bot_messages(chat_id, user_id, message_id, context, kind)
            VALUES (?, ?, ?, ?, ?)
            """,
            (str(chat_id), str(user_id), int(message_id), (context or "")[:100], kind),
        )
        conn.commit()


def list_tracked_bot_messages(chat_id, user_id, limit=30, kinds=None):
    if kinds:
        placeholders = ",".join(["?"] * len(kinds))
        cur.execute(
            f"SELECT * FROM bot_messages WHERE chat_id=? AND user_id=? AND kind IN ({placeholders}) ORDER BY created_at DESC LIMIT ?",  # nosec B608
            [str(chat_id), str(user_id), *list(kinds), int(limit)],
        )
    else:
        cur.execute(
            "SELECT * FROM bot_messages WHERE chat_id=? AND user_id=? ORDER BY created_at DESC LIMIT ?",
            (str(chat_id), str(user_id), int(limit)),
        )
    return cur.fetchall()


def clear_tracked_bot_message(chat_id, message_id):
    cur.execute("DELETE FROM bot_messages WHERE chat_id=? AND message_id=?", (str(chat_id), int(message_id)))
    conn.commit()


def clear_tracked_bot_messages(chat_id, user_id, kinds=None):
    if kinds:
        placeholders = ",".join(["?"] * len(kinds))
        cur.execute(
            f"DELETE FROM bot_messages WHERE chat_id=? AND user_id=? AND kind IN ({placeholders})",  # nosec B608
            [str(chat_id), str(user_id), *list(kinds)],
        )
    else:
        cur.execute("DELETE FROM bot_messages WHERE chat_id=? AND user_id=?", (str(chat_id), str(user_id)))
    conn.commit()


def mark_topup_purchase_completed(topup_id):
    cur.execute("UPDATE topups SET purchase_completed_at=datetime('now') WHERE id=?", (int(topup_id),))
    conn.commit()
    return cur.rowcount == 1


# --- YouPanel purchases and trial claims ---


def plan_delivery_type(plan_or_id) -> str:
    """Backward-compatible binary delivery type used by legacy code paths."""
    return "pool" if plan_provider_key(plan_or_id) == "pool" else "youpanel"


def list_stale_panel_purchases(minutes=15):
    minutes = max(1, int(minutes))
    cur.execute(
        """
        SELECT * FROM purchases
        WHERE COALESCE(provider,'pool')!='pool' AND status='provisioning'
          AND created_at <= datetime('now', ?)
        ORDER BY id
        """,
        (f"-{minutes} minutes",),
    )
    return cur.fetchall()


def begin_panel_purchase(user_id, quantity, unit_price=None, note="", plan_id=None):
    """Reserve wallet funds and create a provisioning purchase atomically."""
    user_id = str(user_id)
    quantity = int(quantity)
    plan_id = int(plan_id) if plan_id is not None else default_plan_id()
    if quantity < 1:
        raise PurchaseError("invalid_quantity", "تعداد خرید معتبر نیست.")
    plan = get_plan(plan_id)
    if not plan or int(plan["is_active"] or 0) != 1:
        raise PurchaseError("invalid_plan", "پلن فعال نیست یا پیدا نشد.")
    provider_key = plan_provider_key(plan)
    if provider_key == "pool":
        raise PurchaseError("wrong_delivery_type", "این پلن از استخر لینک تحویل می‌شود.")
    mode = plan_purchase_mode(plan)
    if mode in {"disabled", "wholesale"}:
        raise PurchaseError("purchase_mode", "خرید مستقیم این پلن فعال نیست.")
    if quantity > max(1, int(plan["max_per_order"] or 1)) or (mode == "direct" and quantity != 1):
        raise PurchaseError("invalid_quantity", "تعداد خرید بیشتر از سقف این پلن است.")
    if not (int(plan["unlimited_volume"] or 0) if "unlimited_volume" in plan.keys() else False) and int(plan["panel_data_limit_bytes"] or 0) <= 0:
        raise PurchaseError("invalid_panel_plan", "حجم ساخت خودکار پلن تنظیم نشده است.")
    if int(plan["panel_duration_days"] or 0) <= 0:
        raise PurchaseError("invalid_panel_plan", "مدت ساخت خودکار پلن تنظیم نشده است.")
    unit_price = int(unit_price if unit_price is not None else plan["price"] or 0)
    total = quantity * unit_price
    with LOCK:
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur.execute("SELECT * FROM users WHERE id=?", (user_id,))
            user = cur.fetchone()
            if not user:
                raise PurchaseError("user_not_found", "کاربر پیدا نشد.")
            if int(user["banned"] or 0):
                raise PurchaseError("banned", "حساب شما مسدود است.")
            balance_before = int(user["balance"] or 0)
            if balance_before < total:
                raise PurchaseError("insufficient_balance", "موجودی کیف پول کافی نیست.")
            is_test = int(user["is_test"] or 0) if "is_test" in user.keys() else 0
            cur.execute(
                """
                INSERT INTO purchases(user_id, quantity, amount, unit_price, status, note, plan_id, is_test, provider)
                VALUES (?, ?, ?, ?, 'provisioning', ?, ?, ?, ?)
                """,
                (user_id, quantity, total, unit_price, note or "", plan_id, is_test, provider_key),
            )
            purchase_id = cur.lastrowid
            balance_after = balance_before - total
            cur.execute("UPDATE users SET balance=? WHERE id=?", (balance_after, user_id))
            cur.execute(
                """
                INSERT INTO ledger(user_id, action, amount, balance_before, balance_after, note, is_test)
                VALUES (?, 'purchase', ?, ?, ?, ?, ?)
                """,
                (user_id, -total, balance_before, balance_after,
                 f"purchase_id={purchase_id};provider={provider_key};status=provisioning", is_test),
            )
            conn.commit()
            return {
                "purchase_id": int(purchase_id), "user_id": user_id, "quantity": quantity,
                "unit_price": unit_price, "amount": total, "balance_before": balance_before,
                "balance_after": balance_after, "plan_id": plan_id, "is_test": is_test,
            }
        except PurchaseError:
            conn.rollback()
            raise
        except Exception as exc:
            conn.rollback()
            raise PurchaseError("unexpected", "خطای داخلی هنگام شروع ساخت سرویس رخ داد.") from exc


def finalize_panel_purchase(purchase_id, provisioned_items):
    """Persist provisioned panel users and complete a reserved purchase."""
    purchase_id = int(purchase_id)
    items = list(provisioned_items or [])
    with LOCK:
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur.execute("SELECT * FROM purchases WHERE id=?", (purchase_id,))
            purchase = cur.fetchone()
            if not purchase:
                raise PurchaseError("purchase_not_found", "خرید پیدا نشد.")
            if purchase["status"] == "completed":
                cur.execute("SELECT * FROM subs WHERE purchase_id=? ORDER BY id", (purchase_id,))
                existing = [dict(row) for row in cur.fetchall()]
                conn.commit()
                return {"purchase": dict(purchase), "items": existing, "already_completed": True}
            if purchase["status"] != "provisioning":
                raise PurchaseError("invalid_purchase_state", "خرید در وضعیت ساخت خودکار نیست.")
            if len(items) != int(purchase["quantity"]):
                raise PurchaseError("item_count_mismatch", "تعداد سرویس‌های ساخته‌شده با سفارش یکسان نیست.")
            saved = []
            for item in items:
                link = (item.get("subscription_url") or item.get("link") or "").strip()
                panel_username = (item.get("username") or "").strip()
                if not link or not panel_username:
                    raise PurchaseError("invalid_panel_response", "پاسخ پنل لینک یا نام کاربری معتبر ندارد.")
                account_name = (item.get("account_name") or generate_service_code()).strip()
                cur.execute(
                    """
                    INSERT INTO subs(
                        link, used, owner, assigned_at, price_paid, account_name, status,
                        purchase_id, plan_id, source_type, panel_provider, panel_username,
                        panel_status, panel_data_limit, panel_used_traffic, panel_expires_at,
                        panel_duration_seconds, is_trial, last_synced_at
                    ) VALUES (?,1,?,datetime('now'),?,?,'delivered',?,?,?,?,?,?,?,?,?,?,0,datetime('now'))
                    """,
                    (
                        link, purchase["user_id"], int(purchase["unit_price"]), account_name,
                        purchase_id, purchase["plan_id"], purchase["provider"] or "youpanel", purchase["provider"] or "youpanel",
                        panel_username, item.get("status") or "active",
                        int(item.get("data_limit") or 0), int(item.get("used_traffic") or 0),
                        item.get("expire"), item.get("on_hold_expire_duration"),
                    ),
                )
                sub_id = cur.lastrowid
                cur.execute(
                    """
                    INSERT INTO purchase_items(purchase_id, sub_id, user_id, account_name, link, price_paid, assigned_at, status, plan_id)
                    VALUES (?, ?, ?, ?, ?, ?, datetime('now'), 'active', ?)
                    """,
                    (purchase_id, sub_id, purchase["user_id"], account_name, link, int(purchase["unit_price"]), purchase["plan_id"]),
                )
                cur.execute("SELECT * FROM subs WHERE id=?", (sub_id,))
                saved.append(dict(cur.fetchone()))
            cur.execute(
                "UPDATE purchases SET status='completed', completed_at=datetime('now'), provision_error=NULL WHERE id=?",
                (purchase_id,),
            )
            cur.execute("UPDATE users SET purchased=purchased+? WHERE id=?", (int(purchase["quantity"]), purchase["user_id"]))
            if not int(purchase["is_test"] or 0):
                _bump_daily_tx("sales", int(purchase["quantity"]))
            conn.commit()
            cur.execute("SELECT * FROM purchases WHERE id=?", (purchase_id,))
            return {"purchase": dict(cur.fetchone()), "items": saved, "already_completed": False}
        except PurchaseError:
            conn.rollback()
            raise
        except Exception as exc:
            conn.rollback()
            raise PurchaseError("unexpected", "ثبت نهایی سرویس‌های پنلی ناموفق بود.") from exc


def refund_panel_purchase(purchase_id, error=""):
    """Refund one failed provisioning purchase exactly once."""
    purchase_id = int(purchase_id)
    with LOCK:
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur.execute("SELECT * FROM purchases WHERE id=?", (purchase_id,))
            purchase = cur.fetchone()
            if not purchase:
                conn.rollback()
                return False, "not_found", None
            if purchase["status"] == "failed" and purchase["refunded_at"]:
                conn.commit()
                return True, "already_refunded", dict(purchase)
            if purchase["status"] == "completed":
                conn.rollback()
                return False, "already_completed", dict(purchase)
            cur.execute("SELECT * FROM users WHERE id=?", (purchase["user_id"],))
            user = cur.fetchone()
            if not user:
                conn.rollback()
                return False, "user_not_found", dict(purchase)
            before = int(user["balance"] or 0)
            after = before + int(purchase["amount"] or 0)
            cur.execute("UPDATE users SET balance=? WHERE id=?", (after, purchase["user_id"]))
            cur.execute(
                """
                INSERT INTO ledger(user_id, action, amount, balance_before, balance_after, note, is_test)
                VALUES (?, 'purchase_refund', ?, ?, ?, ?, ?)
                """,
                (purchase["user_id"], int(purchase["amount"] or 0), before, after,
                 f"purchase_id={purchase_id};provider={purchase['provider'] or 'provider'};error={(error or '')[:300]}", int(purchase["is_test"] or 0)),
            )
            cur.execute(
                "UPDATE purchases SET status='failed', provision_error=?, refunded_at=datetime('now') WHERE id=?",
                ((error or "")[:1000], purchase_id),
            )
            conn.commit()
            return True, "refunded", dict(purchase)
        except Exception:
            conn.rollback()
            raise


# Provider-neutral names used by v6.2. Legacy *panel* functions remain as
# compatibility entry points for older modules and database terminology.
def list_stale_provider_purchases(minutes=15):
    return list_stale_panel_purchases(minutes)


def begin_provider_purchase(user_id, quantity, unit_price=None, note="", plan_id=None):
    return begin_panel_purchase(user_id, quantity, unit_price, note, plan_id)


def finalize_provider_purchase(purchase_id, provisioned_items):
    return finalize_panel_purchase(purchase_id, provisioned_items)


def refund_provider_purchase(purchase_id, error=""):
    return refund_panel_purchase(purchase_id, error)


def list_stale_trial_claims(minutes=15):
    minutes = max(1, int(minutes))
    with LOCK:
        cur.execute(
            """
            SELECT * FROM trial_claims
            WHERE status='pending' AND updated_at <= datetime('now', ?)
            ORDER BY user_id
            """,
            (f"-{minutes} minutes",),
        )
        return cur.fetchall()


def trial_claim_stats():
    cur.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,
               SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,
               SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed
        FROM trial_claims
        """
    )
    row = cur.fetchone()
    return {key: int(row[key] or 0) for key in ("total", "completed", "pending", "failed")}


def list_trial_claims(status=None, limit=30, offset=0):
    sql = """
        SELECT tc.*, u.username, u.display_name, u.joined_at,
               s.link, s.account_name, s.panel_status, s.panel_data_limit,
               s.panel_used_traffic, s.panel_expires_at, s.panel_duration_seconds,
               s.last_synced_at
        FROM trial_claims tc
        LEFT JOIN users u ON u.id=tc.user_id
        LEFT JOIN subs s ON s.id=tc.sub_id
    """
    params = []
    if status:
        sql += " WHERE tc.status=?"
        params.append(status)
    sql += " ORDER BY tc.updated_at DESC LIMIT ? OFFSET ?"
    params.extend([int(limit), int(offset)])
    cur.execute(sql, params)
    return cur.fetchall()


def get_trial_claim_by_sub(sub_id):
    cur.execute("SELECT * FROM trial_claims WHERE sub_id=?", (int(sub_id),))
    return cur.fetchone()


def begin_trial_claim(user_id, panel_username, provider_key="youpanel"):
    user_id = str(user_id)
    panel_username = (panel_username or "").strip()
    provider_key = (provider_key or "youpanel").strip().lower()
    if not panel_username:
        return False, "invalid_username", None
    with LOCK:
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur.execute("SELECT * FROM trial_claims WHERE user_id=?", (user_id,))
            row = cur.fetchone()
            if row and row["status"] in {"pending", "completed"}:
                conn.commit()
                return False, "already_claimed", dict(row)
            if row:
                cur.execute(
                    "UPDATE trial_claims SET panel_username=?, provider_key=?, status='pending', error=NULL, updated_at=datetime('now') WHERE user_id=?",
                    (panel_username, provider_key, user_id),
                )
            else:
                cur.execute("INSERT INTO trial_claims(user_id, panel_username, provider_key, status) VALUES (?, ?, ?, 'pending')", (user_id, panel_username, provider_key))
            conn.commit()
            cur.execute("SELECT * FROM trial_claims WHERE user_id=?", (user_id,))
            return True, "pending", dict(cur.fetchone())
        except sqlite3.IntegrityError:
            conn.rollback()
            return False, "username_conflict", None
        except Exception:
            conn.rollback()
            raise


def complete_trial_claim(user_id, panel_item, provider_key="youpanel"):
    user_id = str(user_id)
    link = (panel_item.get("subscription_url") or "").strip()
    panel_username = (panel_item.get("username") or "").strip()
    if not link or not panel_username:
        raise ValueError("invalid panel trial response")
    with LOCK:
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur.execute("SELECT * FROM trial_claims WHERE user_id=?", (user_id,))
            claim = cur.fetchone()
            if not claim or claim["status"] != "pending":
                raise ValueError("trial claim is not pending")
            cur.execute(
                """
                INSERT INTO subs(
                    link, used, owner, assigned_at, price_paid, account_name, status,
                    purchase_id, plan_id, source_type, panel_provider, panel_username,
                    panel_status, panel_data_limit, panel_used_traffic, panel_expires_at,
                    panel_duration_seconds, is_trial, last_synced_at
                ) VALUES (?,1,?,datetime('now'),0,?,'delivered',NULL,NULL,?,?, ?,?,?,?,?,?,1,datetime('now'))
                """,
                (
                    link, user_id, "اکانت تست", provider_key, provider_key, panel_username,
                    panel_item.get("status") or "on_hold", int(panel_item.get("data_limit") or 0),
                    int(panel_item.get("used_traffic") or 0),
                    panel_item.get("expire"), panel_item.get("on_hold_expire_duration"),
                ),
            )
            sub_id = cur.lastrowid
            cur.execute(
                "UPDATE trial_claims SET sub_id=?, status='completed', error=NULL, updated_at=datetime('now') WHERE user_id=?",
                (sub_id, user_id),
            )
            conn.commit()
            cur.execute("SELECT * FROM subs WHERE id=?", (sub_id,))
            return dict(cur.fetchone())
        except Exception:
            conn.rollback()
            raise


def fail_trial_claim(user_id, error=""):
    with LOCK:
        cur.execute(
            "UPDATE trial_claims SET status='failed', error=?, updated_at=datetime('now') WHERE user_id=?",
            ((error or "")[:1000], str(user_id)),
        )
        conn.commit()
        return cur.rowcount == 1


def get_trial_claim(user_id):
    with LOCK:
        cur.execute("SELECT * FROM trial_claims WHERE user_id=?", (str(user_id),))
        return cur.fetchone()


def reset_trial_claim(user_id) -> bool:
    """Clear a user's trial-claim record so they become eligible for a
    fresh free-trial account again (e.g. after switching providers, or as
    a goodwill gesture). Does not touch/revoke the panel service itself."""
    with LOCK:
        cur.execute("DELETE FROM trial_claims WHERE user_id=?", (str(user_id),))
        conn.commit()
        return cur.rowcount > 0


def update_panel_sub(sub_id, panel_item):
    link = panel_item.get("subscription_url")
    with LOCK:
        cur.execute(
            """
            UPDATE subs SET link=COALESCE(?,link), panel_status=COALESCE(?,panel_status),
                panel_data_limit=COALESCE(?,panel_data_limit), panel_used_traffic=COALESCE(?,panel_used_traffic),
                panel_expires_at=?, panel_duration_seconds=?, last_synced_at=datetime('now')
            WHERE id=? AND COALESCE(source_type,'pool')!='pool'
            """,
            (link, panel_item.get("status"), panel_item.get("data_limit"), panel_item.get("used_traffic"),
             panel_item.get("expire"), panel_item.get("on_hold_expire_duration"), int(sub_id)),
        )
        conn.commit()
        return cur.rowcount == 1


def update_panel_sub_usage(sub_id, used_traffic):
    with LOCK:
        cur.execute(
            "UPDATE subs SET panel_used_traffic=?, last_synced_at=datetime('now') WHERE id=? AND COALESCE(source_type,'pool')!='pool'",
            (max(0, int(used_traffic or 0)), int(sub_id)),
        )
        conn.commit()
        return cur.rowcount == 1


def mark_panel_sub_deleted(sub_id):
    sub_id = int(sub_id)
    with LOCK:
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur.execute("SELECT owner,purchase_id,is_trial,used FROM subs WHERE id=? AND COALESCE(source_type,'pool')!='pool'", (sub_id,))
            row = cur.fetchone()
            if not row:
                conn.rollback()
                return False
            cur.execute(
                "UPDATE subs SET used=0,status='deleted',panel_status='deleted',last_synced_at=datetime('now') WHERE id=? AND COALESCE(source_type,'pool')!='pool'",
                (sub_id,),
            )
            cur.execute(
                "UPDATE purchase_items SET status='deleted',reverted_at=datetime('now'),revert_reason='panel_user_deleted' WHERE sub_id=?",
                (sub_id,),
            )
            if int(row["used"] or 0) == 1 and not int(row["is_trial"] or 0) and row["owner"]:
                cur.execute(
                    "UPDATE users SET purchased=CASE WHEN purchased>0 THEN purchased-1 ELSE 0 END WHERE id=?",
                    (str(row["owner"]),),
                )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise


# --- Admin user notes / testing / logs / reports ---

def set_user_admin_note(user_id, note):
    cur.execute(
        "UPDATE users SET admin_note=? WHERE id=?",
        ((note or "").strip()[:2000], str(user_id)),
    )
    conn.commit()
    return cur.rowcount == 1


def _set_user_test_state(user_id, flag: bool):
    """Classify a user and all of their financial history consistently.

    Inventory remains consumed until an admin explicitly returns a delivered
    test service to the pool. Referral rewards that were already paid to a
    different account are intentionally not reversed automatically.
    """
    user_id = str(user_id)
    value = 1 if flag else 0
    with LOCK:
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur.execute("UPDATE users SET is_test=? WHERE id=?", (value, user_id))
            if cur.rowcount != 1:
                conn.rollback()
                return False
            cur.execute("UPDATE purchases SET is_test=? WHERE user_id=?", (value, user_id))
            cur.execute("UPDATE topups SET is_test=? WHERE user_id=?", (value, user_id))
            cur.execute("UPDATE ledger SET is_test=? WHERE user_id=?", (value, user_id))
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise


def toggle_user_test(user_id):
    user = get_user(user_id)
    if not user:
        return False
    return _set_user_test_state(user_id, not bool(int(user["is_test"] or 0)))


def set_user_test(user_id, flag: bool):
    return _set_user_test_state(user_id, bool(flag))


def log_admin_action(admin_id, action_type, target_user_id=None, details=""):
    try:
        cur.execute(
            """
            INSERT INTO admin_logs(admin_id, action_type, target_user_id, details)
            VALUES (?, ?, ?, ?)
            """,
            (str(admin_id) if admin_id is not None else None, action_type, str(target_user_id) if target_user_id is not None else None, (details or "")[:2000]),
        )
        conn.commit()
        return cur.lastrowid
    except Exception:
        return None


def list_admin_logs(limit=20):
    cur.execute("SELECT * FROM admin_logs ORDER BY id DESC LIMIT ?", (int(limit),))
    return cur.fetchall()


def today_sales_total(include_test=False):
    sql = "SELECT COALESCE(SUM(amount),0) AS s FROM purchases WHERE status='completed' AND date(created_at)=date('now')"
    if not include_test:
        sql += " AND COALESCE(is_test,0)=0"
    cur.execute(sql)
    return int(cur.fetchone()["s"] or 0)


def yesterday_sales_total(include_test=False):
    sql = "SELECT COALESCE(SUM(amount),0) AS s FROM purchases WHERE status='completed' AND date(created_at)=date('now','-1 day')"
    if not include_test:
        sql += " AND COALESCE(is_test,0)=0"
    cur.execute(sql)
    return int(cur.fetchone()["s"] or 0)


def period_sales_total(days=7, include_test=False):
    sql = "SELECT COALESCE(SUM(amount),0) AS s FROM purchases WHERE status='completed' AND created_at >= datetime('now', ?)"
    if not include_test:
        sql += " AND COALESCE(is_test,0)=0"
    cur.execute(sql, (f"-{int(days)} days",))
    return int(cur.fetchone()["s"] or 0)


def period_purchase_count(days=7, include_test=False):
    sql = "SELECT COUNT(*) AS c FROM purchases WHERE status='completed' AND created_at >= datetime('now', ?)"
    if not include_test:
        sql += " AND COALESCE(is_test,0)=0"
    cur.execute(sql, (f"-{int(days)} days",))
    return int(cur.fetchone()["c"] or 0)


def approved_topups_total_for_days(days=1, include_test=False):
    sql = "SELECT COALESCE(SUM(amount),0) AS s FROM topups WHERE status='approved' AND reviewed_at >= datetime('now', ?)"
    if not include_test:
        sql += " AND COALESCE(is_test,0)=0"
    cur.execute(sql, (f"-{int(days)} days",))
    return int(cur.fetchone()["s"] or 0)


def test_sales_total(days=30):
    cur.execute(
        "SELECT COALESCE(SUM(amount),0) AS s FROM purchases "
        "WHERE status='completed' AND COALESCE(is_test,0)=1 AND created_at >= datetime('now', ?)",
        (f"-{int(days)} days",),
    )
    return int(cur.fetchone()["s"] or 0)


def is_plan_low_stock_alerted(plan_id):
    return get_setting(f"plan_low_stock_alerted_{int(plan_id)}", "0") == "1"


def set_plan_low_stock_alerted(plan_id, flag: bool):
    set_setting(f"plan_low_stock_alerted_{int(plan_id)}", "1" if flag else "0")


def reset_plan_low_stock_alerts():
    cur.execute("DELETE FROM settings WHERE key LIKE 'plan_low_stock_alerted_%'")
    conn.commit()


def reward_referral_atomic(referred_id, reward_amount, max_total=0, max_per_day=0):
    """Pay a referral reward exactly once and exclude test accounts.

    Returns: (status, reason, referrer_id).
    """
    referred_id = str(referred_id)
    reward_amount = int(reward_amount)
    with LOCK:
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur.execute("SELECT * FROM users WHERE id=?", (referred_id,))
            referred = cur.fetchone()
            if not referred:
                conn.rollback()
                return "skipped", "user_not_found", None
            referrer_id = str(referred["ref"] or "").strip()
            if not referrer_id:
                conn.rollback()
                return "skipped", "no_referrer", None
            if int(referred["rewarded"] or 0):
                conn.rollback()
                return "skipped", "already_rewarded", referrer_id
            if referrer_id == referred_id:
                conn.rollback()
                return "blocked", "self_referral", referrer_id
            if int(referred["banned"] or 0):
                conn.rollback()
                return "blocked", "referred_banned", referrer_id
            if int(referred["is_test"] or 0):
                conn.rollback()
                return "skipped", "test_referred_user", referrer_id

            cur.execute("SELECT * FROM users WHERE id=?", (referrer_id,))
            referrer = cur.fetchone()
            if not referrer:
                conn.rollback()
                return "blocked", "referrer_not_found", referrer_id
            if int(referrer["banned"] or 0):
                conn.rollback()
                return "blocked", "referrer_banned", referrer_id
            if int(referrer["is_test"] or 0):
                conn.rollback()
                return "skipped", "test_referrer", referrer_id

            if int(max_total or 0) > 0:
                cur.execute("SELECT COUNT(*) AS c FROM users WHERE ref=? AND rewarded=1 AND COALESCE(is_test,0)=0", (referrer_id,))
                if int(cur.fetchone()["c"] or 0) >= int(max_total):
                    conn.rollback()
                    return "blocked", "referral_cap_exceeded", referrer_id
            if int(max_per_day or 0) > 0:
                cur.execute(
                    "SELECT COUNT(*) AS c FROM users WHERE ref=? AND rewarded=1 AND COALESCE(is_test,0)=0 AND date(rewarded_at)=date('now')",
                    (referrer_id,),
                )
                if int(cur.fetchone()["c"] or 0) >= int(max_per_day):
                    conn.rollback()
                    return "blocked", "daily_referral_limit", referrer_id

            before = int(referrer["balance"] or 0)
            after = before + reward_amount
            cur.execute("UPDATE users SET balance=? WHERE id=?", (after, referrer_id))
            cur.execute(
                """
                INSERT INTO ledger(user_id, action, amount, balance_before, balance_after, note, is_test)
                VALUES (?, 'referral_reward', ?, ?, ?, ?, 0)
                """,
                (referrer_id, reward_amount, before, after, f"referred_id={referred_id}"),
            )
            cur.execute(
                "UPDATE users SET rewarded=1, rewarded_at=datetime('now') WHERE id=? AND rewarded=0",
                (referred_id,),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return "skipped", "already_rewarded", referrer_id
            _bump_daily_tx("referral_rewards", reward_amount)
            conn.commit()
            return "rewarded", None, referrer_id
        except Exception:
            conn.rollback()
            raise


def reject_topup_atomic(topup_id, admin_id=None):
    topup_id = int(topup_id)
    with LOCK:
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur.execute("SELECT * FROM topups WHERE id=?", (topup_id,))
            topup = cur.fetchone()
            if not topup:
                conn.rollback()
                return False, "not_found", None
            if topup["status"] != "pending_review":
                conn.rollback()
                return False, "already_reviewed", topup
            cur.execute(
                "UPDATE topups SET status='rejected', reviewed_at=datetime('now') "
                "WHERE id=? AND status='pending_review'",
                (topup_id,),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return False, "already_reviewed", topup
            conn.commit()
            cur.execute("SELECT * FROM topups WHERE id=?", (topup_id,))
            return True, "rejected", cur.fetchone()
        except Exception:
            conn.rollback()
            raise


def database_health():
    with LOCK:
        try:
            cur.execute("PRAGMA quick_check")
            row = cur.fetchone()
            return bool(row and str(row[0]).lower() == "ok")
        except sqlite3.DatabaseError:
            return False


# --- Backup logs / schema metadata ---

def log_backup_operation(admin_id, operation_type, backup_file_name=None, file_size=0, status="ok", note=""):
    cur.execute(
        """
        INSERT INTO backup_logs(admin_id, operation_type, backup_file_name, file_size, status, note)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (str(admin_id) if admin_id is not None else None, operation_type, backup_file_name, int(file_size or 0), status, note or ""),
    )
    conn.commit()
    return cur.lastrowid


def list_backup_logs(limit=10):
    cur.execute("SELECT * FROM backup_logs ORDER BY id DESC LIMIT ?", (int(limit),))
    return cur.fetchall()
