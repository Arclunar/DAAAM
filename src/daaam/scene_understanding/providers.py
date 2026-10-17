"""Provider abstraction for LLM backends (OpenAI, Anthropic).

OpenAI tool format is the canonical representation used by all tools.
Conversion to Anthropic format happens here at the provider boundary.
"""

import json
from enum import Enum
from typing import Any, Callable, Dict, List, Literal, Mapping, Optional, Sequence, Tuple, TypedDict

from daaam.scene_understanding.tools.utils import round_floats


class Provider(Enum):
	OPENAI = "openai"
	ANTHROPIC = "anthropic"


def detect_provider(model_name: str) -> Provider:
	if model_name.startswith("claude"):
		return Provider.ANTHROPIC
	return Provider.OPENAI


def create_client(provider: Provider, timeout: float = 120.0, max_retries: int = 2):
	"""Build a provider-aware client.

	`timeout` is the per-request HTTP timeout in seconds. It must be set when
	running multi-sequence eval so a stalled connection (server-side FIN, dead
	keep-alive, etc.) fails fast and frees the worker thread instead of hanging
	the whole pool.
	"""
	if provider == Provider.ANTHROPIC:
		import anthropic
		return anthropic.Anthropic(timeout=timeout, max_retries=max_retries)
	from openai import OpenAI
	return OpenAI(timeout=timeout, max_retries=max_retries)


# Conversation context and progress events (provider neutral)

ConversationTurn = TypedDict("ConversationTurn", {"role": Literal["user", "assistant"], "content": str})
ProgressCallback = Callable[[Mapping[str, Any]], None]

# Everything a caller supplies for earlier turns and the replay context together; a long chat is trimmed by the caller.
MAX_CONVERSATION_BYTES = 64 * 1024
CONTEXT_KEYS = ("replay_time", "camera", "selected_entity")


def conversation_messages(turns: Optional[Sequence[ConversationTurn]]) -> List[Dict[str, str]]:
	"""Validate earlier turns and copy them as plain role/content pairs."""
	messages = []
	for turn in turns or ():
		role = turn.get("role") if isinstance(turn, Mapping) else None
		content = turn.get("content") if isinstance(turn, Mapping) else None
		if role not in ("user", "assistant") or not isinstance(content, str):
			raise ValueError("conversation turns require a user/assistant role and string content")
		messages.append({"role": role, "content": content})
	return messages


def context_text(context: Optional[Mapping[str, Any]]) -> str:
	"""The replay view the question was asked in, one `key: value` line per allowed field.

	Only the replay fields below are rendered, whatever else the caller passes. Each value is kept on one line so a value
	cannot start a new instruction line.
	"""
	if not context:
		return ""
	if not isinstance(context, Mapping):
		raise ValueError("the replay context must be a mapping")
	lines = []
	for key in CONTEXT_KEYS:
		value = context.get(key)
		if value is None:
			continue
		if not isinstance(value, (str, int, float, bool)):
			try:
				value = json.dumps(value, sort_keys=True, default=str)
			except (TypeError, ValueError):
				raise ValueError("the replay context field {} cannot be rendered".format(key))
		lines.append("{}: {}".format(key, " ".join(str(value).split())))
	if not lines:
		return ""
	return "Current replay context (may be omitted by the user):\n" + "\n".join(lines) + "\n"


def check_conversation_size(messages: Sequence[Mapping[str, str]], context_block: str) -> None:
	size = sum(len(message["content"].encode("utf-8")) for message in messages) + len(context_block.encode("utf-8"))
	if size > MAX_CONVERSATION_BYTES:
		raise ValueError("the conversation and replay context are larger than {} bytes".format(MAX_CONVERSATION_BYTES))


def emit_progress(progress: Optional[ProgressCallback], logger, event: Mapping[str, Any]) -> None:
	"""Offer a safe progress event; a display hook that fails must not fail the query."""
	if progress is None:
		return
	try:
		progress(dict(event))
	except Exception as error:
		logger.warning("progress callback failed: {}".format(error))


# Tool signature conversion: canonical OpenAI → Anthropic

def openai_tools_to_anthropic(openai_tools: list) -> list:
	return [
		{
			"name": t["name"],
			"description": t.get("description", ""),
			"input_schema": t["parameters"],
		}
		for t in openai_tools
	]


