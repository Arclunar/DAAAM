# DAAAM 跨服务器部署

开发机源码已切换为 WSL 整体 src 挂载：`build_full_image.sh image` 会读取当前 Compose 配置，
从活动 WSL src 打包同级仓库，并从原卷复用 venv/build/install；卷内旧 src 不进入新镜像。
本次入口整理验证了源码组装逻辑，没有重新导出几十 GB 的镜像；已有部署包仍为历史版本。

把这台机器上已经配好的 DAAAM 环境整体搬到另一台 GPU 服务器,**不用在服务器上重新编译、重新装依赖、重新下载模型**。

| 在服务器上省掉的事 | 本机实测耗时 |
|---|---|
| 编译 C++ 工作区(gtsam、Hydra、Khronos 等 30 个包) | 约 30 分钟 |
| 装 Python 依赖(torch、TensorRT、dam、boxmot 等,需要翻墙/代理,还要处理依赖冲突) | 约 1–1.5 小时(含几次失败重试) |
| 下载模型(DAM-3B、OpenCLIP、FastSAM、ReID,共约 9 GB) | 视网速 |

服务器端**不需要联网**:本包已经在 `--network none` 的容器里跑通完整的 TUM 序列(见"验证")。

## 1. 部署包里有什么

由 `docker/build_full_image.sh` 生成,默认放在一个目录里(例如 `/mnt/e/daaam-bundle`):

| 文件 | 内容 |
|---|---|
| `daaam-image.tar.zst`(12.7 GB) | Docker 镜像 `daaam:jazzy-cu128-full`(未压缩 29.3 GB):Ubuntu 24.04 + CUDA 12.8 + ROS 2 Jazzy + 编译好的 C++ 工作区 + Python 虚拟环境 + 本仓库 |
| `daaam-assets-hf.tar.zst`(6.6 GB) | Hugging Face 缓存:DAM-3B、OpenCLIP ViT-L-14 |
| `daaam-assets-checkpoints.tar.zst`(0.7 GB) | FastSAM-x/s、CLIP ReID 权重、OpenAI CLIP ViT-B-16 基础权重 |
| `daaam-sample-tum_fr1_desk.tar.zst`(0.16 GB) | 样例数据集:TUM RGB-D fr1/desk,已转成 `ImageSequenceDataset` 格式(298 帧) |
| `deploy.sh` | 服务器端一键安装 |
| `daaam-run` | 运行封装(安装后在 `$DAAAM_HOME/bin/daaam-run`) |
| `bundle.env`、`SHA256SUMS` | 版本标记与校验和 |

合计约 20 GB。模型权重不放进镜像,单独打包:它们很少变、约 9 GB 且基本压不动,分开后换镜像不用重传。

## 2. 服务器要求

- Linux x86_64,Docker ≥ 24,当前用户能访问 Docker(在 `docker` 组里,或用 `sudo`)。
- **NVIDIA 驱动 ≥ 570**(镜像基于 CUDA 12.8)和 **NVIDIA Container Toolkit**(`docker run --gpus all` 能用)。
- `zstd`(`apt-get install zstd`)。
- GPU 显存 ≥ 16 GB(DAM-3B 推理峰值约 15.9 GB;24 GB 以上更宽裕),内存 ≥ 16 GB(完整序列峰值约 12 GB)。
- 磁盘:Docker 数据目录(默认 `/var/lib/docker`)空闲 **≥ 62 GB**——`docker load` 会先把整个归档(未压缩约 29 GB)解到临时目录再建层,装好后占约 32 GB;`$DAAAM_HOME`(默认 `~/daaam`)空闲 ≥ 12 GB。模型权重请放在本地 SSD 上,放在网络盘或机械盘上加载很慢(见"常见问题")。
- GPU 架构:镜像里的 PyTorch 2.8.0+cu128 带有 sm_70 / 75 / 80 / 86 / 90 / 100 / 120 的内核(V100、T4 和 RTX 20、A100、RTX 30/40——Ada 用 sm_86 的二进制、H100、B200、RTX 50);Pascal(sm_61)及更老的卡不支持。

## 3. 部署

