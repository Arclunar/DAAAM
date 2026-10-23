# DAAAM 工作约定

- 先读 jellymap_dev/AGENTS.md 与本仓库 README.md；这是上游 fork，保留必要改动，不做无关格式化或翻译。
- 本仓库负责算法、输入适配、建图运行与地图后处理。数据/GS 实现在 datasets_ws；回放工具、测试和结果在 replay_ws。
- scripts/、tests/、data/、datasets/、output/ 中的旧目录为绑定挂载、部分文件为兼容链接；编辑前确认实际位置，不能把它们当作重复副本删除。
- justfile 仅保留运行所需入口，不添加测试、冒烟或检查配方；开发验证直接运行原脚本。逻辑写脚本，justfile 只编排和转发。
- 整理后检查 TUM、Habitat、CODa、高斯输入建图、replay；公共路径或环境变化时五条均做冒烟，并注明未验证部分。
- 保护未提交修改与历史结果；新实验另建输出目录，不擅自重置、提交或清理模型/依赖卷。
- 只在 CHANGELOG.md 简记用户可感知的变化；纯内部整理写提交说明即可。不新增流水账、计划或交接文档；必要历史资料在 replay_ws/archive/。
- 用户说明只保留职责、命令、输出与限制，避免重复记录实测过程。
- 本地文档仅维护 README.md、AGENTS.md、CHANGELOG.md；保留 LICENSE 和第三方声明。用法、环境与限制统一写 README，不新增重复指南。


## Docker Python 依赖与 --rm
- DAAAM 当前 Python 虚拟环境位于容器 /opt/daaam_ws/.venv，整个 /opt/daaam_ws 挂载持久命名卷（当前 daaam_daaam_workspace）。docker compose run --rm 退出只删除容器，卷内 pip 安装的 Python 包仍保留。
- 添加 VizTracer 等 Python 工具时，先核对 Compose 的实际卷挂载和容器 sys.executable，再用该环境的 python -m pip 安装；通常无需重建 Docker 镜像，也不要为此重新下载 CUDA/PyTorch。
- 同步维护安装入口与用法，并用第二个新 --rm 容器验证持久性。宿主机安装的包不会自动进入容器。
- 系统 apt 包、未挂载路径内的安装不具有上述持久性；新建/更换卷或将镜像部署到另一台机器时，需重新安装或另行打包环境。不要把卷内安装误称为镜像已更新。
- 性能工具统一由 DAAAM docker/requirements-profile.txt 和 just daaam profile-install 安装；profile 生成 VizTracer JSON，profile-sample 生成 Pyinstrument HTML，profile-view 用本地 Perfetto 查看 JSON 或直接展示 HTML。两种采集器不要同时开启。run_pipeline.py 使用 os._exit，新增分析器必须在退出前显式保存，不能只依赖 CLI/atexit。
