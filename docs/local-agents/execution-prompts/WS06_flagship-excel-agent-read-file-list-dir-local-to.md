<!-- WS06 · Phase 2 · depends_on: WS2, WS5 -->
# WS06 — Flagship Excel agent: read_file/list_dir LOCAL tools + drive_excel in-place editor

> **Phase 2** · **Depends on:** WS2, WS5 · **Key files:** agent_team/deliverables.py, agent_team/file_tools.py, agent_team/config.py (data_analyst dict at line 957), tests/test_deliverables.py

> **LOCKED DECISION (2026-06-15) — write-approval UX (tiered, default-safe).** `write_file` / `create_*` (NEW files) → write into an `outputs/` subdir of the user-approved folder, no prompt. `drive_excel` (IN-PLACE edit of the user's real workbook) → requires **per-session folder approval** and writes to a `*_edited.xlsx` **copy by default**; overwriting the original is an explicit per-run opt-in flag, never the default. `encrypt_outputs` MUST be **OFF** for these user-owned dirs (encrypting in place would corrupt their working files — do NOT port the Fernet-at-rest server invariant here). All paths still pass through the WS2 `assert_in_workspace` allowlist before any `open()`.

---

# Execution Prompt — LOCAL file tools (list_dir / read_file / write_file) + `drive_excel` in-place Excel editor + finalize the `data_analyst` flagship manifest

You are a coding agent working in the repo at `/Users/brucetang/Company_Agent_Demo` (a Python/Flask multi-agent app, brand "Tangent AI"). You have NO memory of any prior planning conversation; everything you need is below. Work on the current branch (`feat/fusion-landing`); do not create new branches, do not commit or push unless explicitly asked.

## 1. Objective

The product is gaining a desktop/local form factor: the agent team will run on the *user's own machine* against files that live on their disk (not uploaded blobs). To make the flagship Data Analyst agent ("Mei" / `data_analyst`) work end-to-end on local files, add three things to the **deliverables tool layer**:

1. A new family of **LOCAL filesystem tools** — `list_dir`, `read_file`, `write_file` — exposed in the same Anthropic-tool `input_schema` shape the existing `deliverables.TOOL_SPECS` use, so an agent that lists these names in its `cfg['tools']` can call them. `read_file` wraps `file_tools.parse_file` **verbatim** (no re-implementation of any parser). These tools are confined to an approved root and are default-deny.
2. A new deliverable builder **`drive_excel`** — an *in-place editor* that loads an existing `.xlsx` with openpyxl, mutates specific cells/formulas, and saves it back. This is **distinct** from the already-existing `create_excel_workbook` builder, which emits a *brand-new* file. `drive_excel` writes into a user directory and must NOT encrypt the output (the user needs to open it directly in Excel).
3. Finalize the **`data_analyst` agent manifest + system prompt** so it understands: "the user's files live on their machine — use `list_dir`/`read_file` to find and read them, analyze, then write results back with `create_excel_workbook` (new file) or `drive_excel` (edit in place)."

This is **Phase 2** of the local-form-factor effort. It depends on two upstream workstreams that may or may not already be merged — **read §3 carefully**, because you must handle BOTH the case where they exist and the case where they don't, without breaking the existing test suite.

## 2. Read these first (exact files / functions, in this order)

Read and understand these BEFORE writing any code. The patterns here are the contract you must follow exactly.

- **`agent_team/deliverables.py`** — this is the file you will mainly edit. Study:
  - `TOOL_SPECS` (list of dicts, each `{name, description, input_schema}` in Anthropic tool shape). Note the *already-present* `create_excel_workbook` spec (around the end of the list) — **it already exists in the working tree; do NOT rebuild or duplicate it.**
  - `TOOL_NAMES = {spec["name"] for spec in TOOL_SPECS}`
  - `specs_for(names)` — returns the `TOOL_SPECS` subset for a set of names, preserving declaration order.
  - `run_tool(name, tool_input, out_dir)` — the executor. It looks up `_BUILDERS[name]` → `(ext, kind, builder)`, computes a safe unique filename under `out_dir`, calls `builder(tool_input, path)`, then **`crypto.encrypt_file_in_place(path)`**, and returns a result dict `{ok, message, kind, ext, title, filename, path}`. **Never raises** — it catches and returns `{ok: False, message: ...}`.
  - `_BUILDERS` dict (name → `(ext, kind, builder_fn)`), including the existing `"create_excel_workbook": ("xlsx", "Excel workbook", _build_xlsx)`.
  - `_build_xlsx(data, path)` (already exists — openpyxl `Workbook()`, one sheet per `data["sheets"]`, bold header row, `_xlsx_cell` to keep numbers numeric, `_safe_sheet_title` to sanitize tab names). **Reuse its helpers (`_xlsx_cell`, `_safe_sheet_title`) — do not copy them.** Do NOT modify `_build_xlsx` itself.
  - Helpers `_safe_filename`, `_unique`, `_xml_escape`.
- **`agent_team/file_tools.py`** — `parse_file(path)` returns `{name, ext, chars, text, meta}`, reading xlsx/xls/csv (pandas — shape, columns, numeric `describe()`, 30-row preview), docx (python-docx incl. tables), pdf (pypdf), json, txt/md, with a 60k-char cap and a visible truncation notice. **This is the canonical local-file reader. `read_file` must call it verbatim — do not re-implement any parsing.**
- **`agent_team/base_agent.py`** lines ~34–110 — `Agent.run` and `Agent._run_with_tools`. Note exactly how tools are dispatched: `tool_names = cfg.get("tools")`, `web_search` is split out, the remainder is passed to `deliverables.specs_for(...)`, and an `executor(name, tool_input)` closure calls `deliverables.run_tool(name, tool_input, out_dir)` where `out_dir = self.memory.output_dir()`. Your new tools must be callable through this SAME `run_tool` path with no change to `base_agent` required for the happy path.
- **`agent_team/crypto.py`** — `encrypt_file_in_place(path)` (no-op when `FERNET_KEYS` unset), `read_plaintext(path)`, `materialize_plaintext(path)` (context manager yielding a decrypted temp path; used to feed encrypted files into things that need a real plaintext file). Encryption is OFF by default in dev.
- **`agent_team/config.py`**:
  - Top constants `OPUS = "claude-opus-4-7"`, `SONNET`, `effective_model(agent_id)`, `get_agent(agent_id)`, `COST_BUDGETS_CNY` (~line 865), `AGENTS` list (~line 880).
  - The **`data_analyst`** dict (id `"data_analyst"`, name "Mei", ~line 949). In the working tree its `tools` are already `["web_search", "create_excel_workbook"]` and the system prompt already mentions `create_excel_workbook`. You will EXTEND this.
- **`tests/test_deliverables.py`** — the existing per-builder test file (uses `pytest`, `tmp_path`, loads produced `.xlsx` back via `crypto.read_plaintext` + `openpyxl.load_workbook`). This is the file you will extend with new tests. Follow its style exactly (module-level `ROOT` sys.path shim, `_load(path)` helper that decrypts transparently).

Run the baseline suite once before changing anything so you know it's green:
```
.venv/bin/python -m pytest tests/test_deliverables.py -v
```

## 3. CRITICAL — upstream dependency handling (WS2 workspace guard, WS5 local tool loop)

This workstream is specified to DEPEND ON two others:
- **WS2** = `agent_team/workspace.py` with a default-deny path chokepoint, conventionally `assert_in_workspace(path)` (and/or a function to register approved roots). It canonicalizes a path with `os.path.realpath` + `os.path.commonpath` against user-approved roots and rejects `..`/symlink escapes BEFORE any file is opened.
- **WS5** = a local tool-execution loop that interleaves on-device `deliverables.run_tool` calls with remote model turns.

**These may not be merged yet.** Before writing code, check:
```
ls agent_team/workspace.py 2>/dev/null && echo PRESENT || echo ABSENT
```
- **If `agent_team/workspace.py` is PRESENT**: import and use its real guard function for every path your tools touch (inspect the module to learn the exact function name/signature — likely `assert_in_workspace(path) -> canonical_path` raising on violation, plus an approved-roots registry). Call the guard on EVERY path argument (the directory for `list_dir`, the file for `read_file`/`write_file`/`drive_excel`) BEFORE opening anything.
- **If `agent_team/workspace.py` is ABSENT**: you must NOT block on it. Create a **minimal, self-contained guard inside this workstream** so your tools are still default-deny and the tests pass, structured so it can be trivially swapped for the real WS2 module later. Put it in a small new module `agent_team/workspace.py` with this minimal surface (do not over-build — WS2 owns the full version):
  - `set_approved_roots(roots: list[str])` — store realpath-canonicalized absolute roots in a module global.
  - `approved_roots() -> list[str]` — read them back.
  - `assert_in_workspace(path: str) -> str` — `realpath` the path, and for each approved root verify `os.path.commonpath([root, candidate]) == root`; on success return the canonical path; on failure raise `WorkspaceError` (a new exception subclass of `Exception`). **Default-deny**: if no roots are approved, raise (nothing is allowed until a root is explicitly approved). Reject any path containing a `..` component pre-canonicalization as belt-and-suspenders, and reject symlinks whose realpath escapes the root (the `realpath`+`commonpath` check already covers this).
  - Add a module docstring stating this is the Phase-2 minimal guard and WS2 may replace it.

  Whichever case applies, your `list_dir`/`read_file`/`write_file`/`drive_excel` code must call the guard through the same import (`from . import workspace` then `workspace.assert_in_workspace(...)`), so swapping implementations is a no-op for callers.

**Do NOT build any part of WS5** (no remote router, no `/v1` proxy, no tool loop, no daemon/runner). This workstream stops at: the tool SPECS, the executor entries, the builder/reader functions, the guard wiring, and the manifest. The WS5 loop will call `deliverables.run_tool(...)` exactly as `base_agent` does today.

## 4. Implementation spec

### 4.1 `LOCAL_TOOL_SPECS` in `deliverables.py`

Add a new module-level list `LOCAL_TOOL_SPECS` (a sibling of `TOOL_SPECS`), holding three specs in the same Anthropic `input_schema` shape. Keep descriptions tight and unambiguous (the model reads them). Suggested schemas:

- **`list_dir`** — "List the files and sub-folders inside a directory on the user's machine, so you can find the spreadsheets/CSVs/documents to analyze. Only directories the user approved are accessible."
  - `input_schema`: object, properties: `path` (string, "Absolute path of the directory to list."), required `["path"]`.
- **`read_file`** — "Read ONE file from the user's machine and return its contents as text you can analyze. Handles Excel (.xlsx/.xls), CSV, Word (.docx), PDF, JSON, and plain text. Only files inside an approved folder are readable."
  - `input_schema`: object, properties: `path` (string, "Absolute path of the file to read."), required `["path"]`.
- **`write_file`** — "Write plain text (or CSV/Markdown/JSON content you provide) to a NEW file on the user's machine, inside an approved folder. For spreadsheets prefer create_excel_workbook or drive_excel instead."
  - `input_schema`: object, properties: `path` (string, "Absolute destination path."), `content` (string, "The full text content to write."), required `["path", "content"]`.

Define `LOCAL_TOOL_NAMES = {spec["name"] for spec in LOCAL_TOOL_SPECS}`.

**Wiring decision — make local tools resolvable by `specs_for` and `run_tool` without breaking existing callers:**
- Keep the existing `create_excel_workbook` etc. in `TOOL_SPECS` untouched.
- Update `specs_for(names)` so it can return entries from BOTH `TOOL_SPECS` and `LOCAL_TOOL_SPECS` (search a combined ordered view: file-builder specs first, then local-tool specs), still preserving declaration order and still filtering by the requested name set. This is what lets an agent list `"read_file"` in `cfg['tools']` and have `base_agent` hand the spec to the model. Verify the existing `specs_for` tests still pass.
- `drive_excel` (below) goes into `_BUILDERS` so plain `run_tool` handles it. The three LOCAL fs tools, however, do NOT produce a downloadable deliverable and have different semantics (they take an explicit absolute `path`, not an `out_dir`). Handle them in `run_tool` as a dispatch branch BEFORE the `_BUILDERS` lookup: if `name in LOCAL_TOOL_NAMES`, route to a new `_run_local_tool(name, tool_input)` and return its result dict. Keep `run_tool`'s "never raises, always returns a dict" contract.

### 4.2 `_run_local_tool` executor for the fs tools

Add `def _run_local_tool(name, tool_input)` returning the same-shaped result dict the model loop expects (`{ok: bool, message: str, ...}`; on a successful read include the parsed text). All three are default-deny via the guard.

- **`list_dir`**: guard the dir path → `os.scandir`; return `{ok: True, message: "...", entries: [{name, is_dir, ext, size_bytes}], path}`. Do not recurse. On guard failure return `{ok: False, message: "Access denied: <path> is outside the approved workspace."}`.
- **`read_file`**: guard the file path → then call `file_tools.parse_file(path)` **verbatim**. BUT: if the file may be encrypted-at-rest (these are the user's OWN files on their machine, generally NOT encrypted, but be robust), wrap the read in `crypto.materialize_plaintext(path)` and pass the yielded plaintext path to `parse_file`. Return `{ok: True, name, ext, chars, text, meta, message: "Read <name> (<chars> chars)."}` mirroring `parse_file`'s dict plus the result envelope. Never raise — `parse_file` already swallows per-file errors into a `[Could not read ...]` text, so just propagate.
- **`write_file`**: guard the destination path. Create parent dirs only if they are themselves inside the approved root (re-guard the parent). Write `content` as UTF-8. **Do NOT call `crypto.encrypt_file_in_place`** — this lands in the user's own directory and must be directly openable. Return `{ok: True, path, bytes: <n>, message: "Wrote <path> (<n> bytes)."}`.

`import` `file_tools` lazily inside `_run_local_tool` (e.g. `from . import file_tools`) to match the lazy-import style used elsewhere and keep module import cheap.

### 4.3 `drive_excel` in-place editor builder

This is a **deliverable builder** registered in `_BUILDERS`, but with two behavioral differences from every other builder, so it needs special-casing inside `run_tool` (see below):

**Semantics**: load an EXISTING workbook, apply targeted edits, save it back to the SAME path (in place). Spec:

- Add to `TOOL_SPECS` (so it's a normal tool the model can call) a `drive_excel` spec:
  - description: "Edit an EXISTING Excel (.xlsx) workbook in place on the user's machine: set specific cell values or formulas, add rows, without rebuilding the file. Use this when the user wants their own spreadsheet updated/annotated rather than a brand-new file. Provide the path to the workbook and a list of edits. For a brand-new spreadsheet, use create_excel_workbook instead."
  - `input_schema`: object, properties:
    - `path` (string, "Absolute path of the existing .xlsx to edit."),
    - `edits` (array of objects, each: `sheet` (string, optional — defaults to the active sheet), and ONE of `cell` (string A1 ref, e.g. "B2") with `value` (string|number|boolean|null) OR `formula` (string starting with "=", written into the cell), OR `append_row` (array of cell values appended as a new row at the bottom of the sheet)). Document in the description that a formula string like "=SUM(B2:B10)" is stored as a real Excel formula.
  - required `["path", "edits"]`.

- Implement `def _drive_xlsx(data, path)` where `path` is the IN-PLACE target (already the user's file path, already guarded):
  - `from openpyxl import load_workbook` → `wb = load_workbook(path)`.
  - For each edit: resolve the sheet (`wb[sheet]` if given and present, else `wb.active`; if a named sheet is missing, create it). For `cell`+`value`: `ws[cell] = _xlsx_cell(value)` (reuse the existing helper so numbers stay numeric). For `cell`+`formula`: write the raw formula string (must start with `=`). For `append_row`: `ws.append([_xlsx_cell(v) for v in row])`.
  - `wb.save(path)`.
  - If the file does not exist or is not a valid xlsx, let the exception propagate to `run_tool`'s try/except, which returns `{ok: False, message: ...}`.

- Register it: `_BUILDERS["drive_excel"] = ("xlsx", "Excel edit", _drive_xlsx)` (the `kind` string "Excel edit" distinguishes it in the activity feed from "Excel workbook").

**`run_tool` special-casing for `drive_excel`** (because it edits an existing file rather than emitting a new one into `out_dir`, and must NOT be encrypted):
- In `run_tool`, after the `LOCAL_TOOL_NAMES` branch, add a branch: `if name == "drive_excel"`:
  - guard `tool_input["path"]` via `workspace.assert_in_workspace` (raise→caught→`{ok: False, ...}`),
  - call `_drive_xlsx(tool_input, guarded_path)` directly (NOT through the `out_dir`/`_unique`/`_safe_filename` machinery — the path is fixed and supplied by the model),
  - **skip `crypto.encrypt_file_in_place`** entirely,
  - return `{ok: True, kind: "Excel edit", ext: "xlsx", title: os.path.basename(path), filename: os.path.basename(path), path: guarded_path, message: f"Edited workbook '{...}'."}`.
- The existing `create_excel_workbook` / pptx / pdf / docx / svg path is unchanged and STILL encrypts (those land in the job output dir).

**Guard reach for the encrypted-vs-not decision**: encryption-at-rest stays ON for the report/job output dir (the create_* family); it is OFF for user-directory writes (`write_file`, `drive_excel`). State this rationale in a code comment so a future reader doesn't "helpfully" re-add encryption and lock the user out of their own file.

### 4.4 Finalize the `data_analyst` manifest + system prompt (`config.py`)

In the `data_analyst` dict (id `"data_analyst"`, ~line 949):

- **`tools`**: extend from `["web_search", "create_excel_workbook"]` to include the local fs tools and the in-place editor:
  `["list_dir", "read_file", "web_search", "create_excel_workbook", "drive_excel"]`
  (declaration order is cosmetic; `specs_for` re-sorts by its own internal order). Do NOT add `write_file` to this agent unless you also want it writing arbitrary text files — keep it to the spreadsheet-centric set; `write_file` remains an available tool other agents could opt into.
- **`system_prompt`**: APPEND (do not delete the existing analyst guidance, and DO NOT touch the VERBATIM "UNKNOWN_TERMS" block at the end — the pipeline parses it) a passage that establishes the local-files mental model. It must convey, in plain English:
  "The user's files live on their own machine. Use `list_dir` to discover folders/files and `read_file` to read a spreadsheet/CSV/document into text. Analyze the data, then hand results back EITHER as a brand-new workbook with `create_excel_workbook` OR by editing the user's existing spreadsheet in place with `drive_excel` (set cells, formulas, or append rows). Only files inside folders the user has approved are accessible; if a path is denied, say so and ask the user to grant access rather than guessing another path."
  Insert this new passage BEFORE the VERBATIM `UNKNOWN_TERMS:` instruction (which must remain the very last thing in the prompt, byte-for-byte unchanged).

Do NOT add `model`/provider/¥-rate fields beyond what's there — the manifest's *runner-visible* half carries behavior only; model/pricing resolve server-side (this is a hard invariant — see §5). You are only editing `tools` + `system_prompt` here.

(`media_producer` already has `create_excel_workbook` in the working tree; leave it as-is. Do not add the local fs tools to `media_producer` — it has no business reading the user's whole disk.)

## 5. Invariants you MUST respect

1. **China domestic mode must keep working.** Nothing you add may introduce a hard dependency on Anthropic or on a model key. The tools are pure-Python (openpyxl, pandas via `parse_file`) and run regardless of which provider the cloud resolves. Do NOT call `effective_model()`, `provider_for()`, or any model/key code from the tool layer. (The honest caveat: Anthropic tool-calling degrades to plain text on non-Claude providers — but that's WS5's concern, not yours; the builders execute on-device either way.)
2. **No model keys / no pricing on the customer machine.** The tool SPECS and the manifest carry capability NAMES and behavior text only — never a model id, provider, or per-token ¥ rate. Do not serialize any pricing or model field anywhere in this workstream.
3. **¥ cost-control is untouched.** You add no LLM calls, so `token_manager` / `platform_guard` accounting is unaffected. Do not wire any billing.
4. **Founder-gating unchanged.** You are not touching any admin/`/api` surface in this workstream.
5. **Files-encrypted-at-rest discipline.** The job/output dir (`create_*` family) STAYS encrypted via `crypto.encrypt_file_in_place` (do not remove it). User-directory writes (`write_file`, `drive_excel`) are intentionally NOT encrypted because the user opens them directly — document this asymmetry in a comment.
6. **Default-deny path safety.** Every path the tools touch goes through `workspace.assert_in_workspace` BEFORE any open/scandir/load/save. No approved roots ⇒ everything denied. Reject `..` and symlink escapes. This is the single chokepoint; do not add a second ad-hoc path check that could diverge.
7. **Honesty rule (deliverables.py module docstring).** "Never fake a binary we can't make." `drive_excel` produces a real edited `.xlsx`; `read_file` returns real parsed content. Do not stub. If an edit/read fails, surface the real reason in `message` (the `run_tool` try/except already does this).
8. **`run_tool` never raises.** Preserve the contract: all new branches catch their own exceptions and return a result dict.
9. **Do NOT rebuild `create_excel_workbook` / `_build_xlsx`.** They already exist in the working tree; reuse their helpers, leave them untouched.

## 6. Acceptance criteria + tests

Extend **`tests/test_deliverables.py`** (same file, same style: `tmp_path`, `_load` decrypt-transparent helper). Add a new section of tests. Use a real `tmp_path` directory registered as the approved workspace root (call `workspace.set_approved_roots([str(tmp_path)])` in each test or a fixture; if the real WS2 module uses a different registration API, adapt). All tests must pass via:
```
.venv/bin/python -m pytest tests/test_deliverables.py -v
```
and the FULL suite must stay green:
```
.venv/bin/python -m pytest -q
```

Required tests:

1. **LOCAL_TOOL_SPECS registered & resolvable**: assert `{"list_dir","read_file","write_file"} <= deliverables.LOCAL_TOOL_NAMES`; assert each has a valid `input_schema` with the documented `required` keys; assert `deliverables.specs_for(["read_file","create_excel_workbook"])` returns BOTH (proving `specs_for` now spans both spec lists) and preserves a stable order.
2. **`read_file` wraps `parse_file` verbatim**: write a small real `.xlsx` into `tmp_path` (use the existing `create_excel_workbook` via `run_tool`, then — since that file is in the *output dir* path it'll be encrypted only if FERNET is set; for this test write a plain xlsx directly with openpyxl into `tmp_path` instead so it's a user-style file). Call `run_tool("read_file", {"path": that_path}, out_dir=None)` (or however the signature lands — see note below) and assert the returned `text` contains the column headers / a numeric summary string that `parse_file` produces. Assert it equals what a direct `file_tools.parse_file(path)["text"]` returns (verbatim-wrap proof).
3. **`list_dir`**: create a couple of files + a subdir under `tmp_path`; assert the returned `entries` include them with correct `is_dir` flags.
4. **`write_file`**: write text to `tmp_path/note.txt`; assert `ok`, the file exists on disk as PLAINTEXT (open it directly, no decrypt), and byte count matches.
5. **`drive_excel` edits in place**: create a workbook at `tmp_path/book.xlsx` with openpyxl (sheet "Data", a header row + a couple numeric rows). Call `run_tool("drive_excel", {"path": ..., "edits": [{"sheet":"Data","cell":"C1","value":"Total"}, {"sheet":"Data","cell":"C2","formula":"=A2+B2"}, {"sheet":"Data","append_row":["x", 1, 2]}]}, out_dir=...)`. Re-load with `openpyxl.load_workbook` and assert: `C1 == "Total"`, `C2` holds the formula string `"=A2+B2"`, the appended row exists, the SAME file path was edited (not a `-2` copy), and the file is NOT encrypted (loads without `crypto.read_plaintext`). Assert the result dict has `kind == "Excel edit"` (distinguishing it from `create_excel_workbook`'s `"Excel workbook"`).
6. **`drive_excel` numbers stay numeric**: a `{"cell":"B5","value": 1234}` edit yields `isinstance(ws["B5"].value, int)`.
7. **Default-deny path safety**: with `tmp_path` approved, a `read_file`/`write_file`/`drive_excel`/`list_dir` on a path OUTSIDE `tmp_path` (e.g. `/etc/hosts`, or `tmp_path/../escape`) returns `{ok: False}` with an "access denied"/"outside" message and does NOT open/modify the target. Also assert that with NO approved roots set, every tool denies (default-deny). If you authored the minimal `workspace.py`, also unit-test `assert_in_workspace` directly (accepts in-root, rejects `..`, rejects sibling-prefix tricks like `/approveddir-evil` vs `/approveddir`).
8. **Config wiring**: assert `config.get_agent("data_analyst")["tools"]` is a superset of `{"list_dir","read_file","create_excel_workbook","drive_excel"}`; assert the system prompt mentions both `read_file`/`list_dir` and `drive_excel`; **assert the VERBATIM `UNKNOWN_TERMS:` instruction is still present and is the tail of the prompt** (so you can prove you didn't clobber the pipeline contract).
9. **`create_excel_workbook` regression**: keep the existing tests passing unchanged (they assert `"Excel workbook"`, sheet order, numeric cells, sanitized titles, filename collisions). Do not modify them.

**Signature note**: `run_tool(name, tool_input, out_dir)` currently takes `out_dir` positionally. The local fs tools and `drive_excel` take an absolute `path` from `tool_input` instead of using `out_dir`. Keep `run_tool`'s existing signature (`out_dir` still required positionally so `base_agent` is unchanged); for the local/in-place branches simply ignore `out_dir`. In tests you may pass any value for `out_dir` on those calls (e.g. `str(tmp_path)`).

## 7. Out of scope (do NOT build these here)

- **WS5**: the remote model router / `/v1` proxy / the interleaving local-tool execution loop / the daemon / `agent_team/runner/` / waitress / pystray / pairing / device tokens. You only provide the tool SPECS + executors that WS5 will call.
- **The full WS2 workspace module**: build only the minimal `assert_in_workspace`/`set_approved_roots` guard IF the module is absent (§3). Do not build watched-folder/`watchdog` logic, native file pickers, or an approved-roots UI.
- **Any database / migration work** (no `devices`/`agent_catalog`/`agent_install`/`agent_run` tables, no `0007` migration, no `runs.origin`).
- **Any HTTP endpoint** (`/api/devices*`, `/api/agents/catalog`, `/api/run` path-mode, telemetry sync).
- **Modifying `create_excel_workbook`/`_build_xlsx`** or the other existing builders.
- **Frontend / SPA / `main.js` / nav** changes.
- **`media_producer`** tool changes beyond what's already in the working tree.

## 8. Definition of done

- `agent_team/deliverables.py`: `LOCAL_TOOL_SPECS` + `LOCAL_TOOL_NAMES` added; `specs_for` spans both spec lists; `run_tool` dispatches local fs tools (`_run_local_tool`) and `drive_excel` (in-place, unencrypted) correctly; `_drive_xlsx` builder + `_BUILDERS["drive_excel"]` entry added; all path access goes through `workspace.assert_in_workspace`; `read_file` calls `file_tools.parse_file` verbatim; encryption asymmetry documented in comments.
- `agent_team/workspace.py`: real WS2 module used if present; else minimal default-deny guard authored.
- `agent_team/config.py`: `data_analyst.tools` includes `list_dir/read_file/create_excel_workbook/drive_excel`; system prompt teaches the local-files workflow; `UNKNOWN_TERMS:` tail preserved verbatim.
- `tests/test_deliverables.py`: new tests (§6.1–§6.8) added; §6.9 regressions untouched and green.
- `.venv/bin/python -m pytest -q` is fully green.

Report back the absolute paths you changed and a one-line note per file. Do not write any summary `.md` files.