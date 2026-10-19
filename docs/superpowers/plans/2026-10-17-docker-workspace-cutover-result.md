# Docker 开发工作区切换结果

日期：2026-10-17（Asia/Shanghai）  
状态：**ACTIVE_WSL_SOURCES（全部源码已从 WSL 目录提供，Docker 开发入口已启用）**

## 最终入口

```text
/home/arclunar/warmoon_ws/daaam_ws/src/
├── daaam/              主仓库实际位置
├── daaam_ros/
├── hydra/
├── hydra_ros/
└── 其他13个源码仓库
```

共 17 个仓库。Windows 入口：
`\\wsl.localhost\Ubuntu-20.04\home\arclunar\warmoon_ws\daaam_ws\src`。

原 `/home/arclunar/warmoon_ws/DAAAM` 是指向新主仓库的 **bind 挂载兼容入口**，
与新位置是同一份文件，不是另一份源码。Linux 符号链接在本机 Windows 工作目录启动场景出现错误，
因此改用 bind，并在 `/etc/fstab` 增加：

```text
/home/arclunar/warmoon_ws/daaam_ws/src/daaam /home/arclunar/warmoon_ws/DAAAM none bind,nofail 0 0
```

当前挂载与 Windows 工作目录访问已验证，fstab 已保存；没有为了测试持久化而重启 WSL，
原先运行在 8766 端口的回看服务继续响应 HTTP 200。旧、新入口同一份数据，不应把旧入口当独立副本删除。

## 日常操作

在主仓库中：

```bash
just run
just exec
just build
just build hydra_ros spark_dsg
just check
just tum-test 30
just replay-web
```

- `run` 新开已自动加载 ROS/venv/工作区的交互终端；`exec` 只选择当前 Compose 项目的运行中容器，多个时需提供 ID，不随意选择第一个。
- `build` 仅扫描 src，复用原构建产物；指定包时包含其依赖，失败保留非零退出码。源码缓存/vendor 中的训练项目不会误入构建。
- `check` 验证活动源码、原命名卷、ROS/Python 模块、GLPK_MI 及真实 GPU 张量运算。
- Python 修改在新进程中生效；C++ 需构建，Python 绑定 C++ 修改还需容器内的 `bash docker/setup_workspace.sh pip-ws`。
- 原裸 just、TUM/CODa/回看命令保持已有语义。

## 存储与配置

两个 Compose 服务挂载完整 WSL src 到 `/opt/daaam_ws/src`，并继续使用原 `daaam_daaam_workspace` 卷。
`.venv`、`build`、`install`、模型缓存和 vendor 没有搬走或重装。容器内源码绝对路径保持原样。

本地 `.env` 定向设置 `DAAAM_SRC_DIR`、`COMPOSE_PROJECT_NAME=daaam` 和 `WORKSPACE_VOLUME`，
保留原有其他配置，未输出或提交密钥。绑定目录缺失时明确报错，不自动建立空目录遮蔽源码。

原卷 src 保留为切换前快照，已不再用于开发。部署镜像组装及独立 HF 缓存导出都读取当前 Compose
配置，先核对真实卷身份；组装时只取活动 WSL 源码，不取卷内过时源码。
本次没有重新生成几十 GB 的部署镜像或部署包，现有部署包仍为历史版本。

## 执行证据

备份与日志：`/home/arclunar/warmoon_ws/.daaam-source-export/cutover_20261017_103911`。

其中 `backup/` 保存原 Compose、justfile、检查和部署脚本、本地 `.env`、fstab；含配置的备份限制文件权限。
`source-before.json` 和 `destination-before.json` 记录切换前依赖源码无漂移。
先前导出证据在相邻 `20261017_103512` 目录中保留。

| 验证 | 实际结果 |
|---|---|
| 源码漂移检查 | 切换前原卷与 WSL 16 个依赖一致 |
| Windows 路径 | 新 src 可列出 17 仓库；原路径可作为程序工作目录 |
| 两个 Compose 服务 | src 为新 WSL bind，依赖卷为原卷，无单仓库重复挂载 |
| 双向写读 | 临时探针在 WSL 创建、容器读写、WSL 验证；已移除探针 |
| 环境检查 | rclpy、hydra_python、spark_dsg、daaam、Torch、TensorRT、cvxpy 均可导入；GLPK_MI 和 RTX 5060 Ti CUDA 运算通过 |
| 全工作区增量构建 | 30 packages finished，11.2 秒，无失败包；见 build.log |
| 宿主相关测试 | 99 项通过；见 host-tests.log |
| 容器回看与图处理测试 | 32 项通过；见 container-tests.log |
| Windows Chrome | 多结果标签页独立打开与刷新恢复，1 项实际浏览器测试通过 |
| 新挂载建图冒烟 | TUM fr1_desk 30/30，Hydra 30 帧，命令返回 0；见 tum-test.log |
| 新回看产物 | 8 个快照、参考网格、图像、replay/manifest.json 与 replay.rrd 均生成 |
| 代码审查 | 独立只读审查提出的卷解析和日志表述问题已修复，复查无重要遗留问题 |

冒烟输出：
`/home/arclunar/warmoon_ws/daaam_ws/src/daaam/output/tum_rrd_test/out_20261017_024341`。
输出目录时间使用原容器时区；用户操作日期以 Asia/Shanghai 为准。该测试按现有 tum-test 定义关闭 DAM，
所以没有描述属于预期；它不代表完整 DAM 长序列或新的性能对比测试。

验证中发现 colcon 默认扫描整个工作区会误识别 `cache/torch/hub` 和 `vendor` 中的 Python 项目，
已在新构建入口固定 `--base-paths src`，重跑全工作区构建通过。为卷名独立解析、参数传递、失败退出、
容器选择和打包活动源码增加行为测试，含对应故障到通过的验证。

## 回退方法

如需返回旧依赖源码挂载，先结束本项目正在执行的任务，确认旧卷源码没有被修改，再从上述 `backup/compose.yaml`
恢复原 Compose 配置。原配置仍把当前主仓库挂到 `/opt/daaam_ws/src/daaam`，其余依赖重新来自原卷；
主仓库的 bind 兼容入口和物理新位置可以保留，不需要删除新源码或重建依赖。

回退前先保存新配置和新源码改动，不从旧卷反向覆盖 WSL 中的新编辑。如果要同时恢复原物理目录，
需要在空闲窗口卸载旧入口的 bind、移除其空挂载点、把实际主仓库改名回原位置，并精确移除本任务添加的
fstab 一行；不得递归删除旧入口。该物理恢复本次未执行。

## 完成状态

- [x] 缺失依赖源码导出、校验。
- [x] 主仓库归位及旧路径兼容。
- [x] 完整 src 挂载，原依赖卷保留。
- [x] 开发命令、部署源码来源和文档整理。
- [x] 构建、测试、GPU、建图和浏览器验证。
- [x] CHANGELOG 与状态标记更新，证据与回退记录保存。

没有提交或推送本次修改，也没有清理旧卷、镜像或归档。