# Anthropic message builders

def build_anthropic_initial_messages(
	query: str,
	image_data: Optional[str] = None,
	conversation: Optional[Sequence[ConversationTurn]] = None,
	context_block: str = "",
) -> list:
	"""Earlier turns as text blocks, then the question (with its replay context and image) as the last user turn.

	The Messages API starts with a user turn and alternates roles, so a leading assistant turn (its question was trimmed
	away) is dropped and adjacent turns of one role are merged.
	"""
	messages: list = []
	for turn in conversation_messages(conversation):
		block = {"type": "text", "text": turn["content"]}
		if messages and messages[-1]["role"] == turn["role"]:
			messages[-1]["content"].append(block)
		elif messages or turn["role"] == "user":
			messages.append({"role": turn["role"], "content": [block]})

	user_content: list = []
	if context_block:
		user_content.append({"type": "text", "text": context_block})
	user_content.append({"type": "text", "text": query})
	if image_data:
		user_content.append({
			"type": "image",
			"source": {
				"type": "base64",
				"media_type": "image/jpeg",
				"data": image_data,
			},
		})
	if messages and messages[-1]["role"] == "user":
		messages[-1]["content"].extend(user_content)
	else:
		messages.append({"role": "user", "content": user_content})
	return messages


# Serialization helpers

def _serialize_content_blocks(content_blocks: list) -> list:
	"""Convert Anthropic SDK Pydantic content blocks to plain dicts.

	The SDK returns TextBlock/ToolUseBlock Pydantic objects in response.content,
	but the messages API expects plain dicts when these are passed back as
	assistant message content. Passing raw Pydantic objects triggers a by_alias
	bug in the SDK's _transform_recursive -> model_dump path.
	"""
	serialized = []
	for block in content_blocks:
		if block.type == "text":
			serialized.append({"type": "text", "text": block.text})
		elif block.type == "tool_use":
			serialized.append({
				"type": "tool_use",
				"id": block.id,
				"name": block.name,
				"input": block.input,
			})
		else:
			serialized.append(block.model_dump(mode="json", by_alias=False))
	return serialized


def _serialize_anthropic_response(response) -> dict:
	"""Pre-serialize an Anthropic response into the dict format eval_navqa.py expects."""
	text_preview = ""
	for block in response.content:
		if hasattr(block, "text"):
			text_preview = block.text[:500]
			break
	return {
		"type": "AnthropicMessage",
		"output": text_preview,
		"usage": {
			"input_tokens": response.usage.input_tokens,
			"cached_tokens": getattr(response.usage, "cache_read_input_tokens", 0),
			"output_tokens": response.usage.output_tokens,
			"reasoning_tokens": 0,
			"total_tokens": response.usage.input_tokens + response.usage.output_tokens,
		},
	}


# Anthropic tool-calling loop

