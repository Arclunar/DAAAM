# WSL2 Ubuntu 24.04 原生 DAAAM 迁移实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新建 Ubuntu 24.04 WSL2，将当前 Docker 中已经配置的 DAAAM 工作环境迁为原生开发环境，复用源码、模型和依赖经验，并保留可返回的旧环境。

**Architecture:** 新 WSL 的虚拟磁盘放 E 盘，Linux 依赖与工作区放新发行版自己的 ext4 文件系统。工作区继续使用 `/opt/daaam_ws`，源码放 `/opt/daaam_ws/src/daaam`，尽量减少既有路径变化；大体积 CODa 数据和历史结果继续使用 E 盘。重建系统依赖、Python venv 和工作区绑定，复用已有源码、模型缓存及可验证的下载产物。

**Tech Stack:** WSL2、Ubuntu 24.04、ROS 2 Jazzy、Python 3.12、CUDA 12.8 工具链、PyTorch cu128、TensorRT cu12、colcon。

**Spec:** 本次用户请求：规划新建 24.04 WSL2 后迁移当前 Docker 环境，利用已有依赖踩坑记录提高效率。本文件交付规划；其中未勾选内容均未执行。

## 全局约束

- 不原地升级 Ubuntu-20.04；新旧发行版并存，验收前不清理旧 Docker 镜像、卷、仓库和数据。
- 不改变 Windows GPU 驱动，不在 WSL 安装 Linux NVIDIA 显示驱动。
- 本任务只新增计划和对应 CHANGELOG 记录。实现原生入口、安装系统及重建环境属于后续执行阶段。
- 本次开始核查时主仓库有两个既有未跟踪文件：`scripts/replay_web/artifacts.py`、`tests/test_replay_artifacts.py`，后续状态已变化；迁移时再次核对并保存整个工作副本，不能只 clone 或仅保存已提交文件。
- 实施前重新检查主仓库和每个同级/vendor 仓库的 commit、dirty 状态、未跟踪文件、子模块、符号链接和所有权。旧卷内仓库状态本次未成功核验，不能视为 clean。
- 每次后续仓库修改都要同步记录 CHANGELOG。不要提交、覆盖或清理其他任务的修改。
- 旧、新管线不能同时占用 GPU；编译时也需考虑两个发行版共享整机资源。

## 1. 本次只读核查结果（2026-10-17）

| 项目 | 当前观察 |
|---|---|
| WSL | 2.4.8.0，仅看到 Ubuntu-20.04，运行于 WSL2；本机帮助确认支持 `--location` |
| Docker | 当前连接引擎名称 MS0096；核查时没有运行中的容器 |
| Windows 可用空间 | C 约 72 GB，D 约 34 GB，E 约 245 GB；执行前复查 |
| 用户 `.wslconfig` | 本次未找到该文件；不要把旧日志中的内存/swap 数字当当前配置 |
| Docker 卷 | `/var/lib/docker/volumes/daaam_daaam_workspace/_data` |
| 卷内源码/vendor | `src` 269 MB，`vendor` 301 MB |
| 卷内 Python | `.venv` 约 15 GB，Python 3.12.3，`include-system-site-packages=true` |
| 卷内缓存 | `cache` 约 11 GB，主要是 Hugging Face；没有发现 `cache/pip` |
| 卷内构建 | `build` 641 MB，`install` 85 MB |
| 主仓库数据 | `checkpoints` 2.2 GB，`datasets` 2.5 GB，`output` 1.6 GB；du 默认未追随外部目录符号链接，不代表 CODa 总量 |
| 外置数据 | `E:\daaam-data` 下已有 CODa、FoundationStereo、hf_cache、output、tum 等目录 |
| 结果链接 | `output/coda -> /data/output/coda`；新环境需保留该路径含义 |

现有 Python 分发元数据记录到的版本：

