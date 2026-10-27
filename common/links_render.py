"""Compatibility entry points for the pure managed-link renderer.

The renderer is deliberately side-effect free.  Its implementation is still
in the historical linker module during the migration; these functions give
the publisher and future phase code one stable import surface.
"""

from __future__ import annotations

from typing import Any


def render_page(*args: Any, **kwargs: Any) -> str:
    from graph.linker.render import render_page as render_legacy

    return render_legacy(*args, **kwargs)


def footer_edges(*args: Any, **kwargs: Any) -> Any:
    from graph.linker.render import footer_edges as footer_legacy

    return footer_legacy(*args, **kwargs)


def relative_link(*args: Any, **kwargs: Any) -> str:
    from graph.linker.render import relative_link as relative_legacy

    return relative_legacy(*args, **kwargs)


__all__ = ["footer_edges", "relative_link", "render_page"]
