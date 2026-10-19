# Docker src 缺失源码导出操作手册

> **后续切换已完成：** 当前状态已是 `ACTIVE_WSL_SOURCES`，详见同目录 `2026-10-17-docker-workspace-cutover-result.md`。下面“未启用”标记是第一阶段的历史状态，不能用来覆盖当前状态或重复执行导出。

> **执行状态（2026-10-17）：第一阶段已完成。** 16 个依赖已导出并通过三方快照验证，状态 `EXPORTED_NOT_ACTIVE`；主仓库与运行挂载保持原状。实际结果、版本和证据路径见同目录 `2026-10-17-docker-src-export-result.md`。下文命令是操作手册，目标已存在，不能原样再次执行覆盖。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将当前 Docker 卷中的 16 个依赖源码仓库完整复制到普通 WSL 目录并验证一致，为以后调整挂载准备材料。

**Architecture:** 第一阶段只读访问现有卷，导出源码归档，解压到全新临时目录，核对后发布为 WSL `daaam_ws/src`。现有主仓库、Compose、justfile 和运行环境不切换。后续阶段单独处理源码挂载与开发入口。

**Tech Stack:** 当前 Ubuntu-20.04 WSL2、现有 Docker 镜像、GNU tar、Python3、Git。

**Spec:** 用户已明确先拿出 Docker 中有而 WSL 普通目录中没有的源码，再整理入口。本手册是该步骤的详细规划；优先级高于先前总体计划中“复制阶段即移动主仓库”的安排。

## 0. 本次范围与证据

本手册最初为规划交付，现已执行第一阶段；以下未勾选项保留为复用检查清单，不替代执行结果报告。后续挂载切换尚未执行。

2026-10-17 只读核查：

- 当前 Docker 引擎名称 `MS0096`，核查时没有运行中的容器；执行前必须刷新。
- 存在卷 `daaam_daaam_workspace`，local 驱动。
- 本地依赖镜像 `daaam:jazzy-cu128-deps` 的 ID：`sha256:c1c1d352f01484d0ba246f711fc51e68f39971829c399db97f7731e33aa2269a`。后续以实际 ID 为准，不能只依赖可变 tag。
- 当前主仓库 `/home/arclunar/warmoon_ws/DAAAM` 已在 WSL，不导出卷内的 daaam 挂载占位目录。
- 当前 `/home/arclunar/warmoon_ws` 的一级目录没有与下列 16 个依赖同名的目录；尚未证明其他任意位置完全没有副本。以本次目标目录是否冲突为准，不扫描所有磁盘。
- 目标 `/home/arclunar/warmoon_ws/daaam_ws` 当前不存在。
- 此前卷内 src 约 269 MB；执行前重测。新导出归档和解压副本需额外空间，先预留 2 GB。Linux df 显示的虚拟磁盘可用量不等于 Windows 承载盘可用量，二者都复查。
- 主仓库有其他任务的未提交修改，禁止 reset、clean、批量提交或搬动整个主仓库。

## 1. 导出清单与排除项

期望导出以下 **16 个完整仓库**，包括 `.git`、本地修改、未跟踪文件及忽略文件；不依赖 git archive，因为它会遗漏这些内容。

```text
config_utilities
daaam_ros
gtsam
hydra
hydra_ros
ianvs
khronos
kimera_pgmo
kimera_rpgo
pose_graph_tools
semantic_inference
small_gicp
spark_dsg
spatial_hash
teaser_plusplus
vision_opencv
```

发现额外目录或文件时停止核对清单，不悄悄漏掉；发现子模块或 `.git` 指向外部 gitdir 的仓库时先扩展方案，不能把不完整工作树当导出完成。

第一阶段不导出：卷内 `src/daaam` 占位目录、`.venv`、`build`、`install`、`log`、`cache`、`vendor`。`vendor` 是 src 外的第三方 Python 源码；如后面需要编辑它，再单独复制和映射，本阶段不混入。

目标：

```text
/home/arclunar/warmoon_ws/
├── DAAAM/                         # 当前真实主仓库，保持原位置
├── .daaam-source-export/
│   └── <本次操作编号>/            # 归档、快照、状态文件
└── daaam_ws/
    └── src/                       # 核查完成后才发布的依赖源码副本
        ├── hydra/
        ├── daaam_ros/
        └── 其他14个仓库
```

发布后仍以旧卷内源码为开发源；新副本处于“已导出、未切换”状态，不从这里继续开发。完整工作区图中的 `src/daaam` 在第一阶段故意不存在。

## 2. 执行前检查

