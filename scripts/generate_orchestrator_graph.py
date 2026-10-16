#!/usr/bin/env python3
"""Generate an offline call graph for PipelineOrchestrator.process_frame."""

from __future__ import annotations

import ast
import argparse
import builtins
import copy
import json
import os
from dataclasses import dataclass
from pathlib import Path
import tempfile
from typing import Any, Iterable


DEFAULT_ENTRY_POINT = "daaam.pipeline.orchestrator.PipelineOrchestrator.process_frame"
DATA_MARKER = "/*__DAAAM_GRAPH_DATA__*/"


def _step(
	title: str,
	description: str,
	*calls: str,
	kind: str = "main",
	condition: str = "",
	result: str = "",
) -> dict[str, object]:
	return {
		"title": title,
		"description": description,
		"calls": list(calls),
		"kind": kind,
		"condition": condition,
		"result": result,
		"source_excerpt": "",
	}


def _guide(
	title: str,
	purpose: str,
	inputs: list[tuple[str, str]],
	outputs: list[tuple[str, str]],
	state: list[str],
	steps: list[dict[str, object]],
) -> dict[str, object]:
	return {
		"title": title,
		"purpose": purpose,
		"inputs": [{"name": name, "description": text} for name, text in inputs],
		"outputs": [{"name": name, "description": text} for name, text in outputs],
		"state": state,
		"steps": steps,
	}


