"""skillgram tests — stdlib only: python -m unittest discover -s tests -v"""

import contextlib
import io
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
    def test_the_hub_itself_is_never_an_agent(self):
        """`~/.agents/skills` cae en el barrido `~/.*/skills`: enlazarlo a si mismo
        deja 842 junctions y hace que git materialice copias del hub dentro del hub."""
        hub_here = self.home / ".agents" / "skills"
        (hub_here / "infra-ops" / "deploy").mkdir(parents=True)
        (hub_here / "infra-ops" / "deploy" / "SKILL.md").write_text("x", encoding="utf-8")
        (self.home / ".claude" / "skills").mkdir(parents=True)
        agents = [p.resolve() for p in skillgram.Hub(hub_here).agents()]
        self.assertIn((self.home / ".claude" / "skills").resolve(), agents)
        self.assertNotIn(hub_here.resolve(), agents)

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

    def test_promote_unpromote_and_status(self):
        make_skill(self.hub_root, "infra-ops", "deploy")
        hub = skillgram.Hub(self.hub_root)
        proj = self.tmp / "proj"
        skillgram.init_project("proj", str(proj))

        res = skillgram.promote(hub, ["deploy"], proj)
        self.assertEqual(res["created"], ["deploy"])
        link = proj / ".agents" / "skills" / "deploy"
        self.assertTrue(skillgram.is_link(link))
        self.assertEqual(Path(skillgram.link_target(link)).resolve(), (self.hub_root / "infra-ops" / "deploy").resolve())

        again = skillgram.promote(hub, ["deploy"], proj)
        self.assertEqual(again["created"], [])
        self.assertEqual(again["already_present"], ["deploy"])

        st = skillgram.project_status(hub, proj)
        self.assertEqual(st["promoted"], 1)
        self.assertEqual(st["healthy"], ["deploy"])
        self.assertEqual(st["broken"], [])

        out = skillgram.unpromote(proj, ["deploy"])
        self.assertEqual(out["removed"], ["deploy"])
        self.assertFalse(os.path.lexists(link))

    @unittest.skipUnless(sys.platform == "win32", "junctions: Windows only")
    def test_unknown_skill_is_reported_not_created(self):
        hub = skillgram.Hub(self.hub_root)
        proj = self.tmp / "proj"
        res = skillgram.promote(hub, ["ghost"], proj)
        self.assertFalse(res["ok"])
        self.assertEqual(res["unknown"], ["ghost"])
        self.assertFalse((proj / ".agents" / "skills" / "ghost").exists())


class TestOnboard(HomeCase):
    def test_plan_imports_native_skill(self):
        make_skill(self.hub_root, "infra-ops", "existing")
        agent = self.home / ".fakeagent" / "skills"
        make_skill(agent.parent, "skills", "native-one")
        plan = skillgram.build_onboard_plan(skillgram.Hub(self.hub_root), scope="all")
        apps = {h["app"]: h["native"] for h in plan["harnesses"]}
        self.assertEqual(apps.get("fakeagent"), 1)
        self.assertEqual(plan["summary"]["import"], 1)
        # la categoria propuesta sale del hub (nunca inventa una nueva)
        self.assertIn(plan["actions"][0]["category"], skillgram.Hub(self.hub_root).tipos() or [skillgram.DEFAULT_CATEGORY])

    def test_plan_flags_conflict_with_hub(self):
        make_skill(self.hub_root, "infra-ops", "deploy")
        make_skill(self.home / ".fakeagent", "skills", "deploy")
        plan = skillgram.build_onboard_plan(skillgram.Hub(self.hub_root), scope="all")
        self.assertEqual(plan["summary"]["conflict"], 1)

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
        self.assertFalse(res["applied"])
        self.assertGreaterEqual(res.get("pendientes", 0), 1)
        done = skillgram.prune_global_links(skillgram.Hub(self.hub_root), apply=True)
        self.assertTrue(done["applied"])
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

    def test_same_hub_with_other_separator_is_not_duplicated(self):
        text = "skills:\n  external_dirs:\n    - C:/Users/me/.agents/skills\n"
        new, changed = skillgram._prepend_yaml_external_dirs(text, "C:\\Users\\me\\.agents\\skills")
        self.assertFalse(changed)
        self.assertEqual(new.count("external_dirs"), 1)
        toml = '[skills]\npaths = ["C:/Users/me/.agents/skills"]\n'
        new2, changed2 = skillgram._prepend_toml_paths(toml, "C:\\Users\\me\\.agents\\skills")
        self.assertFalse(changed2)

    def test_only_filter_touches_one_harness(self):
        (self.home / ".hermes").mkdir(parents=True)
        hermes_cfg = self.home / ".hermes" / "config.yaml"
        hermes_cfg.write_text("skills:\n  creation_nudge_interval: 15\n", encoding="utf-8")
        hub = skillgram.Hub(self.hub_root)
        res = skillgram.apply_precedence(hub, apply=False, only="reasonix")
        self.assertEqual([r["harness"] for r in res["configs"]], [])
        # sin filtro, hermes entra en la lista
        sin_filtro = skillgram.apply_precedence(hub, apply=False)
        self.assertIn("hermes", [r["harness"] for r in sin_filtro["configs"]])

    def test_apply_writes_a_backup_and_is_idempotent(self):
        (self.home / ".hermes").mkdir(parents=True)
        cfg = self.home / ".hermes" / "config.yaml"
        cfg.write_text("skills:\n  creation_nudge_interval: 15\n", encoding="utf-8")
        hub = skillgram.Hub(self.hub_root)
        first = skillgram.apply_precedence(hub, apply=True, only="hermes")
        self.assertTrue(first["configs"][0]["applied"])
        self.assertTrue(Path(first["configs"][0]["backup"]).is_file())
        self.assertIn(str(self.hub_root), cfg.read_text(encoding="utf-8"))
        second = skillgram.apply_precedence(hub, apply=True, only="hermes")
        self.assertFalse(second["configs"][0]["cambiado"])