```text
torch==2.8.0+cu128
torchvision==0.23.0+cu128
torchaudio==2.8.0+cu128
tensorrt-cu12==10.13.3.9
transformers==4.55.4
huggingface-hub==0.36.2
pyarrow==23.0.1
cvxpy==1.7.5
cvxopt==1.3.3
opencv-python==4.11.0.86
rerun-sdk==0.23.1
sam2==1.1.0
gradio==6.30.0
gradio-client==2.7.2
pydantic==2.14.1
open3d==0.19.0
```

以上来自旧卷的包元数据，不是本次模型加载/运行验证，也不是完整 lock。NumPy 等包还可能来自系统 site-packages；完整清单必须在旧容器环境中导出。旧环境部分包故意通过 `--no-deps` 安装，不能把 `pip freeze` 当作一次正常依赖求解即可重装的 requirements。

## 2. 已有记录如何直接提高效率

权威顺序：当前可用环境的实际清单和源码状态 → 当前安装辅助脚本 → progress.md 历史记录 → 上游 INSTALL.md。出现矛盾时核对当前代码和运行结果。

| 既有材料 | 迁移时的用途 |
|---|---|
| `progress.md` 的“发现的上游问题”与后续命令记录 | 解释失败原因、识别静默失败和过时结论 |
| `docker/setup_workspace.sh` | 已落地的安装顺序、依赖冲突规避、构建参数和分阶段执行 |
| `docker/Dockerfile`、`docker/apt-deps.txt` | ROS、编译工具、图形库及 rosdep 系统包清单；新 WSL 不包含镜像原有的 CUDA/cuDNN 层，须另补 |
| 卷内 `workspace.lock.yaml` 和各仓库 `.git` | 复用精确源码版本，避免重新拉取漂移中的分支 |
| 卷内 `colcon_defaults.yaml`、`constraints.txt` | 复用实际优化后的构建参数和版本约束；仓库里的默认 colcon 文件不是最终调优版 |
| 卷内 `vendor/` | 复用 BoxMOT、DAM、perception_models、spark_dataset_interfaces 源码 |
| `scripts/check_deployment.py`、`scripts/summarize_run.py` | 检查真实 CUDA 运算、DAM 加载和最终输出，避免仅凭退出码宣称成功 |

重点保留的依赖处理：

1. **不要直接执行上游 `install/install.sh`。** 它未完整涵盖当前已经验证的安装路径，特别是缺少 `hydra_python` 单独安装。
2. **venv 必须用 `--system-site-packages`。** ROS 的 rclpy、系统 OpenCV 等与 apt 包有关；不向系统 Python 执行普通 pip 安装，也不用 Conda 替换 ROS 的解释器。
3. **先固定 torch 三件套 cu128，再装 SAM2。** SAM2 用 `SAM2_BUILD_CUDA=0` 和 `--no-build-isolation`，避免隔离构建重新拉 CUDA 13 torch。若后续要启用 SAM2 CUDA 扩展，应单独验证。
4. **固定 `tensorrt-cu12==10.13.3.9`。** 不装裸 `tensorrt`/`nvidia-tensorrt` 元包；先匹配旧版本，再测试旧 engine。
5. **NumPy 保持 `<2`，pyarrow 保持 23.0.1。** 优先锁旧环境的 cvxpy、rerun、OpenCV，避免 pip 长时间回溯或安装不兼容 ABI。
6. **DAM、perception_models 按现有脚本分离依赖并 `--no-deps` 安装。** DAM 的声明约束与当前 Pydantic/Gradio 组合并不完全一致；保留其已知例外的说明，不能用一次全量 pip 升级“修复”。
7. **Gradio 虽不是此次交互界面目标，仍需能 import。** DAAAM 的 DAM 服务在模块级导入它；沿用脚本的 Gradio 6.30.0、client 2.7.2、`huggingface-hub<1` 与小依赖补装方式。
8. **补 cvxopt 并检查 `GLPK_MI`。** 只装 cvxpy 不够；系统同时要有 GLPK 库。
9. **构建先 GTSAM，再其余包，再装工作区 Python 包。** Release，关闭 tests/examples，初始 `GTSAM_JOBS=2 OTHER_JOBS=2 COLCON_WORKERS=1`，根据内存余量加并行。
10. **保留 Khronos/small_gicp 的补编处理。** 历史记录是上游依赖声明遗漏；先核对复制来的当前源码是否仍如此。必要时 source 已完成的工作区再补编。
11. **模型缓存不能只复制权重文件名。** HF 缓存保留 blobs/snapshots/refs 和相对符号链接，BoxMOT CLIP 缓存同样迁移；DAM 必须验证确实从本地快照加载并产生描述。
12. **标签文件记录有矛盾。** INSTALL.md 称构建生成，而 progress.md 和当前 TUM 脚本表明可在运行时创建；应跟随当前入口的生成逻辑，不把其缺失误判为 C++ 构建失败。
13. **不要照抄历史代理 IP。** 当前 WSL 已打印 localhost 代理映射提示；新发行版重新测试代理可达性，保留对 localhost/127.0.0.1/::1 的代理绕过。不要为了迁移顺带切换全局 WSL 网络模式。
14. **图形环境重新验收。** RViz 使用 WSLg；旧环境原生 Rerun 的图形故障不代表新 WSL 已自动解决，浏览器回看仍是现成验收入口。