FUNCTION_GUIDES: dict[str, dict[str, object]] = {
	DEFAULT_ENTRY_POINT: _guide(
		"单帧处理总控",
		"把单帧 RGB-D 数据依次变成可跨帧关联、可投影到三维世界、可供语义任务使用的对象观测，并生成 Hydra 可消费的语义标签图。",
		[("frame", "当前 RGB 图像，以及可选的深度、相机内参、位姿、时间戳和速度。")],
		[
			("label_image", "每个像素对应的稳定 semantic_id，供后端语义融合使用。"),
			("color_image", "按 semantic_id 着色的可视化图像。"),
		],
		[
			"递增 frame_count，并把全局帧号写入 frame.frame_id。",
			"更新对象标签、三维位置、时序历史、帧快照和待处理集合。",
			"语义分配通过队列异步提交；当前帧不会等待 DAM/grounding 完成。",
		],
		[
			_step("登记当前帧", "递增全局帧计数，并把这个编号写回 Frame，后续历史、快照和异步任务都用它对齐。", result="frame.frame_id: int；全局 frame_count 同步递增。"),
			_step("实例分割", "从 RGB 图像中检测候选对象，得到检测框和与检测顺序对应的二值 mask。", "daaam.segmentation.services.SegmentationService.segment", result="detections: N×6 数组 [x1, y1, x2, y2, confidence, class]；masks: N 个 H×W 二值区域。"),
			_step("跨帧跟踪", "把当前检测与历史轨迹关联，输出稳定 track_id；mask_idx 保留当前轨迹对应的分割 mask。", "daaam.tracking.services.TrackingService.update", result="tracks: M×8 数组 [x1, y1, x2, y2, track_id, confidence, class, mask_idx]。"),
			_step("构建可融合的轨迹观测", "把跟踪器数组转换为 Track，对深度进行过滤，并把二维 mask 中心反投影到相机/世界坐标，同时维护 semantic_id 和时序记录。", "daaam.pipeline.orchestrator.PipelineOrchestrator._process_tracks", result="processed_tracks: List[Track]；track_masks: Dict[track_id, H×W bool]，并可能附带相机/世界三维坐标。"),
			_step("提取可选的视觉语义特征", "对深度有效的目标裁剪图像并批量提取 CLIP 特征，作为后续语义匹配的视觉证据。", "daaam.pipeline.orchestrator.PipelineOrchestrator.compute_clip_features", kind="optional", condition="启用 per-frame CLIP，且到达配置的帧间隔", result="clip_features_map[semantic_id] 追加 D 维视觉向量；D 由所用 CLIP 模型决定。"),
			_step("保存近期帧历史", "保存原始 Frame 与轻量 Track 摘要，让异步 assignment worker 能在一段时间窗口内挑选有代表性的观测。", "daaam.pipeline.orchestrator.PipelineOrchestrator._add_frame_to_history", result="recent_frames_buffer 与 simplified_tracks_cache 追加本帧，并受有界窗口限制。"),
			_step("按周期触发语义分配", "冻结当前历史和修正信息，生成 AssignmentTask 并放入多进程队列；这里只提交任务，不等待语义描述返回。", "daaam.pipeline.orchestrator.PipelineOrchestrator._trigger_query_assignment", kind="async", condition="达到 query_interval_frames，或 frame_count == 60", result="AssignmentTask 被序列化后非阻塞写入 multiprocessing.Queue；当前帧继续向下执行。"),
			_step("生成语义输出图", "把 track_id mask 映射为稳定 semantic_id 与配置颜色，输出给 Hydra/可视化的两张逐像素图。", "daaam.pipeline.orchestrator.PipelineOrchestrator._create_output_images", result="label_image: H×W uint16 semantic_id；color_image: H×W×3 uint8 RGB。"),
			_step("维护时序状态", "周期性关闭长时间未出现的轨迹、清理已消费或过旧快照，并增量同步尚未发送的三维位置。", "daaam.pipeline.orchestrator.PipelineOrchestrator._check_track_closures", "daaam.pipeline.orchestrator.PipelineOrchestrator._cleanup_stale_snapshots", "daaam.pipeline.orchestrator.PipelineOrchestrator._send_incremental_position_updates", kind="maintenance", condition="每 10 帧检查关闭；每 100 帧清理并同步位置", result="closed_tracks、frame_snapshots、pending_temporal_updates 与对象位置同步状态被更新。"),
		],
	),
	"daaam.pipeline.orchestrator.PipelineOrchestrator._process_tracks": _guide(
		"轨迹观测构建",
		"把跟踪器的数值数组转换成 DAAAM 的 Track，并为每个目标补齐 mask、深度有效性、semantic_id、三维位置和时间历史。",
		[("frame / tracks / masks", "当前帧、跟踪器输出以及实例分割 mask。")],
		[("processed_tracks / track_masks", "结构化 Track 列表，以及 track_id 到 mask 的映射。")],
		["可能创建新的 track_id → semantic_id 映射。", "追加三维位置、深度有效性和时序观测历史。", "深度无效时可能写入 unknown 修正。"],
		[
			_step("匹配轨迹与 mask", "读取 track_id 和 mask_idx；越界的 mask 不参与后续处理。"),
			_step("验证深度", "检查 mask 内有效深度比例与近期深度历史，判断该目标能否可靠参与三维融合。", "daaam.pipeline.orchestrator.PipelineOrchestrator._validate_track_depth", condition="Frame 包含深度图"),
			_step("创建 Track", "把稠密 mask 转成多边形轮廓，保存包围框、面积、深度状态和运动信息。", "daaam.tracking.models.Track.from_mask", "daaam.utils.vision.bounding_box_from_mask"),
			_step("分配稳定语义编号", "第一次看到 track_id 时分配新的 semantic_id；后续帧继续复用同一映射。"),
			_step("计算三维位置", "在 mask 内选代表像素，用深度和内参反投影到相机坐标；若有位姿，再变换到世界坐标。", "daaam.utils.geometry.compute_mask_centroid", "daaam.utils.geometry.unproject_pixel_to_3d", "daaam.utils.geometry.pose_to_matrix", kind="optional", condition="深度有效、存在相机内参且 median_depth > 0"),
			_step("维护时序与深度历史", "记录每个 track/semantic_id 在哪些帧出现，并只保留最近的深度有效性窗口。"),
			_step("处理 unknown 状态", "深度持续无效时创建 unknown 修正；从无效恢复为有效时清除自动修正。", "daaam.pipeline.orchestrator.PipelineOrchestrator._create_automatic_unknown_correction", "daaam.pipeline.orchestrator.PipelineOrchestrator._clear_unknown_correction", kind="optional"),
		],
	),
	"daaam.pipeline.orchestrator.PipelineOrchestrator._validate_track_depth": _guide(
		"目标深度可信度检查",
		"用 mask 内有效像素比例、近期深度中值和 mask 面积判断当前轨迹的深度是否足够可靠。",
		[("track_id / mask / depth_image", "目标编号、二值区域和米制深度图。")],
		[("median_depth / is_valid", "当前中值深度及是否允许三维投影。")],
		["更新 track_depth_history，并截断到最近 10 次。"],
		[
			_step("检查有效像素比例", "若 mask 内非零深度少于 25%，立即标记为无效。"),
			_step("计算 mask 中值深度", "忽略无效值，得到对离群点更稳健的代表深度。", "daaam.pipeline.orchestrator.PipelineOrchestrator._calculate_mask_median_depth"),
			_step("平滑近期深度", "对同一 track 最近最多 10 个中值再次取中值，减少单帧抖动。"),
			_step("应用有效性规则", "深度位于配置上下界，或区域足够大时判为可用。"),
		],
	),
	"daaam.pipeline.orchestrator.PipelineOrchestrator._calculate_mask_median_depth": _guide(
		"mask 中值深度",
		"从目标 mask 覆盖的深度像素中剔除零值和非有限值，再计算中值。",
		[("mask / depth_image", "目标区域与深度图。")],
		[("depth or None", "有效中值深度；没有有效像素或计算失败时为 None。")],
		[],
		[
			_step("筛选并求中值", "调用向量化实现提取大于零的有限深度并计算中值。", "daaam.utils.vision.fast_median_depth"),
			_step("规范失败结果", "负值或异常统一转换成 None，让上层按深度无效处理。"),
		],
	),
	"daaam.pipeline.orchestrator.PipelineOrchestrator.compute_clip_features": _guide(
		"批量提取目标视觉特征",
		"把有效目标从 RGB 图中裁剪出来，批量编码成 CLIP 特征并按 semantic_id 保存。",
		[("frame / tracks", "当前 RGB 帧与结构化目标轨迹。")],
		[("无直接返回值", "特征写入 clip_features_map。")],
		["为每个 semantic_id 追加一条视觉特征。"],
		[
			_step("筛选可编码目标", "跳过深度无效、无 bbox、裁剪小于 10 像素或 semantic_id 为 0 的轨迹。"),
			_step("裁剪目标图像", "按 bbox 从 RGB 帧取出目标区域，并保留 Track 与 semantic_id 对齐关系。"),
			_step("批量编码", "一次性调用 CLIPHandler，避免逐目标重复推理。", kind="optional", condition="至少存在一个有效裁剪"),
			_step("按语义对象归档", "把批量输出重新对应到 semantic_id，并追加到该对象的多帧特征列表。"),
		],
	),
	"daaam.pipeline.orchestrator.PipelineOrchestrator._add_frame_to_history": _guide(
		"保存 assignment 时间窗口",
		"保留近期原始帧和轻量轨迹摘要，供后台 worker 从多帧中选择适合 grounding 的观测。",
		[("frame", "已经附带 processed_tracks 的当前帧。")],
		[("无直接返回值", "历史写入两个有界缓存。")],
		["追加 recent_frames_buffer 和 simplified_tracks_cache。"],
		[
			_step("压缩轨迹", "仅保留有 bbox 的轨迹，并转换成不含稠密 mask 的 SimplifiedTrack。"),
			_step("原子追加历史", "在状态锁内同时追加完整 Frame 和对应的轻量轨迹列表。"),
		],
	),
	"daaam.pipeline.orchestrator.PipelineOrchestrator._trigger_query_assignment": _guide(
		"提交异步语义分配任务",
		"把近期多帧轨迹、已有语义修正和帧号映射封装成 AssignmentTask，非阻塞地交给独立 assignment worker。",
		[("内部历史缓存", "近期 Frame、SimplifiedTrack、对象编号和已提示目标。")],
		[("无直接返回值", "序列化任务进入 assignment_task_queue。")],
		["保存 frame_snapshots；预占 pending_track_ids；队列满时回滚本轮预占。"],
		[
			_step("冻结一致的历史快照", "在锁内复制轨迹历史、对象映射和 prompted 集合，同时建立局部帧号到全局帧号的映射。"),
			_step("精简已有修正", "只复制 assignment 决策需要的标签、置信度和任务相关性，避免传输大向量。"),
			_step("构造 AssignmentTask", "组合轨迹窗口、图像尺寸、对象标签、修正和帧号映射。"),
			_step("筛选本轮候选", "跳过已经提示、深度无效、观测不足或已有确定标签的轨迹。"),
			_step("序列化任务", "把 numpy/Pydantic 字段转换成多进程队列可传输的 JSON。", "daaam.assignment.schemas.assignment_task_to_json"),
			_step("非阻塞入队", "使用 put_nowait 提交；队列已满则跳过本轮并回滚 pending 状态。", kind="async"),
		],
	),
	"daaam.pipeline.orchestrator.PipelineOrchestrator._create_output_images": _guide(
		"生成 Hydra 语义图",
		"把每条轨迹的 mask 合并成逐像素 track_id，再查表转换为 semantic_id 和显示颜色。",
		[("tracks / track_masks / image_shape", "当前有效轨迹、对应 mask 与输出分辨率。")],
		[("label_image / color_image", "uint16 语义编号图与 uint8 RGB 颜色图。")],
		[],
		[
			_step("处理空结果", "没有轨迹时直接返回全零标签图和颜色图。"),
			_step("合并轨迹 mask", "把每个有效 mask 区域写成 track_id，并记录需要建立查找表的 ID。"),
			_step("建立语义与颜色查找表", "通过 object_labels 和 scene_graph_service.color_map，把 track_id 映射到稳定语义编号及颜色。"),
			_step("一次性矢量化映射", "用 combined_mask 索引两个查找表，生成最终图像并清零背景。", "daaam.utils.vision.fast_create_output_images"),
		],
	),
	"daaam.pipeline.orchestrator.PipelineOrchestrator._check_track_closures": _guide(
		"关闭长期消失的轨迹",
		"当目标超过跟踪器缓冲期仍未出现时，将其标为关闭并整理最终时序记录。",
		[("track_last_seen", "每条轨迹最后出现的帧号。")],
		[("无直接返回值", "关闭状态和待同步时序数据写入内部字典。")],
		["更新 closed_tracks、semantic_temporal_history 和 pending_temporal_updates。"],
		[
			_step("读取跟踪缓冲长度", "从 TrackingService 获取允许轨迹暂时消失的帧数。", "daaam.tracking.services.TrackingService.get_track_buffer"),
			_step("识别超时轨迹", "当前帧减去 last_seen 大于 track_buffer，且尚未关闭时触发。"),
			_step("固化时序摘要", "去重排序该 semantic_id 的历史，计算首末时间和观测次数，等待后续同步。"),
		],
	),
	"daaam.pipeline.orchestrator.PipelineOrchestrator._cleanup_stale_snapshots": _guide(
		"回收帧快照",
		"删除已经交给 grounding 的快照，并用安全阈值回收异常滞留的旧快照，限制内存增长。",
		[("frame_snapshots / processed ids", "缓存快照、处理完成集合与当前帧号。")],
		[("无直接返回值", "相关快照和 assignment 映射被移除。")],
		["修改 frame_snapshots、frame_snapshot_assignments 和 _processed_snapshot_ids。"],
		[
			_step("找出已消费快照", "选择已经记录为 processed 且仍留在缓存中的帧。"),
			_step("找出异常陈旧快照", "使用 15 倍 query interval 作为宽松安全线，避免 worker 异常时无限堆积。"),
			_step("统一删除", "同时清理快照、assignment 归属和 processed 标记。"),
		],
	),
	"daaam.pipeline.orchestrator.PipelineOrchestrator._send_incremental_position_updates": _guide(
		"增量同步三维位置",
		"只把尚未发送过的对象位置观测交给 SceneGraphService，避免周期任务重复传输历史数据。",
		[("object_3d_positions", "按 semantic_id 保存的全部三维观测。")],
		[("无直接返回值", "新增观测被同步到场景图服务。")],
		["更新 sent_position_frame_ids，记录每个对象已发送的 frame_id。"],
		[
			_step("比较已发送帧号", "按 semantic_id 过滤 frame_id 尚未出现于 sent 集合的观测。"),
			_step("标记本轮增量", "在状态锁内先记录即将发送的 frame_id，保证下个周期不会重复。"),
			_step("提交场景图服务", "仅在确有新位置时调用更新接口。", "daaam.scene_graph.services.SceneGraphService.update_object_positions", kind="async"),
		],
	),
	"daaam.pipeline.orchestrator.PipelineOrchestrator._create_automatic_unknown_correction": _guide(
		"创建自动 unknown 修正",
		"当目标深度不可靠时，为 semantic_id 写入临时 unknown 标签，防止错误几何参与语义融合。",
		[("semantic_id / reason", "对象语义编号与触发原因。")],
		[("无直接返回值", "可能新增一条 ObjectAnnotation。")],
		["通过 SceneGraphService 保存修正。"],
		[
			_step("避免覆盖已有修正", "如果对象已经有人工或模型修正，直接返回。"),
			_step("附加时间历史", "若启用时序记录，填入观察帧、时间范围和次数。"),
			_step("保存 unknown 注释", "构造高置信度 unknown ObjectAnnotation 并交给场景图服务。", "daaam.scene_graph.services.SceneGraphService.store_correction"),
		],
	),
	"daaam.pipeline.orchestrator.PipelineOrchestrator._clear_unknown_correction": _guide(
		"清除自动 unknown 状态",
		"目标从深度无效恢复为有效时，只移除标签确实为 unknown 的自动修正。",
		[("semantic_id", "恢复有效的对象语义编号。")],
		[("无直接返回值", "可能删除一条 unknown 修正。")],
		["修改 SceneGraphService.corrections。"],
		[
			_step("读取当前修正", "在 correction_lock 内查找该 semantic_id。"),
			_step("有条件删除", "仅当 semantic_label.lower() == 'unknown' 时删除，保留确定语义标签。"),
		],
	),
	"daaam.assignment.schemas.assignment_task_to_json": _guide(
		"序列化 assignment 任务",
		"把包含 numpy、Track 摘要和修正对象的 AssignmentTask 转成可跨进程传输的 JSON。",
		[("task", "待提交给 assignment worker 的任务。")],
		[("json string", "不依赖 Python 对象地址的任务载荷。")],
		[],
		[
			_step("转换轨迹字段", "把 bbox、速度等 numpy 数组转换为普通列表。"),
			_step("转换修正与注释", "把 Pydantic 模型和嵌套注释转换为 JSON 可序列化结构。", "daaam.grounding.schemas.annotation_to_json_serializable"),
			_step("编码 JSON", "将规范化字典编码为字符串。"),
		],
	),
	"daaam.grounding.schemas.annotation_to_json_serializable": _guide(
		"规范化语义注释",
		"把 Annotation 模型展开为普通字典，并处理其中不支持直接 JSON 编码的字段。",
		[("annotation", "grounding 或修正阶段产生的注释模型。")],
		[("dictionary", "仅含 JSON 基本类型的注释数据。")],
		[],
		[_step("展开模型", "调用模型导出并把数组等字段转换为列表或标量。")],
	),
	"daaam.segmentation.services.SegmentationService.segment": _guide(
		"实例分割入口",
		"调用配置的分割器，把 RGB 帧转换为检测框和逐实例 mask；失败时返回空结果而不是终止整条流水线。",
		[("frame", "RGB numpy 图像。")],
		[("detections / masks", "N×6 检测数组与 N 个二值 mask。")],
		[],
		[
			_step("检查模型状态", "分割器未初始化属于配置错误，直接抛出异常。"),
			_step("执行分割模型", "实际实现由 UniversalSegmenter/FastSAM 等配置后端提供。"),
			_step("降级为空检测", "模型不支持或单帧推理异常时返回空检测，使后续步骤仍能产生空语义图。"),
		],
	),
	"daaam.tracking.services.TrackingService.update": _guide(
		"跨帧目标关联",
		"把当前检测交给 BoT-SORT，结合运动与可选 ReID 线索延续 track_id。",
		[("detections / frame", "当前检测框与 RGB 图像。")],
		[("tracks", "包含 bbox、track_id、置信度、类别和 mask_idx 的 M×8 数组。")],
		[],
		[
			_step("跳过无检测帧", "detections 为空时直接返回形状固定的空轨迹数组。"),
			_step("更新 BoT-SORT", "根据当前检测与历史状态进行关联；ReID 是否启用由配置决定。"),
			_step("规范输出", "修正空数组维度，把 track_id 从 0 基改为 1 基，并保证返回 M×8。"),
		],
	),
	"daaam.tracking.services.TrackingService.get_track_buffer": _guide(
		"读取轨迹保留窗口",
		"返回跟踪器允许目标暂时消失而不关闭轨迹的帧数。",
		[("内部配置", "TrackingService.track_buffer。")],
		[("track_buffer", "轨迹关闭判断使用的帧数阈值。")],
		[],
		[_step("返回配置值", "不修改跟踪器状态。")],
	),
	"daaam.tracking.models.Track.from_mask": _guide(
		"由 mask 构造 Track",
		"把稠密二值 mask 压缩为多边形轮廓，并计算区域面积，形成可缓存和序列化的 Track。",
		[("id / mask / bbox", "稳定轨迹编号、二值区域与包围框。")],
		[("Track", "包含轮廓、面积、图像尺寸及附加深度/运动字段的对象。")],
		[],
		[
			_step("mask 转多边形", "按 epsilon_factor 简化轮廓，减少长期历史的内存占用。"),
			_step("计算多边形面积", "从轮廓得到 region_area。"),
			_step("创建 Track", "合并几何信息与上层传入的深度、速度等字段。"),
		],
	),
	"daaam.scene_graph.services.SceneGraphService.store_correction": _guide(
		"保存语义修正",
		"区分整帧描述与对象修正；对象标签写入修正表，unknown 标签还会允许以后重新提示。",
		[("correction", "ImageAnnotation 或 ObjectAnnotation。")],
		[("无直接返回值", "注释进入 keyframe_annotations 或 corrections。")],
		["可能触发 re_prompting_callback。"],
		[
			_step("区分注释类型", "ImageAnnotation 按时间戳保存为关键帧描述。"),
			_step("保存对象修正", "普通对象注释交给 add_correction。", "daaam.scene_graph.services.SceneGraphService.add_correction"),
			_step("允许 unknown 重试", "unknown 对象通过回调清理已提示状态，使后续窗口能够重新 grounding。", kind="async"),
		],
	),
	"daaam.scene_graph.services.SceneGraphService.add_correction": _guide(
		"登记对象修正",
		"按 semantic_id 保存最新对象注释；场景图尚未就绪时额外排队，待图建立后应用。",
		[("correction", "对象语义修正。")],
		[("无直接返回值", "修正进入 corrections，并可能进入 corrections_queue。")],
		["修改 corrections 和 corrections_queue。"],
		[
			_step("写入修正表", "在 correction_lock 内以 semantic_id 覆盖该对象的最新修正。"),
			_step("等待场景图就绪", "若 DSG 尚未设置，把修正加入待应用队列。", kind="async", condition="scene_graph_is_set == false"),
		],
	),
	"daaam.scene_graph.services.SceneGraphService.update_object_positions": _guide(
		"合并对象三维位置",
		"按 semantic_id 合并新的三维观测，并用 frame_id 去重。",
		[("positions", "对象编号到新增位置观测列表的映射。")],
		[("无直接返回值", "ObjectPosition 被追加到场景图服务的对象历史。")],
		["修改 SceneGraphService.object_3d_positions。"],
		[
			_step("读取已有帧号", "为每个对象建立当前已保存 frame_id 集合。"),
			_step("转换新增观测", "把普通字典转为 ObjectPosition，并跳过重复帧。"),
			_step("追加对象历史", "在 position_lock 内保存新位置。"),
		],
	),
	"daaam.utils.geometry.compute_mask_centroid": _guide(
		"选择 mask 内代表像素",
		"在目标区域内寻找稳定中心点；默认取像素坐标中值，并保证最终点位于 mask 内。",
		[("mask / method", "二值区域与中心选择策略。")],
		[("(cx, cy) or None", "mask 内代表像素；空 mask 返回 None。")],
		[],
		[
			_step("提取前景像素", "获取 mask 中所有非零坐标。"),
			_step("按策略选择中心", "支持 median、几何质心、最大边界距离和安全质心。"),
			_step("修复落在 mask 外的点", "必要时寻找离候选中心最近的前景像素。", "daaam.utils.geometry.find_nearest_mask_point_fast", kind="optional"),
		],
	),
	"daaam.utils.geometry.find_nearest_mask_point_fast": _guide(
		"寻找最近前景像素",
		"用距离变换在 mask 内找到离指定坐标最近的有效像素，处理凹形或带孔目标。",
		[("mask / cx / cy", "二值区域与候选中心。")],
		[("(x, y) or None", "最近的前景像素。")],
		[],
		[
			_step("裁剪目标坐标", "把候选点限制在图像边界内。"),
			_step("计算距离场", "得到每个像素到候选点的距离。"),
			_step("仅在 mask 内取最小值", "屏蔽背景后选择最近前景像素。"),
		],
	),
	"daaam.utils.geometry.unproject_pixel_to_3d": _guide(
		"像素反投影到相机坐标",
		"使用针孔相机模型，把像素坐标和深度转换为相机坐标系中的三维点。",
		[("u, v, depth, fx, fy, cx, cy", "像素、米制深度与相机内参。")],
		[("[x, y, z]", "相机坐标系三维点。")],
		[],
		[_step("应用针孔模型", "x=(u-cx)z/fx，y=(v-cy)z/fy，z=depth。")],
	),
	"daaam.utils.geometry.pose_to_matrix": _guide(
		"位姿转齐次变换矩阵",
		"把平移加四元数位姿转换为 4×4 world_T_camera 矩阵。",
		[("pose", "[x, y, z, qx, qy, qz, qw]。")],
		[("4×4 matrix", "从相机坐标变换到世界坐标的齐次矩阵。")],
		[],
		[
			_step("写入平移", "前三个元素进入矩阵最后一列。"),
			_step("四元数转旋转矩阵", "使用 SciPy Rotation 填充左上 3×3。"),
		],
	),
	"daaam.utils.vision.bounding_box_from_mask": _guide(
		"从 mask 提取包围框",
		"计算覆盖目标前景的轴对齐矩形，可选先做中值滤波去除噪声。",
		[("mask / smooth", "二值区域与是否平滑。")],
		[("[xmin, ymin, xmax, ymax]", "目标像素包围框。")],
		[],
		[
			_step("可选平滑 mask", "使用中值滤波去除孤立噪声。", kind="optional"),
			_step("计算外接矩形", "调用 OpenCV boundingRect 并转换为角点坐标。"),
		],
	),
	"daaam.utils.vision.fast_create_output_images": _guide(
		"矢量化生成语义图",
		"用 combined_mask 直接索引语义和颜色查找表，一次生成两张输出图。",
		[("combined_mask / lookup tables", "track_id 图、语义编号表和颜色表。")],
		[("label_image / color_image", "语义编号图与颜色图。")],
		[],
		[
			_step("查表生成图像", "NumPy 高级索引把每个 track_id 替换为 semantic_id 和颜色。"),
			_step("清零背景", "combined_mask 为 0 的区域保持无标签和黑色。"),
		],
	),
	"daaam.utils.vision.fast_median_depth": _guide(
		"快速计算有效深度中值",
		"从 mask 覆盖区域提取深度，仅保留有限且大于零的测量。",
		[("depth_image / mask", "深度图与布尔 mask。")],
		[("median depth", "有效深度中值；无有效像素时返回 -1。")],
		[],
		[
			_step("筛选有效测量", "排除 NaN、Inf 和零深度。"),
			_step("计算中值", "使用中值降低边缘混合和离群点影响。"),
		],
	),
}


