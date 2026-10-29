# DAAAM 本地工作区

> 在 WSL Ubuntu-20.04 的 `~/jellymap_dev` 使用根 justfile；算法源码位于 `third_party/src/daaam_ws/src/daaam`。

## 重大更新：长跑内存与描述持久化（2026-10-25）

离线入口按 assignment 生命周期释放仅含 RGB 的历史帧，缓存预算默认512 MiB；它不限制模型、地图或整个进程。首条 DAM 描述立即保存，后续有更新约每5秒原子保存 `corrections.yaml`，保留来源和历史，最终保存复用同一写入器。中断仍可能丢失最近一个保存间隔；不提供地图恢复或精确续跑。

入口设置 `grounding.release_assignment_frames=true`、`scene_graph.corrections_save_interval_seconds=5`，配置本身默认关闭。Khronos静态历史精简由 `KHRONOS_COMPACT_STATIC_FRAMES=1` 启用，动态帧完整保留；`DAAAM_TRIM_INTERVAL_FRAMES=100` 每100帧归还空闲堆。离线入口默认开启二者，设0关闭；原生开关未设置时保持上游。历史容量、采样和输入分辨率不变。更新已有环境用 `just daaam build khronos`，不必重建镜像。完整说明见工作区 `sm-lab/systems/daaam/README.md`。

## 原生在线归并（实验）

先在根目录执行 `just daaam build`，再运行 `just daaam run <Dataset> all --no-tui --reconciliation native`。该模式复用已有CLIP编码DAM文本，将已有语义、已归档且满足原生空间/时序条件的片段交给Khronos变化检测与Reconciler，每30帧及末帧发布当前归并图；Fragment保留，历史快照落盘。第三方默认不开启；实验入口显式设置 `DAAAM_ONLINE_RECONCILIATION=1`。

这是现有离线Hydra后端的原生算法适配，尚不等同于完整ROS/RPGO后端。变化检测随地图增长可能阻塞数十秒，不保证逐帧实时；不会常驻保存完整4D历史图。归并Object的问答和RRD绘制尚未接入。开关、输出及限制详见根目录 `sm-lab/systems/daaam/README.md`。

## UQ-DAAAM（可选，默认关闭）

UQ-DAAAM（arXiv:2606.08277）是对描述环节的改动。原版每个物体只用一帧描述一次；开启后改为：

1. 每个物体先选 r=3 个视角，各描述一次；
2. 用描述嵌入张成的体积 u = log det(ZᵀZ + λI) 衡量这几条描述之间的分歧（Z 的每列是按 token 概率加权的单位嵌入）；
3. u > τ 时，按选帧打分 q 依次补视角，最多 B=2 个，直到 u ≤ τ；
4. 最后由 LLM 把所有描述融合成一条，融合时只能用已有描述里的事实。

`uq.enabled=false` 时走原版路径，保存的配置与上游逐字一致。全部实现都在本仓库，不依赖 JellyMap 代码。

```bash
just daaam run <转换目录> all --uq                                # 结果目录 uq-daaam_<数据>_<时间>_<ID>
EXTRA_OVERRIDES="uq.refine_budget=3 uq.tau=-0.3" just daaam run <转换目录> all --uq   # 其余 uq.* 用覆盖项
```

- **代码**：`src/daaam/uq/`。
  - `uncertainty`：u，以及增量更新（Schur 补）；
  - `refinement`：初始视角、补视角与停止规则、统计；
  - `scoring`：DAM token 平均概率。通过 logits processor 读取，不修改 `dam` 包；
  - `fusion`：融合提示、严格 JSON 校验、确定性回退。
- **接入点**：
  - assignment 的 `select_prompt_images_uq`：r-assignment 整数规划，同时为每个物体选出补视角候选；
  - orchestrator 的 `_process_uq_groups`：把一次 assignment 的全部视角打成一个包，送往同一个 grounding worker；
  - grounding 的 `_process_uq_batch`：批量打分描述、计算 u、补视角、融合。
