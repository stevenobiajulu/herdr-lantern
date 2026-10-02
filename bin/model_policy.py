"""Lantern model policy: shipped defaults plus an optional user override.

The shipped file is model-policy.json at the plugin root. A user override at
$HERDR_PLUGIN_CONFIG_DIR/model-policy.json is laid over it. See
docs/model-policy.md for the schema and the merge rule.

Stdlib only, Python 3.8+. Every reader of a default model or a model ban goes
through this module so the routes, the preflight, the herdr wrapper, onboard,
and the launch appendix cannot disagree.
"""

from __future__ import annotations

import copy
import json
import os
import sys

from model_catalog import model_words

SCHEMA = 1
POLICY_FILE = "model-policy.json"
KINDS = ("claude", "codex", "cursor", "grok", "fugu")

# Onboarding answers and the onboard apply argv they map to. Grammar, not
# defaults: the policy filters out any example its forbid list refuses.
ONBOARD_EXAMPLES = (
    ("Cursor Grok 4.7 high fast", "cursor", "grok 4.7 high fast", ""),
    ("Cursor Grok 4.6 high fast", "cursor", "cursor grok 4.6 high fast", ""),
    ("Claude Opus high", "claude", "opus", "high"),
    ("Codex Astra high", "codex", "astra high", ""),
    ("Claude Fable high", "claude", "fable", "high"),
    ("Codex", "codex", "", ""),
    ("Grok Build", "grok", "", ""),
)


class PolicyError(ValueError):
    pass


def forbid_words(value: str) -> set[str]:
    """model_words for the forbid check only (catalog routing keeps the raw
    words). Dated ids carry a date component that model_words folds into the
    version: claude-opus-5-20261001 -> 5.20261001. Drop dot components of six
    or more digits so a ban on opus 5 still sees 5; claude-opus-5-5 stays 5.5."""
    words = set()
    for word in model_words(value.lower()):
        if word[:1].isdigit():
            parts = [part for part in word.split(".") if not (part.isdigit() and len(part) >= 6)]
            word = ".".join(parts)
            if not word:
                continue
        words.add(word)
    return words


def plugin_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def shipped_path() -> str:
    return os.path.join(plugin_root(), POLICY_FILE)


def override_path() -> str | None:
    config_dir = os.environ.get("HERDR_PLUGIN_CONFIG_DIR", "")
    if not config_dir:
        return None
    return os.path.join(config_dir, POLICY_FILE)


def _read(path: str, label: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as error:
        raise PolicyError(f"{label} model policy unreadable: {path} ({error})") from error
    except json.JSONDecodeError as error:
        raise PolicyError(f"{label} model policy is not valid JSON: {path} ({error})") from error
    if not isinstance(data, dict):
        raise PolicyError(f"{label} model policy is not a JSON object: {path}")
    if data.get("schema") != SCHEMA:
        raise PolicyError(
            f"{label} model policy has schema {data.get('schema')!r}; this Lantern reads schema {SCHEMA}: {path}"
        )
    return data


def _check_shape(policy: dict, label: str) -> None:
    def need(condition: bool, what: str) -> None:
        if not condition:
            raise PolicyError(f"{label} model policy: {what}")

    forbid = policy.get("forbid", {})
    need(isinstance(forbid, dict), "forbid must be an object")
    need(isinstance(forbid.get("fast", False), bool), "forbid.fast must be true or false")
    tiers = forbid.get("service_tiers", [])
    need(isinstance(tiers, list) and all(isinstance(t, str) for t in tiers),
         "forbid.service_tiers must be a list of strings")
    models = forbid.get("models", [])
    need(isinstance(models, list) and all(
        isinstance(group, list) and group and all(isinstance(t, str) and t for t in group)
        for group in models), "forbid.models must be a list of nonempty token lists")
    need(isinstance(forbid.get("reason", ""), str), "forbid.reason must be a string")
    routes = policy.get("routes", {})
    need(isinstance(routes, dict), "routes must be an object")
    for kind, value in routes.items():
        need(kind in KINDS, f"routes.{kind} is not a known kind ({', '.join(KINDS)})")
        need(isinstance(value, dict), f"routes.{kind} must be an object")
        _check_route(kind, value, need)
    for section in ("spawn", "helper"):
        value = policy.get(section, {})
        need(isinstance(value, dict) and all(isinstance(v, str) for v in value.values()),
             f"{section} must be an object of strings")
    notes = policy.get("notes", [])
    need(isinstance(notes, list) and all(isinstance(n, str) for n in notes), "notes must be a list of strings")


ROUTE_KEYS = {
    "claude": {"default"},
    "codex": {"default"},
    "cursor": {"default", "helper_model"},
    "grok": {"default"},
    "fugu": set(),
}


def _is_str_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item for item in value)


