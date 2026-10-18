"""skillgram tests — stdlib only: python -m unittest discover -s tests -v"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import skillgram  # noqa: E402


def make_skill(root: Path, tipo: str, name: str, body: str = "hello") -> Path:
    d = root / tipo / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\n---\n{body}\n", encoding="utf-8")
    return d


class HomeCase(unittest.TestCase):
    """Aisla HOME/APPDATA para que nada toque el hub real."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self._old_home = skillgram.HOME
        self._old_appdata = os.environ.get("APPDATA")
        self._old_env_proj = os.environ.pop(skillgram.PROJECT_ENV, None)
        self._old_cwd = Path.cwd()
        skillgram.HOME = self.home
        os.environ["APPDATA"] = str(self.tmp / "appdata")
        self.hub_root = self.tmp / "hub"
        self.hub_root.mkdir()

    def tearDown(self):
        skillgram.HOME = self._old_home
        if self._old_appdata is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = self._old_appdata
        if self._old_env_proj is not None:
            os.environ[skillgram.PROJECT_ENV] = self._old_env_proj
        os.chdir(self._old_cwd)
        self._tmp.cleanup()


class TestHub(HomeCase):
    def test_skills_and_tipos(self):
        make_skill(self.hub_root, "infra-ops", "deploy")
        make_skill(self.hub_root, "design-ui", "archify")
        hub = skillgram.Hub(self.hub_root)
        self.assertEqual(hub.skills(), {"deploy": "infra-ops", "archify": "design-ui"})
        self.assertEqual(sorted(hub.tipos()), ["design-ui", "infra-ops"])

    def test_ignores_dirs_without_skill_md(self):
        (self.hub_root / "infra-ops" / "nope").mkdir(parents=True)
        make_skill(self.hub_root, "infra-ops", "ok")
        self.assertEqual(skillgram.Hub(self.hub_root).skills(), {"ok": "infra-ops"})

    def test_agents_only_dot_dirs_with_skills(self):
        (self.home / ".claude" / "skills").mkdir(parents=True)
        (self.home / ".codex").mkdir()
        (self.home / "regular" / "skills").mkdir(parents=True)
        names = [p.parent.name for p in skillgram.Hub(self.hub_root).agents()]
        self.assertIn(".claude", names)
        self.assertNotIn(".codex", names)
        self.assertNotIn("regular", names)


class TestProjectResolution(HomeCase):
    def test_explicit_wins(self):
        r = skillgram.resolve_project(str(self.tmp / "proj"))
        self.assertEqual(r["source"], "explicit")
        self.assertEqual(r["name"], "proj")

    def test_env_beats_config(self):
        proj = self.tmp / "proj"
        (proj / ".agents").mkdir(parents=True)
        (proj / ".agents" / "config.json").write_text('{"project_name": "from-config"}', encoding="utf-8")
        os.environ[skillgram.PROJECT_ENV] = str(proj)
        r = skillgram.resolve_project()
        self.assertEqual(r["source"], "env")

    def test_config_discovered_from_subdir_up_to_git_root(self):
        proj = self.tmp / "proj"
        (proj / ".git").mkdir(parents=True)
        (proj / ".agents").mkdir()
        (proj / ".agents" / "config.json").write_text('{"project_name": "lotly"}', encoding="utf-8")
        sub = proj / "src" / "deep"
        sub.mkdir(parents=True)
        os.chdir(sub)
        r = skillgram.resolve_project()
        self.assertEqual(r["source"], "config")
        self.assertEqual(r["name"], "lotly")
        self.assertEqual(Path(r["path"]), proj)

    def test_falls_back_to_git_root_basename(self):
        proj = self.tmp / "repo"
        (proj / ".git").mkdir(parents=True)
        (proj / "sub").mkdir()
        os.chdir(proj / "sub")
        r = skillgram.resolve_project()
        self.assertEqual(r["source"], "cwd")
        self.assertEqual(r["name"], "repo")


