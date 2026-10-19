# Docker 源码工作区与开发入口整理计划

> **最终状态：已完整切换并验证。** 全部 17 个源码仓库位于 WSL `daaam_ws/src`，Docker 使用整体 src 挂载，原主仓库路径通过持久 bind 保持兼容。实际实施以 `2026-10-17-docker-workspace-cutover-result.md` 为准；下文保留原设计与分阶段过程记录，其中原先的符号链接设计已由 bind 替代。

> **第一阶段结果：** 16 个依赖已复制到 WSL `daaam_ws/src`，源与副本一致，尚未切换挂载。详见同目录 `2026-10-17-docker-src-export-result.md`；后续先检查源码漂移，再进入本计划的切换阶段。

> **2026-10-17 执行顺序修订：** 用户确认先取出 Docker 中独有的依赖源码。先完整执行同目录 `2026-10-17-docker-src-export-runbook.md`，只复制核对 16 个依赖，保留主仓库物理路径和全部现有挂载。下文涉及移动主仓库、改变 Compose 与整理 just 的内容均推迟到后续切换窗口；不能在首次复制时自动执行。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 继续使用当前 Docker 开发环境，将容器 `/opt/daaam_ws/src` 的全部源码放入 WSL 普通用户目录，统一查看、编辑、搜索和进入环境的方式。

**Architecture:** WSL 保存完整源码树，整体 bind mount 到容器 `/opt/daaam_ws/src`。原命名卷仍挂载 `/opt/daaam_ws`，保留 `.venv`、`build`、`install`、`cache`、`vendor` 等；新增 src 挂载覆盖其中旧 src 的可见内容，不删除卷中的旧源码。容器内源码绝对路径保持原样。

**Tech Stack:** 现有 Ubuntu-20.04 WSL2、Docker Compose、daaam:jazzy-cu128-deps、ROS 2 Jazzy、现有 colcon/Python 环境。

**Spec:** 用户要求“规划整理入口，让 docker 里面 src 的文件夹可以全部暴露在 wsl 文件系统中”。本次只交付设计与执行清单，不执行移动、复制或挂载变更。当前优先走这条路线；原生 WSL24 迁移计划保留为后续备选。

## 目标布局

```text
WSL /home/arclunar/warmoon_ws/
├── DAAAM -> daaam_ws/src/daaam        # 原路径的兼容链接，验收后才切换
└── daaam_ws/
    └── src/                         # 日常编辑器打开这里
        ├── daaam/                   # 当前主仓库完整工作副本
        ├── daaam_ros/
        ├── hydra/
        ├── hydra_ros/
        ├── spark_dsg/
        ├── khronos/
        ├── gtsam/
        └── ...                      # 现有其他同级仓库

Docker /opt/daaam_ws/
├── src/                             # bind mount 上面的 WSL src
├── .venv/                           # 原命名卷
├── build/                           # 原命名卷
├── install/                         # 原命名卷
├── cache/                           # 原命名卷
└── vendor/                          # 原命名卷，属于 src 之外的依赖
```

Windows 资源管理器入口：`\\wsl.localhost\Ubuntu-20.04\home\arclunar\warmoon_ws\daaam_ws\src`。编辑器从 WSL 环境打开该目录，构建和运行在容器内完成；不能用 WSL20.04 的系统 Python 替代容器 Python 执行 DAAAM。

## 本次核对到的现状

- 主仓库：`/home/arclunar/warmoon_ws/DAAAM`，单独挂载为 `/opt/daaam_ws/src/daaam`。
- 其余源码位于 `daaam_daaam_workspace` 卷的 `src`，包含 config_utilities、daaam_ros、gtsam、hydra、hydra_ros、ianvs、khronos、kimera_pgmo、kimera_rpgo、pose_graph_tools、semantic_inference、small_gicp、spark_dsg、spatial_hash、teaser_plusplus、vision_opencv 共 16 个目录。
- 卷内还存在 `src/daaam` 挂载占位目录，不能把它当主仓库真实内容；主仓库必须从当前 WSL 工作副本取得。
- 此前测得卷内 src 约 269 MB；相比重装依赖，本方案只需复制少量源码。权重、数据和主仓库内其他资产的实际迁移范围另行清点。
- 当前 Compose 两个服务都使用相同的“卷 + 单独主仓库 bind mount”结构，需同步调整。
- 当前 `just run` 新建交互容器，`just exec` 进入正在运行的 daaam-ws 容器；裸 `just` 是 live-test，必须保留。
- 既有未提交修改包含本对话前一份迁移计划及 CHANGELOG，执行前重新核对，不重置工作区。