```bash
# 1) 拷贝整个目录到服务器(约 20 GB)
scp -r /mnt/e/daaam-bundle user@server:~/

# 2) 在服务器上一键安装:校验 → 加载镜像 → 解包权重和样例数据 → 自检
ssh user@server
cd ~/daaam-bundle && ./deploy.sh              # 默认装到 ~/daaam,--home DIR 可改

# 3) 用样例数据端到端跑一遍(16 GB 显卡上约 4 分钟)
~/daaam/bin/daaam-run tum-example
```

`deploy.sh` 可以重复运行:已经装好的部分会跳过(`--force` 强制重装)。选项:`--home DIR`、`--no-verify`(跳过校验和)、`--no-sample`。

## 4. 日常使用

`~/daaam/bin/daaam-run` 把每次运行都包成一个 `docker run --rm`:挂载权重、数据集和输出目录,以调用者的 uid 运行(输出文件归你所有),默认离线(`HF_HUB_OFFLINE=1`)。

| 命令 | 作用 |
|---|---|
| `daaam-run check [--quick]` | 检查 GPU、Python 包、模型文件,并把 DAM-3B 真的加载到 GPU 上(`--quick` 跳过加载) |
| `daaam-run tum-example [帧数] [目标帧率]` | 跑样例数据(默认 298 帧、3 fps) |
| `daaam-run pipeline DATASET_DIR [run_pipeline.py 参数…]` | 跑你自己的数据集(`DATASET_DIR` 是服务器上的路径) |
| `daaam-run summarize output/<名字>/out_<时间>` | 打印运行统计、各层节点数、物体及其 DAM 描述 |
| `daaam-run shell` / `daaam-run exec 命令…` | 容器里开 shell / 执行任意命令 |

目录:`~/daaam/output`(结果)、`~/daaam/datasets`(容器里挂在 `/datasets`)、`~/daaam/assets`(权重)。环境变量:`DAAAM_HOME`、`DAAAM_IMAGE`、`DAAAM_GPUS`、`DAAAM_SHM`、`DAAAM_USER`、`DAAAM_ONLINE=1`(允许联网下载 Hugging Face 模型)、`DAAAM_DOCKER_ARGS`(追加 `docker run` 参数,如 `--network none`)。

**跑自己的数据集**:数据集目录要是 `ImageSequenceDataset` 的格式(`rgb/`、`depth/`、`pose/poses.txt`、`camera_info.json`),参数参考 `scripts/run_tum_example.sh`,例如:

```bash
~/daaam/bin/daaam-run pipeline /data/my_sequence \
  --config config/pipeline_config.yaml --dataset-type ImageSequenceDataset \
  --hydra-config-path config/hydra_config/tum_rgbd_khronos.yaml \
  --sam-model fastsam/FastSAM-x.pt \
  --semantic-config config/labels_pseudo.n5000.yaml --labelspace-colors config/labels_pseudo.n5000.csv \
  --depth-scale 1000 --fps 10 --depth-lb 0.3 --depth-ub 4.0 --zmq-url none \
  --dataset-name my_sequence --target-fps 3 --save-images --verbose \
  --config-overrides tracking.reid_weights=checkpoints/reid_weights/clip_general.pt
```

几个需要按数据调整的地方:

- `--target-fps`:DAM 是异步回答的,管线结束时**不等**还没返回的回答,而且第一次查询在第 60 帧。全速喂帧会让序列在回答回来之前就跑完,物体描述变少。3 fps 是在 16 GB 显卡上的折中,更快的卡可以调高。
- 伪标签空间大小:Hydra 创建时每个标签占约 0.28 MB 内存加固定的 1.1 GB,默认 30000 个标签要 9.5 GB。`labels_pseudo.n5000` 只有 5000 个(够几百条轨迹),轨迹数超过时会明确报错。
- Hydra 配置:`config/hydra_config/tum_rgbd_khronos.yaml` 是桌面尺度的室内 RGB-D 序列用的(0.05 m 体素);房间/走廊尺度的数据用 DAAAM-ROS 里的 `clio_dataset_khronos.yaml`。**改体素或截断距离时,`frontend.freespace_places.gvd.min_distance_m` 必须小于 `truncation_distance`**(详见该文件头部注释),否则整个"地点→房间→建筑"层静默缺失。

