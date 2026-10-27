"""Rebuildable SQLite catalog for linker chunks, decisions and edges."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

import fcntl

from graph.common.hashing import short_hash
from graph.common.markdown import strip_big_tables, strip_image_media
from graph.wiki.storage import read_json, write_json_atomic

from .chunks import Chunk, make_chunks, normalize_name
from .prompts import CHUNK_META_VERSION, EDGE_VERSION_LEGACY, EDGE_VERSION_NEO

EMBED_BATCH = 64
EMBED_MAX_CHARS = 4000
FTS_TOKENIZER = "trigram"


class LinkerModeMismatch(RuntimeError):
    pass


class Catalog:
    def __init__(self, path: Path, *, mode: str, meta_version: str = CHUNK_META_VERSION, edge_version: str | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._has_sqlite_vec = False
        try:
            import sqlite_vec
            self.conn.enable_load_extension(True)
            sqlite_vec.load(self.conn)
            self.conn.enable_load_extension(False)
            self._has_sqlite_vec = True
        except Exception:
            try:
                self.conn.enable_load_extension(False)
            except Exception:
                pass
        self._schema()
        stored = self.meta("mode")
        if stored and stored != mode:
            self.close()
            raise LinkerModeMismatch(f"catalog is {stored}; run: python -m graph.linker rebuild --mode {mode}")
        self.put_meta("mode", mode)
        self.put_meta("meta_version", meta_version)
        self.put_meta("edge_version", edge_version or (EDGE_VERSION_NEO if mode == "neo" else EDGE_VERSION_LEGACY))
        self.conn.commit()

    @staticmethod
    def stored_mode(path: Path) -> str | None:
        """The mode an existing catalog file was built with, without opening it for writing."""
        if not Path(path).exists():
            return None
        with sqlite3.connect(Path(path), timeout=30) as conn:
            try:
                row = conn.execute("SELECT value FROM meta WHERE key='mode'").fetchone()
            except sqlite3.OperationalError:
                return None
        return str(row[0]) if row else None

    @classmethod
    def open(cls, path: Path, *, mode: str = "legacy", meta_version: str = CHUNK_META_VERSION, edge_version: str | None = None) -> "Catalog":
        if mode not in {"legacy", "neo"}:
            raise ValueError("mode must be legacy or neo")
        return cls(Path(path), mode=mode, meta_version=meta_version, edge_version=edge_version)

    def _schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS documents (document TEXT PRIMARY KEY, team TEXT NOT NULL, raw_rel TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pages (page_rel TEXT PRIMARY KEY, document TEXT NOT NULL REFERENCES documents(document) ON DELETE CASCADE, filename TEXT NOT NULL, title TEXT NOT NULL, original_sha256 TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS chunks (
              chunk_id TEXT PRIMARY KEY, page_rel TEXT NOT NULL REFERENCES pages(page_rel) ON DELETE CASCADE,
              document TEXT NOT NULL, team TEXT NOT NULL, ordinal INTEGER NOT NULL, heading TEXT NOT NULL,
              line_start INTEGER NOT NULL, line_end INTEGER NOT NULL, text_sha256 TEXT NOT NULL,
              body TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL, keywords_json TEXT NOT NULL,
              entity TEXT NOT NULL, claims_json TEXT NOT NULL, bridge_probe TEXT NOT NULL,
              entities_json TEXT NOT NULL, behaviours_json TEXT NOT NULL, vectors_ready INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS chunks_page ON chunks(page_rel);
            CREATE INDEX IF NOT EXISTS chunks_team ON chunks(team);
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(chunk_id UNINDEXED, title, heading, summary, keywords, claims, body, tokenize='trigram');
            CREATE TABLE IF NOT EXISTS entities (name_norm TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL, role TEXT NOT NULL, chunk_id TEXT NOT NULL REFERENCES chunks(chunk_id) ON DELETE CASCADE, team TEXT NOT NULL, PRIMARY KEY(name_norm, chunk_id, role));
            CREATE INDEX IF NOT EXISTS entities_lookup ON entities(team, name_norm, role);
            CREATE TABLE IF NOT EXISTS entity_canon (team TEXT NOT NULL, name_norm TEXT NOT NULL, canon TEXT NOT NULL, PRIMARY KEY(team,name_norm));
            CREATE TABLE IF NOT EXISTS alias_decisions (team TEXT NOT NULL, a TEXT NOT NULL, b TEXT NOT NULL, version TEXT NOT NULL, p REAL NOT NULL, PRIMARY KEY(team,a,b,version));
            CREATE TABLE IF NOT EXISTS behaviours (chunk_id TEXT NOT NULL REFERENCES chunks(chunk_id) ON DELETE CASCADE, subject_norm TEXT NOT NULL, action TEXT NOT NULL, object_norm TEXT NOT NULL, team TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS behaviours_subject ON behaviours(team, subject_norm);
            CREATE INDEX IF NOT EXISTS behaviours_object ON behaviours(team, object_norm);
            CREATE TABLE IF NOT EXISTS edges (edge_id TEXT PRIMARY KEY, chunk_a TEXT NOT NULL REFERENCES chunks(chunk_id) ON DELETE CASCADE, chunk_b TEXT NOT NULL REFERENCES chunks(chunk_id) ON DELETE CASCADE, label TEXT NOT NULL, summary TEXT NOT NULL, source TEXT NOT NULL, via_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS edges_a ON edges(chunk_a);
            CREATE INDEX IF NOT EXISTS edges_b ON edges(chunk_b);
            CREATE TABLE IF NOT EXISTS edge_decisions (hash_a TEXT NOT NULL, hash_b TEXT NOT NULL, mode TEXT NOT NULL, edge_version TEXT NOT NULL, accepted INTEGER NOT NULL, label TEXT NOT NULL, summary TEXT NOT NULL, PRIMARY KEY(hash_a, hash_b, mode, edge_version));
            CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, document TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS vectors (channel TEXT NOT NULL, chunk_id TEXT NOT NULL REFERENCES chunks(chunk_id) ON DELETE CASCADE, vector_json TEXT NOT NULL, PRIMARY KEY(channel, chunk_id));
            """
        )
        # ponytail: trigram is the only built-in tokenizer that matches inside Japanese
        # runs (unicode61 treats a whole kanji/kana clause as one token); rebuild an
        # older unicode61 index once and refill it from the chunks table.
        if self.meta("fts_tokenizer") != FTS_TOKENIZER:
            self.conn.execute("DROP TABLE IF EXISTS chunks_fts")
            self.conn.execute(f"CREATE VIRTUAL TABLE chunks_fts USING fts5(chunk_id UNINDEXED, title, heading, summary, keywords, claims, body, tokenize='{FTS_TOKENIZER}')")
            for row in self.conn.execute("SELECT c.*, p.title AS page_title FROM chunks c JOIN pages p ON p.page_rel=c.page_rel").fetchall():
                title = f"{row['page_title']} › {row['heading']}".rstrip(" ›")
                self.conn.execute("INSERT INTO chunks_fts(chunk_id,title,heading,summary,keywords,claims,body) VALUES(?,?,?,?,?,?,?)", (row["chunk_id"], title, row["heading"], row["summary"], " ".join(json.loads(row["keywords_json"] or "[]")), " ".join(json.loads(row["claims_json"] or "[]")), str(row["body"])[:8000]))
            self.put_meta("fts_tokenizer", FTS_TOKENIZER)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else None

    def put_meta(self, key: str, value: Any) -> None:
        self.conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    @contextmanager
    def transaction(self):
        try:
            yield self.conn
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    @contextmanager
    def lock(self, project: Any):
        path = Path(project.metadata) / "wiki-linker.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield self
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _document_for(path: Path, wiki: Path) -> str:
        return path.parent.parent.relative_to(wiki).as_posix()

    def sync_from_planning(self, project: Any, *, skip_document: str | None = None) -> None:
        plans = sorted(Path(project.wiki).rglob("_planning/chunks.json"))
        pending_edges: list[dict[str, Any]] = []
        project_team = Path(project.root).name
        for path in plans:
            data = read_json(path, default={})
            document = str(data.get("document") or self._document_for(path, Path(project.wiki)))
            if document == skip_document:
                continue
            pages_dir = path.parent / "pages"
            if not pages_dir.exists():
                continue
            chunks: list[Chunk] = []
            for page in sorted(pages_dir.glob("*.md")):
                page_data = next((p for p in data.get("pages", []) if p.get("filename") == page.name), {})
                by_hash = {item.get("text_sha256"): item for item in page_data.get("chunks", [])}
                for item in make_chunks(document, project_team, page.name, page.read_text(encoding="utf-8"), id_seed=str(data.get("id_seed") or document)):
                    cached = by_hash.get(item.text_sha256)
                    if cached:
                        from .chunks import _meta_from_json
                        item.meta = _meta_from_json(cached)
                    chunks.append(item)
            self.reconcile(
                document,
                chunks,
                team=project_team,
                raw_rel=str(data.get("raw_rel") or ""),
                page_hashes={p.get("filename", ""): p.get("original_sha256", "") for p in data.get("pages", [])},
            )
            links = read_json(path.parent / "links.json", default={})
            pending_edges.extend(links.get("edges", []))
        for edge in pending_edges:
            self.insert_edge(edge, commit=False)
        self.conn.commit()

    def upsert_document(self, document: str, team: str, raw_rel: str, chunks: list[Chunk], page_hashes: dict[str, str] | None = None) -> None:
        page_hashes = page_hashes or {}
        self.conn.execute("INSERT INTO documents(document,team,raw_rel,updated_at) VALUES(?,?,?,datetime('now')) ON CONFLICT(document) DO UPDATE SET team=excluded.team,raw_rel=excluded.raw_rel,updated_at=excluded.updated_at", (document, team, raw_rel))
        page_names = {c.filename for c in chunks}
        old_pages = self.conn.execute("SELECT page_rel FROM pages WHERE document=?", (document,)).fetchall()
        for row in old_pages:
            if row[0].split("/")[-1] not in page_names:
                self.conn.execute("DELETE FROM pages WHERE page_rel=?", (row[0],))
        for filename in page_names:
            page_rel = f"{document}/{filename}"
            title = next(c.title for c in chunks if c.filename == filename)
            self.conn.execute("INSERT INTO pages(page_rel,document,filename,title,original_sha256) VALUES(?,?,?,?,?) ON CONFLICT(page_rel) DO UPDATE SET title=excluded.title,original_sha256=excluded.original_sha256", (page_rel, document, filename, title, page_hashes.get(filename, "")))
        self.upsert_chunks(chunks, commit=False)

    def upsert_chunks(self, chunks: Iterable[Chunk], *, commit: bool = True) -> None:
        for item in chunks:
            old = self.conn.execute("SELECT text_sha256 FROM chunks WHERE chunk_id=?", (item.chunk_id,)).fetchone()
            changed = old is None or old[0] != item.text_sha256
            if changed:
                self.conn.execute("DELETE FROM chunks_fts WHERE chunk_id=?", (item.chunk_id,))
                self.conn.execute("DELETE FROM vectors WHERE chunk_id=?", (item.chunk_id,))
                if self._has_sqlite_vec:
                    for channel in ("body", "summary", "bridge"):
                        if self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (f"vec_{channel}",)).fetchone():
                            self.conn.execute(f"DELETE FROM vec_{channel} WHERE chunk_id=?", (item.chunk_id,))
            self.conn.execute(
                """INSERT INTO chunks(chunk_id,page_rel,document,team,ordinal,heading,line_start,line_end,text_sha256,body,summary,keywords_json,entity,claims_json,bridge_probe,entities_json,behaviours_json,vectors_ready)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(chunk_id) DO UPDATE SET page_rel=excluded.page_rel,document=excluded.document,team=excluded.team,ordinal=excluded.ordinal,heading=excluded.heading,line_start=excluded.line_start,line_end=excluded.line_end,text_sha256=excluded.text_sha256,body=excluded.body,summary=excluded.summary,keywords_json=excluded.keywords_json,entity=excluded.entity,claims_json=excluded.claims_json,bridge_probe=excluded.bridge_probe,entities_json=excluded.entities_json,behaviours_json=excluded.behaviours_json,vectors_ready=CASE WHEN chunks.text_sha256=excluded.text_sha256 THEN chunks.vectors_ready ELSE 0 END""",
                (item.chunk_id, item.page_rel, item.document, item.team, item.ordinal, item.heading, item.line_start, item.line_end, item.text_sha256, item.model_text, item.summary, json.dumps(item.keywords, ensure_ascii=False), item.entity, json.dumps(item.claims, ensure_ascii=False), item.bridge_probe, json.dumps([x.model_dump(mode="json") for x in item.entities], ensure_ascii=False), json.dumps([x.model_dump(mode="json") for x in item.behaviours], ensure_ascii=False), 0),
            )
            self.conn.execute("DELETE FROM entities WHERE chunk_id=?", (item.chunk_id,))
            self.conn.execute("DELETE FROM behaviours WHERE chunk_id=?", (item.chunk_id,))
            self.conn.execute("DELETE FROM chunks_fts WHERE chunk_id=?", (item.chunk_id,))
            for entity in item.entities:
                self.conn.execute("INSERT OR IGNORE INTO entities(name_norm,name,kind,role,chunk_id,team) VALUES(?,?,?,?,?,?)", (normalize_name(entity.name), entity.name, entity.kind, entity.role, item.chunk_id, item.team))
            for behaviour in item.behaviours:
                self.conn.execute("INSERT INTO behaviours(chunk_id,subject_norm,action,object_norm,team) VALUES(?,?,?,?,?)", (item.chunk_id, normalize_name(behaviour.subject), behaviour.action, normalize_name(behaviour.object), item.team))
            self._fts(item)
        if commit:
            self.conn.commit()

    def _fts(self, item: Chunk) -> None:
        title = f"{item.title} › {item.heading}".rstrip(" ›")
        self.conn.execute("INSERT INTO chunks_fts(chunk_id,title,heading,summary,keywords,claims,body) VALUES(?,?,?,?,?,?,?)", (item.chunk_id, title, item.heading, item.summary, " ".join(item.keywords), " ".join(item.claims), item.model_text[:8000]))

    def reconcile(self, document: str, chunks: list[Chunk], *, team: str, raw_rel: str = "", page_hashes: dict[str, str] | None = None) -> dict[str, Any]:
        old = {row["chunk_id"]: row for row in self.conn.execute("SELECT * FROM chunks WHERE document=?", (document,))}
        new = {item.chunk_id: item for item in chunks}
        unchanged = [cid for cid in new if cid in old and old[cid]["text_sha256"] == new[cid].text_sha256]
        changed = [cid for cid in new if cid in old and cid not in unchanged]
        added = [cid for cid in new if cid not in old]
        removed = [cid for cid in old if cid not in new]
        invalidated = set(changed) | set(removed)
        peer_ids: set[str] = set()
        for cid in invalidated:
            peer_ids.update(self.edge_peers(cid))
        edges_removed = 0
        for cid in invalidated:
            edges_removed += self.conn.execute("DELETE FROM edges WHERE chunk_a=? OR chunk_b=?", (cid, cid)).rowcount
        for cid in removed:
            self.conn.execute("DELETE FROM chunks WHERE chunk_id=?", (cid,))
        self.upsert_document(document, team, raw_rel, chunks, page_hashes)
        self.conn.commit()
        return {"unchanged": unchanged, "changed": changed, "new": added, "removed": removed, "edges_removed": edges_removed, "peers_before": peer_ids}

    def chunk(self, chunk_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM chunks WHERE chunk_id=?", (chunk_id,)).fetchone()

    def chunks_for_document(self, document: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM chunks WHERE document=? ORDER BY page_rel,ordinal", (document,)).fetchall()

    def chunks_for_team(self, team: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM chunks WHERE team=? ORDER BY chunk_id", (team,)).fetchall()

    def page_of(self, chunk_id: str) -> str | None:
        row = self.conn.execute("SELECT page_rel FROM chunks WHERE chunk_id=?", (chunk_id,)).fetchone()
        return str(row[0]) if row else None

    def document_of(self, chunk_id: str) -> str | None:
        row = self.conn.execute("SELECT document FROM chunks WHERE chunk_id=?", (chunk_id,)).fetchone()
        return str(row[0]) if row else None

    def fts_search(self, text: str, team: str, limit: int, exclude_page: str = "", exclude_document: str = "") -> list[str]:
        tokens = re_tokens(text)[:40]
        if not tokens:
            return []
        query = " OR ".join(f'"{token.replace(chr(34), "")}"' for token in tokens)
        rows = self.conn.execute("SELECT f.chunk_id FROM chunks_fts AS f JOIN chunks c ON c.chunk_id=f.chunk_id WHERE c.team=? AND c.page_rel<>? AND c.document<>? AND chunks_fts MATCH ? ORDER BY bm25(chunks_fts) LIMIT ?", (team, exclude_page, exclude_document, query, limit)).fetchall()
        return [str(row[0]) for row in rows]

    def _ensure_vec_tables(self, dim: int) -> None:
        if self.meta("embed_dim") and int(self.meta("embed_dim") or 0) != dim:
            self.reset_vectors()
        if self._has_sqlite_vec:
            for channel in ("body", "summary", "bridge"):
                self.conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_{channel} USING vec0(chunk_id TEXT PRIMARY KEY, embedding float[{dim}])")
        self.put_meta("embed_dim", dim)

    def reset_vectors(self) -> None:
        for channel in ("body", "summary", "bridge"):
            self.conn.execute(f"DROP TABLE IF EXISTS vec_{channel}")
        self.conn.execute("DELETE FROM vectors")
        self.conn.execute("UPDATE chunks SET vectors_ready=0")
        self.conn.execute("DELETE FROM meta WHERE key IN ('embed_dim','embed_model')")

    def set_vector(self, channel: str, chunk_id: str, vector: list[float]) -> None:
        self._ensure_vec_tables(len(vector))
        self.conn.execute("INSERT INTO vectors(channel,chunk_id,vector_json) VALUES(?,?,?) ON CONFLICT(channel,chunk_id) DO UPDATE SET vector_json=excluded.vector_json", (channel, chunk_id, json.dumps(vector)))
        if self._has_sqlite_vec:
            import sqlite_vec
            blob = sqlite_vec.serialize_float32(vector)
            self.conn.execute(f"DELETE FROM vec_{channel} WHERE chunk_id=?", (chunk_id,))
            self.conn.execute(f"INSERT INTO vec_{channel}(chunk_id,embedding) VALUES(?,?)", (chunk_id, blob))

    def vector(self, channel: str, chunk_id: str) -> list[float] | None:
        row = self.conn.execute("SELECT vector_json FROM vectors WHERE channel=? AND chunk_id=?", (channel, chunk_id)).fetchone()
        return json.loads(row[0]) if row else None

    def vec_search(self, channel: str, vector: list[float], team: str, k: int, exclude_page: str = "") -> list[str]:
        if self._has_sqlite_vec and self.meta("embed_dim"):
            import sqlite_vec
            rows = self.conn.execute(f"WITH matches AS (SELECT chunk_id, distance FROM vec_{channel} WHERE embedding MATCH ? ORDER BY distance LIMIT ?) SELECT m.chunk_id FROM matches m JOIN chunks c ON c.chunk_id=m.chunk_id WHERE c.team=? AND c.page_rel<>? ORDER BY m.distance LIMIT ?", (sqlite_vec.serialize_float32(vector), k * 4, team, exclude_page, k)).fetchall()
            return [str(row[0]) for row in rows]
        scored: list[tuple[float, str]] = []
        for row in self.conn.execute("SELECT c.chunk_id,v.vector_json FROM vectors v JOIN chunks c ON c.chunk_id=v.chunk_id WHERE v.channel=? AND c.team=? AND c.page_rel<>?", (channel, team, exclude_page)):
            other = json.loads(row[1])
            scored.append((sum((a - b) ** 2 for a, b in zip(vector, other)), str(row[0])))
        return [cid for _, cid in sorted(scored)[:k]]

    def embed_pending(self, embedder: Any, *, team: str | None = None, chunk_ids: set[str] | None = None) -> int:
        rows = self.conn.execute("SELECT * FROM chunks WHERE vectors_ready=0" + (" AND team=?" if team else ""), (team,) if team else ()).fetchall()
        if chunk_ids is not None:
            rows = [row for row in rows if str(row["chunk_id"]) in chunk_ids]
        if not rows or embedder is None:
            return 0
        model_name = str(getattr(embedder, "model_name", ""))
        if self.meta("embed_model") and self.meta("embed_model") != model_name:
            self.reset_vectors()
            rows = self.conn.execute("SELECT * FROM chunks WHERE vectors_ready=0" + (" AND team=?" if team else ""), (team,) if team else ()).fetchall()
        self.put_meta("embed_model", model_name)
        done = 0
        for offset in range(0, len(rows), EMBED_BATCH):
            batch = rows[offset : offset + EMBED_BATCH]
            try:
                for channel, field in (("body", "body"), ("summary", "summary"), ("bridge", "bridge_probe")):
                    # ponytail: hard character cap instead of token counting; ruri-v3 stops at
                    # 8192 tokens and 4000 Japanese characters stay well below that.
                    texts = [str(row[field])[:EMBED_MAX_CHARS] for row in batch]
                    if channel == "bridge" and not any(texts):
                        continue
                    vectors = embedder.embed_documents(texts)
                    if len(vectors) != len(batch):
                        raise RuntimeError(f"embedding count mismatch: expected {len(batch)}, got {len(vectors)}")
                    for row, vector in zip(batch, vectors):
                        if vector:
                            self.set_vector(channel, str(row["chunk_id"]), [float(v) for v in vector])
                for row in batch:
                    self.conn.execute("UPDATE chunks SET vectors_ready=1 WHERE chunk_id=?", (row["chunk_id"],))
                self.conn.commit()
                done += len(batch)
            except Exception:
                # Leave this batch pending (vectors_ready=0); the next run retries it and
                # the FTS channels still work meanwhile.
                self.conn.rollback()
        return done * 3

    def chunks_with_entity(self, entity: str, team: str, exclude_page: str = "") -> list[str]:
        rows = self.conn.execute("SELECT c.chunk_id FROM chunks c WHERE c.team=? AND lower(c.entity)=lower(?) AND c.page_rel<>? ORDER BY c.chunk_id", (team, entity, exclude_page)).fetchall()
        return [str(row[0]) for row in rows]

    def entity_chunks(self, team: str, name_norm: str, *, role: str | None = None, exclude_page: str = "") -> list[str]:
        sql = "SELECT e.chunk_id FROM entities e JOIN chunks c ON c.chunk_id=e.chunk_id LEFT JOIN entity_canon ec ON ec.team=e.team AND ec.name_norm=e.name_norm WHERE e.team=? AND COALESCE(ec.canon,e.name_norm)=? AND c.page_rel<>?"
        params: list[Any] = [team, self.canonical(team, name_norm), exclude_page]
        if role:
            sql += " AND e.role=?"; params.append(role)
        sql += " ORDER BY e.chunk_id"
        return [str(row[0]) for row in self.conn.execute(sql, params)]

    def canonical(self, team: str, name_norm: str) -> str:
        row = self.conn.execute("SELECT canon FROM entity_canon WHERE team=? AND name_norm=?", (team, name_norm)).fetchone()
        return str(row[0]) if row else name_norm

    def set_canonical(self, team: str, groups: list[list[str]]) -> None:
        self.conn.execute("DELETE FROM entity_canon WHERE team=?", (team,))
        self.conn.executemany("INSERT INTO entity_canon(team,name_norm,canon) VALUES(?,?,?)",
                              [(team, name, min(group)) for group in groups for name in group])

    def entity_names(self, team: str) -> list[tuple[str, str]]:
        return [(str(row[0]), str(row[1])) for row in self.conn.execute(
            "SELECT name_norm,MIN(name) FROM entities WHERE team=? GROUP BY name_norm ORDER BY name_norm", (team,))]

    def alias_decision_get(self, team: str, a: str, b: str, version: str) -> float | None:
        row = self.conn.execute("SELECT p FROM alias_decisions WHERE team=? AND a=? AND b=? AND version=?", (team, a, b, version)).fetchone()
        return float(row[0]) if row else None

    def alias_decision_put(self, team: str, a: str, b: str, version: str, p: float) -> None:
        a, b = sorted((a, b))
        self.conn.execute("INSERT INTO alias_decisions(team,a,b,version,p) VALUES(?,?,?,?,?) ON CONFLICT(team,a,b,version) DO UPDATE SET p=excluded.p", (team, a, b, version, p))

    def behaviour_chunks(self, team: str, names: set[str], *, exclude_page: str = "") -> list[tuple[str, int]]:
        if not names:
            return []
        marks = ",".join("?" for _ in names)
        rows = self.conn.execute(f"SELECT b.chunk_id, COUNT(*) FROM behaviours b JOIN chunks c ON c.chunk_id=b.chunk_id WHERE b.team=? AND (b.subject_norm IN ({marks}) OR b.object_norm IN ({marks})) AND c.page_rel<>? GROUP BY b.chunk_id ORDER BY COUNT(*) DESC,b.chunk_id", [team, *names, *names, exclude_page]).fetchall()
        return [(str(row[0]), int(row[1])) for row in rows]

    def behaviour_links(self, team: str, name_norm: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM behaviours WHERE team=? AND (subject_norm=? OR object_norm=?) ORDER BY chunk_id", (team, name_norm, name_norm)).fetchall()

    def edge_peers(self, chunk_id: str) -> set[str]:
        rows = self.conn.execute("SELECT chunk_a,chunk_b FROM edges WHERE chunk_a=? OR chunk_b=?", (chunk_id, chunk_id)).fetchall()
        return {str(row[1] if row[0] == chunk_id else row[0]) for row in rows}

    @staticmethod
    def edge_id_for(edge: dict[str, Any]) -> str:
        """The stored id of an edge dict, or the id insert_edge would give it."""

        if edge.get("edge_id"):
            return str(edge["edge_id"])
        a = str(edge.get("chunk_a") or edge.get("a", {}).get("chunk_id", ""))
        b = str(edge.get("chunk_b") or edge.get("b", {}).get("chunk_id", ""))
        source = str(edge.get("source") or "legacy_rrf")
        raw_via = edge.get("via") or edge.get("via_json") or []
        via = json.loads(raw_via) if isinstance(raw_via, str) else list(raw_via)
        identity = sorted((a, b))
        if source in {"use", "define"} and via:
            identity.append(normalize_name(str(via[0])))
        return "ledge-" + short_hash("\0".join(identity), 20)

    def insert_edge(self, edge: dict[str, Any], *, commit: bool = True) -> bool:
        a = str(edge.get("chunk_a") or edge.get("a", {}).get("chunk_id", "")); b = str(edge.get("chunk_b") or edge.get("b", {}).get("chunk_id", ""))
        if not a or not b or a == b:
            return False
        # Both endpoints must exist (links.json may name a document that is not
        # catalogued yet) and, when the edge carries content hashes, still have
        # the text the edge was judged on.
        row_a = self.conn.execute("SELECT text_sha256 FROM chunks WHERE chunk_id=?", (a,)).fetchone()
        row_b = self.conn.execute("SELECT text_sha256 FROM chunks WHERE chunk_id=?", (b,)).fetchone()
        if row_a is None or row_b is None:
            return False
        hash_a = str(edge.get("hash_a") or edge.get("a", {}).get("text_sha256", "") or "")
        hash_b = str(edge.get("hash_b") or edge.get("b", {}).get("text_sha256", "") or "")
        if (hash_a or hash_b) and (hash_a, hash_b) != (row_a[0], row_b[0]):
            return False
        source = str(edge.get("source") or "legacy_rrf")
        raw_via = edge.get("via") or edge.get("via_json") or []
        via = json.loads(raw_via) if isinstance(raw_via, str) else list(raw_via)
        edge_id = self.edge_id_for(edge)
        cursor = self.conn.execute("INSERT OR IGNORE INTO edges(edge_id,chunk_a,chunk_b,label,summary,source,via_json,created_at) VALUES(?,?,?,?,?,?,?,?)", (edge_id, a, b, str(edge.get("label") or "related"), str(edge.get("summary") or ""), source, json.dumps(via, ensure_ascii=False), str(edge.get("created_at") or "")))
        if commit:
            self.conn.commit()
        return cursor.rowcount > 0

    def restore_edges(self, links_path: Path, *, commit: bool = True) -> int:
        """Re-insert the edges recorded in one ``links.json`` whose endpoints still match."""
        restored = 0
        for edge in read_json(links_path, default={}).get("edges", []):
            restored += int(self.insert_edge(edge, commit=False))
        if commit:
            self.conn.commit()
        return restored

    def delete_edges_for(self, chunk_ids: Iterable[str], *, commit: bool = True) -> int:
        ids = list(chunk_ids)
        if not ids:
            return 0
        marks = ",".join("?" for _ in ids)
        cur = self.conn.execute(f"DELETE FROM edges WHERE chunk_a IN ({marks}) OR chunk_b IN ({marks})", [*ids, *ids])
        if commit:
            self.conn.commit()
        return cur.rowcount

    def edges_for_page(self, page_rel: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT e.*, a.page_rel AS page_a_rel,pa.filename AS page_a_filename,pa.title AS page_a_title,a.heading AS page_a_heading,a.summary AS summary_a,b.page_rel AS page_b_rel,pb.filename AS page_b_filename,pb.title AS page_b_title,b.heading AS page_b_heading,b.summary AS summary_b FROM edges e JOIN chunks a ON a.chunk_id=e.chunk_a JOIN pages pa ON pa.page_rel=a.page_rel JOIN chunks b ON b.chunk_id=e.chunk_b JOIN pages pb ON pb.page_rel=b.page_rel WHERE a.page_rel=? OR b.page_rel=? ORDER BY e.edge_id", (page_rel, page_rel)).fetchall()

    def all_edges_for_documents(self, documents: set[str]) -> list[sqlite3.Row]:
        if not documents:
            return []
        marks = ",".join("?" for _ in documents)
        return self.conn.execute(f"SELECT e.*,a.document AS a_document,b.document AS b_document FROM edges e JOIN chunks a ON a.chunk_id=e.chunk_a JOIN chunks b ON b.chunk_id=e.chunk_b WHERE a.document IN ({marks}) OR b.document IN ({marks})", [*documents, *documents]).fetchall()

    def edge_decision_get(self, hash_a: str, hash_b: str, mode: str, version: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM edge_decisions WHERE hash_a=? AND hash_b=? AND mode=? AND edge_version=?", (hash_a, hash_b, mode, version)).fetchone()

    def edge_decision_put(self, hash_a: str, hash_b: str, mode: str, version: str, accepted: bool, label: str, summary: str) -> None:
        self.conn.execute("INSERT INTO edge_decisions(hash_a,hash_b,mode,edge_version,accepted,label,summary) VALUES(?,?,?,?,?,?,?) ON CONFLICT(hash_a,hash_b,mode,edge_version) DO UPDATE SET accepted=excluded.accepted,label=excluded.label,summary=excluded.summary", (hash_a, hash_b, mode, version, int(accepted), label, summary))

    def write_links_json(self, project: Any, documents: Iterable[str]) -> None:
        docs = set(documents)
        for document in docs:
            folder = Path(project.wiki) / document
            if not folder.exists():
                continue
            rows = self.conn.execute("SELECT e.*,a.document AS a_document,a.page_rel AS a_page_rel,pa.filename AS a_filename,a.text_sha256 AS a_hash,b.document AS b_document,b.page_rel AS b_page_rel,pb.filename AS b_filename,b.text_sha256 AS b_hash FROM edges e JOIN chunks a ON a.chunk_id=e.chunk_a JOIN pages pa ON pa.page_rel=a.page_rel JOIN chunks b ON b.chunk_id=e.chunk_b JOIN pages pb ON pb.page_rel=b.page_rel WHERE a.document=? OR b.document=? ORDER BY e.edge_id", (document, document)).fetchall()
            edges = []
            for row in rows:
                edges.append({"edge_id": row["edge_id"], "a": {"document": row["a_document"], "filename": row["a_filename"], "chunk_id": row["chunk_a"], "text_sha256": row["a_hash"]}, "b": {"document": row["b_document"], "filename": row["b_filename"], "chunk_id": row["chunk_b"], "text_sha256": row["b_hash"]}, "label": row["label"], "summary": row["summary"], "source": row["source"], "via": json.loads(row["via_json"] or "[]"), "created_at": row["created_at"]})
            write_json_atomic(folder / "_planning" / "links.json", {"schema_version": 1, "mode": self.meta("mode") or "legacy", "edges": edges})

    def delete_document(self, document: str) -> set[str]:
        peers = {str(row[0]) for row in self.conn.execute("SELECT DISTINCT CASE WHEN a.document=? THEN b.document ELSE a.document END FROM edges e JOIN chunks a ON a.chunk_id=e.chunk_a JOIN chunks b ON b.chunk_id=e.chunk_b WHERE a.document=? OR b.document=?", (document, document, document))}
        self.conn.execute("DELETE FROM documents WHERE document=?", (document,))
        self.conn.commit()
        return peers


def re_tokens(text: str) -> list[str]:
    """Query terms for the trigram index: distinct word runs of at least three characters."""
    seen: set[str] = set()
    tokens: list[str] = []
    for token in re.findall(r"\w+", text or "", flags=re.UNICODE):
        if len(token) >= 3 and token.casefold() not in seen:
            seen.add(token.casefold())
            tokens.append(token)
    return tokens


__all__ = ["Catalog", "EMBED_BATCH", "LinkerModeMismatch"]
