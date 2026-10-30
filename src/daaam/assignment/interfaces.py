import multiprocessing as mp
from multiprocessing.synchronize import Event
import queue
import time
from typing import Any, Optional, Callable
import logging

from abc import ABC, abstractmethod
from typing import Dict, List, Set, Union
import json
import traceback

from daaam.interfaces import QueueWorkerProcessInterface
from daaam.assignment.models import AssignmentTask, AssignmentResult
from daaam.assignment.schemas import json_to_assignment_task
from daaam.utils.logging import setup_worker_logging, get_default_logger


class AssignmentWorkerInterface(QueueWorkerProcessInterface):
	"""
	Base class for assignment workers that process AssignmentTask objects
	and output SelectedGroup objects.
	"""
	
	def __init__(
		self,
		assignment_task_queue: mp.Queue,
		selected_groups_queue: mp.Queue,
		stop_event: Event,
		config: dict,
		ready_queue: Optional[mp.Queue] = None,
		name: Optional[str] = None
	):
		"""
		Initialize the assignment worker.
		
		Args:
			assignment_task_queue: Queue containing serialized AssignmentTask objects
			selected_groups_queue: Queue to write SelectedGroup lists to
			stop_event: Shared event to signal process termination
			config: Worker-specific configuration dictionary
			ready_queue: Optional queue to signal when worker is ready
			name: Optional name for the process
		"""
		super().__init__(
			input_queue=assignment_task_queue,
			output_queue=selected_groups_queue,
			stop_event=stop_event,
			name=name,
			timeout=0.1
		)
		self.config = config
		self.ready_queue = ready_queue
		self._setup_worker_logging()
		
		# Signal that worker is ready
		if self.ready_queue:
			worker_id = mp.current_process().name
			self.ready_queue.put(worker_id)
			if hasattr(self, 'worker_logger'):
				self.worker_logger.info(f"Worker {worker_id} signaled ready")
		
	def _setup_worker_logging(self):
		"""Setup logging for this worker."""
		
		log_dir = self.config.get("log_dir")
		if log_dir:
			self.worker_logger = setup_worker_logging(log_dir)
		else:
			self.worker_logger = get_default_logger()
			
	@abstractmethod
	def process_assignment_task(self, assignment_task: AssignmentTask, **kwargs) -> Optional[List]:
		"""
		Process an assignment task and return selected groups.
		
		Args:
			assignment_task: Deserialized AssignmentTask object
			**kwargs: Additional parameters specific to the worker implementation
			
		Returns:
			List of SelectedGroup objects or None if no valid groups
		"""
		pass
		
	def process_item(self, item: str) -> Optional[Union[List, AssignmentResult]]:
		"""
		Process a serialized AssignmentTask from the queue.
		
		Args:
			item: JSON serialized AssignmentTask string
			
		Returns:
			A terminal envelope for identified tasks; legacy groups or None otherwise.
		"""
		assignment_task, start = None, time.time()
		try:
			# deserialize assignment task
			assignment_task = json_to_assignment_task(item)
			self.worker_logger.debug(f"Deserialized AssignmentTask with {len(assignment_task.track_history)} frames")
			
			# call implementation-specific processing method
			groups = self.process_assignment_task(assignment_task)
			if assignment_task.assignment_id is not None:
				return AssignmentResult(assignment_task.assignment_id, groups or [], solve_start=start, solve_end=time.time())
			return groups
			
		except json.JSONDecodeError as e:
			self.worker_logger.error(f"ERROR decoding JSON data: {e}")
			return None
		except Exception as e:
			self.worker_logger.error(f"ERROR processing assignment task: {e}")
			self.worker_logger.error(traceback.format_exc())
			if assignment_task is not None and assignment_task.assignment_id is not None:
				return AssignmentResult(assignment_task.assignment_id, [], "failed", str(e), start, time.time())
			return None
