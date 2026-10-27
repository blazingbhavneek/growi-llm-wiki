"""Generation/linking policy: the only seam between the shared engine and its variants.

The engine (``graph/``) calls the hooks on ``Policy``. Every default below IS the
standard (production) behaviour, so the standard path never depends on a variant and
never imports one. Variants live in their own package and override hooks there:
``graph/fast/policy.py`` for ``sync --fast``.

To give fast new behaviour: add a hook here whose default leaves standard exactly as
it is, call it from the engine, and override it in ``graph/fast/``. The engine must never
branch on a policy name.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Sequence


@dataclass(frozen=True)
class Policy:
    name: str = "standard"
    version: str = "standard-v1"
    repair_attempts: int = 3
    # wiki writer
    research: bool = True  # cross-page reference research before a page is written
    intro: bool = True  # model-written lead paragraph per page
    strict_judge: bool = False  # an unavailable judge or reported gaps -> verbatim source section
    offline: bool = False  # may run without a model client (deterministic plan, verbatim sections)
    # linker
    link: bool = True  # cross-document linking after the build (off: pages publish unlinked)
    # spreadsheets
    model_table_structure: bool = True  # model decides row/column reading of a table
    table_analyses: bool = True  # csv analysis pages

    def cache_key(self, base: str) -> str:
        """Checkpoint/cache version key; variants key apart so standard never resumes them."""
        return base

    def stamp(self, marker: dict[str, Any]) -> dict[str, Any]:
        """Extra fields for persisted linker markers."""
        return marker

    def config_overrides(self) -> dict[str, Any]:
        """WikiConfig field overrides (graph/workspace/writer.py:wiki_config)."""
        return {}

    def model_port(self, config: Any) -> Any:
        """Construct this policy's model seam without exposing policy names to the engine."""
        from graph.wiki.model import ChatModelPort

        return ChatModelPort(config)

    def planning_model(self, model: Any) -> Any:
        """Select the model used for document planning; standard keeps its client."""
        return model

    def model_cache_fields(self, config: Any) -> dict[str, Any]:
        """Return model identity fields that affect this policy's planner cache."""
        del config
        return {}

    def model_if_missing(self, config: Any) -> Any | None:
        """Optional model for legacy callers that historically accepted None."""
        del config
        return None

    # -- wiki pipeline (graph/wiki) --------------------------------------------------

    def title(self, title: str) -> str:
        """Seed page title as shown in the wiki."""
        return title

    def section(self, markdown: str) -> str:
        """One written section before it joins its page."""
        return markdown

    def code_tokens(self, text: str) -> set[str]:
        """Identifiers a rewrite must keep (prompt list and lossless check)."""
        from graph.wiki.page import code_tokens

        return code_tokens(text)

    def wiki_prompt_rules(self, role: str) -> str:
        """Variant-only additions to an existing wiki writer or judge prompt."""
        del role
        return ""

    def writer_units(self, lines: Sequence[str], images: Sequence[Any]) -> list[Any]:
        """Opaque source units restored after writing; standard keeps blocks lossless."""
        from graph.wiki.images import block_units

        return [*images, *block_units(lines)]

    def adjust_seed_plan(self, plan: Any, *, config: Any) -> Any:
        """The compiled model seed plan before pages are made from it."""
        return plan

    def offline_seed_plan(self, lines: Sequence[str]) -> Any:
        """Plan used when ``offline`` and no model client exists."""
        raise NotImplementedError(f"policy {self.name} cannot plan without a model")

    async def hierarchy(self, pages: Sequence[Any], lines: Sequence[str], *, checkpoint: Any) -> dict[str, str] | None:
        """Parent/page summaries without the model, or None for the model path."""
        return None

    def mask_for_linking(self, text: str) -> tuple[str, Callable[[str], str]]:
        """Hide spans link insertion must not touch; returns the masked text and its undo."""
        return text, _same

    async def rewrite_pending_pages(self, **kwargs: Any) -> bool:
        """Optionally replace the standard page writer; True means fully handled."""
        del kwargs
        return False

    # -- linker (graph/linker) -------------------------------------------------------

    def neo_limits(self, caps: list[int], judge: bool) -> tuple[list[int], bool]:
        """Neo candidate hop caps and whether settings may override them."""
        return caps, judge

    async def curate_page(self, **kwargs: Any) -> tuple[list[dict[str, Any]], list[str]] | None:
        """Reference choices for one page without the curator, or None for the curator."""
        return None

    async def filter_target(self, **kwargs: Any) -> tuple[list[dict[str, Any]], int, int] | None:
        """Accepted edges for one chunk, or None for the standard judge."""
        return None

    async def describe_pages(self, chunks: Sequence[Any], **kwargs: Any) -> tuple[dict[str, Any], int]:
        """Chunk metadata found before per-chunk calls: ({text_sha256: meta}, calls)."""
        return {}, 0



def _same(text: str) -> str:
    return text


STANDARD = Policy()


def resolve_policy(value: str | Policy | None) -> Policy:
    if isinstance(value, Policy):
        return value
    normalized = str(value or "standard").strip().lower()
    if normalized in {"", "standard", "standard-v1"}:
        return STANDARD
    if normalized == "fast" or normalized.startswith("fast-v"):
        from graph.fast.policy import FAST  # the standard path never reaches this import

        return FAST
    raise ValueError("policy must be standard or fast")


def policy_of(owner: Any) -> Policy:
    """The policy carried by a settings or config object (missing means standard)."""

    return resolve_policy(getattr(owner, "policy", "standard"))


def __getattr__(name: str) -> Any:
    if name == "FAST":
        from graph.fast.policy import FAST

        return FAST
    raise AttributeError(name)


__all__ = ["FAST", "STANDARD", "Policy", "policy_of", "resolve_policy"]