- [ ] 在 WSL Ubuntu-20.04 的 Bash 中操作，不把下面 Bash 块直接贴进 PowerShell。Windows 先运行 `wsl -d Ubuntu-20.04`，再运行 `bash`。
- [ ] 确认没有编辑器、Agent、容器、编译或安装过程正在改这 16 个仓库；仅 `docker ps` 为空不足以排除宿主进程。若有人正在编辑，只读盘点可继续，归档和最终快照等待写入结束。
- [ ] 核对 Windows 承载盘和 WSL 可用空间，不触发 VHD 压缩、WSL shutdown 或 Docker 清理。
- [ ] 查看主仓库 git status，记录既有修改。第一阶段不修改主仓库运行配置。

以下变量必须在同一 Bash 会话中沿用。若换终端，读取该操作目录里的配置文件，不重新生成另一个编号继续半次操作。

```bash
set -euo pipefail
TASK_ROOT=/home/arclunar/warmoon_ws
TASK_ID=$(date +%Y%m%d_%H%M%S)
TASK_EXPORT="$TASK_ROOT/.daaam-source-export/$TASK_ID"
TASK_WS="$TASK_ROOT/daaam_ws"
TASK_STAGE="$TASK_WS/.src-import-$TASK_ID"
TASK_FINAL="$TASK_WS/src"
TASK_VOLUME=daaam_daaam_workspace
docker volume inspect "$TASK_VOLUME" >/dev/null
TASK_IMAGE=$(docker image inspect daaam:jazzy-cu128-deps --format '{{.Id}}')
TASK_UID=$(id -u)
TASK_GID=$(id -g)
test "$TASK_UID" -eq 1000
test "$TASK_GID" -eq 1000
test ! -e "$TASK_WS" && test ! -L "$TASK_WS"
docker info --format '{{.Name}}'
docker ps --format '{{.Names}} {{.Image}}'
df -h "$TASK_ROOT"
mkdir -p "$TASK_EXPORT" "$TASK_STAGE"
printf '%s\n' "$TASK_ID" > "$TASK_EXPORT/task-id.txt"
declare -p TASK_ROOT TASK_ID TASK_EXPORT TASK_WS TASK_STAGE TASK_FINAL \
  TASK_VOLUME TASK_IMAGE TASK_UID TASK_GID > "$TASK_EXPORT/variables.bash"
```

UID/GID 与当前已知身份不符时先检查所有权，不自动递归 chown Docker 数据根。目标已经存在时停止，核对它是否属于前次导出或已有工作区；绝不向里面叠加拷贝。

## 3. 先记录源码指纹与 Git 状态

把下面内容保存为 `$TASK_EXPORT/snapshot.py`（在未来执行阶段创建）。该脚本只读文件和 Git，输出所有普通文件的 SHA-256、模式、相对路径、符号链接目标及每个仓库的 HEAD/工作区状态；不输出文件内容。隐藏文件和 `.git` 目录也计入。

```python
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

root = Path(sys.argv[1])
expected = sorted('config_utilities daaam_ros gtsam hydra hydra_ros ianvs '
                  'khronos kimera_pgmo kimera_rpgo pose_graph_tools '
                  'semantic_inference small_gicp spark_dsg spatial_hash '
                  'teaser_plusplus vision_opencv'.split())
actual = sorted(p.name for p in root.iterdir() if p.name != 'daaam')
if actual != expected:
    raise SystemExit('source list mismatch: ' + repr(actual))

def git(repo, *args):
    env = dict(os.environ, GIT_OPTIONAL_LOCKS='0')
    result = subprocess.run(
        ['git', '-c', 'safe.directory=' + str(repo), '-C', str(repo), *args],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise SystemExit(repo.name + ': git failed: ' +
                         result.stderr.decode(errors='replace'))
    return result.stdout.decode('utf-8', errors='surrogateescape')

files, repos = [], {}
for name in expected:
    repo = root / name
    if repo.is_symlink() or not repo.is_dir():
        raise SystemExit(name + ': not an independent directory')
    if not (repo / '.git').is_dir() or (repo / '.git').is_symlink():
        raise SystemExit(name + ': external gitdir or incomplete repository')
    repos[name] = {
        'head': git(repo, 'rev-parse', 'HEAD').strip(),
        'status': git(repo, 'status', '--porcelain=v1', '-z', '--untracked-files=all'),
        'submodules': git(repo, 'submodule', 'status', '--recursive'),
        'gitlinks': '\n'.join(line for line in git(repo, 'ls-files', '--stage').splitlines()
                             if line.startswith('160000 ')),
    }
    if any(not line.startswith('-') for line in repos[name]['submodules'].splitlines()):
        raise SystemExit(name + ': initialized submodule requires completeness review')
    paths = [repo]
    for base, dirs, names in os.walk(repo, followlinks=False):
        paths.extend(Path(base) / x for x in dirs + names)
    for p in sorted(paths, key=lambda x: str(x.relative_to(root))):
        st = p.lstat()
        entry = {'path': str(p.relative_to(root)), 'mode': stat.S_IMODE(st.st_mode)}
        if p.is_symlink():
            entry.update(type='link', target=os.readlink(p))
        elif p.is_file():
            before = (st.st_size, st.st_mtime_ns, st.st_ctime_ns)
            h = hashlib.sha256()
            with p.open('rb') as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b''):
                    h.update(chunk)
            after = p.lstat()
            if before != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise SystemExit('file changed during scan: ' + str(p))
            entry.update(type='file', size=st.st_size, sha256=h.hexdigest())
        elif p.is_dir():
            entry.update(type='dir')
        else:
            raise SystemExit('special file needs review: ' + str(p))
        files.append(entry)
json.dump({'repositories': repos, 'files': files}, sys.stdout,
          sort_keys=True, ensure_ascii=True, indent=2)
sys.stdout.write('\n')
```

