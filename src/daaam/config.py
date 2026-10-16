# ════════════════════════════════════════════════════════════════
# 【本文件在 DAAAM 系统中的角色】(中文导读)
#
# 这是整个管道的配置中心：用 dataclass 把配置组织成「每个服务一个类」
# 的层级结构，PipelineConfig 是根节点，聚合所有子配置：
#
#   PipelineConfig
#   ├── segmentation : SegmentationConfig    分割服务（SAM / FastSAM / SAM2）
#   ├── tracking     : TrackingConfig        跟踪服务（BotSort）
#   ├── grounding    : GroundingConfig       语义描述服务（DAM / 多模态 LLM）
#   ├── workers      : WorkerConfig          worker 进程（数量、类型、专属配置）
#   │   ├── assignment_config    : AssignmentWorkerConfig    帧分配 worker
#   │   └── dam_grounding_config : DAMGroundingWorkerConfig  语义描述 worker
#   ├── depth        : DepthConfig           深度有效范围
#   ├── dataset      : DatasetConfig         数据集加载
#   ├── scene_graph  : SceneGraphConfig      场景图服务
#   ├── hydra        : HydraConfig           Hydra 后端集成
#   └── ...                                  顶层字段（输出目录、语义配置等）
#
# 两种使用方式：
#   1. PipelineConfig()            —— 全部使用代码内默认值
#   2. PipelineConfig.from_yaml()  —— 从 YAML 加载，缺失的字段回退到默认值
#
# YAML 文件的层级结构与上面的 dataclass 层级一一对应，例如：
#   workers:
#     num_grounding_workers: 8
#     assignment_config:
#       min_obs_per_track: 10
#
# 运行时流程：run_pipeline.py 加载本配置 → HydraPipelineRunner 读取 →
# PipelineOrchestrator 按配置实例化各服务与 worker。
# ════════════════════════════════════════════════════════════════

from dataclasses import dataclass, field  # dataclass：自动生成 __init__ 等方法的轻量配置类；field：声明默认工厂
from pathlib import Path  # 跨平台的路径处理
from typing import Optional, Dict, Any, Tuple  # 类型标注
import yaml  # YAML 配置文件的读写


@dataclass
class SegmentationConfig:
	"""分割服务配置：控制 SAM / FastSAM / SAM2 模型的加载与推理。"""
	# @dataclass 装饰器自动生成 __init__ 等方法；类中声明的默认值就是「代码内默认值」，
	# from_yaml 加载时 YAML 中缺失的字段会回退到这些默认值。

	model_name: str = "fastsam/FastSAM-s.pt"  # 分割模型权重文件路径（默认 FastSAM 小模型）
	model_config_path: Optional[str] = "fastsam/fastsam_config.yaml"  # 模型结构配置文件路径（与权重配套）
	device: Optional[str] = None  # 推理设备；None = 自动选择（优先 GPU）
	min_mask_region_area: int = 300  # 最小 mask 面积（像素）：过滤过小的分割区域，排除噪声
	max_mask_region_area: int = 640 * 480  # 最大 mask 面积（像素）：过滤过大的分割区域（如整幅背景）
	polygon_epsilon_factor: float = 0.001  # mask 多边形近似系数（0=不近似，0.001-0.01 为典型值）
	imgsz: Optional[Tuple[int, int]] = None  # 推理时的目标图像尺寸 (高, 宽)；None = 自动


@dataclass
class TrackingConfig:
	"""跟踪服务配置：控制 BotSort 多目标跟踪。"""
	device: Optional[str] = None  # 推理设备；None = 自动选择
	track_buffer: int = 30  # 轨迹存活期（帧）：目标最后一次被检测到后，轨迹再保留这么多帧
	enable_temporal_history: bool = True  # 是否启用时序观测历史跟踪
	reid_weights: str = "checkpoints/reid_weights/clip_general.engine"  # ReID 模型权重路径（支持 .pt / .onnx / .engine）
	with_reid: bool = True  # 是否启用 ReID 特征做轨迹关联（遮挡后重识别同一目标的关键）
	reid_half: bool = False  # ReID 推理是否用 FP16（CLIP 模型要求 FP32，故默认关闭）


