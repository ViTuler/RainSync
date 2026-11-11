# RainSync

Local CLI for keeping shared folders in sync across multiple projects, and for one-way folder backups.

中文版见 [README_zh.md](README_zh.md)。

**Current version:** `0.2.0` · CLI name: `synctool`

## What it does

| Feature | Behaviour |
| ------- | --------- |
| **Sync groups** | Projects in a group are peers. A change in one is copied to the others. On a full sync, identical content is filled in where missing; differing content is reported as a conflict. |
| **Map groups** | One-way backup: copy `source` → `target`. Only missing or changed files are copied (size / mtime). Extra files in `target` are left alone. |

Typical uses: share a `.tools` / `utils` folder across several local repos, or periodically back up a working tree to another drive.

## Requirements

- Windows (primary; Paths and background mode are written for it)
- Python 3.10+ (when running from source)
- Dependencies: `ruamel.yaml`, `watchdog` (see `requirements.txt`)

## Quick start (from source)

```text
# 1. Clone and install deps (use a non-base conda env, or a venv)
git clone https://github.com/ViTuler/RainSync.git
cd RainSync
pip install -r requirements.txt

# 2. Create an example config next to the tool
python main.py init

# 3. Edit sync_config.yaml — set real project paths / map folders

# 4. Dry-run, then sync / watch / map
python main.py sync --dry-run
python main.py sync
python main.py watch -d
python main.py map
```

`python -m synctool <command>` is equivalent to `python main.py <command>`.

## Quick start (Windows exe)

