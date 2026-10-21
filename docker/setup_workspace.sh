#!/usr/bin/env bash
# DAAAM 工作区一键安装。在 daaam-ws 容器内执行；幂等，可反复运行以断点续跑。
#
#   docker compose run --rm -T daaam-ws bash docker/setup_workspace.sh              # 全部步骤
#   docker compose run --rm -T daaam-ws bash docker/setup_workspace.sh colcon pip  # 只跑指定步骤
#
# 步骤（默认按顺序全部执行）：
#   repos   导入同级仓库（install/packages.yaml，HTTPS 浅克隆），把精确 commit 导出到工作区的 workspace.lock.yaml
#   colcon  编译 C++ 工作区（先单独编 gtsam，再编其余），安装到 $DAAAM_WS/install
#   pip     Python 依赖（requirements.txt）+ spark_dsg + hydra_python + daaam，等价于依次执行下面两步
#     pip-deps  只装 requirements.txt（不依赖 colcon，可以和 colcon 并行跑）
#     pip-ws    装 spark_dsg、hydra_python、daaam（需要 colcon 已经跑完）
#   check   导入自检
#
# 与上游 install/install.sh 的差异（原因见 progress.md / SETUP_LOG.md）：
#   1. 额外安装 hydra_python。Hydra 的 colcon 构建类型是纯 cmake，不含 Python 绑定，上游脚本漏了这一步。
#   2. 先单独编译 gtsam（占编译量的大头，可以放心用更高并行度）；其余包限制并行度，避免 15 GB 内存 OOM。
#   3. CMAKE_BUILD_PARALLEL 之外，编译用 Release，并关闭 gtsam examples 与各包 tests，缩短编译时间（不影响安装产物）。
#   4. TensorRT 固定到 README.md 里验证过的版本。
#
# 可调环境变量：GTSAM_JOBS(默认 6)、OTHER_JOBS(默认 2)、COLCON_WORKERS(默认 2)、TENSORRT_PIN(默认 10.13.3.9)

# 不使用 set -u：ROS 的 setup 脚本会引用未定义变量。
set -Eeo pipefail
export PYTHONUNBUFFERED=1

WS="${DAAAM_WS:-/opt/daaam_ws}"
REPO="${WS}/src/daaam"
LOG_DIR="${REPO}/output/logs/setup"
GTSAM_JOBS="${GTSAM_JOBS:-6}"
OTHER_JOBS="${OTHER_JOBS:-2}"
COLCON_WORKERS="${COLCON_WORKERS:-2}"
TENSORRT_PIN="${TENSORRT_PIN:-10.13.3.9}"
ROS_SETUP="/opt/ros/${ROS_DISTRO:-jazzy}/setup.bash"

mkdir -p "${LOG_DIR}"

log() { echo "[$(date '+%F %T')] $*"; }
disk() { df -h "${WS}" | awk 'NR==2{print "磁盘 " $3 " 已用 / " $4 " 可用 (" $5 ")"}'; }

# 后台记录内存占用，方便事后判断是否发生过 OOM
( while sleep 60; do echo "$(date +%T) $(free -m | awk 'NR==2{print "used=" $3 "MB avail=" $7 "MB"}')"; done ) >> "${LOG_DIR}/setup-mem.log" &
MEM_PID=$!
trap 'kill ${MEM_PID} 2>/dev/null || true' EXIT

# ----------------------------------------------------------------------------
step_repos() {
  log "== repos: 导入同级仓库 (HTTPS, 浅克隆)"
  cd "${WS}/src"
  vcs import . --input "${REPO}/install/packages.yaml" --workers 4 --retry 3 --shallow --skip-existing 2>&1 \
    | grep -v -e DeprecationWarning -e pkg_resources || true
  vcs export --exact . 2>/dev/null > "${WS}/workspace.lock.yaml"
  log "repos: 完成，$(grep -c 'version:' "${WS}/workspace.lock.yaml") 个仓库的 commit 已记录到 ${WS}/workspace.lock.yaml"
}

