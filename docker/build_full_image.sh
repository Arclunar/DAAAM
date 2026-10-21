#!/usr/bin/env bash
# Build a self-contained DAAAM image from the development workspace volume and export a deployment bundle
# that can be copied to another GPU server (see README.md). Run it on the machine where the workspace was set up
# (docker compose run --rm daaam-ws bash docker/setup_workspace.sh).
#
#   bash docker/build_full_image.sh image             workspace volume + this checkout -> daaam:jazzy-cu128-full
#   bash docker/build_full_image.sh verify            self check of that image without any volume and without network
#   bash docker/build_full_image.sh export OUT_DIR [PART...]
#                                                     image + model assets + sample dataset + deploy scripts -> OUT_DIR;
#                                                     PART is one of image, checkpoints, hf, sample (default: all), so
#                                                     that after a code change `export OUT_DIR image` is enough
#   bash docker/build_full_image.sh all OUT_DIR       image, verify and export
#
# The image is the base layer daaam:jazzy-cu128-deps plus a snapshot of the workspace volume (colcon build and install
# trees, .venv, the imported repositories; without caches) and of this checkout (without .git, output, datasets, logs
# and checkpoints). Model weights are not part of the image: export packs them as separate archives because they
# change rarely and do not compress.
#
# Disk: `image` briefly needs about twice the size of the workspace (~15 GB each) in Docker's storage, the container
# is removed afterwards; an existing image of the same name is kept until the new one is committed, so remove it first
# (docker rmi) when the disk is tight. `export` writes about 20 GB into OUT_DIR; on WSL2 choose a directory on a Windows
# drive that does not hold the WSL virtual disk. A server needs about 60 GB free to `docker load` the image (see README.md).
# Environment: BASE_IMAGE, FULL_IMAGE, WORKSPACE_VOLUME (default: the Compose volume daaam_daaam_workspace).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
REPO="$(pwd)"
BASE_IMAGE="${BASE_IMAGE:-daaam:jazzy-cu128-deps}"
FULL_IMAGE="${FULL_IMAGE:-daaam:jazzy-cu128-full}"
VOLUME="${WORKSPACE_VOLUME:-daaam_daaam_workspace}"
WS=/opt/daaam_ws
STAGE_CONTAINER=daaam-full-stage

log() { printf '\n[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { echo "ERROR: $*" >&2; exit 1; }
usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; }

