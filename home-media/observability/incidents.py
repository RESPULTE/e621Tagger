"""Persistent, evidence-based incident transitions and bounded notification delivery."""

import json
import sqlite3
import time


class Incidents:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS incidents (
                key TEXT PRIMARY KEY, active INTEGER NOT NULL, bad INTEGER NOT NULL,
                good INTEGER NOT NULL, opened REAL, notified REAL, evidence TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY, key TEXT NOT NULL, kind TEXT NOT NULL,
                body TEXT NOT NULL, created REAL NOT NULL, due REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, at REAL NOT NULL);
        """)
        self.db.execute("DROP TABLE IF EXISTS deliveries")
        self.db.commit()

    def state(self, key, default=None):
        row = self.db.execute("SELECT payload FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_state(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, json.dumps(value)))
        self.db.commit()

    def unseen(self, event_id):
        cursor = self.db.execute("INSERT OR IGNORE INTO seen VALUES (?,?)", (event_id, time.time()))
        return cursor.rowcount == 1

    def enqueue(self, key, kind, body, now):
        # One latest unsent message per incident prevents an outage replay flood.
        self.db.execute("DELETE FROM outbox WHERE key=?", (key,))
        self.db.execute("DELETE FROM outbox WHERE created<?", (now - 7 * 86400,))
        count = self.db.execute("SELECT count(*) FROM outbox").fetchone()[0]
        if count >= 10000:
            self.db.execute("DELETE FROM outbox WHERE id IN (SELECT id FROM outbox ORDER BY id LIMIT 1)")
        self.db.execute("INSERT INTO outbox (key,kind,body,created,due) VALUES (?,?,?,?,?)",
                        (key, kind, body[:512], now, now))

    def observe(self, key, status, evidence, *, failures=3, recoveries=3, now=None,
                notification=None):
        now = time.time() if now is None else now
        row = self.db.execute("SELECT active,bad,good,opened,notified FROM incidents WHERE key=?",
                              (key,)).fetchone()
        active, bad, good, opened, notified = row or (0, 0, 0, None, None)
        transition = None
        if status == "unhealthy":
            bad, good = bad + 1, 0
            if not active and bad >= failures:
                active, opened, notified, transition = 1, now, now, "failure"
        elif status == "healthy":
            bad, good = 0, good + 1
            if active and good >= recoveries:
                active, transition = 0, "recovery"
        elif status == "cancelled":
            bad, good = 0, 0
            if active:
                active, transition = 0, "cancelled"
        elif status in {"unknown", "maintenance"}:
            # Absence cannot clear a known failure or accumulate consecutive samples.
            bad, good = 0, 0
        else:
            raise ValueError("Unknown check status")
        if transition == "failure" and notification:
            self.enqueue(key, "error", notification, now)
        elif transition in {"recovery", "cancelled"}:
            # A recovered error waiting on Telegram is stale, not a new notice.
            self.db.execute("DELETE FROM outbox WHERE key=?", (key,))
        self.db.execute("INSERT OR REPLACE INTO incidents VALUES (?,?,?,?,?,?,?)",
                        (key, active, bad, good, opened, notified, evidence[:2000]))
        self.db.commit()
        return transition

    def prune(self):
        now = time.time()
        self.db.execute("DELETE FROM seen WHERE at<?", (now - 7 * 86400,))
        self.db.execute("DELETE FROM seen WHERE id IN (SELECT id FROM seen ORDER BY at DESC LIMIT -1 OFFSET 100000)")
        self.db.execute("DELETE FROM incidents WHERE active=0 AND opened<?", (now - 30 * 86400,))
        self.db.commit()

    def close(self):
        self.db.close()
