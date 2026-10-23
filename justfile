export PATH := parent_directory(just_executable()) + ":" + env_var("PATH")
# DAAAM 算法运行与回看。数据准备、Habitat 实时、查看器、3DGS 在 JellyMap 的 sm-lab/systems/daaam/ops.just。
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

# 在 TUM 数据集上运行建图(不开 RViz、带 DAM,298 帧约 4 分钟),跑完生成 replay/ 回看数据包(用 just replay 打开)和可在 Rerun 播放的 replay.rrd；seq 选序列,输出在 output/tum_replay_<seq>
tum-run frames="298" seq="fr1_desk":
    bash scripts/run_tum_replay.sh {{quote(frames)}} {{quote(seq)}}

# 后处理指定 TUM 环境最近一次 tum-run 结果:对象句子嵌入 → 地点聚类成区域;runs 可写一个或多个 out_<时间> 目录名
tum-post seq="fr1_desk" *runs: _coda-guard
    docker compose run --rm -T -e RESULT_ROOT=output/tum_replay{{ if seq == "fr1_desk" { "" } else { "_" + seq } }} -e CLUSTER_PLACES_CONFIG=config/cluster_places_config_tum.yaml -e PLACES_LAYER=TRAVERSABILITY daaam-ws bash scripts/postprocess_coda.sh {{ runs }}

# 同上,再用 OpenAI(gpt-5-nano,要 .env 里的 OPENAI_API_KEY)为 TUM 区域生成摘要
tum-summarize seq="fr1_desk" *runs: _coda-guard
    docker compose run --rm -T -e RESULT_ROOT=output/tum_replay{{ if seq == "fr1_desk" { "" } else { "_" + seq } }} -e CLUSTER_PLACES_CONFIG=config/cluster_places_config_tum.yaml -e PLACES_LAYER=TRAVERSABILITY daaam-ws bash scripts/postprocess_coda.sh --summaries {{ runs }}

# ── CODa 数据集(README.md 的流程):数据放在 $DAAAM_DATA_DIR/CODa(容器里是 /data/CODa),运行输出直接写在仓库的 output/ 里(output/coda、output/coda_run*);步骤和本机实测的结果见 README.md ──
# 一次性准备:`just coda-download` → `just coda-depth`(要几个小时,可断点续传)
# 日常:`just coda-bag` → `just coda-run`;整段序列(8213 帧)内存装不下,用 `just coda-chunks` 分段跑
# 结果:`just coda-list` 看有哪些 → `just coda-post` → `just coda-summarize`(OpenAI)→ `just coda-view <结果>` / `just coda-ask "问题" <结果>`

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

# 用 DAAAM 给 gs_world 数据集(渲染结果或转换后的 bag)建图,像 tum-run 一样生成 replay/ 回看数据包和 replay.rrd;frames 写 all 为全部帧;输出 output/gs_<数据集>_<轨迹>(name 可改)
gs-daaam dataset frames="all" name="": _coda-guard
    docker compose run --rm -T daaam-ws bash {{ gs_world_dir }}/run_daaam.sh {{ dataset }} {{ frames }} {{ name }}

# 内部:已有别的管线 / 深度估计 / 后处理 / 问答 / 3DGS 训练、渲染或查看器的容器在跑就不启动(显存和 WSL 内存装不下两个)
_coda-guard:
    @if docker ps --no-trunc --format '{{{{.Command}}' | grep -E 'run_coda_example|run_coda_chunks|postprocess_coda|run_foundation_stereo|run_replay|run_pipeline|run_tum_example|run_live_preview|run_habitat_live|demo_query|agent_worker|daaam_node|scripts.gs_world.train|scripts.gs_world.render|scripts.gs_world.view' > /dev/null; then echo "已经有 DAAAM 的管线、深度估计、后处理、问答或 3DGS 训练 / 渲染 / 查看器的容器在跑(docker ps 看):同时跑会 OOM,等它结束,或 docker stop 它(回看页面的问答 worker 叫 daaam-replay-agent,空闲 15 分钟会自己退出)" >&2; exit 1; fi

# 安装到持久工作区 venv；--rm 不会删除该环境，无需重建镜像。
profile-install:
    docker compose run --rm -T --no-deps daaam-ws python -m pip install -r docker/requirements-profile.txt

# 记录统一 RGB-D 建图 Python 调用；参数与 run 相同。
[positional-arguments]
profile dataset frames="30" *args:
    python3 scripts/run_rgbd.py --caller-dir {{quote(env_var_or_default("WARMOON_CALLER_DIR", invocation_directory()))}} --viztracer "$@"

# 容器内启动报告查看器，浏览器访问 http://localhost:9001；路径相对于调用目录。
profile-view report port="9001":
    @WARMOON_CALLER_DIR={{quote(env_var_or_default("WARMOON_CALLER_DIR", invocation_directory()))}} bash scripts/profile_view.sh {{quote(report)}} {{quote(port)}}

# 主线程采样调用树：HTML/TXT/JSON，参数与 run 相同；不与 VizTracer 同时开启。
[positional-arguments]
profile-sample dataset frames="30" *args:
    python3 scripts/run_rgbd.py --caller-dir {{quote(env_var_or_default("WARMOON_CALLER_DIR", invocation_directory()))}} --pyinstrument "$@"