## 3. 路径和资产策略

| 资产 | 目标和处理 |
|---|---|
| 新 WSL 虚拟磁盘 | 建议 `E:\WSL\Ubuntu-24.04`，执行前验证目录不存在、容量足够 |
| 工作区 | `/opt/daaam_ws`，由新普通用户拥有；ROS/system 包由 apt 管理 |
| DAAAM 主仓库 | `/opt/daaam_ws/src/daaam`，复制当前工作副本及 `.git`、未跟踪文件、必要忽略资产 |
| 同级仓库/vendor | 从卷原样复制源码和 git 元数据，记录 commit 与本地修改，不更新远端分支 |
| Python | 新建 `/opt/daaam_ws/.venv`，按旧版本和分阶段例外重装 |
| build/install | 默认重新编译；绝不把复制旧 build/install 直接当作迁移成功依据 |
| checkpoints/TUM | 复制到新工作区，保留文件时间和相对链接；复用已转换的 TUM，避免重新下载转换 |
| HF/torch 缓存 | 优先复制到 `/opt/daaam_ws/cache`；11 GB 缓存保留链接结构，不复制成重复实体文件 |
| CODa/外置输出 | `/data -> /mnt/e/daaam-data`，继续复用已有 CODa/bag/深度/模型；不重复拷贝整个数据集 |
| TUM 兼容路径 | `/datasets -> /opt/daaam_ws/src/daaam/datasets`，保证现有默认路径可用 |
| HF 外置缓存 | 识别 `/data/hf_cache` 与工作区 HF cache 中实际模型分布，保留现有跨缓存链接；不能假定所有模型均在 11 GB 内 |
| `.env`/密钥 | 单独检查和定向迁移；不要批量 source Docker 的 `.env`，不要将密钥写入计划、锁文件、日志或提交 |

同 GPU、同 TensorRT/CUDA 组合下，现有 `.engine` 值得先做实际加载/推理验证；无需预先全部重导出。若反序列化或推理失败，再用保留的原始权重在新环境导出。venv 的脚本、editable 安装、系统库和绝对路径存在耦合，保留相同路径也不能替代重建验证。

空间先预留 **60–80 GB 的迁移预算**，包括新系统/依赖、15 GB 量级的 venv、模型、源码和临时下载；这是预算而非实测目标占用。若额外制作完整 wheelhouse/备份，另算空间。旧 Docker 镜像不必再次 save/load，避免额外几十 GB 临时占用。

## 4. 实施步骤与验收门槛

### 阶段 A：固定当前环境，先准备再安装

