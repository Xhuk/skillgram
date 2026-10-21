"""skillgram tests â€” stdlib only: python -m unittest discover -s tests -v"""

import ast
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
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
        skillgram.hub.HOME = self.home
        os.environ["APPDATA"] = str(self.tmp / "appdata")
        self.hub_root = self.tmp / "hub"
        self.hub_root.mkdir()

    def tearDown(self):
        skillgram.hub.HOME = self._old_home
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

    def test_plan_is_a_healthy_result(self):
        # el plan no es un error: main() deriva el exit code de `ok`
        make_skill(self.home / ".fakeagent", "skills", "native-one")
        plan = skillgram.build_onboard_plan(skillgram.Hub(self.hub_root), scope="all")
        self.assertTrue(plan["ok"])

    def test_duplicate_actions_say_why(self):
        make_skill(self.home / ".a", "skills", "twin")
        make_skill(self.home / ".b", "skills", "twin")
        plan = skillgram.build_onboard_plan(skillgram.Hub(self.hub_root), scope="all")
        dedupe = [a for a in plan["actions"] if a["action"] == "dedupe"]
        self.assertTrue(dedupe)
        self.assertTrue(dedupe[0]["reason"].startswith("identical to"))

    def test_apply_imports_the_native_skill(self):
        make_skill(self.home / ".fakeagent", "skills", "native-one")
        hub = skillgram.Hub(self.hub_root)
        plan = skillgram.build_onboard_plan(hub, scope="all")
        applied = skillgram.apply_onboard(hub, plan)
        self.assertEqual(applied["failed"], [])
        self.assertEqual(len(applied["imported"]), 1)
        dest = Path(applied["imported"][0]["to"])
        self.assertTrue((dest / "SKILL.md").is_file())

    def test_llm_refine_plan_writes_the_keys_the_plan_uses(self):
        make_skill(self.hub_root, "design-ui", "archify")
        make_skill(self.home / ".fakeagent", "skills", "native-one")
        plan = skillgram.build_onboard_plan(skillgram.Hub(self.hub_root), scope="all")
        reply = json.dumps({"skills": [{"name": "native-one", "harness": "fakeagent",
                                        "category": "design-ui", "flavor": "generic",
                                        "reason": "same tool, other wording"}]})
        original = skillgram.adopt.llm_chat
        skillgram.adopt.llm_chat = lambda cfg, system, user: reply
        try:
            refined = skillgram.llm_refine_plan(skillgram.Hub(self.hub_root), plan, {"model": "test"})
        finally:
            skillgram.adopt.llm_chat = original
        action = refined["actions"][0]
        self.assertEqual(action["category"], "design-ui")
        self.assertEqual(action["reason"], "same tool, other wording")
        self.assertNotIn("categoria", action)
        self.assertNotIn("motivo", action)




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
        self.assertIn("already generic", skillgram.port_skill(hub, "plain", "reasonix", None).get("skipped", ""))
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
        self.assertIn("already has a scope", res["skipped"])

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
        self._which = skillgram.harnesses._which
        self._chain = skillgram.harnesses.process_chain
        skillgram.harnesses._which = lambda name: f"/fake/bin/{name}"
        skillgram.harnesses.process_chain = lambda limit=6: []
        # install/propagate resolve the project from the cwd: a test must never
        # touch the checkout it runs in.
        self.cwd = self.tmp / "cwd"
        self.cwd.mkdir()
        os.chdir(self.cwd)

    def tearDown(self):
        skillgram.harnesses._which = self._which
        skillgram.harnesses.process_chain = self._chain
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
        skillgram.harnesses.process_chain = lambda limit=6: ["explorer.exe", "Cursor.exe"]
        self.assertEqual(skillgram.resolve_harness(None)["name"], "cursor")

    def test_unknown_is_reported_never_guessed(self):
        skillgram.harnesses.process_chain = lambda limit=6: ["node.exe", "python.exe"]
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
        skillgram.harnesses._which = lambda name: None
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
        skillgram.harnesses._which = lambda name: str(local)
        argv = skillgram.mcp_launch_argv()
        self.assertEqual(argv, [*skillgram.launch_argv(), "mcp"])
        self.assertEqual(argv[-1], "mcp")

    def test_launch_prefers_an_installed_console_script(self):
        os.environ.pop("SKILLGRAM_MCP_CMD", None)
        exe = self.tmp / "skillgram.exe"
        exe.write_text("", encoding="utf-8")
        skillgram.harnesses._which = lambda name: str(exe)
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
        self.make_repo("b")
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
        self.make_repo("c")
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
        self.assertIn("not in scope: ghost", text)

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
        self.assertIn("resolution: explicit", text)
        self.assertIn("stale: deploy", text)
        self.assertIn("missing links: helper", text)
        self.assertIn("local duplicates (in the hub and also copied): extra", text)
        self.assertIn("local only: mine", text)

    def test_human_commands_keep_their_reporter(self):
        proj = self.tmp / "proj"
        code, out = self.run_cli("init", "named", "--hub", self.hub_root, "--path", proj)
        self.assertEqual(code, 0)
        self.assertTrue(out[0].startswith("project: named"))
        make_skill(self.hub_root, "infra-ops", "deploy")
        code, out = self.run_cli("use", "deploy", "--hub", self.hub_root, "--project", proj)
        self.assertEqual(code, 0)
        self.assertIn("promoted: 1 new, 0 already present", out)
        code, out = self.run_cli("status", "--hub", self.hub_root)
        self.assertEqual(code, 0)
        self.assertTrue(out[0].startswith("hub: "))

    def test_onboard_plan_exits_zero_and_carries_ok(self):
        make_skill(self.home / ".fakeagent", "skills", "native-one")
        code, out = self.run_cli("onboard", "--no-llm", "--json", "--hub", self.hub_root)
        self.assertEqual(code, 0)
        payload = self.json_line(out)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["summary"]["import"], 1)

    def test_onboard_apply_imports_and_reports_the_dest(self):
        make_skill(self.home / ".fakeagent", "skills", "native-one")
        code, out = self.run_cli("onboard", "--no-llm", "--apply", "--json", "--hub", self.hub_root)
        self.assertEqual(code, 0)
        payload = self.json_line(out)
        self.assertEqual(len(payload["applied"]["imported"]), 1)
        self.assertTrue((Path(payload["applied"]["imported"][0]["to"]) / "SKILL.md").is_file())

    def test_onboard_human_report_shows_the_action_reason(self):
        make_skill(self.hub_root, "infra-ops", "deploy")
        make_skill(self.home / ".fakeagent", "skills", "deploy")
        code, out = self.run_cli("onboard", "--no-llm", "--hub", self.hub_root)
        self.assertEqual(code, 0)
        text = "\n".join(out)
        self.assertIn("harnesses with native skills:", text)
        self.assertIn("1 conflict", text)
        self.assertIn("a skill with that name already exists in the hub", text)

    def test_global_flags_work_before_the_subcommand(self):
        make_skill(self.hub_root, "infra-ops", "deploy")
        code, out = self.run_cli("--hub", self.hub_root, "--json", "status")
        self.assertEqual(code, 0)
        self.assertEqual(self.json_line(out)["skills"], 1)

    def test_flags_after_the_subcommand_still_win(self):
        make_skill(self.hub_root, "infra-ops", "deploy")
        code, out = self.run_cli("--hub", self.tmp / "nowhere", "--json", "status", "--hub", self.hub_root)
        self.assertEqual(code, 0)
        self.assertEqual(self.json_line(out)["skills"], 1)