class TestSkillContract(HomeCase):
    """`lint --contract` is the authoring gate: the five sections are required."""

    FULL = (
        "## Activation Contract\n\nUse when: a build fails.\n\nNot for: a lint error.\n\n"
        "## Hard Rules\n\n1. Never skip the test step.\n\n"
        "## Decision Gates\n\n| Observation | Do this |\n|---|---|\n| red build | read the log |\n\n"
        "## Execution Steps\n\n1. Run `make build`.\n\n"
        "## Output Contract\n\nReport: the failing step.\n"
    )

    def _skill(self, body: str, name: str = "demo") -> Path:
        d = self.hub_root / "infra-ops" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Use when a build fails on a native dependency in CI.\n---\n{body}",
            encoding="utf-8",
        )
        return d

    def test_complete_skill_passes_the_contract(self):
        res = skillgram.lint_skill(self._skill(self.FULL), require_contract=True)
        self.assertEqual(res["errors"], [])
        self.assertEqual(res["sections"], list(skillgram.CONTRACT_SECTIONS))

    def test_missing_sections_are_warnings_by_default_and_errors_with_contract(self):
        d = self._skill("Just a paragraph that says nothing about when to use it.\n\nAnd more.\n")
        lenient = skillgram.lint_skill(d)
        self.assertEqual(lenient["errors"], [])
        self.assertEqual(len(lenient["warnings"]), len(skillgram.CONTRACT_SECTIONS))
        strict = skillgram.lint_skill(d, require_contract=True)
        self.assertEqual(len(strict["errors"]), len(skillgram.CONTRACT_SECTIONS))

    def test_empty_section_is_an_error(self):
        body = self.FULL.replace("Use when: a build fails.\n\nNot for: a lint error.", "")
        res = skillgram.lint_skill(self._skill(body), require_contract=True)
        self.assertIn("empty section: ## Activation Contract", res["errors"])

    def test_placeholder_and_live_credential_are_errors(self):
        todo = skillgram.lint_skill(self._skill(self.FULL + "\nTODO write the rest\n"), require_contract=True)
        self.assertTrue(any("placeholder" in e for e in todo["errors"]))
        leaked = skillgram.lint_skill(
            self._skill(self.FULL + "\nexport STRIPE=sk_live_" + "a1B2c3D4e5F6g7H8i9J0k1L2\n"),
            require_contract=True,
        )
        self.assertTrue(any("live credential" in e for e in leaked["errors"]))

    def test_documentation_placeholders_pass(self):
        """A `ghp_xxx` example and a PEM banner alone are documentation, not a leak."""
        body = self.FULL + "\nExample token: ghp_xxxxxxxxxxxxxxxxxxxxxxxx\n-----BEGIN PRIVATE KEY-----\n"
        res = skillgram.lint_skill(self._skill(body), require_contract=True)
        self.assertEqual(res["errors"], [])

    def test_short_description_fails(self):
        d = self.hub_root / "infra-ops" / "tbd"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\nname: tbd\ndescription: Helps.\n---\n" + self.FULL, encoding="utf-8")
        res = skillgram.lint_skill(d)
        self.assertTrue(any("min 20" in e for e in res["errors"]))

    def test_a_bom_is_not_a_missing_frontmatter(self):
        """Windows editors write a BOM; that must not read as "no frontmatter"."""
        d = self.hub_root / "infra-ops" / "bommed"
        d.mkdir(parents=True)
        body = "---\nname: bommed\ndescription: Use when a Windows editor added a BOM to the file.\n---\n"
        (d / "SKILL.md").write_bytes(("\ufeff" + body + self.FULL).encode("utf-8"))
        res = skillgram.lint_skill(d, require_contract=True)
        self.assertEqual(res["errors"], [])