def run_anthropic_tool_loop(
	client,
	model_name: str,
	system_prompt: str,
	messages: list,
	tools: list,
	tool_registry,
	max_iterations: int,
	response_schema,
	format_instructions: str,
	logger,
	verbose: bool = False,
	progress: Optional[ProgressCallback] = None,
	query: Optional[str] = None,
):
	"""Run the Anthropic tool-calling loop, analogous to the OpenAI loop in services.py.

	`progress` receives only {"type": "iteration"|"tool"|"final", ...} with the iteration numbers and tool names, never
	arguments, results or model text. `query` is the question for the interaction history when `messages` start with
	earlier turns.

	Returns:
		Tuple of (parsed_response, interaction_history, messages).
	"""
	interaction_history: Dict[str, Any] = {
		"query": query if query is not None else (messages[0]["content"][0]["text"] if messages else ""),
		"iterations": [],
		"final_response": None,
	}

	for iteration in range(max_iterations):
		if verbose:
			logger.info(f"Iteration {iteration + 1}/{max_iterations}")
		print(f"Iteration {iteration + 1}/{max_iterations}")
		emit_progress(progress, logger, {"type": "iteration", "number": iteration + 1, "maximum": max_iterations})

		response = client.messages.create(
			model=model_name,
			max_tokens=16384,
			system=system_prompt,
			tools=tools,
			messages=messages,
		)

		iteration_data: Dict[str, Any] = {
			"iteration": iteration + 1,
			"response": _serialize_anthropic_response(response),
		}

		# Append assistant turn (Anthropic requires full content list)
		messages.append({"role": "assistant", "content": _serialize_content_blocks(response.content)})

		if response.stop_reason == "tool_use":
			tool_results = []
			for block in response.content:
				if block.type != "tool_use":
					continue

				emit_progress(progress, logger, {"type": "tool", "name": block.name})
				result = tool_registry.call_tool(block.name, json.dumps(block.input))

				text_result = result
				images_payload = None
				if isinstance(result, dict) and "_images" in result:
					images_payload = result["_images"]
					text_result = {k: v for k, v in result.items() if k != "_images"}

				result_json = json.dumps(round_floats(text_result))
				content: list = [{"type": "text", "text": result_json}]
				if images_payload:
					# Anthropic accepts image blocks directly inside tool_result.content.
					for img in images_payload:
						content.append({
							"type": "image",
							"source": {
								"type": "base64",
								"media_type": "image/jpeg",
								"data": img["base64"],
							},
						})
						label = img.get("label", "")
						if label:
							content.append({"type": "text", "text": label})

				tool_results.append({
					"type": "tool_result",
					"tool_use_id": block.id,
					"content": content,
				})
				iteration_data["tool_results"] = text_result

			# Anthropic requires all tool results in a single user message
			messages.append({"role": "user", "content": tool_results})
			interaction_history["iterations"].append(iteration_data)
		else:
			# end_turn — model is done calling tools, produce structured output
			break

	# -----------------------------------------------------------------------
	# Structured output via client.messages.parse()
	# -----------------------------------------------------------------------
	# Build a filtered message list for the final parse call:
	# keep system context (via system= param), user messages, and text-only
	# assistant content. Exclude raw tool_use / tool_result blocks so the
	# parse call doesn't choke on orphaned tool interactions.

	if response.stop_reason == "tool_use":
		# max iterations exhausted while still calling tools
		logger.warning("Max iterations reached without final answer")

	filtered_messages = _build_filtered_messages(messages)
	filtered_messages.append({
		"role": "user",
		"content": "Based on all the information gathered, please provide your final answer now.",
	})

	emit_progress(progress, logger, {"type": "final"})
	final_response = client.messages.parse(
		model=model_name,
		max_tokens=16384,
		system=f"Respond only in the given format. {format_instructions}",
		messages=filtered_messages,
		output_format=response_schema,
	)

	parsed = final_response.parsed_output
	assert parsed is not None, "Anthropic messages.parse() returned None for parsed_output"

	final_iteration_data = {
		"iteration": "final_parse",
		"response": _serialize_anthropic_response(final_response),
	}
	interaction_history["iterations"].append(final_iteration_data)
	interaction_history["final_response"] = parsed

	# Append final assistant content to messages for downstream compatibility
	messages.append({"role": "assistant", "content": _serialize_content_blocks(final_response.content)})

	return parsed, interaction_history, messages


def _build_filtered_messages(messages: list) -> list:
	"""Keep only user text messages and text-only assistant messages.

	Strips tool_use blocks, tool_result blocks, and function call artifacts
	so the final parse() call gets clean context.
	"""
	filtered = []
	for msg in messages:
		if not isinstance(msg, dict):
			continue
		role = msg.get("role")
		content = msg.get("content")

		if role == "user":
			# Keep user messages that contain text (skip pure tool_result messages)
			if isinstance(content, list):
				text_parts = [p for p in content if isinstance(p, dict) and p.get("type") in ("text", "image")]
				if text_parts:
					filtered.append({"role": "user", "content": text_parts})
			elif isinstance(content, str):
				filtered.append(msg)

		elif role == "assistant":
			# Keep only TextBlock content from assistant messages
			if isinstance(content, list):
				text_parts = []
				for block in content:
					if isinstance(block, dict) and block.get("type") == "text":
						text_parts.append(block)
					elif hasattr(block, "type") and block.type == "text":
						text_parts.append({"type": "text", "text": block.text})
				if text_parts:
					filtered.append({"role": "assistant", "content": text_parts})

	return filtered
