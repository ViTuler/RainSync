# synctool

A small local CLI for synchronizing shared tool folders across multiple local
Python projects.

## Model

Projects in the same **group** are peers — there is no canonical source
project. A file change in one project is propagated to the other projects of
that group. During a full sync the most recently modified version of each file
wins and is copied to every project that does not already hold identical
content.

## Project layout

```text
sync_tool.py        thin entry point  (python sync_tool.py <command>)
synctool/           the package
  __main__.py         python -m synctool
  models.py           data structures (Project / Group / SyncStats)
  ignore.py           gitignore-style ignore matching
  logger.py           append-only log file writer (sync history)
  config.py           YAML loading / validation / group selection
  engine.py           sync engine (full scan + event propagation)
  watcher.py          watchdog wrapper with debounce (watch mode)
  cli.py              argument parsing and commands
legacy/             previous single-file implementation (reference only)
tests/              unit tests
```

## Commands

```text
python sync_tool.py sync
python sync_tool.py sync -d              # sync once, then watch
python sync_tool.py sync -c my_config.yaml
python sync_tool.py sync --dry-run
python sync_tool.py watch
python sync_tool.py watch -d             # watch as a detached background process
python sync_tool.py --version
python sync_tool.py --help
```

`python -m synctool <command>` is equivalent. When `-c/--config` is omitted
the tool looks for `sync_config.yaml` in this order:

1. the **working folder** (where you ran the command);
2. the **tool folder** (next to the executable, or the project root when run
   from source).

When neither location has a config, one is created next to the tool. The log
is **always** written to `sync.log` next to the tool, no matter where you run
it from.

```text
A\sync_tool.exe        <- tool folder: config is created here, log always here
B\                     <- run from here: a sync_config.yaml in B takes priority
```

### Background watching (`watch -d`)

`watch -d` re-launches the watcher as a **detached background process** and
returns to the shell immediately.

* **Single instance** — only one background watcher may run for a given config
  and group selection. Starting a second one shows a warning and exits without
  creating a new process. A PID lock file under the system temp dir
  (`synctool-watch-<hash>.pid`) tracks the running watcher; a stale lock from a
  crashed watcher is cleaned up automatically.
* **Console vs windowed builds** — in a normal console build the PID / warning
  messages are printed to the console. In a windowed (`--noconsole`) build they
  are shown as a Windows message box, because there is no console to print to.
* To stop it: `taskkill /IM <exe-name> /F` (kills all instances of the exe).

## Configuration

```yaml
group:
  common:
    target_folder: tools   # folder that is kept in sync inside each project
    init_sync: true        # watch: run a full sync before watching
    allow_delete: false    # propagate file deletions in watch mode
    auto_walk: 3           # search depth for target_folder inside a project
    watch:
      interval: 3          # debounce (seconds) in watch mode

    project:               # at least two projects
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
```

Both `group`/`groups` and `project`/`projects` spellings are accepted.

### auto_walk

| value      | meaning                                          |
| ---------- | ------------------------------------------------ |
| `false` / missing | only the project root is checked       |
| `3`        | search up to 3 folder levels below the project   |
| `true`     | search the whole project tree                    |

### allow_delete / init_sync

* `allow_delete: true` propagates filesystem deletions in watch mode. A full
  sync never guesses that a missing file was deleted on purpose — peers have
  equal authority, so that decision is left to the user.
* `init_sync: true` makes `watch` run a full synchronize before it starts
  observing changes, so new projects converge immediately.

### Conflicts

During a full sync, if the same relative file exists in several projects with
different contents, the tool reports a **conflict** instead of guessing a
winner.

## Logging

Every operation is appended to a single `sync.log` file, always located next
to the tool itself (the folder holding the executable, or the project root
when run from source). Lines are pipe-separated:

```text
2026-09-05 10:20:42 | WATCH_START
2026-09-05 10:20:53 | COPY | common | utils.py | Rain Music -> Rain WeChat
2026-09-05 10:20:53 | DELETE | common | old.py | Rain WeChat -> Rain Music
2026-09-05 10:20:53 | CONFLICT | common | config.py
2026-09-05 10:20:53 | SYNC_DONE | common | copied=3 conflicts=0
```

## YAML comments and formatting

The configuration is parsed with `ruamel.yaml` in round-trip mode with
`preserve_quotes=True`, so comments, ordering, and quotes are preserved if the
file is ever rewritten later. The current commands only read the config and
never rewrite it.

