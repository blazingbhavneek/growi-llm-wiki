import asyncio
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from graph.config import Settings
from graph.clients.embeddings import Embedder
from graph.workspace.project import open_project
from graph.workspace.writer import wiki_config
from publisher.index import build_index, render_document_index
from publisher.index import _related_documents
from graph.linker.catalog import Catalog
from graph.linker.chunks import Chunk, make_chunks, normalize_name
from graph.linker.jev_judge import check_roles, resolve_aliases, primary_definer, judge_edges
from graph.linker.neo import candidates as neo_candidates
from graph.linker.render import RenderEdge, footer_edges
from graph.linker.service import _curate_page, _filter_target
from graph.linker.wire import ChunkEntity, ChunkMeta


def make_project(tmp, documents):
    root = Path(tmp)
    mount = root / "mount"
    mount.mkdir(parents=True, exist_ok=True)
    settings = SimpleNamespace(data_root=str(root / "data"), target_name="t", mount_path=str(mount))
    project = open_project(settings)
    for document, pages in documents.items():
        folder = project.wiki / document
        planning = folder / "_planning"
        planning.mkdir(parents=True)
        filenames = [f"{i:03d}-p.md" for i in range(1, pages + 1)]
        (planning / "linker.json").write_text('{"status":"complete"}', encoding="utf-8")
        (planning / "manifest.json").write_text(json.dumps({"files": [
            {"filename": name, "title": f"{document}-title-{i}"} for i, name in enumerate(filenames)
        ]}), encoding="utf-8")
        (planning / "coverage.json").write_text(json.dumps({"files": [
            {"filename": name, "title": f"{document}-title-{i}", "summary": f"{document}-summary-{i}",
             "header": f"{document}-chapter-{i}"} for i, name in enumerate(filenames)
        ]}), encoding="utf-8")
        (planning / "chunks.json").write_text(json.dumps({"pages": [
            {"filename": name, "chunks": [{"keywords": [f"{document}-keyword"],
                                             "entities": [{"name": f"{document}-entity", "role": "defines"}]}]}
            for name in filenames
        ]}), encoding="utf-8")
        for i, name in enumerate(filenames):
            (folder / name).write_text(f"# {document}-title-{i}\n", encoding="utf-8")
    return settings, project


class IndexTreeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.documents = {"teamA/sub/docA": 2, "teamA/docB": 1, "teamB/docC": 1, "docRoot": 1}
        self.settings, self.project = make_project(self.tmp.name, self.documents)
        self.env = mock.patch.dict(os.environ, {"GROWI_URL": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def build(self, only=None):
        return build_index(self.settings, only=only, publish=False)

    def read_index(self, folder=""):
        path = self.project.metadata / "index" / folder / "index.md" if folder else self.project.metadata / "index" / "index.md"
        return path.read_text(encoding="utf-8")

    def test_root_lists_team_names_only(self):
        self.build()
        root = self.read_index()
        self.assertIn("<!-- llm-wiki-index:root -->", root)
        self.assertIn("[teamA](/teamA/00-目次)", root)
        self.assertIn("[docRoot](/docRoot/00-目次)", root)
        for team in ("teamA", "teamB"):
            line = next(line for line in root.splitlines() if f"[{team}]" in line)
            self.assertIn("— チーム", line)
        self.assertIn("docRoot-keyword", root)
        for private in ("docA-title", "docB-title", "docC-title", "docA-keyword", "docB-entity", "docC-keyword"):
            self.assertNotIn(private, root)
        for team in ("teamA", "teamB"):
            start = root.index(f"- [{team}]")
            end = root.find("\n- [", start + 1)
            block = root[start:end if end >= 0 else None]
            self.assertNotIn("キーワード", block)
            self.assertNotIn("エンティティ", block)
            self.assertNotIn("内容", block)

    def test_team_folder_aggregates_only_its_descendants(self):
        self.build()
        team = self.read_index("teamA")
        self.assertIn("[sub]", team)
        self.assertIn("[docB](/teamA/docB/00-目次)", team)
        self.assertIn("文書数: 1", team)
        self.assertIn("docA-keyword", team)
        self.assertIn("docB-keyword", team)
        self.assertNotIn("docC-keyword", team)

    def test_nested_folder_index(self):
        self.build()
        nested = self.read_index("teamA/sub")
        self.assertIn("[docA]", nested)
        self.assertIn("ページ数: 2", nested)

    def test_document_index_page_cards_unchanged(self):
        self.build()
        cards = __import__("publisher.index", fromlist=["document_cards"]).document_cards(self.project.wiki / "teamA/docB")
        expected = render_document_index("docB", cards, lambda f: f"/teamA/docB/{f.removesuffix('.md')}")
        self.assertEqual(self.read_index("teamA/docB"), expected)

    def test_scoped_build_touches_only_ancestors(self):
        result = self.build(["teamA/docB.md"])
        folders = {item["folder"] for item in result["done"] if "folder" in item}
        self.assertEqual(folders, {"", "teamA"})

    def test_document_that_is_also_a_folder(self):
        settings, project = make_project(self.tmp.name + "/collision", {"teamA/x": 1, "teamA/x/y": 1})
        build_index(settings, publish=False)
        body = (project.metadata / "index/teamA/x/index.md").read_text(encoding="utf-8")
        self.assertIn("## サブフォルダ", body)
        self.assertIn("[y]", body)
        self.assertIn("<!-- llm-wiki-index:document -->", body)

    def test_empty_folder_index_is_removed(self):
        self.build()
        doc = self.project.wiki / "teamB/docC"
        shutil.rmtree(doc)
        self.build(["teamB/docC.md"])
        self.assertFalse((self.project.metadata / "index/teamB/docC/index.md").exists())
        self.assertFalse((self.project.metadata / "index/teamB/index.md").exists())
        self.assertNotIn("[teamB]", self.read_index())

    def test_scoped_move_removes_old_indexes_and_writes_the_new_tree(self):
        self.build()
        old = self.project.wiki / "teamB/docC"
        new = self.project.wiki / "archive/docD"
        new.parent.mkdir(parents=True)
        shutil.move(old, new)

        self.build(["teamB/docC.md", "archive/docD.md"])

        self.assertFalse((self.project.metadata / "index/teamB/docC/index.md").exists())
        self.assertFalse((self.project.metadata / "index/teamB/index.md").exists())
        self.assertTrue((self.project.metadata / "index/archive/docD/index.md").exists())
        self.assertIn("[archive]", self.read_index())
        self.assertNotIn("[teamB]", self.read_index())

    def test_full_build_sweeps_stale_local_index_copies(self):
        self.build()
        stale = self.project.metadata / "index/old/location/index.md"
        stale.parent.mkdir(parents=True)
        stale.write_text('<span hidden data-llm-wiki-index="document"></span>\n', encoding="utf-8")

        self.build()

        self.assertFalse(stale.exists())
        self.assertFalse(stale.parent.exists())

    def test_full_build_sweeps_stale_owned_growi_indexes(self):
        from graph.growi.client import GrowiPage

        class Client:
            def __init__(self):
                self.pages = {
                    "/t/old/location/00-目次": GrowiPage(
                        page_id="stale", revision_id="1",
                        path="/t/old/location/00-目次",
                        body='<span hidden data-llm-wiki-index="document"></span>\n',
                    ),
                }
                self.next_id = 0

            async def get_page(self, *, path=None, page_id=None):
                if path is not None:
                    return self.pages.get(path)
                return next((page for page in self.pages.values() if page.page_id == page_id), None)

            async def list_all_pages(self, _root):
                return list(self.pages.values())

            async def create_page(self, path, body):
                self.next_id += 1
                page = GrowiPage(page_id=f"id{self.next_id}", revision_id="1", path=path, body=body)
                self.pages[path] = page
                return page

            async def update_page(self, page_id, revision_id, body):
                path = next(path for path, page in self.pages.items() if page.page_id == page_id)
                page = GrowiPage(
                    page_id=page_id, revision_id=str(int(revision_id) + 1), path=path, body=body,
                )
                self.pages[path] = page
                return page

            async def delete_pages(self, pages):
                ids = set(pages)
                for path, page in list(self.pages.items()):
                    if page.page_id in ids:
                        del self.pages[path]

        client = Client()
        connection = SimpleNamespace(write_path="/t", root_path="/t", mode="attach")
        publisher = SimpleNamespace(client=client, connection=connection)
        with mock.patch("publisher.index._connection", return_value=connection), \
             mock.patch("publisher.index._publisher", return_value=publisher):
            result = build_index(self.settings, publish=True)

        self.assertEqual(result["failures"], [])
        self.assertNotIn("/t/old/location/00-目次", client.pages)
        self.assertIn("/t/00-目次", client.pages)

    def test_growi_upserts_and_deletes(self):
        class Client:
            def __init__(self):
                self.pages = {}
                self.calls = []
                self.next_id = 0

            async def get_page(self, *, path):
                return self.pages.get(path)

            async def create_page(self, path, body):
                self.next_id += 1
                page = SimpleNamespace(page_id=f"id{self.next_id}", revision_id="1", body=body)
                self.pages[path] = page
                self.calls.append(("create", path))
                return page

            async def update_page(self, page_id, revision_id, body):
                path = next(path for path, page in self.pages.items() if page.page_id == page_id)
                page = SimpleNamespace(page_id=page_id, revision_id=str(int(revision_id) + 1), body=body)
                self.pages[path] = page
                self.calls.append(("update", path))
                return page

            async def delete_pages(self, pages):
                ids = set(pages)
                for path, page in list(self.pages.items()):
                    if page.page_id in ids:
                        del self.pages[path]
                        self.calls.append(("delete", path))

        client = Client()
        connection = SimpleNamespace(write_path="/t", root_path="/t", mode="attach")
        publisher = SimpleNamespace(client=client, connection=connection)
        with mock.patch("publisher.index._connection", return_value=connection), \
             mock.patch("publisher.index._publisher", return_value=publisher):
            build_index(self.settings, publish=True)
            created = [call for call in client.calls if call[0] == "create"]
            self.assertEqual(len(created), len(self.documents) + 4)  # docs, root, teamA, sub, teamB
            client.calls.clear()
            build_index(self.settings, publish=True)
            self.assertEqual(client.calls, [])
            shutil.rmtree(self.project.wiki / "teamB/docC")
            build_index(self.settings, only=["teamB/docC.md"], publish=True)
            self.assertIn(("delete", "/t/teamB/docC/00-目次"), client.calls)
            self.assertIn(("delete", "/t/teamB/00-目次"), client.calls)


class PlannerConcurrencyTest(unittest.TestCase):
    def test_planner_setting_reaches_wiki_config(self):
        settings = SimpleNamespace(
            chat_base_url="http://llm.test/v1", chat_api_key="k", chat_model="m",
            wiki_planner_concurrency=2, wiki_rewrite_concurrency=8, concurrency=4,
        )
        config = wiki_config(settings, run_dir=Path("/tmp/run"))
        self.assertEqual(config.planner_concurrency, 2)
        self.assertEqual(config.rewrite_concurrency, 8)

    def test_env_and_ini_parse(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mount = root / "mount"
            mount.mkdir()
            ini = root / "project.ini"
            ini.write_text(
                f"[project]\ntarget_name = t\ndata_root = {root / 'data'}\nsource_mount = {mount}\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"WIKI_PLANNER_CONCURRENCY": "3", "WIKI_CONCURRENCY": "7"}, clear=True):
                self.assertEqual(Settings.from_env(str(ini)).wiki_planner_concurrency, 3)
            ini.write_text(ini.read_text(encoding="utf-8") + "\n[settings]\nconcurrency = 5\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(Settings.from_env(str(ini)).wiki_planner_concurrency, 5)


class EmbedderOffTest(unittest.TestCase):
    def test_backend_off_raises_value_error(self):
        with self.assertRaises(ValueError):
            Embedder(SimpleNamespace(embed_backend="off"))


class LinkerJevJudgeTest(unittest.TestCase):
    class Engine:
        def __init__(self, values=None): self.values, self.calls = values or {}, []
        async def adecide_many(self, state, questions, **kwargs):
            from types import SimpleNamespace
            self.calls.append((state, questions))
            return [SimpleNamespace(key=q.key, p_yes=self.values.get(q.key, .9)) for q in questions]
        async def adecide_batch(self, requests, **kwargs):
            from types import SimpleNamespace
            self.calls.append(requests)
            return [SimpleNamespace(key=r.question.key, p_yes=self.values.get(r.question.key, .9)) for r in requests]

    def settings(self):
        return SimpleNamespace(wiki_linker_role_threshold=.5, wiki_linker_alias_threshold=.8,
                               wiki_linker_screen_candidates=50, wiki_linker_screen_threshold=.4,
                               wiki_linker_verify_top=5, wiki_linker_verify_threshold=.7,
                               wiki_linker_curate_keep=8, wiki_linker_hop_caps="40,40,20",
                               wiki_linker_judge="jev", wiki_output_language="Japanese")

    def test_judge_settings_from_env_and_ini(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); mount = root / "mount"; mount.mkdir()
            ini = root / "project.ini"
            ini.write_text(f"[project]\ntarget_name=t\ndata_root={root / 'data'}\nsource_mount={mount}\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"WIKI_LINKER_JUDGE": "jev", "WIKI_LINKER_ALIAS_THRESHOLD": "0.85",
                                              "WIKI_INDEX_RELATED_DOCS": "1"}, clear=True):
                settings = Settings.from_env(str(ini))
            self.assertEqual(settings.wiki_linker_judge, "jev")
            self.assertEqual(settings.wiki_linker_alias_threshold, .85)
            self.assertTrue(settings.wiki_index_related_docs)
            ini.write_text(ini.read_text(encoding="utf-8") + "\n[settings]\nwiki_linker_judge=jev\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(Settings.from_env(str(ini)).wiki_linker_judge, "jev")

    def test_project_gguf_settings_override_environment_backend(self):
        from jev import JevConfig

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); mount = root / "mount"; mount.mkdir()
            ini = root / "project.ini"
            ini.write_text(
                f"[project]\ntarget_name=t\ndata_root={root / 'data'}\nsource_mount={mount}\n"
                "\n[settings]\nwiki_jev_backend=gguf\n"
                "wiki_jev_gguf_local_path=/models/jev-gguf\n"
                "wiki_jev_gguf_quant=Q4_K_M\n"
                "wiki_jev_gguf_many_mode=batched\n"
                "wiki_jev_score_bin=/models/jev-score\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"WIKI_JEV_BACKEND": "llm2jev"}, clear=True):
                settings = Settings.from_env(str(ini))
                config = JevConfig.from_settings(settings)

        self.assertEqual(config.backend, "gguf")
        self.assertEqual(config.gguf_local_path, "/models/jev-gguf")
        self.assertEqual(config.gguf_quant, "Q4_K_M")
        self.assertEqual(config.gguf_many_mode, "batched")
        self.assertEqual(config.gguf_binary, "/models/jev-score")

    def test_role_check_overrides_llm_role_and_skips_completed(self):
        chunk = make_chunks("team/doc", "team", "p.md", "# title\n## API\nWidget is the API.")[1]
        chunk.meta = ChunkMeta(entities=[ChunkEntity(name="Widget", kind="API", role="uses")])
        engine = self.Engine()
        self.assertEqual(asyncio.run(check_roles(engine, [chunk], self.settings())), 0)
        self.assertEqual(chunk.entities[0].role, "defines")
        self.assertEqual(chunk.meta.role_judge, "jev-1")
        self.assertEqual(asyncio.run(check_roles(engine, [chunk], self.settings())), 0)
        self.assertEqual(len(engine.calls), 1)

    def test_page_lead_paragraph_never_defines(self):
        lead, section = make_chunks("team/doc", "team", "p.md", "# title\nWidget overview.\n## API\nWidget is the API.")
        only = make_chunks("team/doc", "team", "q.md", "# title\nWidget is the API.")[0]
        for chunk in (lead, section, only):
            chunk.meta = ChunkMeta(entities=[ChunkEntity(name="Widget", kind="API", role="defines")])
        engine = self.Engine()
        asyncio.run(check_roles(engine, [lead, section, only], self.settings()))
        self.assertEqual(lead.entities[0].role, "uses")
        self.assertEqual(lead.meta.role_judge, "jev-1")
        self.assertEqual(len(engine.calls), 2)  # the section and the page without sections

    def test_alias_merge_is_team_scoped_and_canonical(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Catalog.open(Path(tmp) / "catalog.db", mode="neo")
            try:
                a = make_chunks("team/a", "team", "a.md", "# A\n## 定義\nユーザID は利用者を示す識別子です。")[1]
                b = make_chunks("team/b", "team", "b.md", "# B\n## 定義\nユーザーID は利用者を示す識別子です。")[1]
                other = make_chunks("other/c", "other", "c.md", "# C\n## 定義\nユーザID は別チームです。")[1]
                for document, team, chunk in (("team/a", "team", a), ("team/b", "team", b), ("other/c", "other", other)):
                    chunk.meta = ChunkMeta(entities=[ChunkEntity(name=("ユーザID" if chunk is not b else "ユーザーID"), kind="identifier", role="defines")])
                    catalog.upsert_document(document, team, "", [chunk])
                engine = self.Engine()
                asyncio.run(resolve_aliases(catalog, engine, "team", [(normalize_name("ユーザーID"), "ユーザーID")], self.settings()))
                self.assertEqual(catalog.canonical("team", normalize_name("ユーザID")),
                                 catalog.canonical("team", normalize_name("ユーザーID")))
                self.assertEqual(len(engine.calls), 1)
                use = make_chunks("team/use", "team", "use.md", "# Use\n## H\nユーザID を使います。")[1]
                use.meta = ChunkMeta(entities=[ChunkEntity(name="ユーザID", kind="identifier", role="uses")])
                catalog.upsert_document("team/use", "team", "", [use])
                linked = neo_candidates(catalog, use, judge=True, settings=self.settings())
                # Both definers are found through the merged alias; being in other documents,
                # they are leads for the judge rather than automatic links.
                self.assertEqual({item.chunk_id for item in linked if item.source == "name_match"}, {a.chunk_id, b.chunk_id})
                asyncio.run(resolve_aliases(catalog, engine, "other", [(normalize_name("ユーザID"), "ユーザID")], self.settings()))
                self.assertEqual(len(engine.calls), 1)
            finally: catalog.close()

    def test_primary_definer_uses_highest_probability(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Catalog.open(Path(tmp) / "catalog.db", mode="neo")
            try:
                ids = []
                for i in range(3):
                    item = make_chunks(f"team/d{i}", "team", f"{i}.md", f"# D{i}\n## X\nWidget def {i}")[1]
                    item.meta = ChunkMeta(entities=[ChunkEntity(name="Widget", kind="API", role="defines")])
                    catalog.upsert_document(f"team/d{i}", "team", "", [item]); ids.append(item.chunk_id)
                engine = self.Engine({ids[0]: .2, ids[1]: .9, ids[2]: .6})
                chosen = asyncio.run(primary_definer(catalog, engine, "team", "widget", ids, self.settings()))
                self.assertEqual(chosen, ids[1])
            finally: catalog.close()

    def test_screen_then_verify_caps_and_jev_edges(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Catalog.open(Path(tmp) / "catalog.db", mode="neo")
            try:
                target = make_chunks("team/target", "team", "t.md", "# Target\n## H\nA page with a target.")[1]
                candidates = []
                for i in range(60):
                    item = make_chunks(f"team/c{i}", "team", f"c{i}.md", f"# C{i}\n## H\nCandidate content {i}.")[1]
                    candidates.append(item)
                catalog.upsert_document("team/target", "team", "", [target])
                for i, item in enumerate(candidates): catalog.upsert_document(f"team/c{i}", "team", "", [item])
                found = [SimpleNamespace(chunk_id=item.chunk_id, source="topical", via=[]) for item in candidates]
                engine = self.Engine()
                accepted = asyncio.run(judge_edges(catalog, engine, target, found, self.settings()))
                self.assertLessEqual(len(engine.calls[0]), 50)
                self.assertLessEqual(len(engine.calls[1]), 5)
                self.assertTrue(all(edge["source"] == "jev" and edge["label"] == "related" for edge in accepted))
            finally: catalog.close()

    def test_other_documents_get_their_own_screen_and_verify_slots(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Catalog.open(Path(tmp) / "catalog.db", mode="neo")
            try:
                target = make_chunks("team/target", "team", "t.md", "# Target\n## H\nA page with a target.")[1]
                siblings = [make_chunks("team/target", "team", f"s{i}.md", f"# S{i}\n## H\nSibling {i}.")[1] for i in range(8)]
                others = [make_chunks(f"team/o{i}", "team", f"o{i}.md", f"# O{i}\n## H\nOther {i}.")[1] for i in range(3)]
                catalog.upsert_document("team/target", "team", "", [target, *siblings])
                for i, item in enumerate(others): catalog.upsert_document(f"team/o{i}", "team", "", [item])
                found = ([SimpleNamespace(chunk_id=item.chunk_id, source="hop1", via=[]) for item in siblings]
                         + [SimpleNamespace(chunk_id=item.chunk_id, source="topical", via=[]) for item in others])
                # Siblings score higher, and the hops alone fill the screening limit.
                engine = self.Engine({item.chunk_id: .95 for item in siblings})
                settings = self.settings(); settings.wiki_linker_screen_candidates = 8; settings.wiki_linker_verify_top = 2
                accepted = asyncio.run(judge_edges(catalog, engine, target, found, settings))
                peers = {edge["chunk_b"] for edge in accepted}
                self.assertEqual(len(peers & {item.chunk_id for item in others}), 2)
                self.assertEqual(len(peers & {item.chunk_id for item in siblings}), 2)
            finally: catalog.close()

    def test_llm_breaks_ties_just_under_the_verify_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Catalog.open(Path(tmp) / "catalog.db", mode="neo")
            try:
                target = make_chunks("team/target", "team", "t.md", "# Target\n## H\nA page with a target.")[1]
                yes, no, low = (make_chunks(f"team/{n}", "team", f"{n}.md", f"# {n}\n## H\nBody {n}.")[1] for n in ("yes", "no", "low"))
                catalog.upsert_document("team/target", "team", "", [target])
                for n, item in (("yes", yes), ("no", no), ("low", low)): catalog.upsert_document(f"team/{n}", "team", "", [item])
                found = [SimpleNamespace(chunk_id=item.chunk_id, source="topical", via=[]) for item in (yes, no, low)]
                engine = self.Engine({yes.chunk_id: .55, no.chunk_id: .5, low.chunk_id: .45})
                asked = []
                class Model:
                    async def text(self, messages, **kwargs):
                        asked.append(messages[-1].content)
                        return "考察…\nはい" if "Body yes" in messages[-1].content else "いいえ"
                settings = self.settings(); settings.wiki_linker_tiebreak_floor = .5
                accepted = asyncio.run(judge_edges(catalog, engine, target, found, settings, model=Model()))
                self.assertEqual([edge["chunk_b"] for edge in accepted], [yes.chunk_id])
                self.assertEqual(len(asked), 2)  # .45 is under the floor
                self.assertEqual(asyncio.run(judge_edges(catalog, engine, target, found, settings)), [])
            finally: catalog.close()

    def test_other_documents_are_searched_separately(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Catalog.open(Path(tmp) / "catalog.db", mode="neo")
            try:
                target = make_chunks("team/big", "team", "t.md", "# Widget\n## Widget setup\nWidget setup guide.")[1]
                target.meta = ChunkMeta(summary="Widget setup guide", keywords=["Widget", "setup"])
                siblings = [make_chunks("team/big", "team", f"s{i}.md", f"# Widget {i}\n## Widget setup {i}\nWidget setup guide {i}.")[1] for i in range(10)]
                other = make_chunks("team/other", "team", "o.md", "# Other\n## Widget\nWidget notes.")[1]
                catalog.upsert_document("team/big", "team", "", [target, *siblings])
                catalog.upsert_document("team/other", "team", "", [other])
                settings = self.settings(); settings.wiki_linker_screen_candidates = 3
                found = neo_candidates(catalog, target, judge=True, settings=settings)
                self.assertIn(other.chunk_id, [item.chunk_id for item in found])
            finally: catalog.close()

    def test_curation_keeps_slots_for_other_documents(self):
        from graph.linker.jev_judge import curate
        edges = [{"edge_id": f"same{i}", "peer_title": "", "peer_heading": "", "summary": "", "cross": False} for i in range(10)]
        edges += [{"edge_id": f"cross{i}", "peer_title": "", "peer_heading": "", "summary": "", "cross": True} for i in range(2)]
        engine = self.Engine({f"cross{i}": .6 for i in range(2)})
        settings = self.settings(); settings.wiki_linker_curate_keep = 3
        kept = asyncio.run(curate(engine, "page", edges, settings))
        self.assertEqual(sorted(k for k in kept if k.startswith("cross")), ["cross0", "cross1"])
        self.assertEqual(len([k for k in kept if k.startswith("same")]), 3)

    def test_shared_name_links_inside_a_document_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Catalog.open(Path(tmp) / "catalog.db", mode="neo")
            try:
                user = make_chunks("team/a", "team", "u.md", "# U\n## H\nContent is used.")[1]
                same = make_chunks("team/a", "team", "d.md", "# D\n## H\nContent is defined.")[1]
                other = make_chunks("team/b", "team", "o.md", "# O\n## H\nContent is defined.")[1]
                user.meta = ChunkMeta(entities=[ChunkEntity(name="Content", kind="c", role="uses")])
                for chunk in (same, other): chunk.meta = ChunkMeta(entities=[ChunkEntity(name="Content", kind="c", role="defines")])
                catalog.upsert_document("team/a", "team", "", [user, same]); catalog.upsert_document("team/b", "team", "", [other])
                found = {item.chunk_id: item for item in neo_candidates(catalog, user, judge=True, settings=self.settings())}
                self.assertTrue(found[same.chunk_id].programmatic)
                self.assertEqual((found[other.chunk_id].source, found[other.chunk_id].programmatic), ("name_match", False))
            finally: catalog.close()

    def test_cross_document_links_need_llm_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Catalog.open(Path(tmp) / "catalog.db", mode="neo")
            try:
                target = make_chunks("team/t", "team", "t.md", "# T\n## H\nTarget.")[1]
                sibling = make_chunks("team/t", "team", "s.md", "# S\n## H\nSibling.")[1]
                others = [make_chunks(f"team/o{i}", "team", f"o{i}.md", f"# O{i}\n## H\nOther {i}.")[1] for i in range(8)]
                catalog.upsert_document("team/t", "team", "", [target, sibling])
                for i, item in enumerate(others): catalog.upsert_document(f"team/o{i}", "team", "", [item])
                found = [SimpleNamespace(chunk_id=item.chunk_id, source="topical", via=[]) for item in (sibling, *others)]
                asked = []
                class Model:
                    async def text(self, messages, **kwargs):
                        asked.append(1)
                        return "はい" if "Other 0" in messages[-1].content else "いいえ"
                settings = self.settings(); settings.wiki_linker_verify_top = 10
                accepted = asyncio.run(judge_edges(catalog, self.Engine(), target, found, settings, model=Model()))
                self.assertEqual({edge["chunk_b"] for edge in accepted}, {sibling.chunk_id, others[0].chunk_id})
                self.assertEqual(len(asked), 8)  # every cross-document link Jev accepted is checked
            finally: catalog.close()

    def test_shared_target_state_and_verify_candidate_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Catalog.open(Path(tmp) / "catalog.db", mode="neo")
            try:
                target = make_chunks("team/target", "team", "t.md", "# Target\n## H\n" + "target " * 1200)[1]
                candidate = make_chunks("team/peer", "team", "p.md", "# Peer\n## H\n" + "candidate " * 1200)[1]
                catalog.upsert_document("team/target", "team", "", [target])
                catalog.upsert_document("team/peer", "team", "", [candidate])
                engine = self.Engine()
                asyncio.run(judge_edges(catalog, engine, target,
                    [SimpleNamespace(chunk_id=candidate.chunk_id, source="topical", via=[])], self.settings()))
                screen_req = engine.calls[0][0]
                verify_reqs = engine.calls[1]
                # One shared target state per stage; the long candidate body rides
                # token-sized parts instead of a 6000-char truncation.
                self.assertEqual(set(screen_req.state), {"target"})
                self.assertTrue(all(set(r.state) == {"target"} for r in verify_reqs))
                self.assertEqual(len(verify_reqs[0].state["target"]["text"]), 6000)
                self.assertNotIn("本文: ", screen_req.question.text)
                self.assertTrue(all("本文: " in r.question.text for r in verify_reqs))
                bodies = [r.question.text.split("本文: ", 1)[1].split("\n候補の節は", 1)[0]
                          for r in verify_reqs]
                row = catalog.chunk(candidate.chunk_id)
                self.assertEqual("".join(bodies), row["body"])  # full body across parts, nothing cut
            finally: catalog.close()

    def test_verify_splits_long_bodies_into_best_of_parts(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Catalog.open(Path(tmp) / "catalog.db", mode="neo")
            try:
                target = make_chunks("team/target", "team", "t.md", "# Target\n## H\nA page with a target.")[1]
                candidate = make_chunks("team/peer", "team", "p.md", "# Peer\n## H\n" + "あ" * 20000)[1]
                catalog.upsert_document("team/target", "team", "", [target])
                catalog.upsert_document("team/peer", "team", "", [candidate])
                engine = self.Engine()
                accepted = asyncio.run(judge_edges(catalog, engine, target,
                    [SimpleNamespace(chunk_id=candidate.chunk_id, source="topical", via=[])], self.settings()))
                verify_reqs = engine.calls[1]
                self.assertGreater(len(verify_reqs), 1)  # split into parts, never truncated
                self.assertTrue(all(r.question.key == candidate.chunk_id for r in verify_reqs))
                bodies = [r.question.text.split("本文: ", 1)[1].split("\n候補の節は", 1)[0]
                          for r in verify_reqs]
                row = catalog.chunk(candidate.chunk_id)
                self.assertEqual("".join(bodies), row["body"])
                self.assertEqual([edge["chunk_b"] for edge in accepted], [candidate.chunk_id])
            finally: catalog.close()

    def test_no_uses_uses_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Catalog.open(Path(tmp) / "catalog.db", mode="neo")
            try:
                target = make_chunks("team/a", "team", "a.md", "# A\n## H\nWidget is used here.")[1]
                peer = make_chunks("team/b", "team", "b.md", "# B\n## H\nWidget is also used here.")[1]
                target.meta = ChunkMeta(entities=[ChunkEntity(name="Widget", kind="API", role="uses")])
                peer.meta = ChunkMeta(entities=[ChunkEntity(name="Widget", kind="API", role="uses")])
                catalog.upsert_document("team/a", "team", "", [target])
                catalog.upsert_document("team/b", "team", "", [peer])
                found = neo_candidates(catalog, target, judge=True, settings=self.settings())
                self.assertFalse(any(item.chunk_id == peer.chunk_id and item.source == "use" for item in found))
            finally: catalog.close()

    def test_jev_edges_survive_footer_filter(self):
        edge = RenderEdge("edge", "team/b/b.md", "B", "Heading", "related", "", True, "jev", [])
        self.assertEqual(footer_edges([edge]), [edge])

    def test_parallel_describe_all_matches_sequential_meta(self):
        from graph.linker.chunks import describe_all
        from graph.linker.prompts import chunk_meta_prompt
        class Model:
            async def text(self, messages, **kwargs): return '```json\n{"summary": "same"}\n```'
        source = "# T\n## A\nAlpha body.\n## B\nBeta body."
        sequential = make_chunks("team/doc", "team", "p.md", source)
        parallel = make_chunks("team/doc", "team", "p.md", source)
        asyncio.run(describe_all(sequential, model=Model(), output_language="ja", concurrency=2))
        asyncio.run(describe_all(parallel, model=Model(), output_language="ja", concurrency=2, parallel=True))
        self.assertEqual([x.meta.model_dump(exclude={"role_judge"}) for x in sequential],
                         [x.meta.model_dump(exclude={"role_judge"}) for x in parallel])
        prompt = chunk_meta_prompt(page_title="t", heading="h", document="d", text="x", output_language="ja")
        self.assertNotIn("role_judge", prompt.system)

    def test_describe_all_through_real_chat_port(self):
        # Fakes with **kwargs hid a bad keyword once; go through ChatModelPort.text itself.
        from graph.linker.chunks import describe_all
        from graph.wiki.config import WikiConfig
        from graph.wiki.model import ChatModelPort
        bound = {}
        class Llm:
            def bind(self, **kwargs):
                bound.update(kwargs)
                return self
            async def ainvoke(self, messages):
                return SimpleNamespace(content='{"summary": "real", "entities": [{"name": "Alpha", "kind": "API", "role": "defines"}]}')
        chunks = make_chunks("team/doc", "team", "p.md", "# T\nLead.\n## A\nAlpha body.")
        calls, fallbacks = asyncio.run(describe_all(chunks, model=ChatModelPort(WikiConfig(), llm=Llm()),
                                                    output_language="ja", concurrency=1, parallel=True))
        self.assertEqual((calls, fallbacks), (1, 0))
        self.assertEqual(chunks[1].meta.summary, "real")
        self.assertEqual([e.name for e in chunks[1].entities], ["Alpha"])
        self.assertTrue(bound["extra_body"]["chat_template_kwargs"]["enable_thinking"])

    def test_page_lead_paragraph_is_not_sent_to_the_model(self):
        from graph.linker.chunks import describe_all
        class Model:
            def __init__(self): self.calls = 0
            async def text(self, messages, **kwargs):
                self.calls += 1
                return '{"summary": "section"}'
        model = Model()
        sectioned = make_chunks("team/doc", "team", "p.md", "# T\nLead summary.\n## A\nAlpha body.")
        single = make_chunks("team/doc", "team", "q.md", "# Q\nOnly body.")
        asyncio.run(describe_all(sectioned + single, model=model, output_language="ja", concurrency=1, parallel=True))
        self.assertEqual(model.calls, 2)  # section A and the page without sections
        self.assertEqual(sectioned[0].meta.summary, "Lead summary.")
        self.assertEqual(single[0].meta.summary, "section")

    def test_curation_limits_llm_payload_after_jev(self):
        class Model:
            def __init__(self): self.payload = None
            async def structured(self, schema, messages, **kwargs):
                import json
                from graph.linker.wire import PageReferencePlan
                self.payload = json.loads(messages[1].content)
                return PageReferencePlan()
        edges = [RenderEdge(f"e{i}", f"team/d{i}/p.md", f"D{i}", "H", "related", "summary", True, "jev", [])
                 for i in range(12)]
        model = Model()
        asyncio.run(_curate_page(page_rel="team/page/p.md", original="# Page", edges=edges, current=[],
                                 previous_candidates=[], model=model, settings=self.settings(), big_document=False,
                                 mode="neo", jev_engine=self.Engine()))
        self.assertEqual(len(model.payload["candidates"]), 8)

    def test_related_documents_cached_and_rendered_for_both_docs(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_root=tmp, wiki_linker_judge="jev", wiki_index_related_docs=True)
            folders = {"team/a": Path(tmp), "team/b": Path(tmp), "other/c": Path(tmp)}
            summaries = {
                "team/a": {"name": "A", "scope": "auth", "keywords": ["auth", "login"], "entities": ["User"]},
                "team/b": {"name": "B", "scope": "auth flow", "keywords": ["auth", "session"], "entities": ["User"]},
                "other/c": {"name": "C", "scope": "auth", "keywords": ["auth"], "entities": ["User"]},
            }
            engine = self.Engine()
            with mock.patch("jev.get_engine", return_value=engine):
                related = _related_documents(settings, folders, summaries, None)
                call_count = len(engine.calls)
                again = _related_documents(settings, folders, summaries, None)
            self.assertEqual({name for name, _link in related["team/a"]}, {"B"})
            self.assertEqual({name for name, _link in related["team/b"]}, {"A"})
            self.assertEqual(len(engine.calls), call_count)
            self.assertEqual(again, related)
            body = render_document_index("A", [], lambda _filename: "", related["team/a"])
            self.assertIn("## 関連文書", body)
            self.assertIn("[B](/team/b/00-目次)", body)

    def test_jev_error_falls_back_to_llm_for_that_chunk(self):
        target = SimpleNamespace(chunk_id="target")
        engine = self.Engine()
        with mock.patch("graph.linker.jev_judge.judge_edges", side_effect=RuntimeError("unavailable")), \
             mock.patch("graph.linker.service._filter_groups", return_value=([{"chunk_b": "peer"}], 1)) as llm:
            accepted, calls, fallbacks = asyncio.run(_filter_target(
                None, object(), target, [SimpleNamespace(chunk_id="peer")], mode="neo", version="jev",
                artifact_dir=None, stop_check=None, output_language="", strict=True, judge="jev",
                jev_engine=engine, settings=self.settings()))
        self.assertEqual((accepted, calls, fallbacks), ([{"chunk_b": "peer"}], 1, 1))
        llm.assert_called_once()

    def test_llm_mode_keeps_existing_filter(self):
        engine = self.Engine()
        with mock.patch("graph.linker.service._filter_groups", return_value=([], 2)) as llm:
            result = asyncio.run(_filter_target(None, object(), SimpleNamespace(chunk_id="t"), [],
                                                 mode="neo", version="llm", artifact_dir=None,
                                                 stop_check=None, output_language="", strict=False,
                                                 judge="llm", jev_engine=engine, settings=self.settings()))
        self.assertEqual(result, ([], 2, 0))
        self.assertEqual(engine.calls, [])
        llm.assert_called_once()


class StructuredCapTest(unittest.TestCase):
    def test_first_structured_attempt_gets_token_cap_and_temperature(self):
        from graph.clients.chat import structured_ainvoke

        seen = {}

        class Structured:
            async def ainvoke(self, _messages):
                return ChunkMeta(summary="ok")

        class FakeLlm:
            def bind(self, **_kwargs):
                return self

            def with_structured_output(self, _schema, **kwargs):
                seen.update(kwargs)
                return Structured()

        result = asyncio.run(structured_ainvoke(FakeLlm(), ChunkMeta, [], max_output_tokens=123, temperature=0.7))
        self.assertEqual(result.summary, "ok")
        self.assertEqual(seen, {"max_tokens": 123, "temperature": 0.7})


if __name__ == "__main__":
    unittest.main()