# ----------------------------------------------------------------------------
tune_colcon_defaults() {
  local f="${COLCON_DEFAULTS_FILE:-${WS}/colcon_defaults.yaml}"
  if grep -q 'daaam-setup: tuned' "${f}" 2>/dev/null; then
    return
  fi
  [ -f "${f}.orig" ] || cp "${f}" "${f}.orig"
  cat > "${f}" <<'YAML'
---
# daaam-setup: tuned
# 相对上游默认值（保存在 colcon_defaults.yaml.orig）的改动：
#   Release 构建；限制同时编译的包数；关闭 gtsam examples/tests 与各包 tests（只省编译时间，不改安装产物）。
build:
  symlink-install: true
  parallel-workers: 2
  packages-skip:
    - khronos_msgs
    - khronos_ros
    - khronos_eval
    - hydra_multi_ros
    - spark_fast_lio
    - ouroboros_ros
    - ouroboros_msgs
  cmake-args:
    - --no-warn-unused-cli
    - -DCMAKE_BUILD_TYPE=Release
    - -DCONFIG_UTILS_ENABLE_ROS=OFF
    - -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
    - -DGTSAM_USE_SYSTEM_EIGEN=ON
    - -DGTSAM_BUILD_TESTS=OFF
    - -DGTSAM_BUILD_EXAMPLES_ALWAYS=OFF
    - -DGTSAM_BUILD_TIMING_ALWAYS=OFF
    - -DBUILD_TESTING=OFF
YAML
  log "colcon: 已写入调整后的 ${f}（原文件备份为 ${f}.orig）"
}

report_failures() {
  python3 - <<'PY'
import os, pathlib, re
ws = pathlib.Path(os.environ.get("DAAAM_WS", "/opt/daaam_ws"))
events = ws / "log" / "latest_build" / "events.log"
failed = []
if events.exists():
    for line in events.read_text(errors="ignore").splitlines():
        m = re.search(r"\((?P<pkg>[^)]+)\) JobEnded: .*'rc': (?P<rc>[^,}]+)", line)
        if m and m.group("rc").strip() != "0":
            failed.append((m.group("pkg"), m.group("rc").strip()))
if not failed:
    print("colcon: 没有失败的包")
for pkg, rc in failed:
    print(f"\n===== 失败的包: {pkg} (rc={rc}) —— 日志末尾 60 行 =====")
    logf = ws / "log" / "latest_build" / pkg / "stdout_stderr.log"
    if logf.exists():
        print("\n".join(logf.read_text(errors="ignore").splitlines()[-60:]))
PY
}

step_colcon() {
  log "== colcon: 编译 C++ 工作区"
  source "${ROS_SETUP}"
  tune_colcon_defaults
  pip install "empy==3.3.4" catkin_pkg lark pyyaml 2>&1 | tail -2

  # rosdep 的缓存在容器的 ~/.ros 里（不在卷里），每个新容器都要 update 一次
  local ok=0
  for i in 1 2 3; do
    if rosdep update --rosdistro="${ROS_DISTRO:-jazzy}" >/dev/null 2>&1; then ok=1; break; fi
    log "rosdep update 失败，第 ${i} 次重试"; sleep 5
  done
  [ "${ok}" = 1 ] || { log "ERROR: rosdep update 连续失败（检查代理/镜像源）"; return 1; }

  # 系统依赖应已在镜像里（docker/apt-deps.txt）。若工作区新增了仓库导致有缺失，补装并提醒更新清单。
  local sim
  sim="$(rosdep install --from-paths "${WS}/src" --ignore-src -r -y --rosdistro="${ROS_DISTRO:-jazzy}" --simulate 2>&1 || true)"
  # 依赖都已满足时 --simulate 不输出任何内容；有缺失时才会列出要执行的安装命令
  if [ -z "$(tr -d '[:space:]' <<<"${sim}")" ] || grep -q 'All required rosdeps installed successfully' <<<"${sim}"; then
    log "colcon: 系统依赖已齐全"
  else
    log "WARNING: 镜像里缺少部分系统包（模拟输出如下），现在补装（容器退出后会丢失）；请更新 docker/apt-deps.txt 并重建 daaam-ws"
    echo "${sim}" | tail -40
    rosdep install --from-paths "${WS}/src" --ignore-src -r -y --rosdistro="${ROS_DISTRO:-jazzy}"
  fi

  cd "${WS}"
  disk
  log "colcon: [1/2] 单独编译 gtsam (每包 -j${GTSAM_JOBS})"
  CMAKE_BUILD_PARALLEL_LEVEL="${GTSAM_JOBS}" MAKEFLAGS="-j${GTSAM_JOBS}" \
    colcon build --packages-select gtsam --parallel-workers 1
  disk
  log "colcon: [2/2] 编译其余全部包 (同时 ${COLCON_WORKERS} 个包，每包 -j${OTHER_JOBS})"
  local rc=0
  CMAKE_BUILD_PARALLEL_LEVEL="${OTHER_JOBS}" MAKEFLAGS="-j${OTHER_JOBS}" \
    colcon build --continue-on-error --parallel-workers "${COLCON_WORKERS}" || rc=$?
  report_failures

  # 第 2 轮：khronos 的 CMake 里 find_package(small_gicp)，但它的 package.xml 没有声明这个依赖，
  # colcon 就不会把 small_gicp 的安装前缀放进 khronos 的构建环境，第 1 轮必然失败。
  # 在已 source 工作区（环境里带上全部前缀）的环境里补编一轮即可；已编好的包只会做一次空编译。
  if [ "${rc}" -ne 0 ]; then
    log "colcon: 第 1 轮有包失败，在已 source 工作区的环境里补编第 2 轮"
    source "${WS}/install/setup.bash"
    rc=0
    CMAKE_BUILD_PARALLEL_LEVEL="${OTHER_JOBS}" MAKEFLAGS="-j${OTHER_JOBS}" \
      colcon build --continue-on-error --parallel-workers "${COLCON_WORKERS}" || rc=$?
    report_failures
  fi
  disk
  log "colcon: 结束 rc=${rc}"
  return "${rc}"
}

