export PATH := parent_directory(just_executable()) + ":" + env_var("PATH")
# DAAAM 算法运行、数据准备与结果查看。
set dotenv-load := true
proxy := env_var_or_default("HTTPS_PROXY", "")
# 结果目录与 JellyMap 工作区（可选，见 .env.example）：不设时结果在仓库内 output/，scripts/gs_world 用仓库里的副本
output_dir := env_var_or_default("DAAAM_OUTPUT_DIR", "output")
gs_world_dir := if env_var_or_default("JELLYMAP_ROOT", "") == "" { "scripts/gs_world" } else { env_var("JELLYMAP_ROOT") / "sm-lab/datasets/scripts/gs_world" }

default:
    @{{just_executable()}} --list


# 处理已准备的统一 RGB-D 目录；可追加帧数、--target-fps、--dry-run。
[positional-arguments]
run dataset frames="all" *args:
    python3 scripts/run_rgbd.py --caller-dir {{quote(env_var_or_default("WARMOON_CALLER_DIR", invocation_directory()))}} "$@"

# 进入 DAAAM Docker 容器的交互式 Bash（原 just run）。
shell:
    docker compose run --rm daaam-ws bash


# 进入已经运行的 DAAAM 容器
exec container="":
    python3 docker/dev.py exec {{ quote(container) }}

# 增量编译整个工作区或指定包及其依赖，例如 just build hydra_ros
build *packages:
    python3 docker/dev.py build {{ quote(packages) }}

# 列出可用的 TUM 测试序列(名称、大小、是否已下载);名称就是下面各命令的 seq 参数
tum-list:
    docker compose run --rm -T daaam-ws python scripts/fetch_tum.py list

# 下载并转换一个或多个 TUM 序列,例如 `just tum-fetch fr1_room fr3_walking_xyz`(可断点续传;原始数据放 E:\daaam-data\tum)
tum-fetch +names:
    docker compose run --rm -T -e https_proxy={{ proxy }} daaam-ws python scripts/fetch_tum.py get {{ names }}

# 在 TUM 数据集上运行建图(不开 RViz、带 DAM,298 帧约 4 分钟),跑完生成 replay/ 回看数据包(用 just replay-web 打开)和可在 Rerun 播放的 replay.rrd；seq 选序列,输出在 output/tum_replay_<seq>
tum-run frames="298" seq="fr1_desk":
    bash scripts/run_tum_replay.sh {{quote(frames)}} {{quote(seq)}}

# 后处理指定 TUM 环境最近一次 tum-run 结果:对象句子嵌入 → 地点聚类成区域;runs 可写一个或多个 out_<时间> 目录名
tum-post seq="fr1_desk" *runs: _coda-guard
    docker compose run --rm -T -e RESULT_ROOT=output/tum_replay{{ if seq == "fr1_desk" { "" } else { "_" + seq } }} -e CLUSTER_PLACES_CONFIG=config/cluster_places_config_tum.yaml -e PLACES_LAYER=TRAVERSABILITY daaam-ws bash scripts/postprocess_coda.sh {{ runs }}

# 同上,再用 OpenAI(gpt-5-nano,要 .env 里的 OPENAI_API_KEY)为 TUM 区域生成摘要
tum-summarize seq="fr1_desk" *runs: _coda-guard
    docker compose run --rm -T -e RESULT_ROOT=output/tum_replay{{ if seq == "fr1_desk" { "" } else { "_" + seq } }} -e CLUSTER_PLACES_CONFIG=config/cluster_places_config_tum.yaml -e PLACES_LAYER=TRAVERSABILITY daaam-ws bash scripts/postprocess_coda.sh --summaries {{ runs }}

# 预先构建(或检查)一个序列的 1 cm 参考网格:Open3D TSDF 对整段序列的重建,缓存在数据集的 .daaam/reference_mesh/,之后的
# just tum-run 直接用它;mode 写 rebuild 强制重建。just tum-run 默认也会自动构建,环境变量 REFERENCE_MESH=off|rebuild 和
# REFERENCE_MESH_CACHE=目录(只读数据集用)原样传进去;需要 Open3D(pip install open3d==0.19.0,约 450 MB),见 README.md
reference-mesh seq="fr1_desk" mode="auto":
    docker compose run --rm -T -e REFERENCE_MESH_CACHE daaam-ws python scripts/build_reference_mesh.py /datasets/tum_{{ seq }}{{ if mode == "rebuild" { " --force" } else if mode == "auto" { "" } else { error("mode 只能是 auto 或 rebuild") } }}

