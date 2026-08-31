"""SQLite-backed aiogram FSM storage.

The bot keeps FSM data in the same database so state survives a process restart.
All operations are guarded by the shared database lock and malformed legacy JSON
is treated as an empty object instead of crashing the dispatcher.
"""

from __future__ import annotations

import json

from aiogram.dispatcher.storage import BaseStorage

import db


def _ensure_table() -> None:
    with db.LOCK:
        db.cur.execute(
            """
            CREATE TABLE IF NOT EXISTS fsm_state(
                chat_id TEXT,
                user_id TEXT,
                state TEXT,
                data TEXT DEFAULT '{}',
                bucket TEXT DEFAULT '{}',
                PRIMARY KEY (chat_id, user_id)
            )
            """
        )
        db.conn.commit()


def _decode_json(raw) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


class SQLiteStorage(BaseStorage):
    def __init__(self):
        _ensure_table()

    async def close(self):
        # The connection is owned by db.py and shared by the whole application.
        return None

    async def wait_closed(self):
        return None

    def _address(self, chat, user):
        return tuple(map(str, self.check_address(chat=chat, user=user)))

    def _row(self, chat_id, user_id):
        with db.LOCK:
            db.cur.execute(
                "SELECT * FROM fsm_state WHERE chat_id=? AND user_id=?",
                (str(chat_id), str(user_id)),
            )
            return db.cur.fetchone()

    def _upsert(self, chat_id, user_id, **fields):
        chat_id, user_id = str(chat_id), str(user_id)
        with db.LOCK:
            row = self._row(chat_id, user_id)
            if row is None:
                db.cur.execute(
                    """
                    INSERT INTO fsm_state(chat_id, user_id, state, data, bucket)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        chat_id,
                        user_id,
                        fields.get("state"),
                        fields.get("data", "{}"),
                        fields.get("bucket", "{}"),
                    ),
                )
            else:
                state_value = fields.get("state", row["state"])
                data_value = fields.get("data", row["data"])
                bucket_value = fields.get("bucket", row["bucket"])
                db.cur.execute(
                    """
                    UPDATE fsm_state
                    SET state=?, data=?, bucket=?
                    WHERE chat_id=? AND user_id=?
                    """,
                    (state_value, data_value, bucket_value, chat_id, user_id),
                )
            db.conn.commit()

    def _cleanup(self, chat_id, user_id):
        with db.LOCK:
            row = self._row(chat_id, user_id)
            if row and not row["state"] and _decode_json(row["data"]) == {} and _decode_json(row["bucket"]) == {}:
                db.cur.execute(
                    "DELETE FROM fsm_state WHERE chat_id=? AND user_id=?",
                    (str(chat_id), str(user_id)),
                )
                db.conn.commit()

    async def get_state(self, *, chat=None, user=None, default=None):
        chat_id, user_id = self._address(chat, user)
        row = self._row(chat_id, user_id)
        if row is None or row["state"] is None:
            return self.resolve_state(default)
        return row["state"]

    async def get_data(self, *, chat=None, user=None, default=None):
        chat_id, user_id = self._address(chat, user)
        row = self._row(chat_id, user_id)
        return _decode_json(row["data"]) if row else (default or {})

    async def update_data(self, *, chat=None, user=None, data=None, **kwargs):
        chat_id, user_id = self._address(chat, user)
        current = await self.get_data(chat=chat_id, user=user_id)
        current.update(data or {}, **kwargs)
        self._upsert(chat_id, user_id, data=json.dumps(current, ensure_ascii=False))

    async def set_state(self, *, chat=None, user=None, state=None):
        chat_id, user_id = self._address(chat, user)
        self._upsert(chat_id, user_id, state=self.resolve_state(state))
        self._cleanup(chat_id, user_id)

    async def set_data(self, *, chat=None, user=None, data=None):
        chat_id, user_id = self._address(chat, user)
        self._upsert(chat_id, user_id, data=json.dumps(data or {}, ensure_ascii=False))
        self._cleanup(chat_id, user_id)

    async def reset_state(self, *, chat=None, user=None, with_data=True):
        chat_id, user_id = self._address(chat, user)
        fields = {"state": None}
        if with_data:
            fields["data"] = "{}"
        self._upsert(chat_id, user_id, **fields)
        self._cleanup(chat_id, user_id)

    def has_bucket(self):
        return True

    async def get_bucket(self, *, chat=None, user=None, default=None):
        chat_id, user_id = self._address(chat, user)
        row = self._row(chat_id, user_id)
        return _decode_json(row["bucket"]) if row else (default or {})

    async def set_bucket(self, *, chat=None, user=None, bucket=None):
        chat_id, user_id = self._address(chat, user)
        self._upsert(chat_id, user_id, bucket=json.dumps(bucket or {}, ensure_ascii=False))
        self._cleanup(chat_id, user_id)

    async def update_bucket(self, *, chat=None, user=None, bucket=None, **kwargs):
        chat_id, user_id = self._address(chat, user)
        current = await self.get_bucket(chat=chat_id, user=user_id)
        current.update(bucket or {}, **kwargs)
        self._upsert(chat_id, user_id, bucket=json.dumps(current, ensure_ascii=False))
