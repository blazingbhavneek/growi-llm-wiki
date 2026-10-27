import importlib.util
from pathlib import Path

from ..types import JevInputTooLong, JevResult, JevUnavailable
from . import Prepared
from .torch import TorchBackend, _typed, cache_prefix_ids


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
            try: folder = Path(snapshot_download(**kwargs))
            except Exception:
                kwargs.pop("local_files_only", None)
                folder = Path(snapshot_download(**kwargs))
        if not all((folder / name).is_file() for name in files):
            raise JevUnavailable(f"incomplete GGUF snapshot: {folder}")
        spec = importlib.util.spec_from_file_location("jev_style_decision_gguf", folder / files[0])
        if spec is None or spec.loader is None: raise JevUnavailable("cannot load GGUF runtime")
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        kw = {"quant": config.gguf_quant, "many_mode": config.gguf_many_mode}
        if config.gguf_binary: kw["binary"] = config.gguf_binary
        try: runtime = module.JevStyleDecisionGGUF(str(folder), **kw)
        except TypeError: runtime = module.JevStyleDecisionGGUF(str(folder))
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