MEANINGFUL_LEAF_CALLS = {
	"self.segmenter",
	"self.tracker.update",
	"self.clip_handler.extract_image_features_from_arrays",
	"mask_to_polygons",
	"compute_polygon_area",
	"cv2.boundingRect",
	"cv2.medianBlur",
	"cv2.distanceTransform",
	"cv2.minMaxLoc",
	"scipy.spatial.transform.Rotation.from_quat",
}


def _unparse(node: ast.AST | None) -> str:
	"""Return compact source for the AST forms used here, including on Python 3.8."""
	if node is None:
		return ""
	stdlib_unparse = getattr(ast, "unparse", None)
	if stdlib_unparse is not None:
		return stdlib_unparse(node)
	if isinstance(node, ast.Name):
		return node.id
	if isinstance(node, ast.Attribute):
		return f"{_unparse(node.value)}.{node.attr}"
	if isinstance(node, ast.Constant):
		return repr(node.value)
	if isinstance(node, ast.Str):
		return repr(node.s)
	if isinstance(node, ast.Subscript):
		slice_node = node.slice.value if isinstance(node.slice, ast.Index) else node.slice
		return f"{_unparse(node.value)}[{_unparse(slice_node)}]"
	if isinstance(node, ast.Tuple):
		return ", ".join(_unparse(item) for item in node.elts)
	if isinstance(node, ast.List):
		return f"[{', '.join(_unparse(item) for item in node.elts)}]"
	if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
		return f"{_unparse(node.left)} | {_unparse(node.right)}"
	if isinstance(node, ast.arg):
		return node.arg + (f": {_unparse(node.annotation)}" if node.annotation else "")
	if isinstance(node, ast.arguments):
		parts: list[str] = []
		positional = [*getattr(node, "posonlyargs", []), *node.args]
		default_start = len(positional) - len(node.defaults)
		for index, argument in enumerate(positional):
			value = _unparse(argument)
			if index >= default_start:
				value += f"={_unparse(node.defaults[index - default_start])}"
			parts.append(value)
			if getattr(node, "posonlyargs", []) and index + 1 == len(node.posonlyargs):
				parts.append("/")
		if node.vararg:
			parts.append(f"*{_unparse(node.vararg)}")
		elif node.kwonlyargs:
			parts.append("*")
		for argument, default in zip(node.kwonlyargs, node.kw_defaults):
			value = _unparse(argument)
			if default is not None:
				value += f"={_unparse(default)}"
			parts.append(value)
		if node.kwarg:
			parts.append(f"**{_unparse(node.kwarg)}")
		return ", ".join(parts)
	if isinstance(node, ast.Call):
		return f"{_unparse(node.func)}(...)"
	return node.__class__.__name__


