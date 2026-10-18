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




class TestMount(HomeCase):
    def _project_with_scope(self):
        make_skill(self.hub_root, "infra-ops", "deploy")
        make_skill(self.hub_root, "design-ui", "archify")
        proj = self.tmp / "proj"
        skillgram.init_project("proj", str(proj))
        sk = skillgram.Hub(self.hub_root)
        skillgram.promote(sk, ["deploy"], proj)
        (self.home / ".claude" / "skills").mkdir(parents=True)
        return sk, proj

    def test_scope_only_has_promoted(self):
        sk, proj = self._project_with_scope()
        scope = skillgram.project_scope(sk, proj)
        self.assertEqual([r["name"] for r in scope["skills"]], ["deploy"])
        self.assertEqual(scope["skills"][0]["tipo"], "infra-ops")

    @unittest.skipUnless(sys.platform == "win32", "junctions: Windows only")
    def test_mount_materializes_only_project_scope(self):
        sk, proj = self._project_with_scope()
        plan = skillgram.mount_project(sk, proj, apply=False)
        self.assertEqual(plan["skills"], 1)
        ids = [r["harness"] for r in plan["harnesses"]]
        self.assertIn("claude-code", ids)
        applied = skillgram.mount_project(sk, proj, apply=True)
        claude_dir = proj / ".claude" / "skills"
        names = sorted(p.name for p in claude_dir.iterdir())
        self.assertEqual(names, ["deploy"])  # 1, no las 2 del hub
        self.assertTrue(any(r["creados"] for r in applied["harnesses"] if r["harness"] == "claude-code"))

    def test_mount_without_scope_fails_loudly(self):
        make_skill(self.hub_root, "infra-ops", "deploy")
        proj = self.tmp / "proj"
        skillgram.init_project("proj", str(proj))
        res = skillgram.mount_project(skillgram.Hub(self.hub_root), proj, apply=False)
        self.assertFalse(res["ok"])
        self.assertIn("use", res["error"])

    @unittest.skipUnless(sys.platform == "win32", "junctions: Windows only")
    def test_prune_global_counts_hub_links(self):
        make_skill(self.hub_root, "infra-ops", "deploy")
        agent = self.home / ".claude" / "skills"
        agent.mkdir(parents=True)
        skillgram.sync(skillgram.Hub(self.hub_root), quiet=True)
        res = skillgram.prune_global_links(skillgram.Hub(self.hub_root), apply=False)
        self.assertFalse(res["aplicado"])
        self.assertGreaterEqual(res.get("pendientes", 0), 1)
        done = skillgram.prune_global_links(skillgram.Hub(self.hub_root), apply=True)
        self.assertTrue(done["aplicado"])
        self.assertEqual([p for p in agent.iterdir() if skillgram.is_link(p)], [])


class TestFlavor(HomeCase):
    def test_detects_agent_wired_skill(self):
        d = make_skill(self.hub_root, "infra-ops", "wired",
                       "Corre `claude -p` y usa .claude/ hooks; ver claude code docs.")
        det = skillgram.detect_flavor(d)
        self.assertEqual(det["flavor"], "claude")
        self.assertGreaterEqual(det["score"]["claude"], 2)

    def test_generic_when_no_signal(self):
        d = make_skill(self.hub_root, "infra-ops", "plain", "Hace cosas genericas.")
        self.assertEqual(skillgram.detect_flavor(d)["flavor"], "generic")

    def test_meta_sidecar_roundtrip(self):
        d = make_skill(self.hub_root, "infra-ops", "x")
        skillgram.write_skill_meta(d, {"flavor": "codex"})
        self.assertEqual(skillgram.skill_meta(d)["flavor"], "codex")

    def test_port_skips_generic_and_without_llm(self):
        make_skill(self.hub_root, "infra-ops", "plain", "generica")
        hub = skillgram.Hub(self.hub_root)
        self.assertIn("generica", skillgram.port_skill(hub, "plain", "reasonix", None).get("skipped", ""))
        wired = make_skill(self.hub_root, "infra-ops", "wired", "`claude -p` y .claude/ x")
        skillgram.write_skill_meta(wired, {"flavor": "claude"})
        res = skillgram.port_skill(hub, "wired", "reasonix", None)
        self.assertFalse(res["ok"])
        self.assertIn("LLM", res["error"])


