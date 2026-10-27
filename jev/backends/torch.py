import importlib.util
import logging
import threading
from array import array
from collections import OrderedDict
from pathlib import Path


from ..types import JevInputTooLong, JevResult, JevUnavailable
from . import Prepared

log = logging.getLogger(__name__)
REQUIRED = ("jev_style_decision.py", "config.json", "model.safetensors", "readout_config.json", "tokenizer.json")


def _snapshot(config):
    def complete(path): return path.is_dir() and all((path / f).is_file() for f in REQUIRED)
    configured = Path(config.local_path).expanduser() if config.local_path else None
    if configured and complete(configured): return configured
    from huggingface_hub import snapshot_download
    kwargs = {"repo_id": config.model}
    if config.model_revision: kwargs["revision"] = config.model_revision
    if configured:
        kwargs["local_dir"] = str(configured)
        try: path = Path(snapshot_download(**kwargs))
        except Exception as exc:
            log.error("Jev model download failed repo=%s path=%s error=%s: %s (check network, disk space, permissions and the Hugging Face token)", config.model, configured, type(exc).__name__, exc, exc_info=True)
            raise
    else:
        try:
            path = Path(snapshot_download(local_files_only=True, **kwargs))
            if complete(path): return path
            # A partial cached snapshot is a cache miss, not a failure: the download below fixes it.
            log.debug("cached Jev snapshot is incomplete path=%s, downloading from the hub", path)
        except Exception as exc:
            # shortcut: huggingface_hub cannot tell a cold cache from an unusable one and a cold
            # cache is normal, so this stays debug; the download below reports real failures.
            log.debug("Jev model is not in the local cache repo=%s, downloading from the hub error=%s: %s", config.model, type(exc).__name__, exc, exc_info=True)
        try: path = Path(snapshot_download(**kwargs))
        except Exception as exc:
            log.error("Jev model download failed repo=%s error=%s: %s (check network, disk space, permissions and the Hugging Face token)", config.model, type(exc).__name__, exc, exc_info=True)
            raise
    if not complete(path):
        missing = [name for name in REQUIRED if not (path / name).is_file()]
        log.error("incomplete Jev snapshot path=%s missing=%s repo=%s", path, ",".join(missing), config.model)
        raise JevUnavailable(f"incomplete Jev snapshot: {path}")
    return path


def _typed(q):
    crit = q.options
    if q.kind == "choice" and not isinstance(crit, dict): crit = {name: None for name in crit or ()}
    if q.kind == "score": crit = list(crit or ())
    return {"t": q.kind, "ins": q.text, "crit": crit}


def _runtime_options(config):
    device = None if config.device in ("", "auto") else config.device
    dtype = config.dtype
    if dtype == "bfloat16":
        try:
            import torch
            on_gpu = device == "cuda" or (device is None and torch.cuda.is_available())
            if on_gpu and not torch.cuda.is_bf16_supported(): dtype = "float16"
        except Exception as exc:  # Runtime can still select its supported dtype.
            log.error("Jev bf16 probe failed, keeping the configured dtype=%s device=%s error=%s: %s (a broken torch install would otherwise surface much later as a load failure)", dtype, device, type(exc).__name__, exc, exc_info=True)
    return device, dtype


def batch_layout(rendered_lengths, slots_per_item):
    padded = max(rendered_lengths, default=0)
    rows, cols, splits = [], [], []
    for row, slots in enumerate(slots_per_item):
        if isinstance(slots, int):
            splits.append(slots)
            continue
        splits.append(len(slots))
        rows.extend([row] * len(slots)); cols.extend(slots)
    return padded, list(zip(rows, cols)), splits