@dataclass
class GroundingConfig:
	"""语义描述服务配置：控制多模态模型对分割目标生成语义描述。"""
	agent_model_name: str = "gpt-4.1"  # 多模态 LLM 模型名（用于回答关于场景的自然语言查询）
	group_prompt_file: str = "config/prompt_templates/grounded_sam_prompt.txt"  # 提示词模板文件
	query_interval_frames: int = 60  # 语义描述的查询间隔：每 60 帧查询一次而非每帧（模型推理昂贵，间隔查询是实时性的关键）
	sentence_embedding_model: str = "sentence-transformers/sentence-t5-xl"  # 文本嵌入模型（把标签/描述编码成向量做语义对齐）
	enable_perframe_clip_features: bool = True  # 是否逐帧提取 CLIP 视觉特征（用于与文本描述的跨模态匹配）
	clip_feature_interval_frames: int = 5  # CLIP 特征提取间隔：每 5 帧提取一次
	perframe_clip_model_name: str = "ViT-B-16"  # 逐帧 CLIP 特征用的模型
	perframe_clip_model_dataset: str = "openai"  # CLIP 模型的训练数据集来源（openai 对应官方权重）
	cuda_device: Optional[str] = None  # 固定 GPU 编号；null = 继承环境变量。设 "0"、"1" 等可把语义描述 worker 钉在指定 GPU 上


@dataclass
class AssignmentWorkerConfig:
	"""帧分配 worker 配置：从已跟踪的帧序列中选出最少的帧来覆盖所有目标（论文核心贡献）。"""
	# common parameters
	# ---- 通用参数 ----
	min_obs_per_track: int = 6  # 每个轨迹最少需要的观测次数（观测不足的轨迹不参与分配）
	# SOM-specific parameters (for min_frames worker)
	# ---- min_frames worker 专属参数 ----
	N_masks_per_batch: int = 64  # 每个语义描述批次包含的 mask 数量上限
	min_frame_margin_slack: int = 1  # 在最小帧数基础上额外增加的松弛帧数

	min_mask_region_area : int = None  # 最小 mask 面积（像素）；None = 沿用全局分割配置的对应值
	max_mask_region_area : int = None  # 最大 mask 面积（像素）；None = 沿用全局分割配置的对应值
	position_score_weight: float = 0.5  # 优化目标中位置分数的权重
	size_score_weight: float = 0.5  # 优化目标中尺寸分数的权重


@dataclass
class DAMGroundingWorkerConfig:
	"""DAM（Describe Anything Model）语义描述 worker 配置。"""
	dam_model_path: str = "nvidia/DAM-3B"  # DAM 模型权重路径（NVIDIA 的 Describe Anything Model，3B 参数）
	dam_conv_mode: str = "v1"  # DAM 的对话（conv）模板版本
	dam_prompt_mode: str = "focal_prompt"  # DAM 的提示模式（focal_prompt：聚焦描述指定目标）
	multi_image_min_n_masks: Optional[int] = None  # 多图批次的最少 mask 数
	sentence_embedding_model_name: Optional[str] = None  # 文本嵌入模型；None = 使用全局 grounding 配置中的模型
	compute_full_image_description: bool = False  # 是否额外生成整幅图像的全局描述
	save_grounding_images: bool = False  # 是否保存语义描述过程的可视化图（调试用）
	save_plain_grounding_images: bool = False  # 是否保存不带标注叠加的原始描述图（调试用）
	save_object_images: bool = False  # 是否保存单个目标裁剪图（调试用）
	enable_selectframe_clip_features: bool = False  # 是否对「被选中帧」提取 CLIP 特征
	selectframe_clip_model_name: str = "ViT-L-14"  # 被选中帧 CLIP 特征用的模型
	selectframe_clip_model_dataset: str = "openai"  # CLIP 模型的训练数据集来源
	selectframe_clip_backend: str = "openclip"  # CLIP 后端实现："openclip" 或 "pe"（Perception Encoder）


