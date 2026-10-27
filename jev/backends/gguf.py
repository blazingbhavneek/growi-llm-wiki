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
                        log.debug("JevのGGUFデータがローカルにないため、配布元から取得します: リポジトリ=%s、エラー=%s: %s", config.gguf_model, type(exc).__name__, exc, exc_info=True)
                    else:
                        log.error("JevのGGUFデータを取得できないため、1回再試行します: リポジトリ=%s、パス=%s、エラー=%s: %s（通信・容量・権限・Hugging Faceトークンを確認してください）", config.gguf_model, folder, type(exc).__name__, exc, exc_info=True)
                    kwargs.pop("local_files_only", None)
                    folder = Path(snapshot_download(**kwargs))
            except Exception as exc:
                log.error("JevのGGUFモデルを取得できません。使える保存データがなく、2回目の試行も失敗しました: リポジトリ=%s、パス=%s、エラー=%s: %s", config.gguf_model, folder, type(exc).__name__, exc, exc_info=True)
                raise
        if not all((folder / name).is_file() for name in files):
            missing = [name for name in files if not (folder / name).is_file()]
            log.error("GGUFの保存データが不完全です: パス=%s、不足=%s、リポジトリ=%s", folder, ",".join(missing), config.gguf_model)
            raise JevUnavailable(f"incomplete GGUF snapshot: {folder}")
        spec = importlib.util.spec_from_file_location("jev_style_decision_gguf", folder / files[0])
        if spec is None or spec.loader is None:
            log.error("JevのGGUF実行スクリプトを読み込めません: ファイル=%s、リポジトリ=%s", folder / files[0], config.gguf_model)
            raise JevUnavailable("cannot load GGUF runtime")
        try: module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        except Exception as exc:
            log.error("JevのGGUF実行スクリプトを取り込めません。llama.cpp の連携モジュール不足も確認してください: ファイル=%s、リポジトリ=%s、エラー=%s: %s", folder / files[0], config.gguf_model, type(exc).__name__, exc, exc_info=True)
            raise
        kw = {"quant": config.gguf_quant, "many_mode": config.gguf_many_mode}
        if config.gguf_binary: kw["binary"] = config.gguf_binary
        try:
            try: runtime = module.JevStyleDecisionGGUF(str(folder), **kw)
            except TypeError as exc:
                log.error("Jevの実行環境が quant・many_mode・binary に対応しないため、省略して再試行します。既定値が変わる場合があります: パス=%s、量子化=%s、エラー=%s: %s", folder, config.gguf_quant, type(exc).__name__, exc, exc_info=True)
                runtime = module.JevStyleDecisionGGUF(str(folder))
        except Exception as exc:
            log.error("JevのGGUFモデルを読み込めません: パス=%s、量子化=%s、コマンド=%s、エラー=%s: %s（モデルファイル・PATH上の実行コマンド・メモリ残量を確認してください）", folder, config.gguf_quant, config.gguf_binary, type(exc).__name__, exc, exc_info=True)
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
