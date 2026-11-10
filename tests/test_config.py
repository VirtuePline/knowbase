import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import ConfigError, load_settings

BASE = """
llm:
  model: deepseek-flash
  base_url: https://api.deepseek.com/v1/
  api_key: ""
retrieval:
  top_k: 6
  min_coverage: 0.32
chunking:
  chunk_size: 600
  chunk_overlap: 100
server:
  port: 8000
"""


class ConfigTest(unittest.TestCase):
    def _load(self, base: str, local: str | None = None):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.yaml"
            config.write_text(base, encoding="utf-8")
            local_path = Path(tmp) / "config.local.yaml"
            if local is not None:
                local_path.write_text(local, encoding="utf-8")
            return load_settings(config, local_path)

    def test_base_values_are_normalized(self):
        s = self._load(BASE)
        self.assertEqual(s.llm_base_url, "https://api.deepseek.com/v1")
        self.assertFalse(s.llm_available)
        self.assertEqual(s.excel_rows_per_chunk, 12, "未写出的项使用默认值")

    def test_local_file_overrides_only_given_keys(self):
        s = self._load(BASE, "llm:\n  api_key: sk-test\nretrieval:\n  top_k: 3\n")
        self.assertEqual(s.llm_api_key, "sk-test")
        self.assertEqual(s.llm_model, "deepseek-flash", "未覆盖的同段配置保持不变")
        self.assertEqual(s.top_k, 3)
        self.assertEqual(s.min_coverage, 0.32)

    def test_unknown_key_is_rejected(self):
        with self.assertRaises(ConfigError):
            self._load(BASE, "retrieval:\n  min_coverge: 0.5\n")

    def test_invalid_values_are_rejected(self):
        for local in ("retrieval:\n  min_coverage: 1.5\n",
                      "retrieval:\n  top_k: abc\n",
                      "chunking:\n  chunk_overlap: 600\n"):
            with self.assertRaises(ConfigError, msg=local):
                self._load(BASE, local)

    def test_broken_yaml_is_rejected(self):
        with self.assertRaises(ConfigError):
            self._load("llm: [unclosed\n")

    def test_missing_config_file_is_rejected(self):
        with self.assertRaises(ConfigError):
            load_settings(Path("不存在/config.yaml"), Path("不存在/config.local.yaml"))

    def test_web_search_defaults_allow_old_configs_without_network(self):
        with patch.dict("os.environ", {"KB_WEB_SEARCH_API_KEY": ""}):
            s = self._load(BASE)
        self.assertTrue(s.web_search_enabled)
        self.assertEqual(s.web_search_provider, "deepseek")   # 默认走 DeepSeek Search
        self.assertFalse(s.web_search_available)
        self.assertEqual(s.web_search_base_url, "https://api.deepseek.com")
        self.assertEqual(s.web_search_timeout, 15)
        self.assertEqual(s.web_search_max_results, 5)

    def test_deepseek_search_reuses_llm_api_key(self):
        with patch.dict("os.environ", {"KB_WEB_SEARCH_API_KEY": ""}):
            s = self._load(BASE, "llm:\n  api_key: sk-llm-key\n")
        self.assertEqual(s.web_search_api_key, "sk-llm-key", "DeepSeek 搜索与对话共用一个 Key")
        self.assertTrue(s.web_search_available)

    def test_tavily_provider_keeps_its_own_defaults(self):
        with patch.dict("os.environ", {"KB_WEB_SEARCH_API_KEY": ""}):
            s = self._load(BASE, "web_search:\n  provider: tavily\n")
        self.assertEqual(s.web_search_base_url, "https://api.tavily.com")
        self.assertFalse(s.web_search_available, "Tavily 不复用 llm.api_key")

    def test_invalid_web_search_provider_is_rejected(self):
        with self.assertRaises(ConfigError):
            self._load(BASE, "web_search:\n  provider: bing\n")

    def test_web_search_environment_overrides_private_key(self):
        with patch.dict("os.environ", {"KB_WEB_SEARCH_API_KEY": " env-test-key "}):
            s = self._load(BASE, "web_search:\n  api_key: local-test-key\n  base_url: https://search.example.com/\n")
        self.assertEqual(s.web_search_api_key, "env-test-key")
        self.assertTrue(s.web_search_available)
        self.assertEqual(s.web_search_base_url, "https://search.example.com")

    def test_web_search_can_be_disabled_with_a_key(self):
        s = self._load(BASE, "web_search:\n  enabled: false\n  api_key: test-key\n")
        self.assertFalse(s.web_search_available)

    def test_invalid_web_search_config_is_rejected(self):
        for local in ("timeout: 0", "timeout: 121", "max_results: 0", "max_results: 21",
                      "enabled: yesplease", "base_url: file:///tmp/search", "base_url: https://u:p@example.com",
                      "base_url: https://example.com?key=secret", "base_url: https://example.com:bad",
                      "max_result: 5"):
            with self.subTest(local=local):
                with self.assertRaises(ConfigError):
                    self._load(BASE, f"web_search:\n  {local}\n")


if __name__ == "__main__":
    unittest.main()