class TestProtocolBlock(HomeCase):
    """The block is what agents read in every mounted repo, so a command it teaches
    must exist. It used to teach `skillgram add <url>`, which the parser never had:
    every AGENTS.md told the agent to run a command that fails.
    """

    def block(self) -> str:
        return skillgram.protocol_block(skillgram.Hub(self.hub_root))

    def test_every_command_it_teaches_exists(self):
        sub = next(a for a in skillgram.build_parser()._actions if a.dest == "cmd")
        mentioned = set(re.findall(r"`skillgram ([a-z][a-z-]*)", self.block()))
        self.assertTrue(mentioned, "the block should name the commands it wants")
        self.assertEqual(sorted(mentioned - set(sub.choices)), [],
                         "the protocol teaches a command the CLI does not have")

    def test_it_teaches_the_loop_that_feeds_the_record(self):
        text = self.block()
        # touch is the only usage signal: without it `review`/`pin`/`list --used` are empty
        self.assertIn("`skillgram touch", text)
        # and the previous harness's record has to be readable from the next one
        self.assertIn("`skillgram list --project . --used`", text)
        self.assertIn("harness-agnostic", text)
        # a fresh clone has the set but no links: the block has to say how to get them
        self.assertIn("`skillgram mount --apply`", text)
        self.assertIn("`skillgram align`", text)

    def test_the_block_survives_injection_unchanged(self):
        proj = self.tmp / "proj"
        proj.mkdir()
        (proj / "AGENTS.md").write_text("# repo\n\ntext\n", encoding="utf-8")
        skillgram.inject_protocol(skillgram.Hub(self.hub_root), proj, apply=True)
        written = (proj / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn(skillgram.PROTOCOL_BEGIN, written)
        self.assertIn("`skillgram touch", written)
        # idempotent: a second pass does not duplicate the block
        skillgram.inject_protocol(skillgram.Hub(self.hub_root), proj, apply=True)
        again = (proj / "AGENTS.md").read_text(encoding="utf-8")
        self.assertEqual(again.count(skillgram.PROTOCOL_BEGIN), 1)


class TestUsageAcrossHarnesses(HomeCase):
    """Switching harness must not lose what the project was using: the record is the
    project's, not the agent's.
    """

    def project_with_scope(self) -> Path:
        make_skill(self.hub_root, "infra-ops", "deploy")
        proj = self.tmp / "proj"
        skillgram.init_project("proj", str(proj))
        skillgram.promote(skillgram.Hub(self.hub_root), ["deploy"], proj)
        return proj

    def test_the_record_says_which_harness_used_what(self):
        proj = self.project_with_scope()
        skillgram.touch_usage(proj, ["deploy"], by="claude")
        skillgram.touch_usage(proj, ["deploy"], by="codex")
        skillgram.touch_usage(proj, ["deploy"], by="claude")

        rows = skillgram.list_skills(skillgram.Hub(self.hub_root), project=proj, used="used")["rows"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["use_count"], 3)
        self.assertEqual(row["last_used_by"], "claude")
        self.assertEqual(row["by_harness"], {"claude": 2, "codex": 1})

    def test_a_new_harness_reads_the_previous_one(self):
        proj = self.project_with_scope()
        skillgram.touch_usage(proj, ["deploy"], by="claude")
        # a different agent, same project: the manifest is the memory, not the session
        unused = skillgram.list_skills(skillgram.Hub(self.hub_root), project=proj, used="unused")["rows"]
        self.assertEqual(unused, [])
        row = skillgram.list_skills(skillgram.Hub(self.hub_root), project=proj)["rows"][0]
        self.assertEqual(row["by_harness"], {"claude": 1})


class TestRepoSig(HomeCase):
    """`bootstrap` decides with these signals. It used to read only the root
    package.json and turn every dependency into a tag: on vetgroom-demo (a monorepo
    whose root package.json carries a flattened lockfile) the top signals were
    `ansi-regex`, `bare-fs`, `ieee754` and the real stack was invisible.
    """

    def test_a_monorepo_reports_its_real_stack(self):
        repo = self.tmp / "mono"
        (repo / "backend").mkdir(parents=True)
        (repo / "frontend").mkdir()
        (repo / "node_modules" / "junk").mkdir(parents=True)
        (repo / "backend" / "go.mod").write_text("module x\n", encoding="utf-8")
        (repo / "frontend" / "package.json").write_text(json.dumps({
            "dependencies": {"react": "^18", "vite": "^5", "@supabase/supabase-js": "^2"},
            "devDependencies": {"playwright": "^1"},
        }), encoding="utf-8")
        (repo / "package.json").write_text(json.dumps({
            "dependencies": {"ansi-regex": "1", "bare-fs": "1", "ieee754": "1"},
        }), encoding="utf-8")
        (repo / "node_modules" / "junk" / "package.json").write_text("{}", encoding="utf-8")

        signals = skillgram.repo_signals(repo)
        for expected in ("go", "node", "react", "vite", "supabase", "playwright"):
            self.assertIn(expected, signals["tags"])
        # a lockfile dumped into dependencies is not a stack
        for junk in ("ansi-regex", "bare-fs", "ieee754"):
            self.assertNotIn(junk, signals["tags"])
        self.assertNotIn("node_modules", signals["scanned"])

    def test_short_tags_are_kept(self):
        repo = self.tmp / "short"
        repo.mkdir()
        (repo / "go.mod").write_text("module x\n", encoding="utf-8")
        (repo / "tsconfig.json").write_text("{}", encoding="utf-8")
        signals = skillgram.repo_signals(repo)
        self.assertIn("go", signals["tags"])
        self.assertIn("ts", signals["tags"])


class TestMountGitignore(HomeCase):
    """`mount` puts links inside the repo. Git sees a junction as a directory and
    would commit the skill's files -- the copies skillgram promises never to make.
    """

    def project(self) -> Path:
        make_skill(self.hub_root, "infra-ops", "deploy")
        proj = self.tmp / "proj"
        skillgram.init_project("proj", str(proj))
        skillgram.promote(skillgram.Hub(self.hub_root), ["deploy"], proj)
        return proj

    def test_the_links_are_ignored_and_the_scope_is_not(self):
        proj = self.project()
        # a harness home so mount also writes a harness dir inside the project
        (self.home / ".claude" / "skills").mkdir(parents=True)
        (proj / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
        res = skillgram.mount_project(skillgram.Hub(self.hub_root), proj, apply=True)
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["gitignore"]["changed"])

        text = (proj / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("node_modules/", text, "the existing rules must survive")
        self.assertIn(skillgram.GITIGNORE_BEGIN, text)
        self.assertIn(".agents/skills/", text, "links stay out of git")
        self.assertIn(".claude/skills/", text, "every harness dir mount wrote")
        self.assertNotIn(".agents/config.json", text, "the scope is the project's memory")
        self.assertNotIn(".agents/\n", text, "do not hide the whole scope folder")

    def test_the_block_is_idempotent(self):
        proj = self.project()
        (proj / ".gitignore").write_text("dist/\n", encoding="utf-8")
        skillgram.mount_project(skillgram.Hub(self.hub_root), proj, apply=True)
        first = (proj / ".gitignore").read_text(encoding="utf-8")
        again = skillgram.mount_project(skillgram.Hub(self.hub_root), proj, apply=True)
        second = (proj / ".gitignore").read_text(encoding="utf-8")
        self.assertEqual(first, second)
        self.assertEqual(second.count(skillgram.GITIGNORE_BEGIN), 1)
        self.assertFalse(again["gitignore"]["changed"])

    def test_a_plan_does_not_write_the_file(self):
        proj = self.project()
        res = skillgram.mount_project(skillgram.Hub(self.hub_root), proj, apply=False)
        self.assertFalse((proj / ".gitignore").exists())
        self.assertIn(".agents/skills/", res["gitignore"]["paths"])


class TestCallerIdentity(HomeCase):
    """The usage record has to name the mask that was on: `use` wrote the constant
    "agent", so `by_harness` could never answer "which agent was I using this from".
    """

    def setUp(self):
        super().setUp()
        self._env = os.environ.get(skillgram.HARNESS_ENV)
        self._chain = skillgram.harnesses.process_chain
        skillgram.harnesses.process_chain = lambda limit=6: []
        os.environ.pop(skillgram.HARNESS_ENV, None)
        self.proj = self.tmp / "proj"
        skillgram.init_project("proj", str(self.proj))
        make_skill(self.hub_root, "infra-ops", "deploy")

    def tearDown(self):
        skillgram.harnesses.process_chain = self._chain
        if self._env is None:
            os.environ.pop(skillgram.HARNESS_ENV, None)
        else:
            os.environ[skillgram.HARNESS_ENV] = self._env
        super().tearDown()

    def tally(self) -> dict:
        entry = skillgram._read_manifest(self.proj)["skills"]["deploy"]
        return dict(entry.get("by_harness") or {})

    def run_cli(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = skillgram.main([str(a) for a in argv])
        return code, json.loads(buf.getvalue().strip().splitlines()[-1])

    def test_use_records_the_resolved_harness(self):
        os.environ[skillgram.HARNESS_ENV] = "claude"
        code, out = self.run_cli("use", "deploy", "--json", "--hub", self.hub_root, "--project", self.proj)
        self.assertEqual(code, 0, out)
        # the canonical name, so an alias and the harness itself land in one bucket
        canonical = skillgram.normalize_harness("claude")
        self.assertEqual(out["by"], canonical)
        self.assertEqual(self.tally(), {canonical: 1})

    def test_use_falls_back_to_agent_when_nothing_knows(self):
        code, out = self.run_cli("use", "deploy", "--json", "--hub", self.hub_root, "--project", self.proj)
        self.assertEqual(code, 0, out)
        self.assertEqual(out["by"], "agent")
        self.assertEqual(self.tally(), {"agent": 1})

    def test_an_explicit_by_wins(self):
        os.environ[skillgram.HARNESS_ENV] = "claude"
        code, out = self.run_cli("use", "deploy", "--by", "codex", "--json",
                                 "--hub", self.hub_root, "--project", self.proj)
        self.assertEqual(code, 0, out)
        self.assertEqual(out["by"], "codex")
        self.assertEqual(self.tally(), {"codex": 1})

    def test_the_mcp_path_records_the_harness_too(self):
        os.environ[skillgram.HARNESS_ENV] = "reasonix"
        hub = skillgram.Hub(self.hub_root)
        used = skillgram.mcp_call(hub, "skillgram_use", {"skills": ["deploy"], "project": str(self.proj)})
        self.assertEqual(used["by"], "reasonix")
        self.assertEqual(used["uso"], ["deploy"])
        skillgram.mcp_call(hub, "skillgram_touch", {"skills": ["deploy"], "project": str(self.proj)})
        self.assertEqual(self.tally(), {"reasonix": 2})
        explicit = skillgram.mcp_call(hub, "skillgram_touch",
                                      {"skills": ["deploy"], "project": str(self.proj), "by": "codex"})
        self.assertEqual(explicit["by"], "codex")
        self.assertEqual(self.tally(), {"reasonix": 2, "codex": 1})


class TestRankingAndSeeds(HomeCase):
    """`rank_skills` decided with a flat count over name+category+1200 chars of body,
    so a weak mention tied with a real match and the tie broke alphabetically (that is
    how `apple-meta-craft` ranked for a Go+Vite repo). And there was no way to say
    "this one, always": a taste is not something a score should overrule.
    """

    def setUp(self):
        super().setUp()
        self._seeds_env = os.environ.get(skillgram.SEEDS_ENV)
        os.environ[skillgram.SEEDS_ENV] = str(self.tmp / "seeds.json")
        make_skill(self.hub_root, "infra-ops", "deploy-runbook", "rollout steps")
        make_skill(self.hub_root, "infra-ops", "misc-notes", "this body mentions deploy once")
        make_skill(self.hub_root, "design-ui", "palette", "colors for the design system")
        make_skill(self.hub_root, "agent-workflows", "anti-slop", "reject low-evidence work")

    def tearDown(self):
        if self._seeds_env is None:
            os.environ.pop(skillgram.SEEDS_ENV, None)
        else:
            os.environ[skillgram.SEEDS_ENV] = self._seeds_env
        super().tearDown()

    def hub(self):
        return skillgram.Hub(self.hub_root)

    def test_a_name_match_beats_a_body_mention(self):
        rows = skillgram.rank_skills(self.hub(), ["deploy"])
        self.assertEqual([r["skill"] for r in rows], ["deploy-runbook"])
        # the body-only mention scores 1, below RANK_MIN: noise, not a candidate
        self.assertNotIn("misc-notes", [r["skill"] for r in rows])

    def test_a_tag_is_a_word_not_a_fragment(self):
        make_skill(self.hub_root, "languages", "golang-patterns", "go idioms")
        make_skill(self.hub_root, "agent-workflows", "godmode", "unrelated to the go toolchain")
        rows = skillgram.rank_skills(self.hub(), ["go"])
        names = [r["skill"] for r in rows]
        self.assertIn("golang-patterns", names, "the alias must still match the real skill")
        self.assertNotIn("godmode", names, "'go' is not a prefix of a word")

    def test_a_never_seed_is_filtered_out(self):
        skillgram.seed_skills(self.hub(), ["deploy-runbook"], mode="never")
        rows = skillgram.rank_skills(self.hub(), ["deploy", "runbook"],
                                     avoid=skillgram.read_seeds()["never"])
        self.assertEqual(rows, [])

    def test_an_always_seed_comes_first_even_with_no_score(self):
        skillgram.seed_skills(self.hub(), ["anti-slop"], mode="always")
        seeds = skillgram.read_seeds()
        rows = skillgram.rank_skills(self.hub(), ["deploy"], prefer=seeds["always"], avoid=seeds["never"])
        self.assertEqual(rows[0]["skill"], "anti-slop")
        self.assertEqual(rows[0]["score"], 0)
        self.assertTrue(rows[0]["seed"])
        self.assertFalse(rows[1]["seed"])

    def test_candidates_carry_the_seeds(self):
        skillgram.seed_skills(self.hub(), ["anti-slop"], mode="always")
        candidates = skillgram.candidate_skills(self.hub(), {"tags": ["deploy"]})
        self.assertEqual(candidates[0]["skill"], "anti-slop")
        self.assertTrue(candidates[0]["seed"])
        self.assertIn("deploy-runbook", [c["skill"] for c in candidates])

    def test_missing_is_not_hijacked_by_a_seed(self):
        skillgram.seed_skills(self.hub(), ["anti-slop"], mode="always")
        res = skillgram.missing(self.hub(), "rollout deploy runbook")
        self.assertTrue(res["found"])
        self.assertEqual(res["candidates"][0]["skill"], "deploy-runbook")

    def test_seed_cli_add_move_remove_and_list(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            skillgram.main(["seed", "anti-slop", "--hub", str(self.hub_root)])
        self.assertIn("always: anti-slop", buf.getvalue())

        # --never moves it instead of duplicating it in both lists
        with contextlib.redirect_stdout(io.StringIO()):
            skillgram.main(["seed", "anti-slop", "--never", "--hub", str(self.hub_root)])
        seeds = skillgram.read_seeds()
        self.assertEqual(seeds["always"], [])
        self.assertEqual(seeds["never"], ["anti-slop"])

        with contextlib.redirect_stdout(io.StringIO()):
            skillgram.main(["seed", "anti-slop", "--never", "--remove", "--hub", str(self.hub_root)])
        self.assertEqual(skillgram.read_seeds(), {"always": [], "never": []})

    def test_an_unknown_name_is_not_silently_accepted(self):
        res = skillgram.seed_skills(self.hub(), ["ghost-skill"])
        self.assertFalse(res["ok"])
        self.assertEqual(res["unknown"], ["ghost-skill"])
        self.assertEqual(skillgram.read_seeds()["always"], [])

    def test_bootstrap_applies_the_seeds_without_an_llm(self):
        skillgram.seed_skills(self.hub(), ["anti-slop"], mode="always")
        proj = self.tmp / "repo"
        proj.mkdir()
        plan = skillgram.bootstrap(self.hub(), proj, None, apply=False)
        self.assertEqual(plan["seeds"], ["anti-slop"])
        self.assertIn("anti-slop", plan["top_candidates"])

        applied = skillgram.bootstrap(self.hub(), proj, None, apply=True)
        self.assertTrue(applied["applied"], applied)
        self.assertEqual(applied["registradas"], ["anti-slop"])
        entry = skillgram._read_manifest(proj)["skills"]["anti-slop"]
        self.assertEqual(entry["by"], "seed")

    def test_list_marks_the_seeded_rows(self):
        skillgram.seed_skills(self.hub(), ["anti-slop"], mode="always")
        rows = {r["skill"]: r for r in skillgram.list_skills(self.hub())["rows"]}
        self.assertTrue(rows["anti-slop"]["seed"])
        self.assertFalse(rows["deploy-runbook"]["seed"])

    def test_project_status_recommends_the_seed_it_does_not_have(self):
        skillgram.seed_skills(self.hub(), ["anti-slop"], mode="always")
        proj = self.tmp / "proj"
        skillgram.init_project("proj", str(proj))
        skillgram.promote(self.hub(), ["deploy-runbook"], proj)

        st = skillgram.project_status(self.hub(), proj)
        self.assertEqual(st["seeds_missing"], ["anti-slop"])
        self.assertTrue(st["ok"], "a missing seed is a recommendation, not drift")

        skillgram.promote(self.hub(), ["anti-slop"], proj)
        self.assertEqual(skillgram.project_status(self.hub(), proj)["seeds_missing"], [])


class TestAlign(HomeCase):
    """`align` is the read-only question the personal case needs: what does each
    harness actually see of this project, and what is it missing. It never removes an
    `extra` and never touches a native -- the project set is the only source.
    """

    def project(self, skills=("deploy", "review")) -> Path:
        for name in skills:
            make_skill(self.hub_root, "infra-ops", name, f"{name} steps")
        proj = self.tmp / "proj"
        skillgram.init_project("proj", str(proj))
        skillgram.promote(skillgram.Hub(self.hub_root), list(skills), proj)
        return proj

    def harness(self, folder: str) -> Path:
        home = self.home / folder / "skills"
        home.mkdir(parents=True, exist_ok=True)
        return home

    def rows(self, proj) -> dict:
        return {r["harness"]: r for r in skillgram.align(skillgram.Hub(self.hub_root), proj)["rows"]}

    def test_a_harness_with_a_project_dir_reports_missing_then_is_fixed(self):
        proj = self.project()
        self.harness(".claude")
        before = self.rows(proj)["claude-code"]
        self.assertEqual(before["mode"], "project")
        self.assertEqual(before["missing"], ["deploy", "review"])
        self.assertEqual(before["fix"], "mount")

        res = skillgram.align(skillgram.Hub(self.hub_root), proj, apply=True)
        self.assertTrue(res["ok"], res["unfixable"])
        self.assertIn("claude-code", res["applied"])
        after = {r["harness"]: r for r in res["rows"]}["claude-code"]
        self.assertEqual(after["missing"], [])
        self.assertEqual(after["via_project"], 2)

    def test_a_global_only_harness_is_reported_and_not_fixed(self):
        proj = self.project()
        self.harness(".antigravity")
        res = skillgram.align(skillgram.Hub(self.hub_root), proj, apply=True)
        row = {r["harness"]: r for r in res["rows"]}["antigravity"]
        self.assertEqual(row["mode"], "global-only")
        self.assertEqual(row["fix"], "sync")
        self.assertEqual(sorted(row["missing"]), ["deploy", "review"])
        # no project dir to link into: the run cannot be green, and it says so
        self.assertFalse(res["ok"])
        self.assertEqual(res["unfixable"], ["antigravity"])

    def test_an_extra_is_reported_and_never_removed(self):
        proj = self.project()
        self.harness(".claude")
        make_skill(self.hub_root, "design-ui", "archify")
        skillgram.align(skillgram.Hub(self.hub_root), proj, apply=True)
        # someone links a skill that is not in the set
        extra = proj / ".claude" / "skills" / "archify"
        extra.parent.mkdir(parents=True, exist_ok=True)
        skillgram.make_links([(extra, self.hub_root / "design-ui" / "archify")])
        self.assertTrue(skillgram.is_link(extra))

        res = skillgram.align(skillgram.Hub(self.hub_root), proj, apply=True)
        row = {r["harness"]: r for r in res["rows"]}["claude-code"]
        self.assertEqual(row["extra"], ["archify"])
        self.assertTrue(skillgram.is_link(extra), "align never removes")

    def test_a_native_is_reported_and_left_alone(self):
        proj = self.project()
        self.harness(".claude")
        native = proj / ".claude" / "skills" / "mine"
        native.mkdir(parents=True)
        (native / "SKILL.md").write_text("---\nname: mine\n---\nbody\n", encoding="utf-8")
        res = skillgram.align(skillgram.Hub(self.hub_root), proj, apply=True)
        row = {r["harness"]: r for r in res["rows"]}["claude-code"]
        self.assertEqual(row["native"], ["mine"])
        self.assertFalse(skillgram.is_link(native))
        self.assertTrue((native / "SKILL.md").is_file())

    def test_hashes_say_what_each_harness_reads(self):
        proj = self.project()
        self.harness(".claude")
        skillgram.align(skillgram.Hub(self.hub_root), proj, apply=True)

        rows = {r["skill"]: r for r in skillgram.align_hashes(skillgram.Hub(self.hub_root), proj)}
        deploy = rows["deploy"]
        self.assertFalse(deploy["stale"])
        self.assertEqual(deploy["hub"], deploy["manifest"])
        self.assertEqual(deploy["harnesses"]["claude-code"]["state"], "link")

        # the hub moves: the manifest is now behind, and that is the whole "version" story
        (self.hub_root / "infra-ops" / "deploy" / "SKILL.md").write_text("v2\n", encoding="utf-8")
        deploy = {r["skill"]: r for r in skillgram.align_hashes(skillgram.Hub(self.hub_root), proj)}["deploy"]
        self.assertTrue(deploy["stale"])
        self.assertNotEqual(deploy["hub"], deploy["manifest"])

    def test_a_forked_copy_shows_its_own_hash(self):
        proj = self.project()
        self.harness(".claude")
        skillgram.align(skillgram.Hub(self.hub_root), proj, apply=True)
        link = proj / ".claude" / "skills" / "deploy"
        self.assertTrue(skillgram._remove_junction(link))
        link.mkdir()
        (link / "SKILL.md").write_text("---\nname: deploy\n---\nforked\n", encoding="utf-8")

        row = {r["skill"]: r for r in skillgram.align_hashes(skillgram.Hub(self.hub_root), proj)}["deploy"]
        seen = row["harnesses"]["claude-code"]
        self.assertEqual(seen["state"], "native")
        self.assertNotEqual(seen["hash"], row["hub"])

    def test_the_cli_flag_adds_the_table(self):
        proj = self.project()
        self.harness(".claude")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = skillgram.main(["align", "--hashes", "--apply", "--project", str(proj),
                                   "--hub", str(self.hub_root)])
        self.assertEqual(code, 0)
        self.assertIn("manifest", buf.getvalue())
        self.assertIn("ok", buf.getvalue())

    def test_the_cli_reports_a_table_and_exits_by_missing(self):
        proj = self.project()
        self.harness(".claude")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = skillgram.main(["align", "--project", str(proj), "--hub", str(self.hub_root)])
        self.assertEqual(code, 1, "missing skills are drift")
        self.assertIn("harness", buf.getvalue())
        self.assertIn("claude-code", buf.getvalue())

        with contextlib.redirect_stdout(io.StringIO()):
            code = skillgram.main(["align", "--apply", "--project", str(proj), "--hub", str(self.hub_root)])
        self.assertEqual(code, 0)

    def test_json_is_one_line_and_names_the_fix(self):
        proj = self.project()
        self.harness(".claude")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            skillgram.main(["align", "--json", "--project", str(proj), "--hub", str(self.hub_root)])
        payload = json.loads(buf.getvalue().strip())
        self.assertEqual(payload["op"], "align")
        row = {r["harness"]: r for r in payload["rows"]}["claude-code"]
        self.assertEqual(row["fix"], "mount")


class PolicyCase(HomeCase):
    """A project with a policy at each level, and one skill per outcome."""

    def setUp(self):
        super().setUp()
        self._env = os.environ.get(skillgram.POLICY_ENV)
        os.environ.pop(skillgram.POLICY_ENV, None)
        self.hub = skillgram.Hub(self.hub_root)
        make_skill(self.hub_root, "infra-ops", "deploy")
        make_skill(self.hub_root, "clients", "lotly-billing")
        make_skill(self.hub_root, "agent-workflows", "qring-anti-slop")
        self.proj = self.tmp / "proj"
        skillgram.init_project("proj", str(self.proj))
        skillgram.promote(self.hub, ["deploy", "lotly-billing"], self.proj)

    def tearDown(self):
        if self._env is None:
            os.environ.pop(skillgram.POLICY_ENV, None)
        else:
            os.environ[skillgram.POLICY_ENV] = self._env
        super().tearDown()

    def write(self, level: str, data: dict) -> Path:
        if level == "project":
            path = skillgram.project_dir(self.proj) / skillgram.POLICY_FILE
        elif level == "user":
            path = skillgram.user_policy_path()
            path.parent.mkdir(parents=True, exist_ok=True)
        else:
            path = self.tmp / "org-policy.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path


class TestPolicyReadOnly(PolicyCase):
    """The gate reports before it denies: `policy` shows the levels and `policy check`
    names every violation with the rule that produced it.
    """

    def test_no_policy_means_everything_is_allowed(self):
        res = skillgram.policy_check(self.hub, self.proj)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["levels"], [])
        self.assertEqual(res["violations"], [])

    def test_a_project_deny_is_a_violation_with_its_rule(self):
        self.write("project", {"deny": ["clients/*"]})
        res = skillgram.policy_check(self.hub, self.proj)
        self.assertFalse(res["ok"])
        self.assertEqual([v["skill"] for v in res["violations"]], ["lotly-billing"])
        self.assertEqual(res["violations"][0]["rule"], "project:deny:clients/*")

    def test_a_closed_level_allows_only_what_it_lists(self):
        self.write("project", {"mode": "closed", "allow": ["infra-ops/*"]})
        res = skillgram.policy_check(self.hub, self.proj)
        self.assertEqual([v["rule"] for v in res["violations"]], ["project:closed"])

    def test_a_bang_pattern_re_includes_inside_a_closed_level(self):
        self.write("project", {"mode": "closed", "allow": ["*", "!clients/*"]})
        res = skillgram.policy_check(self.hub, self.proj)
        self.assertEqual([v["skill"] for v in res["violations"]], ["lotly-billing"])

    def test_a_child_level_cannot_lift_a_parent_deny(self):
        org = self.write("org", {"deny": ["clients/lotly-*"]})
        self.write("project", {"mode": "open", "allow": ["clients/*"]})
        os.environ[skillgram.POLICY_ENV] = str(org)
        res = skillgram.policy_check(self.hub, self.proj)
        self.assertFalse(res["ok"], "a project file must not undo an organization deny")
        self.assertEqual(res["violations"][0]["rule"], "org:deny:clients/lotly-*")
        self.assertEqual([lv["level"] for lv in res["levels"]], ["org", "project"])

    def test_require_reports_what_the_project_is_missing(self):
        self.write("project", {"require": ["qring-anti-slop", "contract-first"]})
        res = skillgram.policy_check(self.hub, self.proj)
        self.assertEqual(res["required_missing"], ["contract-first", "qring-anti-slop"])
        skillgram.promote(self.hub, ["qring-anti-slop"], self.proj)
        res = skillgram.policy_check(self.hub, self.proj)
        self.assertEqual(res["required_missing"], ["contract-first"])

    def test_show_reports_each_level_with_a_hash(self):
        self.write("project", {"deny": ["clients/*"]})
        res = skillgram.policy_show(self.proj)
        self.assertEqual(res["levels"][0]["level"], "project")
        self.assertEqual(len(res["levels"][0]["hash"]), 16)
        self.assertEqual(res["levels"][0]["deny"], ["clients/*"])

    def test_a_broken_file_is_an_error_not_a_silent_pass(self):
        path = skillgram.project_dir(self.proj) / skillgram.POLICY_FILE
        path.write_text("{not json", encoding="utf-8")
        res = skillgram.policy_check(self.hub, self.proj)
        self.assertFalse(res["ok"])
        self.assertTrue(res["errors"])
        self.assertIn("invalid JSON", res["errors"][0])

    def test_the_reserved_harness_key_is_reported_but_not_enforced(self):
        self.write("project", {"harnesses": {"cursor": {"deny": ["infra-ops/*"]}}})
        shown = skillgram.policy_show(self.proj)
        self.assertEqual(shown["levels"][0]["harnesses_reserved"], ["cursor"])
        res = skillgram.policy_check(self.hub, self.proj)
        self.assertTrue(res["ok"], "per-harness is reserved in 1.7, not interpreted")

    def test_the_cli_shows_and_checks(self):
        self.write("project", {"deny": ["clients/*"]})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = skillgram.main(["policy", "show", "--project", str(self.proj), "--hub", str(self.hub_root)])
        self.assertEqual(code, 0)
        self.assertIn("project", buf.getvalue())
        self.assertIn("deny: clients/*", buf.getvalue())

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = skillgram.main(["policy", "check", "--project", str(self.proj), "--hub", str(self.hub_root)])
        self.assertEqual(code, 1, "a violation is drift")
        self.assertIn("lotly-billing", buf.getvalue())

    def test_json_is_one_line(self):
        self.write("project", {"deny": ["clients/*"]})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            skillgram.main(["policy", "check", "--json", "--project", str(self.proj),
                            "--hub", str(self.hub_root)])
        payload = json.loads(buf.getvalue().strip())
        self.assertEqual(payload["op"], "policy")
        self.assertEqual(payload["action"], "check")


class TestPolicyEnforced(PolicyCase):
    """The two doors that actually write: `use` refuses, `mount` and `sync` skip."""

    def test_use_refuses_a_denied_skill_and_says_why(self):
        self.write("project", {"deny": ["agent-workflows/*"]})
        res = skillgram.promote(self.hub, ["qring-anti-slop", "deploy"], self.proj)
        self.assertFalse(res["ok"])
        self.assertEqual([d["skill"] for d in res["denied"]], ["qring-anti-slop"])
        self.assertEqual(res["denied"][0]["rule"], "project:deny:agent-workflows/*")
        # the allowed one still goes through
        self.assertIn("deploy", res["created"] + res["already_present"])
        self.assertFalse((skillgram.project_dir(self.proj) / "skills" / "qring-anti-slop").exists())

    def test_mount_skips_the_denied_one(self):
        self.write("project", {"deny": ["clients/*"]})
        (self.home / ".claude" / "skills").mkdir(parents=True)
        res = skillgram.mount_project(self.hub, self.proj, apply=True)
        self.assertEqual(res["skills"], 1)
        self.assertEqual([s["skill"] for s in res["policy_skipped"]], ["lotly-billing"])
        self.assertTrue((self.proj / ".claude" / "skills" / "deploy").exists())
        self.assertFalse((self.proj / ".claude" / "skills" / "lotly-billing").exists())

    def test_sync_removes_a_denied_link_and_never_recreates_it(self):
        skills_dir = self.home / ".claude" / "skills"
        skills_dir.mkdir(parents=True)
        # an old link that the policy now forbids
        skillgram.make_links([(skills_dir / "lotly-billing", self.hub_root / "clients" / "lotly-billing")])
        self.assertTrue(skillgram.is_link(skills_dir / "lotly-billing"))

        self.write("user", {"deny": ["clients/*"]})
        skillgram.sync(self.hub, quiet=True)
        self.assertFalse(os.path.lexists(skills_dir / "lotly-billing"), "deny is not advice")
        self.assertTrue(skillgram.is_link(skills_dir / "deploy"), "the rest keeps working")

    def test_align_apply_links_nothing_denied(self):
        self.write("project", {"deny": ["clients/*"]})
        (self.home / ".claude" / "skills").mkdir(parents=True)
        res = skillgram.align(self.hub, self.proj, apply=True)
        self.assertEqual(res["skills"], 2, "align counts the set, the door filters it")
        self.assertFalse((self.proj / ".claude" / "skills" / "lotly-billing").exists())

    def test_touch_is_never_blocked(self):
        self.write("project", {"deny": ["clients/*"]})
        res = skillgram.touch_usage(self.proj, ["lotly-billing"], by="claude")
        self.assertEqual(res["touched"], ["lotly-billing"], "evidence is not access")


def plan_skips(hub) -> list:
    return skillgram.plan(hub, deep=True, policy=skillgram.policy_for(None)).skipped


class TestClone(HomeCase):
    """`clone` is the between-machines door: a git repo (private or public) is the
    transport, git does the auth, and skillgram verifies what arrived and links it.
    """

    def setUp(self):
        super().setUp()
        if not shutil.which("git"):
            self.skipTest("git is not on PATH")
        self.origin = self.tmp / "skills-origin"
        make_skill(self.origin, "infra-ops", "deploy")
        make_skill(self.origin, "design-ui", "archify")
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "hub")

    def git(self, *args) -> None:
        subprocess.run(["git", "-C", str(self.origin), *args], check=True, capture_output=True)

    def test_clone_brings_the_skills_and_links_them(self):
        (self.home / ".claude" / "skills").mkdir(parents=True)
        dest = self.tmp / "hub"
        res = skillgram.clone_hub(str(self.origin), dest)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["transport"], "clone")
        self.assertEqual(res["skills"], 2)
        self.assertEqual(sorted(skillgram.Hub(dest).skills()), ["archify", "deploy"])
        self.assertTrue(skillgram.is_link(self.home / ".claude" / "skills" / "deploy"))
        self.assertTrue((dest / ".git").is_dir())

    def test_no_sync_leaves_the_harnesses_alone(self):
        (self.home / ".claude" / "skills").mkdir(parents=True)
        res = skillgram.clone_hub(str(self.origin), self.tmp / "hub", sync_after=False)
        self.assertTrue(res["ok"], res)
        self.assertIsNone(res["synced"])
        self.assertFalse(os.path.lexists(self.home / ".claude" / "skills" / "deploy"))

    def test_second_run_pulls_instead_of_cloning(self):
        dest = self.tmp / "hub"
        skillgram.clone_hub(str(self.origin), dest, sync_after=False)
        make_skill(self.origin, "infra-ops", "another")
        self.git("add", "-A")
        self.git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "more")
        res = skillgram.clone_hub(str(self.origin), dest, sync_after=False)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["transport"], "pull")
        self.assertEqual(res["skills"], 3)

    def test_another_origin_is_refused_unless_forced(self):
        dest = self.tmp / "hub"
        skillgram.clone_hub(str(self.origin), dest, sync_after=False)
        other = self.tmp / "other-origin"
        make_skill(other, "infra-ops", "x")
        subprocess.run(["git", "-C", str(other), "init", "-q"], check=True, capture_output=True)
        res = skillgram.clone_hub(str(other), dest, sync_after=False)
        self.assertFalse(res["ok"])
        self.assertIn("already tracks", res["error"])

    def test_a_non_empty_folder_needs_force_and_never_overwrites(self):
        dest = self.tmp / "hub"
        make_skill(dest, "infra-ops", "deploy", "my own version")
        res = skillgram.clone_hub(str(self.origin), dest, sync_after=False)
        self.assertFalse(res["ok"])
        self.assertIn("--force", res["error"])

        forced = skillgram.clone_hub(str(self.origin), dest, force=True, sync_after=False)
        self.assertTrue(forced["ok"], forced)
        self.assertEqual(forced["transport"], "merge")
        self.assertEqual(forced["conflicts"], ["infra-ops/deploy"])
        self.assertEqual(forced["added"], ["design-ui/archify"])
        kept = (dest / "infra-ops" / "deploy" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("my own version", kept, "a skill that already exists wins")

    def test_a_repo_without_skills_says_so(self):
        empty = self.tmp / "not-a-hub"
        empty.mkdir()
        (empty / "README.md").write_text("nothing\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(empty), "init", "-q"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(empty), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(empty), "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-q", "-m", "x"], check=True, capture_output=True)
        res = skillgram.clone_hub(str(empty), self.tmp / "hub", sync_after=False)
        self.assertFalse(res["ok"])
        self.assertIn("is this a skill hub", res["error"])

    def test_the_cli_prints_a_summary(self):
        dest = self.tmp / "hub"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = skillgram.main(["clone", str(self.origin), "--hub", str(dest), "--no-sync"])
        self.assertEqual(code, 0)
        text = buf.getvalue()
        self.assertIn("2 skills", text)
        self.assertIn("not linked", text)


class TestRealHarnessCli(HomeCase):
    """AGENTS.md says the `mcp add` argv shapes were checked against the real CLIs: that
    was a manual promise. This is the automated half, and it never writes a config -- it
    asks each installed CLI whether it understands the verb we build.
    """

    CLIENTS = ("reasonix", "codex", "claude", "hermes", "gemini")

    def test_the_installed_cli_knows_the_verb_we_build(self):
        found = [(client, shutil.which(client)) for client in self.CLIENTS]
        found = [(client, path) for client, path in found if path]
        if not found:
            self.skipTest("no harness CLI on PATH")
        for client, path in found:
            code, out = skillgram.run_cli([path, "mcp", "--help"], timeout=60)
            self.assertEqual(code, 0, f"{client} mcp --help failed: {out[:200]}")
            self.assertNotIn("invalid choice", out.lower(), f"{client} has no mcp verb")
            probe = skillgram.mcp_probe_argv(client, skillgram.MCP_SERVER, path)
            code, out = skillgram.run_cli(probe, timeout=60)
            self.assertIn(code, (0, 1), f"{client} probe exited {code}: {out[:200]}")
            for bad in ("unknown command", "unrecognized"):
                self.assertNotIn(bad, out.lower(), f"{client}: {out[:200]}")


class TestWatch(HomeCase):
    """`watch` is the only surface that works without being asked, and it had no test:
    a polling thread that re-syncs when the hub changes.
    """

    def test_it_links_what_appears_after_it_starts(self):
        make_skill(self.hub_root, "infra-ops", "deploy")
        (self.home / ".claude" / "skills").mkdir(parents=True)
        watcher = skillgram.Watcher(skillgram.Hub(self.hub_root), interval=0.2, quiet=True)
        watcher.start()
        try:
            self.assertTrue(watcher.running)
            for _ in range(60):
                if os.path.lexists(self.home / ".claude" / "skills" / "deploy"):
                    break
                time.sleep(0.2)
            self.assertTrue(os.path.lexists(self.home / ".claude" / "skills" / "deploy"),
                            "the watcher never linked the skill")

            make_skill(self.hub_root, "infra-ops", "review")
            for _ in range(60):
                if os.path.lexists(self.home / ".claude" / "skills" / "review"):
                    break
                time.sleep(0.2)
            self.assertTrue(os.path.lexists(self.home / ".claude" / "skills" / "review"),
                            "the watcher missed a skill that appeared while watching")
        finally:
            watcher.stop()
        self.assertFalse(watcher.running)


class TestLintBaseline(HomeCase):
    """A corpus with legacy failures needs a gate that can be green: the baseline is the
    accepted debt, and only new failures fail the run.
    """

    def hub_with_a_bad_skill(self):
        make_skill(self.hub_root, "infra-ops", "good", "Activation Contract\nbody\n")
        bad = self.hub_root / "infra-ops" / "bad"
        bad.mkdir(parents=True)
        (bad / "SKILL.md").write_text("no frontmatter here\n", encoding="utf-8")
        return skillgram.Hub(self.hub_root)

    def test_the_debt_is_accepted_and_only_new_errors_fail(self):
        hub = self.hub_with_a_bad_skill()
        baseline = self.tmp / "lint-baseline.json"

        self.assertFalse(skillgram.lint_hub(hub)["ok"], "without a baseline the corpus is red")

        # writing the baseline records the debt: from here the same corpus is green
        skillgram.lint_hub(hub, baseline=baseline, write_baseline=baseline)
        self.assertTrue(baseline.is_file())
        again = skillgram.lint_hub(hub, baseline=baseline)
        self.assertTrue(again["ok"], again["new"])
        self.assertTrue(again["accepted"])
        self.assertEqual(again["new"], [])

        fresh = self.hub_root / "infra-ops" / "worse"
        fresh.mkdir(parents=True)
        (fresh / "SKILL.md").write_text("---\nname: Nope Name\n---\n", encoding="utf-8")
        third = skillgram.lint_hub(hub, baseline=baseline)
        self.assertFalse(third["ok"], "a new failure is not covered by the debt")
        self.assertEqual([row["skill"] for row in third["new"]], ["worse"])

    def test_the_cli_wires_the_flags(self):
        self.hub_with_a_bad_skill()
        baseline = self.tmp / "b.json"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(skillgram.main(["lint", "--json", "--hub", str(self.hub_root)]), 1)
            self.assertEqual(skillgram.main(["lint", "--json", "--write-baseline", str(baseline),
                                             "--hub", str(self.hub_root)]), 0)
            self.assertEqual(skillgram.main(["lint", "--json", "--baseline", str(baseline),
                                             "--hub", str(self.hub_root)]), 0)


class TestPackageShape(HomeCase):
    """Guards for the split itself: the invariants that broke once and must not again."""

    def module_files(self) -> list:
        return sorted(Path(skillgram.__file__).resolve().parent.glob("*.py"))

    def test_every_module_defers_its_annotations(self):
        # the split dropped `from __future__ import annotations` from most files (it is a
        # compiler directive, not a name) and 3.9 died on `set[str] | None` at import
        missing = []
        for path in self.module_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            ok = any(
                isinstance(node, ast.ImportFrom) and node.module == "__future__"
                and any(a.name == "annotations" for a in node.names)
                for node in tree.body
            )
            if not ok:
                missing.append(path.name)
        self.assertEqual(missing, [], "these modules would break on Python 3.9")

    def test_no_module_captures_home_at_import_time(self):
        # one patch point: every module reads through home_dir()
        offenders = []
        for path in self.module_files():
            if path.name == "hub.py":
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if re.match(r"^[A-Z_]+\s*=\s*HOME\b", line):
                    offenders.append(f"{path.name}:{number}")
        self.assertEqual(offenders, [])

    def test_the_package_runs_without_the_checkout_shim(self):
        root = str(Path(__file__).resolve().parents[1])
        result = subprocess.run([sys.executable, "-m", "skillgram", "--version"],
                                capture_output=True, text=True, encoding="utf-8",
                                cwd=str(self.tmp), env=dict(os.environ, PYTHONPATH=root))
        self.assertEqual(result.returncode, 0, result.stderr[-400:])
        self.assertIn(skillgram.VERSION, result.stdout)


class TestMcpThroughTheRealCli(HomeCase):
    """`install` delegates the MCP registration to the harness CLI, and every other
    test injects a Python callable. That leaves the real path unproven: an actual
    process spawn, the argv each CLI expects, and the re-probe that -- not the exit
    code -- decides whether the registration happened.

    `SKILLGRAM_MCP_CLI_<HARNESS>` is the documented switch for "this launcher owns
    the config", so a stub behind it exercises the same code with nothing at risk.
    """

    ENV_KEYS = ("SKILLGRAM_MCP_CLI_REASONIX", "SKILLGRAM_MCP_CMD")

    def setUp(self):
        super().setUp()
        self._env = {k: os.environ.get(k) for k in self.ENV_KEYS}
        for key in self.ENV_KEYS:
            os.environ.pop(key, None)
        self.calls_log = self.tmp / "calls.log"
        os.environ["SKILLGRAM_MCP_CLI_REASONIX"] = str(self.make_stub())

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        super().tearDown()

    def make_stub(self) -> Path:
        """A launcher that behaves like `mcp get` / `mcp add` and records its argv."""
        launch = skillgram.mcp_launch_argv()
        marker = launch[1] if len(launch) > 1 else launch[0]
        script = self.tmp / "stub.py"
        script.write_text(
            "import json, os, sys\n"
            f"STATE = {str(self.calls_log)!r}\n"
            f"MARKER = {marker!r}\n"
            "argv = sys.argv[1:]\n"
            "open(STATE, 'a', encoding='utf-8').write(json.dumps(argv) + '\\n')\n"
            "if argv[:2] == ['mcp', 'get']:\n"
            "    if not os.path.exists(STATE + '.registered'):\n"
            "        print('no server named skillgram', file=sys.stderr)\n"
            "        sys.exit(1)\n"
            "    print('skillgram')\n"
            "    print('  command: ' + MARKER)\n"
            "    sys.exit(0)\n"
            "if argv[:2] == ['mcp', 'add']:\n"
            "    open(STATE + '.registered', 'w', encoding='utf-8').close()\n"
            "    print('added')\n"
            "    sys.exit(0)\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )
        # run_cli() spawns argv[0] as an executable: on Windows that is a .cmd
        # (and run_cli routes it through `cmd /c`), on POSIX an executable script.
        if os.name == "nt":
            shim = self.tmp / "stub.cmd"
            shim.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        else:
            shim = self.tmp / "stub.sh"
            shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
            shim.chmod(0o755)
        return shim

    def recorded(self) -> list:
        if not self.calls_log.is_file():
            return []
        return [json.loads(line) for line in self.calls_log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_dry_run_names_the_binary_and_spawns_nothing_but_the_probe(self):
        out = skillgram.register_mcp(skillgram.Hub(self.hub_root), "reasonix", apply=False)
        self.assertEqual(out["state"], "planned", out)
        self.assertEqual(out["command"][:3], [out["cli"], "mcp", "add"])
        self.assertEqual([call[:2] for call in self.recorded()], [["mcp", "get"]])

    def test_a_real_spawn_registers_and_the_reprobe_decides(self):
        out = skillgram.register_mcp(skillgram.Hub(self.hub_root), "reasonix", apply=True)
        self.assertEqual(out["state"], "registered", out)
        self.assertTrue(out["verified"])
        # probe -> add -> probe: the config is the evidence, not the exit code
        self.assertEqual([call[:2] for call in self.recorded()],
                         [["mcp", "get"], ["mcp", "add"], ["mcp", "get"]])
        add = self.recorded()[1]
        self.assertEqual(add[:3], ["mcp", "add", "skillgram"])
        # the reasonix form is positional, env pairs as --env=K=V (checked against
        # the real CLI): the test pins that shape, not just "add was called"
        self.assertIn(f"--env={skillgram.HARNESS_ENV}=reasonix", add)
        self.assertTrue(any(arg.startswith("--env=SKILLGRAM_HUB=") for arg in add))
        launch = skillgram.mcp_launch_argv()
        self.assertEqual(add[-len(launch):], launch)

    def test_second_install_is_idempotent_through_the_real_cli(self):
        hub = skillgram.Hub(self.hub_root)
        skillgram.register_mcp(hub, "reasonix", apply=True)
        seen = len(self.recorded())
        again = skillgram.register_mcp(hub, "reasonix", apply=True)
        self.assertEqual(again["state"], "already")
        self.assertEqual([call[:2] for call in self.recorded()[seen:]], [["mcp", "get"]])


class TestDaemon(HomeCase):
    """The daemon is how edgePass talks to skillgram (one JSON line in, one out) and
    it had no test: it owns its own stdin/stdout, so it is exercised as a process.
    """

    def setUp(self):
        super().setUp()
        make_skill(self.hub_root, "infra-ops", "deploy")
        self.proc = None

    def tearDown(self):
        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.kill()
            self.proc.wait(timeout=20)
            for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
        super().tearDown()

    def start(self):
        env = dict(os.environ)
        env.update({
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            # the daemon reports on HOME's agents: keep the real ones out of the test
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "APPDATA": str(self.tmp / "appdata"),
        })
        self.proc = subprocess.Popen(
            [*skillgram.launch_argv(), "daemon", "--hub", str(self.hub_root)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            cwd=str(self.tmp), env=env,
        )
        return self

    def read(self, timeout: float = 30.0) -> dict:
        box = {}
        thread = threading.Thread(target=lambda: box.setdefault("line", self.proc.stdout.readline()), daemon=True)
        thread.start()
        thread.join(timeout)
        if "line" not in box:
            self.proc.kill()
            self.proc.wait(timeout=20)
            self.fail(f"the daemon did not answer in {timeout}s; stderr: {self.proc.stderr.read()[:600]}")
        if not box["line"]:
            self.proc.wait(timeout=20)
            self.fail(f"the daemon closed its stdout (exit {self.proc.returncode}); "
                      f"stderr: {self.proc.stderr.read()[:900]}")
        return json.loads(box["line"])

    def send(self, line: str) -> dict:
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()
        return self.read()

    def test_one_json_line_in_one_json_line_out(self):
        self.start()
        hello = self.read()
        self.assertEqual(hello["op"], "hello")
        self.assertEqual(Path(hello["hub"]), self.hub_root)
        self.assertEqual(hello["version"], skillgram.VERSION)

        self.assertEqual(self.send('{"op": "ping"}'), {"ok": True, "op": "pong"})

        garbage = self.send("this is not json")
        self.assertFalse(garbage["ok"])
        self.assertEqual(garbage["error"], "the line is not JSON")

        unknown = self.send('{"op": "nope"}')
        self.assertFalse(unknown["ok"])
        self.assertIn("unknown op", unknown["error"])

        st = self.send('{"op": "status"}')
        self.assertEqual(st["op"], "status")
        self.assertEqual(st["skills"], 1)
        self.assertEqual(st["version"], skillgram.VERSION)

        self.assertEqual(self.send('{"op": "quit"}')["op"], "quit")
        self.assertEqual(self.proc.wait(timeout=20), 0, "quit must exit 0")


if __name__ == "__main__":
    unittest.main()




