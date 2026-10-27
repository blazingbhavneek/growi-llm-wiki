"""Configuration loading and phase-specific views.

``graph.config.Settings`` remains the compatibility loader until all consumers
have moved.  This module is the only place new phase code needs to know that
legacy detail.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .policy import resolve_policy

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    api_key: str
    model: str
    temperature: float = 0.7
    timeout: int = 300


@dataclass(frozen=True)
class EmbedConfig:
    backend: str
    base_url: str
    api_key: str
    model: str
    dimension: int = 768


@dataclass(frozen=True)
class JevConfig:
    backend: str
    gguf_path: str = ""
    quant: str = "F16"
    many_mode: str = "exact"
    score_binary: str = ""


def load_settings(project: str = "", *, overrides: dict[str, Any] | None = None) -> Any:
    """Load the existing INI/.env contract and apply explicit overrides."""

    from graph import config as legacy_config
    from graph.config import Settings

    if legacy_config.PROJECT_ROOT is None:
        legacy_config.PROJECT_ROOT = Path(__file__).resolve().parent.parent

    selector = project
    if project and Path(project).suffix.lower() == ".ini":
        config_path = Path(project).expanduser()
        if not config_path.is_absolute():
            config_path = (legacy_config.PROJECT_ROOT / config_path).resolve()
        selector = str(config_path)
    settings = Settings.from_env(selector)
    for key, value in (overrides or {}).items():
        if not hasattr(settings, key):
            log.error(
                "存在しない設定項目が指定されています: 設定=%s、値=%r、プロジェクト=%s",
                key,
                value,
                selector or "(not selected)",
            )
            raise ValueError(f"unknown setting: {key}")
        setattr(settings, key, value)
    log.info(
        "プロジェクト設定を読み込みました: 設定=%s、プロジェクト名=%s、データ保存先=%s",
        selector or "(not selected)",
        getattr(settings, "target_name", ""),
        getattr(settings, "data_root", ""),
    )
    return settings


def _number(cast: type[int] | type[float], settings: Any, name: str, default: Any) -> int | float:
    """int/float of a project setting; the failure names the setting, not just the value."""

    value = getattr(settings, name, default)
    try:
        return cast(value)
    except (TypeError, ValueError) as exc:
        log.error(
            "設定値を数値に変換できません: 設定=%s、値=%r、エラー=%s: %s",
            name,
            value,
            type(exc).__name__,
            exc,
            exc_info=True,
        )
        raise


def settings_for_root(settings: Any, root: Path) -> Any:
    """Point settings at another project root for a standalone ``--out`` run.

    The configured root is returned unchanged so its real mount path is kept and
    nothing new (such as ``<root>/mount``) appears in an existing data folder.
    """

    root = Path(root)
    configured = Path(str(getattr(settings, "data_root", ""))) / str(getattr(settings, "target_name", ""))
    if configured.resolve() == root.resolve():
        return settings
    try:
        (root / "mount").mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.error(
            "入力フォルダーを作成できません: パス=%s、エラー=%s: %s（容量・書き込み権限・接続状態を確認してください）",
            root / "mount",
            type(exc).__name__,
            exc,
            exc_info=True,
        )
        raise
    log.info(
        "今回の保存先を変更しました: プロジェクト名=%s、データ保存先=%s、入力フォルダー=%s",
        root.name,
        root.parent,
        root / "mount",
    )
    updates = {"data_root": str(root.parent), "target_name": root.name, "mount_path": str(root / "mount")}
    if hasattr(settings, "model_copy"):
        return settings.model_copy(update=updates)
    copied = type("Settings", (), {})()
    copied.__dict__.update(vars(settings))
    copied.__dict__.update(updates)
    return copied


def llm_config(settings: Any) -> LLMConfig:
    return LLMConfig(
        base_url=str(settings.chat_base_url),
        api_key=str(getattr(settings, "chat_api_key", "local")),
        model=str(settings.chat_model),
        temperature=_number(float, settings, "chat_temperature", 0.7),
        timeout=_number(int, settings, "wiki_request_timeout", 300),
    )


def embed_config(settings: Any) -> EmbedConfig:
    return EmbedConfig(
        backend=str(getattr(settings, "embed_backend", "server")),
        base_url=str(settings.embed_base_url),
        api_key=str(getattr(settings, "embed_api_key", "local")),
        model=str(settings.embed_model),
        dimension=_number(int, settings, "embed_dim", 768),
    )


def jev_config(settings: Any) -> JevConfig:
    return JevConfig(
        backend=str(getattr(settings, "wiki_jev_backend", "torch")),
        gguf_path=str(getattr(settings, "wiki_jev_gguf_local_path", "")),
        quant=str(getattr(settings, "wiki_jev_gguf_quant", "F16")),
        many_mode=str(getattr(settings, "wiki_jev_gguf_many_mode", "exact")),
        score_binary=str(getattr(settings, "wiki_jev_score_bin", "")),
    )


def policy_for(settings: Any) -> Any:
    return resolve_policy(getattr(settings, "policy", "standard"))


__all__ = ["EmbedConfig", "JevConfig", "LLMConfig", "embed_config", "jev_config", "llm_config", "load_settings", "policy_for", "settings_for_root"]