@dataclass(frozen=True)
class Definition:
	qualified_name: str
	module: str
	class_name: str | None
	name: str
	path: Path
	node: ast.FunctionDef | ast.AsyncFunctionDef
	source: str


@dataclass(frozen=True)
class ModuleInfo:
	name: str
	path: Path
	tree: ast.Module
	source: str
	imports: dict[str, str]


@dataclass(frozen=True)
class CallSite:
	node: ast.Call
	order: int
	context: tuple[str, ...]


class _CallVisitor(ast.NodeVisitor):
	"""Collect ordered calls and their enclosing control-flow context."""

	def __init__(self, source: str) -> None:
		self.source = source
		self.calls: list[CallSite] = []
		self.context: list[str] = []

	def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 - ast API
		self.calls.append(CallSite(node=node, order=len(self.calls) + 1, context=tuple(self.context)))
		self.generic_visit(node)

	def visit_If(self, node: ast.If) -> None:  # noqa: N802
		self.visit(node.test)
		self._visit_block(f"if {self._text(node.test)}", node.body)
		if node.orelse:
			self._visit_block(f"else ({self._text(node.test)})", node.orelse)

	def visit_For(self, node: ast.For) -> None:  # noqa: N802
		self.visit(node.iter)
		self._visit_block(
			f"for {self._text(node.target)} in {self._text(node.iter)}",
			node.body,
		)
		if node.orelse:
			self._visit_block("for-else", node.orelse)

	def visit_AsyncFor(self, node: ast.AsyncFor) -> None:  # noqa: N802
		self.visit(node.iter)
		self._visit_block(
			f"async for {self._text(node.target)} in {self._text(node.iter)}",
			node.body,
		)
		if node.orelse:
			self._visit_block("async-for-else", node.orelse)

	def visit_While(self, node: ast.While) -> None:  # noqa: N802
		self.visit(node.test)
		self._visit_block(f"while {self._text(node.test)}", node.body)
		if node.orelse:
			self._visit_block("while-else", node.orelse)

	def visit_With(self, node: ast.With) -> None:  # noqa: N802
		for item in node.items:
			self.visit(item.context_expr)
		label = ", ".join(self._text(item.context_expr) for item in node.items)
		self._visit_block(f"with {label}", node.body)

	def visit_AsyncWith(self, node: ast.AsyncWith) -> None:  # noqa: N802
		for item in node.items:
			self.visit(item.context_expr)
		label = ", ".join(self._text(item.context_expr) for item in node.items)
		self._visit_block(f"async with {label}", node.body)

	def visit_Try(self, node: ast.Try) -> None:  # noqa: N802
		self._visit_block("try", node.body)
		for handler in node.handlers:
			label = f"except {self._text(handler.type)}" if handler.type else "except"
			self._visit_block(label, handler.body)
		if node.orelse:
			self._visit_block("try-else", node.orelse)
		if node.finalbody:
			self._visit_block("finally", node.finalbody)

	def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
		return

	def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
		return

	def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
		return

	def _visit_block(self, label: str, statements: Iterable[ast.stmt]) -> None:
		self.context.append(label)
		for statement in statements:
			self.visit(statement)
		self.context.pop()

	def _text(self, node: ast.AST | None) -> str:
		if node is None:
			return ""
		text = ast.get_source_segment(self.source, node) or _unparse(node)
		return " ".join(text.split())