class TestNew(HomeCase):
    def test_new_scaffolds_the_contract_and_the_sidecar(self):
        (self.hub_root / "infra-ops").mkdir(parents=True)
        res = skillgram.new_skill(skillgram.Hub(self.hub_root), "my-skill", category="infra-ops")
        self.assertTrue(res["ok"], res)
        d = Path(res["path"])
        text = (d / "SKILL.md").read_text(encoding="utf-8")
        for section in skillgram.CONTRACT_SECTIONS:
            self.assertIn(f"## {section}", text)
        meta = json.loads((d / skillgram.SKILL_META).read_text(encoding="utf-8"))
        self.assertEqual(meta["by"], "human")
        self.assertEqual(meta["source"]["kind"], "authored")
        self.assertIn("hash", meta)

    def test_the_scaffold_fails_the_gate_until_it_is_filled(self):
        (self.hub_root / "infra-ops").mkdir(parents=True)
        res = skillgram.new_skill(skillgram.Hub(self.hub_root), "my-skill", category="infra-ops")
        gate = skillgram.lint_skill(Path(res["path"]), require_contract=True)
        self.assertTrue(any("placeholder" in e for e in gate["errors"]))

    def test_new_refuses_duplicates_and_bad_input(self):
        (self.hub_root / "infra-ops").mkdir(parents=True)
        hub = skillgram.Hub(self.hub_root)
        self.assertTrue(skillgram.new_skill(hub, "dup", category="infra-ops")["ok"])
        again = skillgram.new_skill(hub, "dup", category="infra-ops")
        self.assertFalse(again["ok"])
        self.assertIn("already exists", again["error"])
        self.assertIn("kebab-case", skillgram.new_skill(hub, "Not A Slug")["error"])
        self.assertIn("unknown category", skillgram.new_skill(hub, "x", category="nope")["error"])


class TestListAndTouch(HomeCase):
    """The governance view: what is in the hub and what was declared as used."""

    def _hub_with_two(self):
        make_skill(self.hub_root, "infra-ops", "deploy")
        make_skill(self.hub_root, "design-ui", "archify")
        skillgram.write_skill_meta(self.hub_root / "infra-ops" / "deploy",
                                   {"by": "human", "source": {"kind": "authored"}})
        return skillgram.Hub(self.hub_root)

    def test_list_all_and_by_category(self):
        hub = self._hub_with_two()
        self.assertEqual(skillgram.list_skills(hub)["skills"], 2)
        infra = skillgram.list_skills(hub, category="infra-ops")
        self.assertEqual([r["skill"] for r in infra["rows"]], ["deploy"])
        self.assertEqual(infra["rows"][0]["source"], "authored")
        self.assertEqual(infra["rows"][0]["by"], "human")

    def test_used_filter_reads_the_project_manifest(self):
        hub = self._hub_with_two()
        proj = self.tmp / "proj"
        skillgram.init_project("proj", str(proj))
        skillgram.promote(hub, ["deploy", "archify"], proj)
        self.assertEqual(skillgram.list_skills(hub, project=proj, used="used")["skills"], 0)
        skillgram.touch_usage(proj, ["deploy"], by="reasonix")
        used = skillgram.list_skills(hub, project=proj, used="used")
        self.assertEqual([r["skill"] for r in used["rows"]], ["deploy"])
        self.assertEqual(used["rows"][0]["use_count"], 1)
        self.assertEqual(skillgram.list_skills(hub, project=proj, used="unused")["skills"], 1)

    def test_touch_reports_what_is_not_in_scope(self):
        hub = self._hub_with_two()
        proj = self.tmp / "proj"
        skillgram.init_project("proj", str(proj))
        skillgram.promote(hub, ["deploy"], proj)
        res = skillgram.touch_usage(proj, ["deploy", "ghost"], by="codex")
        self.assertEqual(res["touched"], ["deploy"])
        self.assertEqual(res["not_in_scope"], ["ghost"])
        self.assertIn("use", res["hint"])

    def test_mcp_exposes_touch_and_list(self):
        names = [t["name"] for t in skillgram.MCP_TOOLS]
        self.assertIn("skillgram_touch", names)
        self.assertIn("skillgram_list", names)
        self.assertEqual(len(names), 7)
        out = skillgram.mcp_call(self._hub_with_two(), "skillgram_list", {"category": "design-ui"})
        self.assertEqual([r["skill"] for r in out["rows"]], ["archify"])


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
        self.assertEqual(res["selection"], [])
        self.assertIn("no LLM", res["note"])
        self.assertGreaterEqual(res["candidates"], 1)

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
        self.assertEqual(res["results"][0]["skill"], "docker-deploy")
        self.assertEqual(res["indexed"], 2)

    def test_search_without_match(self):
        make_skill(self.hub_root, "infra-ops", "docker-deploy", "deploy docker")
        hub = skillgram.Hub(self.hub_root)
        db = self.tmp / "idx.db"
        skillgram.index_build(hub, path=db)
        res = skillgram.index_search("hipotecario credito", path=db)
        self.assertEqual(res["results"], [])


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
        self.assertEqual(rev["never_used"], ["deploy"])
        skillgram.touch_usage(proj, ["deploy"])
        self.assertEqual(skillgram.review_list(hub, proj)["never_used"], [])

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
        self.assertEqual(pre["imported"], ["deploy"])
        self.assertFalse((other_root / "infra-ops" / "deploy").exists())
        done = skillgram.import_hub(other, out, apply=True)
        self.assertEqual(done["imported"], ["deploy"])
        self.assertTrue((other_root / "infra-ops" / "deploy" / "SKILL.md").is_file())
        again = skillgram.import_hub(other, out, apply=True)
        self.assertEqual(again["already_present"], ["deploy"])


