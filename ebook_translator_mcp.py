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
    get_original_to_file   export one chunk's original to a local file
                           (metadata only: bulk text stays out of the
                           conversation)
    get_translation        full translation + alignment state of one
                           chunk
    get_translation_to_file export one chunk's translation to a local
                           file (offline rework / review / migration)
    write_chunk            write the translation of one chunk, reports
                           alignment
    write_chunk_from_file  write one chunk's translation from a local
                           .txt file (path only: zero content tokens,
                           no truncation risk on huge chunks; BOM
                           stripped, CRLF normalized, readback-verified)
    delete_translations    clear translations of given chunks (for
                           rework)

File pipeline (token-safe bulk text)
------------------------------------
    get_original_to_file and write_chunk_from_file form a symmetric
    pair: originals flow cache -> file, translations flow file ->
    cache. Tens of thousands of characters never pass through the
    agent — zero content tokens, no truncation or mutation risk; the
    local file is the source of truth, the cache is the mirror.
    get_translation_to_file completes the rework loop for yellow
    (misaligned) chunks: export the translation, fix the block count
    offline (editor or script), write it back. It also enables cache
    migration when an engine / language / merge change creates a new
    cache file, and human-review handoff.

Safety boundaries
-----------------
* Only reads/writes existing cache files: never creates books, rows or
  tables, never touches WAL;
* The *_to_file export tools write plain text files to caller-given
  local paths: missing parent directories are auto-created (creating a
  directory destroys nothing), but an existing file is only replaced
  with overwrite=True;
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
                               leave it alone). The file tool's
                               CRLF->LF normalization applies only
                               while this separator is LF-based.

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
# File pipeline: text ingestion, text export, shared write path
# --------------------------------------------------------------------------

def _read_translation_file(file_path: str) -> tuple[str, Path]:
    """Read a translation file for write_chunk_from_file; return
    (text, resolved_path).

    Why a file tool exists: for large chunks (tens of thousands of
    characters) making the agent re-type the translation as a tool
    argument costs huge tokens and risks silent truncation/mutation in
    transit. With a file path the agent never touches the content: the
    local file is the source of truth, the cache becomes a mirror.

    Deliberate choices (each addresses a real-world pitfall):
    * utf-8-sig decoding — transparently strips a UTF-8 BOM (EF BB BF)
      when present. Some editors/tools write one; a BOM would silently
      prepend U+FEFF to the first character of the stored translation.
      Files that are not valid UTF-8 are rejected with a clear error —
      no silent encoding guessing (a wrongly auto-decoded GBK file
      would store mojibake).
    * CRLF/CR -> LF normalization, but only while the configured
      alignment separator is LF-based (the default "\\n\\n" and any
      other "\\n"-only separator): the alignment check splits on the
      literal separator, so text stored with CRLF could never match an
      LF separator (every multi-block chunk would be flagged
      misaligned). The plugin's own engines also store LF, so this
      matches the cache's existing convention. If the separator itself
      contains "\\r" (an exotic CRLF-based separator), CR characters
      are part of the alignment split and are preserved verbatim.
    * Leading/trailing whitespace is stripped — exactly like
      write_chunk (same strip, same stored result for the same text;
      only CRLF input can differ, normalized as above under the
      default LF separator).
    """
    if not isinstance(file_path, str) or not file_path.strip():
        raise _ExpectedError(
            "file_path must be a non-empty string (absolute path "
            "recommended; relative paths resolve against the server "
            "process's working directory, which may differ from the "
            "caller's)")
    path = Path(file_path.strip()).expanduser()
    if not path.is_absolute():
        # Relative paths resolve against THIS process's working
        # directory — the caller's cwd is not visible here and may be
        # different. The resolved path is echoed back as source_file.
        path = Path.cwd() / path
    try:
        data = path.read_bytes()
    except OSError as e:
        raise _ExpectedError(
            f"Cannot read file {path}: {e} (pass an existing UTF-8 "
            f"text file; relative paths resolve against the server's "
            f"working directory — prefer absolute paths)")
    if not data:
        raise _ExpectedError(
            f"File is empty: {path} (the translation would be empty; "
            f"use delete_translations to clear a translation instead)")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise _ExpectedError(
            f"File is not valid UTF-8: {path} ({e}). Re-save the file "
            f"as UTF-8 (a UTF-8 BOM is fine — it is stripped "
            f"automatically) and retry")
    # Normalize CRLF/CR to LF only while the configured separator is
    # LF-based; if the separator itself contains "\r", CR is part of
    # the alignment split and must survive into the database verbatim.
    if "\r" not in _SEPARATOR:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.strip()
    if not text:
        raise _ExpectedError(
            f"File contains only whitespace: {path} (the translation "
            f"would be empty)")
    return text, path


