"""The single vocabulary for paths under a project data root."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

SUPPORTED_SOURCE_SUFFIXES = {".md", ".txt", ".docx", ".doc", ".pdf", ".pptx", ".xlsx", ".xlsm", ".xls", ".csv"}


def wiki_folder_name(raw_name: str) -> str:
    stem = PurePosixPath(raw_name).stem
    base, separator, extension = stem.rpartition("_")
    return f"{base}.{extension}" if separator and base and extension.isalnum() else stem


def raw_name_for(mount_name: str) -> str:
    path = PurePosixPath(mount_name)
    extension = path.suffix.lstrip(".").lower()
    if not extension:
        return path.stem
    base, separator, tail = path.stem.rpartition("_")
    if extension == "md" and separator and base and tail.isalnum():
        return path.name
    return f"{path.stem}_{extension}.md"


def generated_rels(mount_rel: str) -> tuple[str, str]:
    source = PurePosixPath(mount_rel)
    raw = source.parent / raw_name_for(source.name)
    return raw.as_posix(), (raw.parent / wiki_folder_name(raw.name)).as_posix()


@dataclass(frozen=True)
class DataLayout:
    """Stable project paths with ownership-oriented accessors.

    Accessors deliberately return paths only; callers decide when and how to
    write them.  This makes a standalone phase easy to point at another root
    without changing the on-disk layout.
    """

    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))

    @property
    def raw(self) -> Path:
        return self.root / "raw"

    @property
    def metadata(self) -> Path:
        return self.root / "metadata"

    @property
    def wiki(self) -> Path:
        return self.root / "wiki"

    @property
    def mount(self) -> Path:
        return self.root / "mount"

    @property
    def state(self) -> Path:
        return self.metadata / "state"

    @property
    def work(self) -> Path:
        return self.metadata / "work"

    @property
    def convert_state(self) -> Path:
        return self.metadata / "convert.json"

    @property
    def pipeline_state(self) -> Path:
        return self.metadata / "pipeline.json"

    @property
    def linker_database(self) -> Path:
        return self.metadata / "wiki-linker.sqlite"

    @property
    def queue_database(self) -> Path:
        return self.metadata / "watch-queue.sqlite"

    @property
    def pipeline_lock(self) -> Path:
        return self.metadata / "pipeline.lock"

    @property
    def index(self) -> Path:
        return self.metadata / "index"

    @property
    def human_sync(self) -> Path:
        return self.metadata / "human-sync"

    def raw_file(self, raw_rel: str) -> Path:
        return self.raw / PurePosixPath(raw_rel)

    def document(self, raw_rel: str) -> str:
        path = PurePosixPath(raw_rel)
        return (path.parent / wiki_folder_name(path.name)).as_posix()

    def wiki_dir(self, raw_rel: str) -> Path:
        return self.wiki / self.document(raw_rel)

    def state_dir(self, raw_rel: str) -> Path:
        return self.state / self.document(raw_rel)

    def work_dir(self, raw_rel: str) -> Path:
        return self.work / self.document(raw_rel)

    def planning(self, raw_rel: str) -> Path:
        return self.wiki_dir(raw_rel) / "_planning"

    def ensure(self) -> "DataLayout":
        for path in (self.raw, self.metadata, self.wiki):
            path.mkdir(parents=True, exist_ok=True)
        return self


__all__ = ["DataLayout", "generated_rels", "raw_name_for", "wiki_folder_name"]
