"""Unified replay workspace: replay bundle contract, loopback service and its helpers.

The service side (manifest, paths, runs, server) uses only the Python 3.8 standard library so that it runs on the WSL
host; heavy work (bundle finalization, post-processing, scene questions) runs in the daaam-ws container.

With JELLYMAP_ROOT set (a JellyMap workspace), the modules are loaded from the shared copy in
$JELLYMAP_ROOT/sm-lab/viewers/replay/tools/replay_web; otherwise from this directory.
"""

import os as _os

_shared = _os.path.join(_os.environ.get('JELLYMAP_ROOT', ''), 'sm-lab/viewers/replay/tools/replay_web')
if _os.environ.get('JELLYMAP_ROOT') and _os.path.isfile(_os.path.join(_shared, '__init__.py')):
	__path__ = [_shared]