## 方案选择

推荐**整理为完整 WSL 源码目录后整体挂载**：所有现有仓库和后续新导入仓库都直接出现在 WSL 中，无需维护 16 个独立挂载。

若必须保持主仓库当前物理路径，替代方式是只把 16 个依赖复制到普通 WSL 目录，逐仓库 bind mount，主仓库沿用现有挂载，再用主机侧目录链接组成编辑器视图。该方式少一次主仓库位置调整，但每增加一个仓库都需更新挂载，不作为默认方案。

不把 `/var/lib/docker/volumes/.../_data` 直接作为日常开发目录，不用定时同步制造两份可编辑源码。最终只指定 WSL 新 src 为开发源码，卷内原 src 作为回退快照，不继续编辑。

## 阶段 A：盘点和复制

- [ ] 记录 Docker 引擎名称、镜像 ID、实际卷名与运行任务；只在没有构建/建图/编辑写入时做最终切换。暂停写入的时机在执行前说明，不擅自结束用户任务。
- [ ] 对主仓库及 16 个依赖逐个记录 HEAD、工作区差异、未跟踪/忽略文件、子模块和符号链接。根用户宿主 Git 的 safe.directory 行为此前未核验成功，优先用容器内匹配用户身份检查，失败必须记录，不把空输出判为 clean。
- [ ] 确认目标 `/home/arclunar/warmoon_ws/daaam_ws/src` 没有冲突，准备临时目录。复制依赖仓库完整工作副本和 `.git`，保留权限、可执行位、时间戳和链接；不要重新 clone 替代复制本地修改。
- [ ] 从原 WSL DAAAM 取得真实主仓库，不复制卷内 daaam 占位目录。先计算文件数、大小与关键摘要，对比迁移前后清单及 git 状态。
- [ ] 主仓库在同一文件系统时优先同文件系统改名迁入最终 `src/daaam`，避免重复拷贝大数据。必须跨文件系统复制时先完整核对，再切换；未验收不删除旧副本。
- [ ] 原 `/home/arclunar/warmoon_ws/DAAAM` 建立指向新位置的兼容链接。切换前记录当前编辑器、终端、后台服务的工作路径；切换后重新打开入口，验证 Windows UNC 访问和依赖主仓库绝对路径的工具。

## 阶段 B：改变源码挂载，保留原依赖卷

计划修改 `compose.yaml` 的两个服务，源码挂载设计如下（示意，实施时补长语法并检查路径）：

```yaml
volumes:
  - daaam_workspace:/opt/daaam_ws
  - ${DAAAM_SRC_DIR}:/opt/daaam_ws/src
  # 其他 datasets / data / GUI 挂载沿用原配置
```

- [ ] 删除原 `.:/opt/daaam_ws/src/daaam` 条目，用 src 整体挂载替代；不得同时叠加两份主仓库源码。
- [ ] 用 Compose 长语法 `type: bind` 和 `bind.create_host_path: false`，环境变量使用必填校验，防止路径错误时自动创建空目录并遮住旧源码。
- [ ] 用户本地 `.env` 定向加入 `DAAAM_SRC_DIR=/home/arclunar/warmoon_ws/daaam_ws/src`，不打印或重写其中密钥；`.env.example` 记录配置格式。
- [ ] 明确固定原卷身份：`daaam_workspace` 指向已验证存在的 `daaam_daaam_workspace`，可用 `external: true` 与可配置 name，避免主仓库路径/Compose 项目名变化后创建一个空卷。通用首次安装流程同时说明如何显式创建新卷。
- [ ] 固定 Compose 项目选择规则，避免 `just exec` 命中其他副本；不要硬编码全局容器名。
- [ ] 先运行 `docker compose config --quiet`，再核对解析后的挂载源和卷名（仅输出这些字段，不输出可能含密钥的完整配置）。确认 image/build 字段不触发无意重建。
- [ ] 创建新容器确认挂载正确。原卷 src 会被新 bind 隐藏，仍保留原数据；容器路径不变有利于继续使用现有 build/install/venv，但需用运行与构建验证，不能预先承诺全部二进制无影响。

