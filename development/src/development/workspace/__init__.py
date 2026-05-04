"""Filesystem scratch area for in-flight builds (placeholder for v2.x).

The Coder/Tester/Packager stages will eventually need a sandboxed
working directory to write files into and run tests over. v2.x will
borrow round-robin's ``charlie/workspace.py`` sandbox pattern.
"""
