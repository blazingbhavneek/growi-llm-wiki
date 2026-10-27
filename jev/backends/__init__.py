"""Backend contract: prepare on submitter threads; run and close on engine worker."""

from dataclasses import dataclass


@dataclass(slots=True)
class Prepared:
    request: object
    cost: int
    payload: object


def make_backend(config):
    if config.backend == "torch":
        from .torch import TorchBackend
        return TorchBackend.from_config(config)
    if config.backend == "gguf":
        from .gguf import GGUFBackend
        return GGUFBackend.from_config(config)
    if config.backend in {"hosted", "systemone", "sglang", "vllm", "jpt"}:
        from .hosted import HostedBackend  # vanilla /v1/systemone
        return HostedBackend(config)
    if config.backend == "llm2jev":
        from .llm2jev import Llm2JevBackend  # custom batched /score
        return Llm2JevBackend(config)
    if config.backend == "shisa":
        from .shisa_jev import ShisaJevBackend
        return ShisaJevBackend(config)
    raise ValueError(f"unknown Jev backend: {config.backend}")