# 回看工作区:在 WSL 里启动只监听本机(127.0.0.1)的服务,用浏览器打开打印的地址;页面列出 output/ 里的结果,也能输入任意
# WSL 可见的结果目录(/home/…、/mnt/e/…、E:\…、\\wsl.localhost\…),多个结果用标签页切换;场景图文件一出现就自动换上,页面里的
# Postprocess / Summarize / 全部运行 在 daaam-ws 容器里跑(需要 Docker);Ctrl+C 停止。见 README.md
replay-web host="127.0.0.1" port="8765":
    @echo "回看工作区: http://{{ host }}:{{ port }}/replay.html"
    python3 -m scripts.replay_web.server --host {{ host }} --port {{ port }}

# 在浏览器版 Rerun 中播放 .rrd；不传路径时自动选择 output 下最新的 replay.rrd
rerun-play rrd="":
    bash scripts/rerun_play.sh {{quote(rrd)}}

# 用浏览器查看保存的 DSG；不传路径时选择 output 下最新的后处理或原始 DSG，可选显示独立物体 Mesh
rerun-web dsg="" object_meshes="0":
    bash scripts/rerun_web.sh {{quote(dsg)}} {{quote(object_meshes)}}


# 查看单独物体 Mesh；默认选择最近更新的 DSG
mesh dsg="":
    @dsg="{{ dsg }}"; if [ -z "$dsg" ]; then dsg=$(find output -type f \( -name dsg_updated.json -o -name dsg.json \) -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -n 1 | cut -d' ' -f2-); fi; if [ -z "$dsg" ] || [ ! -f "$dsg" ]; then echo "找不到 DSG 文件，请先运行 just tum-run 或指定 DSG 路径" >&2; exit 1; fi; echo "物体 Mesh 数据: $dsg"; {{ just_executable() }} rerun-web "$dsg" 1


# ── Habitat 实时建图:在模拟器里用键盘走动,DAAAM 同时建图,RViz 里看场景图长出来(按键、实测和限制见 README.md)──
# 分步启动(两个终端,按顺序):1. `just habitat-bridge`(Habitat 窗口,先点一下它再按键)→ 2. `just habitat-mapper`(建图 + RViz);一条命令全开:`just habitat-live`
# 用 ~/jellymap_dev/sm-lab/sim/habitat 里已有的 habitat-sim 环境和 ReplicaCAD 场景(只读);位置不同就设 HABITAT_PYTHON / HABITAT_DATA

# 第 1 步:Habitat 窗口和帧服务(W/S/A/D 走,方向键或 Q/E 转,Shift 加速,R 回起点,P 自动走,Esc 退出);后面可加 bridge.py 的参数
habitat-bridge *args:
    bash scripts/habitat_live.sh --bridge-only {{ args }}

# 第 2 步:建图 + RViz(桥要先开着,没开会等 2 分钟);关掉 Habitat 窗口或 Ctrl+C 后保存地图;frames 写数字就在那么多帧后停止;DAM=1 另外描述物体
habitat-mapper frames="all": _coda-guard
    docker compose run --rm -e DAM daaam-ws bash scripts/run_habitat_live.sh {{ frames }}

# 第 1、2 步一起开(桥 + 建图 + RViz):关掉 Habitat 窗口(Esc)或 Ctrl+C 后保存地图
habitat-live frames="all": _coda-guard
    bash scripts/habitat_live.sh {{ frames }}


# ── CODa 数据集(README.md 的流程):数据放在 $DAAAM_DATA_DIR/CODa(容器里是 /data/CODa),运行输出直接写在仓库的 output/ 里(output/coda、output/coda_run*);步骤和本机实测的结果见 README.md ──
# 一次性准备:`just coda-download` → `just coda-depth`(要几个小时,可断点续传)
# 日常:`just coda-bag` → `just coda-run`;整段序列(8213 帧)内存装不下,用 `just coda-chunks` 分段跑
# 结果:`just coda-list` 看有哪些 → `just coda-post` → `just coda-summarize`(OpenAI)→ `just coda-view <结果>` / `just coda-ask "问题" <结果>`

# 下载 CODa 序列 0 的 cam0 / cam1 图像和元数据:frames 是前多少帧(0 = 全部 8213 帧约 31.6 GB;1000 帧约 3.3 GB),可断点续传
coda-download frames="1000":
    docker compose run --rm -T daaam-ws python scripts/download_coda_subset.py -d /data/CODa -se 0 --frames {{ frames }} --proxy {{ proxy }}