def _effort_ok(value: object) -> bool:
    return value is None or isinstance(value, str)


def _check_route(kind: str, value: dict, need) -> None:
    """Every nested field has its type, so a bad override stops every reader."""
    unknown = sorted(set(value) - ROUTE_KEYS[kind])
    need(not unknown, f"routes.{kind} has unknown keys: {', '.join(unknown)}")
    if "helper_model" in value:
        need(isinstance(value["helper_model"], str), f"routes.{kind}.helper_model must be a string")
    if "default" not in value:
        return
    default = value["default"]
    where = f"routes.{kind}.default"
    need(isinstance(default, dict), f"{where} must be an object")
    allowed = {"claude": {"model", "effort"}, "codex": {"model", "effort"},
               "cursor": {"prefer", "fallback", "effort"}, "grok": {"prefer"}}[kind]
    unknown = sorted(set(default) - allowed)
    need(not unknown, f"{where} has unknown keys: {', '.join(unknown)}")
    if kind in ("claude", "codex"):
        need(isinstance(default.get("model"), str) and default.get("model"), f"{where}.model must be a nonempty string")
        need(_effort_ok(default.get("effort")), f"{where}.effort must be a string or null")
    elif kind == "cursor":
        need(_is_str_list(default.get("prefer", [])), f"{where}.prefer must be a list of model ids")
        need(_effort_ok(default.get("effort")), f"{where}.effort must be a string or null")
        fallback = default.get("fallback", {})
        need(isinstance(fallback, dict) and not (set(fallback) - {"require", "exclude"}),
             f"{where}.fallback must be an object with require and exclude")
        for field in ("require", "exclude"):
            need(_is_str_list(fallback.get(field, [])), f"{where}.fallback.{field} must be a list of strings")
    elif kind == "grok":
        prefer = default.get("prefer")
        need(isinstance(prefer, list) and all(
            isinstance(pair, list) and len(pair) == 2 and all(isinstance(item, str) and item for item in pair)
            for pair in prefer), f"{where}.prefer must be a list of [model id, effort] pairs")


def merge(base: dict, over: dict) -> dict:
    merged = copy.deepcopy(base)
    for key in ("forbid", "spawn", "helper"):
        if key in over:
            merged.setdefault(key, {}).update(copy.deepcopy(over[key]))
    for kind, value in over.get("routes", {}).items():
        merged.setdefault("routes", {}).setdefault(kind, {}).update(copy.deepcopy(value))
    merged["notes"] = list(base.get("notes", [])) + list(over.get("notes", []))
    if "source" in over:
        merged["source"] = over["source"]
    merged["override"] = True
    return merged


def load(shipped: str | None = None, override: str | None = None, *, use_env: bool = True) -> dict:
    """Effective policy. A broken override fails loudly; it never falls back."""
    shipped = shipped or shipped_path()
    policy = _read(shipped, "shipped")
    _check_shape(policy, "shipped")
    policy["override"] = False
    if override is None and use_env:
        override = override_path()
    if override and os.path.exists(override):
        user = _read(override, "override")
        _check_shape(user, "override")
        policy = merge(policy, user)
        policy["override_path"] = override
    policy.setdefault("forbid", {})
    policy.setdefault("routes", {})
    for kind in KINDS:
        policy["routes"].setdefault(kind, {})
    policy.setdefault("spawn", {})
    policy.setdefault("helper", {})
    policy.setdefault("notes", [])
    return policy


