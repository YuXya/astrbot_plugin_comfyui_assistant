"""Small transactional store, isolated from host conversations."""

import json
import sqlite3
import time


class Store:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS workflows (id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, event_key TEXT UNIQUE NOT NULL, created REAL NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS configuration (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);
        """)

    def configuration(self):
        row = self.db.execute("SELECT body FROM configuration WHERE id=1").fetchone()
        return json.loads(row[0]) if row else {}

    def configure(self, data):
        with self.db:
            self.db.execute("INSERT INTO configuration VALUES (1,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body", (json.dumps(data, ensure_ascii=False),))

    def workflows(self):
        return [json.loads(r[0]) for r in self.db.execute("SELECT body FROM workflows ORDER BY name")]

    def save_workflow(self, w):
        try:
            with self.db:
                self.db.execute("INSERT INTO workflows VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,body=excluded.body", (w["id"], w["name"], json.dumps(w, ensure_ascii=False)))
        except sqlite3.IntegrityError as exc:
            raise ValueError("已经有同名工作流，请使用不同名称") from exc

    def delete_workflow(self, identifier):
        with self.db:
            self.db.execute("DELETE FROM workflows WHERE id=?", (identifier,))

    def tasks(self):
        return [json.loads(r[0]) for r in self.db.execute("SELECT body FROM tasks ORDER BY created DESC")]

    def task(self, identifier):
        row = self.db.execute("SELECT body FROM tasks WHERE id=?", (identifier,)).fetchone()
        if not row:
            raise ValueError("任务不存在或记录已过期")
        return json.loads(row[0])

    def by_event(self, event_key):
        row = self.db.execute("SELECT body FROM tasks WHERE event_key=?", (event_key,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_task(self, task):
        task["updated"] = time.time()
        with self.db:
            self.db.execute("INSERT INTO tasks VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body", (task["id"], task["event_key"], task["created"], json.dumps(task, ensure_ascii=False)))

    def delete_task(self, identifier):
        with self.db:
            self.db.execute("DELETE FROM tasks WHERE id=?", (identifier,))

    def close(self):
        self.db.close()