- **配置**（`uq.*`，默认值）：

  | 键 | 默认值 | 说明 |
  | --- | --- | --- |
  | `views_per_object` | 3 | r |
  | `min_views` | 2 | 可见帧少于它时只描述 1 次 |
  | `refine_budget` | 2 | B |
  | `tau_mode` / `tau` / `tau_quantile` | `fixed` / −0.25 / 0.8 | `quantile` 模式只统计已见物体的初始 u |
  | `alpha` / `lam` | 1.0 / 1e-8 | |
  | `text_encoder` | `clip` | 复用已加载的 CLIP |
  | `caption_temperature` | 1.0 | |
  | `solver_time_limit_s` | 15 | |
  | `save_views` | true | |
  | `shutdown_timeout_s` | 3600 | |
  | `fusion_enabled` / `fusion_model` | true / `gpt-5-mini` | 密钥取 `.env` 的 `OPENAI_API_KEY`；没有密钥时使用回退描述 |

- **输出**（结果 `out_*` 目录内，回看工具只需读文件）：
  - `uq_objects.jsonl`：每个物体一行，包括：
    - 身份：`semantic_id`、`track_id`；
    - u 与停止：`u_initial`、`u_final`、`tau`、`stop_reason`；
    - 融合：`final_caption`、`fusion_status`、`facts_used`、`facts_omitted`；
    - `views[]`：每个视角的阶段、帧号、时间、q、p、γ、u、是否使 u 下降、描述，`image_path`（`uq_views/frames/<帧>.jpg`）、`mask_path`（`uq_views/masks/<标签>_<帧>.png`）及其哈希。
  - `uq_summary.json`：物体数、初始超阈值数、URR、DAM 调用次数（初始/补视角）、融合状态、u 分布、配置，以及收尾时是否有 grounding 被中断（`grounding_interrupted`）。
  - 写回的描述：每个物体一条融合描述，写入场景图和 `corrections.yaml`，描述源帧是 q 最高的视角。离线入口默认开启 `save_description_masks`，该 Mask 存为 `description_masks/<标签>_<帧>.png`，并记在 `description_source.mask`。
- **与论文的差异**：
  - τ 默认用附录的固定值 −0.25；论文正文用校准集上的分位数，并对 u 做了归一化，所以 u 的数值不能直接和论文比较。
  - CLIP 文本编码在 77 token 处截断。
  - 增量更新用 Schur 补，没有用 Sherman–Morrison。
  - 补视角候选在选帧时一并选出：窗口帧在 assignment 后会被释放，预算 B 内这与论文逐次查询等价。
- **限制**：
  - 只接了离线 RGB-D 入口，ROS2 入口不支持。
  - 融合会把描述文本发到 OpenAI。
  - 开启后总耗时约为原版的 2.4 倍（jellyhold 703 帧实测）。
  - 不保存 grounding 调试图。
  - 没有在 OC-NaVQA 上评测。

## 前端中间结果导出（可选，默认关闭）

`save_intermediates=true`（`scripts/run_rgbd.py --intermediates`，或写进 `EXTRA_OVERRIDES`）时，在 `out_*/intermediates/` 记录 Hydra 场景图之前每一步的产物，供 Replay 的“逐帧过程”页查看：

- `frames.jsonl`：每帧一行，包括检测数、每个 track 的 Track ID → 语义 ID、分数、框、面积、深度校验结果和中位深度。
- `labels/<帧>.png`：送进 Hydra 的 uint16 标签图（像素值 = 语义 ID），每帧一张。重叠的掩码只保留后写的那个，与送入 Hydra 的一致。
- `events.jsonl`：
  - 每次查询（窗口内每个 track 的状态：有资格、已提示、深度无效、观测不足、已有描述，判断顺序与选帧循环一致）；
  - 选帧结果、送入 DAM、描述写回；
  - 自动 unknown、清除 unknown、重新提示，以及因队列满被丢弃的查询。

  事件记的帧号是它发生时主循环正在处理的帧，后台进程返回的结果会晚于触发它的查询。

写盘在后台线程完成，主循环只把引用放进队列，耗时记在 `performance_statistics.csv` 的 `record_intermediates`。默认关闭时不创建目录，保存的配置与上游一致。实现在 `src/daaam/pipeline/intermediates.py`，单测 `tests/test_intermediates.py`。

## 职责与目录

| 位置 | 内容 |
| :--- | :--- |
| `third_party/src/daaam_ws/src/daaam/` | 算法、配置、输入适配、建图与后处理 |
| `sm-lab/datasets/` | TUM/CODa 准备、实录转换、参考网格、GS 训练/渲染与对应单测 |
| `sm-lab/viewers/replay/` | replay/Rerun 工具、相关测试、结果与服务状态 |
| `DAAAM/` | 算法仓库的现有绑定挂载兼容入口 |

