"""Core package exports."""

__all__ = ["config", "AuthManager", "verify_api_key_header", "debug_logger"]

_LAZY_EXPORTS = {
    "config": ".config",
    "AuthManager": ".auth",
    "verify_api_key_header": ".auth",
    "debug_logger": ".logger",
}


def __getattr__(name):
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    from importlib import import_module

    module = import_module(module_name, __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value
