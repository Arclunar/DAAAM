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
#   HYDRA_CONFIG：Hydra 配置文件，默认 config/hydra_config/tum_rgbd_khronos.yaml。
#   EXTRA_OVERRIDES：以空格分隔的 key=value 流水线配置覆盖项，例如
#     "workers.num_grounding_workers=0" 可在不加载 DAM 的情况下测试 Hydra 部分。
#   ZMQ_URL：向实时查看器发布场景图的地址，默认 "none"；
#     scripts/run_live_preview.sh 会将其设为 tcp://127.0.0.1:8001。
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
HYDRA_CONFIG="${HYDRA_CONFIG:-config/hydra_config/tum_rgbd_khronos.yaml}"
EXTRA_OVERRIDES="${EXTRA_OVERRIDES:-}"
ZMQ_URL="${ZMQ_URL:-none}"

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

rc=0
python scripts/run_pipeline.py "${DATASET}" \
  --config config/pipeline_config.yaml \
  --dataset-type ImageSequenceDataset \
  --hydra-config-path "${HYDRA_CONFIG}" \
  --sam-model fastsam/FastSAM-x.pt \
  --semantic-config "${LABELS_BASE}.yaml" \
  --labelspace-colors "${LABELS_BASE}.csv" \
  --depth-scale 5000 --fps "${FPS}" --depth-lb 0.3 --depth-ub 4.0 \
  --zmq-url "${ZMQ_URL}" --dataset-name "${NAME}" \
  "${max_frames_args[@]}" --target-fps "${TARGET_FPS}" \
  --save-images --save-interval 30 --verbose \
  --config-overrides tracking.reid_weights=checkpoints/reid_weights/clip_general.pt ${EXTRA_OVERRIDES} || rc=$?

# the totals printed by run_pipeline.py do not match the saved files, so summarize those; the summary also warns
# when the grounding worker (DAM) dropped out, which the pipeline itself does not treat as an error
latest="$(ls -dt "output/${NAME}"/out_* 2>/dev/null | head -n 1 || true)"
if [ -n "${latest}" ]; then
  if [ "${rc}" -eq 0 ] && [ -f "${latest}/dsg.json" ]; then
    # Revisit final native place geometry using actual saved fragment/depth
    # observations, after asynchronous DAM descriptions have been persisted.
    python scripts/lift_native_places.py --run "${latest}" --dataset "${DATASET}" || rc=$?
  fi
  python scripts/summarize_run.py "${latest}" --max-objects 10 || true
fi
exit "${rc}"
