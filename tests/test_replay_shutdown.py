"""Real-process signal and early-startup shutdown regressions (no Docker)."""
import os
from pathlib import Path
import re
import select
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from scripts.replay_web.server import ServerConfig, create_server

REPO = Path(__file__).resolve().parents[1]


class ReplayShutdownTest(unittest.TestCase):
	@unittest.skipUnless(os.name == 'posix', 'POSIX process signals')
	def test_inherited_ignored_ctrl_c_is_reset_and_the_port_is_released(self):
		with tempfile.TemporaryDirectory() as tmp:
			process = subprocess.Popen([sys.executable, '-u', '-m', 'scripts.replay_web.server',
				'--port', '0', '--output-root', tmp, '--state-root', tmp + '/state'],
				cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
				preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_IGN))
			try:
				self.assertTrue(select.select([process.stdout], [], [], 10)[0], 'server never started')
				line = process.stdout.readline().decode()
				port = int(re.search(r'127\.0\.0\.1:(\d+)/', line).group(1))
				process.send_signal(signal.SIGINT)
				try:
					status = process.wait(timeout=5)
				except subprocess.TimeoutExpired:
					self.fail('server inherited ignored SIGINT and stayed running')
				output = process.stdout.read().decode()
				self.assertEqual(status, 0, output)
				self.assertNotIn('Traceback', output)
				with socket.socket() as probe:
					self.assertNotEqual(probe.connect_ex(('127.0.0.1', port)), 0, 'server still listens after Ctrl+C')
			finally:
				if process.poll() is None:
					process.kill()
				process.wait()
				process.stdout.close()

	def test_interrupting_monitor_start_does_not_break_cleanup(self):
		with tempfile.TemporaryDirectory() as tmp:
			server = create_server(ServerConfig(repo_root=REPO, output_root=Path(tmp),
				state_root=Path(tmp) / 'state', distro='Ubuntu-20.04', port=0))
			try:
				with mock.patch('threading.Thread.start', side_effect=KeyboardInterrupt):
					with self.assertRaises(KeyboardInterrupt):
						server.serve_forever()
				server.server_close()
				self.assertEqual(server.socket.fileno(), -1)
			finally:
				server.socket.close()
