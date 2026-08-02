from __future__ import annotations

from .provider import BrightDataUnlockerProvider, load_plugin_settings


def register(ctx) -> None:
    ctx.register_web_search_provider(
        BrightDataUnlockerProvider(**load_plugin_settings())
    )
