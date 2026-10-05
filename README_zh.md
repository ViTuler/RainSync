# RainSync

本地 CLI：在多个项目之间同步共享文件夹，并做单向文件夹备份。

英文版见 [README.md](README.md)。

**当前版本：** `0.2.0` · 命令行名称：`synctool`

## 能做什么

| 功能 | 行为 |
| ---- | ---- |
| **同步组（sync groups）** | 同组项目对等。一侧变更会复制到其他项目。全量同步时：内容相同则补齐缺失；内容不同则报告冲突。 |
| **映射组（map groups）** | 单向备份：`source` → `target`。只复制缺失或已变化的文件（大小 / 修改时间）。目标侧多出来的文件不会删。 |

常见用途：多个本地仓库共享 `.tools` / `utils`，或把工作目录定期备份到另一块盘。

## 环境要求

- Windows（主要目标平台；路径与后台模式按 Windows 编写）
- 从源码运行时需要 Python 3.10+
- 依赖：`ruamel.yaml`、`watchdog`（见 `requirements.txt`）

## 快速开始（源码）

```text
# 1. 克隆并安装依赖（请用非 base 的 conda 环境，或 venv）
git clone https://github.com/ViTuler/RainSync.git
cd RainSync
pip install -r requirements.txt

# 2. 在工具旁生成示例配置
python main.py init

# 3. 编辑 sync_config.yaml — 填入真实的项目路径 / 备份目录

# 4. 先 dry-run，再 sync / watch / map
python main.py sync --dry-run
python main.py sync
python main.py watch -d
python main.py map
```

`python -m synctool <command>` 与 `python main.py <command>` 等价。

## 快速开始（Windows 可执行文件）