class TestPrecedence(HomeCase):
    def test_toml_paths_prepended(self):
        text = '[skills]\npaths = ["C:/a", "C:/b"]\n[kernel]\nx = 1\n'
        new, changed = skillgram._prepend_toml_paths(text, "C:/hub")
        self.assertTrue(changed)
        self.assertIn('["C:/hub", "C:/a", "C:/b"]', new)
        again, changed2 = skillgram._prepend_toml_paths(new, "C:/hub")
        self.assertFalse(changed2)

    def test_yaml_external_dirs_prepended(self):
        text = "model:\n  default: x\nskills:\n  creation_nudge_interval: 15\n"
        new, changed = skillgram._prepend_yaml_external_dirs(text, "C:/hub")
        self.assertTrue(changed)
        self.assertIn("external_dirs:", new)
        self.assertIn("- C:/hub", new)
        again, changed2 = skillgram._prepend_yaml_external_dirs(new, "C:/hub")
        self.assertFalse(changed2)


class TestBootstrap(HomeCase):
    def test_signals_from_files(self):
        proj = self.tmp / "app"
        (proj).mkdir()
        (proj / "package.json").write_text('{"dependencies":{"next":"15","@stripe/x":"1"}}', encoding="utf-8")
        (proj / "Dockerfile").write_text("FROM node", encoding="utf-8")
        (proj / ".github" / "workflows").mkdir(parents=True)
        sig = skillgram.repo_signals(proj)
        self.assertIn("node", sig["files"])
        self.assertIn("docker", sig["files"])
        self.assertIn("ci", sig["files"])
        self.assertIn("next", sig["tags"])
        self.assertIn("stripe", sig["tags"])

    def test_candidates_ranked_by_signals(self):
        make_skill(self.hub_root, "infra-ops", "docker-patterns", "docker compose patterns")
        make_skill(self.hub_root, "design-ui", "archify", "diagramas")
        hub = skillgram.Hub(self.hub_root)
        rows = skillgram.candidate_skills(hub, {"tags": ["docker"], "files": [], "deps": []})
        self.assertEqual(rows[0]["skill"], "docker-patterns")
        self.assertEqual(len(rows), 1)

    def test_missing_found_and_not_found(self):
        make_skill(self.hub_root, "infra-ops", "docker-local-deploy", "deploy docker a un vps")
        hub = skillgram.Hub(self.hub_root)
        hit = skillgram.missing(hub, "docker deploy vps")
        self.assertTrue(hit["found"])
        self.assertEqual(hit["candidates"][0]["skill"], "docker-local-deploy")
        miss = skillgram.missing(hub, "cotizar credito hipotecario")
        self.assertFalse(miss["found"])
        self.assertEqual(miss["candidates"], [])

    def test_bootstrap_without_scope_and_without_llm(self):
        make_skill(self.hub_root, "infra-ops", "docker-patterns", "docker stuff")
        proj = self.tmp / "app"
        proj.mkdir()
        (proj / "Dockerfile").write_text("x", encoding="utf-8")
        res = skillgram.bootstrap(skillgram.Hub(self.hub_root), proj, None, apply=False)
        self.assertEqual(res["op"], "bootstrap")
        self.assertEqual(res["seleccion"], [])
        self.assertIn("sin LLM", res["note"])
        self.assertGreaterEqual(res["candidatas"], 1)

    def test_bootstrap_skips_when_scope_exists(self):
        make_skill(self.hub_root, "infra-ops", "deploy")
        proj = self.tmp / "app"
        sk = skillgram.Hub(self.hub_root)
        skillgram.init_project("app", str(proj))
        skillgram.promote(sk, ["deploy"], proj)
        res = skillgram.bootstrap(sk, proj, None)
        self.assertIn("ya tiene scope", res["skipped"])

    def test_propose_requires_llm(self):
        proj = self.tmp / "app"
        proj.mkdir()
        res = skillgram.propose_skill(skillgram.Hub(self.hub_root), proj, "ci pipeline", None)
        self.assertFalse(res["ok"])
        self.assertIn("LLM", res["error"])


class TestIndex(HomeCase):
    def test_build_and_search(self):
        make_skill(self.hub_root, "infra-ops", "docker-deploy", "deploy docker compose to a vps")
        make_skill(self.hub_root, "design-ui", "archify", "diagramas de arquitectura")
        hub = skillgram.Hub(self.hub_root)
        db = self.tmp / "idx.db"
        built = skillgram.index_build(hub, path=db)
        self.assertEqual(built["skills"], 2)
        res = skillgram.index_search("docker vps", path=db)
        self.assertEqual(res["resultados"][0]["skill"], "docker-deploy")
        self.assertEqual(res["indexadas"], 2)

    def test_search_without_match(self):
        make_skill(self.hub_root, "infra-ops", "docker-deploy", "deploy docker")
        hub = skillgram.Hub(self.hub_root)
        db = self.tmp / "idx.db"
        skillgram.index_build(hub, path=db)
        res = skillgram.index_search("hipotecario credito", path=db)
        self.assertEqual(res["resultados"], [])