def route_default(policy: dict, kind: str) -> dict:
    value = policy.get("routes", {}).get(kind, {}).get("default")
    if not isinstance(value, dict):
        raise PolicyError(f"model policy has no routes.{kind}.default")
    return value


def _forbid(policy: dict) -> dict:
    return policy.get("forbid", {})


def _reason(policy: dict) -> str:
    reason = _forbid(policy).get("reason", "")
    source = "user model policy" if policy.get("override") else "model policy"
    return f"{source}: {reason}" if reason else source


def forbidden_model(policy: dict, model_id: str) -> str | None:
    """Why this model id is refused, or None. Matches token sets, so
    claude-opus-5-5 is not caught by a ban on opus 5."""
    tokens = forbid_words(model_id)
    for group in _forbid(policy).get("models", []):
        wanted = forbid_words(" ".join(group))
        if wanted and wanted <= tokens:
            return f"{model_id} is forbidden by the {_reason(policy)} (matches {' '.join(group)})"
    if _forbid(policy).get("fast") and "fast" in tokens:
        return f"{model_id} is a fast model; fast routes are forbidden by the {_reason(policy)}"
    return None


def forbidden_tier(policy: dict, tier: str, name: str = "") -> str | None:
    tier = tier.strip().strip("\"'")
    if not tier:
        return None
    banned = {value.lower() for value in _forbid(policy).get("service_tiers", [])}
    if tier.lower() in banned or (name and name.lower() in banned):
        return f"service tier {tier} is forbidden by the {_reason(policy)}"
    if _forbid(policy).get("fast") and tier.lower() != "default":
        return f"service tier {tier} is not the normal tier; fast routes are forbidden by the {_reason(policy)}"
    return None


def forbidden_phrase(policy: dict, phrase: str) -> str | None:
    """Static check of a spoken phrase, before any catalog read."""
    tokens = forbid_words(phrase)
    if _forbid(policy).get("fast") and "fast" in tokens:
        return f'"{phrase}" asks for fast; fast routes are forbidden by the {_reason(policy)}'
    for group in _forbid(policy).get("models", []):
        wanted = forbid_words(" ".join(group))
        if wanted and wanted <= tokens:
            return f'"{phrase}" names a model forbidden by the {_reason(policy)} (matches {" ".join(group)})'
    return None


def check_route(policy: dict, route: dict) -> None:
    """Raise PolicyError when a resolved route breaks the forbid list."""
    model = str(route.get("model") or "")
    why = forbidden_model(policy, model) if model else None
    if why is None and route.get("fast") and _forbid(policy).get("fast"):
        why = f"{model} was requested fast; fast routes are forbidden by the {_reason(policy)}"
    if why is None and route.get("service_tier"):
        why = forbidden_tier(policy, str(route["service_tier"]))
    if why is None and isinstance(route.get("argv"), list):
        # The argv is what launches; an alias in "model" can hide the id.
        problems = argv_violations(policy, str(route.get("kind", "")), [str(a) for a in route["argv"]])
        why = problems[0] if problems else None
    if why:
        raise PolicyError(why)


def _config_value(value: str, key: str) -> str | None:
    if "=" not in value:
        return None
    name, _, setting = value.partition("=")
    if name.strip() != key:
        return None
    return setting.strip().strip("\"'")