1. 从 [GitHub Releases](https://github.com/ViTuler/RainSync/releases) 下载 `RainSync.exe`（或构建产物）。
2. 放到任意目录后执行：

```text
RainSync.exe init
```

3. 编辑 exe 旁生成的 `sync_config.yaml`，然后：

```text
RainSync.exe sync --dry-run
RainSync.exe sync
RainSync.exe watch -d
RainSync.exe map
```

日志始终写在 exe 旁的 `sync.log`。

## 命令

```text
python main.py init                  # 生成示例 sync_config.yaml
python main.py init -f               # 强制用示例覆盖已有配置
python main.py sync                  # 全量同步一次
python main.py sync -d               # 同步一次，然后后台监听
python main.py sync -c my.yaml
python main.py sync --dry-run
python main.py watch                 # 在当前控制台监听（Ctrl+C 停止）
python main.py watch -d              # 以后台分离进程监听
python main.py map                   # 备份全部 map 组
python main.py map Test_Group        # 备份指定 map 组
python main.py map --dry-run
python main.py --version
python main.py --help
```

省略 `-c/--config` 时，按以下顺序查找 `sync_config.yaml`：

1. **工作目录**（执行命令时所在目录）；
2. **工具目录**（可执行文件旁，或从源码运行时的项目根目录）。

两处都没有配置时，会在工具目录创建示例配置。无论从哪里运行，日志**始终**写到工具旁的 `sync.log`。

```text
A\RainSync.exe         <- 工具目录：在此创建配置，日志也固定写在这里
B\                     <- 从这里运行：B 下的 sync_config.yaml 优先
```

### 后台监听（`watch -d`）

`watch -d` 会把监听器以后台**分离进程**重新拉起，然后立即返回 shell。

* 再次执行 `watch -d` **不会**停止已在运行的监听器——每次 `-d` 都是独立进程。
* 控制台构建会把 PID / 警告打印到终端；无控制台（`--noconsole`）构建则弹出 Windows 消息框。
* 停止方式：`taskkill /IM <exe-name> /F`（会结束该 exe 的**全部**实例）。

## 配置

一份配置可以只写同步组、只写映射组，或两者都写。

```yaml
sync_groups:
  common:
    target_folder: tools   # 各项目内需要同步的文件夹名
    init_sync: true        # watch：开始监听前先做一次全量同步
    allow_delete: false    # watch 模式下是否传播删除
    auto_walk: 3           # 在项目内搜索 target_folder 的深度
    watch:
      interval: 3          # watch 防抖间隔（秒）

    projects:              # 至少两个项目
      - name: Rain Music
        path: C:\Users\...\RainM
        ignore: []         # 可选：项目级忽略规则
      - name: Rain WeChat
        path: C:\Users\...\RainWe

    ignore:                # 组级忽略规则（作用于全部项目）
      - .git/
      - __pycache__/
      - "*.pyc"
      - "*.sync_tmp"

map_groups:
  Test_Group:
    source: H:\test           # 要备份的源文件夹
    target: F:\test_mapping   # 备份目标文件夹
    work_threaders: 5         # 并行复制线程数（默认 5）
```

接受的写法：

* 同步组：`sync_groups` / `groups` / `group`
* 项目列表：`projects` / `project`
* 映射组：`map_groups` / `map_group`

### auto_walk

| 取值 | 含义 |
| ---- | ---- |
| `false` / 缺省 | 只检查项目根目录 |
| `3` | 最多向下搜索 3 层 |
| `true` | 搜索整个项目树 |

### allow_delete / init_sync

* `allow_delete: true` 时，watch 模式会传播文件系统删除。全量同步不会把「某侧缺失」当成故意删除——对等项目权限相同，是否删除留给用户决定。
* `init_sync: true` 时，`watch` 会在开始观察变更前先跑一次全量同步，便于新项目立刻对齐。

### 冲突

全量同步时，若同一相对路径在多个项目中**内容不同**，工具会报告 **conflict（冲突）**，不会擅自挑选胜者。请手动解决（或编辑一侧后由 watch 传播），再重新 sync。

### Map 备份行为

* 仅当 `source` 中的文件在 `target` 缺失，或大小 / 最后写入时间不同时才复制。未变化的文件直接跳过，不读取内容。
* 嵌套目录在主线程扫描；文件复制由 `work_threaders` 个工作线程并行执行（默认 **5**）。
* 复制是安全的：先写入目标目录中的临时文件，再替换到位。复制失败不会留下写了一半的文件。
* `target` 不存在时会创建。
* **不会**删除仅存在于 `target` 中的文件。

## 日志

所有操作追加写入工具旁的 `sync.log`，字段以 `|` 分隔：

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

## 打包 Windows 可执行文件

在项目根目录（当前环境已安装 PyInstaller）：

```text
pyinstaller --noconfirm main.spec
```

产物：`dist/main.exe`（上传 Release 前可改名为 `RainSync.exe`）。

**注意：必须保留控制台。** `main.spec` 里故意设置了 `console=True`，**不要**改成 `--noconsole` / `console=False`。

* `sync`、`map` 以及前台 `watch` 需要控制台输出进度，并用 Ctrl+C 结束。
* 使用 `--noconsole` 时，Windows 会把 exe 当成 GUI 程序：shell 可能立刻返回，stdout/stderr 也会被丢弃。
* 后台模式（`watch -d` / `sync -d`）仍用 `CREATE_NO_WINDOW`，分离子进程不会再弹第二个控制台窗口。

## 项目结构

```text
main.py                  薄入口  (python main.py <command>)
synctool/                包代码
  __main__.py              python -m synctool
  models.py                数据结构
  ignore.py                gitignore 风格忽略匹配
  logger.py                追加式同步历史日志
  config.py                YAML 加载 / 校验 / 组选择
  engine.py                同步引擎
  mapper.py                映射 / 备份
  watcher.py               watchdog + 防抖
  cli.py                   命令入口
legacy/                  旧版单文件实现（仅供参考）
tests/                   单元测试
requirements.txt
main.spec                PyInstaller 配置
```

## YAML 说明

配置使用 `ruamel.yaml` 的 round-trip 模式（`preserve_quotes=True`）解析，因此若日后回写文件，注释、顺序与引号可保留。当前命令只**读取**配置，不会改写它。
