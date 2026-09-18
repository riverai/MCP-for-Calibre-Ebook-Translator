#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ebook-Translator MCP Server（chunk 版）
==========================================

让任意 MCP 客户端（dsh / Claude Code / Cursor 等）直接读写 Calibre
「Ebook Translator」插件（bookfere/Ebook-Translator-Calibre-Plugin，
v2.4.2 验证）的翻译缓存，把"分段提取"留给插件、把"翻译本身"交给
对话窗口里的 agent，替代手工复制粘贴。

核心概念（与插件高级模式界面一一对应）
------------------------------------
* 一个 **chunk** = 界面左列表格里的一个编号行 = 缓存表 cache 里的一行
  （合并翻译开启时由 merge_length 上限聚合而成，实测每块约 6000 字符）。
* **chunk_id 即数据库缓存行的 cache.id**——与插件自身寻址方式一致
  （lib/cache.py 的 get/update/delete 均按 id 操作），创建后永不变更。
  在界面里删行只是把该行标记 ignored，其余 chunk 的 id 不受任何
  影响：agent 已拿到的编号永不失效；任何 chunk 出问题（如不对齐）
  只需 delete_translations + write_chunk 重做**这一个** chunk，
  绝不牵连、不重排其他 chunk。对数据库的最小操作单位就是 chunk。
* 界面左列编号是显示位置（未删行时与 chunk_id 一致；删行后会前移）。
  工具返回中的 ui_row 即该显示位置，仅供人工对照界面定位行，
  不可用于寻址。
* 界面"校对"面板显示的 original / translation 就是本工具
  get_original / get_translation 返回的原文 / 译文全文——agent 拿到
  的与人类看到的内容相同，便于运行中人工核对干预。
* **对齐**：开启合并翻译时（info.merge_length > 0），译文与原文按
  空行 "\\n\\n" 切分后的块数必须一致，否则界面会以**黄色高亮**提示
  该行（Non-aligned items）。本工具完全复刻该判定，读/写时都报告
  aligned 状态，可在写回前就知道会不会触发黄色警告。

缓存文件
--------
    <缓存根目录>/cache/<uid>.db   持久缓存（插件设置启用缓存后生成）
    <缓存根目录>/temp/<uid>.db    临时缓存（未启用缓存时高级模式窗口
                                  打开期间存在，关闭即销毁）

    Windows:       %LOCALAPPDATA%\\calibre-cache\\plugins\\ebook-translator
    macOS / Linux: $TMPDIR/com.bookfere.Calibre.EbookTranslator

表结构（与插件 lib/cache.py 一致，已用真实缓存验证）：

    cache(id, md5, raw, original, ignored, attributes, page,
          translation, engine_name, target_lang)
    info(key, value)  -- title / engine_name / target_lang /
                        merge_length / plugin_version ...

重要行为（源自插件源码 + 实测）
------------------------------
* 输出电子书（Output）走 cache_only 模式，只读取缓存中已有译文，
  外部写入的译文会被直接采用；
* 高级模式界面打开时一次性载入：外部写入不会实时刷新表格，写完
  须用相同引擎/语言/合并设置重新打开核对；窗口开着时在界面里点
  Save 会用内存旧数据覆盖该行——agent 批量干活期间建议关窗口；
* 缓存文件名 uid 由 书籍路径+引擎+目标语言+merge_length+编码 哈希
  决定，中途改配置会换一个新缓存文件。

多书寻址（每本书一个独立的 .db 文件）
------------------------------------
* list_books 扫描 cache/ 与 temp/ 下全部缓存书：**是的，会展示所有书**，
  书多时可用 keyword 参数按书名/引擎过滤；
* book_id（缓存文件名）是唯一可靠的书标识：**同一本书用不同引擎/目标
  语言/合并设置分段会产生多个缓存文件（书名相同、book_id 不同）**，
  list_books 对重名书打 duplicate_title 标记，须凭 engine/target_lang/
  merge_length 区分后再选定；
* 所有读写工具都必须显式携带 book_id（无隐式"当前书"状态，杜绝串书），
  且**每个响应都回显 book_id 与书名**——调用方核对回显即可确知操作
  落在哪本书上，与预期不符立即停止；
