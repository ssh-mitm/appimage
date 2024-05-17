import sys

__all__ = ["metadata", "resources"]

if sys.version_info >= (3, 10):
    from importlib import metadata, resources
else:
    import importlib_metadata as metadata
    import importlib_resources as resources
