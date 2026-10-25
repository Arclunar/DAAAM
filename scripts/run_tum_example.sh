#!/usr/bin/env bash
# 在经 scripts/prepare_tum_rgbd.py 转换的 TUM RGB-D 序列上运行离线 DAAAM 流水线。
# 以下参数已在 RTX 5060 Ti（16 GB 显存）和 15 GB 内存的环境中验证。
#
# 通过 daaam-ws 容器运行：
#   docker compose run --rm -T daaam-ws bash scripts/run_tum_example.sh [MAX_FRAMES] [TARGET_FPS]
#   docker compose run --rm -T daaam-ws bash scripts/run_tum_example.sh 298 3
#
# 可通过环境变量覆盖配置：
#   SEQ：测试序列，默认 fr1_desk，即 /datasets/tum_<SEQ>。其他序列用 scripts/fetch_tum.py 下载并转换（just tum-list
#     列出名称，just tum-fetch <名称> 下载）；MAX_FRAMES 写 all 表示该序列的全部帧。
#   DATASET：数据集路径，默认 /datasets/tum_<SEQ>；NAME：输出子目录名称，默认 tum_<SEQ>。
#   LABELS：伪标签空间大小，默认 5000；FPS：数据集帧率，默认 15。
#   DEPTH_SCALE：每米深度单位数，默认 5000；通用 RGB-D 入口从 camera_info.json 读取。
#   HYDRA_CONFIG：Hydra 配置文件，默认 config/hydra_config/tum_rgbd_khronos.yaml。
#   EXTRA_OVERRIDES：以空格分隔的 key=value 流水线配置覆盖项，例如
#     "workers.num_grounding_workers=0" 可在不加载 DAM 的情况下测试 Hydra 部分。
#   ZMQ_URL：向实时查看器发布场景图的地址，默认 "none"；
#     scripts/run_live_preview.sh 会将其设为 tcp://127.0.0.1:8001。
#   DEPTH_LB / DEPTH_UB：深度范围（米），默认 0.3 / 4.0；用同样的范围重跑 Habitat 实时会话的录制（README.md）时要和实时运行的一致。
#
# 注意事项：
#   * TARGET_FPS 控制输入帧的处理速率。DAM 异步返回回答，而流水线结束时不会等待尚未返回的回答。
#     因此，全速输入可能导致流水线在首批回答返回前就结束（首次查询在第 60 帧触发）。
#     在上述 GPU 上，3 fps 是较合适的折中值。
#     该参数仅设定帧率上限：每帧依次执行 CV 和 Hydra，只有处理耗时小于 1/TARGET_FPS 时才等待。
#     此环境下，不启用 DAM 时每帧约需 0.3 秒（3.4 fps）；启用实时查看器时约需 0.47 秒（2.1 fps）。
#     将目标帧率设得更高无法突破处理速度上限（启用查看器时实测：目标 6 fps 得到 2.4 fps，
#     目标 15 fps 得到 2.1 fps）。
#   * LABELS：Hydra 创建伪标签空间时，内存占用约为 1.1 GB 加上每个标签 0.28 MB。
#     Hydra 默认的 30000 个标签约需 9.5 GB 内存；本脚本默认的 5000 个标签约需 2.4 GB，
#     足以处理包含数百条跟踪轨迹的序列（标签耗尽时，流水线会报错并停止）。
set -eo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

MAX_FRAMES="${1:-298}"
TARGET_FPS="${2:-3}"
SEQ="${SEQ:-fr1_desk}"
DATASET="${DATASET:-/datasets/tum_${SEQ}}"
NAME="${NAME:-tum_${SEQ}}"
LABELS="${LABELS:-5000}"
FPS="${FPS:-15}"
DEPTH_SCALE="${DEPTH_SCALE:-5000}"
HYDRA_CONFIG="${HYDRA_CONFIG:-config/hydra_config/tum_rgbd_khronos.yaml}"
EXTRA_OVERRIDES="${EXTRA_OVERRIDES:-}"
FRAME_CACHE_OVERRIDES="grounding.release_assignment_frames=true scene_graph.corrections_save_interval_seconds=5"
export KHRONOS_COMPACT_STATIC_FRAMES="${KHRONOS_COMPACT_STATIC_FRAMES:-1}"
export DAAAM_TRIM_INTERVAL_FRAMES="${DAAAM_TRIM_INTERVAL_FRAMES:-100}"
echo "[memory] compact_static=${KHRONOS_COMPACT_STATIC_FRAMES}, trim_interval=${DAAAM_TRIM_INTERVAL_FRAMES} frames"
ZMQ_URL="${ZMQ_URL:-none}"
DEPTH_LB="${DEPTH_LB:-0.3}"
DEPTH_UB="${DEPTH_UB:-4.0}"

