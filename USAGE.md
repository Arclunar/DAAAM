# DAAAM 使用指南

本文面向希望安装、运行和检查 DAAAM 的用户。项目的推荐运行方式是通过
[DAAAM-ROS](https://github.com/MIT-SPARK/DAAAM-ROS) 接入 ROS 2；本仓库同时
提供一个不经过 ROS 2 的离线入口，适合调试数据集、配置和算法模块。

## 1. 工作流概览

```text
准备环境与模型
    ↓
输入 RGB、深度和位姿（ROS 2 topic、rosbag 或离线数据集）
    ↓
分割 → 跟踪 → 选帧 → DAM 语义描述 → Hydra 动态场景图
    ↓
dsg.json + corrections.yaml + background_objects.yaml
    ↓
文本嵌入 → 区域聚类 → 区域摘要
    ↓
可视化、交互问答或 OC-NaVQA 评测
```

## 2. 环境准备

### 2.1 Docker（推荐用于复现环境）

仓库提供的镜像只准备 Ubuntu 24.04、CUDA 12.8、ROS 2 Jazzy、Python 和编译
工具，不会安装或编译项目本身。

```bash
USER_UID=$(id -u) USER_GID=$(id -g) docker compose build
./docker/check.sh
docker compose run --rm daaam
```

进入容器后安装项目：

```bash
bash install/install.sh
export DAAAM_WS=/opt/daaam_ws
source "${DAAAM_WS}/install/setup.bash"
```

容器中的 `/opt/daaam_ws` 使用命名卷持久化，因此手动下载的同级仓库、Python
依赖和 colcon 构建结果在退出后仍然保留。SSH agent 转发、GUI、数据集挂载、
镜像源切换和清理方法见 [DOCKER.md](DOCKER.md)。

### 2.2 宿主机直接安装

要求 Ubuntu 24.04、ROS 2 Jazzy、CUDA 12.x 和 Python 3.12。把 DAAAM 放在
ROS 2 工作区的 `src/` 下，再执行安装脚本：

```bash
source /opt/ros/jazzy/setup.bash
mkdir -p ~/ros2_ws/src
cd ~/ros2_ws/src
git clone git@github.com:MIT-SPARK/DAAAM.git daaam
bash daaam/install/install.sh
export DAAAM_WS=~/ros2_ws
source "${DAAAM_WS}/install/setup.bash"
```

脚本会导入 DAAAM-ROS、Hydra、Khronos 和其他工作区依赖，执行 rosdep、colcon
和 Python 包安装。逐步安装方法见 [INSTALL.md](INSTALL.md)。

### 2.3 每次打开新终端

先根据运行环境设置工作区位置，再加载工作区：

```bash
# Docker 中使用 /opt/daaam_ws，宿主机安装通常使用 ~/ros2_ws
export DAAAM_WS=~/ros2_ws
source "${DAAAM_WS}/install/setup.bash"
```

Docker 入口脚本会自动加载 ROS 2、虚拟环境和已经存在的工作区安装空间。

## 3. 模型、生成配置和密钥

### 3.1 必需文件

仓库不包含模型权重。默认配置引用以下路径：

```text
checkpoints/
├── fastsam/FastSAM-x-640x480.engine
└── reid_weights/clip_general.engine
```

TensorRT engine 与 GPU 和 TensorRT 版本绑定，必须在实际运行的机器上生成。
如果不使用 TensorRT，可以把配置中的路径改为对应的 `.pt` 文件。

至少先从 FastSAM 上游发布页下载基础权重并放到：

```text
checkpoints/fastsam/FastSAM-x.pt
```

生成 FastSAM TensorRT engine：

```bash
python scripts/export_fastsam_trt.py \
  --model_name FastSAM-x \
  --imgsz 480 640 \
  --half
```

生成通用 CLIP ReID 权重和 engine：

```bash
python scripts/export_vanilla_clip_engine.py --trt-fp16
```

不要安装裸的 `tensorrt` 或 `nvidia-tensorrt` 元包；本项目的 CUDA 12 环境应
使用 `tensorrt-cu12`。更换 CUDA 版本时也要同步调整 PyTorch 和 TensorRT。

### 3.2 构建后生成的配置

以下文件不在全新 clone 中，由 DAAAM-ROS 构建生成：

```text
config/labels_pseudo.yaml
config/labels_pseudo.csv
```

离线运行还需要 DAAAM-ROS 提供的 Hydra 配置，例如：

```text
<工作区>/src/daaam_ros/config/hydra_config/coda_dataset_khronos.yaml
```

如果命令提示找不到标签空间或 Hydra 配置，应先确认 DAAAM-ROS 已成功构建，
且当前终端已经 source 工作区。

### 3.3 API 密钥

区域摘要、交互问答和 OC-NaVQA 默认使用 OpenAI：

```bash
export OPENAI_API_KEY=sk-...
```

也可以在仓库根目录创建不提交到 Git 的 `.env` 文件。DAM 和
SentenceTransformers 模型在首次运行时可能需要从模型仓库下载，请预留缓存和
磁盘空间。

## 4. 推荐方式：通过 ROS 2 运行 CODa

### 4.1 准备 CODa 数据

下载 CODa-devkit 和所需序列：

```bash
git clone git@github.com:ut-amrl/coda-devkit.git
cd coda-devkit
python scripts/download_split.py -d /datasets/CODa -t sequence -se 0
export CODA_ROOT_DIR=/datasets/CODa
```

DAAAM 需要相机图像、位姿以及与 RGB 对齐的深度。项目实验使用
FoundationStereo 预先生成的 `3d_raw_estimated/` 深度；详细目录格式和深度生成
命令见 [RUNNING.md](RUNNING.md)。

### 4.2 生成 rosbag

```bash
ros2 launch daaam_ros dataloader_coda_with_depth.launch.yaml \
  sequence:=0 \
  dataset_path:=${CODA_ROOT_DIR} \
  bag_path:=${CODA_ROOT_DIR}/coda_0_with_depth.bag \
  depth_source:=3d_raw_estimated
```

为 `/tf_static` 创建 QoS 配置 `~/.tf_overrides.yaml`：

```yaml
/tf_static: {depth: 1, durability: transient_local}
```

### 4.3 启动管道并播放 bag

终端 1：

```bash
source "${DAAAM_WS}/install/setup.bash"
ros2 launch daaam_ros coda_daaam_hydra.launch.yaml \
  scene:=coda_sequence_0 \
  sam_model:=fastsam/FastSAM-x-640x480.engine \
  sam_imgsz:=480,640
```

终端 2：

```bash
source "${DAAAM_WS}/install/setup.bash"
ros2 bag play "${CODA_ROOT_DIR}/coda_0_with_depth.bag" \
  --clock \
  -p \
  --qos-profile-overrides-path ~/.tf_overrides.yaml
```

等待所有模型和 worker 初始化完成后，按空格开始播放。处理完成后管道会保存
结果并退出。常用 launch 参数包括：

| 参数 | 用途 |
|---|---|
| `scene` | 输出场景名称 |
| `sam_model`、`sam_imgsz` | 分割模型和输入尺寸 |
| `num_grounding_workers` | DAM worker 数量 |
| `query_interval_frames` | 两次 grounding 之间的帧数 |
| `min_obs_per_track` | 轨迹进入 grounding 前的最少观测数 |
| `depth_lb`、`depth_ub` | 有效深度范围，单位为米 |
| `cuda_device` | grounding worker 使用的 GPU 编号 |
| `exit_after_clock` | bag 播放结束后自动关闭 |

完整参数和队列调优说明见 [RUNNING.md](RUNNING.md)。

### 4.4 本机验证过的流程：CODa 序列 0（先在前 1000 帧上验证，整个序列的步骤相同）

本机的 C: 盘紧张，数据放在 `E:\daaam-data`（WSL 里是 `/mnt/e/daaam-data`）。仓库根目录的
`.env`（已被 git 忽略）里设 `DAAAM_DATA_DIR=/mnt/e/daaam-data`，`compose.yaml` 把它挂到容器的
`/data`，容器里 `CODA_ROOT_DIR=/data/CODa`。WSL、Windows 和容器看到的是同一份文件
（`CODa/`、`FoundationStereo/`、`output/`、`hf_cache/`、`logs/`、`previews/`）。

**`RUNNING.md` 写序列 0 约 15 GB，实际 `0.zip` 是 44.74 GB**：cam0 15.2 GB、cam1 16.4 GB（深度估计用）、
LiDAR 11.6 GB 和 cam3 原始深度 1.5 GB（DAAAM 用不到）。10 Hz，全序列 8213 帧、13.7 分钟。

**用 `just` 跑**（在仓库目录里运行，背后就是下面各步骤的命令；`just --list` 看全部）：

| 步骤 | 命令 | 说明 |
|---|---|---|
| 1 下载 | `just coda-download [frames]` | 默认前 1000 帧，`0` = 全部 8213 帧 |
| 2 深度 | `just coda-depth` | 约 4.3 秒/帧，可断点续传 |
| 3 录 bag | `just coda-bag [start] [frames] [name]` | 按数据集原始的 10 Hz 录；不写参数 = 整段序列 |
| 4 跑管线 | `just coda-run [bag] [rate] [labels]` | 不写 bag = 最新的；`rate` 是播放速度，默认 0.3（3 帧/秒） |
| 7 整段序列 | `just coda-chunks [n] [rate] [段号…]` | 分 n 段（默认 4），每段录 bag + 跑管线 |
| 5 后处理 | `just coda-post [结果…]`、`just coda-summarize [结果…]` | 后者再用 OpenAI 写区域摘要；不写结果 = 最新的 |
| 6 提问和看 | `just coda-ask "问题" <结果>`、`just coda-view <结果>` | `<结果>` 是 `output/coda` 下的目录名，`just coda-list` 列出 |

深度、管线、分段、后处理和提问这几个重活启动前会看 `docker ps`：已经有别的管线 / 深度 / 后处理容器在跑就拒绝启动
（显存和内存装不下两个）。脚本在 `scripts/make_coda_bag.sh`、`run_coda_chunks.sh`、`postprocess_coda.sh`，测试在
`tests/test_coda_scripts.py` 和 `tests/test_coda_just.py`。

**回看数据包**：`just coda-bag` 在 bag 完整关闭后往 bag 目录里写 `daaam_replay.json`（数据集类型、序列、起始帧、帧数、10 Hz、
时间原点 = `timestamps/0.txt` 的第一行；先写临时文件再改名），录之前还会检查起止帧在序列范围内、`timestamps/0.txt` 存在。
`just coda-run` 结束后只给**这次运行新建**的 `output/coda/out_<时间>/` 生成 `replay/` 数据包：CODa 的 ROS 流程不保留历史快照，
所以数据包只有一个时间点，即最终的 `dsg.json`，放在这段 bag 的最后一帧（时间相对序列开头）；数据集类型、序列、起始帧和时间
原点读自 sidecar，不从 bag 的名字推断，相机画面和 DAM 描述源帧从 `/data/CODa` 复制进去（cam0，按文件名里的数字排序）。
在这之前录的 bag 没有 sidecar：管线照常跑完、结果保留，但命令以退出码 3 结束并提示用 `just coda-bag` 重录；`just coda-chunks`
只复用带 sidecar 的 bag，旧 bag 会被重录一次。已有的 CODa 结果也可以手动补一个数据包（帧范围见 `output/coda_chunks.tsv`）：

```bash
docker compose run --rm -T daaam-ws python scripts/replay.py finalize --graph output/coda/out_<时间>/dsg.json \
  --run output/coda/out_<时间> --dataset-kind coda --sequence 0 --dataset /data/CODa --fps 10 \
  --start-frame 821 --frame-count 821 --out output/coda/out_<时间>/replay
```

然后在 `just replay-web` 里打开（见 9.0.1）。

1. **下载**（WSL 里，不需要容器）。devkit 的 `download_split.py` 整包下载且不能续传，代理又会断连，所以用
   `scripts/download_coda_subset.py` 只取前 1000 帧的 cam0/cam1 和元数据（3.34 GB，约 25 分钟，可续传，逐文件 CRC）：

   ```bash
   python3 -I scripts/download_coda_subset.py -d /mnt/e/daaam-data/CODa -se 0 --frames 1000 --proxy http://172.19.64.1:7890
   ```

   `--frames 0` 取全部 8213 帧（31.6 GB；已有的文件会跳过，所以可以在前 1000 帧的基础上接着补全；速度取决于代理和
   TACC，16–24 个连接时 0.7–1.6 MB/s，要 5–11 小时，可以和深度估计同时进行），`--include 3d_comp 3d_raw` 加上
   LiDAR 等，`--list` 只看会下什么。`just coda-download [frames]` 在容器里跑同一个脚本（数据落在同一个
   `E:\daaam-data\CODa`）。

2. **双目深度**（`RUNNING.md` 1.5 节）。一次性准备：`git clone --depth 1 https://github.com/nicogorlo/FoundationStereo.git`
   到 `/mnt/e/daaam-data/FoundationStereo`，再把 README 里 Google Drive 文件夹的 `11-33-40/cfg.yaml` 和
   `model_best_bp2.pth`（788 MB）放进它的 `pretrained_models/11-33-40/`。DAAAM 的环境里有 Perception Encoder 的
   `core` 包，会遮住 FoundationStereo 的 `core`，所以用包装脚本（参数原样转给原脚本）：

   ```bash
   just coda-depth    # 即：
   docker compose run --rm -T -e https_proxy=http://172.19.64.1:7890 daaam-ws \
     python scripts/run_foundation_stereo_coda.py --dataset_folder /data/CODa --sequence_id 0 </dev/null
   ```

   RTX 5060 Ti 上约 4.3 秒/帧：1000 帧 72 分钟，全序列约 9.5 小时。输出 `3d_raw_estimated/cam0/0/depth_cam0_0_<帧号>.png`
   （16 位、毫米）。数据加载器按排序后的下标把深度配给帧，深度必须从第 0 帧起连续，**不能用 `--depth_skip`**。构造模型时
   会自动下约 22 MB 的 timm 权重和 dinov2 的 hub 代码，所以要带代理变量。

   整个序列的深度要 8213 帧 × 4.3 秒 ≈ 9.5 小时，所以包装脚本做了两件事：只用 cam0 和 cam1 **都已有**的帧（原脚本按位置给两个
   文件列表配对，下载到一半会错位），所以可以边下载边跑，后来的帧由下一次运行补上（已有的深度图会跳过）；`STOP_FILE`
   指向的文件一出现，它就在下一帧之前干净退出（正在算的那一帧会算完），让出 GPU，之后再运行同一条命令即可继续：

   ```bash
   docker compose run --rm -T -e STOP_FILE=/data/depth.stop -e https_proxy=http://172.19.64.1:7890 daaam-ws \
     python scripts/run_foundation_stereo_coda.py --dataset_folder /data/CODa --sequence_id 0 </dev/null
   touch /mnt/e/daaam-data/depth.stop   # 要让出 GPU 时（WSL 里）；用完 rm 掉
   ```

   本机用一个循环脚本（不在仓库里，`E:\daaam-data\supervisor\depth_supervisor.sh`）反复启动它：有别的 GPU 任务（回放、
   管线、问答……）或 `E:\daaam-data\gpu.lock` 目录出现时就让深度运行停下，空了再接着跑。自己要跑管线时先
   `mkdir /mnt/e/daaam-data/gpu.lock`，跑完 `rm -rf` 掉。

3. **生成 bag**（约 3 分钟，1.4 GiB）。bag 的名字会被加上时间戳（`coda_0_with_depth_<时间>.bag`），`add_timestamp`
   没有暴露成 launch 参数；加载器在 `end_frame` 默认时少写最后一帧（999 帧）：

   ```bash
   just coda-bag      # 即：
   docker compose run --rm -T daaam-ws bash -c 'ros2 launch daaam_ros dataloader_coda_with_depth.launch.yaml \
     sequence:=0 dataset_path:=$CODA_ROOT_DIR bag_path:=$CODA_ROOT_DIR/coda_0_with_depth.bag \
     depth_source:=3d_raw_estimated' </dev/null
   ```

   `just coda-bag [start] [frames] [name]`（`scripts/make_coda_bag.sh`）还能录一段帧（`just coda-bag 0 1000` 是前 1000
   帧，范围的换算见第 7 步）。bag 按数据集原始的 10 Hz 录，播放速度到第 4 步再选。

4. **跑管道**。launch 文件默认用 TensorRT engine，一次性在容器里建好（`INSTALL.md`，约 3 分钟）：
   `python scripts/export_fastsam_trt.py --model_name FastSAM-x` 和 `python scripts/export_vanilla_clip_engine.py`。
   `scripts/run_coda_example.sh` 启动 launch、等模型加载完、暂停着播放 bag 再用服务继续、等结果保存（说明在脚本开头）：

   ```bash
   just coda-run      # 即：
   docker compose run --rm -T daaam-ws bash scripts/run_coda_example.sh </dev/null
   ```

   `just coda-run [bag] [rate] [labels]` 可以指定 bag（写目录名或容器里的路径）和播放速度：bag 是 10 Hz，`rate` 默认
   0.3，也就是每秒播 3 帧（每帧均值 130–154 ms，p99 400–680 ms，实时的 10 帧/秒每帧只有 100 ms，节点会丢帧）。不写 bag
   时用的是最新的 `coda_0_with_depth*.bag`，如果它是整段序列的 bag（8213 帧），脚本启动时会警告：这么长的一次运行在本机会因为
   内存被杀（见下面），改用 `just coda-chunks`。

   帧选择用 launch 文件默认的 Perception Encoder（`PE-Core-L14-336`，论文的设置）：权重 `facebook/PE-Core-L14-336`
   （2.69 GB）一次性下载，放在 `E:\daaam-data\hf_cache`，再在工作区缓存里放一个软链接指向它，这样不用设 `HF_HOME`：

   ```bash
   docker compose run --rm -T --no-deps -e HF_HOME=/data/hf_cache -e https_proxy=http://172.19.64.1:7890 daaam-ws \
     python -c 'from huggingface_hub import hf_hub_download as d; print(d("facebook/PE-Core-L14-336", "PE-Core-L14-336.pt"))' </dev/null
   docker compose run --rm -T --no-deps daaam-ws ln -s /data/hf_cache/hub/models--facebook--PE-Core-L14-336 \
     /opt/daaam_ws/cache/huggingface/hub/models--facebook--PE-Core-L14-336 </dev/null
   ```

   PE 的 fp32 权重（2.7 GB）会把 16 GB 显存撑满，Windows 把显存换到内存里（每帧 320 ms、丢 14% 的帧、C: 盘的页面文件涨
   25 GB），所以脚本默认设 `DAAAM_CLIP_HALF=1`（fp16，1.3 GB，特征相同，余弦相似度 1.0000）。要用已缓存的 OpenCLIP：
   `CLIP_BACKEND=openclip CLIP_MODEL=ViT-L-14`（加在 `docker compose run -e` 后面）。

   前 1000 帧：约 6.7 分钟，999 / 999 帧，每帧 153 ms（OpenCLIP 那次是 996 / 999 帧、185 ms）；结果在
   `E:\daaam-data\output\coda\out_<UTC 时间>\`（`dsg.json`、`corrections.yaml`、`background_objects.yaml`……）。
   日志里关闭时的 `Error starting node:`、`rcl_shutdown already called` 和 Hydra 启动时的
   `No module of type 'RosMetaDataListener'` 是上游行为，结果照常保存。
   **同一时间只能跑一个流水线**：显存（峰值 15.6 GB）和 WSL 内存（可用最低 465 MB，Hydra 5.8 GB + DAAAM 节点 4.0 GB +
   DAM 3.6 GB）都接近上限，另一个流水线同时跑会 OOM，Windows 也会卡；先看 `docker ps` 和 `nvidia-smi`。

   **整段序列（8213 帧）一次装不下**：在本机的 15.9 GB 内存 + 4 GB 交换区里，第一次完整运行跑到第 3599 帧时内核
   OOM killer 杀了 `hydra_ros_node`（帧处理一直正常：3 fps、每帧约 150 ms、没丢帧；DAAAM 节点把前 3599 帧的场景图存了下来）。
   内存随序列涨：Hydra 每帧约 0.35 MB（前 1000 帧里它看起来持平，那是内核在把东西换出去，要看 `/proc/<pid>/status` 里的
   `VmRSS + VmSwap` 才准），DAAAM 节点在第 1000–1800 帧间从 4.3 涨到 6.1 GB，第 3290 帧时 4 GB 交换区用满。
   `malloc_trim`（`DAAAM_TRIM_INTERVAL_FRAMES`）只还回很少一点（DAM worker 约 0.5 GB，DAAAM 节点每次 70–80 MB），
   所以内存碎片不是主因。Windows 侧可用物理内存也只剩 0.7 GB
   （提交 63.7 / 65.5 GB），调大 WSL 内存没用，要加的是交换区：

   ```ini
   # C:\Users\<你>\.wslconfig，改完在 PowerShell 里 wsl --shutdown（会中断 WSL 里所有任务）
   [wsl2]
   swap=16GB
   swapfile=E:\\wsl-swap.vhdx
   ```

   不改的话把序列切成约 2050 帧一段、每段单独跑，见下面第 7 步（每段一个独立的场景图）。

5. **后处理**（第 8 节）。sentence-t5-xl（2.5 GB）放在 `E:\daaam-data\hf_cache`，所以所有加载它的步骤都要用同一个
   `HF_HOME`，否则会再下载一份（加 `-e HF_HUB_OFFLINE=1` 可让缺模型时直接报错）。`cluster_places` 要
   `interactive:=false`，默认会打开 RViz 和 GUI：

   ```bash
   RUN=/data/output/coda/out_<时间>
   docker compose run --rm -T -e HF_HOME=/data/hf_cache daaam-ws python scripts/postprocess_scene_graph.py --data-dir $RUN </dev/null
   docker compose run --rm -T daaam-ws ros2 launch daaam_ros cluster_places.launch.yaml data_dir:=$RUN interactive:=false </dev/null
   docker compose run --rm -T daaam-ws python scripts/summarize_regions.py --data-dir $RUN --model-name gpt-5-nano --n-samples 20 </dev/null
   ```

   前两步不需要密钥；区域摘要要 `OPENAI_API_KEY`（写在 `.env` 里，见 3.3 节）。`just coda-post [结果…]` 做前两步，
   `just coda-summarize [结果…]` 做全部三步（`scripts/postprocess_coda.sh`，`HF_HOME` 默认 `/data/hf_cache`；
   不写结果 = 最新的，`just coda-list` 看有哪些、做到哪一步）。

6. **提问和可视化**：

   ```bash
   printf '这里有垃圾箱吗？它旁边有什么？\n\n' | docker compose run --rm -T -e HF_HOME=/data/hf_cache daaam-ws \
     python scripts/demo_query.py --dsg-path $RUN/clustered_dsg_with_summaries.json --seq-id 0 --model-name gpt-5-mini
   just rerun-web output/coda/out_<时间>/clustered_dsg_with_summaries.json   # 浏览器里的 Rerun，网址见命令输出
   ```

   `demo_query.py` 会按场景图里记录的 CLIP 编码器建查询编码器（原来的默认值 `openai` 会被 provenance 检查拒绝）。
   Rerun 网页必须用带 `?url=rerun+http://…/proxy` 的完整网址打开，只开主页只会看到欢迎页。
   `just coda-ask "问题" <结果>` 和 `just coda-view <结果>` 就是这两条（`<结果>` 是 `output/coda` 下的目录名）。

7. **整段序列分 4 段跑**（原因见第 4 步末尾的内存说明）。每段约 2050 帧，每段一个独立的场景图（同一个世界坐标系，
   没有合并），都用 Perception Encoder 选帧。每段先建 bag（约 5.7 分钟、3.0 GB），再跑管线（约 14 分钟：模型 1 分钟、
   播放 11.4 分钟、保存 1–2 分钟）。数据加载器的 `start_frame` / `end_frame` 有个毛病：它返回
   `start_frame .. end_frame-start_frame-1` 号帧，所以 `end_frame` 要传 `2×start_frame + 帧数`（`scripts/make_coda_bag.sh`
   已经这样换算）；`ros2 bag play --start-offset` 不能用来切段，它不发 `/tf_static`：

   ```bash
   just coda-chunks            # 4 段：每段录 bag（coda_0_f<起>-<止>_<时间>.bag）再跑管线，约 1.3 小时；跑完打印下一步的命令
   just coda-chunks 4 0.3 3 4  # 只跑第 3、4 段（同一帧范围已有的 bag 会复用）
   just coda-post <结果1> <结果2> <结果3> <结果4>
   # 即，第 k 段（帧 S .. S+L-1；k=1: S=0 L=2053　k=2: S=2053 L=2053　k=3: S=4106 L=2053　k=4: S=6159 L=2054）：
   docker compose run --rm -T daaam-ws bash scripts/make_coda_bag.sh <S> <L> coda_0_f<S>-<S+L> </dev/null
   docker compose run --rm -T -e LABELS=1000 -e OUT=/data/output/coda_run_chunk<k>of4 daaam-ws \
     bash scripts/run_coda_example.sh coda_0_f<S>-<S+L>_<时间>.bag </dev/null
   ```

   本机的结果（四段都是退出码 0、3.00 fps、没丢帧；每帧 `process_frame` 均值 130–154 ms；每段内存：交换区最高 2.1–2.65 GB、
   WSL 可用内存最低 240–670 MB，显存峰值 15.8 GB，所以 2050 帧是这台机器上能放的大小，不要再放大）：

   | 段 | 帧 | 处理的帧 | 物体 / 背景物体 / 地点 | 有 DAM 描述的标签 | 结果目录（`E:\daaam-data\output\coda\`） |
   |---|---|---|---|---|---|
   | 1 | 0–2052 | 2052 | 208 / 139 / 1096 | 346 / 493 | `out_20261016_011014` |
   | 2 | 2053–4105 | 2050 | 270 / 165 / 966 | 428 / 538 | `out_20261016_013121` |
   | 3 | 4106–6158 | 2052 | 326 / 130 / 1022 | 452 / 537 | `out_20261016_015135` |
   | 4 | 6159–8212 | 2054 | 248 / 178 / 1329 | 425 / 630 | `out_20261016_021046` |

   所有 bag 都按原始的 10 Hz 录（帧数 ÷ 时长 = 10.00 Hz），变慢只发生在播放时（`RATE`，默认 0.3，即 3 帧/秒）。完整 bag
   （`coda_0_with_depth_full_*`）8213 帧、821.27 秒、没有缺口；四个分段 bag 合起来少 2 帧（第 2052 和第 6158 帧：加载器在
   第 1、3 段的 end 处少写了一帧，具体原因没查），管线在分段 bag 的 8211 帧里处理了 8208 帧（第 2 段少 3 帧）。

   另有第一次整段运行被内核杀掉 Hydra 之前留下的 0–3598 帧连续场景图（`out_20261016_002406`：328 个物体、235 个背景物体、
   1682 个地点，804 个标签里 554 个有描述；没有 Hydra 最后一遍的结果）。上表这四次是用 `E:\daaam-data\supervisor\chunks.sh`
   （`scripts/run_coda_chunks.sh` 的前身，另外有 GPU 锁和内存看门狗，不在仓库里）跑的，bag 叫 `coda_0_chunk<k>_<时间>.bag`；
   `run_coda_chunks.sh` 按帧范围给 bag 起名，不会复用它们。仓库外还有 `chunk_stats.py`（上表的数）和 `topdown_chunks.py`
   （四段画在一张俯视图里：`E:\daaam-data\previews\coda_seq0_chunks_topdown.png`）。

## 5. 离线运行

离线入口仍然依赖工作区中构建出的 `hydra_python` 和 DAAAM-ROS 的 Hydra 配置。
它主要用于开发调试；正式 ROS 数据流建议使用上一节的 ROS 2 入口。

### 5.1 先检查配置和数据集

```bash
python scripts/run_pipeline.py /datasets/my_sequence \
  --config config/pipeline_config.yaml \
  --dataset-type ImageSequenceDataset \
  --hydra-config-path "${DAAAM_WS}/src/daaam_ros/config/hydra_config/coda_dataset_khronos.yaml" \
  --dry-run
```

`--dry-run` 只加载配置和数据集，不加载模型，也不会开始逐帧处理。

### 5.2 执行管道

```bash
python scripts/run_pipeline.py /datasets/my_sequence \
  --config config/pipeline_config.yaml \
  --dataset-type ImageSequenceDataset \
  --hydra-config-path "${DAAAM_WS}/src/daaam_ros/config/hydra_config/coda_dataset_khronos.yaml" \
  --target-fps 10 \
  --output-dir output/my_run \
  --save-images
```

常用选项：

| 选项 | 说明 |
|---|---|
| `--dataset-type` | `ImageSequenceDataset`、`HM3DSemDataset` 或 `CodaDataset` |
| `--max-frames N` | 只处理前 N 帧，适合冒烟测试 |
| `--target-fps N` | 按指定帧率节流 |
| `--no-throttle` | 不限帧率 |
| `--no-wait-workers` | 不等待 worker 就绪，不建议正式运行使用 |
| `--save-images` | 保存分割中间结果 |
| `--save-interval N` | 每 N 帧保存一次，默认 100 |
| `--zmq-url none` | 关闭实时 DSG ZMQ 发布 |
| `--verbose` | 发生错误时打印完整堆栈 |

## 6. 配置方法

主配置位于 `config/pipeline_config.yaml`，CODa 示例位于
`config/pipeline_config_coda.yaml`。

| 配置段 | 主要内容 |
|---|---|
| `segmentation` | 模型、设备、输入尺寸和 mask 面积阈值 |
| `tracking` | ReID 权重、轨迹缓存和设备 |
| `grounding` | LLM、查询间隔、CLIP 特征和 GPU |
| `workers` | assignment/grounding worker 类型、数量和专属参数 |
| `depth` | 深度上下界 |
| `scene_graph` | 延迟 DSG 处理和背景物体设置 |
| 顶层字段 | 标签空间、输出目录和层级优化 |

不修改 YAML 也可以临时覆盖嵌套字段：

```bash
python scripts/run_pipeline.py /datasets/my_sequence \
  --config config/pipeline_config.yaml \
  --hydra-config-path /path/to/hydra_config.yaml \
  --config-overrides \
    workers.num_grounding_workers=2 \
    grounding.query_interval_frames=120 \
    depth.depth_ub=20.0
```

优先级是：配置类默认值 < YAML < 独立 CLI 参数 < `--config-overrides`。

## 7. 输出文件

每次运行的输出目录通常包含：

| 文件 | 内容 |
|---|---|
| `dsg.json` | Hydra 动态场景图 |
| `corrections.yaml` | 语义标签修正和时序记录 |
| `background_objects.yaml` | 未进入 Hydra 常规对象层的背景对象 |
| `keyframe_annotations.yaml` | 关键帧标注 |
| `pipeline_config.yaml` | 本次运行实际配置的快照 |
| `performance_statistics.csv` | 逐帧性能统计 |
| `final_stats.json` | 离线入口生成的最终统计 |
| `logs/` | 运行日志 |
| `images/`、`grounding_images/` | 启用相应保存选项后的调试图像 |
| `replay/` | `just tum-run` / `just tum-test` / `just coda-run` 生成的回看数据包，用 `just replay-web` 打开（9.0.1） |
| `replay.rrd` | TUM 回看的 Rerun 录制（`just rerun-play`） |

先检查 `dsg.json`、`corrections.yaml` 和 `background_objects.yaml` 是否完整，再进行
后处理。中断运行时应等待优雅关闭完成，避免得到未写完的 YAML/JSON。

## 8. 后处理

下面的命令按顺序执行。

### 8.1 添加文本嵌入

```bash
python scripts/postprocess_scene_graph.py \
  --data-dir output/my_run \
  --sentence-model-name sentence-transformers/sentence-t5-xl
```

输入 `dsg.json`、`corrections.yaml` 和可选的 `background_objects.yaml`，输出
`dsg_updated.json`。

### 8.2 聚类区域

```bash
ros2 launch daaam_ros cluster_places.launch.yaml \
  data_dir:=$(realpath output/my_run)
```

输入 `dsg_updated.json`，输出 `clustered_dsg.json`。

### 8.3 生成区域摘要

```bash
export OPENAI_API_KEY=sk-...
python scripts/summarize_regions.py \
  --data-dir output/my_run \
  --model-name gpt-5-nano \
  --n-samples 20
```

输出 `region_summaries.yaml` 和 `clustered_dsg_with_summaries.json`。默认的
`--feature-key combined` 要求场景图中同时存在 CLIP 和句子嵌入特征。

## 9. 查看和查询结果

### 9.0 实时预览（边跑边看）

离线管线默认通过 ZMQ 把场景图（含网格）发布到 `tcp://127.0.0.1:8001`（`--zmq-url none`
关闭）。Hydra 自带的 streaming visualizer（RViz2）订阅它，就能在处理帧的同时看到网格、
地点图、物体、房间和相机轨迹逐步长出来。在 `daaam-ws` 容器里（host 网络、`DISPLAY` 和
X11 socket 已在 `compose.yaml` 里配好；WSL2 + WSLg 下窗口出现在 Windows 桌面）：

```bash
docker compose run --rm -T daaam-ws bash scripts/run_live_preview.sh 298 3
```

参数和环境变量与 `scripts/run_tum_example.sh` 相同。脚本先启动
`ros2 launch hydra_visualizer streaming_visualizer.launch.yaml use_zmq:=true`，再运行 TUM
样例；跑完后窗口保持打开，方便查看最终的地图，`Ctrl-C` 退出（`KEEP_VIEWER=0` 则自动关闭）。
用自己的数据时，给 `scripts/run_pipeline.py` 传 `--zmq-url tcp://127.0.0.1:8001`，另开一个
容器运行上面的 `ros2 launch` 命令。

说明：

- 窗口默认很大，可能超出屏幕，请最大化或缩小；左键旋转、中键平移、滚轮缩放。
- 默认的层间距是按楼宇尺度设的（`hydra_ros/hydra_visualizer/config/visualizer_config.yaml`
  里 `layer_z_step: 5.5`），桌面尺度的场景里整个场景图会显得又细又高，需要放大到网格附近看；
  可以复制该文件调小 `renderer.layer_z_step` 和各层的 `scale`，用 launch 参数
  `visualizer_config_path:=...` 指定。
- 物体节点只带语义标签（伪标签的编号），DAM 的文字描述不在 RViz 里，见 `corrections.yaml`
  和 `scripts/summarize_run.py`。
- 带着查看器跑时，进程曾在结尾崩溃过一次（8 次运行中 1 次，原因没有查到）：场景图已在崩溃前
  保存，只是缺 `final_stats.json`。脚本里设了 `ulimit -c 0`，避免 WSL2 在 Windows 盘上为
  每次崩溃写出几 GB 的转储文件。
- RViz 窗口弹出后要等约 50 秒才开始跑帧：这是管线在初始化（脚本固定停 8 秒；Python 导入约
  10 秒；FastSAM 和 Hydra 管线约 20 秒；工作进程加载模型约 10 秒），不是卡住了。不开查看器
  时同样的初始化只要约 26 秒（开着 RViz 时它占 5–8 个 CPU 核，初始化跟着变慢，没有细查原因）。
- 启动时会刷出几遍、每遍 12 行红色的 `[ERROR] Cannot register already existent type 'cosine'`
  （还有 `l1`、`l2`、`lerf`、`basis_points`、`distance`、`downsample`），主进程导入
  `hydra_python` 时一遍，每个工作进程启动时各一遍（不带 DAM 共两遍，带 DAM 共三遍：多一个
  grounding 工作进程）。只要加载 `hydra_python` 就会出现（单独加载
  `libhydra.so` 或 `spark_dsg` 不会），是 Hydra 把这些类型又登记了一次、后一次被拒绝，先登记
  的照常生效，运行结果不受影响，可以忽略。原因：这些类型在 Hydra 的头文件里用 `inline static`
  成员注册（`embedding_distances.h`、`gvd_merge_policies.h`、`tsdf_interpolators.h`），
  `libhydra.so` 加载时登记一次；Python 绑定模块 `_hydra_bindings.so` 也编进了同样的注册代码，
  但它只导出 26 个符号（其余隐藏），不能和 `libhydra.so` 共用那些静态对象，`import hydra_python`
  时又登记一次，config_utilities 拒绝后打印这行红字（隐藏可见性是从导出符号表推断的）。
  每个导入 `hydra_python` 的进程各打印一遍。只有重编 `hydra_python` 才能消除。
- 同样每个进程各一行 `[WARNING] 'efficientvit' library not found. EfficientViT models will not
  be functional.`（不带 DAM 两行，带 DAM 三行），也可以忽略。它是 `src/daaam/utils/segmentation.py`
  在导入时打印的：那个文件会尝试导入四种可选的分割后端（EfficientViT-SAM、FastSAM、SAM、SAM2），
  哪个没装就打印一行警告，用到时才报错。这里没装 `efficientvit` 包，而示例脚本用的是 FastSAM
  （`--sam-model fastsam/FastSAM-x.pt`），只有模型文件名里带 `efficientvit` 时才会走 EfficientViT
  这一支，所以不影响运行。要用它得先装 `efficientvit` 并自己准备 `efficientvit_sam_*` 权重。
- 速度：管线逐帧串行，先 CV（FastSAM、跟踪等），再 Hydra；`--target-fps` 只是上限（一帧处理得
  比 1/fps 快时才补一个 sleep），所以把它从 6 提到序列自身的 15 Hz 不会变快。实测（RTX 5060
  Ti，120 帧、不带 DAM）：不开查看器每帧约 0.30 秒（CV 0.21 + Hydra 0.09），约 3.4 帧/秒；开着
  RViz 并发布场景图时每帧约 0.47 秒（CV 0.29 + Hydra 0.18），约 2.1 帧/秒。GPU 平均只有
  30–40% 的时间在忙（其余时间在等 CPU 上的处理，比如 Hydra），GPU 不满是正常的。想更快：不开
  查看器，跑完再用 9.1 节的静态可视化看；或者改流水线（让 CV 和 Hydra 重叠、跳帧、更粗的体素），
  这些会改变算法的行为，这里没有做。

### 9.0.1 建图后回看（回看工作区：一个页面，多个结果）

不开 RViz 建图更快（见上面"速度"一条），建完再拖进度条看地图是怎么一点点长出来的。（原版 DAAAM-ROS 的
ROS 2 流程里有类似的东西：`daaam_ros/nodes/rerun_visualizer_node.py` 把场景图、网格、图像和位姿带着时间
戳记到 Rerun，可以拖 Rerun 的时间条回看；但它要跑 ROS 2 流程、会弹出原生 Rerun 窗口、默认每 10 秒才处理
一次场景图、也不保存录制。这里的是离线脚本流程 `run_pipeline.py` 的对应物，一个本地网页，不依赖 Rerun。）

```bash
just tum-run        # 完整 298 帧、带 DAM，约 4 分钟；just tum-run 120 只跑前 120 帧
just tum-run 298 fr1_room    # 换一个测试序列（序列名是最后一个参数；有哪些序列、怎么下载见 9.5）
just replay-web     # 启动回看工作区（WSL 宿主机上只监听 127.0.0.1 的本地服务），Ctrl+C 停止
# 浏览器打开 http://127.0.0.1:8765/replay.html （Windows 的浏览器也能直接打开这个地址）
```

`just tum-run` 等价于 `docker compose run --rm -T -e NAME=tum_replay daaam-ws bash scripts/run_replay.sh 298`。跑完会打印
这次运行新建的结果目录 `output/tum_replay/out_<时间>/`、它的 `replay/` 回看数据包和 `replay.rrd`，并提示 `just replay-web`。
同一次运行的 `dsg.json`、`replay/`、`replay.rrd`、`images/`、`logs/` 和 `hydra_output/` 都在这个唯一的 `out_<时间>`
目录里；旧版本已经生成的双层目录仍可由 `tum-post` / `tum-summarize` 自动识别。

**整个仓库只有一个回看页面**：根目录的 `replay.html`，由 `just replay-web` 提供（`python3 -m scripts.replay_web.server`，
只用 Python 3.8 标准库，在 WSL 宿主机上运行，所以 WSL 能看到的任何目录都能打开）。结果目录里只放数据，不再生成
`replay.html`；**旧结果里留下的自包含 `replay.html` 不能被新工作区打开，也没有兼容层**——需要回看旧结果时，用保留的录制
重新做数据包（见下面"恢复"）。`replay.rrd` 照旧生成，是给 Rerun 的（`just rerun-play`），工作区不读它。

**回看数据包 `replay/`**（`scripts/replay.py finalize` 写出，格式见 `scripts/replay_web/manifest.py`）：

| 文件 | 内容 |
|---|---|
| `manifest.json` | 版本 1 的清单，最后写入：数据集类型（`tum` / `coda`）、序列、时间原点、帧率、起始帧和帧数、快照编号与时间、每个数据块的相对路径 / 时间范围 / 快照数 / 大小 / SHA-256、相机、图像索引、物体描述和描述来源、实体目录、可选参考网格。只有带有效清单的目录才能打开 |
| `timeline/NNNNNN.json.gz` | 时间轴数据块：每块各自 gzip 压缩，按快照数（默认 24）和未压缩大小（默认 8 MiB）切分，超大的单个快照单独成块；`fr3_long_office_household` 的 250 个快照是 17 块、每块不超过 8.3 MB |
| `images/timeline/{rgb,depth}/` | 每个快照当时处理的那一帧的 RGB 和彩色深度小图（宽 320 px），同一帧只存一次 |
| `images/descriptions/` | DAM 描述物体时实际用的那一帧（最宽 960 px），多个物体共用一帧时只存一次 |
| `reference/reference.json.gz` | 可选的 1 cm 参考网格（9.6） |

数据包先在旁边的临时目录 `.replay.<pid>.tmp` 里写好、校验（含 SHA-256），再整体改名替换；失败时旧数据包原样保留。
清单里的路径都是数据包内的相对路径，服务端拒绝绝对路径、`..` 和指向包外的符号链接。

工作区页面：

- **打开结果**：上方输入框接受 `/home/…`、`/mnt/e/…`、`E:\…`（也可以是 `E:/…`，或资源管理器"复制为路径"带引号的形式）和
  本发行版的 `\\wsl.localhost\Ubuntu-20.04\…` / `\\wsl$\Ubuntu-20.04\…`；也可以输入数据包目录 `…/replay` 或清单本身。
  "output/ 里的结果"列出默认 `output/` 下（最多 4 层，跟随 `output/coda` 指向 E: 盘的软链接）所有有效数据包，最新的在前。
  打不开时在页面下方提示原因，例如期望的 `…/replay/manifest.json` 路径。
- **多个结果用标签页**（不做并排对比）：同一结果再打开会切到它的标签；拖动标签换顺序；标签有焦点时 ←/→/Home/End 切换、
  Delete 关闭（关闭只释放服务里的会话，不删除任何文件）。刷新或重开浏览器后，打开的结果、顺序、当前标签和每个标签的视图
  （时间、相机视角、图层开关、选中、隐藏、相机画面的位置）都会恢复；已经打不开的路径会被跳过并提示。只有当前标签渲染、
  播放和解码图像：切到别的标签时播放暂停，切回来继续。
- 底部进度条：拖动、播放/暂停（空格）、单步（←/→）、倍速；`t` 是数据里的时间（TUM 从 0 起，CODa 从序列第一帧的时间戳起），
  不是墙钟时间。一打开就在最后一个快照（建完的地图）。首次只取包含当前快照的数据块和它前面一块，之后按需取。
- 左键旋转、右键或 Shift+左键平移、滚轮缩放、双击复位。
- 左侧可以切换和开关：网格的来源（1 cm 参考网格 / 动态 Hydra 网格 / 隐藏，见 9.6）和"只画顶点"、物体节点、
  物体框（仅选中 / 全部 / 隐藏）、标签（仅选中 / 全部 / 隐藏）、地点和地点连线、房间/楼宇、层间连线、相机轨迹；
  "层间距"把地点、房间、楼宇依次抬高，和 RViz 的分层显示一样（设成 0 就都在真实位置）。
- 浮动的描述卡片默认只给选中的物体或地点显示。选"全部"会给所有物体、地点、房间和楼宇显示：卡片可能互相重叠，但哪些卡片
  存在只在换快照、选择、点右侧列表的眼睛或换模式时才变；转动、平移、缩放只改它们的位置，卡片只有在它自己的锚点跑出画面
  或转到相机后面时才不显示。选"隐藏"一键收起所有卡片（右侧的实体列表和详情不受影响）。
- 选中的物体用琥珀色粗框（4 px，外面一圈深色光晕，八个角有圆点）画在最上层：不被网格遮住，物体框模式是"隐藏"时也显示，
  不拦截鼠标点击；物体的选中节点同样是琥珀色。
- **点击地图里的物体或地点、或右侧列表的一行，只选中和高亮，不改变时间轴。** 有描述来源的物体在列表里有"描述源帧"按钮：
  弹窗显示 DAM 当时看到的那一帧（帧号、相机、时间戳和图像），打开、关闭都不改变时间；没有记录来源的物体显示灰色的
  "无描述源帧"，不会用首次或最近的观测去猜。来源帧在建图完成后由 `finalize --dataset …` 从数据集复制进数据包，在线管线
  不保存额外图片；数据集以后不再挂载也能看。CODa 的帧号按"起始帧 + frame_id − 1"换算，并用记录的图像时间戳核对：
  节点丢过帧导致对不上时以时间戳为准，哪一帧都对不上就不显示。
- **相机画面卡片**（当时的 RGB 和彩色深度图，蓝近红远、TUM 0–4 m / CODa 0–30 m，黑色是没测到）：拖动卡片的标题栏移动，
  标题栏有焦点时方向键每次移 10 px（Shift 1 px）；点一下卡片放到最前，点图片放大或缩小；窗口缩小时卡片始终留在可见范围内；
  位置、叠放顺序和大小按结果（标签）记住；左侧"重置相机布局"放回侧栏左边的默认位置；"相机画面"可以整体关掉。移动卡片
  不改变时间轴和三维视角。画面取的是管线在这个快照时刚处理完的那一帧（数据时间 × 帧率）；缺图时显示占位和帧号。
- 某个数据块损坏时，只有这个结果停止播放并在画面上方写出是哪一块；其他标签不受影响。

说明：

- 快照是 Hydra 后端发布的图，不是逐帧的；网格在运行中会被压缩（顶点数先增后减），所以不同时刻的
  网格形状会变；房间有时会时有时无。
- 物体要等跟踪窗口结束才进入场景图，所以大部分物体在后半段才出现；物体标签用的是
  `corrections.yaml` 里按语义标签取的 DAM 描述（整个时间轴上都一样），没有 DAM 时显示 `物体 O<编号>`。
- 相机轨迹是位姿图的关键帧（几十个点），不是每帧的位姿。
- CODa 的 ROS 流程不保留历史快照，它的数据包只有一个时间点：最终的 `dsg.json`（见 4.4）。CODa 的可通行区域
  （TRAVERSABILITY）节点目前不作为"地点"画出来。

原理：Hydra 大约每处理一帧就把完整场景图（含网格）通过 ZMQ 发布一次，RViz 看的就是这个流。
`scripts/replay.py record` 订阅它，每 5 份存一个快照（`REPLAY_EVERY` 调整）；建图结束后
`scripts/replay.py finalize` 把快照做成 `replay/` 数据包，`scripts/run_replay.sh` 把这几步串起来。只订阅、不渲染，
对建图速度没有可测的影响（120 帧不带 DAM：3.68 帧/秒，不发布时 3.39；298 帧带 DAM：每帧 0.38–0.39 秒，和不录制时
一样），开着 RViz 才会慢（2.13 帧/秒）。参数和环境变量同 `scripts/run_tum_example.sh`。

**恢复**：建图、录制或数据包生成失败时，原始快照留在 `output/<NAME>/.replay-recording`（`REPLAY_KEEP=1` 时成功也保留），
之后可以重新做数据包（`--dataset` 用来放相机画面和描述源帧，`--reference-mesh` 加上参考网格，都可以不给）：

```bash
docker compose run --rm -T daaam-ws python scripts/replay.py finalize output/tum_replay/.replay-recording \
  --run output/tum_replay/out_<时间> --dataset-kind tum --sequence fr1_desk \
  --dataset /datasets/tum_fr1_desk --fps 15 --out output/tum_replay/out_<时间>/replay
```

别的数据集同理：给 `scripts/run_pipeline.py` 传 `--zmq-url tcp://127.0.0.1:8001`，再用 `scripts/replay.py record/finalize`
（`--dataset-kind` 必须明确给出，程序不会按目录名猜数据集）。

**服务的边界**：只监听 127.0.0.1（`just replay-web host=localhost port=8766` 可以换端口，别的地址会被拒绝）；页面打开结果
时只发送一次路径，之后只用服务给的随机会话编号；服务只发送清单里登记过的文件，没有通用的读文件或执行命令的接口；会改变
状态的请求要求同源和启动时生成的 CSRF 令牌。服务的本地状态在仓库的 `.replay-web/`（已在 `.gitignore` 里）。

#### 场景图热更新

回看播放的是录制的快照，但后处理（9.0.2）会在结果目录里陆续产生更完整的场景图。服务每 2 秒看一次每个已打开结果的这四个
文件（`python3 -m scripts.replay_web.server --graph-interval 秒数` 可调，最短 0.2；WSL、`/mnt` 盘和 Docker 的写入没有统一的
文件通知，所以用轮询），**按这个固定顺序选第一个有效的**：

1. `clustered_dsg_with_summaries.json`（聚类 + 区域摘要）
2. `clustered_dsg.json`（聚类）
3. `dsg_updated.json`（物体句向量）
4. `dsg.json`（管线原始输出）

"有效"指整个文件是完整的 UTF-8 JSON、有节点和边列表、在结果目录之内（符号链接指到目录外的不算）、不超过 512 MiB。
文件被写到一半或损坏时，页面保留上一个有效版本，并在画面下方提示一次"`xxx.json` 无效,继续使用 `yyy.json`:原因"；
当前文件被删除就自动退到下一个有效的，全部没有就显示"没有可用文件"。文件内容变了（哪怕大小和修改时间没变）才算新版本，
只"碰"一下文件不会触发更新。后处理脚本先写临时文件再整体改名（`--atomic`），所以不会出现读到半个文件的情况。

页面里：左上面板的"场景图 …"一行写出当前使用的文件、版本号和物体、房间数；换版本时当前标签页**不动时间、三维视角、图层开关
和相机画面**，只换细节——侧栏多了"房间"列表（房间标签、摘要、地点数），物体和地点的详情会写出它在场景图里属于哪个地点、
哪个房间。选中的物体如果上一个版本有、新版本没有了，选中会被清掉并提示原因；本来就不在场景图里的不受影响。每个版本在画面
下方提示一次。背景标签页只更新标签上的小蓝点，切过去时才应用最新版本（所以不会白白占用渲染）。服务重启或别的页面关掉了
这个结果时，标签页会按路径自动重新打开并保持原来的视图。

只想回看时，原始 `dsg.json` 就够用；区域摘要要用到后处理的结果，页面里的问答（9.0.3 节）要用带句向量的 `dsg_updated.json` 或更靠后的文件。

### 9.0.2 在页面里做后处理（Postprocess / Summarize / 全部运行）

每个结果标签页的右侧栏有"任务"页，三个按钮对应 `scripts/postprocess_coda.sh` 的三个阶段：

| 按钮 | 做什么 | 产生 | 前提 |
|---|---|---|---|
| **Postprocess** | 物体描述的句向量 + 地点聚类成房间 | `dsg_updated.json`、`clustered_dsg.json` | 结果里有管线的 `dsg.json` |
| **Summarize** | 用 OpenAI（`SUMMARY_MODEL`，默认 gpt-5-nano）给每个房间写摘要 | `clustered_dsg_with_summaries.json`、`region_summaries.yaml` | 已有 `clustered_dsg.json`；仓库 `.env`（或服务的环境）里有 `OPENAI_API_KEY` |
| **全部运行** | 依次做上面两步，第一步失败就停 | 以上全部 | 同上 |

- 命令是固定的（不拼接页面上的任何文字）：`docker compose run --rm -T --name daaam-replay-job-<编号> -v <结果目录>:/selected-result …
  daaam-ws bash scripts/postprocess_coda.sh --atomic --stage <阶段> /selected-result`。结果目录就是打开的那个目录，数据集类型来自
  数据包清单（TUM 会带上 `config/cluster_places_config_tum.yaml` 和 `PLACES_LAYER=PLACES`，CODa 用脚本默认值），不看目录名。
  容器里只挂这一个结果目录。**需要 Docker 在运行**；API 密钥只在仓库的 `.env` 里，由容器读取，不会发给浏览器。
- 按钮由服务端状态决定，页面不猜文件名：前提不满足时按钮灰着，旁边写出原因（例如"Summarize needs clustered_dsg.json: run
  Postprocess first"、缺 `OPENAI_API_KEY`）；这个结果有任务在排队或运行时三个按钮都等着。
- **重跑要确认**：阶段的产出已经存在时按钮写成"重新运行 …"，点了先弹出确认并列出会被替换的文件，确认后才发送 `replace`；
  新文件整批写好、校验过才改名替换，失败或取消不会改动现有文件。
- 同一个结果同一时间只有一个任务；不同结果的任务最多同时跑 `--max-jobs` 个（默认 1，其余按顺序排队；用
  `python3 -m scripts.replay_web.server --max-jobs 2` 启动可以调大，一个任务要几 GB 内存和显存，先想好装得下）。另一个 DAAAM 管线、
  深度估计、后处理或问答的容器在跑时不会启动任务（和 `just` 配方的 `_coda-guard` 一样：显存和内存装不下两个）。
- **进度**：状态行、最近几次任务（成功 / 失败 / 已取消 / 已中断，失败的显示退出码和"重试"）和最近一次任务最多 500 行的输出，
  输出是实时的（容器里 `PYTHONUNBUFFERED=1`）。任务成功后新文件马上被场景图热更新选中，失败或取消的任务**不会**改变当前显示的
  场景图。背景标签页只在标签上显示小圆点（琥珀色 = 在运行，绿色 = 在背景时完成了，红色 = 失败或被取消），切回来时再显示输出。
- **取消**：先对任务自己的容器执行 `docker stop`（脚本收到 SIGTERM 立刻停下当前步骤并删掉自己的临时文件），一般 3 秒内结束；
  容器还没创建时会重试，10 秒内停不下来才去终止 `docker compose` 客户端，最后强杀。任务要等容器真的消失才算结束。
  强杀留下的 `.replay-job-*` 临时文件和写到一半的发布，由服务从宿主机清理或回滚到上一版。
- **服务重启**：`.replay-web/jobs/<任务编号>/` 里每个任务有一份记录（`job.json`）和一份最多约 256 KB 的 `log.txt`。重启后仍是"排队中 /
  运行中"的任务一律改成"已中断"并停掉它的容器；历史按结果路径保留，结果重新打开（新的会话编号）后还能看到。想清空就停掉服务后
  删除 `.replay-web/`。
- 命令行等价物不变：`just coda-post` / `just coda-summarize`、`just tum-post` / `just tum-summarize`；脚本本身多了
  `--stage postprocess|summarize|all` 和 `--atomic`（`--summaries` 仍是 `--stage all` 的旧写法）。真机上测得：TUM 的 30 帧小结果
  Postprocess 约 20 秒、Summarize 约 3 秒；CODa 的 11 个物体的小结果 Postprocess 约 18 秒；有 208 个物体、2 个房间的 CODa 结果
  Postprocess 约 34 秒、Summarize（2 次 OpenAI 调用）约 36 秒。第一次后处理要加载 sentence-t5-xl（2.5 GB，缓存在
  `/data/hf_cache`）。

### 9.0.3 在页面里问场景问题（问答）

每个结果标签页的右侧栏有三页：实体 / 任务 / **问答**。对话**属于这个结果**：服务把它存在 `.replay-web/history/`，刷新页面、换一个
窗口、重启服务后都还在，别的结果看不到它；"清空对话"（会先确认）只删这个结果的记录，不影响别的结果，文件不会被删。

**前提**

- 结果当前显示的场景图要带句向量，也就是跑过 Postprocess：`dsg_updated.json`、`clustered_dsg*.json` 可以，原始 `dsg.json` 不行——
  面板写"请先运行 postprocess（场景图要有句子嵌入才能提问）"，发送按钮灰着。"任务"页在跑 Postprocess / Summarize 时也能提问，用的是
  最后一个有效的场景图（回答上写着是哪个文件）。
- 模型的 API key：OpenAI 的模型要 `OPENAI_API_KEY`，`claude-*` 要 `ANTHROPIC_API_KEY`。放在仓库的 `.env` 里（容器自己读），或在启动
  服务的 shell 里 `export`（服务只把变量**名**交给 `docker compose run -e NAME`，值由 Docker 从环境里复制，不会出现在任何命令行上）。页面只知道
  "有 / 没有"：没有 key 的模型在下拉框里灰着并写"（没有 key）"，面板写出缺哪个变量；key 的值不会发给浏览器，也不会写进记录。
- Docker 在运行：回答来自 `daaam-ws` 容器里的一个共享进程（见下面的"worker"）。
- 能选的模型是白名单：环境变量 `REPLAY_AGENT_MODELS`（逗号分隔，默认只有 `gpt-5-mini`，第一个是默认），或者
  `python3 -m scripts.replay_web.server --agent-models gpt-5-mini,claude-sonnet-4-5`；`--no-agent` 把问答整个关掉（面板灰着，
  不会启动任何容器）。`just replay-web` 不带这些参数，要用就直接运行上面的命令（和 `--max-jobs` 一样）。

**怎么问**

- 在输入框里写问题（最多 4000 字），`Ctrl+Enter` 或"发送"。一个结果同一时间只回答一个问题；不同结果的问题在同一个 worker 里排队，状态行
  写"排队中，前面还有 N 个问题"。
- 回答时状态行显示进度：`第 N 轮`、`查询场景: <工具名>`、`正在整理答案…`；答案是模型整理好之后一次出现的（长答案分块到达），不是逐字生成。**不显示**提示词、工具的参数和返回的原始内容，
  也不显示模型的推理文字（就算服务商返回了）。
- 助手只有四个**只读**的场景工具：`get_matching_subjects`、`get_objects_in_radius`、`get_region_information`、
  `get_agent_trajectory_information`。Postprocess、Summarize 这些任务**不是**助手的工具，助手不能启动任务，也读不到别的文件或命令。
- **多轮**：后面的问题会带上这个结果里已经得到回答的问题和答案（最多 56,000 字节、200 轮，更早的压成每轮一行的摘要）；出错或没得到回答的
  问题不会进入上下文。所以可以直接用代词，例如先问"场景里有电话吗？"，再问"它旁边还有什么？"。
- **包含当前视图上下文**（默认勾选）：把时间轴上的时间、选中的实体和 3D 相机位置一起告诉助手，这样可以问"这是什么？"。选中的实体页面只发编号，
  描述由服务从场景图里读出；场景图里没有的编号被丢掉。取消勾选就只发问题。问题上的"含当前视图"标记表示那一问带了视图。
- **停止显示**：不再等这个答案；助手仍会答完，答案之后出现在对话里（页面每 2 秒看一次，最多 5 分钟，之后重新打开标签页再看）。关掉页面也一样。
- 出错时对话里出现一条红色的固定说明和"重试"：worker 没启动（Docker 没开？）、超时、模型服务拒绝了 key / 限流 / 连不上、这个账号用不了所选模型、
  场景图还不能回答……失败的那一问留在对话里，重试不会把它当作上下文。

**场景图版本**

- 每条回答写着 `模型 · 场景图文件 · 版本短码`（短码是文件内容 SHA-256 的前 6 位）。提问那一刻选中的场景图是这一问用的版本；提问过程中热更新
  换了文件，只影响**下一个**问题。
- 热更新选中了另一个场景图后，对话末尾先出现虚线提示"场景图已更新：A → B。下一个问题会用新的版本。"，下一次提问时它变成一条分隔行；旧回答保留
  自己的出处。

**记录和缓存（都在 `.replay-web/` 里，已被 git 忽略；停掉服务后可以删除）**

- `history/<结果规范路径的 SHA-256>/messages.jsonl`：每行一条（问题 / 回答 / 分隔 / 出错），带模型、场景图文件名和版本、提问时的视图；
  `meta.json` 记着结果的路径。**不存**提示词、工具参数、模型的推理文字，更没有 key。
- `cache/<同一个键>/<场景图 SHA-256>.json`：交给 worker 的场景图快照（CODa 还有 `….background.yaml`），每个结果只留最新 2 份，整个目录有
  大小上限；worker 只读这里，不读结果目录。

**worker（容器 `daaam-replay-agent`）**

- 第一次提问时启动：`docker compose run --rm -T --name daaam-replay-agent … daaam-ws python -m scripts.replay_web.agent_worker`。所有标签页和结果共用
  这一个进程（不会一个标签页起一个模型进程），句向量模型和 CLIP 只加载一次，最近用的两个场景图留在内存里。它的日志显示在服务的终端里
  （`[replay-web] agent: …`，key 形状的字符串会被遮盖）；CODa 场景图的 CLIP（PE-Core）在网络不好时会先打印几行 huggingface.co 重试警告，缓存里有就不影响回答。
- 本机实测（gpt-5-mini，TUM fr1_desk2 和一个 CODa 序列 0 的结果同时打开）：worker 冷启动后的第一个问题约 2.5 分钟（模型加载占大头），之后每个
  问题 30–60 秒（主要是模型自己的 2–3 轮调用）；换一个结果的第一个问题多加载一次场景图。两个结果的对话互不影响。CODa 的回答里时间是相对序列开头的秒数
  （数据包清单里的时间原点），不是绝对时间戳。
- 空闲 15 分钟后自己退出，把显存还回去（`--agent-idle-seconds N` 改，`0` = 不退出）；在页面里启动 Postprocess / Summarize 时，空闲的 worker 会立刻
  退出（实测不到 1 秒容器就没了）；Ctrl+C 停服务时它跟着退出（实测 0.4 秒）；下一个问题会再启动它。服务被强杀（`kill -9`）时，worker 在标准输入
  关闭后自己退出；万一还在：`docker stop daaam-replay-agent`。
- 命令行里经过 `_coda-guard` 的重活（`just tum-post`、`tum-summarize`、`coda-depth`、`coda-run`、`coda-chunks`、`coda-post`、`coda-summarize`、`coda-ask`）看得到
  这个 worker：它在跑时这些命令拒绝启动（显存装不下两个），提示里写着办法——等它空闲退出，或 `docker stop daaam-replay-agent`。页面里的任务不受影响
  （启动时会先让空闲的 worker 退出）。
- worker 的容器名是固定的（`daaam-replay-agent`），服务启动 worker 前会先 `docker rm -f` 同名的残留容器（强杀服务后可能留下）。所以**同一台机器上只开一个回看服务**：
  第二个服务启动 worker 时会把第一个服务的 worker 当残留删掉，那边正在回答的问题以"问答进程中途退出了"失败，重试即可。

### 9.1 Rerun 可视化

```bash
python scripts/run_static_visualizer.py \
  --dsg output/my_run/clustered_dsg_with_summaries.json \
  --color-map config/labels_pseudo.csv \
  --spawn
```

Docker 用户需要先按照 [DOCKER.md](DOCKER.md) 配置 X11。无 GUI 环境可使用
`--no-spawn`，或在有桌面的机器上读取输出文件。

### 9.2 交互式场景问答（命令行）

日常用页面里的问答（9.0.3 节）；命令行入口 `scripts/demo_query.py` 适合在容器里直接试，TUM 和 CODa 的场景图都能问。用同一套加载和
只读工具（和页面里的问答一样）：

```bash
export OPENAI_API_KEY=sk-...
# TUM：时间从 0 开始，不需要序列号
python scripts/demo_query.py \
  --dsg-path output/tum_replay/out_<时间>/clustered_dsg.json \
  --dataset-kind tum
# CODa：--seq-id 给出序列开头的时间（也可以用 --time-origin 直接给）
python scripts/demo_query.py \
  --dsg-path output/my_run/clustered_dsg_with_summaries.json \
  --seq-id 0 \
  --model-name gpt-5-mini
```

启动后直接输入问题；输入空行或按 `Ctrl+C` 退出。只给 `--seq-id` 时仍按 CODa 序列处理。

### 9.3 查看数据集的图像和视频

TUM fr1/desk 在仓库里有两份：`datasets/raw/rgbd_dataset_freiburg1_desk/` 是原始数据（613 帧 RGB，
30 Hz，带畸变），`datasets/tum_fr1_desk/` 是 `scripts/prepare_tum_rgbd.py --stride 2` 转换后管线实际用的
那份（298 帧，15 fps，已去畸变，所以图像四周有黑边）。其他测试序列（9.5 节）的转换结果同样在
`datasets/tum_<序列名>/`，下面的命令把 `tum_fr1_desk` 换成它们就行。

- **看图像**：在 Windows 资源管理器里打开
  `\\wsl.localhost\Ubuntu-20.04\home\arclunar\warmoon_ws\DAAAM\datasets\tum_fr1_desk\rgb`，双击一张，
  用"照片"按 ←/→ 翻页（VS Code 里点文件也能预览）。`depth/` 里是 16 位 PNG（单位 1/5000 米），用普通
  看图软件几乎是全黑的，用下面的视频看。
- **看视频**（WSL 里，需要 ffmpeg 和 OpenCV，都已装好）：

  ```bash
  python3 scripts/make_dataset_video.py datasets/tum_fr1_desk
  # -> output/dataset_preview/tum_fr1_desk.mp4：左边 RGB，右边彩色深度（蓝近红远、黑色没有测到），带帧号和时间
  python3 scripts/make_dataset_video.py datasets/tum_fr1_desk --no-depth    # 只要 RGB
  ```

  H.264 编码，Windows 里直接双击就能播放。原始的 30 Hz 序列只要 RGB 的话，一条 ffmpeg 命令：

  ```bash
  ffmpeg -framerate 30 -pattern_type glob -i 'datasets/raw/rgbd_dataset_freiburg1_desk/rgb/*.png' \
    -c:v libx264 -pix_fmt yuv420p -crf 20 output/dataset_preview/tum_fr1_desk_raw_rgb_30fps.mp4
  ```

  `make_dataset_video.py` 适用于任何 `rgb/` + `depth/` + `camera_info.json` 布局的数据集（也就是
  `prepare_tum_rgbd.py` 写出的布局）。

### 9.4 数据集的信息和原始帧率在哪

- **官方网页**：TUM RGB-D 主页 <https://cvg.cit.tum.de/data/datasets/rgbd-dataset>。上面写着 "The data was
  recorded at full frame rate (30 Hz) and sensor resolution (640x480)"：RGB-D 是 30 Hz，动捕真值 100 Hz，另有
  Kinect 的加速度计数据。主页的 Quick Links 有下载页、文件格式（`rgb.txt` 等怎么读，
  <https://cvg.cit.tum.de/data/datasets/rgbd-dataset/file_formats>）、相机内参和工具。许可是 CC BY 4.0，
  使用时请引用 Sturm 等人的 IROS 2012 论文（主页上有 BibTeX）。
- **每个序列的数据**：下载页 <https://cvg.cit.tum.de/data/datasets/rgbd-dataset/download#freiburg1_desk> 里，
  fr1/desk：时长 23.40 s（带真值 23.35 s）、真值轨迹长 9.263 m、平均速度 0.413 m/s、平均角速度 23.3°/s、
  轨迹范围 2.42 × 1.34 × 0.66 m；还有 tgz、ROS bag、"downsampled bag"（带点云）和三个视频的链接：
  [RGB](https://cvg.cit.tum.de/rgbd/dataset/freiburg1/rgbd_dataset_freiburg1_desk-rgb.avi)、
  [深度](https://cvg.cit.tum.de/rgbd/dataset/freiburg1/rgbd_dataset_freiburg1_desk-depth.avi)、
  [外部相机视角](https://cvg.cit.tum.de/rgbd/dataset/freiburg1/rgbd_dataset_freiburg1_desk-external.avi)
  （avi；页面里内嵌的是 Flash 播放器，现在的浏览器打不开，直接下载 avi 用 VLC 等播放）。
- **自己测帧率**：每个传感器的时间戳都在原始序列的 `.txt` 里（每行一个），平均间隔就是帧率：

  ```bash
  awk '!/^#/{n++; if(!t0)t0=$1; t=$1} END{printf "%d 条, %.2f s, 平均 %.1f Hz\n", n, t-t0, (n-1)/(t-t0)}' \
    datasets/raw/rgbd_dataset_freiburg1_desk/rgb.txt
  ```

  实测：RGB 613 帧 / 20.40 s = 30.0 Hz（帧间隔 29–39 ms，有抖动，不要用单个间隔或中位数算）；深度 595 帧 /
  19.82 s = 30.0 Hz；真值 2335 条 / 23.40 s = 99.7 Hz；加速度计 11815 条 / 23.66 s ≈ 499 Hz。转换后的
  `datasets/tum_fr1_desk/timestamps.txt` 是 298 帧 / 19.80 s = 15.0 Hz；`camera_info.json` 里的 `fps: 15.0`
  是转换脚本按 30 ÷ stride 写进去的，不是测出来的。
- 网页上的 23.40 s 和 `groundtruth.txt` 的跨度一致；图像比真值晚约 3 s 才开始，所以 RGB 只覆盖其中的 20.4 s。

### 9.5 更多测试序列（TUM RGB-D 的其他序列）

默认的测试序列是 `fr1_desk`。另外下载并转换了 4 个 TUM 序列，所有 `just` 命令在最后加序列名就能换：

| 序列名 | TUM 原序列 | 相机 | 转换后（每 2 帧取 1 帧，15 fps） | 内容 | 压缩包 |
|---|---|---|---|---|---|
| `fr1_desk` | freiburg1_desk | Kinect | 298 帧，19.8 s，9.3 m | 默认：办公桌上的显示器、键盘、书、杯子 | 344 MB |
| `fr1_desk2` | freiburg1_desk2 | Kinect | 315 帧，21.3 s，9.9 m | 同一间办公室的第二次桌面录制 | 349 MB |
| `fr1_room` | freiburg1_room | Kinect | 681 帧，45.3 s，16.0 m | 绕整间办公室一圈并闭环（四张桌子和四周的墙） | 782 MB |
| `fr3_long_office_household` | freiburg3_long_office_household | Xtion，图像已校正 | 1243 帧，87.0 s，22.1 m | 家居加办公室的大回环，最长 | 1483 MB |
| `fr3_walking_xyz` | freiburg3_walking_xyz | Xtion，图像已校正 | 415 帧，28.7 s，6.5 m | 两个人在办公室里走动（动态物体） | 527 MB |

```bash
just tum-list                              # 序列、压缩包大小、有没有下载、转换了多少帧
just tum-fetch fr1_room fr3_walking_xyz    # 下载并转换（走 7890 代理，可断点续传；已转换的跳过）
just tum-test 60 fr1_room                  # 前 60 帧、不带 DAM，快速检查
just tum-run 298 fr1_room                  # 前 298 帧、带 DAM，生成回看数据包（just replay-web 查看）
just tum-run all fr1_room                  # 帧数写 all 表示整个序列
just tum-post fr1_room                     # 后处理该序列最近一次 tum-run：对象嵌入 + 地点/区域聚类
just tum-summarize fr1_room                # 同上，再用 OpenAI 生成区域摘要
just live-test fr1_room                    # RViz 实时预览（120 帧）
```

序列名是最后一个参数，不写就是 `fr1_desk`，输出目录和以前一样。其他序列的输出分开放：
`output/tum_replay_<序列名>/`（`tum-test` 是 `output/tum_rrd_test_<序列名>/`，`live-test` 是
`output/tum_live_test_<序列名>/`）。直接用脚本时设环境变量 `SEQ`，比如
`docker compose run --rm -T -e SEQ=fr1_room daaam-ws bash scripts/run_replay.sh all`；`DATASET` 和 `NAME`
仍可单独覆盖。

`tum-post` 和 `tum-summarize` 复用 4.4 节 CODa 的后处理链和命令入口，但使用 TUM 专用的
`config/cluster_places_config_tum.yaml`：TUM 聚类 `PLACES` 层并采用小场景阈值，CODa 仍聚类 `TRAVERSABILITY` 层。
`postprocess_scene_graph.py` 写入对象句子嵌入并生成 `dsg_updated.json`，`cluster_places.launch.yaml` 生成
`clustered_dsg.json`。由于上游 `cluster_places_node` 只接受 `TraversabilityNodeAttributes`，共享脚本会临时转换 TUM 的普通
Place 属性，聚类后再还原，临时文件不会保留；`tum-summarize` 的对象到区域关联也使用 `PLACES`，然后调用
`summarize_regions.py`，生成 `clustered_dsg_with_summaries.json` 和 `region_summaries.yaml`。不写结果目录时处理该
序列最新的 `tum-run`；指定某次结果时把目录名放在序列名后，例如
`just tum-summarize fr1_room out_20261016_123456`。摘要需要仓库 `.env` 中的 `OPENAI_API_KEY`；第一次后处理还可能
下载约 2.5 GB 的 sentence-t5-xl 到共享的 `/data/hf_cache`。`tum-test` 默认关闭 DAM，通常没有足够的对象描述可供摘要，
因此这些命令只查找 `tum-run` 的输出目录。

数据放在哪：下载的压缩包解压到 `E:\daaam-data\tum\rgbd_dataset_*`（容器里是 `/data/tum`，不占 C: 盘；解压后压缩包
删掉，4 个序列共 3.2 GB）；转换结果在 `datasets/tum_<序列名>/`（布局和 `tum_fr1_desk` 相同，4 个共 1.5 GB，在
C: 盘的 WSL 虚拟磁盘里）。原始序列留着，想换抽帧间隔就用
`python scripts/prepare_tum_rgbd.py <原始序列目录> <输出目录> --stride 1`（运行时再设 `FPS=30`）。序列的图像和视频
怎么看见 9.3 节。

别的序列也能下：名称写成 `fr1_xyz`、`fr2_desk` 这样（`frN_<序列>` 对应 `rgbd_dataset_freiburgN_<序列>.tgz`，序列列表在
<https://cvg.cit.tum.de/data/datasets/rgbd-dataset/download>），只要它带公开真值就行；`just tum-list` 的表里只有跑过的。
转换脚本里有 fr2 的相机内参，但 fr2 序列没有实际跑过。

在这台机器上用 `just tum-run all <序列名>`（带 DAM、默认 5 cm 配置）实测：

| 序列名 | 用时 | 物体 / 地点 / 房间 | 有 DAM 描述的标签 | 网格精度 / 覆盖（5 cm） | 容器内存峰值 | 当时的回看网页 |
|---|---|---|---|---|---|---|
| `fr1_desk` | 约 4 分钟 | 130 / 23 / 1 | — | 90% / 80% | 约 9 GB | 21 MB |
| `fr1_desk2` | 3 分 18 秒 | 127 / 29 / 1 | 161 | 91% / 72% | 9.5 GiB | 24 MB |
| `fr1_room` | 6 分 24 秒 | 370 / 85 / 3 | 452 | 92% / 86% | 10.4 GiB | 108 MB |
| `fr3_long_office_household` | 10 分 19 秒 | 350 / 55 / 3 | 454 | 97% / 87% | 12.3 GiB | 135 MB |
| `fr3_walking_xyz` | 4 分 47 秒 | 114 / 32 / 3 | 162 | 83% / 45% | 没测 | 24 MB |

网格精度 = 最终网格的顶点里，离参考点云不到 5 cm 的比例；覆盖 = 参考点云里离网格顶点不到 5 cm 的比例；参考点云是用
真值位姿把每 3 帧的深度（0.3–4 m）投回世界坐标得到的。DAM 描述的数量每次运行不同（DAM 是异步的，管线结束时还没返回的
不等）。用时和网页大小是在 `run_replay.sh` 加入 1 cm 参考网格（`REFERENCE_MESH`）之前测的，不含它：之后第一次回放某个
序列时会多建一次这个网格（缓存在 `datasets/tum_<序列名>/.daaam/` 里，`REFERENCE_MESH=off` 可以跳过）。

说明：

- **内存**：序列越长，内存越高。`fr3_long_office_household` 跑完整 1243 帧时容器峰值 12.3 GiB，主机剩余内存一度只剩
  0.7 GB、交换区用了 1.8 GB，已经贴近这台 15.9 GB 机器的极限（跑成了，没有被杀）：跑它时不要同时开别的重型任务，或者
  只跑前一部分（`just tum-run 600 fr3_long_office_household`）。GPU 仍然只放得下一次建图。
- **4 m 以外的深度被丢掉**：`--depth-ub 4.0`（`scripts/run_tum_example.sh`）和 Hydra 的 `max_range: 4.0`
  （`config/hydra_config/tum_rgbd_khronos.yaml`）是按 fr1 的 Kinect 序列设的。fr1 序列几乎没有 4 m 以外的深度
  （`fr1_room` 0.6%），但 `fr3_long_office_household` 有 6%、`fr3_walking_xyz` 有 13.5%（大厅），这部分不会进网格。
  想要的话两处一起改（没试过）。
- **动态序列**：`fr3_walking_xyz` 里走动的人会被当成物体（DAM 会描述他们的衣着），精度和覆盖明显低于静态序列
  （83% / 45%）。原因没有逐项拆开；把网格渲染出来看，桌子等静态结构是完整的，走动的人在网格里留下零碎的残片。
- **房间数**：地点和房间的阈值是在 `fr1_desk` 上调的（见 11 节“mesh 是碎片”），没有按场景再调：桌面序列得到 1 个房间，
  大场景得到 3 个。
- **回看**：上表最后一列是当时生成的自包含网页的大小（长序列 108–135 MB，整个加载）。现在的结果改为 `replay/` 数据包，
  由 `just replay-web` 按块加载：`fr3_long_office_household` 保留的录制做成的数据包是 126 MB（17 个时间块 + 28 MB 参考网格
  + 7.7 MB 图片），打开时只取最后两块，约 1 秒出第一帧。那几个旧网页不能在新工作区里打开。

### 9.6 1 cm 参考网格（回看工作区里的静态网格）

回看数据包默认带一个静态的 **1 cm 参考网格**：用 Open3D 的 TSDF（体素 1 cm、截断 4 cm、深度只取 4 m 以内、带颜色），把数据集
**整段序列**的每一帧 RGB-D 按准备好的相机位姿融合成一张网格。它和 Hydra 的网格互补：Hydra 的网格是建图过程中随时间变化的、
5 cm 体素，用来看"当时 Hydra 有什么"；参考网格细得多、固定不变，用来判断物体框和场景几何对得准不准。它叫**参考网格，不是
真值**：位姿虽然来自 TUM 的动捕真值，但深度图有噪声，标定和位姿插值有误差，TSDF 的参数也会影响表面。回看页面和这份文档里都不叫
它"真值网格"。

```bash
just reference-mesh fr1_desk              # 预先构建（或检查）这个序列的缓存；已有有效缓存就直接用，不重建
just reference-mesh fr1_room rebuild      # 强制重建
just tum-run 298                           # 默认 REFERENCE_MESH=auto：缓存有效就用，没有就在建图结束后建一次
REFERENCE_MESH=off just tum-run 298        # 不要参考网格：数据包里只有动态 Hydra 网格
REFERENCE_MESH=rebuild just tum-run 298    # 重建缓存；建不出来整条命令就失败（auto 只给警告）
REFERENCE_MESH_CACHE=/datasets/cache/tum_fr1_desk just tum-run 298  # 数据集目录只读时把缓存放到别处（每个数据集一个目录）
```

`just tum-run` 和 `just tum-test` 把 `REFERENCE_MESH`、`REFERENCE_MESH_CACHE` 原样传进容器。重建放在建图**结束之后**，不占建图的
速度；直接用脚本是 `python scripts/build_reference_mesh.py <数据集目录> [--cache-dir DIR] [--force]`（只把缓存路径写到
stdout，进度和警告写 stderr），`scripts/replay.py finalize … --reference-mesh <缓存>.npz` 把现成的缓存放进数据包的
`reference/`（用 `REPLAY_KEEP=1` 保留录制，之后就能重新生成数据包）。

- **缓存**：`<数据集>/.daaam/reference_mesh/open3d_tsdf_1cm.npz`（`positions` / `colors` / `normals` / `triangles` 四个数组，
  不抽稀）和 `open3d_tsdf_1cm.json`（清单：指纹、参数、帧数、顶点和三角形数、范围、Python / NumPy / Open3D 的版本）。两个
  文件都是先写临时文件再改名，半成品不会被当成缓存。数据集目录在 `datasets/`（`.gitignore` 已忽略）。
- **什么时候失效**：指纹覆盖 `camera_info.json`、`pose/poses.txt`、`timestamps.txt` 的内容，`rgb/` 和 `depth/` 里每个文件的
  名字、大小和修改时间，重建参数和缓存格式的版本；任何一项变了（包括把数据集拷到别处——修改时间会变）都会重建一次，清单损坏、
  数组文件和清单对不上也一样。重建只要几十秒（见下表）。
- **回看页面里**：左侧"网格"下拉有三项：`1 cm 参考网格`（默认）、`动态 Hydra 网格`、`隐藏网格`，下面一行说明当前是哪一种（参考网格会
  写明"整段序列的重建，不是真值"）。参考网格是静态的：拖进度条时它不变，也不会重新上传显存；"提亮"和"只画顶点"对当前的网格
  生效。没有参考网格的数据包（`REFERENCE_MESH=off`）没有第一项，默认就是动态网格。参考网格是数据包里单独的
  `reference/reference.json.gz`，打开结果时只取一次；索引超过 65,535 个顶点用 32 位索引，浏览器不支持时退回只画顶点并在说明里提示。
- **依赖和失败**：需要 Open3D（`requirements.txt` 里的 `open3d==0.19.0`，wheel 约 450 MB）；只嵌入已有缓存不需要它。
  `REFERENCE_MESH=auto` 下 Open3D 没装、数据集只读（提示用 `REFERENCE_MESH_CACHE`）或重建失败，都只打印警告，数据包照常做出来，
  只是没有参考网格。帧缺 RGB、深度或位姿、解码失败会被计数、跳过并警告（超过 10% 单独强调）；标定不对、图像尺寸和
  `camera_info.json` 对不上、一帧都融合不进去、网格为空是致命错误。
- **本机的 Open3D**：2026-10-16 经你同意，在容器工作区的 venv 里用 `pip install --no-deps open3d==0.19.0` 只装了 wheel
  （447.7 MB，装完约 1 GB 在 docker 卷里）。`import open3d` 会顺带 import plotly、dash（它的网页可视化）和 addict（它的 ML
  工具包），`--no-deps` 没有装它们；`build_reference_mesh.py` 在导入 Open3D 的那一刻，这三个包缺失就用空模块顶替（只在这一次
  import 期间登记，装了真包就用真包），所以没有再下载；要装真包是 `pip install plotly dash addict`，按 `requirements.txt` 正常
  安装（带依赖）则完全不需要这个顶替。`E:\daaam-bundle` 里的部署包是装它之前导出的，没有 Open3D。

在这台机器上（CPU，`ScalableTSDFVolume`，建图结束之后单独跑，序列越大越慢、内存越高）实测：

| 序列 | 融合帧数 | 用时 | 顶点 / 三角形 | 缓存 | 峰值内存 |
|---|---|---|---|---|---|
| `fr1_desk` | 298 | 6 s | 761,282 / 1,393,202 | 19.4 MB | — |
| `fr1_desk2` | 315 | 7 s | 1,076,348 / 1,971,647 | 27.4 MB | 1.5 GB |
| `fr3_walking_xyz` | 415 | — | 906,876 / 1,600,859 | 23.1 MB | 1.4 GB |
| `fr1_room` | 681 | 16 s | 2,424,888 / 4,452,869 | 61.8 MB | 3.2 GB |
| `fr3_long_office_household` | 1243 | 26 s | 1,565,579 / 2,872,450 | 39.6 MB | 1.8 GB |

端到端用 `REFERENCE_MESH=rebuild just tum-run 298`（`fr1_desk`）：建图仍是 298/298 帧、130 个物体 / 23 个地点 / 1 个房间，和原来一样；建图
结束后重建 6 秒；参考网格的负载 14.3 MB（当时内嵌进单个网页，页面从 21 MB 变成 36.5 MB；`.rrd` 不受影响，22 MB）。现在它是数据包里
单独的成员，只在打开结果时下载一次，不会让时间轴的数据块变大（`tum-test 30` 的 `fr1_desk` 数据包：参考网格 14.5 MB，8 个快照的
时间轴 0.3 MB）；`finalize` 会打印参考网格的顶点数、三角形数和负载大小，**不会**悄悄把网格抽稀。

在浏览器里（`fr1_desk` 当时的真实页面）打开检查过：默认是参考网格；切到动态网格是 20,165 个顶点（当前快照），切到隐藏是 0；在参考网格
下拖动进度条经过三个快照，没有重新上传；"全部"标签下 155 张卡片，转动和缩放后卡片集合不变；"隐藏"后没有可见的卡片，右侧
130 个物体的列表还在。

#### 标签模式和选中框

- 标签（浮动的描述卡片）有三种：`仅选中`（默认，只给选中的物体或地点显示）、`全部`（物体、地点、房间、楼宇都显示）、`隐藏`
  （一键收起所有卡片，右侧的实体列表和详情不受影响）。`全部` 下不再按相机位置剔除重叠的卡片，也没有 80 张的上限：卡片可以
  重叠，但哪些卡片存在只在换快照、选择、点右侧的眼睛、换模式或切换图层时才变，转动、平移、缩放只改位置；卡片只有在自己的锚点
  跑出画面或在相机后面时才不显示。选中的卡片画在最上面，带琥珀色外框。
- 选中的物体框是一层 2D 叠加：琥珀色（`#ffb000`）4 px 的线，外面一圈约 10 px 的深色光晕，八个角有圆点；它不依赖深度，透过
  任何一种网格都看得见，"物体框"模式选"隐藏"时也显示，不拦截点击（点击选择仍按"相机视锥里裁剪后的框、重叠时选更小更近的"）。
  选中的节点圆点也由白色改成了琥珀色；右侧列表里关掉这个物体的眼睛，框和卡片一起消失。

#### 测试

`just test` 一次跑完三部分：

- 宿主机（Python 3.8 标准库）：回看数据包清单的契约和路径穿越边界（`tests/test_replay_manifest.py`）、TUM / CODa 的数据集布局
  （`tests/test_replay_datasets.py`）、各种路径写法和已打开结果的注册表（`tests/test_replay_web_paths.py`、`test_replay_web_runs.py`）、
  在临时端口上真正起服务的 HTTP 契约（`tests/test_replay_web_server.py`：CSRF / 同源 / Host 检查、gzip 数据块和缓存头、
  只能取清单里登记的图像、`/etc/passwd` 之类拿不到、`/graph`、两种 SSE 事件流、任务接口的授权 / 冲突 / 确认 / 取消），场景图的选择
  与监控（`tests/test_replay_artifacts.py`：四个文件的优先级、无效的新文件不替换旧的、同大小同时间的内容变化靠摘要发现、删除后退到
  下一个、一个结果出错不影响另一个）、事件代理（`tests/test_replay_events.py`：有界历史、`Last-Event-ID` 续传、历史缺口或读得太慢时
  只补一条当前状态、关闭时唤醒所有订阅者）、任务管理器（`tests/test_replay_jobs.py`：用假进程和假 Docker，不会启动容器，覆盖固定命令、
  一个结果一个任务、全局排队、前提和确认、日志有界、先停容器再杀客户端、强杀后清理、重启后的中断恢复），以及原有的脚本测试
  （`tests/test_coda_scripts.py` 里 `postprocess_coda.sh` 的阶段、原子发布、回滚、SIGTERM 清理）和 justfile 测试。
- 容器：数据包生成（`tests/test_replay_bundle.py`：清单最后写入、按快照数和字节数分块、图片在包外、原子发布与失败回退、
  CODa 按数字排序和时间原点、从最终 `dsg.json` 生成单快照数据包）、`tests/test_reference_mesh.py`（指纹和缓存契约、位姿求逆、
  帧跳过、Open3D 日志不进 stdout、真 Open3D 重建一面 1 m 外的墙）、`tests/test_replay_reference_mesh.py`（参考网格单独量化、
  32 位索引、畸形缓存被拒绝且不留数据包、不导入 Open3D、查看器源码不引用外部网络）、`tests/test_run_replay.py`（`auto / off / rebuild`、
  缓存目录、失败时保留原始快照、不再生成网页）。
- Windows 上带 Chrome 的真实浏览器测试（`tests/test_replay_browser.py`、`tests/test_replay_workspace_browser.py`）：测试进程在
  Windows 的 Python 里驱动 Chrome，`wsl.exe` 在 WSL 里启动 `tests/replay_web_fixture_server.py`（用真正的 `finalize` 生成数据包并
  起服务），Chrome 通过 WSL 的 localhost 转发访问它。覆盖三种网格切换且不重复上传、标签集合在转动时不变、选中框的颜色 / 光晕 /
  像素 / 点击穿透、描述源帧不改时间、按时间戳播放、首屏只取两个数据块、损坏的数据块只影响一个结果、多标签页各自的时间和
  渲染、后台暂停与恢复、刷新后恢复视图、相机画面的拖动 / 置顶 / 键盘 / 不出界 / 重置、场景图热更新（升级 / 回退 / 无效文件 /
  选中被清掉 / 背景标签页 / 服务忘掉结果后自动重开）、任务面板（假容器：流式输出、确认、取消、失败重试、缺密钥、背景标签页的小圆点）。
  浏览器测试需要 Windows 的 Python 装了
  Selenium 和 Chrome，否则 `just test` 会提示它被跳过；在装了 Selenium 和 Chrome 的 Linux 上可以直接
  `RUN_BROWSER_TESTS=1 python -m unittest tests.test_replay_browser tests.test_replay_workspace_browser -v`。

## 10. OC-NaVQA 评测

评测需要序列 `0、3、4、6、16、21、22` 的完整后处理结果：

```bash
export OPENAI_API_KEY=sk-...
python scripts/eval_navqa.py \
  0=output/coda/seq0/clustered_dsg_with_summaries.json \
  3=output/coda/seq3/clustered_dsg_with_summaries.json \
  4=output/coda/seq4/clustered_dsg_with_summaries.json \
  6=output/coda/seq6/clustered_dsg_with_summaries.json \
  16=output/coda/seq16/clustered_dsg_with_summaries.json \
  21=output/coda/seq21/clustered_dsg_with_summaries.json \
  22=output/coda/seq22/clustered_dsg_with_summaries.json
```

结果写入 `output/oc_navqa/`。评测默认执行三个随机种子，并使用仓库中修正后的
标注；指标解释、结果格式和论文参考值见 [EVAL.md](EVAL.md)。

## 11. 常见问题

### 找不到 `labels_pseudo.yaml` 或 `labels_pseudo.csv`

先完整构建 DAAAM-ROS，然后重新 source 工作区。这两个文件不是仓库中的静态
文件，而是构建期间生成的文件。

### 找不到 `hydra_python`

Python requirements 不包含 Hydra Python bindings。需要完成整个 ROS 2 工作区的
colcon 构建，并 source `install/setup.bash`。

### 找不到 FastSAM 权重

DAAAM 不会自动回退下载 FastSAM。检查 `checkpoints/fastsam/` 中是否存在配置所
引用的确切文件名，并确认 `.pt`/`.engine` 与配置一致。

### TensorRT engine 无法加载或 GPU 异常

不要复制其他 GPU 上生成的 engine。删除旧 engine，在当前机器和当前容器中
重新导出，并确保 PyTorch CUDA、TensorRT 和驱动兼容。

### rosbag 播放后没有数据或 TF

确认播放命令包含 `--clock` 和 `/tf_static` QoS 覆盖文件，并等待 DAAAM/Hydra
节点初始化后再按空格开始播放。

### GPU 显存不足

优先减小 SAM 输入尺寸或改用较小模型、减少 grounding worker 数量，并关闭不
需要的逐帧/选帧 CLIP 特征。运行句子嵌入后处理前可以先结束主管道以释放显存。

### 输出不完整

查看 `logs/` 和 `final_stats.json`。如果进程被强制终止，重新运行该序列；不要
把截断的 `background_objects.yaml` 或未完成的 DSG 继续用于评测。

### mesh 是碎片、有重影，或缺了大片表面

用 Kinect 一类有空洞的深度图（TUM RGB-D）时，要同时满足两条。`config/hydra_config/tum_rgbd_khronos.yaml`
和 `hydra.skip_invalid_depth`（默认开）现在都已经这样设置：

- `active_window.projective_integrator.extra_integration_distance` 保持 `0`。clio 配置里的 `1.0` 会在每个表面
  后面再积分一层 1 m 深的"内部"，mesh 里就多出一层落在真实表面后 0.2–1.5 m 的"鬼面"，以及沿视线方向的长条
  （fr1/desk 上 62% 的 mesh 顶点是这样来的，只有 23% 在实测表面 5 cm 以内）。
- 没有测到深度（值为 0）的像素要让 Hydra 跳过：`hydra.skip_invalid_depth` 把它们设成 1 mm。Hydra 把深度 0 变成
  NaN，而它的 TSDF 积分器（`ProjectiveIntegrator::computeSDF` 里的 `std::min(截断距离, sdf)`）遇到 NaN 会返回截断
  距离，也就是把整条射线当成空闲空间。Kinect 深度图里黑色或反光物体、遮挡阴影处的空洞，就会把别的帧测到的表面
  削掉；结果是 mesh 只剩近处（1 m 以内）的一小部分，暗色的显示器、笔记本几乎都没有。

在 fr1/desk 上（5 cm 体素，298 帧，拿深度图按真值位姿反投影出的表面做参考）：落在实测表面 5 cm 以内的 mesh 顶点
23% → 90%，被 mesh 覆盖的实测表面 17% → 80%，连通片数 811 → 158（最大的一片占全部面的 84%）。

代价：深度空洞不再产生"空闲空间"，地点（places）和房间只能靠真正观测到的空闲空间来建；yaml 里放宽了 Voronoi 和
房间的阈值（理由写在文件头的注释里），fr1/desk 上得到 23 个地点、1 个房间（之前是 35 个、1 个）。想回到旧行为：
`--config-overrides hydra.skip_invalid_depth=false`，并把 yaml 里的 `extra_integration_distance` 改回 `1.0`。

## 12. 相关文档

- [INSTALL.md](INSTALL.md)：依赖、工作区结构和手动安装
- [DOCKER.md](DOCKER.md)：容器构建和宿主机集成
- [DEPLOY.md](DEPLOY.md)：把配好的环境打包并一键部署到其他 GPU 服务器
- [RUNNING.md](RUNNING.md)：完整 CODa/ROS 2 工作流和参数
- [EVAL.md](EVAL.md)：OC-NaVQA 评测
- [CODEBASE.md](CODEBASE.md)：代码结构和服务架构
- [data/DATA.md](data/DATA.md)：评测数据说明