def cache_prefix_ids(renderer, serialize_state, max_bytes, stats=None):
    """Cache encoded state prefixes by the runtime's canonical serialization."""
    original = renderer.prefix_ids
    cached, used = OrderedDict(), 0
    stats = stats if stats is not None else {}
    lock = threading.Lock()
    def prefix_ids(state):
        nonlocal used
        key = serialize_state(state)
        with lock:
            if key in cached:
                ids = cached.pop(key); cached[key] = ids
                stats["token_cache_hits"] = stats.get("token_cache_hits", 0) + 1
                return list(ids)
            stats["token_cache_misses"] = stats.get("token_cache_misses", 0) + 1
        ids = array("i", original(state)); size = 4 * len(ids)
        if size <= max_bytes:
            with lock:
                if key in cached:
                    return list(cached[key])
                while cached and used + size > max_bytes:
                    _, old = cached.popitem(last=False); used -= 4 * len(old)
                cached[key] = ids; used += size
        return list(ids)
    renderer.prefix_ids = prefix_ids
    return prefix_ids


def _slots(rendered):
    slots = rendered.slots
    if isinstance(slots, dict): return list(slots.values())
    return list(slots)


def _batched_class(base):
    class BatchedJevStyleDecision(base):
        def _scores_many(self, rendered):
            """Right padding preserves each causal layer's real token states.

            All 24 layers are causal: 6 attention layers use a causal mask and 18
            Gated DeltaNet layers scan left to right with causal conv1d. Right padding
            follows real tokens, so it cannot alter their hidden states or position
            ids. Left padding shifts positions and runs padding through recurrent
            state before real tokens.
            """
            import torch
            from ..types import JevOutOfMemory
            try:
                lengths = [len(r.ids) for r in rendered]
                slots = [_slots(r) for r in rendered]
                padded = max(lengths, default=0)
                device = getattr(self, "device", None)
                device_type = str(device).split(":", 1)[0]
                pinned = device_type == "cuda"
                pad_id = getattr(getattr(self, "tokenizer", None), "eos_token_id", None) or 0
                ids = torch.full((len(rendered), padded), int(pad_id), dtype=torch.long,
                                 pin_memory=pinned)
                for row, item in enumerate(rendered): ids[row, :lengths[row]] = torch.as_tensor(item.ids, dtype=torch.long)
                ids = ids.to(device, non_blocking=pinned)
                _, indexes, _ = batch_layout(lengths, slots)
                row_idx = torch.tensor([row for row, _ in indexes], device=device)
                col_idx = torch.tensor([col for _, col in indexes], device=device)
                with torch.inference_mode():
                    hidden = self.model.model(input_ids=ids, use_cache=False).last_hidden_state
                    vectors = hidden[row_idx, col_idx].float()
                    scores = (vectors * self.direction.float()).sum(dim=-1).cpu().tolist()
                out, offset = [], 0
                for item_slots in slots:
                    out.append(scores[offset:offset + len(item_slots)]); offset += len(item_slots)
                return out
            except torch.cuda.OutOfMemoryError as exc:
                raise JevOutOfMemory(str(exc)) from exc
            except Exception as exc:
                # Not the allocator: a driver error, a device assert or a pinned-memory upload; the engine logs the batch consequence.
                log.error("Jev GPU forward pass failed device=%s items=%d error=%s: %s (this is not CUDA out of memory; check the driver, the device index and pinned memory)", getattr(self, "device", None), len(rendered), type(exc).__name__, exc, exc_info=True)
                raise
    return BatchedJevStyleDecision


