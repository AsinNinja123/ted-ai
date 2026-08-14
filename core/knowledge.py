"""
core/knowledge.py — Ted's personal knowledge base.

Persistent vector store (ChromaDB) + ONNX embeddings (fastembed, no PyTorch).
Stores text as overlapping chunks so long documents are searchable by topic.

All public functions degrade gracefully to no-op / empty string on failure so
a missing model download or a locked DB can never crash Ted.
"""

import os
import json
import time

HOME = os.path.expanduser("~/ted-ai")
DB_PATH     = os.path.join(HOME, "data", "knowledge_db")
INBOX_DIR   = os.path.join(HOME, "inbox")
INDEXED_FILE = os.path.join(HOME, "data", "indexed_files.json")

_client = None
_collection = None
_embedder = None
_init_failed = False   # latched True once we know the stack is unavailable


def _get_collection():
    """Lazy init: connect ChromaDB and load the embedding model on first call.
    Returns the collection, or None if unavailable."""
    global _client, _collection, _embedder, _init_failed
    if _init_failed:
        return None
    if _collection is not None:
        return _collection
    try:
        import chromadb
        from fastembed import TextEmbedding
        os.makedirs(DB_PATH, exist_ok=True)
        _client = chromadb.PersistentClient(path=DB_PATH)
        _collection = _client.get_or_create_collection(
            name="knowledge",
            metadata={"hnsw:space": "cosine"},
        )
        _embedder = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
        print(f"[knowledge] ready — {_collection.count()} chunks in store")
        return _collection
    except Exception as e:
        print(f"[knowledge] init failed (install chromadb + fastembed to enable): {e}")
        _init_failed = True
        return None


def warm():
    """Load ChromaDB and the embedding model ahead of the first question.

    _get_collection() is lazy, so without this the first message of a session
    pays for it: importing chromadb, starting the client, and loading (or on a
    fresh machine downloading) the fastembed model — all inside the retrieval
    window, where it eats the whole context budget and delays the reply.

    Safe to call repeatedly; after the first success it returns immediately.
    """
    try:
        _get_collection()
    except Exception as e:
        print(f"[knowledge] warm-up failed: {e}")


def _embed(texts: list) -> list:
    """Return a list of embedding vectors (as plain lists) for the given texts."""
    global _embedder
    if _embedder is None:
        _get_collection()
    if _embedder is None:
        return []
    try:
        return [v.tolist() for v in _embedder.embed(texts)]
    except Exception as e:
        print(f"[knowledge] embed error: {e}")
        return []


def _chunk(text: str, size: int = 250, overlap: int = 50) -> list:
    """Split text into overlapping word-count chunks."""
    words = text.split()
    if not words:
        return []
    chunks, i = [], 0
    while i < len(words):
        chunks.append(" ".join(words[i : i + size]))
        i += size - overlap
    return chunks


# ── Public API ────────────────────────────────────────────────────────────────

def add_text(text: str, source: str = "voice", metadata: dict = None) -> int:
    """Chunk, embed, and store text. Returns number of chunks stored, or 0 on failure."""
    col = _get_collection()
    if col is None or not text.strip():
        return 0
    chunks = _chunk(text)
    if not chunks:
        return 0
    embeddings = _embed(chunks)
    if not embeddings:
        return 0
    ts = str(int(time.time() * 1000))
    ids = [f"{source}_{ts}_{i}" for i in range(len(chunks))]
    metas = [{"source": source, **(metadata or {})} for _ in chunks]
    try:
        col.add(ids=ids, embeddings=embeddings, documents=chunks, metadatas=metas)
        return len(chunks)
    except Exception as e:
        print(f"[knowledge] add_text failed: {e}")
        return 0


# Cosine distance (the collection is created with hnsw:space=cosine), so 0 is
# identical and 2 is opposite. With BAAI/bge-small-en-v1.5 a genuinely related
# chunk lands below roughly 0.45; unrelated prose sits around 0.6-0.9. Anything
# above the cutoff is dropped rather than injected.
try:
    from config import KNOWLEDGE_MAX_DISTANCE
except Exception:
    KNOWLEDGE_MAX_DISTANCE = 0.45