class SourceIndex:
	"""Static index of Python definitions and imports below a source root."""

	def __init__(self, source_root: Path):
		self.source_root = Path(source_root).resolve()
		self.modules: dict[str, ModuleInfo] = {}
		self.definitions: dict[str, Definition] = {}
		self.classes: set[str] = set()
		self.instance_types: dict[str, dict[str, str]] = {}

	def build(self) -> None:
		if not self.source_root.is_dir():
			raise FileNotFoundError(f"source root does not exist: {self.source_root}")

		for path in sorted(self.source_root.rglob("*.py")):
			module_name = self._module_name(path)
			source = path.read_text(encoding="utf-8")
			try:
				tree = ast.parse(source, filename=str(path))
			except SyntaxError as exc:
				raise SyntaxError(f"cannot parse {path}: {exc.msg}", exc.args) from exc
			imports = self._collect_imports(
				module_name,
				tree,
				is_package=path.name == "__init__.py",
			)
			self.modules[module_name] = ModuleInfo(
				name=module_name,
				path=path,
				tree=tree,
				source=source,
				imports=imports,
			)

		for module in self.modules.values():
			self._index_definitions(module)

		for class_name in sorted(self.classes):
			self.instance_types[class_name] = self._infer_instance_types(class_name)

	def resolve_entry(self, qualified_name: str) -> Definition:
		definition = self.resolve_definition(qualified_name)
		if definition is None:
			raise ValueError(f"entry point not found: {qualified_name}")
		return definition

	def resolve_definition(self, qualified_name: str) -> Definition | None:
		current = qualified_name
		seen: set[str] = set()
		while current not in seen:
			seen.add(current)
			if current in self.definitions:
				return self.definitions[current]
			redirected = self._redirect_import(current)
			if redirected == current:
				break
			current = redirected
		return None

	def resolve_class(self, qualified_name: str) -> str | None:
		current = qualified_name
		seen: set[str] = set()
		while current not in seen:
			seen.add(current)
			if current in self.classes:
				return current
			redirected = self._redirect_import(current)
			if redirected == current:
				break
			current = redirected
		return None

	def calls_in(self, definition: Definition) -> list[CallSite]:
		visitor = _CallVisitor(self.modules[definition.module].source)
		for statement in definition.node.body:
			visitor.visit(statement)
		return visitor.calls

	def relative_path(self, path: Path) -> str:
		return path.resolve().relative_to(self.source_root).as_posix()

	def _module_name(self, path: Path) -> str:
		relative = path.resolve().relative_to(self.source_root)
		parts = list(relative.with_suffix("").parts)
		if parts[-1] == "__init__":
			parts.pop()
		return ".".join(parts)

	def _collect_imports(
		self,
		module_name: str,
		tree: ast.Module,
		*,
		is_package: bool,
	) -> dict[str, str]:
		imports: dict[str, str] = {}
		for node in tree.body:
			if isinstance(node, ast.Import):
				for alias in node.names:
					local = alias.asname or alias.name.split(".")[0]
					imports[local] = alias.name
			elif isinstance(node, ast.ImportFrom):
				base = self._resolve_from_module(
					module_name,
					node.module,
					node.level,
					is_package=is_package,
				)
				for alias in node.names:
					if alias.name == "*":
						continue
					local = alias.asname or alias.name
					imports[local] = f"{base}.{alias.name}" if base else alias.name
		return imports

	@staticmethod
	def _resolve_from_module(
		module_name: str,
		imported: str | None,
		level: int,
		*,
		is_package: bool,
	) -> str:
		if level == 0:
			return imported or ""
		package_parts = module_name.split(".") if is_package else module_name.split(".")[:-1]
		keep = max(0, len(package_parts) - (level - 1))
		base = package_parts[:keep]
		if imported:
			base.extend(imported.split("."))
		return ".".join(base)

	def _index_definitions(self, module: ModuleInfo) -> None:
		for node in module.tree.body:
			if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
				self._add_definition(module, None, node)
			elif isinstance(node, ast.ClassDef):
				class_name = f"{module.name}.{node.name}"
				self.classes.add(class_name)
				for member in node.body:
					if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
						self._add_definition(module, node.name, member)

	def _add_definition(
		self,
		module: ModuleInfo,
		class_name: str | None,
		node: ast.FunctionDef | ast.AsyncFunctionDef,
	) -> None:
		parts = [module.name]
		if class_name:
			parts.append(class_name)
		parts.append(node.name)
		qualified_name = ".".join(parts)
		self.definitions[qualified_name] = Definition(
			qualified_name=qualified_name,
			module=module.name,
			class_name=class_name,
			name=node.name,
			path=module.path,
			node=node,
			source=ast.get_source_segment(module.source, node) or "",
		)

	def _redirect_import(self, qualified_name: str) -> str:
		parts = qualified_name.split(".")
		for split_at in range(len(parts) - 1, 0, -1):
			module_name = ".".join(parts[:split_at])
			module = self.modules.get(module_name)
			if module is None or split_at >= len(parts):
				continue
			symbol = parts[split_at]
			if symbol in module.imports:
				return ".".join([module.imports[symbol], *parts[split_at + 1 :]])
		return qualified_name

	def _infer_instance_types(self, class_name: str) -> dict[str, str]:
		constructor = self.resolve_definition(f"{class_name}.__init__")
		if constructor is None:
			return {}
		result: dict[str, str] = {}
		for node in ast.walk(constructor.node):
			if not isinstance(node, (ast.Assign, ast.AnnAssign)):
				continue
			targets = node.targets if isinstance(node, ast.Assign) else [node.target]
			value = node.value
			if not isinstance(value, ast.Call):
				continue
			constructor_name = self._expression_name(value.func)
			if not constructor_name:
				continue
			resolved_name = self._resolve_symbol(constructor.module, constructor_name)
			resolved_class = self.resolve_class(resolved_name)
			if resolved_class is None:
				continue
			for target in targets:
				if (
					isinstance(target, ast.Attribute)
					and isinstance(target.value, ast.Name)
					and target.value.id == "self"
				):
					result[target.attr] = resolved_class
		return result

	def _resolve_symbol(self, module_name: str, expression: str) -> str:
		parts = expression.split(".")
		module = self.modules[module_name]
		if parts[0] in module.imports:
			return ".".join([module.imports[parts[0]], *parts[1:]])
		return f"{module_name}.{expression}"

	@staticmethod
	def _expression_name(node: ast.AST) -> str | None:
		if isinstance(node, ast.Name):
			return node.id
		if isinstance(node, ast.Attribute):
			base = SourceIndex._expression_name(node.value)
			return f"{base}.{node.attr}" if base else node.attr
		return None