* chunk_id 只在其所属书籍内有效：定位唯一 chunk 需要 (book_id,
  chunk_id) 二元组，绝不能把 A 书的 chunk_id 用于 B 书。

工具一览
--------
    list_books             书籍与进度总览（含未对齐 chunk 数，可按关键词过滤）
    get_book_info          某本书的引擎/语言/合并规则/未对齐编号清单
    list_chunks            轻量状态表（不带全文，可按状态筛选编号）
    get_original           读取一个编号（chunk）的完整原文
    get_translation        读取一个编号（chunk）的完整译文与对齐状态
    write_chunk            写入一个编号（chunk）的译文，报告对齐状态
    delete_translations    清空指定编号的译文（重译用）

安全边界
--------
* 只读写已存在的缓存文件：不建书、不建行、不改表结构、不碰 WAL；
* 写入为短事务（BEGIN IMMEDIATE + busy 重试），与插件界面并存安全；
* book_id 仅接受缓存目录内已存在的文件名（防路径穿越），SQL 全参数化。

环境变量
--------
EBOOK_TRANSLATOR_CACHE_DIR     显式指定缓存根目录（优先级最高；
                               其次读取 Calibre 插件配置 cache_path，
                               最后用平台默认路径）
EBOOK_TRANSLATOR_ENGINE_NAME   写入译文登记的引擎名，默认 "MCP"
EBOOK_TRANSLATOR_SEPARATOR     对齐判定分隔符，默认 "\\n\\n"（与插件
                               内置引擎 separator 一致，一般勿改）

运行方式
--------
    stdio（dsh / Claude Code 等本地客户端）:
        python ebook_translator_mcp.py
    HTTP（远程接入，dsh 的 streamable-http transport）:
        python ebook_translator_mcp.py --transport http --port 8420
    自检（打印探测到的缓存目录与书籍列表）:
        python ebook_translator_mcp.py --selftest

依赖: pip install mcp  （兼容 1.x 与 2.x；或 uv run --with mcp 免安装）
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

try:  # mcp 2.x：FastMCP 更名为 MCPServer
    from mcp.server.mcpserver import MCPServer as _MCPServerBase
    from mcp.server.mcpserver.exceptions import ToolError as _ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _MCPServerBase
    try:
        from mcp.server.fastmcp.exceptions import ToolError as _ToolError
    except ImportError:  # 极旧版本兜底
        class _ToolError(Exception):
            pass

# stdio 模式下 stdout 属于协议信道，日志只能走 stderr
logging.basicConfig(
    stream=sys.stderr, level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s")

_SEPARATOR = os.environ.get("EBOOK_TRANSLATOR_SEPARATOR", "\n\n")
_ENGINE_NAME = os.environ.get("EBOOK_TRANSLATOR_ENGINE_NAME", "MCP")
_BOOK_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
_SEPARATOR_PATTERN = re.compile(_SEPARATOR)

mcp = _MCPServerBase(name="ebook-translator")


# --------------------------------------------------------------------------
# 缓存目录发现
# --------------------------------------------------------------------------

def _calibre_config_candidates() -> list[Path]:
    """Calibre 插件配置文件（ebook_translator.json）的可能位置。"""
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
    """解析缓存根目录：环境变量 > Calibre 插件配置 > 平台默认路径。"""
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
# SQLite 基础设施
# --------------------------------------------------------------------------

class _ExpectedError(_ToolError, ValueError):
    """预期内的业务错误：完整消息会透传给 agent。"""


class BookNotFound(_ExpectedError):
    pass


def _connect(path: Path) -> sqlite3.Connection:
    """打开已存在的缓存库；读工具不写，写工具短事务。"""
    if not path.is_file():
        raise BookNotFound(
            f"缓存数据库不存在：{path}（用 list_books 查看可用书籍）")
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.isolation_level = None  # autocommit；写操作显式 BEGIN IMMEDIATE
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _available_books_summary(limit: int = 12) -> str:
    """供错误消息使用：列出当前可用书籍（id + 书名 + 引擎/进度）。"""
    root = cache_root()
    entries: list[str] = []
    for sub, persistent in (("cache", ""), ("temp", "，临时")):
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
                f"{file.stem}《{info.get('title') or file.stem}》"
                f"（{info.get('engine_name')}→{info.get('target_lang')}，"
                f"进度 {progress['translated']}/{progress['total_chunks']}"
                f"{persistent}）")
    if not entries:
        return "当前缓存目录下没有任何可用书籍"
    shown = entries[:limit]
    more = "" if len(entries) <= limit else f" 等 {len(entries)} 本"
    return "可用书籍：" + "；".join(shown) + more