def search(query: str, k: int = 4) -> str:
    """Return RELEVANT stored text for a query, or '' when nothing is relevant.

    A vector store always has a nearest neighbour. It returned the closest four
    chunks for every question ever asked, including "how are you", and that was
    ~375 tokens of unrelated text on turns that could not use it. Nearest is not
    the same as relevant, and the distance needed to be read rather than
    ignored.

    Distances are logged when something is dropped, so the cutoff can be tuned
    against real queries instead of guessed at.
    """
    col = _get_collection()
    if col is None or not query.strip():
        return ""
    total = col.count()
    if total == 0:
        return ""
    emb = _embed([query])
    if not emb:
        return ""
    try:
        results = col.query(
            query_embeddings=emb,
            n_results=min(k, total),
            include=["documents", "distances"],
        )
        docs = results.get("documents", [[]])[0]
        dists = (results.get("distances") or [[]])[0]
        if not docs:
            return ""
        if not dists:                      # older Chroma: keep the old behaviour
            return "\n".join(d for d in docs if d)
        kept = [d for d, dist in zip(docs, dists)
                if d and dist <= KNOWLEDGE_MAX_DISTANCE]
        if len(kept) < len(docs):
            print(f"[knowledge] {len(kept)}/{len(docs)} chunks above the "
                  f"relevance bar (nearest {min(dists):.3f}, "
                  f"cutoff {KNOWLEDGE_MAX_DISTANCE})")
        return "\n".join(kept)
    except Exception as e:
        print(f"[knowledge] search failed: {e}")
        return ""


def list_sources() -> list:
    """Return sorted list of unique source labels in the knowledge base."""
    col = _get_collection()
    if col is None:
        return []
    try:
        items = col.get(include=["metadatas"])
        sources = list({m.get("source", "unknown") for m in (items.get("metadatas") or [])})
        return sorted(sources)
    except Exception as e:
        print(f"[knowledge] list_sources failed: {e}")
        return []


def delete_source(source: str) -> int:
    """Remove all chunks with the given source label. Returns count deleted."""
    col = _get_collection()
    if col is None:
        return 0
    try:
        items = col.get(where={"source": source}, include=["metadatas"])
        ids = items.get("ids") or []
        if ids:
            col.delete(ids=ids)
        return len(ids)
    except Exception as e:
        print(f"[knowledge] delete_source failed: {e}")
        return 0


def count() -> int:
    """Return total number of chunks in the knowledge base."""
    col = _get_collection()
    if col is None:
        return 0
    try:
        return col.count()
    except Exception:
        return 0


# ── File / inbox intake ───────────────────────────────────────────────────────

def _load_indexed() -> set:
    try:
        with open(INDEXED_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_indexed(indexed: set) -> None:
    try:
        os.makedirs(os.path.dirname(INDEXED_FILE), exist_ok=True)
        with open(INDEXED_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(indexed), f)
    except Exception as e:
        print(f"[knowledge] save indexed list failed: {e}")


def _extract_text(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        try:
            from pypdf import PdfReader
            reader = PdfReader(path)
            parts = []
            for page in reader.pages:
                t = page.extract_text()
                if t:
                    parts.append(t)
            return "\n".join(parts)
        except Exception as e:
            print(f"[knowledge] PDF extract failed ({os.path.basename(path)}): {e}")
            return ""
    else:
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                return f.read()
        except Exception as e:
            print(f"[knowledge] file read failed: {e}")
            return ""


def add_file(path: str, source: str = None) -> int:
    """Ingest a file (PDF, txt, md) into the knowledge base.
    Returns number of chunks stored, or 0 on failure."""
    if not os.path.isfile(path):
        return 0
    source = source or os.path.basename(path)
    text = _extract_text(path)
    if not text.strip():
        return 0
    return add_text(text, source=source)


def index_inbox(inbox_dir: str = None) -> dict:
    """Scan the inbox folder and index any new files.
    Returns {"indexed": int, "skipped": int, "total_chunks": int}."""
    inbox_dir = inbox_dir or INBOX_DIR
    os.makedirs(inbox_dir, exist_ok=True)

    already_indexed = _load_indexed()
    new_count   = 0
    total_chunks = 0
    skipped     = 0
    supported   = {".pdf", ".txt", ".md", ".text"}

    for fname in os.listdir(inbox_dir):
        if os.path.splitext(fname)[1].lower() not in supported:
            continue
        if fname in already_indexed:
            skipped += 1
            continue
        fpath = os.path.join(inbox_dir, fname)
        n = add_file(fpath, source=fname)
        if n > 0:
            already_indexed.add(fname)
            new_count   += 1
            total_chunks += n
            print(f"[knowledge] indexed {fname} → {n} chunks")

    if new_count:
        _save_indexed(already_indexed)

    return {"indexed": new_count, "skipped": skipped, "total_chunks": total_chunks}


def list_indexed_files() -> list:
    """Return sorted list of file names already in the index."""
    return sorted(_load_indexed())