## 5. 它是怎么构成的

镜像分层:`nvidia/cuda:12.8.1-cudnn-devel-ubuntu24.04` → `daaam:jazzy-cu128-dev`(`docker/Dockerfile`:ROS 2、编译工具) → `daaam:jazzy-cu128-deps`(`docker/Dockerfile.deps`:rosdep 解析出的 26 个系统包) → `daaam:jazzy-cu128-full`(在上一层之上加入工作区快照:`/opt/daaam_ws` 下的 `build/`、`install/`、`.venv/`、导入的同级仓库,以及本仓库 `/opt/daaam_ws/src/daaam`)。

- 路径和开发环境完全一致(`/opt/daaam_ws`),所以 colcon 的 `--symlink-install` 符号链接、`.venv`、可编辑安装的 `daaam` 都原样可用。
- C++ 部分没有用 `-march=native`(GTSAM、small_gicp、TEASER++ 均关闭),二进制可以放到别的 CPU 上。
- TensorRT 的 `.engine` 文件与 GPU 型号绑定,**不要**在机器之间拷贝;本流程用的是 `.pt` 权重,不涉及。
- 容器里的 `daaam` 用户 uid 为 1000;`daaam-run` 用调用者的 uid 运行并把 `HOME` 设为 `/tmp`,所以其他 uid 也能用。

## 6. 在本机重新打包

工作区或权重有变化后(代码改动只需重新打包,不需要重装):

```bash
# 1) 镜像:工作区卷 + 当前仓库 → daaam:jazzy-cu128-full(约 3 分钟)。过程中 Docker 存储临时多占约 30 GB;
#    旧镜像要等新镜像提交完才会被取代,磁盘紧张时先 docker rmi daaam:jazzy-cu128-full
bash docker/build_full_image.sh image
# 2) 不带卷、不联网地自检这个镜像
bash docker/build_full_image.sh verify
# 3) 导出部署包(约 20 GB);WSL2 里请写到不在 WSL 虚拟盘所在分区的 Windows 盘,例如 /mnt/e/daaam-bundle。
#    docker save 会先把整个镜像(29 GB)暂存在 Docker 存储里再压缩,所以导出期间 Docker 存储临时多占约 29 GB
bash docker/build_full_image.sh export /mnt/e/daaam-bundle
# 只改了代码时,只重新导出镜像(权重压缩包和它们的校验和沿用,约 8 分钟):
bash docker/build_full_image.sh export /mnt/e/daaam-bundle image
# 或者一次做完:  bash docker/build_full_image.sh all /mnt/e/daaam-bundle
```

在 WSL2 里还要注意:WSL 的虚拟磁盘 `ext4.vhdx` 只会变大、不会自己变小,`docker load` / `docker save` 的临时空间会把它撑大,而它通常在 C 盘上。动手前看一下 C 盘空闲空间。

## 7. 没有预构建包时:在有网络的机器上从零部署

```bash
docker compose build daaam-ws                                         # 开发镜像 + 系统包层
docker compose run --rm -T daaam-ws bash docker/setup_workspace.sh    # 导入仓库、编译、装依赖、自检(可断点续跑)
docker compose run --rm -T daaam-ws python scripts/download_models.py # 模型权重
bash docker/build_full_image.sh all /path/to/bundle                   # 想继续打包时
```

`docker/setup_workspace.sh` 记录了对上游 `install/install.sh` 的所有补充和绕行(`hydra_python`、`khronos` 缺失的依赖声明、`sam2`/`dam`/`gradio` 的依赖冲突等),原因见 `progress.md` 的"发现的上游问题"。

## 8. 常见问题