创建并验证脚本后，用现有镜像、只读卷、普通用户身份扫描。容器不使用原 entrypoint，不激活或创建 venv，不访问 GPU或网络。实际镜像若缺少 Git/Python/tar，则先报告，不自动下载新镜像。

```bash
TASK_SOURCE_MOUNT="type=volume,src=$TASK_VOLUME,dst=/source,readonly,volume-nocopy"
docker run --rm -i --network none --read-only \
  --user "$TASK_UID:$TASK_GID" \
  --mount "$TASK_SOURCE_MOUNT" \
  --entrypoint python3 "$TASK_IMAGE" - /source/src \
  < "$TASK_EXPORT/snapshot.py" > "$TASK_EXPORT/source-before.json"
```

- [ ] 检查 16 个仓库的 Git 状态，包括 dirty 项；dirty 不代表禁止复制，但必须完整保留。Git 错误不能当成 clean。
- [ ] 检查快照里的链接：绝对链接或指向 src 外的相对链接先解释用途，确认导出后是否仍可解析。脚本保留链接文本，不代表已证明链接有效。
- [ ] 单独保存所有权/时间/硬链接信息摘要：`tar -tvf` 可检查模式与归档元数据，`find` 可核对链接；快照比较不强求保留 UID/GID（目标应属于当前用户），也不比较 atime/ctime。

## 4. 导出归档，解压到临时目录

```bash
docker run --rm --network none --read-only \
  --user "$TASK_UID:$TASK_GID" \
  --mount "$TASK_SOURCE_MOUNT" \
  --entrypoint tar "$TASK_IMAGE" \
  --format=posix -C /source/src --exclude=./daaam -cf - . \
  > "$TASK_EXPORT/src.tar.partial"
mv -- "$TASK_EXPORT/src.tar.partial" "$TASK_EXPORT/src.tar"
sha256sum "$TASK_EXPORT/src.tar" > "$TASK_EXPORT/src.tar.sha256"
tar -tf "$TASK_EXPORT/src.tar" > "$TASK_EXPORT/archive-members.txt"
```

- [ ] 检查成员全部位于 `./` 下的这 16 个目录，包含 `.git`，无绝对路径或 `..` 路径分量。归档由已核对的本地源码生成；发现异常链接/特殊文件先停，不盲目解压。
- [ ] 归档成功、清单核对后才执行解压。不要解压到主仓库或已有依赖目录。

```bash
tar --extract --preserve-permissions --no-same-owner \
  --file "$TASK_EXPORT/src.tar" --directory "$TASK_STAGE"
```

所有命令都在真实 Bash 文件或交互 Bash 会话中执行。不要从 Bash stdin 连续喂整段脚本再让 docker 无意读取剩余内容；这里唯一 `-i` 的扫描容器明确从独立 snapshot.py 文件读入。

## 5. 三方一致性验证

复制后再扫描旧卷，并扫描新目录。只有 **source-before = source-after = destination** 才通过。

```bash
docker run --rm -i --network none --read-only \
  --user "$TASK_UID:$TASK_GID" --mount "$TASK_SOURCE_MOUNT" \
  --entrypoint python3 "$TASK_IMAGE" - /source/src \
  < "$TASK_EXPORT/snapshot.py" > "$TASK_EXPORT/source-after.json"

docker run --rm -i --network none --read-only \
  --user "$TASK_UID:$TASK_GID" \
  --mount "type=bind,src=$TASK_STAGE,dst=/target,readonly" \
  --entrypoint python3 "$TASK_IMAGE" - /target \
  < "$TASK_EXPORT/snapshot.py" > "$TASK_EXPORT/destination.json"

cmp "$TASK_EXPORT/source-before.json" "$TASK_EXPORT/source-after.json"
cmp "$TASK_EXPORT/source-after.json" "$TASK_EXPORT/destination.json"
sha256sum -c "$TASK_EXPORT/src.tar.sha256"
```

