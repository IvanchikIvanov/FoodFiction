import hashlib
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


_SEARCH_STOP_WORDS = frozenset({
    "а", "без", "бы", "был", "была", "были", "было", "в", "во", "вот", "вы", "где",
    "для", "до", "его", "ее", "её", "если", "есть", "же", "за", "и", "из", "или", "их",
    "как", "какая", "какие", "какой", "каким", "когда", "кто", "ли", "мне", "мы", "на",
    "над", "не", "но", "о", "об", "он", "она", "они", "от", "по", "под", "при", "про",
    "с", "со", "так", "то", "у", "что", "это", "я",
    "a", "an", "and", "are", "for", "from", "how", "in", "is", "of", "on", "or", "the",
    "to", "what", "when", "where", "which", "who", "with",
})


def search_terms(query):
    tokens = [token for token in re.findall(r"[^\W_]+", query.lower(), flags=re.UNICODE)
              if len(token) > 1 and token not in _SEARCH_STOP_WORDS][:20]
    terms = []
    for token in tokens:
        if re.fullmatch(r"[а-яё]+", token):
            token = token.replace("ё", "е")
            # Prefix matching tolerates common Russian inflections without changing
            # the original instruction text stored or returned to employees.
            for ending in ("аться", "иться", "ами", "ями", "ого", "его", "ому", "ему",
                           "ать", "ить", "еть", "ять", "ых", "их", "ое", "ее", "ый", "ий",
                           "ая", "яя", "ие", "ые", "ой", "ей", "ов", "ев", "ам", "ям",
                           "ом", "ем", "а", "я", "у", "ю", "ы", "и", "ь"):
                if token.endswith(ending) and len(token)-len(ending) >= 4:
                    token = token[:-len(ending)]
                    break
        if token not in terms:
            terms.append(token)
    return terms


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS scopes (
                    key TEXT PRIMARY KEY, state TEXT NOT NULL, checked REAL NOT NULL DEFAULT 0,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS documents (
                    scope TEXT NOT NULL, id TEXT NOT NULL, title TEXT NOT NULL,
                    revision TEXT NOT NULL, fingerprint TEXT NOT NULL, status TEXT NOT NULL,
                    url TEXT, pages TEXT NOT NULL, PRIMARY KEY(scope,id)
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
                    scope UNINDEXED, document_id UNINDEXED, page UNINDEXED, title, body,
                    tokenize='unicode61'
                );
                CREATE TABLE IF NOT EXISTS queue (
                    id INTEGER PRIMARY KEY CHECK(id=1), requested INTEGER NOT NULL DEFAULT 0,
                    completed INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
                    next_try REAL NOT NULL DEFAULT 0, last_error TEXT
                );
                INSERT OR IGNORE INTO queue(id) VALUES(1);
                CREATE TABLE IF NOT EXISTS web_sessions (
                    token_hash TEXT PRIMARY KEY, code_fingerprint TEXT NOT NULL,
                    expires REAL NOT NULL
                );
            """)
            if "images" not in {row[1] for row in db.execute("PRAGMA table_info(documents)")}:
                db.execute("ALTER TABLE documents ADD COLUMN images TEXT NOT NULL DEFAULT '[]'")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def bind_portal(self, portal, user_id):
        identity = digest([portal.rstrip('/').lower(), user_id])
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT value FROM metadata WHERE key='portal'").fetchone()
            if previous and previous[0] != identity:
                raise ValueError("Use a separate database for a different portal or integration user")
            db.execute("INSERT OR IGNORE INTO metadata VALUES('portal',?)", (identity,))

    def web_session_valid(self, token, code_fingerprint):
        if not token or len(token) > 128:
            return False
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self.connection() as db:
            return db.execute("SELECT 1 FROM web_sessions WHERE token_hash=? AND code_fingerprint=? AND expires>?",
                              (token_hash, code_fingerprint, time.time())).fetchone() is not None

    def configure_web_auth(self, code_fingerprint):
        with self.connection() as db:
            previous = db.execute("SELECT value FROM metadata WHERE key='web_code_fingerprint'").fetchone()
            if previous and previous[0] != code_fingerprint:
                db.execute("DELETE FROM web_sessions")
            db.execute("INSERT OR REPLACE INTO metadata VALUES('web_code_fingerprint',?)", (code_fingerprint,))

    def create_web_session(self, token, code_fingerprint, expires, previous=""):
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM web_sessions WHERE expires<=? OR code_fingerprint<>?",
                       (time.time(), code_fingerprint))
            if previous:
                db.execute("DELETE FROM web_sessions WHERE token_hash=?",
                           (hashlib.sha256(previous.encode()).hexdigest(),))
            if db.execute("SELECT count(*) FROM web_sessions").fetchone()[0] >= 5000:
                return False
            db.execute("INSERT INTO web_sessions VALUES(?,?,?)", (token_hash, code_fingerprint, expires))
            return True

    def revoke_web_session(self, token):
        if token:
            with self.connection() as db:
                db.execute("DELETE FROM web_sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))

    def configure(self, keys):
        with self.connection() as db:
            db.execute("UPDATE scopes SET state='disabled'")
            for key in keys:
                db.execute("INSERT INTO scopes(key,state) VALUES(?,'pending') ON CONFLICT(key) DO UPDATE SET state='pending'", (key,))

    def request(self):
        with self.connection() as db:
            # A counter coalesces events without losing arrivals during a running scan.
            db.execute("UPDATE queue SET requested=requested+1 WHERE id=1")

    def ticket(self):
        with self.connection() as db:
            row = db.execute("SELECT * FROM queue WHERE id=1").fetchone()
            return row["requested"] if row["requested"] > row["completed"] and row["next_try"] <= time.time() else None

    def finish(self, ticket, ok):
        with self.connection() as db:
            if ok:
                db.execute("UPDATE queue SET completed=?,attempts=0,next_try=0,last_error=NULL WHERE id=1", (ticket,))
            else:
                attempts = db.execute("SELECT attempts FROM queue WHERE id=1").fetchone()[0] + 1
                db.execute("UPDATE queue SET attempts=?,next_try=?,last_error='Synchronization failed; inspect scope status' WHERE id=1",
                           (attempts, time.time() + min(300, 2 ** min(attempts, 8))))

    def existing(self, scope, identity):
        with self.connection() as db:
            row = db.execute("SELECT * FROM documents WHERE scope=? AND id=?", (scope, identity)).fetchone()
            if not row:
                return None
            result = dict(row)
            result["pages"] = json.loads(result["pages"])
            result["images"] = json.loads(result["images"])
            return result

    @staticmethod
    def add_chunks(db, scope, doc):
        if doc["status"] != "ready":
            return
        pages = doc["pages"]
        if scope.startswith("landing:"):
            # A landing page is one article, not a separate document for each HTML
            # block. Keep preparation/context together with steps and measurements.
            pages = ["\n\n".join(text for text in pages if not text.startswith(
                "[Изображений без распознавания текста:"))]
        for page, text in enumerate(pages, 1):
            for start in range(0, len(text), 1600):
                db.execute("INSERT INTO chunks VALUES(?,?,?,?,?)",
                           (scope, doc["id"], page, doc["title"], text[start:start+2000]))

    def snapshot(self, scope, documents, *, blocked_reason=None):
        # Deletions only happen after a COMPLETE successful source traversal.
        with self.connection() as db:
            old = {r["id"]: dict(r) for r in db.execute("SELECT id,fingerprint,status FROM documents WHERE scope=?", (scope,))}
            db.execute("DELETE FROM chunks WHERE scope=?", (scope,))
            db.execute("DELETE FROM documents WHERE scope=?", (scope,))
            for original in documents:
                doc = dict(original)
                previous = old.get(doc["id"], {})
                if previous.get("fingerprint") == doc["fingerprint"] and previous.get("status") == "ready":
                    doc["status"] = "ready"
                db.execute("INSERT INTO documents(scope,id,title,revision,fingerprint,status,url,pages,images) VALUES(?,?,?,?,?,?,?,?,?)",
                           (scope, doc["id"], doc["title"], doc["revision"], doc["fingerprint"], doc["status"],
                            doc.get("url"), json.dumps(doc["pages"], ensure_ascii=False),
                            json.dumps(doc.get("images", []), ensure_ascii=False)))
                self.add_chunks(db, scope, doc)
            db.execute("UPDATE scopes SET state=?,checked=?,error=? WHERE key=?",
                       ("blocked" if blocked_reason else "ready", time.time(), blocked_reason, scope))

    def fail(self, scope):
        with self.connection() as db:
            db.execute("UPDATE scopes SET state='error',error='Source unavailable or incomplete' WHERE key=?", (scope,))

    def status(self):
        with self.connection() as db:
            return {"sources": [dict(r) for r in db.execute("SELECT * FROM scopes")],
                    "queue": dict(db.execute("SELECT * FROM queue WHERE id=1").fetchone()),
                    "documents": [dict(r) for r in db.execute("SELECT scope,id,title,revision,fingerprint,status,url FROM documents ORDER BY scope,id")]}

    def source_states(self):
        with self.connection() as db:
            return {r["key"]: dict(r) for r in db.execute("SELECT * FROM scopes")}

    def approve(self, scope, identity, fingerprint):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM documents WHERE scope=? AND id=?", (scope, identity)).fetchone()
            if not row or row["fingerprint"] != fingerprint or row["status"] != "needs_review":
                return False
            doc = dict(row)
            doc["pages"] = json.loads(doc["pages"])
            doc["status"] = "ready"
            db.execute("UPDATE documents SET status='ready' WHERE scope=? AND id=?", (scope, identity))
            db.execute("DELETE FROM chunks WHERE scope=? AND document_id=?", (scope, identity))
            self.add_chunks(db, scope, doc)
            return True

    def search(self, query, scopes, max_stale, limit=8, *, priority_query=None):
        tokens = search_terms(query)
        priority_tokens = search_terms(priority_query) if priority_query else tokens
        if not tokens or not scopes:
            return []
        def variants(token):
            # unicode61 preserves ё: query both spellings, while ranking treats them
            # alike. All tokens come from the alphanumeric tokenizer above.
            choices = {token}
            for index, character in enumerate(token):
                if character == "е":
                    choices.add(token[:index] + "ё" + token[index+1:])
            return "(" + " OR ".join('"' + item + '"*' for item in sorted(choices)) + ")"
        match = " OR ".join(variants(token) for token in tokens)
        placeholders = ",".join("?" for _ in scopes)
        with self.connection() as db:
            rows = db.execute(f"""
                SELECT c.scope,c.document_id,c.page,c.title,c.body,d.url,d.revision,d.fingerprint,
                       bm25(chunks) AS rank
                FROM chunks c JOIN scopes s ON s.key=c.scope
                JOIN documents d ON d.scope=c.scope AND d.id=c.document_id
                WHERE chunks MATCH ? AND c.scope IN ({placeholders})
                  AND s.state='ready' AND s.checked>=? AND d.status='ready'
                ORDER BY rank LIMIT ?
            """, [match, *scopes, time.time()-max_stale, max(100, limit * 10)]).fetchall()
            results = [dict(r) for r in rows]
            def coverage(item):
                words = re.findall(r"[^\W_]+", (item["title"] + " " + item["body"]).lower().replace("ё", "е"))
                count = lambda wanted: sum(any(word.startswith(token) for word in words) for token in wanted)
                return (-count(priority_tokens), -count(tokens), item["rank"])
            results.sort(key=coverage)
            return results[:limit]
