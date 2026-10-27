"""The fast policy: overrides of the common.policy.Policy hooks, nothing else."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Sequence

from common.policy import Policy


@dataclass(frozen=True)
class FastPolicy(Policy):
    def cache_key(self, base: str) -> str:
        return f"{base}:{self.version}"

    def stamp(self, marker: dict[str, Any]) -> dict[str, Any]:
        return {**marker, "policy": self.name, "policy_version": self.version}

    def config_overrides(self) -> dict[str, Any]:
        # Window inventories are ~3.3K tokens of JSON and reasoning counts toward the
        # cap; at 4000 any longer reasoning truncated the JSON (three more full calls).
        return {
            "write_attempts": self.repair_attempts + 1,
            "planner_max_output_tokens": 12000,
            "judge_max_output_tokens": 16000,
        }

    def model_port(self, config: Any) -> Any:
        from .model import model_pair

        return model_pair(config)

    def planning_model(self, model: Any) -> Any:
        from graph.wiki.model import judge_model

        return judge_model(model)

    def model_cache_fields(self, config: Any) -> dict[str, Any]:
        return {
            "judge_base_url": config.judge_base_url,
            "judge_model": config.judge_model,
        }

    def model_if_missing(self, config: Any) -> Any:
        return self.model_port(config)

    def title(self, title: str) -> str:
        from .wiki import strip_heading_number

        return strip_heading_number(title) or title

    def section(self, markdown: str) -> str:
        from .wiki import strip_heading_numbers

        return strip_heading_numbers(markdown)

    def code_tokens(self, text: str) -> set[str]:
        from .wiki import code_tokens

        return code_tokens(text)

    def wiki_prompt_rules(self, role: str) -> str:
        from .wiki import generation_prompt_rules

        return generation_prompt_rules(role)

    def writer_units(self, lines: Sequence[str], images: Sequence[Any]) -> list[Any]:
        """Fast rewrites tables/fences; only image bytes remain opaque."""
        del lines
        return list(images)

    def adjust_seed_plan(self, plan: Any, *, config: Any) -> Any:
        from .wiki import merge_small_pages

        return merge_small_pages(plan, target=config.page_target_lines)

    def offline_seed_plan(self, lines: Sequence[str]) -> Any:
        from .wiki import deterministic_seed_plan

        return deterministic_seed_plan(lines)

    async def hierarchy(self, pages: Sequence[Any], lines: Sequence[str], *, checkpoint: Any) -> dict[str, str]:
        from .wiki import hierarchy

        return hierarchy(pages, lines, checkpoint=checkpoint, version=self.version)

    async def rewrite_pending_pages(self, **kwargs: Any) -> bool:
        from .writer import rewrite_pending_pages

        await rewrite_pending_pages(**kwargs)
        return True

    def mask_for_linking(self, text: str) -> tuple[str, Callable[[str], str]]:
        from .wiki import mask_math

        return mask_math(text)

    def neo_limits(self, caps: list[int], judge: bool) -> tuple[list[int], bool]:
        return [caps[0], 0, 0], False  # 1-hop only, settings may not widen it

    async def curate_page(self, **kwargs: Any) -> tuple[list[dict[str, Any]], list[str]]:
        from .linker import curate_page

        return await curate_page(**kwargs)

    async def filter_target(self, **kwargs: Any) -> tuple[list[dict[str, Any]], int, int]:
        from .linker import filter_target

        return await filter_target(**kwargs)

    async def describe_pages(self, chunks: Sequence[Any], **kwargs: Any) -> tuple[dict[str, Any], int]:
        from .linker import describe_pages

        return await describe_pages(chunks, **kwargs)



FAST = FastPolicy(
    name="fast",
    version="fast-v7",
    repair_attempts=3,
    research=False,
    intro=False,
    strict_judge=True,
    offline=True,
    link=True,  # sync uses the resumable inline linker after accepting the wiki
    model_table_structure=False,
    table_analyses=False,
)

__all__ = ["FAST", "FastPolicy"]
