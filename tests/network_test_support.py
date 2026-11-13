"""模拟在线服务的协议测试显式放行，避免单元测试读取真实运行时数据库。"""
from functools import wraps
from unittest.mock import patch


def online_api_test(case):
    setup = case.setUp

    @wraps(setup)
    def set_up(self):
        policy = patch("app.core.network_policy.external_apis_enabled", return_value=True)
        policy.start()
        self.addCleanup(policy.stop)
        setup(self)

    case.setUp = set_up
    return case