## 阶段 C：整理开发入口

只补足必要命令，避免把当前所有命令迁到一个长期后台容器：

| 入口 | 计划行为 |
|---|---|
| 编辑源码 | 打开 WSL `daaam_ws/src`，所有仓库一起搜索和编辑 |
| `just run` | 保留当前行为，进入已加载 ROS/venv/工作区的交互容器 |
| `just exec` | 保留进入运行中容器的用途，按当前 Compose 项目和服务定位 |
| `just build [packages...]` | 默认编译工作区；指定包时增量构建，复用旧 build/install；成功后新开运行终端加载新安装空间 |
| `just check` | 验证挂载、源码路径、GPU/关键导入，正确返回失败退出码 |
| 原 TUM/CODa/回看命令 | 保留原名称、参数、数据位置和默认行为 |
| 裸 `just` | 继续 live-test |

- [ ] 更新 `justfile`、必要的开发辅助脚本、`DOCKER.md` 和 `USAGE.md`，解释“WSL 编辑，容器运行”。每次执行写操作前核对当前用户修改。
- [ ] 对 Hydra/DAAAM-ROS C++ 及 Python 绑定明确提示重新构建；普通 editable Python 代码通常在新进程生效，长驻进程需要重启，不承诺所有改动热更新。
- [ ] 只对挂载校验、卷选择、build 参数传递和失败退出码增加必要测试，不编写仅重复配置文本的测试。
- [ ] 若后续需要 IDE 的容器解释器/语言服务，再单独配置；本次先解决完整源码可见与命令入口，不把 IDE 安装作为前置条件。

## 阶段 D：验收与回退

- [ ] 对比全部仓库的文件清单、摘要、git HEAD/差异；验证普通用户在 WSL 和容器里都能写一个临时文件并立即从另一侧读到，删除临时文件。
- [ ] 验证 UID/GID 和可执行位，不对整个 Docker 数据根做 chown。
- [ ] 检查容器中 `/opt/daaam_ws/src` 包含真实主仓库和全部同级仓库，且 `.venv/build/install/cache` 仍来自原卷。
- [ ] 验证 `hydra_python`、`spark_dsg`、`daaam`、ROS 接口导入、模型路径、CUDA 运算，检查现有绑定/安装链接没有指向失效位置。
- [ ] 跑一次无源码修改的增量构建，确认未意外全量重编；若确有受影响包，先定位路径/时间戳原因，再只重编必要包。
- [ ] 跑 TUM 30 帧无 DAM 冒烟，检查新结果及回看；按实际改动范围再测 ROS 节点/可视化，不把仅宿主文件可见视为开发流程已通过。
- [ ] 验证旧 DAAAM 路径兼容链接和新 src 的 Windows/WSL 编辑入口，检查已有测试与裸 just 的行为。
- [ ] 对本次实际修改同步写 CHANGELOG，记录验收和回退方式。

回退时先停止新挂载方式的任务，恢复原 Compose 源码挂载和原主仓库位置；主仓库切换后若有新编辑，先保存新状态，不从旧副本覆盖。旧卷 src 仍在，但依赖若切换后有新编辑也必须保留并核对，不能简单回到旧快照丢掉修改。回退计划不包含 `docker compose down -v`、prune 或删除旧卷。

## 本次范围和预计收益

本次仅新增本计划和对应 CHANGELOG 条目，未迁移主仓库、未复制卷数据、未修改 Compose/justfile、未运行新挂载环境。

预期无需重新安装 ROS/CUDA/Python 依赖，无需重新下载模型。实施投入主要在源码状态核对、一次目录切换、挂载配置和小规模验证；源码暴露本身不提高推理速度，但会减少进入容器查代码、定位依赖和跨仓库编辑的操作。

参考：
- Docker bind mount 用途：https://docs.docker.com/engine/storage/
- 固定现有卷身份：https://docs.docker.com/reference/compose-file/volumes/
- Compose 项目命名规则：https://docs.docker.com/compose/how-tos/project-name/
