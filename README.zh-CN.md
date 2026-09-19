# MCP for Calibre Ebook Translator

[English](README.md) | [简体中文](README.zh-CN.md)

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![插件 v2.4.2 已验证](https://img.shields.io/badge/plugin-v2.4.2%20verified-brightgreen)
![许可证 MIT](https://img.shields.io/badge/license-MIT-green)

一个 [MCP](https://modelcontextprotocol.io) 服务器:让任意 AI agent(Claude Desktop、Claude Code、Cursor、Cline、dsh……)直接读写 Calibre [Ebook Translator](https://github.com/bookfere/Ebook-Translator-Calibre-Plugin) 插件的翻译缓存——按 chunk 逐块读写,替代手工复制粘贴。

插件继续做它擅长的事(解析电子书、把文本切分成编号块、合并输出),翻译本身交给对话窗口里的 agent,模型随你挑。书全程不离开你的电脑:一切都通过本地 SQLite 缓存文件完成。

![插件高级模式:左列表格的每个编号行就是一个可被 MCP 工具读写的 chunk](https://github.com/user-attachments/assets/8577f6b4-fab9-4210-9eb7-5622afab97be)

## 工作原理

Ebook Translator 插件把每本书的翻译进度存放在 SQLite 缓存里。本服务器以读写方式打开这些缓存文件,暴露七个工具:列书、查看编号状态、读取原文、读取/写入译文、清除译文(重译用)。每个响应都回显 book_id 与书名,agent(和你)随时可以核对操作落在哪本书上。

核心特性:

- **以 chunk 寻址**:一个 chunk = 插件高级模式表格里的一个编号行。`chunk_id` 就是数据库行 id——永不变更,哪怕你在界面里删了其他行。
- **对齐感知**:插件开启合并翻译时,译文按空行切分的块数必须与原文一致,否则界面将该行黄色高亮。本服务器完整复刻该判定,读写都报告 `aligned`——不用重开插件就知道某行会不会变黄。
- **构造上安全**:只碰已存在的缓存文件;写入为带重试的短事务,Calibre 开着也安全;SQL 全参数化,`book_id` 无法逃出缓存目录。

## 前置要求

- [Calibre](https://calibre-ebook.com) + [Ebook Translator 插件](https://github.com/bookfere/Ebook-Translator-Calibre-Plugin)(按 v2.4.2 验证),且已用**高级模式**给某本书分段(启用了缓存,或高级模式窗口仍开着)
- Python 3.10+(建议从 [python.org](https://www.python.org/downloads/) 安装;Windows 上避开 Microsoft Store 版,见[故障排查](#故障排查))
- 任意支持 stdio 服务器的 MCP 客户端

## 安装

**方式 A——用 [uv](https://docs.astral.sh/uv/) 直接运行(免安装、免克隆):**

```bash
uvx --from git+https://github.com/riverai/MCP-for-Calibre-Ebook-Translator ebook-translator-mcp --selftest
```

**方式 B——手动:**

```bash
pip install mcp
# 下载 ebook_translator_mcp.py,然后:
python /path/to/ebook_translator_mcp.py --selftest
```

自检会打印探测到的缓存目录和它能看到的每一本书。书都在,服务器就能用。

## 接入 MCP 客户端

服务器名字随意,关键是 `command`/`args`。

**Claude Desktop**(`claude_desktop_config.json`):

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

或者用本地副本 + `pip install mcp`:

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
# 或:claude mcp add ebook-translator -- python /绝对路径/ebook_translator_mcp.py
```

**Cursor**(`.cursor/mcp.json` 或 `~/.cursor/mcp.json`)和 **Cline**(`cline_mcp_settings.json`):用与 Claude Desktop 相同的 `mcpServers` JSON。

**dsh**:见 [`dsh/register.yml`](dsh/register.yml)——其中 `toolCallTimeoutMs` 的建议务必留意。

**远程部署(Calibre 在另一台机器)**:在那台机器上启动 HTTP 模式,任意 streamable-http 客户端接入:

```bash
python ebook_translator_mcp.py --transport http --port 8420
# 端点:http://<那台机器>:8420/mcp
```

## 工具一览

| 工具 | 作用 |
|---|---|
| `list_books` | 列出所有缓存书:引擎、语言、进度、未对齐数;`keyword` 按书名/引擎过滤 |
| `get_book_info` | 某本书的引擎、目标语言、合并/对齐规则、进度,以及未对齐编号清单 |
| `list_chunks` | 轻量编号状态表(不含全文);按 `status`(`all`/`untranslated`/`translated`/`misaligned`)或 `keyword` 筛选 |
| `get_original` | 读取一个编号的完整原文——与插件校对面板显示的完全一致 |
| `get_translation` | 读取一个编号的完整译文与对齐判定(`yellow_warning` = 界面会高亮) |
| `write_chunk` | 写入一个编号的译文;返回对齐状态,块数不一致时附带 `warning` |
| `delete_translations` | 清空指定编号的译文(重译用);原文不受影响 |

## 寻址模型

- `book_id` 是缓存文件名,来自 `list_books`。每次调用都回显 `book_id` + 书名——请核对回显。
- 同一本书换引擎/目标语言/合并设置分段会产生**独立缓存**(书名相同、id 不同)。重名书带 `duplicate_title` 标记,凭 `engine`/`target_lang`/`merge_length` 区分。
- `chunk_id` 是数据库行 id:永不变更,但仅在本本书内有效。某个 chunk 出问题绝不牵连其他编号——只重做它自己(`delete_translations` + `write_chunk`)。
- `ui_row` 是该行在插件表格里的显示位置(给人看的,用于在界面上定位)。绝不可以用它寻址。

## 对齐(黄色高亮)

开启合并翻译时,原文与译文按空行(`\n\n`)切分出的块数必须相等,否则插件把该行标黄(Non-aligned)。所有读写工具都报告:

```json
"alignment": {
  "applicable": true,
  "aligned": false,
  "original_blocks": 17,
  "translation_blocks": 16
}
```

agent 翻译、写回后立刻就能看到该行会不会变黄——在你打开 Calibre 之前就把它修好。

## 推荐工作流

1. `list_books` → 选对 `book_id`(注意 `duplicate_title`)
2. `get_book_info` → 记下合并设置和已有的未对齐编号
3. `list_chunks` 用 `status="untranslated"` → 挑一个编号
4. `get_original` → 在对话里翻译(开合并时保持块数一致)
5. `write_chunk` → 检查响应里的 `alignment.aligned`
6. 不对齐?重写该编号(或先 `delete_translations`)直到对齐
7. 重复,直到 `list_chunks` 的 `status="misaligned"` 返回空
8. 人工步骤:在 Calibre 用相同引擎/语言/合并设置重开高级模式,核对后点 **Output**

## 重要行为

- **输出按缓存原样采用**:插件的 Output 只读缓存里已有的译文,外部写入的会被直接采用。
- **不实时刷新**:高级模式表格在打开时一次性载入。窗口开着时写入的内容要重开才会显示。而且窗口开着时在界面里点 Save,可能用内存里的旧数据覆盖外部写入的行——agent 批量写入期间请关掉窗口。
- **配置决定缓存文件**:缓存文件名是 书籍+引擎+目标语言+合并设置 的哈希。中途改任何一项,都会指向另一个缓存文件。
- **临时缓存随窗口销毁**:插件设置里没启用缓存的话,缓存只在高级模式窗口打开期间存在。

### 超时:报"MCP error -32001"但写入实际成功了

如果写入撞上 Calibre 界面持有的写锁(任何 Save/忽略操作),服务器会静默等待——最长约 23 秒(4 次尝试 × 5s busy 超时 + 退避)——期间**一个字节都不发给客户端**。客户端的工具调用超时若短于此,会先报 `-32001: Request timed out`,而服务器在锁释放后仍会完成写入。两层防御:

1. 把客户端的工具调用超时调到 60 秒以上(dsh:`toolCallTimeoutMs: 120000`,[`dsh/register.yml`](dsh/register.yml) 已配置)。
2. 万一看到 -32001:**不要盲目重写**。先调 `get_translation` 看译文是否实际已写入,缺了再重写。

每次锁等待都会记录到服务器的 stderr,便于诊断。

## 故障排查

| 症状 | 原因与办法 |
|---|---|
| `list_books` 找不到任何缓存 | 插件设置里缓存未启用(或高级模式窗口已关、临时缓存已销毁)。先用高级模式给一本书分段;或把 `EBOOK_TRANSLATOR_CACHE_DIR` 指到你的缓存根目录 |
| 写入落在错误的书上 | 同书名多个缓存。回 `list_books` 核对 `engine`/`target_lang`/`merge_length`,全程只用一个 id |
| Windows:闪一下黑色控制台窗口,服务器起不来 | PATH 里的 `python` 是 Microsoft Store 的占位程序。从 python.org 安装 Python,并在客户端配置里用其绝对路径 |
| `write_chunk` 报 `MCP error -32001` | 见[超时一节](#超时报mcp-error--32001但写入实际成功了)——写入多半已成功,先用 `get_translation` 复核 |
| 插件里行是黄色的 | 该编号不对齐:译文按空行切分的块数与原文不一致。让 agent 按相同块数重写该编号 |

## 环境变量

| 变量 | 含义 | 默认值 |
|---|---|---|
| `EBOOK_TRANSLATOR_CACHE_DIR` | 显式指定缓存根目录(优先级最高;其次插件配置的 `cache_path`,最后平台默认路径) | 自动探测 |
| `EBOOK_TRANSLATOR_ENGINE_NAME` | 写入译文时登记的引擎名 | `MCP` |
| `EBOOK_TRANSLATOR_SEPARATOR` | 对齐判定所用分隔符 | `\n\n` |

## 相关项目

- [bookfere/Ebook-Translator-Calibre-Plugin](https://github.com/bookfere/Ebook-Translator-Calibre-Plugin) —— 本服务器所对接的插件(无隶属关系;本仓库是独立的第三方工具)

## 许可证

[MIT](LICENSE)