# ----------------------------------------------------------------------------
# pip-deps 只依赖网络，不依赖 colcon 的编译结果，可以和 colcon 并行执行
step_pip_deps() {
  log "== pip-deps: requirements.txt（torch cu128、boxmot、dam、ultralytics 等，体量大）"
  source "${ROS_SETUP}"
  cd "${REPO}"
  disk

  # 约束：① README.md 里验证过的 TensorRT 版本（requirements.txt 里没有固定）；
  # ② pyarrow 24 起要求 NumPy 2，而 DAAAM 固定 numpy<2.0.0，rerun-sdk 会因此 import 失败。
  printf 'tensorrt-cu12==%s\npyarrow<24\n' "${TENSORRT_PIN}" > "${WS}/constraints.txt"

  # requirements.txt 里有 3 个 git+https 依赖（boxmot / dam / perception_models）。pip 用
  # `git clone --filter=blob:none` 拉取它们，经当前代理会无限卡住；改成先浅克隆到本地再安装。
  log "pip-deps: 浅克隆 git 依赖到 ${WS}/vendor"
  python3 "${REPO}/docker/vendor_git_requirements.py" requirements.txt "${WS}/vendor" > "${WS}/requirements.local.txt"

  # [1/3] 先装 requirements.txt 里固定版本的 torch 三件套（+cu128）。
  log "pip-deps: [1/3] torch / torchvision / torchaudio（+cu128）"
  grep -E '^(torch|torchvision|torchaudio)==' requirements.txt > "${WS}/torch-pins.txt"
  pip install -r "${WS}/torch-pins.txt" --extra-index-url https://download.pytorch.org/whl/cu128 -c "${WS}/constraints.txt"

  # [2/3] sam2 只有源码包，它的构建依赖里写了 torch>=2.5.1。pip 的隔离构建环境会因此去装当前 PyPI 上
  # 最新的 torch（CUDA 13 版，外加 2 GB 以上的 nvidia-*-cu13 包），既慢又和系统的 nvcc 12.8 不匹配。
  # 改为复用上一步装好的 torch 免隔离构建，并跳过 sam2 可选的 CUDA 扩展（DAAAM 默认用 FastSAM，不需要它）。
  log "pip-deps: [2/3] sam2（--no-build-isolation，SAM2_BUILD_CUDA=0）"
  pip install wheel setuptools
  SAM2_BUILD_CUDA=0 pip install --no-build-isolation sam2

  # [3/3] 其余依赖。
  # dam 声明了 pydantic<=2.10.6（只是为了它自带的 gradio 演示界面，DAAAM 不用），而 google-genai 等最新版要求
  # pydantic>=2.12.5，两者冲突会让 pip 在 openai / langgraph / google-genai 的几百个历史版本上回溯很久；
  # 它声明的 gradio（最新的 6.x 与 transformers 4.55.4 的 huggingface-hub<1.0 冲突）、fastapi、uvicorn 也只被
  # dam 仓库里的演示 / 服务脚本使用，dam 库本身不导入。
  # 所以：dam 用 --no-deps 安装，它的其余依赖照常装，只去掉 pydantic / gradio / fastapi / uvicorn 这几条。
  #
  # perception_models 同理：它的 requirements.txt 是整套训练 / 评测依赖（wandb、lm-eval、viztracer、torchcodec……），
  # 而 DAAAM 只在可选的 "pe" 后端里按需导入 core.vision_encoder.pe（默认后端是 OpenCLIP），只需要
  # timm / einops / ftfy / regex 这几个。所以也用 --no-deps 安装，只补这几个。
  python3 - "${WS}/requirements.local.txt" "${WS}/vendor/describe-anything-batch" > "${WS}/requirements.main.txt" <<'PY'
import re
import sys
import tomllib

requirements, dam_dir = sys.argv[1:3]
with open(f"{dam_dir}/pyproject.toml", "rb") as f:
    dam_deps = [d for d in tomllib.load(f)["project"]["dependencies"]
                if not d.lower().startswith(("pydantic", "gradio", "fastapi", "uvicorn"))]
replacements = {
    "dam": dam_deps,
    "perception_models": ["timm", "einops", "ftfy", "regex"],
}
for line in open(requirements):
    match = re.match(r"^\s*([A-Za-z0-9_.\-]+)\s*@", line)
    if match and match.group(1) in replacements:
        print(f"# {match.group(1)} is installed with --no-deps afterwards; installing only the dependencies DAAAM needs:")
        print("\n".join(replacements[match.group(1)]))
    else:
        print(line.rstrip())
PY
  # requirements.txt 末尾的 open3d（wheel 约 450 MB）只用于回放页面的 1 cm 参考网格（scripts/build_reference_mesh.py）；
  # 没有它回放照常，只是少这个网格（run_replay.sh 的 REFERENCE_MESH=auto 会退回只有动态 Hydra 网格的页面）。
  # scripts/run_foundation_stereo_coda.py 里给 open3d 放的空桩模块是那个脚本自己的做法，和这里是否安装无关。
  log "pip-deps: [3/3] requirements.txt 的其余依赖（dam / perception_models 除外；cvxpy / rerun-sdk 会因 numpy<2 回退到旧版；open3d 的 wheel 约 450 MB）"
  pip install -r "${WS}/requirements.main.txt" -c "${WS}/constraints.txt"
  log "pip-deps: dam、perception_models（--no-deps）"
  pip install --no-deps "${WS}/vendor/describe-anything-batch" "${WS}/vendor/perception_models"

  # DAAAM 的 src/daaam/query_manager/dam/services.py 在模块顶部 `import gradio as gr`（实际没有使用），所以 gradio
  # 必须能被导入，否则 DAM grounding worker 一启动就 ModuleNotFoundError。但 gradio 6 的依赖会把 huggingface_hub
  # 升到 2.x，而 transformers 4.55.4 要求 huggingface-hub<1.0。所以 --no-deps 安装 gradio 及其导入时需要的小依赖，
  # 保留 huggingface_hub 0.36（已验证 import gradio 可用）。
  log "pip-deps: gradio（--no-deps，保留 huggingface_hub 0.x）"
  pip install --no-deps "gradio==6.30.0" "gradio_client==2.7.2" "hf-gradio==0.4.1" fastapi starlette uvicorn groovy \
    pydub python-multipart safehttpx semantic-version tomlkit typer shellingham rich markdown-it-py mdurl annotated-doc brotli
  pip install opentelemetry-api

  # README.md 的可选项：cvxopt 提供 GLPK_MI 求解器。min_frames_max_size 分配 worker 用 CVXPY 解混合整数规划，
  # 没有 GLPK 会退回 SciPy/HiGHS（scipy>=1.15 上慢 5~7 倍）。系统包 glpk-utils / libglpk-dev 已在基础镜像里。
  log "pip-deps: cvxopt（GLPK_MI）"
  pip install cvxopt
  disk
  log "pip-deps: 完成"
}