1. Download `RainSync.exe` (or the build artifact) from the [GitHub Releases](https://github.com/ViTuler/RainSync/releases) page.
2. Put the exe in a folder of your choice and run:

```text
RainSync.exe init
```

3. Edit the generated `sync_config.yaml` next to the exe, then:

```text
RainSync.exe sync --dry-run
RainSync.exe sync
RainSync.exe watch -d
RainSync.exe map
```

Logs always go to `sync.log` next to the exe.

## Commands

```text
python main.py init                  # create example sync_config.yaml
python main.py init -f               # overwrite existing config with the example
python main.py sync                  # full sync once
python main.py sync -d               # sync once, then watch in the background
python main.py sync -c my.yaml
python main.py sync --dry-run
python main.py watch                 # watch in this console (Ctrl+C to stop)
python main.py watch -d              # watch as a detached background process
python main.py map                   # backup all map groups
python main.py map Test_Group        # backup one map group
python main.py map --dry-run
python main.py --version
python main.py --help
```

When `-c/--config` is omitted, the tool looks for `sync_config.yaml` in this order:

1. the **working folder** (where you ran the command);
2. the **tool folder** (next to the executable, or the project root when run from source).

If neither has a config, one is created next to the tool. The log is **always** written to `sync.log` next to the tool.

```text
A\RainSync.exe         <- tool folder: config is created here, log always here
B\                     <- run from here: a sync_config.yaml in B takes priority
```

### Background watching (`watch -d`)

`watch -d` re-launches the watcher as a **detached background process** and returns to the shell immediately.

* Starting `watch -d` again does **not** stop an already running watcher — each `-d` launch is its own process.
* Console builds print PID / warnings to the terminal; windowed (`--noconsole`) builds show a Windows message box.
* To stop: `taskkill /IM <exe-name> /F` (ends **all** instances of that exe).

## Configuration

A config may define sync groups, map groups, or both.

```yaml
sync_groups:
  common:
    target_folder: tools   # folder that is kept in sync inside each project
    init_sync: true        # watch: run a full sync before watching
    allow_delete: false    # propagate file deletions in watch mode
    auto_walk: 3           # search depth for target_folder inside a project
    watch:
      interval: 3          # debounce (seconds) in watch mode

    projects:              # at least two projects
      - name: Rain Music
        path: C:\Users\...\RainM
        ignore: []         # optional project-level ignore patterns
      - name: Rain WeChat
        path: C:\Users\...\RainWe

    ignore:                # group-level ignore patterns (all projects)
      - .git/
      - __pycache__/
      - "*.pyc"
      - "*.sync_tmp"

map_groups:
  Test_Group:
    source: H:\test           # folder to back up
    target: F:\test_mapping   # destination folder
    work_threaders: 5         # parallel copy workers (default: 5)
    # ignore:                 # optional extras; use !pattern to re-include a built-in skip
    #   - "*.log"
    #   - "!target/"
```

Accepted spellings:

* sync groups: `sync_groups` / `groups` / `group`
* projects: `projects` / `project`
* map groups: `map_groups` / `map_group`

### auto_walk

| value | meaning |
| ----- | ------- |
| `false` / missing | only the project root is checked |
| `3` | search up to 3 folder levels below the project |
| `true` | search the whole project tree |

### allow_delete / init_sync

* `allow_delete: true` propagates filesystem deletions in watch mode. A full sync never treats a missing file as an intentional delete — peers have equal authority.
* `init_sync: true` makes `watch` run a full synchronize before observing changes, so new projects converge immediately.

### Conflicts

During a full sync, if the same relative file exists in several projects with **different contents**, the tool reports a **conflict** and does not pick a winner. Resolve it manually (or via watch after you edit one side), then sync again.

### Map backup behaviour

* Copies a file under `source` into `target` only when it is missing, or when size or last-write time differs. Unchanged files are skipped without reading contents.
* Nested folders are scanned on the main thread; file copies use `work_threaders` workers (default **5**).
* Copies go to a temp file in the destination, then swap into place — a failed copy does not leave a half-written file.
* Creates `target` when it does not exist.
* Does **not** delete files that exist only in `target`.
* While scanning, **automatically prunes** common-language cache/dependency dirs (JS, Python, Rust, Java, C++; does not descend into them). Packaging/output dirs such as `build/`, `dist/`, `.output/`, `out/` are **not** skipped by default. Add more with `ignore`, or force-include a built-in skip with `!pattern` (e.g. `!target/`).

#### Built-in map cache skips

Only the common stacks below. Patterns use gitignore-style rules (any depth; trailing `/` means directory-only):

| Ecosystem | Skipped by default |
| --------- | ------------------ |
| JS / TS / Node | `node_modules/`, `bower_components/`, `.npm/`, `.yarn/cache/`, `.yarn/unplugged/`, `.pnpm-store/` |
| Python | `__pycache__/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `.tox/`, `.nox/`, `.hypothesis/`, `.eggs/`, `*.egg-info/`, `.venv/`, `venv/` |
| Rust | `target/` (Cargo; Maven often uses the same name) |
| Java | `.gradle/`, `.mvn/` |
| C / C++ | `CMakeFiles/`, `cmake-build-debug/`, `cmake-build-release/`, `cmake-build-*/`, `.cxx/` |

The full tuple lives in `synctool/models.py` as `DEFAULT_MAP_CACHE_IGNORES`.

## Logging

Every operation is appended to `sync.log` next to the tool. Lines are pipe-separated:

```text
2026-09-05 10:20:42 | WATCH_START
2026-09-05 10:20:53 | COPY | common | utils.py | Rain Music -> Rain WeChat
2026-09-05 10:20:53 | DELETE | common | old.py | Rain WeChat -> Rain Music
2026-09-05 10:20:53 | CONFLICT | common | config.py
2026-09-05 10:20:53 | SYNC_DONE | common | copied=3 conflicts=0
2026-09-18 14:50:01 | MAP_START
2026-09-18 14:50:02 | MAP_COPY | Test_Group | nested/file.txt
2026-09-18 14:50:02 | MAP_DONE | Test_Group | copied=12 skipped=3 dirs=4 errors=0
```

## Build a Windows executable

From the project root (with PyInstaller installed in your active env):

```text
pyinstaller --noconfirm main.spec
```

Output: `dist/main.exe` (rename to `RainSync.exe` if you like before uploading to a release).

**Keep the console subsystem on.** `main.spec` sets `console=True` on purpose — do **not** switch to `--noconsole` / `console=False`.

* `sync`, `map`, and foreground `watch` need a console for progress and Ctrl+C.
* With `--noconsole`, Windows treats the exe as a GUI app: the shell may return immediately and stdout/stderr are dropped.
* Background mode (`watch -d` / `sync -d`) still uses `CREATE_NO_WINDOW` so the detached child does not open a second console.

## Project layout

```text
main.py                  thin entry point  (python main.py <command>)
synctool/                package
  __main__.py              python -m synctool
  models.py                data structures
  ignore.py                gitignore-style ignore matching
  logger.py                append-only sync history log
  config.py                YAML load / validate / group selection
  engine.py                sync engine
  mapper.py                map / backup
  watcher.py               watchdog + debounce
  cli.py                   commands
legacy/                  previous single-file implementation (reference only)
tests/                   unit tests
requirements.txt
main.spec                PyInstaller spec
```

## YAML notes

Config is parsed with `ruamel.yaml` in round-trip mode (`preserve_quotes=True`), so comments, ordering, and quotes are preserved if the file is rewritten later. Current commands only **read** the config and never rewrite it.
