# synctool

本地 CLI 工具：在多个本地 Python 项目之间同步共享工具目录，并通过单向 **map** 组备份整棵文件夹。

英文版见 [README.md](README.md)。

## 模型

**同步组（sync groups）** — 同组内的项目是对等关系，没有唯一主源。任一项目中的文件变更会传播到同组其他项目。全量同步时，同一文件取最近修改的版本，复制到尚未持有相同内容的项目。

**映射组（map groups）** — 每组将一个 `source` 文件夹递归备份到 `target`。内容相同的文件会跳过；仅存在于目标侧的文件不会删除（不是破坏性镜像）。

## 项目结构

```text
sync_tool.py / main.py   薄入口  (python main.py <command>)
synctool/                包代码
  __main__.py              python -m synctool
  models.py                数据结构（Project / Group / MapGroup / …）
  ignore.py                gitignore 风格的忽略匹配
  logger.py                追加式日志（同步历史）
  config.py                YAML 加载 / 校验 / 组选择
  engine.py                同步引擎（全量扫描 + 事件传播）
  mapper.py                文件夹映射 / 备份（map 命令）
  watcher.py               watchdog 封装与防抖（watch 模式）
  cli.py                   参数解析与命令入口
legacy/                  旧版单文件实现（仅供参考）
tests/                   单元测试
```

## 命令

```text
python sync_tool.py sync
python sync_tool.py sync -d              # 同步一次，然后进入监听
python sync_tool.py sync -c my_config.yaml
python sync_tool.py sync --dry-run
python sync_tool.py watch
python sync_tool.py watch -d             # 以后台分离进程方式监听
python sync_tool.py map                  # 备份全部 map_groups
python sync_tool.py map Yuch_Group       # 备份指定 map 组
python sync_tool.py map --dry-run
python sync_tool.py --version
python sync_tool.py --help
```

`python -m synctool <command>` 效果相同。省略 `-c/--config` 时，按以下顺序查找 `sync_config.yaml`：

1. **工作目录**（执行命令时所在目录）；
2. **工具目录**（可执行文件旁，或从源码运行时的项目根目录）。

两处都没有配置时，会在工具目录旁创建一份示例配置。无论从哪里运行，日志**始终**写到工具旁的 `sync.log`。

```text
A\sync_tool.exe        <- 工具目录：在此创建配置，日志也固定写在这里
B\                     <- 从这里运行：B 下的 sync_config.yaml 优先
```

### 后台监听（`watch -d`）

`watch -d` 会把监听器以后台**分离进程**重新拉起，然后立即返回 shell。

* **单实例** — 同一配置与组选择只允许一个后台监听。再次启动会提示警告并退出，不会新建进程。系统临时目录下的 PID 锁文件（`synctool-watch-<hash>.pid`）用于跟踪运行中的监听器；崩溃残留的陈旧锁会自动清理。
* **控制台 vs 无窗口构建** — 普通控制台构建会把 PID / 警告打印到控制台；无控制台（`--noconsole`）构建则弹出 Windows 消息框。
* 停止方式：`taskkill /IM <exe-name> /F`（会结束该 exe 的全部实例）。

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
  Yuch_Group:
    source: H:\test           # 要备份的源文件夹
    target: F:\test_mapping   # 备份目标文件夹
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

全量同步时，若同一相对路径在多个项目中内容不同，工具会报告 **conflict（冲突）**，而不会擅自挑选胜者。

### Map 备份行为

* 将 `source` 下的全部文件与子目录复制到 `target`。
* `target` 不存在时会创建。
* 内容相同则跳过；内容不同则覆盖。
* **不会**删除仅存在于 `target` 中的文件。

## 日志

所有操作追加写入同一个 `sync.log`，位置始终在工具旁（可执行文件所在目录，或从源码运行时的项目根）。字段以 `|` 分隔：

```text
2026-09-05 10:20:42 | WATCH_START
2026-09-05 10:20:53 | COPY | common | utils.py | Rain Music -> Rain WeChat
2026-09-05 10:20:53 | DELETE | common | old.py | Rain WeChat -> Rain Music
2026-09-05 10:20:53 | CONFLICT | common | config.py
2026-09-05 10:20:53 | SYNC_DONE | common | copied=3 conflicts=0
2026-09-18 14:50:01 | MAP_START
2026-09-18 14:50:02 | MAP_COPY | Yuch_Group | nested/file.txt
2026-09-18 14:50:02 | MAP_DONE | Yuch_Group | copied=12 skipped=3 dirs=4 errors=0
```

## YAML 注释与格式

配置使用 `ruamel.yaml` 的 round-trip 模式（`preserve_quotes=True`）解析，因此若日后回写文件，注释、顺序与引号可保留。当前命令只读取配置，不会改写它。
