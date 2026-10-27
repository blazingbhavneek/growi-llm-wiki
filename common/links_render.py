"""Compatibility entry points for the pure managed-link renderer.

The renderer is deliberately side-effect free.  Its implementation is still
in the historical linker module during the migration; these functions give
the publisher and future phase code one stable import surface.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def render_page(*args: Any, **kwargs: Any) -> str:
    from graph.linker.render import render_page as render_legacy

    try:
        return render_legacy(*args, **kwargs)
    except Exception as exc:
        log.error("リンク処理でページを整形できません: ページ=%s、エラー=%s: %s", kwargs.get("page_rel", ""), type(exc).__name__, exc, exc_info=True)
        raise


def footer_edges(*args: Any, **kwargs: Any) -> Any:
    from graph.linker.render import footer_edges as footer_legacy

    try:
        return footer_legacy(*args, **kwargs)
    except Exception as exc:
        log.error("ページ末尾の関連リンクを作成できません: ページ=%s、エラー=%s: %s", kwargs.get("page_rel", ""), type(exc).__name__, exc, exc_info=True)
        raise


def relative_link(*args: Any, **kwargs: Any) -> str:
    from graph.linker.render import relative_link as relative_legacy

    return relative_legacy(*args, **kwargs)


__all__ = ["footer_edges", "relative_link", "render_page"]
