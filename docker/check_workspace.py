"""Check the running development environment; failures return nonzero."""
import importlib
from pathlib import Path
import sys

failures = []
sources = Path('/opt/daaam_ws/src')
for name in ('daaam', 'daaam_ros', 'hydra', 'hydra_ros', 'spark_dsg', 'gtsam', 'khronos'):
    if not (sources / name / '.git').is_dir():
        failures.append('missing source repository: ' + name)
for name in ('rclpy', 'hydra_python', 'spark_dsg', 'daaam', 'torch', 'tensorrt', 'cvxpy'):
    try:
        importlib.import_module(name)
        print('OK import', name)
    except Exception as exc:
        failures.append(name + ': ' + str(exc))
try:
    import torch
    if not torch.cuda.is_available() or (torch.ones(8, device='cuda') * 2).sum().item() != 16:
        failures.append('CUDA kernel check failed')
    else:
        print('OK CUDA', torch.cuda.get_device_name(0))
    import cvxpy
    if 'GLPK_MI' not in cvxpy.installed_solvers():
        failures.append('GLPK_MI solver is missing')
except Exception as exc:
    failures.append(str(exc))
for failure in failures:
    print('FAIL', failure, file=sys.stderr)
print('Workspace check:', 'FAILED' if failures else 'PASS')
sys.exit(1 if failures else 0)
