from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import httpx

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from provider import BrightDataUnlockerProvider, load_plugin_settings


def test_plugin_settings_read_unlocker_config(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "web": {
                "brightdata_unlocker": {
                    "zone": "web_unlocker1",
                    "render": True,
                    "timeout": 90,
                    "max_concurrency": 2,
                    "datasets": {
                        "www.ozon.ru/product/": "gd_ozon",
                        "www.wildberries.ru/catalog/": "gd_wb",
                    },
                    "dataset_timeout": 600,
                    "dataset_poll_interval": 5,
                }
            }
        },
    )

    assert load_plugin_settings() == {
        "zone": "web_unlocker1",
        "render": True,
        "timeout": 90.0,
        "max_concurrency": 2,
        "max_attempts": 2,
        "retry_delay": 1.0,
        "data_format": "markdown",
        "datasets": {
            "www.ozon.ru/product/": "gd_ozon",
            "www.wildberries.ru/catalog/": "gd_wb",
        },
        "dataset_timeout": 600.0,
        "dataset_poll_interval": 5.0,
        "collectors": {},
        "collector_timeout": 120.0,
        "collector_poll_interval": 1.0,
    }


def test_plugin_settings_accept_cli_json_mapping_strings(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "web": {
                "brightdata_unlocker": {
                    "datasets": '{"www.ozon.ru/product/":"gd_ozon"}',
                    "collectors": '{"market.yandex.ru/card/":"collector-1"}',
                }
            }
        },
    )

    settings = load_plugin_settings()

    assert settings["datasets"] == {"www.ozon.ru/product/": "gd_ozon"}
    assert settings["collectors"] == {"market.yandex.ru/card/": "collector-1"}


def test_plugin_registers_extraction_only_provider(monkeypatch):
    monkeypatch.setenv("BRIGHTDATA_API_KEY", "test-key")
    entry_path = PLUGIN_DIR / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "brightdata_unlocker_plugin",
        entry_path,
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    class Context:
        provider = None

        def register_web_search_provider(self, provider):
            self.provider = provider

    context = Context()
    module.register(context)

    assert context.provider is not None
    assert context.provider.name == "brightdata-unlocker"
    assert context.provider.supports_extract()
    assert not context.provider.supports_search()
    assert context.provider.is_available()


def test_regex_collector_pattern_matches_listing_but_not_search():
    provider = BrightDataUnlockerProvider(
        api_key="test-key",
        collectors={r"re:^www\.avito\.ru/.+_\d+$": "avito-collector"},
    )

    assert (
        provider._collector_for_url(
            "https://www.avito.ru/moskva/nastolnye_kompyutery/mini_pk_7983537442"
        )
        == "avito-collector"
    )
    assert provider._collector_for_url("https://www.avito.ru/moskva?q=mini+pc") == ""