- [ ] 复查 `wsl --version`、`wsl --list --verbose`、本地安装命令帮助、三盘空间、当前 RAM/swap、Docker 引擎名称和运行中任务。
- [ ] 在 E 盘创建独立迁移暂存目录，记录日期、主仓库 HEAD/dirty/untracked、同级/vendor 仓库状态和精确 commit；记录可能不准确的历史 workspace.lock 与实际 HEAD 的差异。
- [ ] 从实际旧镜像加旧卷启动临时只读核查环境，导出 `/etc/os-release`、Python/CUDA/cuDNN 信息、`dpkg-query` 包版本、`python -m pip freeze --all`、`pip list --format=json` 和 `pip check` 的原始输出。该步骤须记录镜像 ID、挂载和基线异常；不能用 Ubuntu 20.04 宿主解释器执行卷内 3.12 venv。
- [ ] 将实际版本转成可安装的约束：普通第三方包锁版本；editable/本地路径/VCS 包单列，映射到迁移后的源码。`pip check` 已知元数据冲突形成明确例外，不要求不可能的“全绿”，也不忽略新增冲突。
- [ ] 检查每个旧 venv 的 direct_url 元数据和 vendor HEAD，保留构建源码；从 cache 中核对模型清单及关键文件摘要。
- [ ] 从旧环境先生成 torch/TensorRT/NumPy/pyarrow 等“大包优先”的兼容 wheel 下载目录，下载目标为 Linux x86_64 / CPython 3.12，并保留 cu128 的 extra-index。VCS/本地源码不当作普通版本号下载。
- [ ] 无 pip 缓存时优先下载关键大包即可，不强制离线构建全量 wheelhouse。需要源码编译的包按当前分阶段方式构建，避免一次 wheel 全部 requirements 再次触发 DAM/SAM2 冲突。
- [ ] 暂存或跨发行版传输源码、权重、缓存；只复制所需资产，不复制整个 Docker 数据根。数据归属、软链和摘要清单单独保存。

**通过条件：** 可以明确回答“正在迁移哪个版本、哪些本地修改、哪些模型、哪些包存在已知冲突”，关键资产有可核对清单。

### 阶段 B：在 E 盘创建官方 Ubuntu 24.04 WSL2

- [ ] 执行前复查本机 `wsl --help`。本次已确认当前 WSL 2.4.8 帮助包含 `--location`，可以优先采用指定 E 盘位置的直接安装方式。
- [ ] 支持时使用下列 PowerShell 安装命令；不支持时，从 Ubuntu 官方渠道取得兼容的 Ubuntu 24.04 WSL rootfs，验证校验和后通过 `wsl --import Ubuntu-24.04 E:\WSL\Ubuntu-24.04 <已验证rootfs的实际路径> --version 2` 导入。执行方案需记录实际下载 URL、摘要和文件路径，不能把 Docker export 当官方 WSL 镜像。

```powershell
# 仅在本机帮助明确支持 --location、目标路径核验后执行：
wsl --install -d Ubuntu-24.04 --location E:\WSL\Ubuntu-24.04
wsl --list --verbose
wsl -d Ubuntu-24.04
```

- [ ] 创建普通用户，建议继续使用 arclunar；确认 UID/GID，设置默认用户（import 方案尤其需要）。确认 `VERSION_ID=24.04`、发行版 version=2、磁盘实际位于 E 盘。
- [ ] 检查 WSLg、Windows 驱动映射和 `nvidia-smi`；复用现有 Windows 驱动。若需更新 WSL 组件/重启，安排到旧任务空闲时，不自动 shutdown 全部发行版。
- [ ] 验证网络/代理和 Ubuntu/ROS 软件源；不硬编码旧网关。新环境安装原生 DAAAM不需要安装 Docker。
- [ ] 依据当前机器可用内存安排构建；若要调整 `.wslconfig`，单独说明它影响全部 WSL2 发行版及重启时机，不在新发行版里擅自覆盖全局设置。

**通过条件：** 新 24.04 WSL2 独立启动、普通用户正常、E 盘位置正确、GPU 可见、WSLg/网络可用，20.04 可继续启动。

### 阶段 C：恢复系统依赖和源码

