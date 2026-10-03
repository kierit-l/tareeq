"""Alert subscriptions that survive restarts (production only).

Kept in their own SQLite file, apart from road reports, because this is the one place we hold a
phone number. Numbers are encrypted at rest (Fernet, key from TAREEQ_SUBS_KEY) and rows are looked
up by an HMAC of the number, so the table can't be searched by number without the key. Unsubscribing
deletes the row. Demo mode uses a plain in-memory dict instead.

Behaves like a dict {sender: sub}; callers must re-assign (subs[sender] = sub) after mutating a sub
so the change is written through.
"""
import base64
import hashlib
import hmac
import json
import sqlite3
import threading
from collections.abc import MutableMapping

from cryptography.fernet import Fernet


def derive_key(secret: str) -> bytes:
    return base64.urlsafe_b64encode(hashlib.sha256(("tareeq-subs:" + secret).encode()).digest())


class PersistentSubscriptions(MutableMapping):
    def __init__(self, path, secret, resolve_place):
        self.fernet = Fernet(derive_key(secret))
        self.mac_key = hashlib.sha256(("tareeq-subs-mac:" + secret).encode()).digest()
        self.resolve = resolve_place          # place name -> place dict
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.execute("""CREATE TABLE IF NOT EXISTS subscriptions (
            id TEXT PRIMARY KEY, phone_enc BLOB, from_place TEXT, to_place TEXT,
            segs TEXT, since REAL, channel TEXT)""")
        self.db.commit()
        self.mem = {}
        for _, enc, a, b, segs, since, channel in self.db.execute("SELECT * FROM subscriptions"):
            pa, pb = self.resolve(a), self.resolve(b)
            if not (pa and pb):
                continue   # place renamed/removed from the gazetteer: drop silently
            phone = self.fernet.decrypt(enc).decode()
            self.mem[phone] = {"from": pa, "to": pb, "segs": set(json.loads(segs)), "since": since, "channel": channel}

    def _id(self, phone):
        return hmac.new(self.mac_key, phone.encode(), hashlib.sha256).hexdigest()

    def __getitem__(self, phone):
        return self.mem[phone]

    def __setitem__(self, phone, sub):
        self.mem[phone] = sub
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO subscriptions VALUES (?,?,?,?,?,?,?)", (
                self._id(phone), self.fernet.encrypt(phone.encode()), sub["from"]["name"], sub["to"]["name"],
                json.dumps(sorted(sub["segs"])), sub["since"], sub.get("channel", "web")))
            self.db.commit()

    def __delitem__(self, phone):
        del self.mem[phone]
        with self.lock:
            self.db.execute("DELETE FROM subscriptions WHERE id=?", (self._id(phone),))
            self.db.commit()

    def __iter__(self):
        return iter(list(self.mem))

    def __len__(self):
        return len(self.mem)

    def clear(self):
        self.mem.clear()
        with self.lock:
            self.db.execute("DELETE FROM subscriptions")
            self.db.commit()
