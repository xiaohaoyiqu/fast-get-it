---
name: twitter-crawler-project
description: Project-specific guidance for modifying this Python content downloader, including its Tkinter desktop UI, Twitter adapter, local SQLite library, responsive layout, tests, security boundaries, and documentation. Use when changing code, UI behavior, crawler integration, storage, or project documents in this repository.
---

# Twitter Crawler Project

## Project Scope

Treat `software_app/` as the current product implementation. The older
`app_core/`, `adapters/`, `desktop_ui/`, `prototype.py`, and `run_desktop.py`
remain regression references and should not become the new implementation
path unless the task explicitly targets them.

Use these boundaries:

- `run_software.py`: CLI entry point; no command starts the Tkinter window.
- `software_app/app.py`: application context construction.
- `software_app/core/`: adapter contracts, task management, events, settings,
  SQLite storage, and shared models.
- `software_app/adapters/`: thin platform-facing application boundaries. They
  call software-owned crawler services and must not wrap legacy project CLIs.
- `software_app/crawlers/<platform>/`: native crawler implementations owned by
  this software. The legacy `推特爬虫/`, `Pixiv爬虫/`, `JMComic爬虫/`, and
  `谷歌图片搜索工具/` directories are read-only behavioral references.
- `software_app/ui/`: Tkinter UI and preview rendering.
- `data/software_app/`: runtime database, cookies, crawler state, history,
  indexes, and cached profile data. Treat these files as runtime data, not
  source fixtures.

Read `文档/开发与架构说明.md` and `文档/功能路线图.md` before making a structural
change. Use `文档/设计/平台接入方案.md` for adapter and storage decisions.

## UI Rules

Keep the desktop UI information-dense and tool-oriented. Prefer the existing
Tkinter/`ttk` patterns and `grid` layout over introducing a new UI framework.

- Keep the exact user-facing label `下载类型`; its value uses comma-separated
  media codes such as `1,2,3,4`.
- Every expandable region must use `rowconfigure`/`columnconfigure` weights
  and `sticky="nsew"` or `sticky="ew"` so content receives the available
  space.
- Add vertical and horizontal scrolling to tables, lists, and text areas when
  content can exceed the viewport. Do not rely on window scaling to make
  content visible.
- Use wrapping labels whose wrap length follows the current widget width.
  Avoid fixed widths for descriptions, URLs, and other variable-length text.
- Keep stable minimum and default window sizes. Verify at least the configured
  minimum (`1080x680`), the default (`1240x780`), and a wider desktop window.
- Preserve image references for `PhotoImage` objects. Cancel scheduled
  `after()` callbacks before destroying the window and guard callbacks against
  destroyed widgets.
- Keep empty, loading, error, and unavailable-preview states visible and
  actionable. Do not leave a blank panel when a folder has no previewable
  image.

The 下载库 has three intentional information levels:

1. Top: platform, author, and folder summary plus folder actions.
2. Middle: the selected folder's file list and metadata such as type, name,
   and size.
3. Bottom: local image/file preview and actions for opening the selected file
   or folder.

Keep these responsibilities distinct. Do not hide the middle file list or
bottom preview merely because the window is resized; use scrolling and
responsive weights instead.

## Change Workflow

1. Inspect the relevant documentation and neighboring code before editing.
2. For behavior or bug fixes, write or identify a focused regression test
   before changing the implementation where the test framework supports it.
3. Keep changes within the owning module. Reuse existing models, callbacks,
   storage helpers, and adapter interfaces instead of adding parallel paths.
4. For UI work, exercise the real window and representative small/large
   sizes. Check that labels, buttons, tables, logs, previews, and scrollbars
   remain reachable.
5. Review the diff for correctness, readability, architecture, security, and
   performance before declaring the work complete.

## Context and Token Efficiency

Keep the working context focused. Prefer a small amount of verified project
context over loading the whole repository.

- Start with `rg --files`, headings, symbol searches, and line counts. Read
  only the source and document sections relevant to the current task.
- For a document audit, inspect the table of contents or headings first, then
  read the required sections. Do not repeatedly load unchanged full documents.
- Before editing, read the target file, its nearby tests, one similar existing
  implementation, and the interfaces it uses. Avoid scanning unrelated
  platform folders.
- Use bounded command output such as `rg -n`, `Select-Object -First`, focused
  test filters, and summarized logs. Never paste or retain an entire large
  build/test log when the failure lines are sufficient.
- Maintain a short working summary of changed files, verified commands, open
  issues, and important decisions. Reuse that summary when switching between
  phases instead of rereading the same files.
- Run independent reads or checks in parallel. Do not rerun an unchanged test
  command unless the code or test input changed.
- Do not add subagents, MCP servers, or token-compression dependencies for a
  routine edit. Use them only when the task is large enough that the extra
  context produces clear value.
- If installed for development, `E:\aimodel\headroom` or similar compression
  tooling may be used to reduce very large logs or reports. Keep such tooling
  outside the application's runtime dependencies and fall back to bounded
  native commands when it is unavailable.
- Keep progress updates and final reports concise: state actions, evidence,
  blockers, and next steps without repeating the full investigation.

## Crawler and Data Safety

- Treat usernames, URLs, crawler output, JSON, cookies, and downloaded files
  as untrusted external data.
- Never commit or print cookies, API keys, tokens, or credential contents.
  Load secrets from protected runtime files or environment configuration.
- Use `pathlib.Path` for filesystem paths and validate output locations before
  writing.
- Avoid shell interpolation and `shell=True` for user-controlled values.
- Prefer in-process software-owned crawler services. A subprocess is only for
  an unavoidable external executable or an explicitly isolated compatibility
  path, never the default platform architecture.
- Keep SQLite writes and task events consistent with the shared core models.
  A UI change must not bypass task manager or storage callbacks.
- Do not delete downloaded media when clearing indexes or crawler records
  unless the user explicitly requests media deletion.

## Verification

Run the narrowest useful checks first, then the broader checks that apply:

```powershell
python -m py_compile software_app/ui/tk_app.py
python -m compileall software_app
python .\run_software.py modules
python .\run_software.py preview twitter '@example' -t 1,2
```

For a desktop UI change, also run:

```powershell
python .\run_software.py
```

Manually verify the window at `1080x680`, `1240x780`, and a wider size. If
tests are added or discovered, run the repository's focused test and full test
commands; do not invent a default test command when no test configuration is
present. Real crawling requires valid runtime cookies, a compatible Chrome
and ChromeDriver, and network access, so keep it separate from offline
smoke tests.

## Documentation

Keep documentation aligned with behavior:

- Update `文档/开发与架构说明.md` for current user-visible capabilities and layout.
- Update `文档/测试与开发记录.md` with new checks, known limits, and next steps.
- Update `文档/设计/平台接入方案.md` when adapter, storage, or integration boundaries change.
- Update `文档/设计/界面设计记录.md` when UI design references or rendering strategy
  changes.
- Treat `文档/历史资料/第一版说明.md` and older `prototype.py`/`run_desktop.py` notes as
  historical unless the task explicitly concerns the legacy prototype.

Document the reason and trade-off for architectural decisions. Avoid comments
that merely restate obvious code.

## Review Checklist

- Does the change preserve the `software_app/` architecture and adapter
  contracts?
- Can all variable-length content still be reached at the minimum window size?
- Are empty, error, cancel, and unavailable-resource paths handled?
- Are external data and credentials kept out of logs and source?
- Is there a focused regression test or a documented manual verification?
- Are affected project documents updated without claiming planned features are
  already implemented?