class TestMcp(HomeCase):
    def test_tools_list_and_call(self):
        make_skill(self.hub_root, "infra-ops", "deploy", "deploy docker")
        hub = skillgram.Hub(self.hub_root)
        self.assertEqual(len(skillgram.MCP_TOOLS), 7)
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
        self.assertEqual(len(lines[1]["result"]["tools"]), 7)
        self.assertIn("content", lines[2]["result"])
class FakeCli:
    """Stands in for `reasonix|codex|claude|hermes mcp ...` so no real config is touched."""

    def __init__(self, add_exit: int = 0, point_elsewhere: bool = False):
        self.calls = []
        self.registered = False
        self.add_exit = add_exit
        self.add_argv = None
        self.point_elsewhere = point_elsewhere

    def marker(self) -> str:
        parts = os.environ.get("SKILLGRAM_MCP_CMD", "").split()
        return parts[1] if len(parts) > 1 else "skillgram"

    def __call__(self, argv):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        verb = argv[2] if len(argv) > 2 else ""
        marker = "C:/somewhere/else/other-mcp.py" if self.point_elsewhere else self.marker()
        if verb == "get":
            return (0, f"{argv[3]}\n  command: python\n  args: {marker} mcp") if self.registered else (1, "not found")
        if verb == "list":
            return 0, (f"skillgram  stdio  {marker} mcp" if self.registered else "no servers configured")
        if verb == "add":
            if self.add_exit == 0:
                self.registered = True
                self.add_argv = argv
            return self.add_exit, "added" if self.add_exit == 0 else "boom: config is locked"
        return 0, ""

    def adds(self) -> list:
        return [c for c in self.calls if len(c) > 2 and c[2] == "add"]


class InstallCase(HomeCase):
    """Isolates the ledger, the env switchboard, the process probe and `which`."""

    ENV_KEYS = ("SKILLGRAM_HARNESS", "SKILLGRAM_LEDGER", "SKILLGRAM_MCP_CMD")

    def setUp(self):
        super().setUp()
        self._env = {k: os.environ.get(k) for k in self.ENV_KEYS}
        for key in self.ENV_KEYS:
            os.environ.pop(key, None)
        for key in [k for k in os.environ if k.startswith("SKILLGRAM_MCP_CLI_")]:
            os.environ.pop(key, None)
        self.ledger = self.tmp / "ledger.json"
        os.environ["SKILLGRAM_LEDGER"] = str(self.ledger)
        os.environ["SKILLGRAM_MCP_CMD"] = f"{sys.executable} {self.tmp / 'skillgram.py'} mcp"
        self._which = skillgram._which
        self._chain = skillgram.process_chain
        skillgram._which = lambda name: f"/fake/bin/{name}"
        skillgram.process_chain = lambda limit=6: []
        # install/propagate resolve the project from the cwd: a test must never
        # touch the checkout it runs in.
        self.cwd = self.tmp / "cwd"
        self.cwd.mkdir()
        os.chdir(self.cwd)

    def tearDown(self):
        skillgram._which = self._which
        skillgram.process_chain = self._chain
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        super().tearDown()

    def make_repo(self, name: str = "app", with_scope: bool = True) -> Path:
        make_skill(self.hub_root, "infra-ops", "deploy")
        repo = self.tmp / "repos" / name
        (repo / ".git").mkdir(parents=True)
        if with_scope:
            skillgram.init_project(name, str(repo))
            skillgram.promote(skillgram.Hub(self.hub_root), ["deploy"], repo)
        return repo


class TestLinks(HomeCase):
    """Link creation must work outside Windows too: CI runs on Linux."""

    def test_make_links_creates_one_link_per_pair(self):
        target = self.tmp / "hub" / "infra-ops" / "deploy"
        (target / "sub").mkdir(parents=True)
        dest = self.tmp / "repo" / ".claude" / "skills" / "deploy"
        dest.parent.mkdir(parents=True)
        self.assertEqual(skillgram.make_links([(dest, target)]), 1)
        self.assertTrue(skillgram.is_link(dest))
        self.assertEqual(Path(skillgram.link_target(dest)).resolve(), target.resolve())
        self.assertTrue(skillgram._remove_junction(dest))
        self.assertFalse(os.path.lexists(dest))

    def test_make_links_never_touches_a_real_directory(self):
        target = self.tmp / "hub" / "infra-ops" / "deploy"
        target.mkdir(parents=True)
        real = self.tmp / "repo" / "skills" / "native"
        real.mkdir(parents=True)
        (real / "SKILL.md").write_text("keep me", encoding="utf-8")
        skillgram.make_links([(real, target)])
        self.assertTrue((real / "SKILL.md").is_file())
        self.assertFalse(skillgram.is_link(real))