- **运行跑完了,但物体没有描述(`daaam-run summarize` 里 "0 with a DAM description")**:DAM 没能加载。管线对此不报错,会照常跑完;`summarize` 会打印 WARNING 和 `logs/GroundingWorker-*.log` 里的错误。常见原因:权重不在 `assets/hf` 里(`daaam-run check` 会指出),或者你手动换了 `dam_model_path` 且设了 `HF_HUB_OFFLINE=1`。这个包已经保证:缓存里有 DAM-3B 时,`scripts/run_pipeline.py` 直接用本地快照目录,不访问 Hugging Face(DAM 的加载器对仓库名会调用 Hugging Face API,断网时必然失败)。
- **日志里 `Timeout waiting for workers after 60.0s ... Proceeding despite workers not being fully ready`**:权重加载得比 60 秒慢(常见于网络盘、机械盘),管线先开始处理帧,DAM 加载好之后才开始出描述。序列较短时可能来不及。把权重放到本地 SSD,或者调低 `--target-fps` 让序列跑得更慢。
- **`docker: Error response from daemon: could not select device driver "" with capabilities: [[gpu]]`**:没有装 NVIDIA Container Toolkit(或装了没重启 Docker)。
- **CUDA out of memory**:16 GB 显存贴着上限(DAM-3B 推理峰值 15.9 GB)。关掉占显存的其他进程,或者换更大的 GPU。
- **想更新 DAM-3B**:删掉 `$DAAAM_HOME/assets/hf/hub/models--nvidia--DAM-3B`,在能联网的服务器上执行 `DAAAM_ONLINE=1 daaam-run exec python scripts/download_models.py --only dam`;或者在有网的机器上更新缓存后重新打包(`docker/build_full_image.sh export <目录> hf`)。

## 9. 验证记录

2026-10-14,在 WSL2 Ubuntu-20.04(RTX 5060 Ti 16 GB,驱动 591.86,Docker 28.1.1)上模拟"另一台服务器"。逐条的命令和结果见 `progress.md` 条目 130–148。

| 验证 | 结果 |
|---|---|
| 删掉镜像后用 `deploy.sh` 全新安装(预检 → 校验和 → `docker load` → 解包 → 自检) | 通过,约 10 分钟 |
| `daaam-run check`(含把 DAM-3B 加载到 GPU),容器 `--network none` | 通过,DAM 加载 3 秒(权重在本地 SSD 上) |
| `daaam-run tum-example 298 3`,容器 `--network none`,不挂工作区卷 | rc=0,298/298 帧,约 3.3 分钟;场景图 55 个物体 / 35 个地点 / 1 个房间 / 1 栋建筑;268 个标签里 162 个有 DAM 描述;内存峰值 11.4 GB,显存峰值 15.7 GB |
| 以其他 uid(`DAAAM_USER=4321:4321`)运行 `check` 和 30 帧的运行 | 通过,输出文件归该 uid |

说明:

- 第一次断网测试发现 DAM 在断网时加载不了而管线照常跑完(没有描述),已修复(`progress.md` 条目 138–140);上表是修复之后的结果。
- 运行不是逐位可复现的:几次完整运行的物体数在 53 到 65 之间波动(DAM 回答到达的时机影响 Khronos 何时把轨迹提取成物体),有描述的标签数稳定在 162 / 268。
- 最终镜像归档(`daaam-image.tar.zst`)用 `zstd -t` 验证了完整性,并用同一个镜像(同一个镜像 ID)在本机跑了上面的测试;没有再对它做一遍 `docker load`,因为 `docker load` 需要约两倍镜像大小的临时空间,会占满这台机器的 C 盘。`docker load` 这条路径用上一份同格式的归档完整验证过。

## 10. 数据集、模型与许可证

- 样例数据来自 TUM RGB-D 数据集 `freiburg1_desk`(CC BY 4.0),已用 `scripts/prepare_tum_rgbd.py` 去畸变并重组为 `ImageSequenceDataset` 格式。引用:J. Sturm, N. Engelhard, F. Endres, W. Burgard, D. Cremers, *A Benchmark for the Evaluation of RGB-D SLAM Systems*, IROS 2012。
- 部署包里的模型权重各有自己的许可证:DAM-3B 按 NVIDIA 非商用许可发布(许可证文件在 `assets/hf/hub/models--nvidia--DAM-3B/snapshots/*/LICENSE`),FastSAM、CLIP ReID、OpenAI CLIP 等请自行核对。部署包只用于你自己的服务器之间搬运,**不要公开分发**,商用前先核对许可证。