def _book_path(book_id: str) -> Path:
    if not isinstance(book_id, str) or not _BOOK_ID_PATTERN.match(book_id):
        raise BookNotFound(
            f"无效的 book_id：{book_id!r}（应使用 list_books 返回的 id）")
    root = cache_root()
    for sub in ("cache", "temp"):
        path = root / sub / f"{book_id}.db"
        if path.is_file():
            return path
    raise BookNotFound(
        f"找不到缓存文件 {book_id}.db。{_available_books_summary()}。"
        f"若列表为空或缺少此书：确认插件设置中已启用缓存（或高级模式"
        f"窗口仍打开），且引擎/目标语言/合并设置与分段时一致——缓存"
        f"文件名由这些配置哈希决定。")


def _retry_write(conn: sqlite3.Connection, action) -> None:
    """短事务写入，遇 locked/busy 自动重试，与插件界面并存时安全。

    重要：重试期间（最长约 23s = 4 次尝试 × 5s busy_timeout + 退避）
    **不会向客户端发送任何字节**——若 MCP 客户端的 toolCallTimeoutMs
    小于实际锁等待时长，客户端会先报 -32001 超时，而本进程仍会在
    锁释放后完成写入并返回（表现为"报超时但写入实际成功"）。
    因此 dsh 侧务必配置 toolCallTimeoutMs >= 60000（推荐 120000），
    且每次锁等待都记录到 stderr 日志以便诊断。"""
    started = time.perf_counter()
    for attempt in range(4):
        try:
            conn.execute("BEGIN IMMEDIATE")
            action()
            conn.execute("COMMIT")
            waited = time.perf_counter() - started
            if attempt:
                logging.warning(
                    "写入在锁等待后成功：第 %d 次尝试，累计等待 %.1fs"
                    "（锁可能来自 Calibre 界面的写操作；期间客户端收不到"
                    "任何响应，超时阈值不足会报 -32001 但写入已完成）",
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
                    "数据库被占用（BEGIN IMMEDIATE 第 %d 次失败，已等待 "
                    "%.1fs，最多再试 %d 次）——正在等待 Calibre 释放写锁",
                    attempt + 1, time.perf_counter() - started,
                    3 - attempt)
                time.sleep(0.5 * (attempt + 1))
                continue
            raise _ExpectedError(
                f"数据库写入失败（可能正被 Calibre 长时间占用，请稍后重试）：{e}")
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
# chunk 模型（与高级模式界面一一对应）
# --------------------------------------------------------------------------

def _block_count(text: str | None) -> int:
    """按分隔符（默认空行）切分后的块数，与界面校对面板的行组一致。"""
    if not text or not text.strip():
        return 0
    return len(_SEPARATOR_PATTERN.split(text.strip()))


def _alignment(original: str | None, translation: str | None,
               merge_enabled: bool) -> dict[str, Any]:
    """完全复刻插件 Paragraph.is_alignment：
    开启合并翻译时，译文与原文按空行切分的块数必须一致，否则该行
    在界面中以黄色高亮提示（Non-aligned）。"""
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
    """扫描全部未忽略 chunk（与插件 all() 同序，即 rowid 序）。
    chunk_id 用数据库 cache.id（稳定永不变更）；ui_row 为界面左列
    显示位置（0 起，删行后会前移，仅供人工对照）。"""
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
            chunk_id = cid  # 非常规 id 原样保留
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
    """表格首列式的单行预览（换行折叠为空格）。"""
    if not text:
        return ""
    collapsed = " ".join(text.split())
    return collapsed[:width] + ("…" if len(collapsed) > width else "")


