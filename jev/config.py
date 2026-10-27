import logging
import os
from dataclasses import dataclass, replace

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class JevConfig:
    backend: str = "shisa"
    model: str = "jev"
    tokenizer: str = "shisa-ai/shisa-de-2-FP8-dynamic"
    model_revision: str = ""
    local_path: str = ""
    device: str = "auto"
    dtype: str = "bfloat16"
    base_url: str = ""
    api_key: str = ""
    timeout: int = 60
    gguf_model: str = "chaoliangUNSW/Jev-Style-0.8B-Decision-v3-GGUF"
    gguf_local_path: str = ""
    gguf_quant: str = "F16"
    gguf_many_mode: str = "exact"
    gguf_binary: str = ""
    max_batch_requests: int = 64
    record_path: str = ""
    record_max: int = 300
    max_batch_tokens: int = 65536
    batch_wait_ms: int = 2
    token_cache_mb: int = 512
    share_state: bool = False
    share_state_max_forks: int = 8
    stats_seconds: int = 60
    compile: bool = False
    http_concurrency: int = 20  # hosted / llm2jev / shisa requests in flight

    @classmethod
    def from_env(cls, env=os.environ):
        get = env.get
        backend = (get("WIKI_JEV_BACKEND") or "shisa").strip().lower()
        if not (get("WIKI_JEV_BACKEND") or "").strip():
            log.warning("WIKI_JEV_BACKEND が未設定のため、指定の実行方式を使います: 方式=%s。接続先に合う WIKI_JEV_BACKEND を設定してください", backend)
        base, local = get("WIKI_JEV_BASE_URL", ""), get("WIKI_JEV_LOCAL_PATH", "")
        # hosted = the vanilla /v1/systemone API (stock llm2jev over sglang/vllm);
        # llm2jev = the custom batched /score scorer.
        if backend in {"systemone", "sglang", "vllm", "jpt"}:
            backend = "hosted"
        elif backend == "local":
            backend = "torch"
        elif backend == "auto":
            # Auto is cloud-only. A missing Shisa endpoint must fail closed rather
            # than silently loading the local Torch runtime.
            backend = "shisa"
        default_model = ("jev" if backend == "shisa"
                         else "chaoliangUNSW/Jev-Style-0.8B-Decision-v3")
        default_tokenizer = ("shisa-ai/shisa-de-2-FP8-dynamic"
                             if backend == "shisa" else "")
        fields = dict(
            backend=backend, model=get("WIKI_JEV_MODEL", default_model),
            tokenizer=get("WIKI_JEV_TOKENIZER", default_tokenizer),
            model_revision=get("WIKI_JEV_MODEL_REVISION", ""), local_path=local,
            device=get("WIKI_JEV_DEVICE", "auto").lower(), dtype=get("WIKI_JEV_DTYPE", "bfloat16").lower(),
            base_url=base, api_key=get("WIKI_JEV_API_KEY", ""),
            timeout=_int(get, "WIKI_JEV_TIMEOUT", 60),
            gguf_model=get("WIKI_JEV_GGUF_MODEL", "chaoliangUNSW/Jev-Style-0.8B-Decision-v3-GGUF"),
            gguf_local_path=get("WIKI_JEV_GGUF_LOCAL_PATH", ""), gguf_quant=get("WIKI_JEV_GGUF_QUANT", "F16"),
            gguf_many_mode=get("WIKI_JEV_GGUF_MANY_MODE", "exact"), gguf_binary=get("WIKI_JEV_SCORE_BIN", ""),
            max_batch_requests=_int(get, "WIKI_JEV_MAX_BATCH_REQUESTS", 64),
            record_path=get("WIKI_JEV_RECORD", ""), record_max=_int(get, "WIKI_JEV_RECORD_MAX", 300),
            max_batch_tokens=_int(get, "WIKI_JEV_MAX_BATCH_TOKENS", 65536),
            batch_wait_ms=_int(get, "WIKI_JEV_BATCH_WAIT_MS", 2),
            token_cache_mb=_int(get, "WIKI_JEV_TOKEN_CACHE_MB", 512),
            share_state=_bool(get("WIKI_JEV_SHARE_STATE", "0")),
            share_state_max_forks=_int(get, "WIKI_JEV_SHARE_STATE_MAX_FORKS", 8),
            stats_seconds=_int(get, "WIKI_JEV_STATS_SECONDS", 60),
            compile=_bool(get("WIKI_JEV_COMPILE", "0")),
            http_concurrency=_int(get, "WIKI_JEV_HTTP_CONCURRENCY" if get("WIKI_JEV_HTTP_CONCURRENCY")
                                  else "WIKI_JEV_LLM2JEV_CONCURRENCY", 20),
        )
        if backend not in {"torch", "gguf", "hosted", "llm2jev", "shisa"}:
            log.error("WIKI_JEV_BACKEND の値が不正です: 値=%r、指定可能=torch,gguf,hosted,llm2jev,shisa", backend)
            raise ValueError("WIKI_JEV_BACKEND must be torch, gguf, hosted, llm2jev or shisa")
        if fields["device"] not in {"auto", "cuda", "mps", "cpu"}:
            log.error("WIKI_JEV_DEVICE の値が不正です: 値=%r、指定可能=auto,cuda,mps,cpu", fields["device"])
            raise ValueError("WIKI_JEV_DEVICE must be auto, cuda, mps or cpu")
        if fields["dtype"] not in {"float32", "bfloat16", "float16"}:
            log.error("WIKI_JEV_DTYPE の値が不正です: 値=%r、指定可能=float32,bfloat16,float16", fields["dtype"])
            raise ValueError("WIKI_JEV_DTYPE must be float32, bfloat16 or float16")
        if fields["gguf_many_mode"] not in {"exact", "batched"}:
            log.error("WIKI_JEV_GGUF_MANY_MODE の値が不正です: 値=%r、指定可能=exact,batched", fields["gguf_many_mode"])
            raise ValueError("WIKI_JEV_GGUF_MANY_MODE must be exact or batched")
        for key in ("timeout", "max_batch_requests", "record_max", "max_batch_tokens", "token_cache_mb", "share_state_max_forks", "http_concurrency"):
            if fields[key] <= 0:
                log.error("Jevの設定が正の数ではありません: 設定=WIKI_JEV_%s、値=%s", key.upper(), fields[key])
                raise ValueError(f"WIKI_JEV_{key.upper()} must be positive")
        if fields["batch_wait_ms"] < 0 or fields["stats_seconds"] < 0:
            log.error("Jevの時間設定が負の値です: WIKI_JEV_BATCH_WAIT_MS=%s、WIKI_JEV_STATS_SECONDS=%s", fields["batch_wait_ms"], fields["stats_seconds"])
            raise ValueError("WIKI_JEV_BATCH_WAIT_MS and WIKI_JEV_STATS_SECONDS must be non-negative")
        if backend in {"hosted", "llm2jev", "shisa"}:
            if not base:
                log.warning("WIKI_JEV_BASE_URL が未設定のため、Jevへの要求は失敗します: 実行方式=%s", backend)
            if not fields["api_key"]:
                log.warning("WIKI_JEV_API_KEY が未設定です。認証ヘッダーなしで要求を送信します: 実行方式=%s", backend)
        log.info("Jevの設定を決定しました: 実行方式=%s、モデル=%s、実行装置=%s、データ型=%s、接続先=%s、一括要求上限=%d、一括トークン上限=%d、待機時間ms=%d、記録先=%s", fields["backend"], fields["model"], fields["device"], fields["dtype"], base or "-", fields["max_batch_requests"], fields["max_batch_tokens"], fields["batch_wait_ms"], fields["record_path"] or "-")
        return cls(**fields)

    @classmethod
    def from_settings(cls, settings):
        """Apply project settings after environment defaults."""

        config = cls.from_env()
        configured = getattr(settings, "wiki_jev_http_concurrency", config.http_concurrency)
        try:
            concurrency = int(configured)
        except (TypeError, ValueError) as exc:
            log.error("wiki_jev_http_concurrency が整数ではありません: 値=%r、エラー=%s: %s", configured, type(exc).__name__, exc, exc_info=True)
            raise
        resolved = replace(
            config,
            backend=str(getattr(settings, "wiki_jev_backend", config.backend)),
            model=str(getattr(settings, "wiki_jev_model", "") or config.model),
            tokenizer=str(getattr(settings, "wiki_jev_tokenizer", "") or config.tokenizer),
            base_url=str(getattr(settings, "wiki_jev_base_url", "") or config.base_url),
            http_concurrency=concurrency,
            gguf_local_path=str(getattr(
                settings, "wiki_jev_gguf_local_path", config.gguf_local_path,
            )),
            gguf_quant=str(getattr(settings, "wiki_jev_gguf_quant", config.gguf_quant)),
            gguf_many_mode=str(getattr(
                settings, "wiki_jev_gguf_many_mode", config.gguf_many_mode,
            )),
            gguf_binary=str(getattr(settings, "wiki_jev_score_bin", config.gguf_binary)),
        )
        if resolved.backend not in {"torch", "gguf", "hosted", "llm2jev", "shisa"}:
            # Project settings are applied after from_env, so they skip every WIKI_JEV_* check.
            log.error("wiki_jev_backend の値が不正なため、Jevを準備できません: 値=%r、指定可能=torch,gguf,hosted,llm2jev,shisa", resolved.backend)
        return resolved


def _int(get, name, default):
    value = get(name, str(default))
    try:
        return int(value)
    except ValueError as exc:
        # The rejected text is dropped by the raise below, so name it here.
        log.error("Jevの設定が整数ではありません: 設定=%s、値=%r、エラー=%s: %s", name, value, type(exc).__name__, exc, exc_info=True)
        raise ValueError(f"{name} must be an integer") from exc


def _bool(value):
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
