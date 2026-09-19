#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ebook-Translator MCP Server (chunk edition)
=============================================

Lets any MCP client (dsh / Claude Code / Cursor / ...) read and write the
translation cache of the Calibre "Ebook Translator" plugin
(bookfere/Ebook-Translator-Calibre-Plugin, verified against v2.4.2)
directly: segmentation stays with the plugin, while the translation
itself is done by an agent in the chat window — replacing manual
copy-paste.

Core concepts (mirroring the plugin's advanced-mode UI)
-------------------------------------------------------
* One **chunk** = one numbered row in the left-hand table = one row of
  the cache table (when merged translation is enabled, paragraphs are
  aggregated up to the merge_length limit; measured ~6000 characters
  per chunk).
* **chunk_id is the cache row's cache.id** — the same addressing the
  plugin itself uses (lib/cache.py get/update/delete all operate by id);
  it never changes after creation. Deleting a row in the UI only marks
  that row ignored; the ids of all other chunks are unaffected: ids the
  agent already holds never go stale. If a chunk goes wrong (e.g. it is
  misaligned), redo just that one chunk via delete_translations +
  write_chunk — no other chunk is touched or renumbered. The minimal
  unit of database operation is the chunk.
* The left-column number in the UI is a display position (equal to
  chunk_id while no rows are deleted; shifts up after deletions). The
  ui_row field in tool results is that display position — for humans to
  locate the row in the UI; never use it for addressing.
* The original / translation shown in the UI "Proofreading" panel is
  exactly what get_original / get_translation return in full — the agent
  sees the same text a human sees, so people can review and intervene
  while the agent runs.
* **Alignment**: when merged translation is on (info.merge_length > 0),
  translation and original must split into the same number of blocks by
  the blank-line separator "\\n\\n", otherwise the UI highlights the row
  in **yellow** (Non-aligned items). This server replicates that exact
  check and reports the aligned state on every read/write, so you know
  before writing back whether the yellow warning would appear.

Cache files
-----------
    <cache root>/cache/<uid>.db   persistent cache (created once the
                                  plugin setting enables caching)
    <cache root>/temp/<uid>.db    temporary cache (when caching is not
                                  enabled: exists while the advanced-mode
                                  window is open, destroyed on close)

    Windows:       %LOCALAPPDATA%\\calibre-cache\\plugins\\ebook-translator
    macOS / Linux: $TMPDIR/com.bookfere.Calibre.EbookTranslator

Schema (identical to the plugin's lib/cache.py, verified on a real cache):

    cache(id, md5, raw, original, ignored, attributes, page,
          translation, engine_name, target_lang)
    info(key, value)  -- title / engine_name / target_lang /
                        merge_length / plugin_version ...

Important behaviors (from plugin source + live testing)
-------------------------------------------------------
* Book output (Output) runs in cache_only mode and only reads
  translations already present in the cache — externally written
  translations are picked up as-is;
* The advanced-mode UI loads everything once when opened: external
  writes do not refresh the table live; after writing, reopen the
  window with the same engine/language/merge settings to review. While
  the window is open, clicking Save in the UI overwrites that row with
  stale in-memory data — close the window while an agent writes in
  batch;
* The cache file name (uid) is a hash of book path + engine + target
  language + merge_length + encoding; changing any setting mid-way
  switches to a new cache file.

Multi-book addressing (one independent .db file per book)
---------------------------------------------------------
* list_books scans every cached book under cache/ and temp/: yes, all
  books are shown; use the keyword argument to filter by title/engine
  when there are many;
* book_id (the cache file name) is the only reliable book identity:
  **segmenting the same book with a different engine / target language /
  merge setting produces multiple cache files (same title, different
  book_id)**. list_books marks duplicate-title books; disambiguate by
  engine/target_lang/merge_length before choosing one;
* every read/write tool must carry book_id explicitly (there is no
  implicit "current book" state — books can never be mixed up), and
  **every response echoes book_id and title** — the caller can verify
  which book an operation landed on and stop immediately on mismatch;
* chunk_id is valid only inside its own book: locating a unique chunk
  requires the (book_id, chunk_id) pair; never use book A's chunk_id on
  book B.

Tools
-----
    list_books             overview of books & progress (incl. count of
                           non-aligned chunks; filterable by keyword)
    get_book_info          engine / languages / merge rule / list of
                           non-aligned chunk ids for one book
    list_chunks            lightweight status table (no full texts;
                           filterable by status)
    get_original           full original text of one chunk
    get_translation        full translation + alignment state of one
                           chunk
    write_chunk            write the translation of one chunk, reports
                           alignment
    delete_translations    clear translations of given chunks (for
                           rework)

Safety boundaries
-----------------
* Only reads/writes existing cache files: never creates books, rows or
  tables, never touches WAL;
* Writes are short transactions (BEGIN IMMEDIATE + busy retry), safe to
  run alongside the plugin UI;
* book_id only accepts file names that already exist inside the cache
  directories (path-traversal proof); SQL is fully parameterized.

Environment variables
---------------------
EBOOK_TRANSLATOR_CACHE_DIR     Explicit cache root (highest priority;
                               then the plugin's cache_path from the
                               Calibre config; then the platform
                               default)
EBOOK_TRANSLATOR_ENGINE_NAME   Engine name recorded with written
                               translations; default "MCP"
EBOOK_TRANSLATOR_SEPARATOR     Separator used for alignment checks;
                               default "\\n\\n" (same as the plugin's
                               built-in engine separator; usually
                               leave it alone)

Running
-------
    stdio (local clients: dsh / Claude Code / ...):
        python ebook_translator_mcp.py
    HTTP (remote access, dsh streamable-http transport):
        python ebook_translator_mcp.py --transport http --port 8420
    Self-test (prints the detected cache directory and book list):
        python ebook_translator_mcp.py --selftest

Dependency: pip install mcp  (works with 1.x and 2.x; or run via
uv run --with mcp to skip installing)
"""

import json
import logging
import os
import re
import sqlite3
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

try:  # mcp 2.x: FastMCP was renamed to MCPServer
    from mcp.server.mcpserver import MCPServer as _MCPServerBase
    from mcp.server.mcpserver.exceptions import ToolError as _ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _MCPServerBase
    try:
        from mcp.server.fastmcp.exceptions import ToolError as _ToolError
    except ImportError:  # fallback for very old versions
        class _ToolError(Exception):
            pass

# In stdio mode stdout belongs to the protocol channel; logs go to stderr
logging.basicConfig(
    stream=sys.stderr, level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s")

_SEPARATOR = os.environ.get("EBOOK_TRANSLATOR_SEPARATOR", "\n\n")
_ENGINE_NAME = os.environ.get("EBOOK_TRANSLATOR_ENGINE_NAME", "MCP")
_BOOK_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
_SEPARATOR_PATTERN = re.compile(_SEPARATOR)

mcp = _MCPServerBase(name="ebook-translator")


# --------------------------------------------------------------------------
# Cache directory discovery
# --------------------------------------------------------------------------

def _calibre_config_candidates() -> list[Path]:
    """Candidate locations of the Calibre plugin config (ebook_translator.json)."""
    override = os.environ.get("CALIBRE_CONFIG_DIRECTORY")
    if sys.platform == "win32":
        base = override or os.environ.get("APPDATA") or ""
        if not base:
            return []
        return [Path(base) / "calibre" / "plugins" / "ebook_translator.json"]
    if sys.platform == "darwin":
        base = override or str(Path.home() / "Library" / "Application Support")
        return [Path(base) / "calibre" / "plugins" / "ebook_translator.json"]
    base = override or os.environ.get("XDG_CONFIG_HOME") or \
        str(Path.home() / ".config")
    return [Path(base) / "calibre" / "plugins" / "ebook_translator.json"]


def cache_root() -> Path:
    """Resolve the cache root: env var > Calibre plugin config > platform default."""
    env = os.environ.get("EBOOK_TRANSLATOR_CACHE_DIR")
    if env:
        return Path(env)
    for cfg in _calibre_config_candidates():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            path = data.get("cache_path")
            if isinstance(path, str) and path.strip() and Path(path).exists():
                return Path(path)
        except Exception:
            continue
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or \
            os.path.expanduser("~/AppData/Local")
        return Path(base) / "calibre-cache" / "plugins" / "ebook-translator"
    return Path(tempfile.gettempdir()) / "com.bookfere.Calibre.EbookTranslator"


# --------------------------------------------------------------------------
# SQLite infrastructure
# --------------------------------------------------------------------------

class _ExpectedError(_ToolError, ValueError):
    """Expected business error: the full message is passed through to the agent."""


class BookNotFound(_ExpectedError):
    pass


def _connect(path: Path) -> sqlite3.Connection:
    """Open an existing cache database; read tools never write, write tools use short transactions."""
    if not path.is_file():
        raise BookNotFound(
            f"Cache database not found: {path} (use list_books to see "
            f"available books)")
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.isolation_level = None  # autocommit; writes use explicit BEGIN IMMEDIATE
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _available_books_summary(limit: int = 12) -> str:
    """For error messages: list the currently available books (id + title + engine/progress)."""
    root = cache_root()
    entries: list[str] = []
    for sub, persistent in (("cache", ""), ("temp", ", temp")):
        directory = root / sub
        if not directory.is_dir():
            continue
        for file in sorted(directory.glob("*.db")):
            try:
                conn = _connect(file)
            except BookNotFound:
                continue
            try:
                scan = _scan_chunks(conn)
            except sqlite3.DatabaseError:
                continue
            finally:
                conn.close()
            info = scan["info"]
            progress = _progress_payload(scan)
            entries.append(
                f"{file.stem} '{info.get('title') or file.stem}'"
                f" ({info.get('engine_name')}->{info.get('target_lang')},"
                f" progress {progress['translated']}"
                f"/{progress['total_chunks']}{persistent})")
    if not entries:
        return "no available books under the current cache directory"
    shown = entries[:limit]
    more = "" if len(entries) <= limit else f" ({len(entries)} total)"
    return "available books: " + "; ".join(shown) + more


def _book_path(book_id: str) -> Path:
    if not isinstance(book_id, str) or not _BOOK_ID_PATTERN.match(book_id):
        raise BookNotFound(
            f"Invalid book_id: {book_id!r} (use the id returned by "
            f"list_books)")
    root = cache_root()
    for sub in ("cache", "temp"):
        path = root / sub / f"{book_id}.db"
        if path.is_file():
            return path
    raise BookNotFound(
        f"Cache file {book_id}.db not found. There are {_available_books_summary()}. "
        f"If the list is empty or this book is missing: make sure caching "
        f"is enabled in the plugin settings (or the advanced-mode window "
        f"is still open), and that the engine / target language / merge "
        f"settings match the ones used for segmentation — the cache file "
        f"name is derived from a hash of these settings.")


def _retry_write(conn: sqlite3.Connection, action) -> None:
    """Short-transaction write with automatic locked/busy retry; safe to
    run alongside the plugin UI.

    Important: during retries (up to ~23s = 4 attempts x 5s busy_timeout
    + backoff) **not a single byte is sent to the client** — if the MCP
    client's toolCallTimeoutMs is shorter than the actual lock wait, the
    client reports a -32001 timeout first, while this process still
    completes the write and returns once the lock is released (appearing
    as "timed out but the write actually succeeded"). Therefore make sure
    the client's toolCallTimeoutMs >= 60000 (120000 recommended); every
    lock wait is also logged to stderr for diagnosis."""
    started = time.perf_counter()
    for attempt in range(4):
        try:
            conn.execute("BEGIN IMMEDIATE")
            action()
            conn.execute("COMMIT")
            waited = time.perf_counter() - started
            if attempt:
                logging.warning(
                    "Write succeeded after waiting for the lock: attempt "
                    "%d, waited %.1fs in total (the lock likely came from "
                    "a Calibre UI write; the client received no response "
                    "during the wait — a too-low timeout threshold "
                    "reports -32001 even though the write completed)",
                    attempt + 1, waited)
            return
        except sqlite3.OperationalError as e:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            message = str(e).lower()
            if ("lock" in message or "busy" in message) and attempt < 3:
                logging.warning(
                    "Database is busy (BEGIN IMMEDIATE failed on attempt "
                    "%d, waited %.1fs so far, %d retries left) — waiting "
                    "for Calibre to release the write lock",
                    attempt + 1, time.perf_counter() - started,
                    3 - attempt)
                time.sleep(0.5 * (attempt + 1))
                continue
            raise _ExpectedError(
                f"Database write failed (it may be held by Calibre for a "
                f"long time; retry later): {e}")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise


def _read_info(conn: sqlite3.Connection) -> dict[str, Any]:
    try:
        rows = conn.execute("SELECT key, value FROM info").fetchall()
    except sqlite3.OperationalError:
        return {}
    return {key: value for key, value in rows}


def _merge_length(info: dict[str, Any]) -> int:
    try:
        return int(info.get("merge_length") or 0)
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------
# Chunk model (mirroring the advanced-mode UI)
# --------------------------------------------------------------------------

def _block_count(text: str | None) -> int:
    """Number of blocks after splitting by the separator (blank line by
    default) — same grouping as the row groups in the UI proofing panel."""
    if not text or not text.strip():
        return 0
    return len(_SEPARATOR_PATTERN.split(text.strip()))


def _alignment(original: str | None, translation: str | None,
               merge_enabled: bool) -> dict[str, Any]:
    """Exact replica of the plugin's Paragraph.is_alignment: when merged
    translation is enabled, translation and original must yield the same
    number of blocks when split by blank lines, otherwise the row is
    highlighted yellow in the UI (Non-aligned)."""
    if not merge_enabled:
        return {"applicable": False, "aligned": True}
    if translation is None or not translation.strip():
        return {"applicable": True, "aligned": True}
    original_parts = _block_count(original)
    translation_parts = _block_count(translation)
    return {
        "applicable": True,
        "aligned": original_parts == translation_parts,
        "original_blocks": original_parts,
        "translation_blocks": translation_parts,
    }


def _scan_chunks(conn: sqlite3.Connection) -> dict[str, Any]:
    """Scan all non-ignored chunks (same order as the plugin's all(),
    i.e. rowid order). chunk_id is the database cache.id (stable, never
    changes); ui_row is the UI left-column display position (0-based,
    shifts up after row deletions; for human reference only)."""
    info = _read_info(conn)
    merge_enabled = _merge_length(info) > 0
    rows = conn.execute(
        "SELECT rowid, id, original, translation FROM cache"
        " WHERE NOT ignored ORDER BY rowid").fetchall()
    chunks = []
    translated = 0
    non_aligned_chunks = []
    for ui_row, (rowid, cid, original, translation) in enumerate(rows):
        try:
            chunk_id = int(cid)
        except (TypeError, ValueError):
            chunk_id = cid  # keep non-numeric ids as-is
        has_translation = bool(
            translation is not None and translation.strip())
        aligned = _alignment(original, translation, merge_enabled)["aligned"]
        if has_translation:
            translated += 1
            if not aligned:
                non_aligned_chunks.append(chunk_id)
        chunks.append({
            "chunk_id": chunk_id,
            "ui_row": ui_row,
            "rowid": rowid,
            "original": original,
            "translation": translation if has_translation else None,
            "translated": has_translation,
            "aligned": aligned,
        })
    return {
        "info": info,
        "merge_enabled": merge_enabled,
        "chunks": chunks,
        "total": len(chunks),
        "translated": translated,
        "untranslated": len(chunks) - translated,
        "non_aligned_chunks": non_aligned_chunks,
    }


def _preview(text: str | None, width: int = 60) -> str:
    """Single-line preview like the table's first column (newlines collapsed into spaces)."""
    if not text:
        return ""
    collapsed = " ".join(text.split())
    return collapsed[:width] + ("…" if len(collapsed) > width else "")


def _chunk_by_id(scan: dict[str, Any], chunk_id: int) -> dict[str, Any]:
    """Fetch a chunk by chunk_id (the database cache id, stable)."""
    if not isinstance(chunk_id, int) or isinstance(chunk_id, bool):
        raise _ExpectedError(
            f"chunk_id must be an integer (the database cache id; see "
            f"list_chunks): {chunk_id!r}")
    for chunk in scan["chunks"]:
        if chunk["chunk_id"] == chunk_id:
            return chunk
    raise _ExpectedError(
        f"chunk_id {chunk_id} does not exist. chunk_id is the database "
        f"cache id and never changes (the valid ids are exactly those "
        f"returned by list_chunks; if the row was deleted or ignored in "
        f"the UI it no longer takes part in translation or output)")


def _progress_payload(scan: dict[str, Any]) -> dict[str, Any]:
    return {
        "total_chunks": scan["total"],
        "translated": scan["translated"],
        "untranslated": scan["untranslated"],
        "non_aligned": len(scan["non_aligned_chunks"]),
    }


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

@mcp.tool()
def list_books(keyword: str = "") -> list[dict[str, Any]]:
    """List **all** cached translation books with progress (including
    temporary caches). The returned id is the book_id used by every other
    tool. Note: segmenting the same book with different engine / target
    language / merge settings produces multiple caches (same title,
    different ids) — duplicate titles are flagged duplicate_title=true;
    disambiguate by engine/target_lang/merge_length and pick one, since
    picking the wrong book makes every later read/write land on the
    wrong cache. keyword fuzzy-filters by title/engine/language (useful
    when there are many books). persistent=false means a temporary cache
    (destroyed as soon as the advanced-mode window closes)."""
    root = cache_root()
    if not root.exists():
        raise BookNotFound(
            f"Cache root directory does not exist: {root}. Enable the "
            f"cache in the Calibre plugin and segment a book in advanced "
            f"mode first, or set the EBOOK_TRANSLATOR_CACHE_DIR "
            f"environment variable.")
    books: list[dict[str, Any]] = []
    for sub, persistent in (("cache", True), ("temp", False)):
        directory = root / sub
        if not directory.is_dir():
            continue
        for file in sorted(directory.glob("*.db")):
            try:
                conn = _connect(file)
            except BookNotFound:
                continue
            try:
                scan = _scan_chunks(conn)
            except sqlite3.DatabaseError:
                continue  # not a valid database (leftover file); skip
            finally:
                conn.close()
            info = scan["info"]
            stat = file.stat()
            entry = {
                "id": file.stem,
                "title": info.get("title") or file.stem,
                "engine": info.get("engine_name"),
                "target_lang": info.get("target_lang"),
                "merge_length": _merge_length(info),
                "persistent": persistent,
                "size_bytes": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime)
                .strftime("%Y-%m-%d %H:%M:%S"),
                **_progress_payload(scan),
            }
            if keyword:
                haystack = " ".join(filter(None, (
                    entry["title"], entry["engine"],
                    entry["target_lang"], entry["id"]))).lower()
                if keyword.lower() not in haystack:
                    continue
            books.append(entry)
    if not books:
        raise BookNotFound(
            f"No caches found under {root}. Common causes: caching is "
            f"not enabled in the plugin settings; or caching was never "
            f"enabled and the advanced-mode window has been closed (its "
            f"temporary cache is destroyed).")
    # Duplicate-title flag: one title with multiple caches (different
    # engine/language/merge settings)
    title_counts: dict[str, int] = {}
    for entry in books:
        title_counts[entry["title"]] = title_counts.get(entry["title"], 0) + 1
    for entry in books:
        if title_counts[entry["title"]] > 1:
            entry["duplicate_title"] = True
            entry["disambiguation_note"] = (
                "Duplicate title: this book has multiple caches. Confirm "
                "by engine/target_lang/merge_length/persistent, pick "
                "exactly one, and use the same id for the whole run")
    return books


@mcp.tool()
def get_book_info(book_id: str) -> dict[str, Any]:
    """Details of one book's cache: engine, target language, merge /
    alignment rules, progress, and the list of non-aligned (yellow-
    highlighted) chunk ids."""
    conn = _connect(_book_path(book_id))
    try:
        scan = _scan_chunks(conn)
        info = scan["info"]
        merge_enabled = scan["merge_enabled"]
        return {
            "book_id": book_id,
            "title": info.get("title"),
            "engine": info.get("engine_name"),
            "target_lang": info.get("target_lang"),
            "merge_length": _merge_length(info),
            "merge_translation_enabled": merge_enabled,
            "alignment_rule": (
                f"Translation and original must split into the same "
                f"number of blocks by blank lines ({_SEPARATOR!r}); "
                f"otherwise the chunk is highlighted yellow in the "
                f"plugin UI (Non-aligned)"
                if merge_enabled else
                "Merged translation is disabled; the UI performs no "
                "alignment check"),
            "non_aligned_chunks": scan["non_aligned_chunks"],
            "identity_note": (
                "chunk_id is the database cache id and never changes: "
                "deleting rows in the UI does not renumber any chunk, and "
                "a broken chunk only needs its own redo "
                "(delete_translations + write_chunk). The UI left-column "
                "display position is returned as ui_row and is for human "
                "reference only. The cache file is uniquely determined by "
                "book + engine + target language + merge settings; reopen "
                "advanced mode with the same settings"),
            "output_hint": (
                "When everything is translated: reopen advanced mode in "
                "Calibre with the same engine/language/merge settings "
                "(the table loads the written translations), review, "
                "then click Output to produce the ebook"),
            **_progress_payload(scan),
        }
    finally:
        conn.close()


@mcp.tool()
def list_chunks(
    book_id: str,
    status: str = "all",
    keyword: str = "",
) -> dict[str, Any]:
    """Lightweight list of every chunk id of one book with status, without
    full texts (to keep context small); mirrors the left-hand table of
    the UI. status options: all / untranslated / translated / misaligned
    (the UI's yellow rows); keyword fuzzy-matches original or translation
    text. Each item contains: chunk_id (for addressing; never changes;
    valid only inside this book), ui_row (UI display position, human
    reference only), status, alignment, block count, character count and
    a one-line preview. Always address chunks as book_id + chunk_id —
    never use ui_row, and never carry this book's chunk_id over to
    another book. The response echoes book_id and title so the caller
    can verify it is working on the intended book."""
    if status not in ("all", "untranslated", "translated", "misaligned"):
        raise _ExpectedError(
            "status must be one of all / untranslated / translated / "
            "misaligned")
    conn = _connect(_book_path(book_id))
    try:
        scan = _scan_chunks(conn)
        merge_enabled = scan["merge_enabled"]
        items: list[dict[str, Any]] = []
        for chunk in scan["chunks"]:
            if status == "untranslated" and chunk["translated"]:
                continue
            if status == "translated" and not chunk["translated"]:
                continue
            if status == "misaligned" and (
                    not chunk["translated"] or chunk["aligned"]):
                # yellow highlighting applies only to translated rows
                # with mismatched block counts
                continue
            if keyword:
                haystack = (
                    (chunk["original"] or "") + "\n"
                    + (chunk["translation"] or "")).lower()
                if keyword.lower() not in haystack:
                    continue
            item = {
                "chunk_id": chunk["chunk_id"],
                "ui_row": chunk["ui_row"],
                "status": "translated" if chunk["translated"]
                          else "untranslated",
                "aligned": chunk["aligned"] if chunk["translated"] else None,
                "characters": len(chunk["original"] or ""),
                "preview": _preview(chunk["original"]),
            }
            if merge_enabled:
                item["blocks"] = _block_count(chunk["original"])
            items.append(item)
        return {
            "book_id": book_id,
            "title": scan["info"].get("title"),
            "filter": {"status": status, "keyword": keyword or None},
            "count": len(items),
            "chunks": items,
        }
    finally:
        conn.close()


@mcp.tool()
def get_original(
    book_id: str,
    chunk_id: int,
    include_raw: bool = False,
) -> dict[str, Any]:
    """Read the full original text of one chunk — identical to what the
    UI's proofreading panel shows, so the agent sees the same text a
    human sees. chunk_id is this book's database cache id (never changes;
    see list_chunks; valid only inside this book — never use it across
    books). When merged translation is enabled the response includes
    blocks: the translation's blank-line block count must equal it to be
    aligned (otherwise the UI highlights the chunk yellow).
    include_raw=True additionally returns the raw HTML. The response
    echoes book_id and title — verify it is the intended book."""
    conn = _connect(_book_path(book_id))
    try:
        scan = _scan_chunks(conn)
        chunk = _chunk_by_id(scan, chunk_id)
        result: dict[str, Any] = {
            "book_id": book_id,
            "chunk_id": chunk["chunk_id"],
            "ui_row": chunk["ui_row"],
            "title": scan["info"].get("title"),
            "status": "translated" if chunk["translated"]
                      else "untranslated",
            # same as the UI proofreading panel: show the stripped original
            "original": (chunk["original"] or "").strip(),
            "merge_enabled": scan["merge_enabled"],
        }
        if scan["merge_enabled"]:
            result["blocks"] = _block_count(chunk["original"])
        if include_raw:
            extra = conn.execute(
                "SELECT raw FROM cache WHERE rowid = ?",
                (chunk["rowid"],)).fetchone()
            if extra:
                result["raw"] = extra[0]
        return result
    finally:
        conn.close()


@mcp.tool()
def get_translation(book_id: str, chunk_id: int) -> dict[str, Any]:
    """Read the full translation and alignment state of one chunk —
    identical to what the UI's proofreading panel shows. chunk_id is
    this book's database cache id (never changes; see list_chunks; valid
    only inside this book — never use it across books). translation is
    null when the chunk is untranslated. When merged translation is
    enabled the response carries the alignment verdict (whether
    translation and original have equal blank-line block counts, i.e.
    whether the UI highlights the row yellow). The response echoes
    book_id and title — verify it is the intended book."""
    conn = _connect(_book_path(book_id))
    try:
        scan = _scan_chunks(conn)
        chunk = _chunk_by_id(scan, chunk_id)
        alignment = _alignment(
            chunk["original"], chunk["translation"], scan["merge_enabled"])
        result: dict[str, Any] = {
            "book_id": book_id,
            "chunk_id": chunk["chunk_id"],
            "ui_row": chunk["ui_row"],
            "title": scan["info"].get("title"),
            "status": "translated" if chunk["translated"]
                      else "untranslated",
            # same as the UI proofreading panel: show the stripped translation
            "translation": chunk["translation"].strip()
            if chunk["translation"] else None,
            "merge_enabled": scan["merge_enabled"],
            "alignment": alignment,
        }
        extra = conn.execute(
            "SELECT engine_name, target_lang FROM cache"
            " WHERE rowid = ?", (chunk["rowid"],)).fetchone()
        if extra:
            result["engine_name"] = extra[0]
            result["target_lang"] = extra[1]
        if scan["merge_enabled"] and chunk["translated"]:
            result["yellow_warning"] = not alignment["aligned"]
        return result
    finally:
        conn.close()


@mcp.tool()
def write_chunk(
    book_id: str,
    chunk_id: int,
    translation: str,
    overwrite: bool = True,
) -> dict[str, Any]:
    """Write the full translation of one chunk (stripped before writing,
    matching plugin behavior; engine name and target language are
    recorded). chunk_id is this book's database cache id (never changes;
    see list_chunks; valid only inside this book). Right after writing,
    the tool replicates the UI alignment check and returns the aligned
    state — mismatched block counts come with a warning (the chunk will
    be highlighted yellow in the UI). With overwrite=False an existing
    translation is skipped. The response echoes book_id and title:
    writing is irreversible, verify it is the intended book first."""
    if not isinstance(translation, str) or not translation.strip():
        raise _ExpectedError(
            "translation must not be empty; use delete_translations to "
            "clear a translation")
    text = translation.strip()
    conn = _connect(_book_path(book_id))
    try:
        scan = _scan_chunks(conn)
        chunk = _chunk_by_id(scan, chunk_id)
        if chunk["translated"] and not overwrite:
            return {
                "book_id": book_id,
                "title": scan["info"].get("title"),
                "chunk_id": chunk["chunk_id"], "ui_row": chunk["ui_row"],
                "written": False,
                "skipped_existing": True,
                "progress": _progress_payload(scan),
            }
        target_lang = scan["info"].get("target_lang")

        def action() -> None:
            conn.execute(
                "UPDATE cache SET translation = ?, engine_name = ?,"
                " target_lang = ? WHERE rowid = ?",
                (text, _ENGINE_NAME, target_lang, chunk["rowid"]))
        _retry_write(conn, action)

        # rescan after the write: return the UI-consistent alignment state
        # and fresh progress
        scan = _scan_chunks(conn)
        updated = _chunk_by_id(scan, chunk["chunk_id"])
        alignment = _alignment(
            updated["original"], updated["translation"], scan["merge_enabled"])
        result: dict[str, Any] = {
            "book_id": book_id,
            "title": scan["info"].get("title"),
            "chunk_id": updated["chunk_id"],
            "ui_row": updated["ui_row"],
            "written": True,
            "characters": len(text),
            "alignment": alignment,
            "progress": _progress_payload(scan),
        }
        if scan["merge_enabled"] and not alignment["aligned"]:
            result["warning"] = (
                f"Alignment warning: original has "
                f"{alignment['original_blocks']} blocks / translation has "
                f"{alignment['translation_blocks']} blocks (blank-line "
                f"block counts must match, otherwise this chunk is "
                f"highlighted yellow in the plugin UI as Non-aligned)")
        return result
    finally:
        conn.close()


@mcp.tool()
def delete_translations(
    book_id: str,
    chunk_ids: list[int],
) -> dict[str, Any]:
    """Clear the translations of the given chunks (for rework); originals
    and all other fields are untouched. chunk_ids is a list of this
    book's database cache ids (never change; see list_chunks; valid only
    inside this book). A chunk that went wrong only needs its own redo —
    other chunks are not affected. The response echoes book_id and
    title; verify them."""
    if not chunk_ids:
        raise _ExpectedError("chunk_ids must not be an empty list")
    conn = _connect(_book_path(book_id))
    try:
        scan = _scan_chunks(conn)
        rowids: list[int] = []
        deleted: list[int] = []
        not_found: list[int] = []
        seen: set[int] = set()
        for chunk_id in chunk_ids:
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            try:
                chunk = _chunk_by_id(scan, chunk_id)
            except _ExpectedError:
                not_found.append(chunk_id)
                continue
            rowids.append(chunk["rowid"])
            deleted.append(chunk["chunk_id"])
        if rowids:
            placeholders = ", ".join("?" for _ in rowids)

            def action() -> None:
                conn.execute(
                    f"UPDATE cache SET translation = NULL,"
                    f" engine_name = NULL, target_lang = NULL"
                    f" WHERE rowid IN ({placeholders})", rowids)
            _retry_write(conn, action)
        return {
            "book_id": book_id,
            "title": scan["info"].get("title"),
            "deleted_chunk_ids": deleted,
            "not_found_chunk_ids": not_found,
            "progress": _progress_payload(_scan_chunks(conn)),
        }
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def _selftest() -> None:
    root = cache_root()
    print(f"Cache root: {root}")
    try:
        for book in list_books():
            persistent = "persistent" if book["persistent"] else "temp"
            print(f"  [{persistent}] {book['id']}  '{book['title']}'"
                  f"  engine={book['engine']}"
                  f"  target_lang={book['target_lang']}"
                  f"  progress={book['translated']}/{book['total_chunks']}"
                  f" chunks  non_aligned={book['non_aligned']}")
        print("Self-test passed: the cache directory is accessible and the"
              " books above can be read and written by the MCP tools.")
    except BookNotFound as e:
        print(f"Note: {e}")
        sys.exit(1)


def main() -> None:
    args = sys.argv[1:]
    if "--selftest" in args:
        _selftest()
        return
    transport = "stdio"
    if "--transport" in args:
        index = args.index("--transport")
        transport = args[index + 1] if index + 1 < len(args) else "stdio"
    if transport == "http":
        transport = "streamable-http"
    if transport == "streamable-http":
        port = 8420
        if "--port" in args:
            index = args.index("--port")
            port = int(args[index + 1]) if index + 1 < len(args) else 8420
        if hasattr(mcp, "settings"):  # mcp 1.x
            mcp.settings.host = "127.0.0.1"
            mcp.settings.port = port
            mcp.run(transport="streamable-http")
        else:  # mcp 2.x
            mcp.run(transport="streamable-http",
                    host="127.0.0.1", port=port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