@dataclass
class WorkerConfig:
	"""worker 进程总配置：数量、类型选择，以及两个嵌套的专属配置。"""
	num_assignment_workers: int = 1  # 帧分配 worker 的进程数（并行度）
	num_grounding_workers: int = 4  # 语义描述 worker 的进程数（并行度；DAM 推理较慢，通常开多个）
	assignment_worker: str = "min_frames_max_size"  # 帧分配策略：可选 "min_frames" / "min_frames_max_size"
	grounding_worker: str = "dam_multi_image"  # 语义描述后端：可选 "dam_multi_image"

	# Worker-specific configurations
	# ---- worker 专属配置 ----
	# 注意必须用 field(default_factory=...)：dataclass 的可变默认值如果直接写
	# =AssignmentWorkerConfig()，所有 WorkerConfig 实例会共享同一个对象，互相污染
	assignment_config: AssignmentWorkerConfig = field(default_factory=AssignmentWorkerConfig)
	dam_grounding_config: DAMGroundingWorkerConfig = field(default_factory=DAMGroundingWorkerConfig)


@dataclass
class DepthConfig:
	"""深度范围过滤配置：把深度不可靠的点排除在场景图构建之外。"""
	depth_lb: float = 0.25  # 深度下限（米）：过滤比它更近的点（近距离伪影）
	depth_ub: float = 5.0  # 深度上限（米）：过滤比它更远的点（远距离深度估计不可靠）


@dataclass
class DatasetConfig:
	"""数据集加载配置。"""
	dataset_type: Optional[str] = None  # 数据集类型："image_sequence" / "rosbag" / "coda" / "hm3d" 等；None = 自动检测
	data_path: Optional[str] = None  # 数据集路径
	depth_scale: float = 1.0  # 缩放系数：把原始深度值换算成米
	fps: float = 30.0  # 数据集录制帧率
	compute_velocities: bool = True  # 是否从位姿序列计算帧间速度
	velocity_window: int = 10  # 速度计算使用的滑动窗口大小
	velocity_alpha: float = 0.4  # 速度平滑用的 EMA（指数滑动平均）系数
	max_frames: Optional[int] = None  # 最多处理的帧数（None = 全部）
	# CODa-specific fields
	# ---- CODA 自动驾驶数据集专属字段 ----
	sequence: str = "0"  # 序列 / 场景 ID（CODA、HM3D 使用）
	camera_id: str = "cam0"  # 使用的相机（CODA："cam0" / "cam1"）
	depth_source: str = "3d_raw_estimated"  # 深度来源（CODA："3d_raw_estimated" / "3d_raw" / "none"）


@dataclass
class SceneGraphConfig:
	"""场景图服务配置。"""
	defer_dsg_processing: bool = False  # 是否把 DSG（动态场景图）处理推迟到管道结束时再做（换性能）
	enable_background_objects: bool = True  # 是否跟踪所有目标，包括被 Hydra 过滤掉的那些
	hydra_dsg_path: str = ""  # Hydra 输出目录；结束时从 <path>/backend/dsg.json 加载场景图


@dataclass
class HydraConfig:
	"""Hydra 后端集成配置（Hydra 是 MIT-SPARK 的 3D 动态场景图构建框架）。"""
	enable_hydra: bool = False  # 是否启用 Hydra 管道（ROS 版可动态开关；离线脚本恒为 True）
	hydra_config_path: str = "daaam/daaam_ros/config/hydra_config/clio_dataset_khronos.yaml"  # Hydra 自己的配置 YAML 路径
	labelspace_path: Optional[str] = "config/labels_pseudo.yaml"  # 语义标签空间定义 YAML（与 Hydra 层对齐）
	labelspace_colors: Optional[str] = "config/labels_pseudo.csv"  # 标签配色 CSV（可视化用）
	zmq_url: Optional[str] = "tcp://127.0.0.1:8001"  # 通过 ZMQ 实时发布 DSG 的地址（外部可视化工具订阅）
	glog_level: int = 0  # Hydra 的 glog 日志级别
	verbosity: int = 0  # 管道自身日志的详细程度
	skip_invalid_depth: bool = True  # 没有测到深度(0)的像素让 Hydra 跳过；否则 Hydra 把它们当成整条射线都是空闲空间，空洞多的深度图（Kinect）会把别的帧测到的表面削掉（见 hydra/integration.py）