def _export_text_to_file(
    text: str,
    file_path: str,
    overwrite: bool,
) -> dict[str, Any]:
    """Write `text` to a local file for the *_to_file export tools;
    return a partial result dict (exported_file / characters, plus
    verify_mismatch on mismatch) to be merged into the tool response.

    Deliberate choices (mirroring _read_translation_file's philosophy):
    * UTF-8 without BOM, LF line endings. newline="" is mandatory: with
      the default newline=None, Python would translate "\n" to
      os.linesep (\r\n on Windows) — the exported file would then
      contradict the LF-based alignment conventions and differ from
      what the database stores. newline="" writes the text verbatim.
    * Missing parent directories are created automatically. Principle:
      automate the non-destructive, guard the destructive — creating a
      directory destroys nothing (a mistyped path merely lands one
      extra file, and the exported_file echo makes it immediately
      visible), whereas overwriting an existing file may destroy data
      (e.g. a finished translation draft), so that stays behind the
      explicit overwrite flag (default False).
    * The file is read back after writing and compared with `text`: on
      mismatch the response carries verify_mismatch: true (absent when
      verified) — the same hard guarantee the write tools give, so a
      caller never has to trust an export blindly.
    * Relative paths resolve against THIS process's working directory
      (same rule as the read side); the resolved absolute path is
      echoed back as exported_file so the caller can catch mismatches.
    """
    if not isinstance(file_path, str) or not file_path.strip():
        raise _ExpectedError(
            "file_path must be a non-empty string (absolute path "
            "recommended; relative paths resolve against the server "
            "process's working directory, which may differ from the "
            "caller's)")
    if not isinstance(text, str) or not text.strip():
        raise _ExpectedError("Nothing to export: the text is empty")
    path = Path(file_path.strip()).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if path.is_dir():
        raise _ExpectedError(
            f"Target path is a directory, not a file: {path}")
    if path.exists() and not overwrite:
        raise _ExpectedError(
            f"File already exists: {path} (pass overwrite=True to "
            f"replace it; refusing by default so a mistyped path can "
            f"never silently destroy an existing file)")
    try:
        # auto-create missing parents: non-destructive convenience
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
    except OSError as e:
        raise _ExpectedError(f"Cannot write file {path}: {e}")
    result: dict[str, Any] = {
        "exported_file": str(path),
        "characters": len(text),
    }
    try:
        readback = path.read_text(encoding="utf-8")
    except OSError as e:
        raise _ExpectedError(f"Cannot read back {path}: {e}")
    if readback != text:
        # Theoretically unreachable; kept as a hard guarantee for the
        # caller: an absent verify_mismatch field means "verified".
        result["verify_mismatch"] = True
        result["verify_note"] = (
            "Readback verification failed: the file on disk differs "
            "from the exported text. Do not trust this export; inspect "
            "the file and retry")
    return result


