# Changelog

## 当前功能
- Docker：宿主机源码挂载，复用现有 ROS/Hydra 编译环境与模型缓存。
- TUM：下载/转换、建图、参考网格和地图后处理。
- Habitat：实时输入建图、记录与回放；独立鱼眼/ROS 适配在 habitat_ws。
- CODa：数据准备、深度估计、bag、分段建图和后处理。
- GS：训练、渲染、实录转换和模型查看，源码与测试归 datasets_ws。
- Replay：时间轴、地图/对象查看、可选任务与问答，源码与测试归 replay_ws。

## 2026-10-20
- Agent `codex/01a15948-a9f6-74a1-a25b-7d5a3c13ba01/root`：新增 `just run <统一RGB-D目录> [帧数]`，运行器归 DAAAM，读取数据自身的深度比例/帧率，不依赖 Jellyhold 报告；原容器终端入口改为 `shell`。跨工作区归属与验证见 warmoon_ws/CHANGELOG.md。
- justfile 仅保留运行入口，移除测试、冒烟与检查配方；默认只列命令，根目录提供 tum-run 与 fisheye-run。
- 数据/回放工作区自行维护测试入口；参考网格 Open3D 依赖移出算法基础依赖。
- 修复迁移后的 Rerun 工具定位，保留旧命令入口；恢复核心文件上游注释，保持执行逻辑不变。
- 数据与 GS 工具、配套测试和本地数据移入 datasets_ws。
- 回放工具、配套测试、建图结果和服务状态移入 replay_ws，保留正在使用的兼容入口。
- 将长 shell 配方移入脚本，下载代理改由本机 .env 配置。
- 精简运行文档，将详细记录与旧计划归档；移除已确认的误产物与重复密钥文件。

原详细变更记录保存在 replay_ws/archive/daaam-20261020/CHANGELOG.md；原有未提交工作保持保留。
- 本地使用与环境说明合并到 README，移除重复指南和过时文档引用；保留许可声明。


## 2026-10-21 · 回放结果分享
- codex/01a15c69-be2b-76d3-8599-6741d6110620/root：回看页面增加“导出独立 HTML”，完整资源打包后可离线浏览；入口与限制见 ../replay_ws/README.md。新增 8 项导出测试、回放宿主机与容器回归通过，实测 75/138 快照离线加载；验证详情见 warmoon_ws 根 CHANGELOG。


## 2026-10-23 — DAAAM VizTracer
- Agent: codex/01a16749-4c50-7f51-bf2d-27dc42addc15/root
- 根/fork AGENTS.md 记录持久卷 Python 环境与 --rm；安装 VizTracer 1.1.1，无镜像重建。
- fork 安装要求/安装脚本、justfile、run_rgbd.py、run_tum_example.sh、run_tum_replay.sh、run_pipeline.py 和 README 接入可选主进程追踪及退出前保存；根系统 README 与任务归档同步。
- 验证：新 --rm 容器导入、布局/语法/diff、21 项 CLI/replay 测试通过；TUM 30 帧、8 快照、11514 trace 事件且无溢出，HTML 导出及 Windows HTTP 200。
- 全量回归 1116 项、55 skipped，daaam_cli/phonegs/gs_view_portal 三个 suite 未通过；未宣称全绿。未验证全段语义质量和其他数据链路新实跑。
- 证据：runs/reports/viztracer_01a16749/；任务 docs/completed/2026-10-23-daaam-viztracer-01a16749.md。

## 2026-10-25 · 重大更新：内存与 DAM 持久化
- Agent: codex/01a16d82-3f68-7d23-b9cb-344fc412dd58/root.
- Assignment-owned RGB history, explicit terminal results/failure handling, periodic atomic corrections, optional allocator diagnostics and offline native compaction/trim defaults. Safe C YAML backend with safe Python fallback.
- Validation: 1469 CV/Hydra frames completed without OOM; final graph and1096 descriptions saved. Full launcher failed143 because the validation time guard interrupted replay export; a separate recovery exported296 snapshots/219 verified chunks and RRD. Reconciliation separately generated162 groups without changing native graph/meshes. Core/host tests and limits recorded in root task report; semantic accuracy and native RRD playback unverified.