class TestHarnessIdentity(InstallCase):
    def test_env_wins_over_flag_and_says_so(self):
        os.environ["SKILLGRAM_HARNESS"] = "codex"
        got = skillgram.resolve_harness("claude")
        self.assertEqual((got["name"], got["source"]), ("codex", "env"))
        self.assertIn("wins", got["note"])

    def test_flag_used_when_env_is_absent(self):
        got = skillgram.resolve_harness("claude")
        self.assertEqual((got["name"], got["source"]), ("claude-code", "flag"))

    def test_process_tree_is_the_last_resort(self):
        skillgram.process_chain = lambda limit=6: ["explorer.exe", "Cursor.exe"]
        self.assertEqual(skillgram.resolve_harness(None)["name"], "cursor")

    def test_unknown_is_reported_never_guessed(self):
        skillgram.process_chain = lambda limit=6: ["node.exe", "python.exe"]
        got = skillgram.resolve_harness(None)
        self.assertEqual((got["name"], got["source"]), ("unknown", "unknown"))

    def test_aliases_normalize(self):
        for raw, want in (("Claude", "claude-code"), ("gemini", "gemini-cli"), ("qwen", "qwen-code")):
            self.assertEqual(skillgram.normalize_harness(raw), want)


class TestMcpDelegation(InstallCase):
    def launch(self):
        return ["py", "skillgram.py", "mcp"]

    def envs(self):
        return ["SKILLGRAM_HARNESS=codex", "SKILLGRAM_HUB=C:/hub"]

    def test_reasonix_uses_the_positional_form_and_dashed_env(self):
        argv = skillgram.mcp_add_argv("reasonix", "skillgram", self.launch(), self.envs())
        self.assertEqual(argv[2:4], ["add", "skillgram"])
        self.assertIn("--env=SKILLGRAM_HARNESS=codex", argv)
        self.assertNotIn("--", argv)  # the legacy form rejects a separator

    def test_codex_and_claude_use_the_separator(self):
        codex = skillgram.mcp_add_argv("codex", "skillgram", self.launch(), self.envs())
        self.assertEqual(codex[-4:], ["--", "py", "skillgram.py", "mcp"])
        self.assertIn("--env", codex)
        claude = skillgram.mcp_add_argv("claude", "skillgram", self.launch(), self.envs())
        self.assertEqual(claude[-4:], ["--", "py", "skillgram.py", "mcp"])
        self.assertIn("user", claude)

    def test_hermes_takes_command_and_args(self):
        argv = skillgram.mcp_add_argv("hermes", "skillgram", self.launch(), self.envs())
        self.assertEqual(argv[argv.index("--command"):], ["--command", "py", "--args", "skillgram.py", "mcp"])

    def test_register_is_idempotent(self):
        hub = skillgram.Hub(self.hub_root)
        cli = FakeCli()
        first = skillgram.register_mcp(hub, "codex", apply=True, runner=cli)
        self.assertEqual(first["state"], "registered")
        self.assertEqual(len(cli.adds()), 1)
        self.assertIn("SKILLGRAM_HARNESS=codex", cli.add_argv)
        second = skillgram.register_mcp(hub, "codex", apply=True, runner=cli)
        self.assertEqual(second["state"], "already")
        self.assertNotIn("note", second)
        self.assertEqual(len(cli.adds()), 1)

    def test_dry_run_never_registers(self):
        got = skillgram.register_mcp(skillgram.Hub(self.hub_root), "codex", apply=False, runner=FakeCli())
        self.assertEqual(got["state"], "planned")
        self.assertTrue(got["command"])
        self.assertEqual(got.get("exit"), None)

    def test_a_server_pointing_elsewhere_is_flagged(self):
        cli = FakeCli(point_elsewhere=True)
        cli.registered = True
        got = skillgram.register_mcp(skillgram.Hub(self.hub_root), "codex", apply=True, runner=cli)
        self.assertEqual(got["state"], "already")
        self.assertIn("mcp remove", got["note"])

    def test_harness_without_mcp_cli_says_what_to_do(self):
        got = skillgram.register_mcp(skillgram.Hub(self.hub_root), "cursor", apply=True, runner=FakeCli())
        self.assertEqual(got["state"], "unsupported")
        self.assertIn("Settings > MCP", got["detail"])

    def test_missing_cli_is_reported_not_faked(self):
        skillgram._which = lambda name: None
        got = skillgram.register_mcp(skillgram.Hub(self.hub_root), "hermes", apply=True, runner=FakeCli())
        self.assertEqual(got["state"], "not-installed")
        self.assertIn("SKILLGRAM_MCP_CLI_HERMES", got["detail"])

    def test_a_cli_that_exits_zero_without_saving_is_not_success(self):
        class SilentCli(FakeCli):
            """Like hermes: exits 0 after its own Y/n prompt was cancelled."""

            def __call__(self, argv):
                argv = [str(a) for a in argv]
                self.calls.append(argv)
                verb = argv[2] if len(argv) > 2 else ""
                if verb == "list":
                    return 0, "no servers configured"
                if verb == "get":
                    return 1, "not found"
                return 0, "  Enable all 5 tools? [Y/n/select]: \n  Cancelled."

        got = skillgram.register_mcp(skillgram.Hub(self.hub_root), "hermes", apply=True, runner=SilentCli())
        self.assertEqual(got["state"], "failed")
        self.assertFalse(got["verified"])
        self.assertIn("not registered", got["detail"])

    def test_cli_override_wins_over_path(self):
        launcher = self.tmp / "hermes-gov.cmd"
        launcher.write_text("@echo off\n", encoding="utf-8")
        os.environ["SKILLGRAM_MCP_CLI_HERMES"] = str(launcher)
        cli = FakeCli()
        got = skillgram.register_mcp(skillgram.Hub(self.hub_root), "hermes", apply=True, runner=cli)
        self.assertEqual(got["cli"], str(launcher))
        self.assertEqual(got["state"], "registered")
        self.assertEqual(cli.adds()[0][0], str(launcher))
        self.assertEqual(cli.calls[0][0], str(launcher))  # the probe uses it too

    def test_launch_never_picks_a_script_or_shim(self):
        os.environ.pop("SKILLGRAM_MCP_CMD", None)
        local = self.tmp / "skillgram.py"
        local.write_text("", encoding="utf-8")
        skillgram._which = lambda name: str(local)
        argv = skillgram.mcp_launch_argv()
        self.assertEqual(argv[1], str(Path(skillgram.__file__).resolve()))
        self.assertEqual(argv[-1], "mcp")

    def test_launch_prefers_an_installed_console_script(self):
        os.environ.pop("SKILLGRAM_MCP_CMD", None)
        exe = self.tmp / "skillgram.exe"
        exe.write_text("", encoding="utf-8")
        skillgram._which = lambda name: str(exe)
        self.assertEqual(skillgram.mcp_launch_argv(), [str(exe), "mcp"])