def argv_violations(policy: dict, kind: str, argv: list[str]) -> list[str]:
    """Policy problems in an agent argv (the part after herdr's --)."""
    models: list[str] = []
    tiers: list[str] = []

    def config(setting: str) -> None:
        for key, bucket in (("model", models), ("service_tier", tiers)):
            value = _config_value(setting, key)
            if value is not None:
                bucket.append(value)

    # Every spelling a CLI parser accepts: -m X, -mX, --model X, --model=X,
    # and the same four for -c/--config.
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            # The agent CLI's own end of options: what follows is prompt
            # text (codex accepts -- before a prompt), never a setting.
            break
        following = argv[index + 1] if index + 1 < len(argv) else ""
        if arg in ("-m", "--model"):
            models.append(following.strip("\"'"))
            index += 2
            continue
        if arg in ("-c", "--config"):
            config(following)
            index += 2
            continue
        if arg.startswith("--model="):
            models.append(arg.split("=", 1)[1].strip("\"'"))
        elif arg.startswith("--config="):
            config(arg.split("=", 1)[1])
        elif arg.startswith("-m") and not arg.startswith("--"):
            models.append(arg[2:].strip("\"'"))
        elif arg.startswith("-c") and not arg.startswith("--"):
            config(arg[2:])
        index += 1
    problems = []
    for model in models:
        why = forbidden_model(policy, model) if model else None
        if why:
            problems.append(why)
    for tier in tiers:
        why = forbidden_tier(policy, tier)
        if why:
            problems.append(why)
    return problems


def onboard_examples(policy: dict) -> list[tuple[str, str]]:
    """(spoken answer, onboard apply args) pairs the policy allows."""
    rows = []
    for spoken, kind, phrase, effort in ONBOARD_EXAMPLES:
        if phrase and forbidden_phrase(policy, phrase):
            continue
        args = f"--kind {kind}"
        if phrase:
            args += f' --model "{phrase}"' if " " in phrase else f" --model {phrase}"
        if effort:
            args += f" --effort {effort}"
        if not phrase:
            args += "` with no `--model"
        rows.append((spoken, args))
    return rows


def _default_words(policy: dict) -> dict:
    routes = policy["routes"]
    words = {}
    claude = routes["claude"].get("default", {})
    words["claude"] = (
        f"`model-route claude default` resolves `{claude.get('model', '')}`"
        + (f" at effort {claude['effort']}" if claude.get("effort") else "")
        + " through the live initialization catalog. Pass the resolved id, never the bare alias."
    )
    codex = routes["codex"].get("default", {})
    words["codex"] = (
        f"`model-route codex default` resolves `{codex.get('model', '')}` at "
        + (f"effort {codex['effort']}" if codex.get("effort") else "the catalog default effort")
        + ', with the explicit normal service tier `service_tier="default"`.'
    )
    cursor = routes["cursor"].get("default", {})
    fallback = cursor.get("fallback", {})
    prefer = ", ".join(f"`{name}`" for name in cursor.get("prefer", [])) or "nothing"
    words["cursor"] = (
        f"`model-route cursor default` prefers {prefer}; otherwise the first live id with "
        f"{', '.join(fallback.get('require', [])) or 'any'} and none of "
        f"{', '.join(fallback.get('exclude', [])) or 'nothing'}. A Cursor helper chat with an empty "
        f"HELPER_MODEL runs `{routes['cursor'].get('helper_model', '')}`."
    )
    grok = routes["grok"].get("default", {})
    words["grok"] = "`model-route grok default` prefers " + (", then ".join(
        f"`{name}` at {effort}" for name, effort in grok.get("prefer", [])) or "nothing") + "."
    return words


