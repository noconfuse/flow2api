"""Regression test for browser_profile_runtime.py 错误的 `from .main` 懒导入。

之前的写法 `from .main import proxy_pool_service` 在尝试加载
`src.services.main` 时失败，导致浏览器凭证同步报错
"No module named 'src.services.main'"。
"""

import asyncio
import unittest

from src.services.browser_profile_runtime import _resolve_browser_proxy_args


class BrowserProxyArgsImportTests(unittest.IsolatedAsyncioTestCase):
    async def test_lazy_import_does_not_raise_module_not_found(self):
        """pool_service=None 时不应再触发 ModuleNotFoundError。"""
        # pool_service=None 触发懒导入路径
        args = await _resolve_browser_proxy_args(
            db=None,
            token_id=9999,
            pool_service=None,
        )
        # db=None 不应让此函数崩溃；期望返回空 list 或 proxy-server 列表
        self.assertIsInstance(args, list)


if __name__ == "__main__":
    unittest.main()