# pip-ws 需要 colcon 已经把 hydra / spark_dsg 等装进 $DAAAM_WS/install
step_pip_ws() {
  log "== pip-ws: spark_dsg + hydra_python + daaam"
  source "${ROS_SETUP}"
  source "${WS}/install/setup.bash"
  cd "${REPO}"

  log "pip-ws: spark_dsg"
  pip install "${WS}/src/spark_dsg"

  # hydra 的依赖里有 spark_dataset_interfaces @ git+https://…，同样会卡在部分克隆上：
  # 先浅克隆安装它，再用 --no-deps 安装 hydra，并手动补上 hydra 的其余依赖（见 hydra/pyproject.toml）。
  log "pip-ws: spark_dataset_interfaces（hydra_python 的依赖，浅克隆）"
  pip install "$(python3 "${REPO}/docker/vendor_git_requirements.py" --clone https://github.com/MIT-SPARK/spark_dataset_interfaces "${WS}/vendor")"
  pip install click distinctipy imageio networkx numpy pyqtgraph pyyaml scipy

  log "pip-ws: hydra_python（上游 install.sh 漏掉的一步；需要已 source 的 colcon 工作区来 find_package(hydra)）"
  pip install --no-deps "${WS}/src/hydra"

  log "pip-ws: daaam (editable)"
  pip install -e "${REPO}"
  disk
  log "pip-ws: 完成"
}

