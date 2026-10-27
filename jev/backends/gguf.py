import importlib.util
import logging
from pathlib import Path

from ..types import JevInputTooLong, JevResult, JevUnavailable
from . import Prepared
from .torch import TorchBackend, _typed, cache_prefix_ids

log = logging.getLogger(__name__)


class GGUFBackend(TorchBackend):
    name = "gguf"
    # jev-score decodes each request in its own context: there is no padded batch to budget, and
    # splitting one state's questions across requests would decode the state again.
    max_batch_tokens = 1 << 30
    pads_batches = False

    @classmethod
    def from_config(cls, config):
        files = ("jev_style_decision_gguf.py", "readout_config.json", "tokenizer/tokenizer.json",
                 f"Jev-Style-0.8B-Decision-v3-{config.gguf_quant}.gguf")
        folder = Path(config.gguf_local_path).expanduser() if config.gguf_local_path else None
        if not folder or not all((folder / name).is_file() for name in files):
            from huggingface_hub import snapshot_download
            kwargs = {"repo_id": config.gguf_model}
            if config.model_revision: kwargs["revision"] = config.model_revision
            if folder: kwargs["local_dir"] = str(folder)
            else: kwargs["local_files_only"] = True
            try:
                try: folder = Path(snapshot_download(**kwargs))
                except Exception as exc:
                    # A cold HF cache and an unusable one raise the same way; a cold cache is a normal miss.
                    if kwargs.get("local_files_only"):
                        log.debug("Jev GGUF snapshot is not in the local cache repo=%s, downloading from the hub error=%s: %s", config.gguf_model, type(exc).__name__, exc, exc_info=True)
                    else:
                        log.error("Jev GGUF snapshot download failed, retrying once repo=%s path=%s error=%s: %s (check network, disk space, permissions and the Hugging Face token)", config.gguf_model, folder, type(exc).__name__, exc, exc_info=True)
                    kwargs.pop("local_files_only", None)
                    folder = Path(snapshot_download(**kwargs))
            except Exception as exc:
                log.error("Jev GGUF model download failed repo=%s path=%s error=%s: %s (there is no usable GGUF snapshot, so the second attempt failed too)", config.gguf_model, folder, type(exc).__name__, exc, exc_info=True)
                raise
        if not all((folder / name).is_file() for name in files):
            missing = [name for name in files if not (folder / name).is_file()]
            log.error("incomplete GGUF snapshot path=%s missing=%s repo=%s", folder, ",".join(missing), config.gguf_model)
            raise JevUnavailable(f"incomplete GGUF snapshot: {folder}")
        spec = importlib.util.spec_from_file_location("jev_style_decision_gguf", folder / files[0])
        if spec is None or spec.loader is None:
            log.error("Jev GGUF runtime script cannot be loaded file=%s repo=%s", folder / files[0], config.gguf_model)
            raise JevUnavailable("cannot load GGUF runtime")
        try: module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        except Exception as exc:
            log.error("Jev GGUF runtime script could not be imported file=%s repo=%s error=%s: %s (a missing llama.cpp binding is reported here, not later)", folder / files[0], config.gguf_model, type(exc).__name__, exc, exc_info=True)
            raise
        kw = {"quant": config.gguf_quant, "many_mode": config.gguf_many_mode}
        if config.gguf_binary: kw["binary"] = config.gguf_binary
        try:
            try: runtime = module.JevStyleDecisionGGUF(str(folder), **kw)
            except TypeError as exc:
                log.error("Jev GGUF runtime constructor rejected quant, many_mode and binary, retrying without them path=%s quant=%s error=%s: %s (a runtime API change; the model may load with different defaults)", folder, config.gguf_quant, type(exc).__name__, exc, exc_info=True)
                runtime = module.JevStyleDecisionGGUF(str(folder))
        except Exception as exc:
            log.error("Jev GGUF model load failed path=%s quant=%s binary=%s error=%s: %s (check the model file, the runtime binary on PATH and free memory)", folder, config.gguf_quant, config.gguf_binary, type(exc).__name__, exc, exc_info=True)
            raise
        backend = cls(module, runtime)
        backend.stats = {"token_cache_hits": 0, "token_cache_misses": 0}
        if getattr(runtime, "renderer", None) is not None and hasattr(module, "serialize_state"):
            cache_prefix_ids(runtime.renderer, module.serialize_state, int(config.token_cache_mb * 1024 * 1024), backend.stats)
        return backend

    def prepare(self, request):
        q = self.module.make_question(_typed(request.question))
        try: rendered = self.runtime._render(request.state, q)
        except getattr(self.module, "InputBudgetError", ()): raise JevInputTooLong("Jev input exceeds runtime budget")
        parts = getattr(rendered, "parts", None)
        return Prepared(request, sum(len(p.ids) for p in parts) if parts is not None else len(rendered.ids), (rendered, q))

    def run(self, batch):
        try:
            groups = {}
            for p in batch: groups.setdefault(p.request.state_id, []).append(p)
            out = {}
            for group in groups.values():
                # One jev-score request per state; with many_mode="batched" all its questions share
                # one decode of the state.
                scores = self.runtime._score_all([p.payload[0] for p in group])
                for p, score in zip(group, scores):
                    raw = self.runtime._result(*p.payload, score)
                    out[id(p)] = JevResult(p.request.question.key, str(raw["answer"]), raw["probabilities"],
                                           float(raw["top_probability"]), float(raw["entropy_concentration"]),
                                           int(raw.get("input_tokens", p.cost)))
            return [out[id(p)] for p in batch]
        except Exception as exc: raise JevUnavailable(f"GGUF Jev runtime failed: {exc}") from exc
