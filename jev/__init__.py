import logging
import threading

from .config import JevConfig
from .engine import JevEngine
from .types import JevInputTooLong, JevOutOfMemory, JevQuestion, JevRequest, JevResult, JevUnavailable

log = logging.getLogger(__name__)

_lock = threading.Lock()
_engine = None


def get_engine(config=None):
    global _engine
    with _lock:
        if _engine is None:
            config = config or JevConfig.from_env()
            from .backends import make_backend
            log.info("Jevを準備しています。初回のモデル読み込みには数分かかる場合があります: 実行方式=%s、モデル=%s、実行装置=%s、接続先=%s", config.backend, config.model, config.device, config.base_url or "-")
            _engine = JevEngine(make_backend(config), config)
        return _engine


def get_engine_for(settings):
    """Return the JEV engine selected by the resolved project settings."""

    return get_engine(JevConfig.from_settings(settings))


def reset_engine():
    global _engine
    with _lock:
        engine, _engine = _engine, None
    if engine: engine.close()


__all__ = ["JevConfig", "JevEngine", "JevQuestion", "JevRequest", "JevResult", "JevInputTooLong", "JevUnavailable", "JevOutOfMemory", "get_engine", "get_engine_for", "reset_engine"]
