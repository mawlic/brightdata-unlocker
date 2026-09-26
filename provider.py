from __future__ import annotations

import asyncio
import json
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

import httpx

from agent.web_search_provider import WebSearchProvider, get_provider_env

_API_URL = "https://api.brightdata.com/request"
_DCA_TRIGGER_URL = "https://api.brightdata.com/dca/trigger_immediate"
_DCA_RESULT_URL = "https://api.brightdata.com/dca/get_result"
_DATASET_SCRAPE_URL = "https://api.brightdata.com/datasets/v3/scrape"
_DATASET_PROGRESS_URL = "https://api.brightdata.com/datasets/v3/progress"
_DATASET_SNAPSHOT_URL = "https://api.brightdata.com/datasets/v3/snapshot"
_BLOCK_MARKERS = (
    "вы не робот",
    "подтвердите, что запросы отправляли вы",
    "yandex smartcaptcha",
    "verify you are human",
    "are you a robot",
    "access to the site has been temporarily restricted",
    "доступ к сайту временно ограничен владельцем веб-ресурса",
)


def _read_mapping(value: Any, path: str) -> dict[Any, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise ValueError(f"{path} must be a mapping or JSON object") from exc
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be a mapping or JSON object")
    return value


def load_plugin_settings() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        config = load_config() or {}
    except Exception:
        config = {}
    raw = config.get("web", {}).get("brightdata_unlocker", {}) or {}
    timeout = float(raw.get("timeout", 120.0))
    max_concurrency = int(raw.get("max_concurrency", 3))
    max_attempts = int(raw.get("max_attempts", 2))
    retry_delay = float(raw.get("retry_delay", 1.0))
    data_format = str(raw.get("data_format", "markdown")).strip().lower()
    datasets_raw = _read_mapping(
        raw.get("datasets"), "web.brightdata_unlocker.datasets"
    )
    datasets = {
        str(pattern).strip().lower(): str(dataset_id).strip()
        for pattern, dataset_id in datasets_raw.items()
        if str(pattern).strip() and str(dataset_id).strip()
    }
    collectors_raw = _read_mapping(
        raw.get("collectors"), "web.brightdata_unlocker.collectors"
    )
    collectors = {
        str(pattern).strip().lower(): str(collector_id).strip()
        for pattern, collector_id in collectors_raw.items()
        if str(pattern).strip() and str(collector_id).strip()
    }
    collector_timeout = float(raw.get("collector_timeout", 120.0))
    collector_poll_interval = float(raw.get("collector_poll_interval", 1.0))
    if not 1 <= timeout <= 300:
        raise ValueError("web.brightdata_unlocker.timeout must be between 1 and 300")
    if not 1 <= max_concurrency <= 10:
        raise ValueError("web.brightdata_unlocker.max_concurrency must be between 1 and 10")
    if not 1 <= max_attempts <= 3:
        raise ValueError("web.brightdata_unlocker.max_attempts must be between 1 and 3")
    if not 0 <= retry_delay <= 10:
        raise ValueError("web.brightdata_unlocker.retry_delay must be between 0 and 10")
    if data_format not in {"", "markdown"}:
        raise ValueError("web.brightdata_unlocker.data_format must be markdown or empty")
    if not 5 <= collector_timeout <= 600:
        raise ValueError("web.brightdata_unlocker.collector_timeout must be between 5 and 600")
    if not 0.1 <= collector_poll_interval <= 30:
        raise ValueError(
            "web.brightdata_unlocker.collector_poll_interval must be between 0.1 and 30"
        )
    dataset_timeout = float(raw.get("dataset_timeout", 300.0))
    dataset_poll_interval = float(raw.get("dataset_poll_interval", 2.0))
    if not 30 <= dataset_timeout <= 900:
        raise ValueError(
            "web.brightdata_unlocker.dataset_timeout must be between 30 and 900"
        )
    if not 0.5 <= dataset_poll_interval <= 60:
        raise ValueError(
            "web.brightdata_unlocker.dataset_poll_interval must be between 0.5 and 60"
        )
    return {
        "zone": str(raw.get("zone", "web_unlocker1")).strip(),
        "render": bool(raw.get("render", True)),
        "timeout": timeout,
        "max_concurrency": max_concurrency,
        "max_attempts": max_attempts,
        "retry_delay": retry_delay,
        "data_format": data_format,
        "datasets": datasets,
        "dataset_timeout": dataset_timeout,
        "dataset_poll_interval": dataset_poll_interval,
        "collectors": collectors,
        "collector_timeout": collector_timeout,
        "collector_poll_interval": collector_poll_interval,
    }


class _VisibleHTMLParser(HTMLParser):
    _HIDDEN = {"script", "style", "noscript", "template", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden_depth = 0
        self.in_title = False
        self.in_body = False
        self.saw_body = False
        self.title_parts: list[str] = []
        self.all_parts: list[str] = []
        self.body_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if self.hidden_depth or tag in self._HIDDEN:
            self.hidden_depth += 1
            return
        if tag == "title":
            self.in_title = True
        elif tag == "body":
            self.in_body = True
            self.saw_body = True

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.hidden_depth:
            self.hidden_depth -= 1
            return
        if tag == "title":
            self.in_title = False
        elif tag == "body":
            self.in_body = False

    def handle_data(self, data: str) -> None:
        if self.hidden_depth:
            return
        if self.in_title:
            self.title_parts.append(data)
        self.all_parts.append(data)
        if self.in_body:
            self.body_parts.append(data)


def _visible_html(html: str) -> tuple[str, str]:
    parser = _VisibleHTMLParser()
    parser.feed(html)
    parser.close()
    title = re.sub(r"\s+", " ", " ".join(parser.title_parts)).strip()
    raw_parts = parser.body_parts if parser.saw_body else parser.all_parts
    lines = []
    previous = None
    for raw in raw_parts:
        line = re.sub(r"\s+", " ", raw).strip()
        if line and line != previous:
            lines.append(line)
            previous = line
    if title and (not lines or lines[0] != title):
        lines.insert(0, title)
    return title, "\n".join(lines)


def _compact_markdown(raw: str) -> tuple[str, str]:
    lines: list[str] = []
    previous_blank = False
    for line in raw.splitlines():
        stripped = line.lstrip()
        if stripped.startswith('{"widgets"') and len(stripped) > 500:
            continue
        cleaned = line.rstrip()
        is_blank = not cleaned.strip()
        if is_blank and previous_blank:
            continue
        lines.append(cleaned)
        previous_blank = is_blank
    content = "\n".join(lines).strip()
    title = ""
    h1_titles = []
    for line in lines:
        match = re.match(r"^#\s+(.+?)\s*$", line.strip())
        if match:
            h1_titles.append(match.group(1).strip())
    if h1_titles:
        title = next((candidate for candidate in h1_titles if len(candidate) >= 8), h1_titles[0])
    if not title:
        for line in lines:
            match = re.match(r"^#{2,6}\s+(.+?)\s*$", line.strip())
            if match:
                title = match.group(1).strip()
                break
    return title, content


def _error_class(status: int) -> str:
    if status in {401, 403, 407}:
        return "BRD_AUTH"
    if status in {400, 404, 405, 422}:
        return "BRD_PERMANENT"
    return "BRD_RETRIABLE"


class BrightDataUnlockerProvider(WebSearchProvider):
    def __init__(
        self,
        *,
        api_key: str | None = None,
        zone: str = "web_unlocker1",
        render: bool = True,
        timeout: float = 120.0,
        max_concurrency: int = 3,
        max_attempts: int = 2,
        retry_delay: float = 1.0,
        data_format: str = "markdown",
        datasets: dict[str, str] | None = None,
        dataset_timeout: float = 300.0,
        dataset_poll_interval: float = 2.0,
        collectors: dict[str, str] | None = None,
        collector_timeout: float = 120.0,
        collector_poll_interval: float = 1.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = (api_key or get_provider_env("BRIGHTDATA_API_KEY")).strip()
        self._zone = zone.strip()
        self._render = bool(render)
        self._timeout = float(timeout)
        self._max_concurrency = int(max_concurrency)
        self._max_attempts = int(max_attempts)
        self._retry_delay = float(retry_delay)
        self._data_format = str(data_format).strip().lower()
        self._datasets = {
            str(pattern).strip().lower(): str(dataset_id).strip()
            for pattern, dataset_id in (datasets or {}).items()
        }
        self._dataset_timeout = float(dataset_timeout)
        self._dataset_poll_interval = float(dataset_poll_interval)
        self._collectors = {
            str(pattern).strip().lower(): str(collector_id).strip()
            for pattern, collector_id in (collectors or {}).items()
        }
        self._collector_timeout = float(collector_timeout)
        self._collector_poll_interval = float(collector_poll_interval)
        self._transport = transport

    @property
    def name(self) -> str:
        return "brightdata-unlocker"

    @property
    def display_name(self) -> str:
        return "Bright Data Web Unlocker"

    def is_available(self) -> bool:
        return bool(self._api_key and self._zone)

    def supports_search(self) -> bool:
        return False

    def supports_extract(self) -> bool:
        return True

    def get_setup_schema(self) -> dict[str, Any]:
        return {
            "name": self.display_name,
            "badge": "free-tier",
            "tag": "Protected-page extraction through a configured Web Unlocker zone.",
            "env_vars": [
                {
                    "key": "BRIGHTDATA_API_KEY",
                    "prompt": "Bright Data API key",
                    "url": "https://brightdata.com/cp/setting/users",
                }
            ],
        }

    async def extract(self, urls: list[str], **kwargs: Any) -> list[dict[str, Any]]:
        if not self.is_available():
            return [self._failure(url, "BRD_AUTH: API key or zone is missing") for url in urls]
        semaphore = asyncio.Semaphore(self._max_concurrency)
        include_raw = bool(kwargs.get("include_raw", False))
        async with httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=True,
            transport=self._transport,
        ) as client:
            tasks = [
                self._extract_one(client, semaphore, url, include_raw=include_raw)
                for url in urls
            ]
            return list(await asyncio.gather(*tasks))

    async def _extract_one(
        self,
        client: httpx.AsyncClient,
        semaphore: asyncio.Semaphore,
        url: str,
        *,
        include_raw: bool,
    ) -> dict[str, Any]:
        dataset_id = self._dataset_for_url(url)
        dataset_error = ""
        if dataset_id:
            structured = await self._extract_dataset(
                client, semaphore, url, dataset_id
            )
            if not structured.get("error"):
                return structured
            dataset_error = str(structured.get("error") or "")
        collector_id = self._collector_for_url(url)
        collector_error = ""
        if collector_id:
            structured = await self._extract_collector(
                client, semaphore, url, collector_id
            )
            if not structured.get("error"):
                return structured
            collector_error = str(structured.get("error") or "")
        payload = {
            "zone": self._zone,
            "url": url,
            "format": "raw",
            "render": "true" if self._render else "false",
        }
        if self._data_format:
            payload["data_format"] = self._data_format
        for attempt in range(1, self._max_attempts + 1):
            try:
                async with semaphore:
                    response = await client.post(
                        _API_URL,
                        headers={
                            "Authorization": f"Bearer {self._api_key}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    )
            except httpx.TransportError as exc:
                result = self._failure(
                    url, f"BRD_RETRIABLE: {type(exc).__name__}"
                )
            else:
                internal_raw = response.headers.get("x-brd-status-code")
                try:
                    internal_status = (
                        int(internal_raw) if internal_raw else response.status_code
                    )
                except ValueError:
                    internal_status = response.status_code
                if response.status_code >= 400 or internal_status >= 400:
                    status = (
                        internal_status
                        if internal_status >= 400
                        else response.status_code
                    )
                    detail = (
                        response.headers.get("x-brd-error")
                        or response.text.strip()
                        or "request failed"
                    )
                    result = self._failure(
                        url, f"{_error_class(status)} {status}: {detail[:300]}"
                    )
                else:
                    raw_content = response.text
                    if not raw_content.strip():
                        result = self._failure(url, "BRD_RETRIABLE: empty response")
                    else:
                        content_type = response.headers.get("content-type", "")
                        if (
                            self._data_format == "markdown"
                            and "<html" not in raw_content[:1000].lower()
                        ):
                            title, content = _compact_markdown(raw_content)
                        elif (
                            "html" in content_type.lower()
                            or "<html" in raw_content[:1000].lower()
                        ):
                            title, content = _visible_html(raw_content)
                        else:
                            title, content = "", raw_content.strip()
                        lowered = content.lower()
                        marker = next(
                            (item for item in _BLOCK_MARKERS if item in lowered),
                            None,
                        )
                        if marker:
                            result = self._failure(
                                url,
                                "BRD_RETRIABLE: CAPTCHA or bot challenge "
                                f"detected ({marker})",
                            )
                        elif not content:
                            result = self._failure(
                                url, "BRD_RETRIABLE: empty extracted content"
                            )
                        else:
                            return {
                                "url": url,
                                "title": title,
                                "content": content,
                                "raw_content": raw_content if include_raw else "",
                                "metadata": {
                                    "backend": self.name,
                                    "content_type": content_type,
                                    "brd_status_code": internal_status,
                                    "attempts": attempt,
                                    **(
                                        {"dataset_fallback_error": dataset_error}
                                        if dataset_error
                                        else {}
                                    ),
                                    **(
                                        {"collector_fallback_error": collector_error}
                                        if collector_error
                                        else {}
                                    ),
                                },
                            }
            if (
                str(result.get("error") or "").startswith("BRD_RETRIABLE")
                and attempt < self._max_attempts
            ):
                if self._retry_delay:
                    await asyncio.sleep(self._retry_delay)
                continue
            if dataset_error or collector_error:
                metadata = result.setdefault("metadata", {})
                if dataset_error:
                    metadata["dataset_fallback_error"] = dataset_error
                if collector_error:
                    metadata["collector_fallback_error"] = collector_error
            return result
        return self._failure(url, "BRD_RETRIABLE: retry budget exhausted")

    def _dataset_for_url(self, url: str) -> str:
        parsed = urlsplit(url)
        target = f"{parsed.hostname or ''}{parsed.path}".lower()
        for pattern, dataset_id in sorted(
            self._datasets.items(), key=lambda item: len(item[0]), reverse=True
        ):
            if pattern.startswith("re:"):
                if re.search(pattern[3:], target):
                    return dataset_id
            elif target.startswith(pattern):
                return dataset_id
        return ""

    async def _extract_dataset(
        self,
        client: httpx.AsyncClient,
        semaphore: asyncio.Semaphore,
        url: str,
        dataset_id: str,
    ) -> dict[str, Any]:
        try:
            async with semaphore:
                response = await client.post(
                    _DATASET_SCRAPE_URL,
                    params={
                        "dataset_id": dataset_id,
                        "format": "json",
                        "include_errors": "true",
                    },
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=[{"url": url}],
                )
        except httpx.TransportError as exc:
            return self._failure(
                url, f"DATASET_RETRIABLE: {type(exc).__name__}"
            )
        body = response.text.strip()
        if response.status_code < 200 or response.status_code >= 300:
            return self._failure(
                url,
                f"DATASET_ERROR {response.status_code}: {body[:300]}",
            )
        try:
            data = response.json()
        except ValueError:
            return self._failure(url, "DATASET_ERROR: invalid JSON response")
        if response.status_code == 202:
            snapshot_id = str(data.get("snapshot_id") or "") if isinstance(data, dict) else ""
            if not snapshot_id:
                return self._failure(url, "DATASET_ERROR: missing snapshot_id")
            return await self._poll_dataset(
                client, semaphore, url, dataset_id, snapshot_id
            )
        return self._dataset_result(url, dataset_id, data)

    async def _poll_dataset(
        self,
        client: httpx.AsyncClient,
        semaphore: asyncio.Semaphore,
        url: str,
        dataset_id: str,
        snapshot_id: str,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        deadline = asyncio.get_running_loop().time() + self._dataset_timeout
        transport_errors = 0
        while True:
            try:
                async with semaphore:
                    progress = await client.get(
                        f"{_DATASET_PROGRESS_URL}/{snapshot_id}",
                        headers=headers,
                    )
            except httpx.TransportError as exc:
                transport_errors += 1
                if (
                    transport_errors < self._max_attempts
                    and asyncio.get_running_loop().time() < deadline
                ):
                    if self._retry_delay:
                        await asyncio.sleep(self._retry_delay)
                    continue
                return self._failure(
                    url, f"DATASET_RETRIABLE: {type(exc).__name__}"
                )
            transport_errors = 0
            if progress.status_code < 200 or progress.status_code >= 300:
                return self._failure(
                    url,
                    f"DATASET_ERROR {progress.status_code}: {progress.text[:300]}",
                )
            try:
                progress_data = progress.json()
            except ValueError:
                return self._failure(url, "DATASET_ERROR: invalid progress JSON")
            status = str(progress_data.get("status") or "").lower()
            if status == "ready":
                try:
                    async with semaphore:
                        snapshot = await client.get(
                            f"{_DATASET_SNAPSHOT_URL}/{snapshot_id}",
                            params={"format": "json"},
                            headers=headers,
                        )
                except httpx.TransportError as exc:
                    return self._failure(
                        url, f"DATASET_RETRIABLE: {type(exc).__name__}"
                    )
                if snapshot.status_code < 200 or snapshot.status_code >= 300:
                    return self._failure(
                        url,
                        f"DATASET_ERROR {snapshot.status_code}: {snapshot.text[:300]}",
                    )
                try:
                    data = snapshot.json()
                except ValueError:
                    return self._failure(
                        url, "DATASET_ERROR: invalid snapshot JSON"
                    )
                return self._dataset_result(url, dataset_id, data)
            if status == "failed":
                detail = str(progress_data.get("error") or "snapshot failed")
                return self._failure(url, f"DATASET_ERROR: {detail[:300]}")
            if status not in {"starting", "running"}:
                return self._failure(
                    url, f"DATASET_ERROR: unexpected snapshot status {status or 'empty'}"
                )
            if asyncio.get_running_loop().time() >= deadline:
                return self._failure(url, "DATASET_RETRIABLE: polling timeout")
            await asyncio.sleep(self._dataset_poll_interval)

    def _dataset_result(
        self, url: str, dataset_id: str, data: Any
    ) -> dict[str, Any]:
        if not isinstance(data, list) or not data:
            return self._failure(url, "DATASET_ERROR: empty result")
        first = data[0]
        if not isinstance(first, dict):
            return self._failure(url, "DATASET_ERROR: invalid result record")
        if first.get("error"):
            return self._failure(
                url, f"DATASET_ERROR: {str(first.get('error'))[:300]}"
            )
        title = str(
            first.get("title")
            or first.get("product_title")
            or first.get("name")
            or first.get("hotel_name")
            or ""
        )
        return {
            "url": url,
            "title": title,
            "content": json.dumps(data, ensure_ascii=False, indent=2),
            "raw_content": "",
            "metadata": {
                "backend": self.name,
                "dataset_id": dataset_id,
                "structured": True,
            },
        }

    def _collector_for_url(self, url: str) -> str:
        parsed = urlsplit(url)
        target = f"{parsed.hostname or ''}{parsed.path}".lower()
        for pattern, collector_id in sorted(
            self._collectors.items(), key=lambda item: len(item[0]), reverse=True
        ):
            if pattern.startswith("re:"):
                if re.search(pattern[3:], target):
                    return collector_id
            elif target.startswith(pattern):
                return collector_id
        return ""

    async def _extract_collector(
        self,
        client: httpx.AsyncClient,
        semaphore: asyncio.Semaphore,
        url: str,
        collector_id: str,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            async with semaphore:
                trigger = await client.post(
                    _DCA_TRIGGER_URL,
                    params={"collector": collector_id},
                    headers=headers,
                    json={"url": url},
                )
        except httpx.TransportError as exc:
            return self._failure(
                url, f"COLLECTOR_RETRIABLE: {type(exc).__name__}"
            )
        if trigger.status_code < 200 or trigger.status_code >= 300:
            return self._failure(
                url,
                f"COLLECTOR_ERROR {trigger.status_code}: {trigger.text[:300]}",
            )
        try:
            response_id = str(trigger.json().get("response_id") or "")
        except (ValueError, AttributeError):
            response_id = ""
        if not response_id:
            return self._failure(url, "COLLECTOR_ERROR: missing response_id")

        deadline = asyncio.get_running_loop().time() + self._collector_timeout
        while True:
            try:
                async with semaphore:
                    response = await client.get(
                        _DCA_RESULT_URL,
                        params={"response_id": response_id},
                        headers=headers,
                    )
            except httpx.TransportError as exc:
                return self._failure(
                    url, f"COLLECTOR_RETRIABLE: {type(exc).__name__}"
                )
            body = response.text.strip()
            if 200 <= response.status_code < 300 and body and body != "null":
                try:
                    data = response.json()
                except ValueError:
                    data = body
                if not (isinstance(data, dict) and data.get("pending") is True):
                    if (
                        isinstance(data, list)
                        and data
                        and all(
                            isinstance(item, dict) and item.get("error")
                            for item in data
                        )
                    ):
                        return self._failure(
                            url, f"COLLECTOR_ERROR: {str(data[0].get('error'))[:300]}"
                        )
                    first = data[0] if isinstance(data, list) and data else data
                    title = ""
                    if isinstance(first, dict):
                        title = str(
                            first.get("title")
                            or first.get("product_title")
                            or first.get("name")
                            or first.get("hotel_name")
                            or ""
                        )
                    return {
                        "url": url,
                        "title": title,
                        "content": json.dumps(data, ensure_ascii=False, indent=2),
                        "raw_content": "",
                        "metadata": {
                            "backend": self.name,
                            "collector_id": collector_id,
                            "structured": True,
                        },
                    }
            elif response.status_code in {400, 401, 403, 404, 407, 422}:
                return self._failure(
                    url,
                    f"COLLECTOR_ERROR {response.status_code}: {body[:300]}",
                )
            if asyncio.get_running_loop().time() >= deadline:
                return self._failure(url, "COLLECTOR_RETRIABLE: polling timeout")
            await asyncio.sleep(self._collector_poll_interval)

    @staticmethod
    def _failure(url: str, error: str) -> dict[str, Any]:
        return {"url": url, "title": "", "content": "", "error": error}