resolve_workspace() {
  local resolved
  resolved=$(docker compose config --format json | python3 -c '
import json, sys
c=json.load(sys.stdin)
m={v["target"]:v for v in c["services"]["daaam-ws"]["volumes"]}
assert m["/opt/daaam_ws/src"]["type"] == "bind"
print(m["/opt/daaam_ws/src"]["source"])
print(c["volumes"][m["/opt/daaam_ws"]["source"]]["name"])
')
  SOURCE_TREE=$(head -n 1 <<<"${resolved}")
  VOLUME=$(tail -n 1 <<<"${resolved}")
  docker volume inspect "${VOLUME}" >/dev/null 2>&1 || die "workspace volume ${VOLUME} not found"
}

step_image() {
  resolve_workspace
  [ -f "${SOURCE_TREE}/daaam/pyproject.toml" ] || die "active source tree is incomplete"
  docker image inspect "${BASE_IMAGE}" >/dev/null 2>&1 || die "base image ${BASE_IMAGE} not found (docker compose build daaam-ws)"
  docker volume inspect "${VOLUME}" >/dev/null 2>&1 || die "workspace volume ${VOLUME} not found"
  docker run --rm -v "${VOLUME}:/ws:ro" --entrypoint test "${BASE_IMAGE}" -x /ws/.venv/bin/python -a -f /ws/install/setup.bash \
    || die "the workspace volume has no .venv / install tree: run docker/setup_workspace.sh first"

  docker rm -f "${STAGE_CONTAINER}" >/dev/null 2>&1 || true
  log "copying the workspace volume and this checkout into a container"
  docker run --name "${STAGE_CONTAINER}" --user root --entrypoint bash \
    -v "${VOLUME}:/mnt/src_volume:ro" -v "${REPO}:/mnt/src_repo:ro" \
    --mount "type=bind,src=${SOURCE_TREE},dst=/mnt/src_tree,readonly" \
    "${BASE_IMAGE}" -e -u -o pipefail -c '
      rm -rf /opt/daaam_ws/.venv
      bash /mnt/src_repo/docker/copy_workspace.sh /mnt/src_volume /mnt/src_tree /mnt/src_repo /opt/daaam_ws
      chmod 1777 /opt/daaam_ws/cache
      chown -R daaam:daaam /opt/daaam_ws/src/daaam
    ' || { docker rm -f "${STAGE_CONTAINER}" >/dev/null 2>&1 || true; die "copying into the container failed"; }

  log "committing ${FULL_IMAGE}"
  docker commit \
    --message "DAAAM workspace snapshot $(date -u +%F)" \
    --change "ENV HF_HOME=${WS}/cache/huggingface" \
    --change "ENV TORCH_HOME=${WS}/cache/torch" \
    --change "ENV XDG_CACHE_HOME=${WS}/cache" \
    --change "USER daaam" \
    --change "WORKDIR ${WS}/src/daaam" \
    --change 'ENTRYPOINT ["/usr/local/bin/daaam-entrypoint"]' \
    --change 'CMD ["bash"]' \
    "${STAGE_CONTAINER}" "${FULL_IMAGE}"
  docker rm "${STAGE_CONTAINER}" >/dev/null
  docker image ls "${FULL_IMAGE}"
}

step_verify() {
  docker image inspect "${FULL_IMAGE}" >/dev/null 2>&1 || die "image ${FULL_IMAGE} not found; run the 'image' step first"
  log "self check of ${FULL_IMAGE} (no volumes, no network)"
  docker run --rm --gpus all --network none "${FULL_IMAGE}" bash docker/setup_workspace.sh check
}

pack() {  # pack OUT_FILE LEVEL < tar stream
  zstd -T0 "-$2" -f -q -o "$1.part" && mv "$1.part" "$1"
}

PARTS=()
wanted() {  # is the part among the requested ones (all parts when none were requested)?
  local p
  [ "${#PARTS[@]}" -eq 0 ] && return 0
  for p in "${PARTS[@]}"; do [ "${p}" = "$1" ] && return 0; done
  return 1
}

step_export() {
  local out="${1:-}" p image_id
  local fresh=()  # archives written by this call, whose checksums must be recomputed
  [ -n "${out}" ] || die "usage: build_full_image.sh export OUT_DIR [PART...]"
  shift
  PARTS=("$@")
  for p in ${PARTS[@]+"${PARTS[@]}"}; do
    case "${p}" in image|checkpoints|hf|sample) ;; *) die "unknown part '${p}' (image, checkpoints, hf, sample)" ;; esac
  done
  command -v zstd >/dev/null || die "zstd is required"
  mkdir -p "${out}"

  if wanted image; then
    docker image inspect "${FULL_IMAGE}" >/dev/null 2>&1 || die "image ${FULL_IMAGE} not found; run the 'image' step first"
    log "image -> ${out}/daaam-image.tar.zst"
    docker save "${FULL_IMAGE}" | pack "${out}/daaam-image.tar.zst" 6
    fresh+=(daaam-image.tar.zst)
    image_id="$(docker image inspect "${FULL_IMAGE}" --format '{{.Id}}')"
  else
    image_id="$(sed -n 's/^IMAGE_ID=//p' "${out}/bundle.env" 2>/dev/null || true)"
    [ -n "${image_id}" ] || die "no earlier bundle in ${out}: export the image part first"
  fi

  if wanted checkpoints; then
    [ -f checkpoints/clip/ViT-B-16.pt ] || die "model files are incomplete: run python scripts/download_models.py"
    log "model files in checkpoints/ -> ${out}/daaam-assets-checkpoints.tar.zst"
    tar -C "${REPO}" -cf - --exclude='*.onnx' --exclude='*.engine' --exclude='*.part' checkpoints \
      | pack "${out}/daaam-assets-checkpoints.tar.zst" 3
    fresh+=(daaam-assets-checkpoints.tar.zst)
  fi

  if wanted hf; then
    resolve_workspace
    log "Hugging Face cache -> ${out}/daaam-assets-hf.tar.zst"
    docker run --rm -v "${VOLUME}:/ws:ro" --entrypoint tar "${BASE_IMAGE}" \
      -C /ws/cache/huggingface -cf - --exclude='*.incomplete' --exclude='.locks' hub \
      | pack "${out}/daaam-assets-hf.tar.zst" 3
    fresh+=(daaam-assets-hf.tar.zst)
  fi

  if wanted sample && [ -d datasets/tum_fr1_desk ]; then
    log "sample dataset -> ${out}/daaam-sample-tum_fr1_desk.tar.zst"
    tar -C "${REPO}/datasets" -cf - tum_fr1_desk | pack "${out}/daaam-sample-tum_fr1_desk.tar.zst" 3
    fresh+=(daaam-sample-tum_fr1_desk.tar.zst)
  fi

  log "deploy scripts and checksums"
  install -m 755 docker/deploy/deploy.sh docker/deploy/daaam-run "${out}/"
  [ ! -f README.md ] || install -m 644 README.md "${out}/"
  # checksums of the large archives only (they guard the transfer; the scripts and the guide are plain text).
  # Archives that were not exported now keep the hash recorded earlier, which saves minutes on a partial export.
  (
    cd "${out}"
    [ -f SHA256SUMS ] || : > SHA256SUMS
    for f in daaam-*.tar.zst; do
      if ! grep -q "  ${f}\$" SHA256SUMS || printf '%s\n' ${fresh[@]+"${fresh[@]}"} | grep -qx "${f}"; then
        sed -i "/  ${f}\$/d" SHA256SUMS
        sha256sum -- "${f}" >> SHA256SUMS
      fi
    done
    sort -k2 -o SHA256SUMS SHA256SUMS
  )
  # ASSETS_ID changes only when a weights archive changes, so deploy.sh can skip unpacking unchanged weights
  printf 'IMAGE_TAG=%s\nIMAGE_ID=%s\nBUNDLE_ID=%s\nASSETS_ID=%s\n' "${FULL_IMAGE}" "${image_id}" "$(date -u +%Y%m%d-%H%M%S)" \
    "$(grep 'daaam-assets-' "${out}/SHA256SUMS" | sha256sum | cut -c1-16)" > "${out}/bundle.env"
  ls -lh "${out}"
}

case "${1:-}" in
  image)  step_image ;;
  verify) step_verify ;;
  export) shift; step_export "$@" ;;
  all)    step_image; step_verify; step_export "${2:-}" ;;
  *)      usage; exit 2 ;;
esac
