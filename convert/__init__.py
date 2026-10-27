"""Standalone source-to-raw conversion phase."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from common.context import Context
from common.legacy import convert_mount, open_project, parse_document, read_text_source
from common.paths import DataLayout, raw_name_for
from common.storage import read_json, write_json_atomic, write_text_atomic


@dataclass(frozen=True)
class Config:
    parser_base_url: str = ""
    parser_fallback_base_url: str = ""
    parser_timeout: float = 7200.0
    parser_describe_images: bool = True
    chat_base_url: str = ""
    chat_api_key: str = "local"
    chat_model: str = ""
    request_timeout: int = 300
    settings: Any = None

    @classmethod
    def from_settings(cls, settings: Any) -> "Config":
        return cls(
            parser_base_url=str(getattr(settings, "parser_base_url", "")),
            parser_fallback_base_url=str(getattr(settings, "parser_fallback_base_url", "")),
            parser_timeout=float(getattr(settings, "parser_timeout", 7200.0)),
            parser_describe_images=bool(getattr(settings, "parser_describe_images", True)),
            chat_base_url=str(getattr(settings, "chat_base_url", "")),
            chat_api_key=str(getattr(settings, "chat_api_key", "local")),
            chat_model=str(getattr(settings, "chat_model", "")),
            request_timeout=int(getattr(settings, "wiki_request_timeout", 300)),
            settings=settings,
        )


@dataclass(frozen=True)
class Input:
    source: Path
    relative: str = ""
    previous_raw: Path | None = None


@dataclass(frozen=True)
class Result:
    raw_path: Path
    relative: str
    changed: bool
    source: Path


def _settings(cfg: Config) -> Any:
    if cfg.settings is not None:
        return cfg.settings
    return SimpleNamespace(
        parser_base_url=cfg.parser_base_url,
        parser_fallback_base_url=cfg.parser_fallback_base_url,
        parser_timeout=cfg.parser_timeout,
        parser_describe_images=cfg.parser_describe_images,
        chat_base_url=cfg.chat_base_url,
        chat_api_key=cfg.chat_api_key,
        chat_model=cfg.chat_model,
        wiki_request_timeout=cfg.request_timeout,
    )


def run(cfg: Config, inp: Input, out: Path, ctx: Context | None = None) -> Result:
    """Convert one source while preserving the historical raw naming rule."""

    source = Path(inp.source)
    if not source.is_file():
        raise FileNotFoundError(source)
    layout = DataLayout(Path(out)).ensure()
    relative = inp.relative or source.name
    raw_rel = str(Path(relative).parent / raw_name_for(Path(relative).name))
    raw_path = layout.raw_file(raw_rel)
    previous = inp.previous_raw or (raw_path if raw_path.exists() else None)
    settings = _settings(cfg)
    with (ctx.stage("convert", relative) if ctx is not None else _null_context()):
        if source.suffix.lower() in {".md", ".txt"}:
            text = read_text_source(source)
        else:
            if not cfg.parser_base_url:
                raise RuntimeError("parser_base_url is required for non-Markdown files")
            text = parse_document(
                source,
                base_url=cfg.parser_base_url,
                settings=settings,
                timeout_s=cfg.parser_timeout,
                previous_markdown=previous.read_text(encoding="utf-8") if previous and previous.exists() else None,
                describe_images=cfg.parser_describe_images,
            )
        changed = not raw_path.exists() or raw_path.read_text(encoding="utf-8") != text
        if changed:
            write_text_atomic(raw_path, text)
        state = read_json(layout.convert_state, default={})
        state[relative] = {"raw": raw_rel, "source": str(source), "size": source.stat().st_size, "mtime_ns": source.stat().st_mtime_ns}
        write_json_atomic(layout.convert_state, state)
    return Result(raw_path, raw_rel, changed, source)


def run_project(settings: Any, *, on_progress: Any = None) -> dict[str, Any]:
    """Compatibility batch entry point used by ``runner``."""

    return convert_mount(open_project(settings), settings=settings, on_progress=on_progress)


class _null_context:
    def __enter__(self):
        return None

    def __exit__(self, *_args):
        return False


__all__ = ["Config", "Input", "Result", "run", "run_project"]