class TestLedgerAndDiscovery(InstallCase):
    def test_ledger_roundtrip_under_the_env_path(self):
        self.assertEqual(skillgram.ledger_path(), self.ledger)
        self.assertEqual(skillgram.read_ledger()["repos"], {})
        data = skillgram.read_ledger()
        data["repos"]["c:/repo"] = {"harness": "codex", "at": "now"}
        skillgram.write_ledger(data)
        self.assertIn("c:/repo", skillgram.read_ledger()["repos"])

    def test_discovery_finds_nested_repos_and_skips_noise(self):
        root = self.tmp / "repos"
        (root / ".git").mkdir(parents=True)
        (root / "a" / "b" / ".git").mkdir(parents=True)
        (root / "node_modules" / "junk" / ".git").mkdir(parents=True)
        (root / "a" / "deep" / "deeper" / ".git").mkdir(parents=True)
        found = [str(p) for p in skillgram.discover_repos(root, depth=2)]
        self.assertIn(str(root.resolve()), found)
        self.assertIn(str((root / "a" / "b").resolve()), found)
        self.assertFalse(any("node_modules" in p for p in found))
        self.assertFalse(any("deeper" in p for p in found))  # beyond depth


class TestInstall(InstallCase):
    def test_install_unknown_harness_stops_loudly(self):
        make_skill(self.hub_root, "infra-ops", "deploy")
        cli = FakeCli()
        res = skillgram.install(skillgram.Hub(self.hub_root), apply=True, runner=cli)
        self.assertFalse(res["ok"])
        self.assertEqual(res["harness"]["name"], "unknown")
        self.assertEqual(cli.calls, [])
        self.assertTrue(any("--harness" in item for item in res["blocked"]))

    def test_install_creates_the_hub_when_missing(self):
        hub_root = self.tmp / "fresh-hub"
        res = skillgram.install(skillgram.Hub(hub_root), harness="codex", apply=True, runner=FakeCli())
        self.assertTrue(hub_root.is_dir())
        self.assertIn("created", " ".join(res["done"]))
        self.assertFalse(res["ok"])  # empty hub: it says so instead of pretending

    def test_dry_run_writes_nothing(self):
        hub_root = self.tmp / "fresh-hub"
        cli = FakeCli()
        repo = self.make_repo()  # noqa: F841 - the hub gets the skill
        (self.home / ".codex" / "skills").mkdir(parents=True)
        res = skillgram.install(skillgram.Hub(self.hub_root), harness="codex", root=str(repo),
                                apply=False, runner=cli)
        self.assertFalse(res["applied"])
        self.assertEqual(cli.adds(), [])
        self.assertFalse(self.ledger.exists())
        self.assertTrue(any("would mount" in item for item in res["done"]))
        self.assertFalse((repo / ".codex" / "skills" / "deploy").exists())
        self.assertFalse((repo / "AGENTS.md").exists())
        self.assertFalse(hub_root.exists())

    def test_install_registers_then_reports_already(self):
        make_skill(self.hub_root, "infra-ops", "deploy")
        cli = FakeCli()
        first = skillgram.install(skillgram.Hub(self.hub_root), harness="codex", apply=True, runner=cli)
        self.assertTrue(any("registered" in item for item in first["done"]))
        self.assertIn("Codex:", " ".join(first["effect"]))
        second = skillgram.install(skillgram.Hub(self.hub_root), harness="codex", apply=True, runner=cli)
        self.assertEqual(len(cli.adds()), 1)
        self.assertTrue(any("already registered" in item for item in second["already"]))

    def test_failed_registration_is_not_ok(self):
        make_skill(self.hub_root, "infra-ops", "deploy")
        res = skillgram.install(skillgram.Hub(self.hub_root), harness="codex", apply=True,
                                runner=FakeCli(add_exit=2))
        self.assertFalse(res["ok"])
        self.assertTrue(any("failed" in item for item in res["blocked"]))

    def test_install_reports_the_effect_step(self):
        make_skill(self.hub_root, "infra-ops", "deploy")
        res = skillgram.install(skillgram.Hub(self.hub_root), harness="reasonix", apply=True, runner=FakeCli())
        self.assertIn("/reload", " ".join(res["effect"]))
        self.assertEqual(res["mcp"]["client"], "reasonix")

    @unittest.skipUnless(sys.platform == "win32", "junctions: Windows only")
    def test_install_propagates_scope_protocol_and_ledger(self):
        repo = self.make_repo("app")
        (self.home / ".codex" / "skills").mkdir(parents=True)
        cli = FakeCli()
        res = skillgram.install(skillgram.Hub(self.hub_root), harness="codex", root=str(repo),
                                apply=True, runner=cli)
        self.assertTrue(res["ok"], res["blocked"])
        self.assertTrue(skillgram.is_link(repo / ".codex" / "skills" / "deploy"))
        agents_md = (repo / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("skillgram:protocol:begin", agents_md)
        ledger = skillgram.read_ledger()["repos"]
        self.assertEqual(len(ledger), 1)
        self.assertEqual(list(ledger.values())[0]["harness"], "codex")
        again = skillgram.install(skillgram.Hub(self.hub_root), harness="codex", root=str(repo),
                                  apply=True, runner=cli)
        self.assertTrue(any("already in the ledger" in item for item in again["already"]))
        self.assertEqual(len(cli.adds()), 1)

    def test_repo_without_scope_is_skipped_with_the_next_step(self):
        repo = self.make_repo("bare", with_scope=False)
        res = skillgram.install(skillgram.Hub(self.hub_root), harness="codex", root=str(repo),
                                apply=False, runner=FakeCli())
        rows = res["propagate"]["rows"]
        self.assertEqual(rows[0]["status"], "skipped")
        self.assertIn("use", rows[0]["reason"])


class TestPropagate(InstallCase):
    def test_needs_a_harness(self):
        repo = self.make_repo()
        res = skillgram.propagate(skillgram.Hub(self.hub_root), root=str(repo), apply=False)
        self.assertFalse(res["ok"])
        self.assertIn("harness", res["error"])

    def test_root_must_exist_and_hold_repos(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        res = skillgram.propagate(skillgram.Hub(self.hub_root), harness="codex", root=str(empty), apply=False)
        self.assertFalse(res["ok"])
        self.assertEqual(res["repos_found"], 0)

    def test_ledger_is_skipped_unless_forced(self):
        a = self.make_repo("a")
        b = self.make_repo("b")
        hub = skillgram.Hub(self.hub_root)
        data = skillgram.read_ledger()
        data["repos"][skillgram._ledger_key(a)] = {"harness": "codex", "at": "now"}
        skillgram.write_ledger(data)
        res = skillgram.propagate(hub, harness="codex", root=str(self.tmp / "repos"), apply=False)
        self.assertEqual(res["repos_found"], 2)
        self.assertEqual(res["ledger_skipped"], 1)
        self.assertEqual([Path(r["repo"]).name for r in res["rows"]], ["b"])
        forced = skillgram.propagate(hub, harness="codex", root=str(self.tmp / "repos"), apply=False, force=True)
        self.assertEqual(forced["ledger_skipped"], 0)
        self.assertEqual(len(forced["rows"]), 2)

    @unittest.skipUnless(sys.platform == "win32", "junctions: Windows only")
    def test_apply_records_the_ledger(self):
        repo = self.make_repo("c")
        (self.home / ".codex" / "skills").mkdir(parents=True)
        res = skillgram.propagate(skillgram.Hub(self.hub_root), harness="codex",
                                  root=str(self.tmp / "repos"), apply=True)
        self.assertTrue(res["applied"])
        self.assertEqual(res["recorded"], 1)
        self.assertTrue(self.ledger.is_file())
        self.assertEqual(res["rows"][0]["status"], "mounted")

    def test_the_hub_itself_is_never_a_target(self):
        (self.hub_root / ".git").mkdir()
        make_skill(self.hub_root, "infra-ops", "deploy")
        res = skillgram.propagate(skillgram.Hub(self.hub_root), harness="codex",
                                  root=str(self.hub_root), apply=False)
        self.assertEqual(res["rows"], [])
        self.assertEqual(res["ignored_hub"], [str(self.hub_root.resolve())])


class TestCliSurface(HomeCase):
    """La capa que nadie probaba: que `main` ejecute el comando una sola vez y que
    cada salida (JSON de maquina / reporte humano) lea claves que existen."""

    def run_cli(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = skillgram.main([str(a) for a in argv])
        return code, [line for line in buf.getvalue().splitlines() if line.strip()]

    def json_line(self, out):
        self.assertEqual(len(out), 1, f"se esperaba una sola linea JSON, salio: {out}")
        return json.loads(out[0])

    def test_machine_commands_run_once_and_print_one_json_line(self):
        make_skill(self.hub_root, "infra-ops", "deploy")
        code, out = self.run_cli("new", "fresh-skill", "--hub", self.hub_root)
        # una segunda corrida del mismo comando devolveria "already exists" y exit 1
        self.assertEqual(code, 0)
        payload = self.json_line(out)
        self.assertTrue(payload["ok"])
        self.assertTrue((self.hub_root / "imported" / "fresh-skill" / "SKILL.md").is_file())

        for argv in (("list",), ("demand",), ("index",), ("flavor",), ("search", "deploy")):
            code, out = self.run_cli(*argv, "--hub", self.hub_root)
            self.assertEqual(code, 0, argv)
            self.json_line(out)

    def test_retire_runs_once_and_reports_the_tombstone(self):
        make_skill(self.hub_root, "infra-ops", "old-one")
        code, out = self.run_cli("retire", "old-one", "--hub", self.hub_root)
        self.assertEqual(code, 0)
        payload = self.json_line(out)
        self.assertTrue(payload["ok"])
        self.assertIn("_retired", payload["to"])

    def test_json_flag_wins_over_the_human_report(self):
        make_skill(self.hub_root, "infra-ops", "deploy")
        code, out = self.run_cli("status", "--hub", self.hub_root, "--json")
        self.assertEqual(code, 0)
        payload = self.json_line(out)
        self.assertEqual(payload["skills"], 1)

    def test_unuse_says_what_was_not_in_scope(self):
        proj = self.tmp / "proj"
        skillgram.init_project("proj", str(proj))
        code, out = self.run_cli("unuse", "ghost", "--hub", self.hub_root, "--project", proj)
        self.assertEqual(code, 0)
        text = "\n".join(out)
        self.assertIn("removed: 0", text)
        self.assertIn("no estaban: ghost", text)

    def test_project_status_prints_every_section_it_counts(self):
        make_skill(self.hub_root, "infra-ops", "deploy")
        make_skill(self.hub_root, "infra-ops", "helper")
        make_skill(self.hub_root, "infra-ops", "extra")
        proj = self.tmp / "proj"
        skillgram.init_project("proj", str(proj))
        hub = skillgram.Hub(self.hub_root)
        skillgram.promote(hub, ["deploy", "helper"], proj)
        links = proj / ".agents" / "skills"
        # stale: el hub cambio despues de promover
        (self.hub_root / "infra-ops" / "deploy" / "SKILL.md").write_text("changed\n", encoding="utf-8")
        # missing link: una skill registrada cuyo enlace ya no existe
        self.assertTrue(skillgram._remove_junction(links / "helper"))
        (links / "helper").mkdir()
        (links / "helper" / "SKILL.md").write_text("x\n", encoding="utf-8")
        # local duplicate: copia real de una skill del hub fuera del manifest
        (links / "extra").mkdir()
        (links / "extra" / "SKILL.md").write_text("x\n", encoding="utf-8")
        # local only: carpeta propia del proyecto
        (links / "mine").mkdir()
        (links / "mine" / "SKILL.md").write_text("x\n", encoding="utf-8")

        code, out = self.run_cli("project-status", "--hub", self.hub_root, "--project", proj)
        # drift (un enlace faltante) se reporta con exit 1, no con una excepcion
        self.assertEqual(code, 1)
        text = "\n".join(out)
        self.assertIn("resolucion: explicit", text)
        self.assertIn("stale: deploy", text)
        self.assertIn("missing links: helper", text)
        self.assertIn("local duplicates (estan en el hub y ademas copiadas): extra", text)
        self.assertIn("local only: mine", text)

    def test_human_commands_keep_their_reporter(self):
        proj = self.tmp / "proj"
        code, out = self.run_cli("init", "named", "--hub", self.hub_root, "--path", proj)
        self.assertEqual(code, 0)
        self.assertTrue(out[0].startswith("proyecto: named"))
        make_skill(self.hub_root, "infra-ops", "deploy")
        code, out = self.run_cli("use", "deploy", "--hub", self.hub_root, "--project", proj)
        self.assertEqual(code, 0)
        self.assertIn("promoted: 1 nuevas, 0 already present", out)
        code, out = self.run_cli("status", "--hub", self.hub_root)
        self.assertEqual(code, 0)
        self.assertTrue(out[0].startswith("hub: "))

if __name__ == "__main__":
    unittest.main()