def _chunk_by_id(scan: dict[str, Any], chunk_id: int) -> dict[str, Any]:
    """按 chunk_id（数据库 cache.id，稳定不变）取 chunk。"""
    if not isinstance(chunk_id, int) or isinstance(chunk_id, bool):
        raise _ExpectedError(
            f"chunk_id 必须是整数（数据库缓存 id，见 list_chunks 返回）："
            f"{chunk_id!r}")
    for chunk in scan["chunks"]:
        if chunk["chunk_id"] == chunk_id:
            return chunk
    raise _ExpectedError(
        f"chunk_id {chunk_id} 不存在。chunk_id 即数据库缓存 id，永不变更"
        f"（可用编号以 list_chunks 为准；若该行已在界面删除/被忽略，"
        f"则不再参与翻译与输出）")


def _progress_payload(scan: dict[str, Any]) -> dict[str, Any]:
    return {
        "total_chunks": scan["total"],
        "translated": scan["translated"],
        "untranslated": scan["untranslated"],
        "non_aligned": len(scan["non_aligned_chunks"]),
    }


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------

@mcp.tool()
def list_books(keyword: str = "") -> list[dict[str, Any]]:
    """列出**所有**翻译缓存书籍及进度（含临时缓存）。返回的 id 即
    其他工具的 book_id。注意：同一本书用不同引擎/目标语言/合并设置
    分段会产生多个缓存（书名相同、id 不同）——重名书会带
    duplicate_title=true 标记，须凭 engine/target_lang/merge_length
    选定其一；选错书所有后续读写都会落在错误的缓存上，务必先确认。
    keyword 按书名/引擎/语言模糊过滤（书很多时用）。
    persistent=false 表示临时缓存（高级模式窗口关闭即销毁）。"""
    root = cache_root()
    if not root.exists():
        raise BookNotFound(
            f"缓存根目录不存在：{root}。请先在 Calibre 插件中启用缓存并用"
            f"高级模式分段，或设置环境变量 EBOOK_TRANSLATOR_CACHE_DIR。")
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
                continue  # 非有效数据库（残留文件），跳过
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
            f"在 {root} 下未找到任何缓存。常见原因：插件设置里缓存未启用；"
            f"或未启用缓存时高级模式窗口已关闭（临时缓存已销毁）。")
    # 重名书标记：同一书名对应多个缓存（不同引擎/语言/合并设置）
    title_counts: dict[str, int] = {}
    for entry in books:
        title_counts[entry["title"]] = title_counts.get(entry["title"], 0) + 1
    for entry in books:
        if title_counts[entry["title"]] > 1:
            entry["duplicate_title"] = True
            entry["disambiguation_note"] = (
                "书名重复：同一书存在多个缓存，请凭 engine/target_lang/"
                "merge_length/persistent 确认后选定其一，全流程只用同一 id")
    return books