def render_prompt(policy: dict) -> str:
    forbid = _forbid(policy)
    lines = []
    origin = "the shipped model-policy.json"
    if policy.get("override"):
        origin += f" with the user override at {policy.get('override_path', 'the config directory')}"
    lines.append(f"Model policy (from {origin}; do not ignore):")
    words = _default_words(policy)
    lines.append(f"- Claude default: {words['claude']}")
    lines.append(f"- Codex default: {words['codex']}")
    lines.append(f"- Cursor default: {words['cursor']}")
    lines.append(f"- Grok Build default: {words['grok']}")
    spawn = policy.get("spawn", {})
    if spawn.get("kind"):
        lines.append(
            f"- With an empty helper.conf spawn default, the spawn default is kind {spawn['kind']}"
            + (f", model phrase `{spawn['model']}`" if spawn.get("model") else ", that kind's live default")
            + (f", effort {spawn['effort']}" if spawn.get("effort") else "")
            + "."
        )
    bans = []
    if forbid.get("fast"):
        bans.append("every fast route (a phrase with fast, a model id with fast, or any non-default service tier), even when the user asks for it")
    if forbid.get("service_tiers"):
        bans.append("service tiers " + ", ".join(f"`{t}`" for t in forbid["service_tiers"]))
    if forbid.get("models"):
        bans.append("models matching " + "; ".join(" ".join(group) for group in forbid["models"]))
    if bans:
        lines.append("- A default candidate the forbid list refuses is skipped, never launched; when none is left the default route fails.")
        lines.append("- Forbidden: " + "; ".join(bans) + ". The resolver, preflight, herdr wrapper, and onboard refuse these. Do not look for another way to launch them; report the refusal.")
        if forbid.get("reason"):
            lines.append(f"  Reason: {forbid['reason']}")
    else:
        lines.append("- Forbidden: nothing. Fast is off unless requested and the live catalog publishes one Fast tier ID.")
    lines.append(
        "- An explicit user model phrase resolves through `$HERDR_PLUGIN_ROOT/bin/model-route <kind> \"<phrase>\"` "
        "against the live catalog. A model missing from this policy is not a reason to refuse; only the forbid list refuses."
    )
    examples = onboard_examples(policy)
    if examples:
        lines.append("- Onboarding answers map to `$HERDR_PLUGIN_ROOT/bin/onboard apply`: " + "; ".join(
            f"{spoken} → `{args}`" for spoken, args in examples) + "; keep the current default → `onboard apply --keep`.")
    for note in policy.get("notes", []):
        lines.append(f"- {note}")
    return "\n".join(lines)


SHELL_KEYS = (
    ("HELPER_POLICY_SPAWN_KIND", ("spawn", "kind")),
    ("HELPER_POLICY_SPAWN_MODEL", ("spawn", "model")),
    ("HELPER_POLICY_SPAWN_EFFORT", ("spawn", "effort")),
    ("HELPER_POLICY_AGENT", ("helper", "agent")),
    ("HELPER_POLICY_MODEL", ("helper", "model")),
    ("HELPER_POLICY_EFFORT", ("helper", "effort")),
)


def shell_defaults(policy: dict) -> str:
    """KEY=value lines for lib.sh. Values are checked again in the shell."""
    out = []
    for key, (section, field) in SHELL_KEYS:
        out.append(f"{key}={policy.get(section, {}).get(field, '')}")
    out.append(f"HELPER_POLICY_CURSOR_HELPER_MODEL={policy['routes']['cursor'].get('helper_model', '')}")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    """model_policy.py <shell|policy|prompt|check-phrase KIND PHRASE|check-argv KIND -- ARGV...>"""
    if not argv:
        print(main.__doc__, file=sys.stderr)
        return 2
    try:
        policy = load()
        command = argv[0]
        if command == "shell":
            print(shell_defaults(policy))
            return 0
        if command == "policy":
            print(json.dumps(policy, indent=2, sort_keys=True))
            return 0
        if command == "prompt":
            print(render_prompt(policy))
            return 0
        if command == "check-phrase" and len(argv) == 3:
            why = forbidden_phrase(policy, argv[2])
            if why:
                print(f"model policy: {why}", file=sys.stderr)
                return 2
            return 0
        if command == "check-argv" and len(argv) >= 2:
            rest = argv[2:]
            if rest and rest[0] == "--":
                rest = rest[1:]
            problems = argv_violations(policy, argv[1], rest)
            for problem in problems:
                print(f"model policy: {problem}", file=sys.stderr)
            return 2 if problems else 0
    except PolicyError as error:
        print(f"model policy: {error}", file=sys.stderr)
        return 2
    print(main.__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