class GraphBuilder:
	def __init__(self, index: SourceIndex):
		self.index = index
		self.nodes: dict[str, dict[str, Any]] = {}
		self.edges: list[dict[str, Any]] = []
		self.visited: set[str] = set()

	def build(self, root: Definition) -> dict[str, object]:
		self._visit(root, ())
		nodes = sorted(self.nodes.values(), key=lambda item: item["id"])
		edges = sorted(
			self.edges,
			key=lambda item: (
				item["source"],
				item["order"],
				item["target"],
				item["expression"],
			),
		)
		logic_edges = [edge for edge in edges if self._is_logic_edge(edge)]
		for node in nodes:
			if node["kind"] == "internal":
				node["guide"] = self._guide_for(node, logic_edges, edges)
		return {
			"root": root.qualified_name,
			"nodes": nodes,
			"edges": edges,
			"logic_edges": logic_edges,
		}

	def _is_logic_edge(self, edge: dict[str, Any]) -> bool:
		target = self.nodes[edge["target"]]
		if (
			target["id"].startswith("daaam.utils.memory.")
			or edge["expression"] in {"release_freed_memory", "resident_mb", "_malloc_trim"}
		):
			return False
		return target["kind"] == "internal" or edge["expression"] in MEANINGFUL_LEAF_CALLS

	@staticmethod
	def _guide_for(
		node: dict[str, Any],
		logic_edges: list[dict[str, Any]],
		raw_edges: list[dict[str, Any]],
	) -> dict[str, object]:
		curated = FUNCTION_GUIDES.get(node["id"])
		if curated is not None:
			guide = copy.deepcopy(curated)
		else:
			calls = [edge["target"] for edge in logic_edges if edge["source"] == node["id"]]
			guide = _guide(
				node["name"],
				node["summary"],
				[("函数参数", "见右侧函数签名和源码。")],
				[("返回值", "见右侧返回注解和调用位置。")],
				[],
				[
					_step(
						"执行函数主体",
						"按源码顺序完成该函数职责；右侧源码可用于核对边界条件。",
						*calls,
					)
				],
			)

		steps = guide["steps"]
		for index, step in enumerate(steps):
			step["source_excerpt"] = GraphBuilder._source_excerpt(
				node,
				step,
				index,
				len(steps),
				raw_edges,
			)
			if not step.get("result"):
				step["result"] = "本步骤完成后，结果写入返回值或内部状态，并交给下一处理阶段。"
		return guide

	@staticmethod
	def _source_excerpt(
		node: dict[str, Any],
		step: dict[str, Any],
		step_index: int,
		step_count: int,
		raw_edges: list[dict[str, Any]],
	) -> str:
		"""Return a short, real source slice that corresponds to one explained step."""
		lines = node["source"].splitlines()
		if not lines:
			return "# 此节点没有可显示的本地源码。"

		call_targets = set(step.get("calls", []))
		matching_lines = [
			edge["line"]
			for edge in raw_edges
			if edge["source"] == node["id"] and edge["target"] in call_targets
		]
		if matching_lines:
			local_start = max(0, min(matching_lines) - node["line_start"] - 1)
			local_end = min(len(lines), max(matching_lines) - node["line_start"] + 3)
		else:
			body_start = 1 if len(lines) > 1 else 0
			body_count = max(1, len(lines) - body_start)
			chunk_size = max(1, (body_count + step_count - 1) // step_count)
			local_start = min(len(lines) - 1, body_start + step_index * chunk_size)
			local_end = min(len(lines), local_start + max(3, chunk_size))

		return "\n".join(lines[local_start:local_end]).rstrip() or lines[0]

	def _visit(self, definition: Definition, stack: tuple[str, ...]) -> None:
		qualified_name = definition.qualified_name
		self.nodes.setdefault(qualified_name, self._internal_node(definition))
		if qualified_name in self.visited:
			return
		self.visited.add(qualified_name)
		active_stack = (*stack, qualified_name)

		for call_site in self.index.calls_in(definition):
			call = call_site.node
			target_name, kind, target_definition = self._resolve_call(definition, call)
			cycle = target_definition is not None and target_name in active_stack
			if target_definition is not None:
				self.nodes.setdefault(target_name, self._internal_node(target_definition))
			else:
				self.nodes.setdefault(target_name, self._leaf_node(target_name, kind))
			self.edges.append(
				{
					"source": qualified_name,
					"target": target_name,
					"order": call_site.order,
					"context": list(call_site.context),
					"line": call.lineno,
					"expression": _unparse(call.func),
					"cycle": cycle,
				}
			)
			if target_definition is not None and not cycle:
				self._visit(target_definition, active_stack)

	def _resolve_call(
		self,
		definition: Definition,
		call: ast.Call,
	) -> tuple[str, str, Definition | None]:
		expression = self.index._expression_name(call.func) or _unparse(call.func)
		candidate: str | None = None

		if expression.startswith("self.") and definition.class_name:
			parts = expression.split(".")
			class_qualified = f"{definition.module}.{definition.class_name}"
			if len(parts) == 2:
				candidate = f"{class_qualified}.{parts[1]}"
			elif len(parts) >= 3:
				instance_class = self.index.instance_types.get(class_qualified, {}).get(parts[1])
				if instance_class:
					candidate = ".".join([instance_class, *parts[2:]])
		else:
			candidate = self.index._resolve_symbol(definition.module, expression)

		if candidate:
			target_definition = self.index.resolve_definition(candidate)
			if target_definition is not None:
				return target_definition.qualified_name, "internal", target_definition

		root = expression.split(".", 1)[0]
		module = self.index.modules[definition.module]
		if root in module.imports:
			external_name = ".".join([module.imports[root], *expression.split(".")[1:]])
			if not external_name.startswith("daaam."):
				return external_name, "external", None
		if isinstance(call.func, ast.Name) and hasattr(builtins, call.func.id):
			return f"builtins.{call.func.id}", "external", None
		return expression, "unresolved", None

	def _internal_node(self, definition: Definition) -> dict[str, Any]:
		return_annotation = (
			f" -> {_unparse(definition.node.returns)}" if definition.node.returns else ""
		)
		signature = f"{definition.name}({_unparse(definition.node.args)}){return_annotation}"
		docstring = ast.get_docstring(definition.node, clean=True)
		summary = docstring.splitlines()[0] if docstring else f"DAAAM function {definition.name}."
		return {
			"id": definition.qualified_name,
			"name": definition.name,
			"qualified_name": definition.qualified_name,
			"kind": "internal",
			"file": self.index.relative_path(definition.path),
			"line_start": definition.node.lineno,
			"line_end": definition.node.end_lineno or definition.node.lineno,
			"signature": signature,
			"summary": summary,
			"source": definition.source,
			"expandable": True,
		}

	@staticmethod
	def _leaf_node(qualified_name: str, kind: str) -> dict[str, Any]:
		return {
			"id": qualified_name,
			"name": qualified_name.rsplit(".", 1)[-1],
			"qualified_name": qualified_name,
			"kind": kind,
			"file": "",
			"line_start": None,
			"line_end": None,
			"signature": "",
			"summary": (
				"External dependency; traversal stops here."
				if kind == "external"
				else "Dynamic call could not be resolved statically; traversal stops here."
			),
			"source": "",
			"expandable": False,
		}


def build_graph(source_root: Path, entry_point: str = DEFAULT_ENTRY_POINT) -> dict[str, object]:
	index = SourceIndex(source_root)
	index.build()
	return GraphBuilder(index).build(index.resolve_entry(entry_point))


def render_html(graph: dict[str, object], template_path: Path) -> str:
	template = Path(template_path).read_text(encoding="utf-8")
	if template.count(DATA_MARKER) != 1:
		raise ValueError(f"template must contain exactly one {DATA_MARKER}")
	payload = json.dumps(graph, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
	payload = payload.replace("</", "<\\/")
	return template.replace(DATA_MARKER, f"const GRAPH_DATA = {payload};")


def write_atomic(path: Path, content: str) -> None:
	path = Path(path)
	path.parent.mkdir(parents=True, exist_ok=True)
	temporary_name: str | None = None
	try:
		with tempfile.NamedTemporaryFile(
			mode="w",
			encoding="utf-8",
			dir=path.parent,
			prefix=f".{path.name}.",
			suffix=".tmp",
			delete=False,
		) as temporary:
			temporary.write(content)
			temporary.flush()
			os.fsync(temporary.fileno())
			temporary_name = temporary.name
		Path(temporary_name).replace(path)
	finally:
		if temporary_name:
			Path(temporary_name).unlink(missing_ok=True)


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
	repository_root = Path(__file__).resolve().parents[1]
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--source-root", type=Path, default=repository_root / "src")
	parser.add_argument(
		"--template",
		type=Path,
		default=repository_root / "scripts" / "orchestrator_graph_viewer.html",
	)
	parser.add_argument(
		"--output",
		type=Path,
		default=repository_root / "docs" / "orchestrator_process_frame.html",
	)
	parser.add_argument("--entry-point", default=DEFAULT_ENTRY_POINT)
	return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
	args = parse_args(argv)
	graph = build_graph(args.source_root, args.entry_point)
	html = render_html(graph, args.template)
	write_atomic(args.output, html)
	print(f"Generated {args.output.resolve()}")
	print(f"Nodes: {len(graph['nodes'])}; edges: {len(graph['edges'])}")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
