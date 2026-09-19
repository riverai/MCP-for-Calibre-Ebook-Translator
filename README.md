# MCP for Calibre Ebook Translator

[English](README.md) | [简体中文](README.zh-CN.md)

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![Plugin v2.4.2 verified](https://img.shields.io/badge/plugin-v2.4.2%20verified-brightgreen)
![License MIT](https://img.shields.io/badge/license-MIT-green)

An [MCP](https://modelcontextprotocol.io) server that lets any AI agent (Claude Desktop, Claude Code, Cursor, Cline, dsh, ...) read and write the translation cache of the Calibre [Ebook Translator](https://github.com/bookfere/Ebook-Translator-Calibre-Plugin) plugin — directly, chunk by chunk, replacing manual copy-paste.

The plugin keeps doing what it is good at (parsing ebooks, splitting text into numbered chunks, merging output). The agent does the translation in your chat window, using any model you like. The book never leaves your machine: everything happens through local SQLite cache files.

![The plugin's advanced mode: each numbered row is one chunk the MCP tools can read and write](https://github.com/user-attachments/assets/8577f6b4-fab9-4210-9eb7-5622afab97be)

## How it works

The Ebook Translator plugin stores each book's translation progress in a SQLite cache. This server opens those cache files read/write and exposes seven tools: list books, inspect chunk status, read a chunk's original text, read or write its translation, and clear translations for rework. Every response echoes the book id and title so the agent (and you) can always verify which book is being touched.

Key properties:

- **Chunk-addressed**: one chunk = one numbered row in the plugin's advanced mode table. `chunk_id` is the database row id — it never changes, even if you delete other rows in the UI.
- **Alignment-aware**: when the plugin's merged translation is enabled, a translation must split into the same number of blank-line-separated blocks as its original, otherwise the plugin highlights the row yellow. The server replicates this exact check and reports `aligned` on every read and write — you know *before* reopening the plugin whether a row would go yellow.
- **Safe by construction**: only existing cache files are touched; writes are short retrying transactions, safe to run while Calibre is open; SQL is fully parameterized and `book_id` cannot escape the cache directory.

## Requirements

- [Calibre](https://calibre-ebook.com) with the [Ebook Translator plugin](https://github.com/bookfere/Ebook-Translator-Calibre-Plugin) (verified against v2.4.2), with a book already segmented in **advanced mode** (caching enabled, or the advanced-mode window still open)
- Python 3.10+ (from [python.org](https://www.python.org/downloads/) — on Windows, avoid the Microsoft Store stub, see [Troubleshooting](#troubleshooting))
- Any MCP client that supports stdio servers

## Install and selftest

**Option A — run directly with [uv](https://docs.astral.sh/uv/) (no install, no clone):**

```bash
uvx --from git+https://github.com/riverai/MCP-for-Calibre-Ebook-Translator ebook-translator-mcp --selftest
```

**Option B — manual:**

```bash
pip install mcp
# download ebook_translator_mcp.py, then:
python /path/to/ebook_translator_mcp.py --selftest
```

The self-test prints the cache directory it detected and every book it can see. If your books show up there, the server will work.

## Connect your MCP client

The server name below can be anything; `command`/`args` is what matters.

**Claude Desktop** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "ebook-translator": {
      "command": "uvx",
      "args": [
        "--from", "git+https://github.com/riverai/MCP-for-Calibre-Ebook-Translator",
        "ebook-translator-mcp"
      ]
    }
  }
}
```

or, with a local copy and `pip install mcp`:

```json
{
  "mcpServers": {
    "ebook-translator": {
      "command": "python",
      "args": ["C:/tools/MCP-for-Calibre-Ebook-Translator/ebook_translator_mcp.py"]
    }
  }
}
```

**Claude Code**:

```bash
claude mcp add ebook-translator -- uvx --from git+https://github.com/riverai/MCP-for-Calibre-Ebook-Translator ebook-translator-mcp
# or: claude mcp add ebook-translator -- python /absolute/path/to/ebook_translator_mcp.py
```

**Cursor** (`.cursor/mcp.json` or `~/.cursor/mcp.json`) and **Cline** (`cline_mcp_settings.json`): use the same `mcpServers` JSON as Claude Desktop.

**dsh**: see [`dsh/register.yml`](dsh/register.yml) — note the `toolCallTimeoutMs` recommendation there.

**Remote (Calibre on another machine)**: start HTTP mode on that machine, then point any streamable-http client at it:

```bash
python ebook_translator_mcp.py --transport http --port 8420
# endpoint: http://<that-machine>:8420/mcp
```

## Tools

| Tool | What it does |
|---|---|
| `list_books` | List every cached book: engine, languages, progress, non-aligned count; `keyword` filters by title/engine |
| `get_book_info` | One book's engine, target language, merge/alignment rule, progress, and the list of non-aligned chunk ids |
| `list_chunks` | Lightweight per-chunk status table (no full texts); filter by `status` (`all`/`untranslated`/`translated`/`misaligned`) or `keyword` |
| `get_original` | Full original text of one chunk — exactly what the plugin's proofreading panel shows |
| `get_translation` | Full translation of one chunk plus the alignment verdict (`yellow_warning` = will be highlighted in the UI) |
| `write_chunk` | Write one chunk's translation; returns alignment state and a `warning` if the block counts mismatch |
| `delete_translations` | Clear translations of the given chunks (for rework); originals untouched |

## Addressing model

- `book_id` is the cache file name, obtained from `list_books`. Every call echoes `book_id` + title — check the echo.
- Segmenting the same book with a different engine / target language / merge setting creates a **separate cache** (same title, different id). Duplicate titles are flagged `duplicate_title`; disambiguate by `engine` / `target_lang` / `merge_length`.
- `chunk_id` is the database row id: stable forever, but valid only inside its own book. One chunk going wrong never renumbers the others — redo just that chunk (`delete_translations` + `write_chunk`).
- `ui_row` is the row's display position in the plugin table (for humans to find the row on screen). Never address by it.

## Alignment (the yellow highlight)

With merged translation enabled, original and translation must split into the same number of blocks on blank lines (`\n\n`). If they don't, the plugin highlights the row yellow as "Non-aligned". All read/write tools report:

```json
"alignment": {
  "applicable": true,
  "aligned": false,
  "original_blocks": 17,
  "translation_blocks": 16
}
```

So an agent can translate, write, and immediately see whether the row would go yellow — and fix it before you ever open Calibre.

## Recommended workflow

1. `list_books` → pick the right `book_id` (watch `duplicate_title`)
2. `get_book_info` → note merge settings and existing non-aligned chunks
3. `list_chunks` with `status="untranslated"` → pick a chunk
4. `get_original` → translate it in the chat (keep the block count if merging is on)
5. `write_chunk` → check `alignment.aligned` in the response
6. Misaligned? Rewrite the chunk (or `delete_translations` first) until aligned
7. Repeat until `list_chunks` with `status="misaligned"` comes back empty
8. Human step: reopen advanced mode in Calibre with the same engine/language/merge settings, review, then click **Output**

## Important behaviors

- **Output uses the cache as-is**: the plugin's Output step reads whatever translations are in the cache; externally written ones are picked up directly.
- **No live refresh**: the advanced-mode table loads once at open. Writes made while it is open are not shown until you reopen it. Also, clicking Save in the UI while the window is open can overwrite externally written rows with stale in-memory data — close the window while an agent is writing in bulk.
- **Settings define the cache file**: the cache file name is a hash of book + engine + target language + merge setting. Changing any of these mid-project points at a different cache file.
- **Temporary caches die with the window**: if caching is disabled in plugin settings, the cache exists only while the advanced-mode window is open.

### Timeouts: "MCP error -32001" but the write actually succeeded

If a write collides with a write lock held by the Calibre UI (any Save/ignore action), the server waits silently — up to ~23s (4 attempts x 5s busy timeout + backoff) — and sends the client **nothing** meanwhile. Clients whose tool-call timeout is shorter will report `-32001: Request timed out` first, while the server still completes the write after the lock is released. Two defenses:

1. Raise the client's tool-call timeout to 60s or more (dsh: `toolCallTimeoutMs: 120000`, already set in [`dsh/register.yml`](dsh/register.yml)).
2. If you ever see -32001: **do not blind-retry the write**. Call `get_translation` first to check whether the translation actually landed; rewrite only if it is missing.

Every lock wait is logged to the server's stderr for diagnosis.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `list_books` finds no caches | Caching is not enabled in plugin settings (or the advanced-mode window was closed and its temp cache vanished). Segment a book in advanced mode first; or point `EBOOK_TRANSLATOR_CACHE_DIR` at your cache root |
| Writes land on the wrong book | Same title, multiple caches. Re-check `engine` / `target_lang` / `merge_length` in `list_books` and stick to one id |
| Windows: a black console window flashes and the server never starts | The `python` on PATH is the Microsoft Store stub. Install Python from python.org and use its absolute path in the client config |
| `MCP error -32001` on `write_chunk` | See the [timeouts section](#timeouts-mcp-error--32001-but-the-write-actually-succeeded) — the write probably succeeded; verify with `get_translation` |
| Rows show yellow in the plugin | Misaligned chunk: the translation's blank-line block count differs from the original's. Have the agent rewrite that chunk with matching block count |

## Environment variables

| Variable | Meaning | Default |
|---|---|---|
| `EBOOK_TRANSLATOR_CACHE_DIR` | Explicit cache root (highest priority; then the plugin's configured `cache_path`, then the platform default) | auto-detect |
| `EBOOK_TRANSLATOR_ENGINE_NAME` | Engine name recorded next to written translations | `MCP` |
| `EBOOK_TRANSLATOR_SEPARATOR` | Separator used by the alignment check | `\n\n` |

## Related projects

- [bookfere/Ebook-Translator-Calibre-Plugin](https://github.com/bookfere/Ebook-Translator-Calibre-Plugin) — the plugin this server talks to (not affiliated; this is an independent third-party tool)

## License

[MIT](LICENSE)