- [ ] 检查全部命令退出码，不能只看到 json 文件存在就认为扫描成功。
- [ ] source-before/source-after 不同：有并发写入或不稳定内容，不发布；保留归档/报告，待稳定后创建新操作编号重做。
- [ ] 目标快照不同：定位实际差异（内容、链接、模式、Git 状态），不向旧卷反向同步，不使用 `rsync --delete`。
- [ ] 验证普通用户所有权、可执行脚本模式、链接，以及归档中的文件时间和硬链接关系；确认解压未留下 root 文件。
- [ ] 记录 16 个仓库路径、HEAD、dirty/untracked 数量、文件数/大小、归档摘要、镜像 ID、卷名和验证结论。只写“导出完成”，不写“挂载已切换”。

## 6. 发布导出目录并明确状态

```bash
test -d "$TASK_STAGE" && test ! -L "$TASK_STAGE"
test ! -e "$TASK_FINAL" && test ! -L "$TASK_FINAL"
mv -T -- "$TASK_STAGE" "$TASK_FINAL"
printf '%s\n' \
  'EXPORTED_NOT_ACTIVE' \
  'Source of truth remains the Docker workspace volume until an explicit mount cutover.' \
  "Evidence: $TASK_EXPORT" > "$TASK_WS/SOURCE_EXPORT_STATUS.txt"
```

- [ ] 用 WSL 普通用户列出全部依赖，并通过 Windows UNC 打开 `\\wsl.localhost\Ubuntu-20.04\home\arclunar\warmoon_ws\daaam_ws\src`。
- [ ] 不在新副本编辑，不用新副本运行安装器或构建；旧环境继续照常开发。
- [ ] 保留原卷源码、归档和验证报告。本阶段不删除任何原始资源。
- [ ] 在主仓库 CHANGELOG 的实际执行日期记录“依赖源码已导出并校验，运行挂载未切换”。生成的归档/报告放主仓库外，不批量加入 Git。

## 7. 后续切换是另一个操作窗口

本阶段完成后，再按总体计划处理以下步骤；不能因为副本校验通过就自动进行。

1. 停止源码写入，重新比较旧卷与已导出副本。若两者漂移，先查明修改来自哪边，创建新导出并验证；禁止直接覆盖新副本可能存在的人工修改。
2. 决定是否把主仓库完整工作副本归入 `src/daaam`。默认第一阶段没有做这件事；若采用总体方案，先处理所有当前编辑器/后台服务/Agent 的路径，再保留旧路径兼容链接。
3. 固定现有卷身份，修改 Compose 为完整 src bind mount，保持容器内 `/opt/daaam_ws/src` 路径，保留旧 .venv/build/install/cache。
4. 配置解析和挂载检查通过后，核对全部源码路径、Python 绑定和增量构建，再跑 30 帧 TUM 冒烟。修改/运行生成的测试文件只放新工作区且清理明确目标。
5. 新增必要 `just build`/`just check`，保持既有 just/TUM/CODa/回看行为。切换成功后指定新 WSL src 为唯一开发源，旧卷 src 停止编辑。
6. 记录实际验证结果与返回旧配置的方法，之后才考虑主仓库入口或 IDE 容器连接的进一步整理。

## 8. 异常处理与交接

| 现象 | 后续动作 |
|---|---|
| 卷/镜像不存在、引擎名异常 | 先确认是否连接了 Docker Desktop 的另一引擎，不自动创建卷或重新安装 |
| 目标目录已存在 | 检查状态标记、报告和用户修改，不合并、不覆盖 |
| 中途退出 | 保留 partial、临时目录和报告；不能继续发布。复核来源稳定后用新编号重做 |
| 文件权限不足 | 核对源 UID/GID、容器身份和目标所有权，只处理本次新目录，不 chown 旧 Docker 根目录 |
| Git 查询失败/外部 gitdir/子模块 | 先补全仓库关系和访问方式，不把错误过滤后继续 |
| 第一阶段需要撤回 | 不影响旧运行环境；保留导出目录并标记未启用即可，不需要清理或恢复 Docker |

交接必须给出：操作编号、实际目录、源镜像/卷、16 仓库状态、三份快照比较结果、归档摘要、是否发生并发写入，以及明确的 `EXPORTED_NOT_ACTIVE` 状态。未运行建图、未切挂载，就不要宣称 Docker 开发迁移完成。

参考：[Docker 卷只读挂载与备份](https://docs.docker.com/engine/storage/volumes/)、[宿主机与容器共享源码](https://docs.docker.com/engine/storage/bind-mounts/)。