# FoundationStereo 为每一帧估计深度(约 4.3 秒/帧,全序列约 9.5 小时,可断点续传;约 8 GB 显存,期间不要跑别的管线)
coda-depth: _coda-guard
    docker compose run --rm -T -e https_proxy={{ proxy }} daaam-ws bash -c 'python scripts/run_foundation_stereo_coda.py --dataset_folder "$CODA_ROOT_DIR" --sequence_id 0'

# 按数据集原始的 10 Hz 录 bag:start 起始帧,frames 帧数(0 = 到序列结尾),name 是 bag 名(加载器会再加时间戳);播放速度到跑管线时再选
coda-bag start="0" frames="0" name="coda_0_with_depth":
    docker compose run --rm -T daaam-ws bash scripts/make_coda_bag.sh {{ start }} {{ frames }} {{ name }}

# 在 bag 上跑 DAAAM + Hydra(Perception Encoder 选帧):bag 不写 = 最新的 coda_0_with_depth*.bag(要写就写 bag 的目录名或容器里的路径);rate 是播放速度(bag 是 10 Hz,0.3 = 3 帧/秒);labels 是伪标签数
coda-run bag="" rate="0.3" labels="5000": _coda-guard
    docker compose run --rm -T -e RATE={{ rate }} -e LABELS={{ labels }} daaam-ws bash scripts/run_coda_example.sh {{ bag }}

# 整段序列分 n 段跑(每段先录 bag 再跑管线,每段一个场景图;一次跑 8213 帧在 15.9 GB 内存里装不下);which 只跑指定的段,例如 `just coda-chunks 4 0.3 3 4`
coda-chunks n="4" rate="0.3" *which: _coda-guard
    docker compose run --rm -T -e RATE={{ rate }} daaam-ws bash scripts/run_coda_chunks.sh {{ n }} {{ which }}

# 后处理:句子嵌入 → 地点聚类成房间 / 区域;runs 是 output/coda 下的目录名(`just coda-list`),不写 = 最新的结果
coda-post *runs: _coda-guard
    docker compose run --rm -T daaam-ws bash scripts/postprocess_coda.sh {{ runs }}

# 同上,再用 OpenAI(gpt-5-nano,要 .env 里的 OPENAI_API_KEY)给每个区域写摘要
coda-summarize *runs: _coda-guard
    docker compose run --rm -T daaam-ws bash scripts/postprocess_coda.sh --summaries {{ runs }}

# 列出结果目录(最新的在前)、帧数,以及后处理做到哪一步
coda-list:
    docker compose run --rm -T --no-deps daaam-ws bash scripts/postprocess_coda.sh --list

# 对一个做完 `coda-summarize` 的结果提问(检索用场景图里记的 CLIP 编码器,回答用 OpenAI gpt-5-mini,要 OPENAI_API_KEY);run 是 output/coda 下的目录名
coda-ask question run: _coda-guard
    @printf '%s\n\n' {{ quote(question) }} | docker compose run --rm -T -e HF_HOME=/data/hf_cache daaam-ws python scripts/demo_query.py --dsg-path output/coda/{{ run }}/clustered_dsg_with_summaries.json --seq-id 0 --model-name gpt-5-mini

# 在浏览器版 Rerun 里看一个做完 `coda-summarize` 的结果(保持运行,Ctrl+C 停止);run 是 output/coda 下的目录名
coda-view run:
    @{{ just_executable() }} rerun-web {{ output_dir }}/coda/{{ run }}/clustered_dsg_with_summaries.json


# ── 3DGS 测试世界(gsplat,scripts/gs_world):把带位姿的 RGB-D 训练成 3D 高斯模型,再沿原轨迹或新轨迹渲染出 RGB-D 数据集给 DAAAM 建图;模型、渲染和 bag 转换结果在 $DAAAM_DATA_DIR/gs_world(容器里 /data/gs_world,本机 E:\daaam-data\gs_world),见 README.md ──
# 一次性：`just gs-install` 准备隔离的 GS 依赖。
# 日常:`just gs-train fr1_room` → `just gs-render latest` → `just gs-daaam <渲染目录>`;D455 实录:`just gs-bag <bag> --list` → `just gs-bag <bag> --extrinsic …` → `just gs-train <转换目录>`

# 安装 gsplat 1.5.3 和 ninja、jaxtyping、wadler-lindig 到工作区卷的 /opt/daaam_ws/gs_site(--no-deps,不改 DAAAM 的 venv;版本与 scripts/gs_world/env.py 一致)
gs-install:
    docker compose run --rm -T daaam-ws pip install --no-deps --target /opt/daaam_ws/gs_site gsplat==1.5.3 ninja==1.13.2 jaxtyping==0.3.11 wadler-lindig==0.1.7