数据与结果旧目录保留绑定挂载入口；源码与结果只有一份。容器同时挂载 sm-lab/datasets、sm-lab/viewers/replay；现有模型和编译依赖仍在原 Docker 卷。环境操作见下方“开发环境”。

## 建图

```bash
cd ~/jellymap_dev
just daaam run <转换目录>             # 统一 RGB-D → DAAAM；默认全段
just daaam run <转换目录> 50          # 连续前 50 帧
just daaam run <转换目录> 50 --dry-run # 只校验输入并预览启动命令
just tum-run 298 fr1_room              # TUM RGB-D 语义建图
just daaam tum-run 298 fr1_room        # TUM 建图与 replay 导出
just habitat-live                     # 手动操作 Habitat
just fisheye-run                     # 鱼眼 → ROS 2 → DAAAM/Hydra 语义建图
just coda-run <bag目录> 0.3            # 已准备的 CODa bag
just gs-daaam <容器内RGB-D目录> 30     # 高斯渲染或实录转换后的数据
```

建图任务串行运行。仿真需要 WSLg；前台任务用 Ctrl+C 收尾。不要直接杀进程后把未完整保存的结果算作成功。

`run` 消费 sm-lab/datasets 已处理的统一 RGB-D，不读取 ROS1 bag、标定或 Jellyhold 转换报告。输入是逐帧同名 `rgb/*.png`（8 位彩色）、`depth/*.png`（16 位、0 无效）、`pose/poses.txt`（每行 16 个数的行优先 RGB 光学 `T_world_camera`）与 `camera_info.json`（width、height、零畸变 intrinsics、depth_scale、fps）；不要混入 JPG/NPY 等另一套帧文件。同一目录可交给 GS 训练。路径相对于调用命令时的目录，也支持绝对路径和含空格路径。默认处理上限 3 FPS，可加 `--target-fps`；建图深度范围默认 0.3–8 m，可加 `--depth-lb/--depth-ub`。

运行读取元信息的深度比例和帧率，不猜测传感器单位；原始 `timestamps.txt` 若存在会校验数量与递增性，但当前 DAAAM 时间轴仍按帧号/fps。输入只读，每次生成新 `rgbd_*` 结果目录并保存 `rgbd_launch.json` 和 `rgbd_run.log`。旧 `gs-daaam` 兼容入口保留，新数据使用通用 `run`。本地测试实体统一在 tests；从 jellymap_dev 执行 `python3 tests/scripts/run_tests.py --agent <codex或claude/会话ID/执行agent> --dataset <转换目录>`，由运行器执行入口测试与实际 GS/DAAAM 读取器对比。完整运行结束后再用 `tests/scripts/verify_full_rgbd.py <转换目录> <运行父目录>` 验收。

结果统一位于 runs/daaam/ 下的独立 out_* 目录；旧 output/ 入口仍有效。独立 sm-lab/sim/habitat 适配器的结果保留原位，runs/ 提供对应入口。

## 数据与 GS

```bash
just datasets --list
just datasets tum-fetch fr1_room
just datasets coda-download 1000
just datasets coda-depth
just datasets coda-bag 0 100 demo
just datasets gs-train fr1_room
just datasets gs-render latest
just datasets gs-view latest
```

容器内 /datasets 表示转换后 TUM，/data 表示现有 E 盘数据根目录。详细输入格式、传输与处理命令见 [sm-lab/datasets](../../../sm-lab/datasets/README.md)；GS 继续使用原有隔离依赖。

## 后处理与回放

```bash
just daaam tum-post fr1_room           # 原生地点特征 → 区域聚类
just daaam tum-summarize fr1_room      # 另需模型服务凭据
just daaam coda-list
just daaam coda-post <运行目录名>
just replay                           # http://127.0.0.1:8765
just replay-tools server --no-agent   # 关闭问答的查看服务
just daaam rerun-play <replay.rrd>
just daaam rerun-web <dsg.json>
```

replay 可选择或输入已有结果绝对路径。问答/摘要需相应模型与凭据；普通查看无需启动问答。缺少原生地点特征的历史结果不能仅靠后处理补出语义地点。

## 开发环境

复用本机已有镜像、`daaam_daaam_workspace` 卷和模型缓存。容器源码在 `/opt/daaam_ws/src`；Compose 同时挂载 sm-lab/datasets 与 sm-lab/viewers/replay。更换机器需同步调整 `.env`、挂载清单和文件链接；全新安装与部署包尚未重新验收。

