"""统一服务只能启动一个监听器，包含开发重载与容器端口覆盖。"""
import unittest
from unittest.mock import patch

from app import cli


class ServeTest(unittest.TestCase):
    def serve(self, *args, reload=""):
        with patch("sys.argv", ["app.cli", "serve", *args]), \
                patch.dict("os.environ", {"KB_RELOAD": reload}), \
                patch("uvicorn.run") as run:
            cli.main()
        return run

    def test_default_starts_one_unified_service(self):
        run = self.serve()
        run.assert_called_once_with("app.main:app", host=cli.settings.host, port=cli.settings.port,
                                    log_level="info", reload=False)

    def test_container_can_override_the_single_listener_to_port_80(self):
        run = self.serve("--host", "0.0.0.0", "--port", "80")
        run.assert_called_once_with("app.main:app", host="0.0.0.0", port=80,
                                    log_level="info", reload=False)

    def test_reload_still_starts_only_the_unified_service(self):
        run = self.serve(reload="1")
        run.assert_called_once_with("app.main:app", host=cli.settings.host, port=cli.settings.port,
                                    log_level="info", reload=True)


if __name__ == "__main__":
    unittest.main()
