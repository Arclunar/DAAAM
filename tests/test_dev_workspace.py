"""Behavioral checks for project-scoped container access and incremental build commands."""
import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]


def load_dev():
    spec = importlib.util.spec_from_file_location('daaam_dev', REPO / 'docker/dev.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DevWorkspaceTest(unittest.TestCase):
    def test_exec_scopes_selection_and_preserves_failure_code(self):
        dev = load_dev()
        calls = []
        def call(argv, **kwargs):
            calls.append(argv)
            if argv[:3] == ['docker', 'compose', 'config']:
                return subprocess.CompletedProcess(argv, 0, '{"name":"my-workspace"}', '')
            if argv[:2] == ['docker', 'ps']:
                return subprocess.CompletedProcess(argv, 0, 'abc123\n', '')
            return subprocess.CompletedProcess(argv, 19)
        with patch.object(dev.subprocess, 'run', side_effect=call):
            self.assertEqual(dev.main(['exec']), 19)
        self.assertIn('label=com.docker.compose.project=my-workspace', calls[1])
        self.assertIn('label=com.docker.compose.service=daaam-ws', calls[1])
        self.assertEqual(calls[2][-2:], ['abc123', 'bash'])

    def test_exec_refuses_ambiguous_or_foreign_container(self):
        dev = load_dev()
        for selected in ([], ['foreign']):
            with patch.object(dev, 'capture', side_effect=['{"name":"p"}', 'a\nb\n']):
                with patch.object(dev.subprocess, 'run') as runner:
                    self.assertEqual(dev.main(['exec'] + selected), 1)
                    runner.assert_not_called()

    def test_build_preserves_arguments_without_shell_expansion(self):
        dev = load_dev()
        with patch.object(dev.subprocess, 'run', return_value=subprocess.CompletedProcess([], 7)) as runner:
            self.assertEqual(dev.main(['build', 'hydra_ros', 'spark_dsg']), 7)
        self.assertEqual(runner.call_args[0][0][-4:],
                         ['bash', 'docker/build_workspace.sh', 'hydra_ros', 'spark_dsg'])

    def test_build_rejects_options_as_package_names(self):
        dev = load_dev()
        with patch.object(dev.subprocess, 'run') as runner:
            self.assertEqual(dev.main(['build', '--cmake-clean-cache']), 1)
            runner.assert_not_called()

    def test_build_script_selects_dependency_closure_and_propagates_failure(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'colcon'
            p.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\nexit 23\n')
            p.chmod(0o755)
            env = dict(os.environ, PATH=d + os.pathsep + os.environ['PATH'], DAAAM_WS=d)
            result = subprocess.run(['bash', str(REPO / 'docker/build_workspace.sh'), 'hydra_ros'],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 23, result.stderr)
            self.assertIn('--packages-up-to\nhydra_ros', result.stdout)
            self.assertIn('--base-paths\nsrc', result.stdout)

    def test_snapshot_uses_active_sources_instead_of_stale_volume_sources(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            volume, sources, target = root/'volume', root/'src', root/'image'
            for p in (volume/'.venv', volume/'src/hydra', volume/'install', sources/'hydra', sources/'daaam'):
                p.mkdir(parents=True, exist_ok=True)
            (volume/'src/hydra/version.txt').write_text('old')
            (volume/'install/setup.bash').write_text('installed')
            (sources/'hydra/version.txt').write_text('new')
            (sources/'daaam/pyproject.toml').write_text('project')
            (sources/'daaam/.env').write_text('secret')
            result = subprocess.run(['bash', str(REPO/'docker/copy_workspace.sh'), str(volume),
                                     str(sources), str(sources/'daaam'), str(target)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((target/'src/hydra/version.txt').read_text(), 'new')
            self.assertEqual((target/'install/setup.bash').read_text(), 'installed')
            self.assertFalse((target/'src/daaam/.env').exists())

    def test_standalone_hf_export_resolves_compose_volume_and_never_creates_missing_volume(self):
        import os
        import shlex
        import tempfile
        config = '{"services":{"daaam-ws":{"volumes":[{"target":"/opt/daaam_ws/src","type":"bind","source":"/active/src"},{"target":"/opt/daaam_ws","type":"volume","source":"ws"}]}},"volumes":{"ws":{"name":"custom-real-volume"}}}'
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            out = root/'out'
            out.mkdir()
            (out/'bundle.env').write_text('IMAGE_ID=existing-image\n')
            fake = root/'docker'
            fake.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$FAKE_LOG"\n'
                            'if [ "$1 $2" = "compose config" ]; then printf "%s\\n" ' + shlex.quote(config) + '; exit 0; fi\n'
                            'if [ "$1 $2" = "volume inspect" ]; then exit 1; fi\nexit 91\n')
            fake.chmod(0o755)
            (root/'zstd').write_text('#!/bin/sh\nexit 90\n')
            (root/'zstd').chmod(0o755)
            env = dict(os.environ, PATH=d+os.pathsep+os.environ['PATH'], FAKE_LOG=str(root/'calls'))
            env.pop('WORKSPACE_VOLUME', None)
            result = subprocess.run(['bash', str(REPO/'docker/build_full_image.sh'), 'export', str(out), 'hf'],
                                    env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            calls = (root/'calls').read_text()
            self.assertIn('volume inspect custom-real-volume', calls)
            self.assertNotIn('run ', calls)


if __name__ == '__main__':
    unittest.main()
