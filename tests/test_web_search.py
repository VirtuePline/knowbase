"""联网搜索的协议、脱敏和失败边界；不依赖网络或 MongoDB。"""
import io
import json
import unittest
import urllib.error
from dataclasses import replace
from unittest.mock import MagicMock, patch

from app.config import settings
from app.core.search import client


class WebSearchTest(unittest.TestCase):
    def setUp(self):
        self.config = replace(settings, web_search_enabled=True, web_search_provider="tavily",
                              web_search_api_key="test-search-key",
                              web_search_base_url="https://api.tavily.com", web_search_max_results=2,
                              web_search_timeout=9)
        self.config_patch = patch.object(client, "settings", self.config)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)

    @staticmethod
    def response(payload):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(payload).encode("utf-8")
        return response

    def test_search_protocol_uses_snippets_and_only_sanitized_question(self):
        row = {"title": "厂商故障文档", "url": "https://example.com/support", "content": "重启采集服务。", "score": 0.9}
        with patch.object(client.urllib.request, "urlopen", return_value=self.response({"results": [row]})) as opening:
            hits = client.search("MES E-1024 超时 192.168.2.3 token=private-value")
        request = opening.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request.full_url, "https://api.tavily.com/search")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-search-key")
        self.assertEqual(payload, {"query": "MES E-1024 超时", "search_depth": "basic", "max_results": 2,
                                   "include_answer": False, "include_raw_content": False})
        self.assertEqual(opening.call_args.kwargs["timeout"], 9)
        self.assertEqual(hits, [client.WebResult("厂商故障文档", "https://example.com/support", "重启采集服务。", 0.9)])
        self.assertEqual(opening.call_count, 1, "不抓取搜索返回的网页")

    def test_deepseek_provider_posts_to_tools_search_and_prefers_summary(self):
        config = replace(self.config, web_search_provider="deepseek", web_search_api_key="sk-deepseek-key",
                         web_search_base_url="https://api.deepseek.com")
        row = {"title": "MES 采集服务超时排查", "url": "https://example.com/mes", "summary": "先看采集服务日志。",
               "snippet": "短摘要", "content": "网页正文", "rerank_score": 2.4}
        with patch.object(client, "settings", config):
            with patch.object(client.urllib.request, "urlopen", return_value=self.response({"results": [row]})) as opening:
                hits = client.search("MES 超时 192.168.1.9")
        request = opening.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request.full_url, "https://api.deepseek.com/v1/tools/search")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer sk-deepseek-key")
        self.assertEqual(payload, {"query": "MES 超时", "count": 2, "need_content": True})
        self.assertEqual(hits, [client.WebResult("MES 采集服务超时排查", "https://example.com/mes",
                                                 "先看采集服务日志。", 2.4)])

    def test_deepseek_result_falls_back_to_content_then_snippet(self):
        config = replace(self.config, web_search_provider="deepseek", web_search_api_key="sk-deepseek-key",
                         web_search_base_url="https://api.deepseek.com")
        rows = [{"title": "只有正文", "url": "https://example.com/1", "content": "正文摘要", "rerank_score": 1.8},
                {"title": "只有短摘要", "url": "https://example.com/2", "snippet": "短摘要"}]
        with patch.object(client, "settings", config):
            with patch.object(client.urllib.request, "urlopen", return_value=self.response({"results": rows})):
                hits = client.search("MES 超时")
        self.assertEqual([hit.content for hit in hits], ["正文摘要", "短摘要"])
        self.assertEqual([hit.score for hit in hits], [1.8, 0.0])

    def test_disabled_missing_key_and_blank_query_do_not_use_network(self):
        configs = [replace(self.config, web_search_enabled=False), replace(self.config, web_search_api_key=""), self.config]
        queries = ["MES 超时", "MES 超时", " "]
        with patch.object(client.urllib.request, "urlopen") as opening:
            for config, query in zip(configs, queries):
                with patch.object(client, "settings", config):
                    self.assertEqual(client.search(query), [])
            self.assertEqual(client.search("192.168.1.1 token=secret"), [])
        opening.assert_not_called()

    def test_sanitizer_redacts_private_addresses_mac_and_credentials(self):
        query = ('MES 超时 10.0.1.2/24:8080 172.16.0.2 127.0.0.1 169.254.1.2 '
                 'fd00::12 fe80::1 ::1 AA:BB:CC:DD:EE:FF 00-11-22-33-44-55 '
                 'password="two words" 密码：秘密 api_key=private token=abc Bearer abc.def '
                 'sk-1234567890 tvly-1234567890')
        self.assertEqual(client.sanitize_query(query), "MES 超时")
        self.assertEqual(client.sanitize_query("设备192.168.1.1报错 E-1024"), "设备 报错 E-1024")
        self.assertEqual(client.sanitize_query("服务器fd00::1故障"), "服务器 故障")
        self.assertEqual(client.sanitize_query("公开DNS 8.8.8.8"), "公开DNS 8.8.8.8")

    def test_results_reject_unsafe_urls_and_deduplicate(self):
        urls = ["javascript:alert(1)", "file:///etc/passwd", "//evil.com", "https://user:pass@example.com",
                "https://example.com:bad", "https://example.com/\npath", "https:///missing-host",
                "https://example.com/valid", "https://example.com/valid", "http://example.com/second"]
        rows = [{"title": "参考", "url": url, "content": "摘要"} for url in urls]
        with patch.object(client.urllib.request, "urlopen", return_value=self.response({"results": rows})):
            hits = client.search("MES 超时")
        self.assertEqual([hit.url for hit in hits], ["https://example.com/valid", "http://example.com/second"])

    def test_malformed_rows_do_not_break_valid_results(self):
        rows = [None, "bad", {"url": "https://example.com", "content": "", "title": "空摘要"},
                {"url": "https://example.com", "content": "摘要", "title": "参考", "score": "invalid"}]
        with patch.object(client.urllib.request, "urlopen", return_value=self.response({"results": rows})):
            hits = client.search("MES 超时")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].score, 0.0)

    def test_empty_results_return_empty_list(self):
        with patch.object(client.urllib.request, "urlopen", return_value=self.response({"results": []})):
            self.assertEqual(client.search("MES 超时"), [])

    def test_network_errors_hide_response_credentials_and_query(self):
        errors = [TimeoutError("private-query"), urllib.error.URLError("test-search-key"),
                  urllib.error.HTTPError("https://secret", 401, "secret-response", {}, io.BytesIO(b"sensitive-body"))]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                with patch.object(client.urllib.request, "urlopen", side_effect=error):
                    with self.assertRaises(client.WebSearchError) as raised:
                        client.search("private-query")
                self.assertNotIn("test-search-key", str(raised.exception))
                self.assertNotIn("private-query", str(raised.exception))
                self.assertNotIn("secret-response", str(raised.exception))
                self.assertNotIn("sensitive-body", str(raised.exception))

    def test_invalid_json_and_structure_raise_safe_error(self):
        for payload in (None, [], {}, {"results": None}, {"results": "wrong"}):
            with self.subTest(payload=payload):
                with patch.object(client.urllib.request, "urlopen", return_value=self.response(payload)):
                    with self.assertRaises(client.WebSearchError):
                        client.search("MES 超时")
        response = self.response({})
        response.__enter__.return_value.read.return_value = b"<html>secret-response</html>"
        with patch.object(client.urllib.request, "urlopen", return_value=response):
            with self.assertRaises(client.WebSearchError) as raised:
                client.search("MES 超时")
        self.assertNotIn("secret-response", str(raised.exception))

    def test_oversized_response_is_rejected(self):
        response = self.response({})
        response.__enter__.return_value.read.return_value = b"x" * (client._MAX_RESPONSE_BYTES + 1)
        with patch.object(client.urllib.request, "urlopen", return_value=response):
            with self.assertRaises(client.WebSearchError):
                client.search("MES 超时")


if __name__ == "__main__":
    unittest.main()