def test_matching_dataset_returns_structured_json():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, dict(request.url.params)))
        assert json.loads(request.content) == [
            {"url": "https://www.ozon.ru/product/example-123/"}
        ]
        return httpx.Response(
            200,
            json=[{"name": "Ozon Product", "final_price": 61034}],
        )

    provider = BrightDataUnlockerProvider(
        api_key="test-key",
        datasets={"www.ozon.ru/product/": "gd_ozon"},
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(
        provider.extract(["https://www.ozon.ru/product/example-123/"])
    )[0]

    assert result["title"] == "Ozon Product"
    assert '"final_price": 61034' in result["content"]
    assert result["metadata"]["dataset_id"] == "gd_ozon"
    assert calls == [
        (
            "POST",
            "/datasets/v3/scrape",
            {"dataset_id": "gd_ozon", "format": "json", "include_errors": "true"},
        )
    ]


def test_dataset_empty_error_record_falls_back_to_web_unlocker():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/datasets/v3/scrape":
            return httpx.Response(
                200,
                json=[{"error": "Dead page detected", "error_code": None, "input": {"url": "x"}}],
            )
        if request.url.path == "/request":
            return httpx.Response(
                200,
                headers={"content-type": "text/plain", "x-brd-status-code": "200"},
                text="# Fallback Ozon\n\nЦена 30 000 ₽",
            )
        raise AssertionError(f"unexpected request: {request.url}")

    provider = BrightDataUnlockerProvider(
        api_key="test-key",
        datasets={"www.ozon.ru/product/": "gd_ozon"},
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(
        provider.extract(["https://www.ozon.ru/product/example-123/"])
    )[0]

    assert result["title"] == "Fallback Ozon"
    assert "Dead page detected" in result["metadata"]["dataset_fallback_error"]


def test_matching_collector_returns_structured_json():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.url.query))
        if request.url.path == "/dca/trigger_immediate":
            return httpx.Response(200, json={"response_id": "rid-1"})
        if request.url.path == "/dca/get_result":
            return httpx.Response(
                200,
                json=[{"product_title": "Product", "price_rub": 44016}],
            )
        raise AssertionError(f"unexpected request: {request.url}")

    provider = BrightDataUnlockerProvider(
        api_key="test-key",
        collectors={"market.yandex.ru/card/": "collector-1"},
        collector_poll_interval=0,
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(
        provider.extract(["https://market.yandex.ru/card/product/123"])
    )[0]

    assert result["title"] == "Product"
    assert '"price_rub": 44016' in result["content"]
    assert result["metadata"]["collector_id"] == "collector-1"
    assert [item[1] for item in calls] == [
        "/dca/trigger_immediate",
        "/dca/get_result",
    ]


def test_delayed_dataset_polls_snapshot_until_ready():
    paths = []
    progress_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal progress_calls
        paths.append(request.url.path)
        if request.url.path == "/datasets/v3/scrape":
            return httpx.Response(202, json={"snapshot_id": "s_1"})
        if request.url.path == "/datasets/v3/progress/s_1":
            progress_calls += 1
            status = "running" if progress_calls == 1 else "ready"
            return httpx.Response(200, json={"snapshot_id": "s_1", "status": status})
        if request.url.path == "/datasets/v3/snapshot/s_1":
            return httpx.Response(
                200,
                json=[{"name": "WB Product", "sale_price": 57322}],
            )
        raise AssertionError(f"unexpected request: {request.url}")

    provider = BrightDataUnlockerProvider(
        api_key="test-key",
        datasets={"www.wildberries.ru/catalog/": "gd_wb"},
        dataset_timeout=5,
        dataset_poll_interval=0,
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(
        provider.extract(["https://www.wildberries.ru/catalog/123/detail.aspx"])
    )[0]

    assert result["title"] == "WB Product"
    assert '"sale_price": 57322' in result["content"]
    assert result["metadata"]["dataset_id"] == "gd_wb"
    assert paths == [
        "/datasets/v3/scrape",
        "/datasets/v3/progress/s_1",
        "/datasets/v3/progress/s_1",
        "/datasets/v3/snapshot/s_1",
    ]


def test_dataset_progress_retries_one_transport_disconnect():
    progress_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal progress_calls
        if request.url.path == "/datasets/v3/scrape":
            return httpx.Response(202, json={"snapshot_id": "s_1"})
        if request.url.path == "/datasets/v3/progress/s_1":
            progress_calls += 1
            if progress_calls == 1:
                raise httpx.RemoteProtocolError("server disconnected")
            return httpx.Response(200, json={"snapshot_id": "s_1", "status": "ready"})
        if request.url.path == "/datasets/v3/snapshot/s_1":
            return httpx.Response(200, json=[{"name": "WB Product"}])
        raise AssertionError(f"unexpected request: {request.url}")

    provider = BrightDataUnlockerProvider(
        api_key="test-key",
        datasets={"www.wildberries.ru/catalog/": "gd_wb"},
        retry_delay=0,
        dataset_poll_interval=0,
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(
        provider.extract(["https://www.wildberries.ru/catalog/123/detail.aspx"])
    )[0]

    assert result["title"] == "WB Product"
    assert result["metadata"]["dataset_id"] == "gd_wb"
    assert progress_calls == 2


def test_dataset_progress_disconnect_falls_back_to_web_unlocker():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/datasets/v3/scrape":
            return httpx.Response(202, json={"snapshot_id": "s_1"})
        if request.url.path == "/datasets/v3/progress/s_1":
            raise httpx.RemoteProtocolError("server disconnected")
        if request.url.path == "/request":
            return httpx.Response(
                200,
                headers={"content-type": "text/plain", "x-brd-status-code": "200"},
                text="# Fallback product\n\nЦена 50 000 ₽",
            )
        raise AssertionError(f"unexpected request: {request.url}")

    provider = BrightDataUnlockerProvider(
        api_key="test-key",
        datasets={"www.wildberries.ru/catalog/": "gd_wb"},
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(
        provider.extract(["https://www.wildberries.ru/catalog/123/detail.aspx"])
    )[0]

    assert result["title"] == "Fallback product"
    assert "RemoteProtocolError" in result["metadata"]["dataset_fallback_error"]


def test_dataset_failure_falls_back_to_web_unlocker():
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/datasets/v3/scrape":
            return httpx.Response(503, text="dataset unavailable")
        if request.url.path == "/request":
            return httpx.Response(
                200,
                headers={"content-type": "text/plain", "x-brd-status-code": "200"},
                text="# Generic WB product\n\nЦена 12 345 ₽",
            )
        raise AssertionError(f"unexpected request: {request.url}")

    provider = BrightDataUnlockerProvider(
        api_key="test-key",
        datasets={"www.wildberries.ru/catalog/": "gd_wb"},
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(
        provider.extract(["https://www.wildberries.ru/catalog/123/detail.aspx"])
    )[0]

    assert result["title"] == "Generic WB product"
    assert "12 345 ₽" in result["content"]
    assert "dataset unavailable" in result["metadata"]["dataset_fallback_error"]
    assert paths == ["/datasets/v3/scrape", "/request"]


def test_dataset_and_unlocker_failures_preserve_both_causes():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/datasets/v3/scrape":
            return httpx.Response(503, text="dataset unavailable")
        if request.url.path == "/request":
            return httpx.Response(
                200,
                headers={
                    "x-brd-status-code": "502",
                    "x-brd-error": "unlocker timeout",
                },
                text="",
            )
        raise AssertionError(f"unexpected request: {request.url}")

    provider = BrightDataUnlockerProvider(
        api_key="test-key",
        datasets={"www.wildberries.ru/catalog/": "gd_wb"},
        max_attempts=1,
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(
        provider.extract(["https://www.wildberries.ru/catalog/123/detail.aspx"])
    )[0]

    assert "BRD_RETRIABLE 502" in result["error"]
    assert "dataset unavailable" in result["metadata"]["dataset_fallback_error"]


def test_collector_failure_falls_back_to_web_unlocker():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/dca/trigger_immediate":
            return httpx.Response(500, text="collector unavailable")
        if request.url.path == "/request":
            return httpx.Response(
                200,
                headers={"content-type": "text/plain", "x-brd-status-code": "200"},
                text="# Generic product\n\nЦена 10 ₽",
            )
        raise AssertionError(f"unexpected request: {request.url}")

    provider = BrightDataUnlockerProvider(
        api_key="test-key",
        collectors={"market.yandex.ru/card/": "collector-1"},
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(
        provider.extract(["https://market.yandex.ru/card/product/123"])
    )[0]

    assert result["title"] == "Generic product"
    assert "Цена 10 ₽" in result["content"]
    assert "collector unavailable" in result["metadata"]["collector_fallback_error"]


def test_markdown_mode_compacts_embedded_widget_json():
    captured = {}
    widget_blob = '{"widgets":{"huge":"' + ("x" * 1200) + '"}}'

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode()
        return httpx.Response(
            200,
            headers={"content-type": "text/html", "x-brd-status-code": "200"},
            text=(
                "# Язык\n\n## Нет в продаже\n\n# Product title\n\nЦена 44 016 ₽\n\n"
                + widget_blob
                + "\n\nВ наличии"
            ),
        )

    provider = BrightDataUnlockerProvider(
        api_key="test-key",
        data_format="markdown",
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(provider.extract(["https://example.com/product"]))[0]

    assert '"data_format":"markdown"' in captured["body"]
    assert result["title"] == "Product title"
    assert "44 016 ₽" in result["content"]
    assert "В наличии" in result["content"]
    assert '"widgets"' not in result["content"]


def test_successful_html_response_becomes_visible_text():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-key"
        assert b'"zone":"web_unlocker1"' in request.content
        return httpx.Response(
            200,
            headers={
                "content-type": "text/html; charset=utf-8",
                "x-brd-status-code": "200",
            },
            text="""
                <html><head><title>Product title</title><style>.x{}</style></head>
                <body><main>Price 42 000 ₽</main><script>secretState()</script></body></html>
            """,
        )

    provider = BrightDataUnlockerProvider(
        api_key="test-key",
        zone="web_unlocker1",
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(provider.extract(["https://example.com/product"]))[0]

    assert result["title"] == "Product title"
    assert result["content"] == "Product title\nPrice 42 000 ₽"
    assert "secretState" not in result["content"]
    assert result["metadata"]["backend"] == "brightdata-unlocker"


def test_internal_brd_error_inside_http_200_is_reported():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "x-brd-status-code": "502",
                "x-brd-error": "Navigation timeout",
            },
            text="",
        )

    provider = BrightDataUnlockerProvider(
        api_key="test-key",
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(provider.extract(["https://example.com/failure"]))[0]

    assert "BRD_RETRIABLE 502" in result["error"]
    assert "Navigation timeout" in result["error"]


def test_retriable_internal_error_is_retried_once():
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                headers={
                    "x-brd-status-code": "502",
                    "x-brd-error": "Timed out waiting for DomContentLoaded",
                },
                text="",
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/plain", "x-brd-status-code": "200"},
            text="Recovered content",
        )

    provider = BrightDataUnlockerProvider(
        api_key="test-key",
        max_attempts=2,
        retry_delay=0,
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(provider.extract(["https://example.com/retry"]))[0]

    assert calls == 2
    assert result["content"] == "Recovered content"
    assert result["metadata"]["attempts"] == 2


def test_successful_captcha_body_is_reported_as_retriable():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html", "x-brd-status-code": "200"},
            text="<html><title>Вы не робот?</title><body>Yandex SmartCaptcha</body></html>",
        )

    provider = BrightDataUnlockerProvider(
        api_key="test-key",
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(provider.extract(["https://example.com/captcha"]))[0]

    assert "BRD_RETRIABLE" in result["error"]
    assert "captcha" in result["error"].lower()


def test_result_order_matches_url_order_under_concurrency():
    async def handler(request: httpx.Request) -> httpx.Response:
        url = request.url.params.get("unused", "")
        return httpx.Response(200, text=url)

    def sync_handler(request: httpx.Request) -> httpx.Response:
        target = request.content.decode()
        return httpx.Response(
            200,
            headers={"content-type": "text/plain", "x-brd-status-code": "200"},
            text=target,
        )

    urls = ["https://example.com/one", "https://example.com/two"]
    provider = BrightDataUnlockerProvider(
        api_key="test-key",
        max_concurrency=2,
        transport=httpx.MockTransport(sync_handler),
    )
    results = asyncio.run(provider.extract(urls))

    assert [item["url"] for item in results] == urls