- [ ] 配置 ROS 2 Jazzy 官方 apt 源，安装基础 ROS/开发工具、Python 3.12 venv、构建工具和图形库，参考当前 Dockerfile 的实际安装清单。
- [ ] 以 `docker/apt-deps.txt` 为已知清单，再执行 rosdep 检查/补装。不要把旧 `dpkg` 全量列表不加筛选地安装到 WSL；其中可能含容器特有软件和间接依赖。
- [ ] 补齐旧 CUDA 镜像提供的用户态工具/库，按导出的版本比对。需要 nvcc 时安装 `cuda-toolkit-12-8`，cuDNN 按实际 ABI 补齐；不使用会引入 Linux 驱动的 cuda/cuda-drivers 元包。pip 的 torch 自带运行库与系统 CUDA 工具链分开核验。
- [ ] 创建用户可写的 `/opt/daaam_ws`，复制源码和模型缓存，修复所有权；用 rsync/tar 保留链接和时间，不沿符号链接复制整个外置数据目录。
- [ ] 创建 `/data` 和 `/datasets` 路径映射前验证目标不存在或符合预期；不覆盖已有目录。检查 `output/coda` 和模型缓存链接在新环境中解析正确。
- [ ] 复制实际调优后的 colcon 配置和 constraints，核对所有源码 commit/差异及关键模型摘要。

**通过条件：** ROS 可 source、rosdep 系统依赖齐全、源码和模型清单吻合、所有必要链接可解析。

### 阶段 D：分阶段重建 Python 与 C++

在新 WSL 使用以下统一环境（这是目标配置，实施时核对后再写入专用 `env.sh`，避免污染全局 shell）：

```bash
export DAAAM_WS=/opt/daaam_ws
export COLCON_DEFAULTS_FILE="$DAAAM_WS/colcon_defaults.yaml"
export HF_HOME="$DAAAM_WS/cache/huggingface"
export TORCH_HOME="$DAAAM_WS/cache/torch"
export XDG_CACHE_HOME="$DAAAM_WS/cache"
export PIP_CACHE_DIR="$DAAAM_WS/cache/pip"
export CODA_ROOT_DIR=/data/CODa
export ROS_DISTRO=jazzy
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
source /opt/ros/jazzy/setup.bash
python3 -m venv --system-site-packages "$DAAAM_WS/.venv"
source "$DAAAM_WS/.venv/bin/activate"
```

- [ ] 提供原生安装专用入口/约束注入方式，再复用 `docker/setup_workspace.sh` 的分阶段逻辑。该脚本不要求 Docker，但原先依赖 entrypoint 自动激活 venv，且 `pip-deps` 会重写两项 constraints，不能直接照跑就声称遵循完整版本锁。
- [ ] 安装构建所需 empy/catkin_pkg/lark/yaml；固定旧 torch 三件套，按既有方式安装 SAM2、普通包、DAM/perception_models、Gradio 补充项和 cvxopt。普通包安装贯穿完整迁移约束。
- [ ] 用本地 vendor 源码安装，不重新拉 Git 分支；当前 vendor 脚本会复用已有 `.git` 目录而不 fetch/pull，实施时确认该行为及目录映射仍一致，不能自动覆盖迁移来的本地修改。
- [ ] 在依赖安装稳定后构建 GTSAM，再构建其余包；初次迁移不边改同一 venv 边编译。可并行准备下载包和复制独立数据，但不要让编译看到安装一半的 Python 环境。
- [ ] source 新 `install/setup.bash` 后单独安装 spark_dsg、spark_dataset_interfaces、hydra_python、daaam；为每一步保存日志与真正退出码。
- [ ] 比较旧/新包版本、系统 site-packages 和 pip check。Open3D 必须单独实际 import/重建检查：旧环境有 `--no-deps` 安装及局部导入兼容处理，不假定全量 import 已通过。

**通过条件：** CUDA 张量计算成功、关键包导入成功、`GLPK_MI` 存在、colcon 没有失败包、Hydra Python 可运行。现有 setup 脚本的部分检查仅打印 FAIL 不返回非零，验收不能只读脚本退出码。

### 阶段 E：补齐原生运行入口