@dataclass
class PipelineConfig:
	"""管道顶层配置：聚合所有子配置（YAML 的根节点）。"""
	# 所有子配置都用 field(default_factory=...) 创建独立实例，
	# 避免多个 PipelineConfig 共享同一个可变子配置对象
	segmentation: SegmentationConfig = field(default_factory=SegmentationConfig)
	tracking: TrackingConfig = field(default_factory=TrackingConfig)
	grounding: GroundingConfig = field(default_factory=GroundingConfig)
	workers: WorkerConfig = field(default_factory=WorkerConfig)
	depth: DepthConfig = field(default_factory=DepthConfig)
	dataset: DatasetConfig = field(default_factory=DatasetConfig)
	scene_graph: SceneGraphConfig = field(default_factory=SceneGraphConfig)
	hydra: HydraConfig = field(default_factory=HydraConfig)
	# ---- 顶层字段 ----
	semantic_config_path: str = "config/labels_pseudo.yaml"  # 语义配置文件（标签定义）
	labelspace_colors_path: str = "config/labels_pseudo.csv"  # 标签配色 CSV
	hierarchical_optimization: bool = True  # 是否启用层级式优化
	output_dir: str = "output"  # 结果输出目录
	log_dir: Optional[str] = None  # 运行时统一日志目录

	@classmethod
	def from_yaml(cls, path: str, validate_files: bool = True) -> 'PipelineConfig':
		"""从 YAML 文件加载并构造配置（工厂方法）。"""
		# （注意：validate_files 参数当前未在函数体内使用）
		config_path = Path(path)
		if not config_path.exists():
			# 配置文件不存在直接报错，而不是静默使用默认值（避免误以为配置已生效）
			raise FileNotFoundError(f"Config file not found: {path}")

		with open(config_path, 'r') as f:
			data = yaml.safe_load(f)  # safe_load：只解析基本类型，避免任意代码执行

		# nested configs
		# 逐个构造子配置：data.get(section, {}) 在 YAML 缺该 section 时传空 dict，
		# 于是该子配置全部字段回退到类中声明的默认值
		segmentation_config = SegmentationConfig(**data.get('segmentation', {}))
		tracking_config = TrackingConfig(**data.get('tracking', {}))
		grounding_config = GroundingConfig(**data.get('grounding', {}))
		depth_config = DepthConfig(**data.get('depth', {}))
		dataset_config = DatasetConfig(**data.get('dataset', {}))
		scene_graph_config = SceneGraphConfig(**data.get('scene_graph', {}))
		hydra_config = HydraConfig(**data.get('hydra', {}))

		# Handle workers config with nested worker-specific configs
		# workers 段的特殊处理：它内部还有两个嵌套的专属配置
		workers_data = data.get('workers', {})
		assignment_config = AssignmentWorkerConfig(**workers_data.get('assignment_config', {}))
		dam_grounding_config = DAMGroundingWorkerConfig(**workers_data.get('dam_grounding_config', {}))

		# Create workers config with nested configurations
		# 把 workers 段里除两个嵌套配置外的其余字段（如 num_grounding_workers）
		# 平铺出来，与嵌套配置一起传给 WorkerConfig
		workers_base = {k: v for k, v in workers_data.items()
					   if k not in ['assignment_config', 'dam_grounding_config']}
		workers_config = WorkerConfig(
			assignment_config=assignment_config,
			dam_grounding_config=dam_grounding_config,
			**workers_base
		)

		# Extract top-level config
		# 除去所有已知 section，剩下的顶层键（如 output_dir、semantic_config_path）
		# 直接展开传给 PipelineConfig 构造器
		top_level = {k: v for k, v in data.items()
					if k not in ['segmentation', 'tracking', 'grounding', 'workers', 'depth', 'dataset', 'scene_graph', 'hydra']}

		config = cls(
			segmentation=segmentation_config,
			tracking=tracking_config,
			grounding=grounding_config,
			workers=workers_config,
			depth=depth_config,
			dataset=dataset_config,
			scene_graph=scene_graph_config,
			hydra=hydra_config,
			**top_level
		)

		return config

	def to_dict(self) -> Dict[str, Any]:
		"""把嵌套配置转成纯 dict，用于跨进程传给 worker 子进程。"""
		# 原因：worker 是独立子进程，对象无法直接共享，
		# 只能传递可序列化的纯数据（dict），子进程再据此重建配置
		# Create workers dict with nested configurations
		# 先拷贝 WorkerConfig 的 __dict__，再把两个嵌套配置换成各自的 __dict__（一层层打平）
		workers_dict = self.workers.__dict__.copy()
		workers_dict['assignment_config'] = self.workers.assignment_config.__dict__
		workers_dict['dam_grounding_config'] = self.workers.dam_grounding_config.__dict__

		# （观察：输出中未包含 scene_graph 和 log_dir；
		#   log_dir 在 get_worker_config 中单独补充，scene_graph 则仅主进程使用）
		return {
			'segmentation': self.segmentation.__dict__,
			'tracking': self.tracking.__dict__,
			'grounding': self.grounding.__dict__,
			'workers': workers_dict,
			'depth': self.depth.__dict__,
			'dataset': self.dataset.__dict__,
			'hydra': self.hydra.__dict__,
			'semantic_config_path': self.semantic_config_path,
			'labelspace_colors_path': self.labelspace_colors_path,
			'hierarchical_optimization': self.hierarchical_optimization,
			'output_dir': self.output_dir,
		}

	def get_worker_config(self, worker_type: str) -> Dict[str, Any]:
		"""获取指定 worker 类型所需的完整配置 = 基础配置 + 该 worker 的专属配置。"""
		base_config = self.to_dict()  # 先取所有 worker 共享的基础配置

		# Add runtime fields that aren't in to_dict()
		# 补充运行时字段（to_dict 里没有的）
		if self.log_dir:
			base_config['log_dir'] = self.log_dir

		if worker_type in ["min_frames", "min_frames_max_size"]:
			# Assignment worker config
			# 帧分配 worker：在基础配置上平铺合并 assignment_config 的字段
			config = base_config.copy()
			config.update(self.workers.assignment_config.__dict__)
			return config
		elif worker_type == "dam_multi_image":
			# DAM grounding worker config
			# 语义描述 worker：在基础配置上平铺合并 dam_grounding_config 的字段
			config = base_config.copy()
			config.update(self.workers.dam_grounding_config.__dict__)
			return config
		else:
			# 未知 worker 类型：只给基础配置
			return base_config

	def to_yaml(self, path: str):
		"""把配置保存为 YAML 文件（常用于把最终生效的配置落盘，便于复现实验）。"""
		output_path = Path(path)
		output_path.parent.mkdir(parents=True, exist_ok=True)  # 自动创建父目录

		def convert_tuples_to_lists(obj):
			"""递归把 tuple 转成 list，保证 YAML 可移植序列化。"""
			# 原因：yaml.dump 会把 tuple 写成 !!python/tuple 这样的 Python 专有标签，
			# 其他程序 / 后续 yaml.safe_load 都无法解析，先转成 list 再写
			if isinstance(obj, dict):
				return {k: convert_tuples_to_lists(v) for k, v in obj.items()}
			elif isinstance(obj, (list, tuple)):
				return [convert_tuples_to_lists(item) for item in obj]
			return obj

		data = convert_tuples_to_lists(self.to_dict())  # 序列化数据来自 to_dict()
		with open(output_path, 'w') as f:
			yaml.dump(data, f, default_flow_style=False)  # default_flow_style=False：使用块状风格，可读性好
