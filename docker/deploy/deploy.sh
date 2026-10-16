#!/usr/bin/env bash
# Install a DAAAM deployment bundle on this machine: loads the Docker image, unpacks the model assets and the
# sample dataset, installs the daaam-run wrapper and runs a self check. No internet access is needed.
#
#   ./deploy.sh [--home DIR] [--no-verify] [--no-sample] [--force]
#
#   --home DIR    where assets, datasets, output and bin/daaam-run are installed (default: $HOME/daaam)
#   --no-verify   skip the SHA256SUMS check of the bundle files
#   --no-sample   do not unpack the TUM sample dataset
#   --force       reload the image and unpack the assets again even if they are already installed
#
# Needs: docker (daemon reachable by the current user), the NVIDIA driver (>= 570) with the NVIDIA Container
# Toolkit, zstd, tar and sha256sum.
set -euo pipefail

BUNDLE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAAAM_HOME="${DAAAM_HOME:-${HOME}/daaam}"
verify=1
sample=1
force=0

log() { printf '\n[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { echo "ERROR: $*" >&2; exit 1; }
usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --home)      [ "$#" -ge 2 ] || die "--home needs a directory"; DAAAM_HOME="$2"; shift 2 ;;
    --no-verify) verify=0; shift ;;
    --no-sample) sample=0; shift ;;
    --force)     force=1; shift ;;
    -h|--help)   usage; exit 0 ;;
    *)           echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[ -f "${BUNDLE}/bundle.env" ] || die "bundle.env not found next to deploy.sh; run it from the bundle directory"
# shellcheck source=/dev/null
source "${BUNDLE}/bundle.env"   # IMAGE_TAG, IMAGE_ID, BUNDLE_ID, ASSETS_ID

avail_gb() { df -Pk "$1" | awk 'NR==2 {printf "%d", $4 / 1048576}'; }

log "preflight"
for c in docker zstd tar sha256sum; do
  command -v "${c}" >/dev/null || die "'${c}' is required (zstd: apt-get install zstd / yum install zstd)"
done
docker info >/dev/null 2>&1 || die "cannot reach the Docker daemon (is it running, and is the current user allowed to use it?)"
command -v nvidia-smi >/dev/null || die "nvidia-smi not found: install the NVIDIA driver (>= 570) and the NVIDIA Container Toolkit"
driver="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -n1)"
echo "GPU: $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | head -n1), driver ${driver}"
[ "${driver%%.*}" -ge 570 ] || die "NVIDIA driver ${driver} is too old: the image is built on CUDA 12.8, which needs driver >= 570"
docker info --format '{{json .Runtimes}}' | grep nvidia >/dev/null \
  || echo "WARNING: no 'nvidia' runtime listed by docker info; 'docker run --gpus all' needs the NVIDIA Container Toolkit"
docker_root="$(docker info --format '{{.DockerRootDir}}')"
# docker load unpacks the whole archive into a temporary directory before it creates the layers
docker_free="$(avail_gb "$(dirname "${docker_root}")")"
[ "${docker_free}" -ge 35 ] || die "only ${docker_free} GB free next to ${docker_root}: the image takes about 32 GB once loaded, and loading needs about 60 GB temporarily"
[ "${docker_free}" -ge 62 ] || echo "WARNING: ${docker_free} GB free next to ${docker_root}; loading the image needs about 60 GB temporarily (32 GB remain afterwards) and may fail"
mkdir -p "${DAAAM_HOME}"
[ "$(avail_gb "${DAAAM_HOME}")" -ge 12 ] || die "less than 12 GB free in ${DAAAM_HOME} (model weights and datasets)"

if [ "${verify}" = 1 ]; then
  log "verifying the bundle files (--no-verify skips this)"
  (cd "${BUNDLE}" && sha256sum -c SHA256SUMS)
fi

unpack() {  # unpack ARCHIVE DEST_DIR
  mkdir -p "$2"
  zstd -dc "$1" | tar -x --no-same-owner -C "$2"
}

have_id="$(docker image inspect "${IMAGE_TAG}" --format '{{.Id}}' 2>/dev/null || true)"
if [ "${force}" = 0 ] && [ -n "${have_id}" ] && [ "${have_id}" = "${IMAGE_ID}" ]; then
  log "image ${IMAGE_TAG} is already loaded"
else
  log "loading the Docker image (a few minutes)"
  if command -v pv >/dev/null; then
    pv "${BUNDLE}/daaam-image.tar.zst" | zstd -dc | docker load
  else
    zstd -dc "${BUNDLE}/daaam-image.tar.zst" | docker load
  fi
  docker image inspect "${IMAGE_TAG}" >/dev/null 2>&1 || die "docker load did not produce ${IMAGE_TAG}"
fi

marker="${DAAAM_HOME}/assets/.bundle-id"
if [ "${force}" = 0 ] && [ -f "${marker}" ] && [ "$(cat "${marker}")" = "${ASSETS_ID}" ]; then
  log "model assets are already unpacked in ${DAAAM_HOME}/assets"
else
  log "unpacking the model assets into ${DAAAM_HOME}/assets"
  unpack "${BUNDLE}/daaam-assets-checkpoints.tar.zst" "${DAAAM_HOME}/assets"
  unpack "${BUNDLE}/daaam-assets-hf.tar.zst" "${DAAAM_HOME}/assets/hf"
  echo "${ASSETS_ID}" > "${marker}"
fi

if [ "${sample}" = 1 ] && [ -f "${BUNDLE}/daaam-sample-tum_fr1_desk.tar.zst" ]; then
  if [ "${force}" = 0 ] && [ -d "${DAAAM_HOME}/datasets/tum_fr1_desk" ]; then
    log "sample dataset is already in ${DAAAM_HOME}/datasets"
  else
    log "unpacking the TUM fr1/desk sample dataset"
    unpack "${BUNDLE}/daaam-sample-tum_fr1_desk.tar.zst" "${DAAAM_HOME}/datasets"
  fi
fi

mkdir -p "${DAAAM_HOME}/bin" "${DAAAM_HOME}/output" "${DAAAM_HOME}/datasets"
install -m 755 "${BUNDLE}/daaam-run" "${DAAAM_HOME}/bin/daaam-run"
# started with sudo: hand everything over to the invoking user, who runs the containers
if [ -n "${SUDO_USER:-}" ]; then
  chown -R "${SUDO_USER}:" "${DAAAM_HOME}"
fi

log "self check (loads DAM-3B onto the GPU)"
if ! "${DAAAM_HOME}/bin/daaam-run" check; then
  die "everything is installed in ${DAAAM_HOME} but the self check failed (output above). A GPU that is busy with other jobs can make loading DAM-3B fail; run ${DAAAM_HOME}/bin/daaam-run check again"
fi

cat <<EOF

DAAAM is installed in ${DAAAM_HOME}.

  ${DAAAM_HOME}/bin/daaam-run tum-example                # TUM fr1/desk sample, about 4 minutes on a 16 GB GPU
  ${DAAAM_HOME}/bin/daaam-run pipeline /path/to/dataset --dataset-type ImageSequenceDataset ...
  ${DAAAM_HOME}/bin/daaam-run help

Results are written to ${DAAAM_HOME}/output.
EOF
