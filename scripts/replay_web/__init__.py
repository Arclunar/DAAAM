"""Unified replay workspace: replay bundle contract, loopback service and its helpers.

The service side (manifest, paths, runs, server) uses only the Python 3.8 standard library so that it runs on the WSL
host; heavy work (bundle finalization, post-processing, scene questions) runs in the daaam-ws container.
"""