def _write_translation(
    conn: sqlite3.Connection,
    book_id: str,
    scan: dict[str, Any],
    chunk: dict[str, Any],
    text: str,
    overwrite: bool,
) -> dict[str, Any]:
    """Shared write path of write_chunk and write_chunk_from_file.

    Keeping one implementation guarantees the two tools store identical
    content (same strip, same UPDATE) and return identical structures —
    the file variant only adds the source_file echo and (on failure)
    the verify_mismatch flag.

    Readback verification: after writing, the stored text is re-read
    from the database (the rescan below) and compared with `text`. On
    mismatch the response carries verify_mismatch: true — a last line
    of defense so the caller never has to trust a write blindly (the
    field is absent when the readback matches)."""
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
    # and fresh progress — the same re-read doubles as the readback
    # verification below
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
    if updated["translation"] != text:
        # Theoretically unreachable; kept as a hard guarantee for the
        # caller: an absent verify_mismatch field means "verified".
        result["verify_mismatch"] = True
        result["verify_note"] = (
            "Readback verification failed: the text stored in the cache "
            "differs from the text that was submitted. Do not trust "
            "this write; inspect with get_translation and retry")
    if scan["merge_enabled"] and not alignment["aligned"]:
        result["warning"] = (
            f"Alignment warning: original has "
            f"{alignment['original_blocks']} blocks / translation has "
            f"{alignment['translation_blocks']} blocks (blank-line "
            f"block counts must match, otherwise this chunk is "
            f"highlighted yellow in the plugin UI as Non-aligned)")
    return result


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
def get_original_to_file(
    book_id: str,
    chunk_id: int,
    file_path: str,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Export one chunk's original text to a local file and return only
    metadata — the text itself never enters the conversation (zero
    content tokens, no truncation risk on huge chunks). Mirrors
    get_original (same metadata fields, same stripped text as the UI
    proofreading panel shows) and is the symmetric counterpart of
    write_chunk_from_file: originals flow cache -> file, translations
    flow file -> cache, bulk text never passes through the agent.

    Typical use: the first pass of a formal translation workflow —
    export the original to a file, translate against the file, write
    the result back with write_chunk_from_file. For ad-hoc inspection
    prefer get_original directly.

    File handling: UTF-8 without BOM, LF line endings. Missing parent
    directories are created automatically (creating a directory
    destroys nothing; the resolved path is echoed as exported_file).
    An existing file is NOT overwritten unless overwrite=True — a
    mistyped path must never silently destroy an existing file (e.g. a
    finished translation draft). After writing, the file is read back
    and verified; verify_mismatch appears only if it differs. Relative
    paths resolve against the server process's working directory,
    which may differ from the caller's — absolute paths are strongly
    recommended. chunk_id is this book's database cache id (see
    list_chunks); the response echoes book_id and title — verify it is
    the intended book."""
    conn = _connect(_book_path(book_id))
    try:
        scan = _scan_chunks(conn)
        chunk = _chunk_by_id(scan, chunk_id)
        text = (chunk["original"] or "").strip()
        if not text:
            raise _ExpectedError(
                f"chunk {chunk_id} has no original text to export")
        result: dict[str, Any] = {
            "book_id": book_id,
            "chunk_id": chunk["chunk_id"],
            "ui_row": chunk["ui_row"],
            "title": scan["info"].get("title"),
            "status": "translated" if chunk["translated"]
                      else "untranslated",
            "merge_enabled": scan["merge_enabled"],
        }
        if scan["merge_enabled"]:
            result["blocks"] = _block_count(chunk["original"])
        # merge the export result (exported_file / characters /
        # verify_mismatch on mismatch); the response never contains
        # the original text itself
        result.update(_export_text_to_file(text, file_path, overwrite))
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
def get_translation_to_file(
    book_id: str,
    chunk_id: int,
    file_path: str,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Export one chunk's current translation to a local file and
    return only metadata — the text itself never enters the
    conversation. Mirrors get_translation (same metadata and alignment
    fields, same stripped text as the UI proofreading panel shows).

    Typical uses:
    * Rework a yellow (misaligned) chunk without any bulk text through
      the agent: export the translation, fix the block count offline
      (editor or script — deterministic, zero tokens), write it back
      with write_chunk_from_file.
    * Migrate translations after changing engine / target language /
      merge settings (the plugin then creates a new cache file):
      export every chunk from the old cache, write them into the new
      one.
    * Hand the current draft to a human reviewer, or archive a book's
      translations as plain text.

    The chunk must already have a translation (status translated);
    exporting an untranslated chunk is an error. File handling is the
    same as get_original_to_file: UTF-8 without BOM, LF; missing
    parent directories are created automatically; an existing file is
    not overwritten unless overwrite=True; the file is read back and
    verified (verify_mismatch appears only on mismatch). Relative
    paths resolve against the server process's working directory —
    absolute paths recommended. chunk_id is this book's database cache
    id (see list_chunks); the response echoes book_id and title —
    verify it is the intended book."""
    conn = _connect(_book_path(book_id))
    try:
        scan = _scan_chunks(conn)
        chunk = _chunk_by_id(scan, chunk_id)
        if not chunk["translated"]:
            raise _ExpectedError(
                f"chunk {chunk_id} has no translation to export (its "
                f"status is untranslated; write one first via "
                f"write_chunk or write_chunk_from_file)")
        text = (chunk["translation"] or "").strip()
        alignment = _alignment(
            chunk["original"], chunk["translation"], scan["merge_enabled"])
        result: dict[str, Any] = {
            "book_id": book_id,
            "chunk_id": chunk["chunk_id"],
            "ui_row": chunk["ui_row"],
            "title": scan["info"].get("title"),
            "status": "translated",
            "merge_enabled": scan["merge_enabled"],
            "alignment": alignment,
        }
        extra = conn.execute(
            "SELECT engine_name, target_lang FROM cache"
            " WHERE rowid = ?", (chunk["rowid"],)).fetchone()
        if extra:
            result["engine_name"] = extra[0]
            result["target_lang"] = extra[1]
        if scan["merge_enabled"]:
            result["yellow_warning"] = not alignment["aligned"]
        # merge the export result (exported_file / characters /
        # verify_mismatch on mismatch); the response never contains
        # the translation text itself
        result.update(_export_text_to_file(text, file_path, overwrite))
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
    be highlighted yellow in the UI), and the stored text is read back
    and verified (verify_mismatch appears only if it differs). With
    overwrite=False an existing translation is skipped. The response
    echoes book_id and title: writing is irreversible, verify it is the
    intended book first. For very large translations prefer
    write_chunk_from_file (file-based: zero content tokens, no
    truncation risk)."""
    if not isinstance(translation, str) or not translation.strip():
        raise _ExpectedError(
            "translation must not be empty; use delete_translations to "
            "clear a translation")
    text = translation.strip()
    conn = _connect(_book_path(book_id))
    try:
        scan = _scan_chunks(conn)
        chunk = _chunk_by_id(scan, chunk_id)
        return _write_translation(
            conn, book_id, scan, chunk, text, overwrite)
    finally:
        conn.close()


@mcp.tool()
def write_chunk_from_file(
    book_id: str,
    chunk_id: int,
    file_path: str,
    overwrite: bool = True,
) -> dict[str, Any]:
    """Write one chunk's translation from a local text (.txt) file — the
    recommended path for large translations. The agent passes only a
    file path, never the content: zero token cost for the text and no
    risk of truncation or mutation in transit. Whatever is in the file
    is exactly what gets stored (the local file is the source of truth,
    the cache is the mirror).

    File handling: the file must exist and be valid UTF-8. A UTF-8 BOM
    is stripped automatically; leading/trailing whitespace is stripped
    exactly like write_chunk; and — with the default LF-based alignment
    separator — CRLF/CR line endings are normalized to LF (text stored
    with CRLF could never match an LF separator and every multi-block
    chunk would be flagged misaligned). If the configured separator
    itself contains CR, line endings are preserved verbatim instead.
    Empty or whitespace-only files are rejected. Relative paths resolve
    against the server process's working directory, which may differ
    from the caller's — absolute paths are strongly recommended (the
    resolved path is echoed back as source_file so mismatches are
    visible).

    The response has the same structure as write_chunk (written /
    characters / alignment / progress / warning), plus source_file.
    After writing, the stored text is read back and compared inside
    this tool: if verify_mismatch is present (true), the stored text
    differs from the file — do not trust the write; inspect with
    get_translation and retry. overwrite=False skips chunks that
    already have a translation. chunk_id is this book's database cache
    id (see list_chunks); the response echoes book_id and title —
    verify it is the intended book."""
    text, path = _read_translation_file(file_path)
    conn = _connect(_book_path(book_id))
    try:
        scan = _scan_chunks(conn)
        chunk = _chunk_by_id(scan, chunk_id)
        result = _write_translation(
            conn, book_id, scan, chunk, text, overwrite)
        # echo the resolved path: with relative paths the caller can
        # immediately see which file the server actually read
        result["source_file"] = str(path)
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
