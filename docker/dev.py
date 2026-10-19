"""Host-side development commands. Run with the WSL system Python, not the container venv."""
import json
from pathlib import Path
import re
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]


def capture(argv):
    return subprocess.run(argv, cwd=str(REPO), check=True, capture_output=True, text=True).stdout


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if not args or args[0] not in ('exec', 'build', 'check'):
            raise ValueError('usage: dev.py exec [container-id] | build [package ...] | check')
        action, rest = args[0], args[1:]
        if action == 'build':
            rest = [name for item in rest for name in item.split()]
            if any(not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', name) for name in rest):
                raise ValueError('build accepts ROS package names only')
            return subprocess.run(['docker', 'compose', 'run', '--rm', '-T',
                                   'daaam-ws', 'bash', 'docker/build_workspace.sh', *rest], cwd=str(REPO)).returncode
        config = json.loads(capture(['docker', 'compose', 'config', '--format', 'json']))
        if action == 'exec':
            if len(rest) > 1:
                raise ValueError('exec accepts at most one container id')
            ids = capture(['docker', 'ps', '-q', '--filter',
                           'label=com.docker.compose.project=' + config['name'], '--filter',
                           'label=com.docker.compose.service=daaam-ws']).split()
            selected = rest[0] if rest else None
            matches = [cid for cid in ids if selected is None or cid.startswith(selected)]
            if len(matches) != 1:
                raise ValueError('expected one running container in this workspace; use just run, '
                                 'or just exec <id> to select one of: ' + ', '.join(ids))
            return subprocess.run(['docker', 'exec', '-it', matches[0], 'bash'], cwd=str(REPO)).returncode
        if rest:
            raise ValueError('check takes no arguments')
        service = config['services']['daaam-ws']
        mounts = {item['target']: item for item in service['volumes']}
        src = mounts['/opt/daaam_ws/src']
        volume = mounts['/opt/daaam_ws']
        if src['type'] != 'bind' or not (Path(src['source']) / 'daaam').samefile(REPO):
            raise ValueError('source mount does not point to this checkout in a complete src workspace')
        if volume['type'] != 'volume':
            raise ValueError('dependency workspace must remain a named volume')
        physical_volume = config['volumes'][volume['source']]['name']
        capture(['docker', 'volume', 'inspect', physical_volume])
        print('Source:', src['source'], '| dependency volume:', physical_volume, flush=True)
        capture(['docker', 'image', 'inspect', service['image']])
        return subprocess.run(['docker', 'compose', 'run', '--rm', '-T',
                               'daaam-ws', 'python', 'docker/check_workspace.py'], cwd=str(REPO)).returncode
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as exc:
        print('ERROR:', exc, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
