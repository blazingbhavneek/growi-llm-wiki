"""Standalone index and mokuji phase facade."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from common.context import Context
from common.legacy import index as index_project
from common.paths import DataLayout
from common.settings import settings_for_root

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Config:
    related_documents: bool = False
    publish: bool = False
    settings: Any = None

    @classmethod
    def from_settings(cls, settings: Any, *, publish: bool = False) -> "Config":
        return cls(bool(getattr(settings, "wiki_index_related_docs", False)), publish, settings)


@dataclass(frozen=True)
class Input:
    root: Path
    scope: tuple[str, ...] = ()


@dataclass(frozen=True)
class Result:
    done: tuple[dict[str, Any], ...]
    failures: tuple[str, ...]
    scope: tuple[str, ...]


def run(cfg: Config, inp: Input, out: Path, ctx: Context | None = None) -> Result:
    layout = DataLayout(Path(out)).ensure()
    settings = cfg.settings or SimpleNamespace(
        data_root=str(layout.root.parent),
        target_name=layout.root.name,
        mount_path=str(layout.root / "mount"),
        wiki_index_related_docs=cfg.related_documents,
        growi_url="",
    )
    if cfg.settings is not None:
        settings = settings_for_root(settings, layout.root)
    else:
        # growi_url="" is only harmless for a metadata-only run; a publish run cannot reach GROWI.
        if cfg.publish:
            log.error(
                "GROWI_URL が未設定のため、公開用の目次設定ではGROWIに公開できません。--config を指定してください: 出力先=%s",
                out,
            )
        else:
            log.debug(
                "プロジェクト設定が指定されていないため、GROWIに公開しない目次設定を作成しました: 出力先=%s",
                out,
            )
        try:
            layout.mount.mkdir(exist_ok=True)
        except OSError as exc:
            log.error(
                "入力フォルダーを作成できません: パス=%s、エラー=%s: %s（容量・書き込み権限・接続状態を確認してください）",
                layout.mount,
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            raise
    with (ctx.stage("index", "<scope>") if ctx is not None else _null_context()):
        result = index_project(settings, only=list(inp.scope) or None, publish=cfg.publish)
    return Result(tuple(result.get("done", [])), tuple(result.get("failures", [])), inp.scope)


def run_project(settings: Any, *, only: list[str] | None = None, publish: bool = True, on_progress: Any = None) -> dict[str, Any]:
    return index_project(settings, only=only, publish=publish, on_progress=on_progress)


class _null_context:
    def __enter__(self):
        return None

    def __exit__(self, *_args):
        return False


__all__ = ["Config", "Input", "Result", "run", "run_project"]
