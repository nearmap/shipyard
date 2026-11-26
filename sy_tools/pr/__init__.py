"""The pull-request surface: writes to the code host's pull requests, never to the tracker.

The only package outside `sy_tools/tracker/` allowed to run `gh`, and only for pull requests: a pull
request lives on the code host whichever tracker a repository uses, so it is not tracker vocabulary.
"""
from __future__ import annotations
