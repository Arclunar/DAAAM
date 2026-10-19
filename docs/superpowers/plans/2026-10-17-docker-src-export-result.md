# Docker 依赖源码导出执行结果

> **后续状态更新：** 导出阶段之后已完成整体源码挂载和开发入口切换。当前活动状态及验证结果见 `2026-10-17-docker-workspace-cutover-result.md`；本文件保留首次导出时的证据，不代表当前仍未启用。

日期：2026-10-17（Asia/Shanghai）  
操作编号：`20261017_103512`  
状态：**EXPORTED_NOT_ACTIVE（已导出、未切换挂载）**

## 实际完成

- 将原 Docker 卷内的 16 个依赖仓库完整导出到 `/home/arclunar/warmoon_ws/daaam_ws/src`。
- 保留 `.git`、隐藏文件、工作树内容、文件模式、修改时间和硬链接关系。导出前后 16 个仓库均无 Git 工作区修改，HEAD 和状态一致。
- 源卷只读访问，无联网下载、不加载模型、不启动管线；使用现有镜像完成导出和核查。
- Windows UNC 列目录成功：`\\wsl.localhost\Ubuntu-20.04\home\arclunar\warmoon_ws\daaam_ws\src`。
- 原主仓库仍为 `/home/arclunar/warmoon_ws/DAAAM`，不是兼容链接；新目录中故意没有 `src/daaam`。
- `compose.yaml`、`justfile`、`docker/entrypoint.sh` 的 SHA-256 在操作前后相同。依赖、缓存、编译产物和原卷源码未迁移或清理。

## 来源与证据

- Docker 引擎：`MS0096`；导出开始时无运行中的容器。
- 卷：`daaam_daaam_workspace`。
- 镜像：`sha256:c1c1d352f01484d0ba246f711fc51e68f39971829c399db97f7731e33aa2269a`。
- 证据目录：`/home/arclunar/warmoon_ws/.daaam-source-export/20261017_103512`。
- 操作记录和执行器副本：`context.json`、`executor.py`。
- 三份快照：`source-before.json`、`source-after.json`、`destination.json`。
- 汇总：`summary.json`；归档：`src.tar`、`src.tar.sha256`；归档成员和元数据清单一并保留。
- 状态标记：`/home/arclunar/warmoon_ws/daaam_ws/SOURCE_EXPORT_STATUS.txt`。

## 验证数值

| 项目 | 结果 |
|---|---|
| 仓库数 | 16 |
| 文件及目录条目数 | 8,720 |
| 普通文件数 | 7,512 |
| 普通文件内容总字节数 | 257,682,795（约 246 MiB） |
| 归档字节数 | 273,100,800（约 260 MiB） |
| 源复制前与复制后 | 完全一致 |
| 源与解压副本 | 完全一致 |
| 权限、UID/GID、mtime_ns | 一致；目标属于 1000:1000 |
| 硬链接分组 | 一致 |
| 符号链接 | 本次源码树中未发现 |

三份快照的 SHA-256 均为：

```text
a7c1b766ad95faf66c067437c43a353cc6bde9c08fb321ba3700dd3d50866fa1
```

归档 SHA-256：

```text
1b3a3012134f9e00875ee65630be94e5778834744f88f2d02f89cd44f2f0061e
```

## Git 版本

完整 HEAD 和状态在快照及 summary.json 中；以下是便于查阅的短版本。

| 仓库 | HEAD 前12位 |
|---|---|
| config_utilities | 1df5f6b5444f |
| daaam_ros | b80964647efe |
| gtsam | 3985b08a8237 |
| hydra | f9fc834b07f1 |
| hydra_ros | 5e6f14bf6395 |
| ianvs | 36f09956de88 |
| khronos | 3b961d6c6b43 |
| kimera_pgmo | 382b1f3530f4 |
| kimera_rpgo | 222ad5e863da |
| pose_graph_tools | 965dfe864487 |
| semantic_inference | 96e5dd0b3021 |
| small_gicp | fa0cfc983417 |
| spark_dsg | 39f1c2e28789 |
| spatial_hash | 8045892f3497 |
| teaser_plusplus | 52a9c52ee7d4 |
| vision_opencv | 50f6f1feccc6 |

## 子模块核对与计划细化

原手册遇到 `.gitmodules` 即要求先检查。本次已核对：Hydra 和 Spark-DSG 的该文件为空，没有 Git 子模块条目；semantic_inference 声明 Mask2Former 子模块，gitlink 为 `9b0651c6c1d5b3af2e6da0589b719c514ec0d69a`，当前未初始化、对应工作树目录为空。

执行器据此将子模块状态与 gitlink 写入快照，原样保留未初始化状态，没有运行 `git submodule update`。导出是当前实际源码状态的完整副本，不声称包含未曾下载的 Mask2Former 源码。不存在已初始化子模块或外部 gitdir 的复制遗漏。

## 下一步边界

现在可以浏览、搜索新目录，但继续编辑和构建仍以原 Docker 卷源码为准。未更改挂载、未做新挂载环境构建或建图验收，不能把本结果视为完整开发入口切换。

后续执行总体计划的切换阶段前，重新比较原卷与导出副本，确认没有漂移或新副本人工修改，再处理主仓库路径和 Compose 配置。保留原卷、归档和报告即可返回原使用方式；本次无需执行任何恢复或删除命令。
