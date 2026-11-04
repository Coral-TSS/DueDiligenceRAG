"""Thread-local binding from legacy SQL publication points to the durable stage owner."""
from contextlib import contextmanager
from contextvars import ContextVar

binding = ContextVar('dd_stage_publication', default=None)


class StageSuperseded(Exception):
    """The caller no longer owns this exact input; do not write its result."""


class StagePaused(Exception):
    """One stage stopped at a durable page/region boundary; other stages may continue."""


@contextmanager
def bind_publications(store, stage, tasks):
    token = binding.set((store,stage,{t['scope']['pages'][0]:t for t in tasks}))
    try:
        yield
    finally:
        binding.reset(token)