```bash
cd ~/jellymap_dev/third_party/src/daaam_ws/src/daaam
just shell                  # 新容器终端（原 just run）
just exec                   # 进入运行中的容器
just build                  # 增量编译
docker compose config --quiet
```

代理及凭据只在本机 `.env` 配置。前台任务 Ctrl+C 收尾，容器终端 `exit`；保留共享依赖卷。


成功判据：退出状态正常、预期帧得到处理、保存产物可解析，replay 能读取结果。几何冒烟不等于 DAM 语义质量验收；仿真深度/位姿与真实单目估计须区分。


## 上游与许可

[MIT-SPARK/DAAAM](https://github.com/MIT-SPARK/DAAAM) · [论文](https://arxiv.org/abs/2512.00565) · [DAAAM-ROS](https://github.com/MIT-SPARK/DAAAM-ROS)

许可证见 [LICENSE](LICENSE)，依赖声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## Paper

If you use this code in your work, please cite the following paper:

Nicolas Gorlo, Lukas Schmid, and Luca Carlone, "**Describe Anything, Anywhere, at Any Moment**". *arXiv preprint arXiv:2512.00565*, 2025.

```bibtex
@inproceedings{Gorlo26cvpr-DAAAM,
      title={Describe anything anywhere at any moment},
      author={Gorlo, Nicolas and Schmid, Lukas and Carlone, Luca},
      booktitle={Proceedings of the IEEE/CVF Conference on Computer Vision and Pattern Recognition},
      pages={35002--35013},
      year={2026}
}
```

> This work was supported by the ARL DCIST program and the ONR RAPID program.

测试脚本保留在各自工作空间，justfile 仅提供运行入口。参考网格的可选 Open3D 依赖见 sm-lab/datasets/requirements-reference-mesh.txt。

## VizTracer
Python 工具安装在持久工作区卷的 /opt/daaam_ws/.venv；--rm 不会删除它，无需重建镜像。
在 jellymap_dev 根目录运行：
```bash
just daaam profile-install
just daaam profile ./data/processed/tum/tum_fr1_desk 30 --depth-ub 4.0
# 等价于 just daaam run <Dataset> 30 --viztracer
just daaam profile-view ./runs/daaam/<本次运行>/profile/<trace文件>.json
```
浏览器打开 http://localhost:9001；Ctrl+C 关闭查看器。Windows/WSL 宿主机无需安装 VizTracer。
TUM 旧入口也可使用 VIZTRACER=1 just daaam tum-run 30 fr1_desk。
默认记录建图主进程及其线程中 DAAAM Python 源码与 run_pipeline.py 的函数调用（不含导入初始化和独立 DAM/assignment 子进程），报告保存在每次运行的 profile/。
这是 Python 调用时间线，不是 CUDA kernel 性能分析；长运行可能耗尽默认事件缓冲区，需检查 VizTracer 的溢出警告。
普通 run 默认关闭记录。ROS/CODa 和 Habitat 实时入口尚未接入记录；GS 或 Habitat 导出的统一 RGB-D 可直接使用 profile。
新环境安装脚本 pip-deps 会安装同一版本；镜像文件本身没有因此更新。

### Perfetto 与 Pyinstrument
```bash
# 原有 JSON 使用本地 Perfetto 界面，不需要重跑算法。
just daaam profile-view <trace.json> 9001
# 独立运行采样分析；等价于 run <Dataset> 30 --pyinstrument。
just daaam profile-sample ./data/processed/tum/tum_fr1_desk 30 --depth-ub 4.0
# 新运行 profile/ 下有 pyinstrument_*.html/.txt/.json；打开 HTML，而非采样 JSON。
just daaam profile-view <pyinstrument报告.html> 9002
```
JSON trace 的 profile-view 使用 VizTracer 自带 Perfetto；旧 VizTracer HTML 仍是旧式查看器。
Pyinstrument HTML 是交互式调用树/时间线，TXT 为文本摘要，JSON 为采样数据（不是 Perfetto trace）。
Pyinstrument 只采样启动它的主线程，包含等待时间；独立 Worker、其他线程和 CUDA kernel 不在覆盖范围。
两个采集器互斥运行；都在 os._exit 前显式保存。现有 trace 不会因更换查看器增加 Worker 数据。
打开浏览器中的 localhost 地址即可，无需在 Windows 或 WSL 宿主机再安装包。