class TestLifecycle(HomeCase):
    def _project(self):
        make_skill(self.hub_root, "infra-ops", "deploy")
        proj = self.tmp / "proj"
        hub = skillgram.Hub(self.hub_root)
        skillgram.init_project("proj", str(proj))
        skillgram.promote(hub, ["deploy"], proj)
        return hub, proj

    def test_touch_usage_and_pin(self):
        _hub, proj = self._project()
        skillgram.touch_usage(proj, ["deploy"])
        skillgram.touch_usage(proj, ["deploy"])
        manifest = json.loads((proj / ".agents" / "skills.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["skills"]["deploy"]["use_count"], 2)
        self.assertIn("last_used", manifest["skills"]["deploy"])
        skillgram.pin_skill(proj, ["deploy"])
        manifest = json.loads((proj / ".agents" / "skills.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["skills"]["deploy"]["pinned"])

    def test_review_lists_never_used(self):
        hub, proj = self._project()
        rev = skillgram.review_list(hub, proj)
        self.assertEqual(rev["nunca_usadas"], ["deploy"])
        skillgram.touch_usage(proj, ["deploy"])
        self.assertEqual(skillgram.review_list(hub, proj)["nunca_usadas"], [])

    def test_retire_moves_and_tombstones(self):
        hub, _proj = self._project()
        res = skillgram.retire_skill(hub, "deploy", reason="test")
        self.assertTrue(res["ok"])
        self.assertNotIn("deploy", hub.skills())          # fuera del hub activo
        self.assertTrue((self.hub_root / "_retired" / "deploy" / "SKILL.md").is_file())
        log = json.loads((self.hub_root / "_retired" / "_retired.json").read_text(encoding="utf-8"))
        self.assertEqual(log[0]["skill"], "deploy")
        self.assertEqual(log[0]["reason"], "test")

    def test_retired_dir_is_not_a_category(self):
        make_skill(self.hub_root, "infra-ops", "x")
        (self.hub_root / "_retired" / "y").mkdir(parents=True)
        (self.hub_root / "_retired" / "y" / "SKILL.md").write_text("---\nname: y\n---\n", encoding="utf-8")
        hub = skillgram.Hub(self.hub_root)
        self.assertNotIn("_retired", hub.tipos())
        self.assertEqual(list(hub.skills()), ["x"])


class TestExportImport(HomeCase):
    def test_roundtrip(self):
        make_skill(self.hub_root, "infra-ops", "deploy", "v1")
        hub = skillgram.Hub(self.hub_root)
        out = self.tmp / "hub.tar.gz"
        exp = skillgram.export_hub(hub, out)
        self.assertEqual(exp["skills"], 1)
        self.assertTrue(out.is_file())

        other_root = self.tmp / "other"
        other_root.mkdir()
        other = skillgram.Hub(other_root)
        pre = skillgram.import_hub(other, out, apply=False)
        self.assertEqual(pre["importadas"], ["deploy"])
        self.assertFalse((other_root / "infra-ops" / "deploy").exists())
        done = skillgram.import_hub(other, out, apply=True)
        self.assertEqual(done["importadas"], ["deploy"])
        self.assertTrue((other_root / "infra-ops" / "deploy" / "SKILL.md").is_file())
        again = skillgram.import_hub(other, out, apply=True)
        self.assertEqual(again["ya_existian"], ["deploy"])


class TestMcp(HomeCase):
    def test_tools_list_and_call(self):
        make_skill(self.hub_root, "infra-ops", "deploy", "deploy docker")
        hub = skillgram.Hub(self.hub_root)
        self.assertEqual(len(skillgram.MCP_TOOLS), 5)
        res = skillgram.mcp_call(hub, "skillgram_missing", {"query": "docker deploy"})
        self.assertTrue(res["found"])
        bad = skillgram.mcp_call(hub, "nope", {})
        self.assertFalse(bad["ok"])

    def test_stdio_session(self):
        import io

        make_skill(self.hub_root, "infra-ops", "deploy", "deploy docker")
        hub = skillgram.Hub(self.hub_root)
        stdin = io.StringIO(
            '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
            '{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n'
            '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"skillgram_status","arguments":{}}}\n'
        )
        stdout = io.StringIO()
        skillgram.mcp_serve(hub, stdin=stdin, stdout=stdout)
        lines = [json.loads(line) for line in stdout.getvalue().strip().splitlines()]
        self.assertEqual(lines[0]["result"]["serverInfo"]["name"], "skillgram")
        self.assertEqual(len(lines[1]["result"]["tools"]), 5)
        self.assertIn("content", lines[2]["result"])
if __name__ == "__main__":
    unittest.main()
