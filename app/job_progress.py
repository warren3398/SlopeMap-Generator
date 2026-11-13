"""Per-job stage estimates; context-local callbacks prevent cross-job updates."""
from contextvars import ContextVar
REPORTER=ContextVar('map_job_progress',default=None)

def report(percent,stage):
    callback=REPORTER.get()
    if callback:callback(max(0,min(99,int(percent))),stage)