if [ ! -d "${DATASET}/rgb" ]; then
  echo "Dataset ${DATASET} not found. Download and convert the sequence first: just tum-fetch ${SEQ}" >&2
  exit 1
fi
max_frames_args=(--max-frames "${MAX_FRAMES}")
if [ "${MAX_FRAMES}" = all ]; then
  max_frames_args=()  # no limit: the whole sequence
fi

LABELS_BASE="config/labels_pseudo.n${LABELS}"
if [ ! -f "${LABELS_BASE}.yaml" ]; then
  # a read-only config directory (container started with another uid than the image's) falls back to /tmp
  [ -w config ] || LABELS_BASE="/tmp/labels_pseudo.n${LABELS}"
  if [ ! -f "${LABELS_BASE}.yaml" ]; then
    python -c "from daaam.utils.vision import create_label_files; create_label_files('${LABELS_BASE}', ${LABELS})"
  fi
fi

if [ "${VIZTRACER:-0}" = 1 ] && [ "${PYINSTRUMENT:-0}" = 1 ]; then
  echo "Select one profiler per run: VIZTRACER or PYINSTRUMENT" >&2
  exit 2
fi
if [ "${PYINSTRUMENT:-0}" = 1 ]; then
  python -c 'import pyinstrument' || { echo "Run just daaam profile-install first" >&2; exit 1; }
  mkdir -p "output/${NAME}/profile"
  sample="output/${NAME}/profile/pyinstrument_$(date -u +%Y%m%dT%H%M%S)_$$.html"
  export DAAAM_PYINSTRUMENT_OUTPUT="$PWD/${sample}"
  echo "Pyinstrument report: ${sample}"
fi

# Opt-in profiling is applied to the actual mapping process, not the replay recorder.
if [ "${VIZTRACER:-0}" = 1 ]; then
  python -c 'import viztracer' || { echo "Run just daaam profile-install first" >&2; exit 1; }
  mkdir -p "output/${NAME}/profile"
  trace="output/${NAME}/profile/trace_$(date -u +%Y%m%dT%H%M%S)_$$.json"
  export DAAAM_VIZTRACER_OUTPUT="$PWD/${trace}"
  echo "VizTracer report: ${trace}"
fi
rc=0
python scripts/run_pipeline.py "${DATASET}" \
  --config config/pipeline_config.yaml \
  --dataset-type ImageSequenceDataset \
  --hydra-config-path "${HYDRA_CONFIG}" \
  --sam-model fastsam/FastSAM-x.pt \
  --semantic-config "${LABELS_BASE}.yaml" \
  --labelspace-colors "${LABELS_BASE}.csv" \
  --depth-scale "${DEPTH_SCALE}" --fps "${FPS}" --depth-lb "${DEPTH_LB}" --depth-ub "${DEPTH_UB}" \
  --zmq-url "${ZMQ_URL}" --dataset-name "${NAME}" \
  "${max_frames_args[@]}" --target-fps "${TARGET_FPS}" \
  --save-images --save-interval 30 --verbose \
  --config-overrides tracking.reid_weights=checkpoints/reid_weights/clip_general.pt ${FRAME_CACHE_OVERRIDES} ${EXTRA_OVERRIDES} || rc=$?

if [ "${VIZTRACER:-0}" = 1 ] && [ ! -s "${trace}" ]; then
  echo "VizTracer report was not saved: ${trace}" >&2
  [ "${rc}" -ne 0 ] || rc=1
fi

if [ "${PYINSTRUMENT:-0}" = 1 ] && [ ! -s "${sample}" ]; then
  echo "Pyinstrument report was not saved: ${sample}" >&2
  [ "${rc}" -ne 0 ] || rc=1
fi

# the totals printed by run_pipeline.py do not match the saved files, so summarize those; the summary also warns
# when the grounding worker (DAM) dropped out, which the pipeline itself does not treat as an error
latest="$(ls -dt "output/${NAME}"/out_* 2>/dev/null | head -n 1 || true)"
if [ -n "${latest}" ]; then
  if [ "${rc}" -eq 0 ] && [ -f "${latest}/dsg.json" ]; then
    # Revisit final native place geometry using actual saved fragment/depth
    # observations, after asynchronous DAM descriptions have been persisted.
    python scripts/lift_native_places.py --run "${latest}" --dataset "${DATASET}" || rc=$?
  fi
  python "${JELLYMAP_ROOT:?JELLYMAP_ROOT is not set}/sm-lab/viewers/replay/tools/summarize_run.py" "${latest}" --max-objects 10 || true
fi
exit "${rc}"
