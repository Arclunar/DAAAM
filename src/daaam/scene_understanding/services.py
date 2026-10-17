from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, List, Mapping, Sequence
import json
import base64
from io import BytesIO
from pydantic import BaseModel

import spark_dsg as sdsg
import numpy as np
import torch
from PIL import Image

from daaam.scene_understanding.config import SceneUnderstandingConfig
from daaam.utils.embedding import CLIPHandler, SentenceEmbeddingHandler, verify_embedding_provenance
from daaam.utils.logging import PipelineLogger, get_default_logger
from daaam.scene_understanding.models import Response
from daaam.scene_understanding.interfaces import (
    SceneUnderstandingInterface
)
from daaam.scene_understanding.tools import create_default_tool_registry
from daaam.scene_understanding.tools.utils import round_floats
from daaam.scene_understanding.prompt_utils import (
    build_system_prompt,
    get_response_format_instructions
)
from daaam.scene_understanding.providers import (
	Provider, detect_provider, create_client,
	openai_tools_to_anthropic, run_anthropic_tool_loop,
	build_anthropic_initial_messages,
	ConversationTurn, ProgressCallback,
	conversation_messages, context_text, check_conversation_size, emit_progress,
)

class SceneUnderstandingAgent(SceneUnderstandingInterface):
	"""Service for handling scene understanding with tool calling."""

	def __init__(
		self,
		config: SceneUnderstandingConfig,
		logger: Optional[PipelineLogger] = None,
		clip_handler: Optional[CLIPHandler] = None,
		sentence_handler: Optional[SentenceEmbeddingHandler] = None,
	):
		"""Build a scene-understanding agent.

		If clip_handler / sentence_handler are passed, the agent reuses them instead
		of constructing fresh ones — used by multi-sequence eval to share heavy models
		across per-sequence agents.
		"""
		super().__init__(config, logger)

		self.provider = detect_provider(config.model_name)
		self.client = create_client(self.provider)
		self.tool_registry = create_default_tool_registry(
			config.tool_config,
			tools_to_include=config.available_tools
		)

		if clip_handler is None and config.tool_config.clip_model_name and config.tool_config.clip_backend:
			clip_handler = CLIPHandler(
				model_name=config.tool_config.clip_model_name,
				backend=config.tool_config.clip_backend,
			)
		if sentence_handler is None:
			sentence_handler = SentenceEmbeddingHandler(
				model_name=config.tool_config.sentence_embedding_model_name,
				device="cuda" if torch.cuda.is_available() else "cpu",
			)

		self.clip_handler = clip_handler
		self.sentence_handler = sentence_handler

		self.tool_registry.set_embedding_handlers(self.clip_handler, self.sentence_handler)

		# Scene graph stays empty until update_scene_graph() is called.


	def answer_query(
		self,
		ResponseFormat: type,
		query: str,
		image: Optional[Any] = None,
		*,
		conversation: Optional[Sequence[ConversationTurn]] = None,
		context: Optional[Mapping[str, Any]] = None,
		progress: Optional[ProgressCallback] = None,
		**kwargs,
	) -> Tuple[Response, Dict[str, Any], List[Any]]:
		"""
		Answer a query about the scene by using provided tools.

		Args:
			query: The user's query about the scene.
			image: Optional image (PIL Image, numpy array, or path) to include.
			conversation: Earlier turns, oldest first, as {"role": "user"|"assistant", "content": str}. Both providers
				receive them as plain messages ahead of the question.
			context: The replay view the question is asked in (replay_time, camera, selected_entity); other keys are ignored.
				Conversation and context together are limited to 64 KiB.
			progress: Called with {"type": "iteration", "number", "maximum"}, {"type": "tool", "name"} and {"type": "final"}
				only (never prompts, tool arguments or results, or model reasoning). A callback that raises is logged and
				the query goes on.
			**kwargs: Additional context.

		Returns:
			Tuple[Response, Dict[str, Any], List[Any]]:
				Response: The final answer and reasoning.
				Dict[str, Any]: The full interaction history.
				List[Any]: The provider messages.

		Raises:
			ValueError: Invalid conversation turns or too much conversation/context, before any provider call.
		"""
		history = conversation_messages(conversation)
		context_block = context_text(context)
		check_conversation_size(history, context_block)

		# Ensure scene graph has been set
		if not self.scene_graph or self.scene_graph.num_nodes() == 0:
			raise RuntimeError("Scene graph not set. Call update_scene_graph() before answer_query().")

		if self.config.verbose:
			self.logger.info(f"Processing query: {query}")

		if self.provider == Provider.ANTHROPIC:
			return self._answer_query_anthropic(ResponseFormat, query, image, history, context_block, progress)
		return self._answer_query_openai(ResponseFormat, query, image, history, context_block, progress)

	def _answer_query_openai(
		self,
		ResponseFormat: type,
		query: str,
		image: Optional[Any] = None,
		history: Optional[List[Dict[str, str]]] = None,
		context_block: str = "",
		progress: Optional[ProgressCallback] = None,
	):
		"""OpenAI Responses API tool-calling loop."""
		history = history or []
		messages = self._build_initial_messages(query, image, ResponseFormat, history, context_block)
		system_prompt = messages.pop(0)["content"]
		tools = self.tool_registry.get_openai_tools()
		assert tools, f"No tools registered — check available_tools config"

		if self.config.verbose:
			self.logger.info(f"Registered {len(tools)} tools: {[t['name'] for t in tools]}")

		interaction_history = {
			"query": query,
			"iterations": [],
			"final_response": None
		}

		for iteration in range(self.config.max_iterations):
			if self.config.verbose:
				self.logger.info(f"Iteration {iteration + 1}/{self.config.max_iterations}")

			print(f"Iteration {iteration + 1}/{self.config.max_iterations}")
			emit_progress(progress, self.logger, {"type": "iteration", "number": iteration + 1, "maximum": self.config.max_iterations})
			response = self.client.responses.create(
				model=self.config.model_name,
				instructions=system_prompt,
				input=messages,
				tools=tools,
				tool_choice="auto",
			)

			output_types = [item.type for item in response.output]
			print(f"  Response output types: {output_types}")

			iteration_data = {
				"iteration": iteration + 1,
				"response": response
			}

			# Append all output items first (canonical Responses API pattern)
			messages += response.output

			# Then process function calls and append their outputs
			for item in response.output:
				if item.type == "function_call":
					emit_progress(progress, self.logger, {"type": "tool", "name": item.name})
					result = self.tool_registry.call_tool(item.name, item.arguments)

					text_result = result
					images_payload = None
					if isinstance(result, dict) and "_images" in result:
						images_payload = result["_images"]
						text_result = {k: v for k, v in result.items() if k != "_images"}

					messages.append({
						"type": "function_call_output",
						"call_id": item.call_id,
						"output": json.dumps(round_floats(text_result))
					})

					if images_payload:
						# Responses API user-message form. image_url is a data URL string,
						# distinct from the older chat-completions nested object form.
						image_content: List[Dict[str, Any]] = [
							{"type": "input_text", "text": f"Images returned by {item.name}:"}
						]
						for img in images_payload:
							image_content.append({
								"type": "input_image",
								"image_url": f"data:image/jpeg;base64,{img['base64']}"
							})
							label = img.get("label", "")
							if label:
								image_content.append({"type": "input_text", "text": label})
						messages.append({"role": "user", "content": image_content})

					iteration_data["tool_results"] = text_result

			interaction_history["iterations"].append(iteration_data)

			has_function_calls = any(item.type == "function_call" for item in response.output)
			if not has_function_calls:
				# Build parse input: user query for context + model's complete final output
				# response.output includes reasoning + message items as a valid pair
				parse_input = self._user_messages(messages, len(history)) + list(response.output)

				emit_progress(progress, self.logger, {"type": "final"})
				format_instructions = get_response_format_instructions(ResponseFormat)
				final_response = self.client.responses.parse(
					model=self.config.model_name,
					instructions=f"Respond only in the given format. {format_instructions}",
					input=parse_input,
					text_format=ResponseFormat,
				)
				iteration_data["response"] = final_response

				interaction_history["iterations"].append(iteration_data)
				interaction_history["final_response"] = final_response.output_parsed
				messages += final_response.output

				return final_response.output_parsed, interaction_history, messages

		# Max iterations reached
		self.logger.warning("Max iterations reached without final answer")

		filtered_messages = self._filter_messages_for_parse(messages, len(history))
		filtered_messages.append({
			"role": "user",
			"content": "Max iterations reached. Based on all the information gathered, please provide your final answer now."
		})

		emit_progress(progress, self.logger, {"type": "final"})
		format_instructions = get_response_format_instructions(ResponseFormat)
		final_response = self.client.responses.parse(
			model=self.config.model_name,
			instructions=f"Respond only in the given format. {format_instructions}",
			input=filtered_messages,
			text_format=ResponseFormat,
		)
		iteration_data["response"] = final_response

		interaction_history["iterations"].append(iteration_data)
		interaction_history["final_response"] = final_response.output_parsed
		messages += final_response.output

		return final_response.output_parsed, interaction_history, messages

	def _answer_query_anthropic(
		self,
		ResponseFormat: type,
		query: str,
		image: Optional[Any] = None,
		history: Optional[List[Dict[str, str]]] = None,
		context_block: str = "",
		progress: Optional[ProgressCallback] = None,
	):
		"""Anthropic Messages API tool-calling loop."""
		system_prompt = build_system_prompt(ResponseFormat)
		image_data = self._process_image(image) if image else None
		messages = build_anthropic_initial_messages(query, image_data, history, context_block)
		tools = openai_tools_to_anthropic(self.tool_registry.get_openai_tools())
		format_instructions = get_response_format_instructions(ResponseFormat)

		return run_anthropic_tool_loop(
			client=self.client,
			model_name=self.config.model_name,
			system_prompt=system_prompt,
			messages=messages,
			tools=tools,
			tool_registry=self.tool_registry,
			max_iterations=self.config.max_iterations,
			response_schema=ResponseFormat,
			format_instructions=format_instructions,
			logger=self.logger,
			verbose=self.config.verbose,
			progress=progress,
			query=query,
		)

	@staticmethod
	def _user_messages(messages: list, history_length: int = 0) -> list:
		"""The user messages, plus the earlier assistant turns that lead the list, in order."""
		return [
			msg for index, msg in enumerate(messages)
			if isinstance(msg, dict) and (msg.get("role") == "user" or (index < history_length and msg.get("role") == "assistant"))
		]

	def _filter_messages_for_parse(self, messages: list, history_length: int = 0) -> list:
		"""Keep user messages (and the earlier assistant turns of the conversation) and model output (reasoning + message).
		Strips function_call/function_call_output items to avoid
		orphaned call_id references in the parse call."""
		filtered = []
		for index, msg in enumerate(messages):
			if isinstance(msg, dict):
				if msg.get("role") == "user" or (index < history_length and msg.get("role") == "assistant"):
					filtered.append(msg)
			elif hasattr(msg, "type") and msg.type in ("message", "reasoning"):
				filtered.append(msg)
		return filtered

	def _build_initial_messages(
		self,
		query: str,
		image: Optional[Any] = None,
		response_type: Optional[type] = None,
		history: Optional[Sequence[Dict[str, str]]] = None,
		context_block: str = "",
	) -> List[Dict[str, Any]]:
		"""Build initial messages for the conversation.
		
		Args:
			query: User's query.
			image: Optional image to include.
			history: Earlier turns (role/content), placed between the system prompt and the question.
			context_block: The rendered replay context, sent ahead of the question text.
		
		Returns:
			List of message dictionaries.
		"""
		# Build system prompt using response-specific template
		if response_type:
			system_content = build_system_prompt(response_type)
		else:
			# Fallback to generic prompt if no response type specified
			system_content = build_system_prompt(Response)

		messages = [
			{
				"role": "system",
				"content": system_content
			}
		]
		messages.extend(conversation_messages(history))
		
		# Build user message
		user_content = []
		
		# The replay view the question is asked in
		if context_block:
			user_content.append({
				"type": "input_text",
				"text": context_block
			})
		
		# Add text query
		user_content.append({
			"type": "input_text",
			"text": query
		})
		
		# Add image if provided
		if image is not None:
			image_data = self._process_image(image)
			if image_data:
				user_content.append({
					"type": "image_url",
					"image_url": {
						"url": f"data:image/jpeg;base64,{image_data}"
					}
				})
		
		messages.append({
			"role": "user",
			"content": user_content
		})
		
		return messages
	

	def _process_image(self, image: Any) -> Optional[str]:
		"""Process image to base64 string.
		
		Args:
			image: PIL Image, numpy array, or file path.
		
		Returns:
			Base64 encoded image string or None.
		"""
		try:
			# Handle different image types
			if isinstance(image, str) or isinstance(image, Path):
				# Load from file path
				pil_image = Image.open(image)
			elif isinstance(image, np.ndarray):
				# Convert numpy array to PIL
				pil_image = Image.fromarray(image)
			elif hasattr(image, 'save'):
				# Assume it's already a PIL Image
				pil_image = image
			else:
				self.logger.warning(f"Unknown image type: {type(image)}")
				return None
			
			# Convert to base64
			buffered = BytesIO()
			pil_image.save(buffered, format="JPEG")
			img_str = base64.b64encode(buffered.getvalue()).decode()
			
			return img_str
			
		except Exception as e:
			self.logger.error(f"Failed to process image: {e}")
			return None
	
	def _parse_response(self, content: str) -> Response:
		"""Parse the model's response into a Response object.
		
		Args:
			content: Model's response content.
		
		Returns:
			Response object.
		"""
		try:
			# Try to parse as JSON
			if '{' in content and '}' in content:
				# Extract JSON from content
				start = content.index('{')
				end = content.rindex('}') + 1
				json_str = content[start:end]
				data = json.loads(json_str)
				
				return Response(
					reasoning=data.get("reasoning", "No reasoning provided"),
					answer=data.get("answer", content)
				)
		except:
			pass
		
		# Fallback: use content as answer
		return Response(
			reasoning="Direct response without structured format",
			answer=content
		)
	
	def update_scene_graph(self, new_graph: sdsg.DynamicSceneGraph):
		"""Update the internal scene graph representation."""
		verify_embedding_provenance(
			new_graph,
			sentence_handler=self.sentence_handler,
			clip_handler=self.clip_handler,
			logger=self.logger,
		)
		self.scene_graph = new_graph
		# Update tool store with new scene graph
		self.tool_registry.set_scene_graph(new_graph)