class TorchBackend:
    name = "torch"

    def __init__(self, module, runtime): self.module, self.runtime = module, runtime

    @classmethod
    def from_config(cls, config):
        path = _snapshot(config)
        spec = importlib.util.spec_from_file_location("jev_style_decision", path / "jev_style_decision.py")
        if spec is None or spec.loader is None:
            log.error("Jev runtime script cannot be loaded file=%s repo=%s", path / "jev_style_decision.py", config.model)
            raise JevUnavailable("cannot load Jev runtime")
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        version = getattr(module, "TEMPLATE_VERSION", None)
        if version != "macjev-render-v1":
            log.error("unsupported Jev runtime TEMPLATE_VERSION found=%r expected=macjev-render-v1 file=%s repo=%s", version, path / "jev_style_decision.py", config.model)
            raise JevUnavailable(f"unsupported Jev TEMPLATE_VERSION: {version!r}")
        device, dtype = _runtime_options(config)
        if config.share_state:
            log.warning("WIKI_JEV_SHARE_STATE is unavailable: cache continuation exceeded the parity tolerance")
        runtime_class = _batched_class(module.JevStyleDecision)
        try:
            try: runtime = runtime_class(str(path), device=device, dtype=dtype)
            except TypeError as exc:
                log.error("Jev runtime constructor rejected device and dtype, retrying without them model=%s device=%s dtype=%s error=%s: %s (a runtime API change; the model may now load with different defaults)", config.model, device, dtype, type(exc).__name__, exc, exc_info=True)
                runtime = runtime_class(str(path))
        except Exception as exc:
            # torch names its allocator failure OutOfMemoryError; a load OOM is an environment
            # failure and must not read as a model-file or driver bug.
            name = type(exc).__name__
            if name == "OutOfMemoryError":
                log.error("CUDA out of memory loading the Jev model model=%s path=%s device=%s dtype=%s error=%s: %s (the load is not retried; free GPU memory or lower the batch budget)", config.model, path, device, dtype, name, exc, exc_info=True)
            else:
                log.error("Jev model load failed model=%s path=%s device=%s dtype=%s error=%s: %s", config.model, path, device, dtype, name, exc, exc_info=True)
            raise
        log.info("Jev torch model loaded model=%s path=%s device=%s dtype=%s", config.model, path, device, dtype)
        backend = cls(module, runtime)
        backend.stats = {"token_cache_hits": 0, "token_cache_misses": 0}
        try:
            import fla  # noqa: F401
            import causal_conv1d  # noqa: F401
            backend.stats["fast_kernels"] = True
        except ImportError:
            backend.stats["fast_kernels"] = False
        log.debug("Jev fast kernels available: %s", backend.stats["fast_kernels"])
        renderer = getattr(runtime, "renderer", None)
        serializer = getattr(module, "serialize_state", None)
        if renderer is not None and serializer is not None:
            cache_prefix_ids(renderer, serializer, int(config.token_cache_mb * 1024 * 1024), backend.stats)
        if config.compile:
            try:
                import torch
                runtime.model.model = torch.compile(runtime.model.model, mode="reduce-overhead")
            except Exception as exc: log.warning("torch.compile unavailable: %s", exc)
        return backend

    def prepare(self, request):
        typed = _typed(request.question)
        q = self.module.make_question(typed)
        try: rendered = self.runtime._render(request.state, q)
        except getattr(self.module, "InputBudgetError", ()): raise JevInputTooLong("Jev input exceeds runtime budget")
        parts = getattr(rendered, "parts", None)
        cost = sum(len(p.ids) for p in parts) if parts is not None else len(rendered.ids)
        return Prepared(request, cost, (rendered, q))

    def run(self, batch):
        try:
            scores = self.runtime._score_all([p.payload[0] for p in batch])
            out = []
            for p, score in zip(batch, scores):
                r, q = p.payload
                raw = self.runtime._result(r, q, score)
                probs = raw["probabilities"]
                out.append(JevResult(p.request.question.key, str(raw["answer"]), probs,
                                     float(raw["top_probability"]), float(raw["entropy_concentration"]),
                                     int(raw.get("input_tokens", p.cost))))
            return out
        except Exception as exc:
            from ..types import JevOutOfMemory
            if isinstance(exc, (JevInputTooLong, JevOutOfMemory)): raise
            # The engine reports this failure with the state and request count; this layer only wraps the cause.
            raise JevUnavailable(f"torch Jev runtime failed: {exc}") from exc

    def count_tokens(self, text):
        encoder = getattr(self.runtime, "encode", None)
        if callable(encoder):
            return len(encoder(text))
        for name in ("text_encoder", "encoder", "tokenizer"):
            encoder = getattr(self.runtime, name, None)
            if encoder is not None and hasattr(encoder, "encode"):
                return len(encoder.encode(text))
        log.error("Jev runtime exposes no token encoder backend=%s runtime=%s", self.name, type(self.runtime).__name__)
        raise JevUnavailable("runtime exposes no token encoder")
    def close(self):
        close = getattr(self.runtime, "close", None)
        if close: close()