- [ ] 新增专用环境加载文件和原生 justfile（建议 `justfile.native`），复用既有 TUM/CODa shell/Python 脚本，保留旧 justfile 便于返回旧环境。不要在本次规划中提前修改命令行为。
- [ ] 原生入口覆盖：进入环境、TUM 冒烟/完整运行/后处理、CODa bag/运行/分段/后处理、回看服务、测试；逐一检查原先由 Compose 注入的缓存、数据路径、ROS domain/RMW、fp16 和内存控制变量。
- [ ] 检查 `run_live_preview.sh` 等脚本内部的 Docker 调用与固定 `/opt/daaam_ws`、`/datasets`、`/data` 路径；不能只替换 justfile 第一层 Docker 命令。
- [ ] 确认浏览器测试从新副本运行：当前 `tests/replay_browser_support.py` 会根据 UNC 路径解析发行版并启动相应 WSL fixture，并非硬编码 Ubuntu-20.04；验证它解析到新路径。路径单测中 Ubuntu-20.04 是显式测试样例，不应全部替换。Chrome/Windows 测试依赖继续使用原来的 Windows 环境。
- [ ] 旧测试里对 Docker 命令文本的断言保留旧模式验证；新增原生模式行为覆盖。先用 `just -f justfile.native ...` 验收，新环境日常默认入口在验收后统一。

**通过条件：** 新环境的正常开发、运行和回看流程无需 Docker；旧环境入口仍可使用。

### 阶段 F：从小到大验收，再切换

- [ ] 新旧环境采用相同数据、参数、模型与可视化条件，给新结果独立目录，不覆盖旧结果。
- [ ] 运行部署检查、真实 TensorRT engine 加载及小规模推理，GLPK 求解器检查，必要的包导入检查。
- [ ] 先跑 TUM fr1_desk 30 帧无 DAM，再跑 298 帧带 DAM；通过最终文件和描述数量确认 worker 工作，打开新生成的 replay/ 数据包和图像。
- [ ] 单独验证 RViz、ROS graph/topic/TF/clock，随后跑一个现有 CODa 短 bag；TUM 离线路径通过不能代表 ROS 2 CODa 链路通过。保留原有内存/标签/fp16/分段参数，不在迁移时混入参数优化。
- [ ] 比较实际处理帧数、丢帧、峰值 RAM/显存和耗时，注明 DAM 异步结果可有差异；没有可比运行前不声称提速。
- [ ] 完成与原生入口相关的测试及必要浏览器验收。只把已完成项目勾选，记录失败和复现日志。
- [ ] 将编辑器/终端和日常文档指向新发行版；验收后再决定默认发行版。现有部署包可能早于 Open3D/回看更新，不能拿历史部署包代替当前工作副本。

**返回旧环境：** 停止新环境任务，用 `wsl -d Ubuntu-20.04` 返回原仓库及原 Docker 命令。暂不删除新旧任一环境；两边源码此后可能分叉，指定新工作区为主后再开始开发。

## 5. 工作量与优先顺序

效率收益最大的顺序是：**固定实际版本和本地改动 → 复制 11 GB 模型缓存/现成数据 → 复用 vendor 源码 → 分阶段安装 → 原生入口 → 小规模验收。**

网络正常时，建议安排半天窗口：安装系统/依赖、复制和构建通常可在数小时内推进，实际耗时取决于 wheel 下载、代理、内存及原生入口适配。不要把历史一次编译耗时作为这次时限保证。第一轮先保住 TUM + ROS 短 bag 的可用性，长序列与扩展实验在迁移验收后开展。

本次没有安装 Ubuntu 24.04、导出完整包锁、制作 wheelhouse、移动模型或运行新环境；这些均为待执行步骤。

## 官方核对来源

- WSL 安装与参数：https://learn.microsoft.com/en-us/windows/wsl/basic-commands
- Linux 文件系统性能建议：https://learn.microsoft.com/en-us/windows/wsl/filesystems
- ROS Jazzy Ubuntu 安装：https://docs.ros.org/en/jazzy/Installation/Ubuntu-Install-Debs.html
- CUDA on WSL（驱动边界与 toolkit 元包）：https://docs.nvidia.com/cuda/archive/13.0.0/wsl-user-guide/index.html