@mcp.tool()
def get_book_info(book_id: str) -> dict[str, Any]:
    """查看某本书缓存的详情：引擎、目标语言、合并/对齐规则、进度，
    以及未对齐（界面黄色高亮）的编号清单。"""
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
                f"译文与原文按空行（{_SEPARATOR!r}）切分后的块数必须一致，"
                f"否则该编号在插件界面中黄色高亮（Non-aligned）"
                if merge_enabled else
                "合并翻译未启用，界面不做对齐校验"),
            "non_aligned_chunks": scan["non_aligned_chunks"],
            "identity_note": (
                "chunk_id 即数据库缓存 id，永不变更：界面删行不影响任何"
                "chunk 的编号，出问题只需重做单个 chunk（delete_translations"
                " + write_chunk）；界面左列显示位置见各工具返回的 ui_row，"
                "仅供人工对照。缓存文件由 书籍+引擎+目标语言+合并设置 唯一"
                "决定，重新打开高级模式须用相同配置"),
            "output_hint": (
                "全部译完后：在 Calibre 中以相同引擎/语言/合并设置重新打开"
                "高级模式（表格会加载已写入的译文），核对后点 Output 输出"),
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
    """轻量列出某本书全部编号（chunk）的状态，不含全文（避免撑爆
    上下文），形态对应界面左列表格。status 可选：all / untranslated /
    translated / misaligned（界面黄色高亮行）；keyword 模糊匹配原文
    或译文。每条含：chunk_id（寻址用，永不变更，仅本书内有效）、
    ui_row（界面左列显示位置，仅供人工对照）、状态、是否对齐、块数、
    字符数、单行预览。所有工具一律以 book_id+chunk_id 寻址，不要用
    ui_row，也不要把本书的 chunk_id 用于其他书。返回外层回显
    book_id 与书名，供调用方核对是否在操作预期的书。"""
    if status not in ("all", "untranslated", "translated", "misaligned"):
        raise _ExpectedError(
            "status 只能是 all / untranslated / translated / misaligned")
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
                # 黄色高亮只作用于已译且块数不一致的行
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
    """读取一个编号（chunk）的完整原文——与界面"校对"面板显示的
    原文一致，agent 拿到的与人类看到的相同。chunk_id 为该书数据库
    缓存 id（永不变更，见 list_chunks；仅在 book_id 这本书内有效，
    勿跨书使用）。合并翻译开启时附带 blocks：译文的空行块数必须与
    该数一致才能对齐（否则界面黄色高亮）。include_raw=True 时额外
    返回原始 HTML。响应回显 book_id 与书名，请核对是否为目标书。"""
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
            # 与界面校对面板一致：显示 strip 后的原文
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
    """读取一个编号（chunk）的完整译文与对齐状态——与界面"校对"
    面板显示的译文一致。chunk_id 为该书数据库缓存 id（永不变更，
    见 list_chunks；仅在 book_id 这本书内有效，勿跨书使用）。
    未翻译时 translation 为 null。合并翻译开启时附带对齐判定
    （译文与原文的空行块数是否一致，即界面是否黄色高亮）。
    响应回显 book_id 与书名，请核对是否为目标书。"""
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
            # 与界面校对面板一致：显示 strip 后的译文
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
    """写入一个编号（chunk）的完整译文（写前自动 strip，与插件行为
    一致；登记引擎名与目标语言）。chunk_id 为该书数据库缓存 id
    （永不变更，见 list_chunks；仅在 book_id 这本书内有效）。
    写回后立即复刻界面对齐判定并返回 aligned 状态——若块数不一致
    会带 warning（该编号将在界面中黄色高亮）。overwrite=False 时
    该编号已有译文则跳过。响应回显 book_id 与书名：写入是不可逆
    操作，请先核对是否为目标书。"""
    if not isinstance(translation, str) or not translation.strip():
        raise _ExpectedError(
            "译文不能为空；清除译文请用 delete_translations")
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

        # 写后重新扫描，返回与界面一致的对齐状态与最新进度
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
                f"对齐警告：原文 {alignment['original_blocks']} 块 / "
                f"译文 {alignment['translation_blocks']} 块（按空行切分须"
                f"一致，否则该编号在插件界面中黄色高亮 Non-aligned）")
        return result
    finally:
        conn.close()


@mcp.tool()
def delete_translations(
    book_id: str,
    chunk_ids: list[int],
) -> dict[str, Any]:
    """清空指定编号（chunk）的译文（用于重译），原文与其他字段不受
    影响。chunk_ids 为该书数据库缓存 id 列表（永不变更，见
    list_chunks；仅在 book_id 这本书内有效）。某个 chunk 出问题只需
    重做它自己，不影响其他 chunk。响应回显 book_id 与书名，请核对。"""
    if not chunk_ids:
        raise _ExpectedError("chunk_ids 列表不能为空")
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
# 入口
# --------------------------------------------------------------------------

def _selftest() -> None:
    root = cache_root()
    print(f"缓存根目录: {root}")
    try:
        for book in list_books():
            persistent = "持久" if book["persistent"] else "临时"
            print(f"  [{persistent}] {book['id']}  《{book['title']}》"
                  f"  引擎={book['engine']}  目标语言={book['target_lang']}"
                  f"  进度={book['translated']}/{book['total_chunks']} chunk"
                  f"  未对齐={book['non_aligned']}")
        print("自检通过：缓存目录可访问，以上书籍可被 MCP 工具读写。")
    except BookNotFound as e:
        print(f"提示: {e}")
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
