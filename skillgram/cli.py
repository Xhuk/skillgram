"""The command line: parser, dispatch, renderers, `main`."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .adopt import (
    adopt_natives,
    adopt_skill,
    apply_onboard,
    apply_precedence,
    flavor_scan,
    onboard,
    port_missing,
    port_skill,
    replace_native_with_link,
)
from .drift import clone_hub, status, sync
from .govern import policy_show, seed_skills
from .harnesses import (
    caller_identity,
    doctor,
    install,
    install_report,
    propagate,
    propagate_report,
)
from .hub import VERSION, Hub, default_hub, detect_harnesses, home_dir, normalize_harness
from .lifecycle import (
    demand_report,
    diff_skill,
    export_hub,
    hub_git,
    import_hub,
    index_build,
    index_search,
    lint_hub,
    list_skills,
    new_skill,
    pin_skill,
    retire_skill,
    review_list,
    touch_usage,
)
from .llm import llm_config
from .scope import (
    ALIGN_LIST_LIMIT,
    align,
    init_project,
    inject_protocol,
    mount_project,
    policy_check,
    project_status,
    promote,
    resolve_project,
    unpromote,
)
from .select import bootstrap, missing, propose_skill
from .serve import (
    Watcher,
    daemon,
    install_autostart,
    mcp_serve,
    single_instance,
    uninstall_autostart,
)


def add_common_flags(parser: argparse.ArgumentParser, with_defaults: bool = True) -> None:
    """--hub/--json/--quiet para un parser.

    `with_defaults=False` deja cada opcion en SUPPRESS: el subparser solo escribe
    el valor cuando el flag aparece de verdad. argparse vuelca los defaults del
    subparser sobre el namespace que ya lleno el parser de arriba, asi que un
    default normal aca borraria `skillgram --hub X status`.
    """
    parser.add_argument(
        "--hub",
        default=str(default_hub()) if with_defaults else argparse.SUPPRESS,
        help="hub root (default ~/.agents/skills)",
    )
    parser.add_argument(
        "--json", action="store_true",
        default=False if with_defaults else argparse.SUPPRESS,
        help="one JSON line on stdout",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        default=False if with_defaults else argparse.SUPPRESS,
        help="no logs on stdout",
    )


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="skillgram",
        description="Keeps the skill library linked into every agent.",
    )
    # Los flags globales valen en las dos posiciones: `skillgram --hub X status`
    # y `skillgram status --hub X`. El parser de arriba lleva los defaults; las
    # copias de los subparsers van en SUPPRESS para no pisarlos.
    add_common_flags(ap)
    ap.add_argument("--version", action="version", version=f"skillgram {VERSION}",
                    help="print the version and exit")
    common = argparse.ArgumentParser(add_help=False)
    add_common_flags(common, with_defaults=False)

    sub = ap.add_subparsers(dest="cmd", required=True)

    p_onb = sub.add_parser("onboard", parents=[common], help="detect the harnesses and unify their skills into the hub (optional LLM)")
    p_onb.add_argument("--scope", choices=["all", "current"], help="unify all harnesses or just one (asks when missing)")
    p_onb.add_argument("--harness", help="harness name for --scope current")
    p_onb.add_argument("--apply", action="store_true", help="execute the plan (without it: show it only)")
    p_onb.add_argument("--replace-natives", action="store_true", help="replace the native copy with a link (the copy goes to quarantine)")
    p_onb.add_argument("--no-llm", action="store_true", help="do not ask the connected LLM")
    p_onb.add_argument("--plan", help="write the JSON plan to this path")
    sub.add_parser("flavor", parents=[common], help="detect which agent each hub skill is wired to").add_argument(
        "--apply", action="store_true", help="save the flavor into SKILLMETA.json")
    p_port = sub.add_parser("port", parents=[common], help="port skills hard-wired to another harness (agents/<harness>.md)")
    p_port.add_argument("skill", nargs="?", help="one skill (default: scan the whole hub)")
    p_port.add_argument("--to", help="target harness (default: every installed one)")
    p_port.add_argument("--apply", action="store_true", help="write the files (default: plan only)")
    p_port.add_argument("--no-llm", action="store_true")
    p_prec = sub.add_parser("precedence", parents=[common], help="declare the hub first in each harness config")
    p_prec.add_argument("--apply", action="store_true", help="write (with backup); default: show only")
    p_prec.add_argument("--harness", help="only this harness (e.g. hermes, with its own HERMES_HOME)")
    p_adopt = sub.add_parser("adopt", parents=[common], help="adopt native skills: copy into the hub, verify the hash, leave a link")
    p_adopt.add_argument("path", nargs="?", help="the skill folder")
    p_adopt.add_argument("--all", action="store_true", help="every native skill of every harness")
    p_mount = sub.add_parser("mount", parents=[common], help="materialize the project scope in every harness")
    p_mount.add_argument("--project", help="project folder (default: engram-style resolution)")
    p_mount.add_argument("--apply", action="store_true", help="create/remove the links (default: plan only)")
    p_mount.add_argument("--prune-global", action="store_true", help="also remove the home links to the hub (strict)")
    p_mount.add_argument("--no-protocol", action="store_true", help="do not inject the skillgram protocol into AGENTS.md/CLAUDE.md")
    p_mount.add_argument("--bootstrap", dest="bootstrap", action="store_true", default=None,
                         help="if the repo has no scope, bootstrap it with the connected LLM first (default: ask)")
    p_mount.add_argument("--no-bootstrap", dest="bootstrap", action="store_false", help="never bootstrap from mount")
    p_mount.add_argument("--yes", "-y", action="store_true", help="assume yes for the bootstrap confirmation")
    p_install = sub.add_parser("install", parents=[common],
                               help="one idempotent command: harness -> hub -> MCP -> propagate")
    p_install.add_argument("--harness", help="harness identity (default: SKILLGRAM_HARNESS, then the process tree)")
    p_install.add_argument("--root", help="directory whose git repos get the scope (default: the current project only)")
    p_install.add_argument("--dry-run", action="store_true", help="print the exact commands, write nothing")
    p_install.add_argument("--bootstrap", dest="bootstrap_repos", action="store_true",
                           help="repos without a scope: pick a set with the connected LLM (default: skip and report)")
    p_install.add_argument("--depth", type=int, default=2, help="how deep to look for repos under --root (default 2)")
    p_prop = sub.add_parser("propagate", parents=[common],
                            help="mount + protocol + ledger for every repo of a harness under a root")
    p_prop.add_argument("--harness", help="harness identity (default: SKILLGRAM_HARNESS, then the process tree)")
    p_prop.add_argument("--root", help="directory to scan for git repos (default: the current project)")
    p_prop.add_argument("--apply", action="store_true", help="write (default: plan only)")
    p_prop.add_argument("--force", action="store_true", help="ignore the ledger and redo every repo")
    p_prop.add_argument("--bootstrap", dest="bootstrap_repos", action="store_true",
                        help="repos without a scope: pick a set with the connected LLM")
    p_prop.add_argument("--depth", type=int, default=3, help="how deep to look for repos under --root (default 3)")
    p_boot = sub.add_parser("bootstrap", parents=[common], help="repo without scope: signals + LLM picks the initial set (and reports gaps)")
    p_boot.add_argument("--project")
    p_boot.add_argument("--apply", action="store_true", help="record the chosen set in the project")
    p_boot.add_argument("--no-llm", action="store_true")
    p_miss = sub.add_parser("missing", parents=[common], help="deterministic query: is there a skill for this?")
    p_miss.add_argument("text", nargs="+")
    p_miss.add_argument("--project")
    p_seed = sub.add_parser("seed", parents=[common], help="skills that are always recommended (or never)")
    p_seed.add_argument("skills", nargs="*", help="skill names (none: show the list)")
    p_seed.add_argument("--never", action="store_true", help="never recommend them instead")
    p_seed.add_argument("--remove", action="store_true", help="drop them from the list")
    p_prop = sub.add_parser("propose", parents=[common], help="draft a new skill with the LLM and (with --apply) publish it")
    p_prop.add_argument("need", nargs="+")
    p_prop.add_argument("--project")
    p_prop.add_argument("--apply", action="store_true", help="publish into the hub, record it and mount it")
    p_prop.add_argument("--no-llm", action="store_true")
    sub.add_parser("index", parents=[common], help="(re)build the hub FTS5 index")
    p_srch = sub.add_parser("search", parents=[common], help="full-text search in the hub")
    p_srch.add_argument("text", nargs="+")
    p_srch.add_argument("--limit", type=int, default=10)
    p_rev = sub.add_parser("review", parents=[common], help="skills never used or not used recently")
    p_rev.add_argument("--project")
    p_rev.add_argument("--days", type=int, default=90)
    p_rev.add_argument("--fail-if-stale", action="store_true", help="exit 1 when something is stale (CI gate)")
    p_pin = sub.add_parser("pin", parents=[common], help="pin project skills (they never go stale)")
    p_pin.add_argument("skills", nargs="+")
    p_pin.add_argument("--project")
    p_pin.add_argument("--unpin", action="store_true")
    p_ret = sub.add_parser("retire", parents=[common], help="retire a hub skill into _retired (never deletes)")
    p_ret.add_argument("skill")
    p_ret.add_argument("--reason", default="")
    p_exp = sub.add_parser("export", parents=[common], help="export the hub into a tar.gz with hashes")
    p_exp.add_argument("file")
    p_imp = sub.add_parser("import", parents=[common], help="import a tar.gz exported by skillgram")
    p_imp.add_argument("file")
    p_imp.add_argument("--apply", action="store_true")
    p_lint = sub.add_parser("lint", parents=[common], help="quality gate: frontmatter, name, description, body size")
    p_lint.add_argument("skill", nargs="?", help="one skill (default: the whole hub)")
    p_lint.add_argument("--strict", action="store_true", help="warnings also fail")
    p_lint.add_argument("--contract", action="store_true",
                        help="require the five sections of docs/SKILL-CONTRACT.md")
    p_lint.add_argument("--baseline", help="JSON file of accepted failures: only new ones fail")
    p_lint.add_argument("--write-baseline", dest="write_baseline",
                        help="write the current failures to that file as accepted debt")
    p_new = sub.add_parser("new", parents=[common], help="scaffold a skill from the contract (does not publish)")
    p_new.add_argument("name", help="kebab-case name; it becomes the folder")
    p_new.add_argument("--category", help="existing hub category (default: imported)")
    p_new.add_argument("--by", default="human", help="who authors it (default: human)")
    p_list = sub.add_parser("list", parents=[common], help="what is in the hub (and what was used in this project)")
    p_list.add_argument("--category", help="only this category")
    p_list.add_argument("--project", help="read usage from this project scope")
    p_list.add_argument("--used", choices=["used", "unused"], help="only used / never used in that project")
    p_list.add_argument("--limit", type=int, help="show at most N")
    p_touch = sub.add_parser("touch", parents=[common], help="declare that you used these skills (the only usage signal)")
    p_touch.add_argument("skills", nargs="+")
    p_touch.add_argument("--project", help="project scope (default: engram-style resolution)")
    p_touch.add_argument("--by", default="agent", help="who used it (harness or agent, default: agent)")
    p_dem = sub.add_parser("demand", parents=[common], help="queries the hub could not cover (real roadmap)")
    p_dem.add_argument("--limit", type=int, default=20)
    p_git = sub.add_parser("git", parents=[common], help="hub as a git repo: status | init | pull | push")
    p_git.add_argument("action", choices=["status", "init", "pull", "push"], nargs="?", default="status")
    p_git.add_argument("--apply", action="store_true")
    p_diff = sub.add_parser("diff", parents=[common], help="compare a hub skill against another copy (divergence)")
    p_diff.add_argument("skill")
    p_diff.add_argument("--against", required=True, help="path to the other copy")
    p_mcp = sub.add_parser("mcp", parents=[common], help="MCP server (stdio) so the agent can query the hub")
    p_mcp.add_argument("--read-only", action="store_true", help="the agent can query but not write")
    sub.add_parser("doctor", parents=[common], help="inventory: harnesses, native, collisions, drift")
    p_status = sub.add_parser("status", parents=[common], help="current drift, touching nothing")
    p_status.add_argument("--verify", action="store_true", help="also verify where each link points (slow)")
    p_sync = sub.add_parser("sync", parents=[common], help="apply the desired state (idempotent)")
    p_sync.add_argument("--dry-run", action="store_true")
    p_watch = sub.add_parser("watch", parents=[common], help="watch the hub and re-sync on change")
    p_watch.add_argument("--interval", type=float, default=5.0)
    p_daemon = sub.add_parser("daemon", parents=[common], help="JSON-line server for edgePass")
    p_daemon.add_argument("--interval", type=float, default=5.0)
    p_daemon.add_argument("--watch", action="store_true", help="start watching immediately")
    p_auto = sub.add_parser("install-autostart", parents=[common], help="scheduled task at logon")
    p_auto.add_argument("--interval", type=float, default=10.0)
    sub.add_parser("uninstall-autostart", parents=[common], help="remove the scheduled task")

    p_init = sub.add_parser("init", parents=[common], help="create the project's .agents/config.json")
    p_init.add_argument("project", nargs="?", help="project name (default: the cwd basename)")
    p_init.add_argument("--path", help="project folder (default: cwd)")
    p_init.add_argument("--force", action="store_true", help="replace an existing config")

    p_use = sub.add_parser("use", parents=[common], help="promote hub skills into the project")
    p_use.add_argument("skills", nargs="+")
    p_use.add_argument("--project", help="project folder (default: engram-style resolution)")
    p_use.add_argument("--by", help="who is using them (default: the harness that invoked skillgram)")

    p_unuse = sub.add_parser("unuse", parents=[common], help="drop skills from the project scope")
    p_unuse.add_argument("skills", nargs="+")
    p_unuse.add_argument("--project")

    p_pstat = sub.add_parser("project-status", parents=[common], help="health of the project scope")
    p_pstat.add_argument("--project")

    p_align = sub.add_parser("align", parents=[common],
                             help="what each harness actually sees of this project, and what it lacks")
    p_align.add_argument("--project")
    p_align.add_argument("--apply", action="store_true", help="link the missing ones (never removes, never touches natives)")
    p_align.add_argument("--hashes", action="store_true",
                         help="also show which content each harness reads (link, forked copy, stale manifest)")

    p_policy = sub.add_parser("policy", parents=[common],
                              help="the gate: show the effective policy, or check a project against it")
    p_policy.add_argument("action", nargs="?", choices=["show", "check"], default="show")
    p_policy.add_argument("--project")
    p_policy.add_argument("--policy", help="organization policy: a path, or url#ref of a git repo")

    p_clone = sub.add_parser("clone", parents=[common],
                             help="bring a hub in from a git repo (private or public) and link it")
    p_clone.add_argument("url", help="git URL of the hub repository")
    p_clone.add_argument("--ref", help="branch or tag to take (default: the repo default)")
    p_clone.add_argument("--force", action="store_true",
                         help="merge into a non-empty folder, or re-point an existing clone (never overwrites a skill)")
    p_clone.add_argument("--no-sync", action="store_true", help="do not link the harnesses afterwards")
    return ap


def _mount_bootstrap(hub: Hub, root: Path, args, result: dict) -> dict | None:
    """Repo sin scope: ofrece bootstrapear y, si el set se aplico, re-monta.

    Devuelve el resultado nuevo, o None para quedarse con el original (solo se
    anoto la pista o no se aplico nada). La confirmacion es interactiva salvo
    --bootstrap/--yes, y sin LLM solo se explica el paso siguiente.
    """
    cfg = llm_config()
    if not cfg:
        result["bootstrap_hint"] = "no LLM configured: run `skillgram use <skill>...` in this repo"
        return None
    if not args.apply:
        return None
    want = args.bootstrap is True or bool(args.yes)
    if not want and sys.stdin.isatty() and not args.json:
        ans = ""
        try:
            ans = input(f"no skills in scope for {root.name}; bootstrap it now? [y/N]: ")
        except (EOFError, OSError):
            ans = ""
        want = ans.strip().lower().startswith("y")
    if not want:
        return None
    result["bootstrap"] = bootstrap(hub, root, cfg, apply=True)
    if not result["bootstrap"].get("applied"):
        return None
    again = mount_project(hub, root, apply=True, prune_global=args.prune_global)
    again["bootstrap"] = "done"
    return again


def _onboard(hub: Hub, args) -> dict | int:
    """onboard: sin --scope pregunta, sin --apply solo devuelve el plan.

    Un int es una salida temprana con su codigo (invocacion mala), igual que en
    `dispatch`.
    """
    scope = args.scope
    if not scope:
        ans = ""
        try:
            if sys.stdin.isatty() and not args.json:
                ans = input("Unify ALL harnesses or only the current one? [all/current] (all): ")
        except (EOFError, OSError):
            ans = ""
        scope = "current" if ans.strip().lower().startswith("c") else "all"
    harness = args.harness or os.environ.get("SKILLGRAM_HARNESS")
    if scope == "current" and not harness:
        print(json.dumps({"ok": False, "error": "scope=current requires --harness or SKILLGRAM_HARNESS"}), flush=True)
        return 2
    plan = onboard(hub, scope=scope, harness=harness, use_llm=not args.no_llm)
    if args.plan:
        Path(args.plan).write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if not args.apply:
        return plan
    applied = apply_onboard(hub, plan)
    replaced = {"quarantine": [], "links": [], "failures": []}
    if args.replace_natives and applied["imported"]:
        replaced = replace_native_with_link(
            hub, applied["imported"], home_dir() / ".agents" / "skillgram-quarantine"
        )
    linked = sync(hub, quiet=True)
    return {
        **plan,
        "applied": applied,
        "reemplazo": replaced,
        "sync": linked,
        "ok": not applied["failed"] and not replaced["failures"],
    }


def dispatch(args, hub: Hub) -> dict | int:
    """Corre un comando y devuelve su resultado.

    Un int significa que el comando ya hizo su propia I/O (mcp, watch, daemon) y
    `main` no imprime nada mas. Hay UNA sola cadena de decision: el texto humano
    vive en `human_output` y solo lee el dict que sale de aca, asi que un comando
    no puede ejecutarse dos veces.
    """
    cmd = args.cmd

    if cmd == "status":
        return status(hub, deep=args.verify)

    if cmd == "flavor":
        return flavor_scan(hub, apply=args.apply)

    if cmd == "port":
        cfg = None if args.no_llm else llm_config()
        if not args.skill:
            targets = [args.to] if args.to else [h["app"] for h in detect_harnesses() if h["skills_dir"]]
            return port_missing(hub, targets, cfg, apply=args.apply)
        if not args.to:
            return {"ok": False, "op": "port", "error": "--to <harness> is required for a single skill"}
        return port_skill(hub, args.skill, args.to, cfg)

    if cmd == "precedence":
        return apply_precedence(hub, apply=args.apply,
                                only=normalize_harness(args.harness) if args.harness else None)

    if cmd == "list":
        proj = resolve_project(args.project) if args.project else None
        return list_skills(hub, category=args.category,
                           project=Path(proj["path"]) if proj else None,
                           used=args.used, limit=args.limit)

    if cmd == "touch":
        proj = resolve_project(args.project)
        result = touch_usage(Path(proj["path"]), args.skills, by=args.by)
        result["project"] = str(proj["path"])
        return result

    if cmd == "new":
        return new_skill(hub, args.name, category=args.category, author=args.by)

    if cmd == "lint":
        return lint_hub(hub, only=args.skill, strict=args.strict, contract=args.contract,
                        baseline=Path(args.baseline) if args.baseline else None,
                        write_baseline=Path(args.write_baseline) if args.write_baseline else None)

    if cmd == "diff":
        return diff_skill(hub, args.skill, Path(args.against))

    if cmd == "demand":
        return demand_report(limit=args.limit)

    if cmd == "git":
        return hub_git(hub, action=args.action, apply=args.apply)

    if cmd == "index":
        return index_build(hub)

    if cmd == "search":
        return index_search(" ".join(args.text), limit=args.limit)

    if cmd == "review":
        proj = resolve_project(args.project)
        return review_list(hub, Path(proj["path"]), days=args.days, fail_if_stale=args.fail_if_stale)

    if cmd == "pin":
        proj = resolve_project(args.project)
        return pin_skill(Path(proj["path"]), args.skills, pinned=not args.unpin)

    if cmd == "retire":
        result = retire_skill(hub, args.skill, reason=args.reason)
        if result.get("ok"):
            result["sync"] = sync(hub, quiet=True)
        return result

    if cmd == "export":
        return export_hub(hub, Path(args.file))

    if cmd == "import":
        return import_hub(hub, Path(args.file), apply=args.apply)

    if cmd == "mcp":
        return mcp_serve(hub, read_only=args.read_only)

    if cmd == "bootstrap":
        proj = resolve_project(args.project)
        cfg = None if args.no_llm else llm_config()
        result = bootstrap(hub, Path(proj["path"]), cfg, apply=args.apply)
        result["resolucion"] = proj["source"]
        return result

    if cmd == "missing":
        proj = resolve_project(args.project)
        result = missing(hub, " ".join(args.text))
        result["project"] = str(proj["path"])
        return result

    if cmd == "seed":
        return seed_skills(hub, args.skills, mode="never" if args.never else "always",
                           remove=args.remove)

    if cmd == "propose":
        proj = resolve_project(args.project)
        cfg = None if args.no_llm else llm_config()
        result = propose_skill(hub, Path(proj["path"]), " ".join(args.need), cfg, apply=args.apply)
        result["resolucion"] = proj["source"]
        return result

    if cmd == "mount":
        proj = resolve_project(args.project)
        root = Path(proj["path"])
        result = mount_project(hub, root, apply=args.apply, prune_global=args.prune_global)
        if not result.get("ok") and args.bootstrap is not False:
            booted = _mount_bootstrap(hub, root, args, result)
            if booted is not None:
                result = booted
        if result.get("ok") and args.apply and not args.no_protocol:
            result["protocol"] = inject_protocol(hub, root, apply=True)
        result["resolucion"] = proj["source"]
        return result

    if cmd == "adopt":
        if args.all:
            return adopt_natives(hub)
        if args.path:
            return adopt_skill(hub, Path(args.path))
        return {"ok": False, "op": "adopt", "error": "pass a folder or --all"}

    if cmd == "install":
        return install(hub, harness=args.harness, root=args.root, apply=not args.dry_run,
                       bootstrap_repos=args.bootstrap_repos, depth=args.depth)

    if cmd == "propagate":
        return propagate(hub, harness=args.harness, root=args.root, apply=args.apply,
                         depth=args.depth, force=args.force, bootstrap_repos=args.bootstrap_repos)

    if cmd == "doctor":
        return doctor(hub)

    if cmd == "onboard":
        return _onboard(hub, args)

    if cmd == "sync":
        return sync(hub, quiet=args.quiet, dry_run=args.dry_run)

    if cmd == "watch":
        if not hub.root.is_dir():
            print(json.dumps({"ok": False, "error": f"hub not found {hub.root}"}), flush=True)
            return 2
        if not single_instance():
            print(
                json.dumps({"ok": False, "error": "a watcher is already running (not starting another)"}),
                flush=True,
            )
            return 3
        watcher = Watcher(hub, interval=args.interval)
        # El sync inicial lo hace el propio vigilante (ver `_loop`), para no dejar sin
        # sincronizar lo que aparezca entre ese sync y la primera huella.
        watcher.start()
        try:
            while watcher.running:
                time.sleep(1)
        except KeyboardInterrupt:
            watcher.stop()
        return 0

    if cmd == "daemon":
        return daemon(hub, interval=args.interval, watch_at_start=args.watch)

    if cmd == "install-autostart":
        return install_autostart(hub, args.interval)

    if cmd == "uninstall-autostart":
        return uninstall_autostart()

    if cmd == "init":
        return init_project(args.project, args.path, force=args.force)

    if cmd == "use":
        proj = resolve_project(args.project)
        result = promote(hub, args.skills, Path(proj["path"]))
        # promoting a skill *is* using it: record who did it, so the next harness
        # reads a name instead of "agent"
        by = args.by or caller_identity()
        result["uso"] = touch_usage(Path(proj["path"]), args.skills, by=by).get("touched", [])
        result["by"] = by
        result["resolucion"] = proj["source"]
        return result

    if cmd == "unuse":
        proj = resolve_project(args.project)
        result = unpromote(Path(proj["path"]), args.skills)
        result["resolucion"] = proj["source"]
        return result

    if cmd == "project-status":
        proj = resolve_project(args.project)
        result = project_status(hub, Path(proj["path"]))
        result["resolucion"] = proj["source"]
        return result

    if cmd == "align":
        proj = resolve_project(args.project)
        result = align(hub, Path(proj["path"]), apply=args.apply, hashes=args.hashes)
        result["resolucion"] = proj["source"]
        return result

    if cmd == "policy":
        proj = resolve_project(args.project) if (args.project or args.action == "check") else None
        root = Path(proj["path"]) if proj else None
        if args.action == "check":
            result = policy_check(hub, root or Path.cwd(), org=args.policy)
        else:
            result = policy_show(root, org=args.policy)
        if proj:
            result["resolucion"] = proj["source"]
        return result

    if cmd == "clone":
        return clone_hub(args.url, Path(args.hub), ref=args.ref, force=args.force,
                         sync_after=not args.no_sync)

    return {"ok": False, "op": cmd, "error": f"unknown command: {cmd}"}  # argparse ya valida


def human_output(args, result: dict) -> str | None:
    """Reporte humano de los comandos que tienen uno.

    None = comando de maquina: una sola linea JSON (el contrato de AGENTS.md),
    nunca texto a medias. Todo lo que se lee aca tiene que existir en el dict que
    devuelve `dispatch`: es el punto donde un rename de clave rompe el CLI en
    silencio.
    """
    cmd = args.cmd

    if cmd == "doctor":
        installed = [h["app"] for h in result["harnesses"] if h["instalado"]]
        with_skills = [h["app"] for h in result["harnesses"] if h["skills_dir"]]
        return (
            f"hub: {result['hub']}\n"
            f"skills: {result['skills_hub']} in {result['categories']} categories\n"
            f"harnesses installed ({len(installed)}): {', '.join(installed) or '-'}\n"
            f"with a skills/ folder ({len(with_skills)}): {', '.join(with_skills) or '-'}\n"
            "harness                       links  dead  missing  native  collisions\n"
            + "\n".join(
                f"{r['app'][:28]:28}  {r['hub_links']:>7}  {r['dead']:>7}  "
                f"{r['missing']:>6}  {r['native']:>7}  {len(r['collisions']):>10}"
                for r in result["agents"]
            )
            + "\n"
            + ("\n".join(f"- {r}" for r in result["recommendations"]) if result["recommendations"] else "")
        )

    if cmd == "onboard":
        return (
            f"scope: {result['scope']}"
            + (f" ({result['harness']})" if result.get("harness") else " (all harnesses)")
            + f"  | hub: {result['hub']}\n"
            + "harnesses with native skills:\n"
            + ("\n".join(f"  {h['app']:16} {h['native']:>4} native" for h in result["harnesses"] if h["native"])
               or "  (none)")
            + f"\nplan: {result['summary']['import']} import | {result['summary']['dedupe']} duplicate | "
            f"{result['summary']['conflict']} conflict"
            + (f"\nllm: {result['llm']['model']} ({result['llm']['aplicadas']} skills classified)"
               if result.get("llm") else "\nllm: not used")
            + (f"\nllm_error: {result['llm_error']}" if result.get("llm_error") else "")
            + "\n"
            + "\n".join(
                f"  {a['action']:8} {a['harness']}/{a['skill']} -> {a['category']}"
                + (f" [{a.get('flavor')}]" if a.get("flavor") else "")
                + (f"  ({a['reason']})" if a.get("reason") else "")
                for a in result["actions"][:40]
            )
            + (f"\n  ... and {len(result['actions']) - 40} more" if len(result["actions"]) > 40 else "")
            + ("\n\napplied: " + str(len(result["applied"]["imported"])) + " imported, "
               + str(len(result["applied"]["skipped"])) + " skipped, "
               + str(len(result["applied"]["failed"])) + " failed"
               if result.get("applied") else "\n(plan only: run with --apply to execute it)")
        )

    if cmd == "status":
        return (
            f"hub: {result['hub']}\n"
            f"skills: {result['skills']} in {result['categories']} categories "
            f"({result['nested']} nested), agents: {result['agents']}\n"
            f"drift: {result['drift']} (missing {result['missing']}, mispointed {result['mispointed']}, "
            f"dead {result['dead']}, skipped {result['skipped']})"
        )

    if cmd == "install":
        return install_report(result)

    if cmd == "propagate":
        return propagate_report(result)

    if cmd == "init":
        if result.get("ok"):
            return f"project: {result['project_name']}  ({result['config']})"
        return result.get("error", "could not initialize")

    if cmd == "use":
        return (
            f"project: {result['project_name']} ({result['project']})\n"
            f"promoted: {len(result['created'])} new, {len(result['already_present'])} already present\n"
            + (f"unknown in hub: {', '.join(result['unknown'])}\n" if result["unknown"] else "")
            + (f"occupied without link: {', '.join(result['ocupadas_sin_enlace'])}\n"
               if result["ocupadas_sin_enlace"] else "")
            + f"manifest: {result['manifest']}"
        )

    if cmd == "unuse":
        lines = [
            f"project: {result['project']}",
            f"removed: {len(result['removed'])}",
        ]
        if result["absent"]:
            lines.append(f"not in scope: {', '.join(result['absent'])}")
        return "\n".join(lines)

    if cmd == "seed":
        lines = []
        for label, key in (("always", "always"), ("never", "never")):
            if result.get(key):
                lines.append(f"{label}: {', '.join(result[key])}")
        if not lines:
            lines.append("no seeds yet: `skillgram seed <skill>...` marks one as always recommended")
        if result.get("changed"):
            verb = "removed from" if result.get("removed") else "seeded in"
            lines.append(f"{verb} {result['mode']}: {', '.join(result['changed'])}")
        if result.get("unknown"):
            lines.append(f"not in the hub: {', '.join(result['unknown'])}")
        lines.append(f"file: {result['file']}")
        return "\n".join(lines)

    if cmd == "policy" and result.get("action") == "show":
        lines = [f"organization: {result['org'] or '(none: set SKILLGRAM_POLICY or --policy)'}"]
        if not result["levels"]:
            lines.append("no policy files found: everything is allowed (mode open by default)")
        for level in result["levels"]:
            lines.append(f"{level['level']:<8} {level['mode']:<7} {level['hash']}  {level['path']}")
            for key in ("allow", "deny", "require"):
                if level[key]:
                    lines.append(f"         {key}: {', '.join(level[key])}")
            if level["harnesses_reserved"]:
                lines.append(f"         harnesses (reserved, not enforced in 1.7): "
                             f"{', '.join(level['harnesses_reserved'])}")
        if result["require"]:
            lines.append(f"required anywhere: {', '.join(result['require'])}")
        for error in result["errors"]:
            lines.append(f"error: {error}")
        return "\n".join(lines)

    if cmd == "clone":
        lines = [
            f"hub: {result['hub']}  ({result.get('transport', 'clone')}, "
            f"{result.get('skills', 0)} skills in {result.get('categories', 0)} categories)",
            f"origin: {result.get('origin') or result['url']}"
            + (f"  ref: {result['ref']}" if result.get("ref") else "")
            + (f"  commit: {result['commit']}" if result.get("commit") else ""),
        ]
        if result.get("added"):
            lines.append(f"added (merge): {len(result['added'])}")
        if result.get("conflicts"):
            lines.append(f"kept yours (never overwritten): {', '.join(result['conflicts'])}")
        synced = result.get("synced") or {}
        if synced:
            lines.append(f"linked into the harnesses: +{synced.get('creados', 0)} created, "
                         f"~{synced.get('arreglados', 0)} fixed, -{synced.get('removed', 0)} removed")
        elif result.get("ok"):
            lines.append("not linked (--no-sync): run `skillgram sync` when you want them visible")
        if result.get("error"):
            lines.append(f"error: {result['error']}")
        return "\n".join(lines)

    if cmd == "policy" and result.get("action") == "check":
        lines = [
            f"project: {result['project']}  scope: {result['scope']}",
            "levels: " + (", ".join(f"{lv['level']}@{lv['hash']}" for lv in result["levels"]) or "(none)"),
        ]
        if result["violations"]:
            lines.append(f"violations: {len(result['violations'])}")
            for item in result["violations"]:
                lines.append(f"  {item['skill']}  ({item['category']})  <- {item['rule']}")
        else:
            lines.append("violations: none")
        if result["required_missing"]:
            lines.append(f"required but not in scope: {', '.join(result['required_missing'])}")
        for error in result["errors"]:
            lines.append(f"error: {error}")
        return "\n".join(lines)

    if cmd == "align":
        lines = [
            f"project: {result['project_name']} ({result['project']})  "
            f"skills in scope: {result['skills']}",
            f"{'harness':<16}{'mode':<13}{'project':>8}{'home':>7}{'missing':>9}{'extra':>7}{'native':>8}",
        ]
        for row in result["rows"]:
            lines.append(
                f"{row['harness'][:15]:<16}{row['mode']:<13}{row['via_project']:>8}{row['via_home']:>7}"
                f"{len(row['missing']):>9}{row['extra_count']:>7}{len(row['native']):>8}"
            )
        for row in result["rows"]:
            if row["missing"]:
                lines.append(f"{row['harness']}: missing {', '.join(row['missing'][:ALIGN_LIST_LIMIT])}"
                             + (f" (+{len(row['missing']) - ALIGN_LIST_LIMIT} more)"
                                if len(row["missing"]) > ALIGN_LIST_LIMIT else "")
                             + f" -- fix with `{row['fix']}`")
            if row["native"]:
                lines.append(f"{row['harness']}: native (not touched) {', '.join(row['native'][:ALIGN_LIST_LIMIT])}")
            if row["extra_count"]:
                if row["extra_count"] <= ALIGN_LIST_LIMIT:
                    lines.append(f"{row['harness']}: {row['extra_count']} extra (reported, never removed): "
                                 f"{', '.join(row['extra'])}")
                else:
                    lines.append(f"{row['harness']}: {row['extra_count']} extra -- the whole hub is linked here; "
                                 "`mount --prune-global` makes the project set the only source")
        if result["applied"]:
            lines.append(f"linked in: {', '.join(result['applied'])}")
        if result["unfixable"]:
            lines.append(f"still missing (no project dir to link into): {', '.join(result['unfixable'])}"
                         " -- `skillgram sync` exposes the whole hub instead")
        if result.get("hashes"):
            lines.append("")
            lines.append(f"{'skill':<28}{'hub':<18}{'manifest':<18}state")
            for row in result["hashes"]:
                state = "stale: the hub moved, `use` re-seals it" if row["stale"] else "ok"
                if not row["manifest"]:
                    state = "not recorded"
                lines.append(f"{row['skill'][:27]:<28}{row['hub']:<18}{row['manifest'] or '-':<18}{state}")
                for harness, seen in sorted(row["harnesses"].items()):
                    if seen["state"] == "link":
                        continue
                    if seen["state"] == "native":
                        lines.append(f"   {harness:<14} forked copy {seen['hash']} ({seen['where']})"
                                     " -- `diff` it, `adopt` lifts it into the hub")
                    elif seen["state"] == "link-elsewhere":
                        lines.append(f"   {harness:<14} links somewhere else ({seen['where']})")
                    else:
                        lines.append(f"   {harness:<14} absent")
        return "\n".join(lines)

    if cmd == "project-status":
        lines = [
            f"project: {result['project_name']} ({result['project']})  resolution: {result['resolucion']}",
            f"promoted: {result['promoted']} | healthy {len(result['healthy'])} | "
            f"stale {len(result['stale'])} | broken {len(result['broken'])} | "
            f"missing link {len(result['missing_links'])}",
        ]
        for label, items in (
            ("stale", result["stale"]),
            ("broken", result["broken"]),
            ("missing links", result["missing_links"]),
            ("unregistered links", result["links_sin_registrar"]),
            ("local duplicates (in the hub and also copied)", result["local_duplicates"]),
            ("local only", result["local_only"]),
        ):
            if items:
                lines.append(f"{label}: {', '.join(items)}")
        if not result["tiene_config"]:
            lines.append("no .agents/config.json (run `init` to pin the name)")
        if result.get("seeds_missing"):
            lines.append(f"seeds not in this project (recommended): {', '.join(result['seeds_missing'])}"
                         " -- `skillgram use <skill>` to add them")
        return "\n".join(lines)

    return None


def render(args, result: dict) -> None:
    """Un comando de maquina imprime una linea JSON; los humanos, su reporte.

    `--json` gana siempre: es la unica forma de que un humano (o un test) vea el
    dict crudo sin cambiar el flujo.
    """
    text = None if args.json else human_output(args, result)
    print(text if text is not None else json.dumps(result, ensure_ascii=False), flush=True)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = dispatch(args, Hub(Path(args.hub)))
    if isinstance(result, int):
        return result
    render(args, result)
    return 0 if result.get("ok", False) else 1