class TestProjectScope(HomeCase):
    def test_init_writes_config_and_is_idempotent(self):
        proj = self.tmp / "proj"
        first = skillgram.init_project("my-api", str(proj))
        self.assertTrue(first["ok"])
        cfg = proj / ".agents" / "config.json"
        self.assertEqual(json.loads(cfg.read_text(encoding="utf-8"))["project_name"], "my-api")
        self.assertTrue((proj / ".agents" / "skills").is_dir())
        second = skillgram.init_project("other", str(proj))
        self.assertFalse(second["ok"])
        forced = skillgram.init_project("other", str(proj), force=True)
        self.assertTrue(forced["ok"])

    def test_hash_detects_content_change(self):
        d = make_skill(self.hub_root, "infra-ops", "deploy", "v1")
        h1 = skillgram.skill_hash(d)
        (d / "SKILL.md").write_text("v2", encoding="utf-8")
        self.assertNotEqual(h1, skillgram.skill_hash(d))

    @unittest.skipUnless(sys.platform == "win32", "junctions: Windows only")
    def test_promote_unpromote_and_status(self):
        make_skill(self.hub_root, "infra-ops", "deploy")
        hub = skillgram.Hub(self.hub_root)
        proj = self.tmp / "proj"
        skillgram.init_project("proj", str(proj))

        res = skillgram.promote(hub, ["deploy"], proj)
        self.assertEqual(res["creadas"], ["deploy"])
        link = proj / ".agents" / "skills" / "deploy"
        self.assertTrue(skillgram.is_link(link))
        self.assertEqual(Path(skillgram.link_target(link)).resolve(), (self.hub_root / "infra-ops" / "deploy").resolve())

        again = skillgram.promote(hub, ["deploy"], proj)
        self.assertEqual(again["creadas"], [])
        self.assertEqual(again["ya_estaban"], ["deploy"])

        st = skillgram.project_status(hub, proj)
        self.assertEqual(st["promocionadas"], 1)
        self.assertEqual(st["sanas"], ["deploy"])
        self.assertEqual(st["rotas"], [])

        out = skillgram.unpromote(proj, ["deploy"])
        self.assertEqual(out["retiradas"], ["deploy"])
        self.assertFalse(os.path.lexists(link))

    @unittest.skipUnless(sys.platform == "win32", "junctions: Windows only")
    def test_unknown_skill_is_reported_not_created(self):
        hub = skillgram.Hub(self.hub_root)
        proj = self.tmp / "proj"
        res = skillgram.promote(hub, ["ghost"], proj)
        self.assertFalse(res["ok"])
        self.assertEqual(res["desconocidas"], ["ghost"])
        self.assertFalse((proj / ".agents" / "skills" / "ghost").exists())


class TestOnboard(HomeCase):
    def test_plan_imports_native_skill(self):
        make_skill(self.hub_root, "infra-ops", "existing")
        agent = self.home / ".fakeagent" / "skills"
        make_skill(agent.parent, "skills", "native-one")
        plan = skillgram.build_onboard_plan(skillgram.Hub(self.hub_root), scope="all")
        apps = {h["app"]: h["nativas"] for h in plan["harnesses"]}
        self.assertEqual(apps.get("fakeagent"), 1)
        self.assertEqual(plan["resumen"]["import"], 1)
        # la categoria propuesta sale del hub (nunca inventa una nueva)
        self.assertIn(plan["acciones"][0]["categoria"], skillgram.Hub(self.hub_root).tipos() or [skillgram.DEFAULT_CATEGORY])

    def test_plan_flags_conflict_with_hub(self):
        make_skill(self.hub_root, "infra-ops", "deploy")
        make_skill(self.home / ".fakeagent", "skills", "deploy")
        plan = skillgram.build_onboard_plan(skillgram.Hub(self.hub_root), scope="all")
        self.assertEqual(plan["resumen"]["conflict"], 1)

    def test_scope_current_filters_harness(self):
        make_skill(self.home / ".a", "skills", "one")
        make_skill(self.home / ".b", "skills", "two")
        plan = skillgram.build_onboard_plan(skillgram.Hub(self.hub_root), scope="current", harness="a")
        self.assertEqual([h["app"] for h in plan["harnesses"]], ["a"])

    def test_llm_config_none_without_key(self):
        saved = {k: os.environ.pop(k, None) for k in ("SKILLGRAM_LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY")}
        try:
            self.assertIsNone(skillgram.llm_config())
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
