"""Paths for one project data folder."""

from __future__ import annotations

import io
import logging
import os
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from common.paths import generated_rels as _generated_rels
from common.paths import raw_name_for as _raw_name_for
from common.paths import wiki_folder_name as _wiki_folder_name

log = logging.getLogger(__name__)

VERBATIM = {".md", ".txt"}


def wiki_folder_name(raw_name: str) -> str:
    return _wiki_folder_name(raw_name)


def raw_name_for(mount_name: str) -> str:
    return _raw_name_for(mount_name)


def generated_rels(mount_rel: str) -> tuple[str, str]:
    """Return the raw-file and wiki/state-directory paths for one mount path."""
    return _generated_rels(mount_rel)


def walk_files(root: Path) -> list[Path]:
    """List files under root; ``Path.rglob`` drops unreadable directories without a word."""

    if not root.is_dir():
        return []

    def unreadable(error: OSError) -> None:
        log.error("フォルダーを読み込めません: パス=%s、エラー=%s: %s", getattr(error, "filename", root), type(error).__name__, error, exc_info=True)

    files: list[Path] = []
    for folder, _folders, names in os.walk(root, onerror=unreadable):
        files.extend(Path(folder) / name for name in names)
    return files


def assert_unique_generated_paths(mount_rels: Iterable[str]) -> None:
    """Reject mount layouts that cannot be represented without overwriting output."""

    raw_owners: dict[str, str] = {}
    wiki_owners: dict[str, str] = {}
    for mount_rel in mount_rels:
        rel = PurePosixPath(mount_rel).as_posix()
        raw_rel, wiki_rel = generated_rels(rel)
        for kind, generated, owners in (
            ("raw path", raw_rel, raw_owners),
            ("wiki/state path", wiki_rel, wiki_owners),
        ):
            previous = owners.get(generated)
            if previous is not None and previous != rel:
                log.error("複数の入力パスが同じ出力先を使っています: 種類=%s、出力先=%s、入力パス=%s、別の入力パス=%s", kind, generated, rel, previous)
                raise ValueError(
                    f"mount paths resolve to the same {kind} {generated!r}: "
                    f"{previous!r}, {rel!r}"
                )
            owners[generated] = rel

    # Raw documents are files. A source directory whose mapped name is another
    # document's raw filename would otherwise make the tree impossible to write.
    for raw_rel, owner in raw_owners.items():
        for parent in PurePosixPath(raw_rel).parents:
            parent_rel = parent.as_posix()
            if parent_rel == ".":
                break
            parent_owner = raw_owners.get(parent_rel)
            if parent_owner is not None:
                log.error("変換テキストのパスがファイルとフォルダーで重複しています: パス=%s、元文書=%s、別の元文書=%s", parent_rel, parent_owner, owner)
                raise ValueError(
                    f"generated raw path is both a file and a directory {parent_rel!r}: "
                    f"{parent_owner!r}, {owner!r}"
                )


RESERVED_TEAMS = {"all", "admin", "assets"}


def team_of(rel: str) -> str:
    head, sep, _ = rel.partition("/")
    return head if sep and head else "general"


@dataclass(frozen=True)
class Project:
    root: Path
    mount_root: Path | None = None

    @property
    def mount(self) -> Path:
        return self.mount_root if self.mount_root is not None else self.root / "mount"

    @property
    def raw(self) -> Path:
        from common.paths import DataLayout

        return DataLayout(self.root).raw

    @property
    def metadata(self) -> Path:
        from common.paths import DataLayout

        return DataLayout(self.root).metadata

    @property
    def wiki(self) -> Path:
        from common.paths import DataLayout

        return DataLayout(self.root).wiki

    @property
    def database(self) -> Path:
        return self.root / "graph.sqlite"

    @property
    def engine_db(self) -> Path:
        return self.root / "engine.sqlite"

    @property
    def linker_database(self) -> Path:
        from common.paths import DataLayout

        return DataLayout(self.root).linker_database

    @property
    def queue_database(self) -> Path:
        from common.paths import DataLayout

        return DataLayout(self.root).queue_database

    @property
    def last_sha_path(self) -> Path:
        return self.metadata / "last_sha"

    @property
    def convert_log_path(self) -> Path:
        return self.metadata / "convert.json"

    def raw_file(self, rel: str) -> Path:
        return self.raw / rel

    def wiki_dir(self, rel: str) -> Path:
        path = PurePosixPath(rel)
        return self.wiki / path.parent / wiki_folder_name(path.name)

    def state_dir(self, rel: str) -> Path:
        path = PurePosixPath(rel)
        return self.metadata / "state" / path.parent / wiki_folder_name(path.name)

    def work_dir(self, rel: str) -> Path:
        path = PurePosixPath(rel)
        return self.metadata / "work" / path.parent / wiki_folder_name(path.name)

    def ensure(self) -> "Project":
        for directory in (self.raw, self.metadata, self.wiki):
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                log.error("プロジェクトフォルダーを作成できません: パス=%s、エラー=%s: %s", directory, type(exc).__name__, exc, exc_info=True)
                raise
        return self

    def raw_files(self) -> list[str]:
        return sorted(
            path.relative_to(self.raw).as_posix()
            for path in walk_files(self.raw)
            if path.name.endswith(".md") and ".git" not in path.parts
        )

    def teams(self) -> list[str]:
        names: set[str] = set()
        for base in (self.mount, self.raw):
            if not base.is_dir():
                continue
            for path in base.iterdir():
                if path.is_dir() and not path.name.startswith(".") and path.name not in RESERVED_TEAMS:
                    names.add(path.name)
        return sorted(names)


def open_project(settings: Any) -> Project:
    name = str(getattr(settings, "target_name", "")).strip()
    mount_value = str(getattr(settings, "mount_path", "")).strip()
    if not name or not mount_value:
        log.error("プロジェクト設定に必要な値がありません: target_name=%s、source_mount=%s", name, mount_value)
        raise ValueError("project config did not provide target_name and source_mount")
    mount = Path(mount_value)
    if not mount.is_dir():
        log.error("入力フォルダーが見つかりません: パス=%s、プロジェクト=%s", mount, name)
        raise FileNotFoundError(f"mount directory not found: {mount}")
    return Project(Path(settings.data_root) / name, mount).ensure()


def zip_wiki(project: Project, team: str | None = None) -> bytes:
    buffer = io.BytesIO()
    base = project.wiki / team if team else project.wiki
    # The whole archive is built in RAM, so name the tree before it is read.
    log.info("WikiのZIP圧縮を開始します: パス=%s、チーム=%s", base, team or "")
    files = 0
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(walk_files(base)):
            if not path.is_file() or "_planning" in path.parts:
                continue
            archive.write(path, path.relative_to(project.wiki).as_posix())
            files += 1
    payload = buffer.getvalue()
    log.info("WikiのZIP圧縮が完了しました: ファイル数=%d、バイト数=%d", files, len(payload))
    return payload
