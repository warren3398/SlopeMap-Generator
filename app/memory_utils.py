"""Return released native GIS buffers to the operating system when supported."""
import gc
import ctypes

try:
    _trim=ctypes.CDLL(None).malloc_trim
    _trim.argtypes=[ctypes.c_size_t]
    _trim.restype=ctypes.c_int
except (AttributeError,OSError):
    _trim=None


def release_memory(full=True):
    gc.collect() if full else gc.collect(0)
    if _trim is not None:
        _trim(0)
