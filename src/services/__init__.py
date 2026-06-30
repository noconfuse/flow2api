"""Services package exports.

Use lazy imports so one optional dependency does not block importing
unrelated service modules during debugging.
"""

__all__ = [
    "FlowClient",
    "ProxyManager",
    "LoadBalancer",
    "ConcurrencyManager",
    "TokenManager",
    "GenerationHandler",
]

_LAZY_EXPORTS = {
    "FlowClient": ".flow_client",
    "ProxyManager": ".proxy_manager",
    "LoadBalancer": ".load_balancer",
    "ConcurrencyManager": ".concurrency_manager",
    "TokenManager": ".token_manager",
    "GenerationHandler": ".generation_handler",
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