# 训练 3DGS 模型(MCMC 增密 + 抗锯齿 + 深度监督 + 逐帧曝光补偿):data 是 /datasets 下的名字(fr1_room 或 tum_fr1_room)或容器里的数据集目录,可追加 train 的参数(如 --iterations 7000、--pose-opt);fr1_room 3 万步约 25 分钟,输出 /data/gs_world/models/<数据集>/<UTC 时间>
gs-train data *args: _coda-guard
    docker compose run --rm -T daaam-ws python -m scripts.gs_world.train {{ data }} {{ args }}

# 用模型渲染 RGB-D 数据集:model 是模型目录或 latest(最新的模型);追加 --trajectory test|轨迹文件、--offset X,Y,Z、--rotate RX,RY,RZ、--exposure nearest 等;输出 <模型>/renders/<轨迹名>,渲染原轨迹时顺带和原始帧对比
gs-render model="latest" *args: _coda-guard
    docker compose run --rm -T daaam-ws python -m scripts.gs_world.render {{ model }} {{ args }}

# 把 D455 的 ROS1 bag(彩色 + 对齐深度 + camera_info + 里程计话题或 --trajectory 轨迹)转成带位姿的 RGB-D 数据集:bag 是 WSL 里的路径(只读挂进容器),先加 --list 看话题,外参如 --extrinsic /data/calib/chain.yaml:T_cam_livox;输出 /data/gs_world/datasets/<bag 名>
gs-bag bag *args:
    @bag="$(realpath {{ quote(bag) }})"; docker compose run --rm -T -v "$(dirname "$bag")":/bag_in:ro daaam-ws python -m scripts.gs_world.rosbag_to_rgbd "/bag_in/$(basename "$bag")" {{ args }}

# 用 DAAAM 给 gs_world 数据集(渲染结果或转换后的 bag)建图,像 tum-run 一样生成 replay/ 回看数据包和 replay.rrd;frames 写 all 为全部帧;输出 output/gs_<数据集>_<轨迹>(name 可改)
gs-daaam dataset frames="all" name="": _coda-guard
    docker compose run --rm -T daaam-ws bash {{ gs_world_dir }}/run_daaam.sh {{ dataset }} {{ frames }} {{ name }}

# 比较同一世界坐标系里的几个 DAAAM 结果(第一个是参照,如真实帧的 output/tum_replay_fr1_room):物体 / 地点 / 房间数、0.3 m 内一一配上的物体比例和距离、配上的物体 DAM 描述的一致程度;runs 写 output/<名字> 或其中的 out_* 目录,可追加 --radius、--json
gs-compare +runs:
    docker compose run --rm -T --no-deps daaam-ws python -m scripts.gs_world.compare_maps {{ runs }}

# 在浏览器里像看三维模型一样看 3DGS 模型(http://127.0.0.1:8770/):model 是 latest、all(所有场景并排)、<数据集>/<时间> 或模型目录,可接着写多个并排显示,页面右侧「场景」面板里也能换;Ctrl+C 停止,空闲 30 分钟自己退出;怎么操作见 README.md
gs-view model="latest" *args: _coda-guard
    @echo "3DGS 查看器: http://127.0.0.1:8770/ (Ctrl+C 停止)"
    docker compose run --rm -T daaam-ws python -m scripts.gs_world.view {{ model }} {{ args }}

# 内部:已有别的管线 / 深度估计 / 后处理 / 问答 / 3DGS 训练、渲染或查看器的容器在跑就不启动(显存和 WSL 内存装不下两个)
_coda-guard:
    @if docker ps --no-trunc --format '{{{{.Command}}' | grep -E 'run_coda_example|run_coda_chunks|postprocess_coda|run_foundation_stereo|run_replay|run_pipeline|run_tum_example|run_live_preview|run_habitat_live|demo_query|agent_worker|daaam_node|scripts.gs_world.train|scripts.gs_world.render|scripts.gs_world.view' > /dev/null; then echo "已经有 DAAAM 的管线、深度估计、后处理、问答或 3DGS 训练 / 渲染 / 查看器的容器在跑(docker ps 看):同时跑会 OOM,等它结束,或 docker stop 它(回看页面的问答 worker 叫 daaam-replay-agent,空闲 15 分钟会自己退出)" >&2; exit 1; fi
