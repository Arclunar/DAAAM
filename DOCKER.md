# DAAAM Docker 开发环境

该镜像只准备 DAAAM 所需的基础环境：Ubuntu 24.04、CUDA 12.8、cuDNN、
ROS 2 Jazzy、Python 虚拟环境、C/C++ 构建工具和系统库。它不会复制、拉取、
安装或编译 DAAAM 及其工作区依赖，也不会执行 `requirements.txt`。

## 宿主机要求

- Linux 和 NVIDIA GPU 驱动（宿主机不需要安装 CUDA Toolkit）
- Docker、Docker Compose v2 和 NVIDIA Container Toolkit
- 建议至少 24 GB GPU 显存；显存较小时需使用较小模型或输入尺寸

## 构建

在仓库根目录执行：

```bash
USER_UID=$(id -u) USER_GID=$(id -g) docker compose build
```

首次构建会下载 CUDA 基础镜像、ROS 2 和系统开发包。镜像本身不含项目
Python 依赖和其他工作区源码，所以项目代码变动不会触发镜像重建。
默认使用 USTC 的 Ubuntu、ROS 2 和 rosdep 镜像以改善国内网络下的构建速度。
如果需要官方源，可显式覆盖：

```bash
UBUNTU_MIRROR=http://archive.ubuntu.com/ubuntu \
UBUNTU_SECURITY_MIRROR=http://security.ubuntu.com/ubuntu \
ROS_MIRROR=https://packages.ros.org/ros2/ubuntu \
ROSDISTRO_INDEX_URL=https://raw.githubusercontent.com/ros/rosdistro/master/index-v4.yaml \
USER_UID=$(id -u) USER_GID=$(id -g) docker compose build
```

## 进入环境

```bash
docker compose run --rm daaam
```

当前仓库会出现在 `/opt/daaam_ws/src/daaam`，并作为默认工作目录。入口脚本会
自动加载 ROS 2、`/opt/daaam_ws/.venv`，以及存在时的 colcon 安装空间。

`daaam_workspace` 命名卷保存虚拟环境、同级依赖仓库、`build/`、`install/`
和 `log/`。因此即使使用 `--rm`，你之后手动完成的项目安装和编译仍会保留。
仓库源码则直接绑定到宿主机，容器内的源码修改会立即反映到宿主机。

## 手动安装项目

进入容器后，由你按项目文档执行安装。例如完整安装入口是：

```bash
bash install/install.sh
```

该脚本会导入其他仓库、运行 rosdep 和 colcon，并安装 Python 依赖。仓库清单
使用 GitHub SSH 地址。若要用 SSH 克隆，需先在宿主机启动 SSH agent 并加载
密钥：

```bash
eval "$(ssh-agent -s)"
ssh-add ~/.ssh/id_rsa    # 换成你自己的私钥路径
```

agent 与当前终端会话绑定，新开的终端需重新执行上面的命令。之后
`$SSH_AUTH_SOCK` 会指向 agent 的 socket；若该变量为空，下面的挂载会报
`invalid mount path` 错误。然后可用下面的命令进入容器并转发 agent：

```bash
docker compose run --rm \
  -e SSH_AUTH_SOCK="$SSH_AUTH_SOCK" \
  -v "$SSH_AUTH_SOCK:$SSH_AUTH_SOCK" \
  daaam
```

首次连接 GitHub 时会询问 host key 是否可信，直接回答 `yes` 即可。

也可以自行把清单中的地址换成 HTTPS。上述项目安装步骤特意没有放进
Dockerfile。

## 自动化安装与跨服务器部署

除了 `install/install.sh`，仓库还带了一套可断点续跑的自动化安装，以及把安装结果搬到
其他服务器的打包流程：

- `daaam-ws` 服务（`docker/Dockerfile.deps`）：在开发镜像之上固化 rosdep 解析出的系统包。
  工作区卷里编译好的产物依赖这些库，而在 `run --rm` 容器里用 rosdep 装的包会随容器消失。
- `docker/setup_workspace.sh`：导入仓库（HTTPS，不需要 SSH 密钥）、编译 C++ 工作区、
  安装 Python 依赖（含 `install.sh` 漏装的 `hydra_python`）并做导入自检：

```bash
docker compose build daaam-ws
docker compose run --rm -T daaam-ws bash docker/setup_workspace.sh
docker compose run --rm -T daaam-ws python scripts/download_models.py
```

- `docker/build_full_image.sh` 与 `docker/deploy/`：把装好的环境打成自包含镜像和部署包，
  在另一台服务器上一键部署，不需要重新编译或下载。详见 [DEPLOY.md](DEPLOY.md)。

## 环境检查

镜像构建完成后执行：

```bash
./docker/check.sh
```

检查脚本只验证 GPU、ROS 2、colcon、Python、CMake、虚拟环境和挂载目录，
不会要求 DAAAM 已经安装。

## GUI、ROS 2 网络与数据

Compose 使用 host 网络方便 DDS 发现，并挂载 X11 socket。需要 GUI 时可在
宿主机执行：

```bash
xhost +si:localuser:$(id -un)
```

使用其他 ROS domain：

```bash
ROS_DOMAIN_ID=42 docker compose run --rm daaam
```

大型数据集建议按需额外挂载，避免复制进仓库或镜像：

```bash
docker compose run --rm -v /宿主机/数据路径:/datasets:ro daaam
```

## 清理

普通的 `docker compose down` 不会删除工作区。只有确定不再需要容器内手动
安装的依赖和编译结果时，才执行以下命令：

```bash
docker compose down --volumes
```

该命令会删除 `daaam_workspace` 卷，其中的虚拟环境、依赖源码和编译结果无法
从容器恢复；绑定挂载的当前 DAAAM 仓库不会被删除。