step_pip() { step_pip_deps; step_pip_ws; }

# ----------------------------------------------------------------------------
step_check() {
  log "== check: 导入自检"
  source "${ROS_SETUP}"
  source "${WS}/install/setup.bash"
  cd "${REPO}"
  python - <<'PY'
import importlib
mods = ["torch", "torchvision", "hydra_python", "spark_dsg", "daaam", "boxmot", "dam",
        "ultralytics", "open_clip", "sentence_transformers", "cvxpy", "rerun", "tensorrt", "cv2", "open3d"]
bad = 0
for m in mods:
    try:
        mod = importlib.import_module(m)
        print(f"  OK   {m:24s} {getattr(mod, '__version__', '')}")
    except Exception as e:
        bad += 1
        print(f"  FAIL {m:24s} {type(e).__name__}: {e}")
try:
    import torch
    print("  torch.cuda.is_available():", torch.cuda.is_available())
    if torch.cuda.is_available():
        print("  GPU:", torch.cuda.get_device_name(0), "| capability:", torch.cuda.get_device_capability(0))
except Exception as e:
    print("  torch check failed:", e)
print("自检失败的模块数:", bad)
PY

  # 逐个导入 daaam 包下的全部模块：把"某个子模块在导入时才需要的依赖"（如 gradio）在安装阶段就暴露出来，
  # 而不是等真实运行到启动 worker 时才报错。daaam.assignment.interfaces 单独导入会因循环导入顺序失败，
  # 通过包正常使用时不会，属于已知的无害项。
  log "check: 导入 daaam 下的所有模块"
  python - <<'PY' 2>&1 | grep -v "Cannot register"
import importlib
import pkgutil

import daaam

known_harmless = {"daaam.assignment.interfaces"}
ok, bad = 0, []
for m in pkgutil.walk_packages(daaam.__path__, "daaam."):
    try:
        importlib.import_module(m.name)
        ok += 1
    except BaseException as e:
        bad.append((m.name, type(e).__name__, (str(e).splitlines() or [""])[0][:150]))
real = [b for b in bad if b[0] not in known_harmless]
print(f"  daaam 模块导入成功 {ok} 个，失败 {len(bad)} 个（其中已知无害 {len(bad) - len(real)} 个）")
for b in real:
    print("  FAIL", *b)
PY
}

# ----------------------------------------------------------------------------
STEPS=("$@")
[ ${#STEPS[@]} -eq 0 ] && STEPS=(repos colcon pip check)

log "DAAAM 工作区安装开始，步骤: ${STEPS[*]}"
disk
for s in "${STEPS[@]}"; do
  case "${s}" in
    repos|colcon|pip|check) "step_${s}" ;;
    pip-deps) step_pip_deps ;;
    pip-ws)   step_pip_ws ;;
    *) echo "未知步骤: ${s}（可选 repos colcon pip pip-deps pip-ws check）" >&2; exit 2 ;;
  esac
done
log "全部步骤结束"
