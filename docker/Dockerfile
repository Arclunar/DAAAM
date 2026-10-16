# syntax=docker/dockerfile:1.7
FROM nvidia/cuda:12.8.1-cudnn-devel-ubuntu24.04

ARG ROS_DISTRO=jazzy
ARG ROS_APT_SOURCE_VERSION=1.3.0
ARG UBUNTU_MIRROR=https://mirrors.ustc.edu.cn/ubuntu
ARG UBUNTU_SECURITY_MIRROR=https://mirrors.ustc.edu.cn/ubuntu
ARG ROS_MIRROR=https://mirrors.ustc.edu.cn/ros2/ubuntu
ARG ROSDISTRO_INDEX_URL=https://mirrors.ustc.edu.cn/rosdistro/index-v4.yaml
ARG USERNAME=daaam
ARG USER_UID=1000
ARG USER_GID=1000

ENV DEBIAN_FRONTEND=noninteractive \
    ROS_DISTRO=${ROS_DISTRO} \
    ROSDISTRO_INDEX_URL=${ROSDISTRO_INDEX_URL} \
    LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8 \
    VIRTUAL_ENV=/opt/daaam_ws/.venv \
    PATH=/opt/daaam_ws/.venv/bin:${PATH} \
    COLCON_DEFAULTS_FILE=/opt/daaam_ws/colcon_defaults.yaml \
    CMAKE_BUILD_PARALLEL_LEVEL=2 \
    MAKEFLAGS=-j2 \
    PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility,graphics

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Configure package-management and general development tooling. Project source,
# Python requirements and ROS workspace dependencies are intentionally omitted.
RUN printf '%s\n' \
      'Acquire::Retries "5";' \
      'Acquire::http::Timeout "30";' \
      'Acquire::https::Timeout "30";' \
      > /etc/apt/apt.conf.d/80-daaam-network \
    && sed -i \
      -e "s|http://archive.ubuntu.com/ubuntu|${UBUNTU_MIRROR}|g" \
      -e "s|http://security.ubuntu.com/ubuntu|${UBUNTU_SECURITY_MIRROR}|g" \
      /etc/apt/sources.list.d/ubuntu.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
      ca-certificates curl git gnupg2 locales software-properties-common sudo \
    && locale-gen en_US en_US.UTF-8 \
    && add-apt-repository universe \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL \
      "https://github.com/ros-infrastructure/ros-apt-source/releases/download/${ROS_APT_SOURCE_VERSION}/ros2-apt-source_${ROS_APT_SOURCE_VERSION}.$(. /etc/os-release && echo "${VERSION_CODENAME}")_all.deb" \
      -o /tmp/ros2-apt-source.deb \
    && apt-get install -y /tmp/ros2-apt-source.deb \
    && rm /tmp/ros2-apt-source.deb \
    && sed -i \
      -e "s|http://packages.ros.org/ros2/ubuntu|${ROS_MIRROR}|g" \
      -e "s|https://packages.ros.org/ros2/ubuntu|${ROS_MIRROR}|g" \
      -e 's/^Types: deb deb-src$/Types: deb/' \
      /etc/apt/sources.list.d/ros2.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
      build-essential cmake ninja-build pkg-config \
      python3-dev python3-pip python3-tk python3-venv python3-vcstool \
      libgoogle-glog-dev nlohmann-json3-dev glpk-utils libglpk-dev \
      libgl1 libglib2.0-0t64 libxext6 libxrender1 libsm6 \
      ros-${ROS_DISTRO}-ros-base ros-dev-tools \
    && (rosdep init 2>/dev/null || true) \
    && rm -rf /var/lib/apt/lists/*

# Match the host user so bind-mounted source files are not created as root.
RUN existing_group="$(getent group "${USER_GID}" | cut -d: -f1 || true)" \
    && if [[ -n "${existing_group}" && "${existing_group}" != "${USERNAME}" ]]; then \
         groupmod --new-name "${USERNAME}" "${existing_group}"; \
       elif [[ -z "${existing_group}" ]]; then \
         groupadd --gid "${USER_GID}" "${USERNAME}"; \
       fi \
    && existing_user="$(getent passwd "${USER_UID}" | cut -d: -f1 || true)" \
    && if [[ -n "${existing_user}" && "${existing_user}" != "${USERNAME}" ]]; then \
         usermod --login "${USERNAME}" --home "/home/${USERNAME}" --move-home "${existing_user}"; \
       elif [[ -z "${existing_user}" ]]; then \
         useradd --uid "${USER_UID}" --gid "${USER_GID}" -m -s /bin/bash "${USERNAME}"; \
       fi \
    && usermod --gid "${USER_GID}" --shell /bin/bash "${USERNAME}" \
    && usermod -aG video "${USERNAME}" \
    && printf '%s ALL=(ALL) NOPASSWD:ALL\n' "${USERNAME}" > "/etc/sudoers.d/${USERNAME}" \
    && chmod 0440 "/etc/sudoers.d/${USERNAME}"

# The named workspace volume preserves the virtualenv, imported repositories and
# colcon outputs created later by the user.
RUN mkdir -p /opt/daaam_ws/src \
    && python3 -m venv --system-site-packages /opt/daaam_ws/.venv \
    && chown -R "${USERNAME}:${USERNAME}" /opt/daaam_ws

COPY --chown=${USERNAME}:${USERNAME} docker/colcon_defaults.yaml /opt/daaam_ws/colcon_defaults.yaml
COPY --chmod=755 docker/entrypoint.sh /usr/local/bin/daaam-entrypoint

USER ${USERNAME}
WORKDIR /opt/daaam_ws/src/daaam
ENTRYPOINT ["/usr/local/bin/daaam-entrypoint"]
CMD ["bash"]
