"""Model routing tests. Catalog fixtures never launch a model session."""

import importlib.util
import io
import json
import os
import shutil
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))
# A developer override must not change what these tests see.
os.environ.pop("HERDR_PLUGIN_CONFIG_DIR", None)


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, BIN / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


route = load("model_route", "model-route.py")
policy = load("model_policy", "model_policy.py")
preflight = load("model_preflight", "model-preflight.py")


def codex_catalog(fast=False):
    return json.dumps({"models": [
        {"slug": "gpt-6-astra", "display_name": "GPT-6-Astra", "visibility": "list",
         "default_reasoning_level": "medium",
         "supported_reasoning_levels": [{"effort": level} for level in
                                        ("low", "medium", "high", "xhigh", "max", "ultra")],
         "service_tiers": [{"id": "live-fast", "name": "Fast"}] if fast else [],
         "additional_speed_tiers": ["fast"] if fast else []},
        {"slug": "gpt-5.5", "display_name": "GPT-5.5", "visibility": "list",
         "default_reasoning_level": "medium",
         "supported_reasoning_levels": [{"effort": "medium"}]},
    ]})


CLAUDE_HELP = """
  --effort <level>  Effort level (low, medium, high, xhigh, max)
  --model <model>   Use 'fable', 'opus', or 'sonnet', or 'claude-fable-5-1'.
  -n, --name <name> A name such as 'not-a-model'
"""
CLAUDE_CATALOG = (Path(__file__).parent / "fixtures/claude-models.json").read_text()


def claude_catalog_with_badged_opus():
    # Matches the live initialization catalog: Opus's value/resolvedModel now
    # carry a terminal-style "[1m]" context badge and there is no bare
    # "opus"/"claude-opus-5" row at all.
    catalog = json.loads(CLAUDE_CATALOG)
    for row in catalog["response"]["response"]["models"]:
        if row["displayName"] == "Opus":
            row["value"] = "opus[1m]"
            row["resolvedModel"] = "claude-opus-5[1m]"
            row["displayName"] = "Opus (1M context)"
    return json.dumps(catalog)


def claude_read(command, *, input_text=None):
    if command == ["claude", "--help"]:
        return CLAUDE_HELP.replace("claude-fable-5-1", "claude-fable-5")
    return CLAUDE_CATALOG


CURSOR = """Available models
claude-fable-5-high - Claude Fable 5 1M
claude-fable-5-1-high - Claude Fable 5.1 1M
claude-fable-5-1-thinking-high - Claude Fable 5.1 1M Thinking
claude-fable-5-1-max - Claude Fable 5.1 1M Max
"""


class Models(unittest.TestCase):
    def test_astra_spoken_phrases_and_default(self):
        with patch.object(route, "run_catalog", return_value=codex_catalog()) as read:
            for phrase in ("astra", "gpt-6 astra", "gpt-6-astra", "default"):
                result = route.codex_route(phrase)
                self.assertEqual(result["model"], "gpt-6-astra")
                self.assertEqual(result["effort"], "medium")
                self.assertFalse(result["fast"])
                self.assertEqual(result["service_tier"], "default")
                self.assertEqual(result["argv"], ["-m", "gpt-6-astra", "-c", 'model_reasoning_effort="medium"', "-c", 'service_tier="default"'])
            read.assert_called_with(["codex", "debug", "models"])

    def test_astra_all_efforts(self):
        with patch.object(route, "run_catalog", return_value=codex_catalog()):
            for effort in ("low", "medium", "high", "xhigh", "max", "ultra"):
                self.assertEqual(route.codex_route(f"astra {effort}")["effort"], effort)
            for phrase in ("astra minimal", "astra none", "astra low high"):
                with self.assertRaises(route.RouteError):
                    route.codex_route(phrase)

    def test_gpt6_sol_and_luna_need_a_generation(self):
        catalog = json.dumps({"models": [
            {"slug": "gpt-6-sol", "display_name": "GPT-6-Sol", "visibility": "list",
             "default_reasoning_level": "medium",
             "supported_reasoning_levels": [{"effort": "high"}, {"effort": "medium"}]},
            {"slug": "gpt-6-luna", "display_name": "GPT-6-Luna", "visibility": "list",
             "default_reasoning_level": "medium",
             "supported_reasoning_levels": [{"effort": "medium"}, {"effort": "high"}, {"effort": "max"}]},
            {"slug": "gpt-5.6-sol", "display_name": "GPT-5.6-Sol", "visibility": "list",
             "default_reasoning_level": "low",
             "supported_reasoning_levels": [{"effort": "high"}, {"effort": "low"}]},
            {"slug": "gpt-5.6-luna", "display_name": "GPT-5.6-Luna", "visibility": "list",
             "default_reasoning_level": "medium",
             "supported_reasoning_levels": [{"effort": "high"}]},
        ]})
        with patch.object(route, "run_catalog", return_value=catalog):
            self.assertEqual(route.codex_route("gpt-6 sol high")["model"], "gpt-6-sol")
            self.assertEqual(route.codex_route("6 sol")["effort"], "medium")
            self.assertEqual(route.codex_route("gpt-6 luna")["model"], "gpt-6-luna")
            with self.assertRaisesRegex(route.RouteError, "ultra"):
                route.codex_route("gpt-6 luna ultra")
            for phrase in ("sol", "sol high", "luna"):
                with self.assertRaisesRegex(route.RouteError, "ambiguous"):
                    route.codex_route(phrase)

    def test_cursor_current_openai_and_grok_ids(self):
        catalog = """Available models
grok-4.7-high-fast - Grok 4.7 High Fast
cursor-grok-4.6-high-fast - Cursor Grok 4.6 Fast
gpt-5.3-codex-high-fast - Codex 5.3 High Fast
claude-opus-5-5-high-fast - Claude Opus 5.5 1M High Fast
"""
        with patch.object(route, "run_catalog", return_value=catalog):
            self.assertEqual(route.cursor_route("grok 4.7 high fast")["model"], "grok-4.7-high-fast")
            self.assertEqual(route.cursor_route("codex 5.3 high fast")["model"], "gpt-5.3-codex-high-fast")
            self.assertEqual(route.cursor_route("opus 5.5 high fast")["model"], "claude-opus-5-5-high-fast")
            with self.assertRaises(route.RouteError):
                route.cursor_route("cursor grok 4.7 high fast")

    def test_grok_default_prefers_47_over_build_fast(self):
        catalog = """Available models:
  - grok-4.7
  * grok-4.7-build-fast (default)
  - grok-4.6
"""
        with patch.object(route, "run_catalog", return_value=catalog):
            result = route.grok_route("default")
            self.assertEqual(result["argv"], ["-m", "grok-4.7-build-fast", "--reasoning-effort", "medium"])
            self.assertEqual(route.grok_route("grok 4.7 build fast")["model"], "grok-4.7-build-fast")

    def test_badged_opus_pins_resolved_model(self):
        catalog = json.dumps({"type": "control_response", "response": {
            "subtype": "success", "request_id": "lantern-model-catalog", "response": {"models": [
                {"value": "opus[1m]", "resolvedModel": "claude-opus-5-5[1m]",
                 "displayName": "Opus (1M context)",
                 "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"]},
                {"value": "claude-fable-5-1[1m]", "resolvedModel": "claude-fable-5-1",
                 "displayName": "Fable",
                 "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"]},
            ]}}})

        def read(command, *, input_text=None):
            if command == ["claude", "--help"]:
                return CLAUDE_HELP
            return catalog

        with patch.object(route, "run_catalog", side_effect=read):
            result = route.claude_route("opus high")
            self.assertEqual(result["argv"], ["--model", "claude-opus-5-5[1m]", "--effort", "high"])
            self.assertEqual(route.claude_route("fable high")["model"], "claude-fable-5-1")

    def test_fugu_routes_current_catalog(self):
        catalog = json.dumps({"models": [
            {"slug": "fugu-max", "display_name": "Fugu Max", "visibility": "list",
             "supported_reasoning_levels": [{"effort": "high"}, {"effort": "xhigh"}]},
            {"slug": "fugu-ultra-v2.0", "display_name": "Fugu Ultra v2.0", "visibility": "list",
             "supported_reasoning_levels": [{"effort": "high"}, {"effort": "xhigh"}]},
            {"slug": "fugu-ultra", "display_name": "Fugu Ultra", "visibility": "list",
             "supported_reasoning_levels": [{"effort": "high"}, {"effort": "xhigh"}]},
            {"slug": "fugu", "display_name": "Fugu", "visibility": "list",
             "supported_reasoning_levels": [{"effort": "high"}, {"effort": "xhigh"}]},
            {"slug": "fugu-ultra-v1.1", "display_name": "Fugu Ultra v1.1", "visibility": "list",
             "supported_reasoning_levels": [{"effort": "high"}, {"effort": "xhigh"}, {"effort": "max"}]},
            {"slug": "fugu-ultra-v1.0", "display_name": "Fugu Ultra v1.0", "visibility": "list",
             "supported_reasoning_levels": [{"effort": "high"}, {"effort": "xhigh"}]},
        ]})
        with tempfile.TemporaryDirectory() as home:
            path = os.path.join(home, "fugu.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(catalog)
            with patch.dict(os.environ, {"CODEX_HOME": home}):
                default = route.fugu_route("default")
                self.assertEqual(default["model"], "fugu")
                self.assertEqual(default["effort"], "high")
                self.assertEqual(route.fugu_route("fugu")["model"], "fugu")
                self.assertEqual(route.fugu_route("fugu deep")["model"], "fugu")
                self.assertEqual(route.fugu_route("fugu deep")["effort"], "xhigh")
                self.assertEqual(route.fugu_route("fugu ultra deep")["effort"], "xhigh")
                with self.assertRaises(route.RouteError):
                    route.fugu_route("fugu deep high")
                self.assertEqual(route.fugu_route("fugu ultra")["model"], "fugu-ultra-v2.0")
                self.assertEqual(route.fugu_route("fugu max")["model"], "fugu-max")
                self.assertEqual(route.fugu_route("ultra max")["model"], "fugu-ultra-v1.1")
                self.assertEqual(route.fugu_route("fugu ultra max")["model"], "fugu-ultra-v1.1")
                self.assertEqual(route.fugu_route("fugu-ultra-v1.1 max")["effort"], "max")
                with self.assertRaises(route.RouteError):
                    route.fugu_route("fugu-ultra-v1.0")

    def test_bare_generation_requires_choice(self):
        with patch.object(route, "run_catalog", return_value=codex_catalog()):
            for phrase in ("gpt-6", "gpt 6 high", "codex gpt-6"):
                with self.assertRaisesRegex(route.RouteError, "ambiguous"):
                    route.codex_route(phrase)
            self.assertEqual(route.codex_route("gpt-5.5")["model"], "gpt-5.5")
            with self.assertRaises(route.RouteError):
                route.codex_route("gpt-7 astra")

    def test_malformed_catalogs_fail_with_route_errors(self):
        for catalog in ('[]', '{}', '{"models":[null]}', '{"models":"invalid"}',
                        '{"models":[{"slug":"gpt-6-astra","visibility":"list","supported_reasoning_levels":null}]}'):
            with patch.object(route, "run_catalog", return_value=catalog):
                with self.assertRaises(route.RouteError):
                    route.codex_route("astra")
            with patch.object(preflight, "run", return_value=catalog):
                with self.assertRaises(preflight.CheckError):
                    self.check("codex", "gpt-6-astra", "high")

    def test_no_astra_fallback(self):
        with patch.object(route, "run_catalog", return_value='{"models":[]}'):
            with self.assertRaises(route.RouteError):
                route.codex_route("default")

    def test_fast_requires_live_tier_and_explicit_request(self):
        with patch.object(route, "run_catalog", return_value=codex_catalog()):
            with self.assertRaisesRegex(route.RouteError, "does not support fast"):
                route.codex_route("astra high fast")
        with patch.object(route, "run_catalog", return_value=codex_catalog(fast=True)):
            self.assertFalse(route.codex_route("astra")["fast"])
            result = route.codex_route("astra high fast")
            self.assertEqual(result["service_tier"], "live-fast")
            self.assertIn('service_tier="live-fast"', result["argv"])

    def test_normal_route_overrides_inherited_priority(self):
        with patch.object(route, "run_catalog", return_value=codex_catalog(fast=True)):
            result = route.codex_route("default")
        self.assertIn('service_tier="default"', result["argv"])
        self.assertNotIn('service_tier="priority"', result["argv"])
        self.assertEqual(result["service_tier"], "default")

    def test_cursor_fable_versions_and_exact_ids(self):
        with patch.object(route, "run_catalog", return_value=CURSOR):
            for phrase, model in (
                ("fable 5 high", "claude-fable-5-high"),
                ("fable 5.1 high", "claude-fable-5-1-high"),
                ("claude-fable-5-1-high", "claude-fable-5-1-high"),
                ("fable 5.1 thinking high", "claude-fable-5-1-thinking-high"),
                ("fable 5.1 max", "claude-fable-5-1-max"),
            ):
                self.assertEqual(route.cursor_route(phrase)["model"], model)
            for phrase in ("astra", "gpt-6 astra", "fable 5.1 fast"):
                with self.assertRaises(route.RouteError):
                    route.cursor_route(phrase)

    def test_cursor_only_accepts_astra_if_listed(self):
        with patch.object(route, "run_catalog", return_value=CURSOR + "gpt-6-astra - GPT-6 Astra\n"):
            self.assertEqual(route.cursor_route("astra")["model"], "gpt-6-astra")

    def test_claude_fable_alias_and_full_version(self):
        with patch.object(route, "run_catalog", side_effect=claude_read):
            for phrase, model in (("fable high", "claude-fable-5-1"),
                                  ("fable 5.1 high", "claude-fable-5-1"),
                                  ("claude-fable-5-1 high", "claude-fable-5-1")):
                self.assertEqual(route.claude_route(phrase)["argv"], ["--model", model, "--effort", "high"])
            for phrase in ("fable 5", "fable ultra", "fable fast", "not-a-model"):
                with self.assertRaises(route.RouteError):
                    route.claude_route(phrase)
        # Help still says 5. The initialization response, not its examples,
        # controls both aliases and full IDs.
        with patch.object(route, "run_catalog", side_effect=claude_read) as read:
            self.assertEqual(route.claude_route("fable")["model"], "claude-fable-5-1")
            request = json.loads(read.call_args.kwargs["input_text"])
            self.assertEqual(request["type"], "control_request")
            self.assertEqual(request["request"], {"subtype": "initialize"})
            self.assertIn("--safe-mode", read.call_args.args[0])
            self.assertIn("--no-session-persistence", read.call_args.args[0])

    def test_claude_unavailable_catalog_never_uses_help_as_allowlist(self):
        for output in ('{}', 'invalid', CLAUDE_CATALOG.replace('lantern-model-catalog', 'wrong-id'),
                       CLAUDE_CATALOG.replace('"success"', '"error"')):
            with patch.object(route, "run_catalog", side_effect=[CLAUDE_HELP, output]):
                with self.assertRaises(route.RouteError):
                    route.claude_route("fable")

    def test_claude_model_specific_efforts(self):
        with patch.object(route, "run_catalog", side_effect=claude_read):
            with self.assertRaises(route.RouteError):
                route.claude_route("sonnet max")

    def test_claude_catalog_normalizes_badged_opus_identity(self):
        badged_catalog = claude_catalog_with_badged_opus()

        def badged_read(command, *, input_text=None):
            if command == ["claude", "--help"]:
                return CLAUDE_HELP
            return badged_catalog

        with patch.object(route, "run_catalog", side_effect=badged_read):
            # The catalog only lists "opus[1m]" / "claude-opus-5[1m]", but the
            # bare phrase and exact bare model id must still resolve to it.
            for phrase in ("opus high", "claude-opus-5 high"):
                result = route.claude_route(phrase)
                self.assertEqual(result["model"], "claude-opus-5[1m]")
                self.assertEqual(result["argv"], ["--model", "claude-opus-5[1m]", "--effort", "high"])

        def badged_run(command, *, input_text=None):
            if command == ["claude", "/usage", "-p", "--output-format", "json"]:
                return json.dumps({"result": "Current session: 0% used"})
            return badged_read(command, input_text=input_text)

        with patch.object(preflight, "run", side_effect=badged_run):
            code, result = self.check("claude", "claude-opus-5", "high")
            self.assertEqual(code, 0)
            self.assertTrue(result["available"])
            code, result = self.check("claude", "opus", "high")
            self.assertEqual(code, 0)
            self.assertTrue(result["available"])


    def check(self, kind, model, effort):
        output = io.StringIO()
        with redirect_stdout(output):
            code = preflight.check(kind, model, effort)
        return code, json.loads(output.getvalue())

    def test_codex_preflight_effort(self):
        with patch.object(preflight, "run", return_value=codex_catalog()):
            self.assertEqual(self.check("codex", "gpt-6-astra", "ultra")[0], 0)
            self.assertEqual(self.check("codex", "gpt-6-astra", "none")[0], 3)
            self.assertEqual(self.check("codex", "gpt-6-invented", "high")[0], 3)

    def test_fable_51_quota_and_substitute(self):
        def run(command, *, input_text=None):
            if command != ["claude", "/usage", "-p", "--output-format", "json"]:
                return claude_read(command, input_text=input_text)
            return json.dumps({"result": "Current session: 0% used\nCurrent week (Fable 5.1): 100% used"})
        with patch.object(preflight, "run", side_effect=run):
            for model in ("fable", "claude-fable-5-1"):
                code, result = self.check("claude", model, "high")
                self.assertEqual(code, 3)
                self.assertIn("Fable 5.1", result["reason"])
                self.assertEqual(result["substitute"]["model"], "opus")
            self.assertEqual(self.check("claude", "opus", "high")[0], 0)

    def test_fable_51_available_and_unreadable_usage(self):
        for usage in ("Current session: 0% used", "unparseable"):
            with patch.object(preflight, "run", side_effect=[CLAUDE_HELP, CLAUDE_CATALOG, json.dumps({"result": usage})]):
                if usage == "unparseable":
                    with self.assertRaises(preflight.CheckError):
                        self.check("claude", "claude-fable-5-1", "high")
                else:
                    self.assertEqual(self.check("claude", "claude-fable-5-1", "high")[0], 0)


STRICT_OVERRIDE = {
    "schema": 1,
    "source": "test override",
    "forbid": {
        "fast": True,
        "service_tiers": ["fast", "priority"],
        "models": [["astra"], ["fable"], ["opus", "4.6"], ["opus", "5"], ["opus", "5.1"]],
        "reason": "cost",
    },
    "routes": {
        "codex": {"default": {"model": "gpt-6.1-sol", "effort": "low"}},
        "cursor": {"default": {"prefer": ["claude-opus-5-5-high"],
                               "fallback": {"require": ["high"], "exclude": ["composer", "fast", "auto"]},
                               "effort": "high"}},
        "grok": {"default": {"prefer": [["grok-4.7", "high"], ["grok-4.6", "high"]]}},
    },
    "spawn": {"kind": "claude", "model": "claude-opus-5-5", "effort": "high"},
    "helper": {"agent": "codex", "model": "gpt-6.1-sol", "effort": "low"},
    "notes": ["Antigravity is Gemini only."],
}

SOL_CATALOG = json.dumps({"models": [
    {"slug": "gpt-6.1-sol", "display_name": "GPT-6.1-Sol", "visibility": "list",
     "default_reasoning_level": "low",
     "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high")],
     "service_tiers": [{"id": "priority", "name": "Fast"}], "additional_speed_tiers": ["fast"]},
    {"slug": "gpt-6-astra", "display_name": "GPT-6-Astra", "visibility": "list",
     "default_reasoning_level": "medium",
     "supported_reasoning_levels": [{"effort": "medium"}],
     "service_tiers": [{"id": "priority", "name": "Fast"}], "additional_speed_tiers": ["fast"]},
    {"slug": "gpt-7-nova", "display_name": "GPT-7-Nova", "visibility": "list",
     "default_reasoning_level": "medium",
     "supported_reasoning_levels": [{"effort": "medium"}]},
]})


class Policy(unittest.TestCase):
    def override(self, data):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "model-policy.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(data if isinstance(data, str) else json.dumps(data))
        return patch.dict(os.environ, {"HERDR_PLUGIN_CONFIG_DIR": directory})

    def test_shipped_policy_reproduces_todays_defaults(self):
        shipped = policy.load()
        self.assertFalse(shipped["override"])
        self.assertEqual(shipped["forbid"], {"fast": False, "service_tiers": [], "models": [], "reason": ""})
        self.assertEqual(route.default_phrase("codex"), "astra")
        self.assertEqual(route.default_phrase("claude"), "opus high")
        self.assertEqual(shipped["routes"]["cursor"]["default"]["prefer"], ["gpt-5.6-sol-high-fast"])
        self.assertEqual(shipped["routes"]["cursor"]["helper_model"], "grok-4.7-high-fast")
        self.assertEqual(shipped["routes"]["grok"]["default"]["prefer"][0], ["grok-4.7-build-fast", "medium"])
        self.assertEqual(shipped["spawn"]["kind"], "claude")
        with patch.object(route, "run_catalog", return_value=CURSOR_DEFAULTS):
            result = route.cursor_route("default")
        self.assertEqual(result, {"kind": "cursor", "model": "gpt-5.6-sol-high-fast", "effort": "high",
                                  "fast": True, "argv": ["--model", "gpt-5.6-sol-high-fast"]})
        without_sol = CURSOR_DEFAULTS.replace("gpt-5.6-sol-high-fast - GPT-5.6 Sol High Fast\n", "")
        with patch.object(route, "run_catalog", return_value=without_sol):
            # Old fallback: high and fast, never Grok, Composer, or auto.
            self.assertEqual(route.cursor_route("default")["model"], "claude-opus-5-5-high-fast")

    def test_override_replaces_codex_default_and_spawn_only(self):
        with self.override(STRICT_OVERRIDE):
            merged = policy.load()
            self.assertTrue(merged["override"])
            self.assertEqual(route.default_phrase("codex"), "gpt-6.1-sol low")
            self.assertEqual(route.default_phrase("claude"), "opus high")  # untouched kind keeps shipped
            self.assertEqual(merged["routes"]["cursor"]["helper_model"], "grok-4.7-high-fast")  # per-key merge
            self.assertEqual(merged["spawn"], STRICT_OVERRIDE["spawn"])
            self.assertEqual(merged["notes"], ["Antigravity is Gemini only."])
            with patch.object(route, "run_catalog", return_value=SOL_CATALOG):
                result = route.codex_route("default")
            self.assertEqual(result["argv"], ["-m", "gpt-6.1-sol", "-c", 'model_reasoning_effort="low"',
                                              "-c", 'service_tier="default"'])
            shell = policy.shell_defaults(merged)
            self.assertIn("HELPER_POLICY_SPAWN_MODEL=claude-opus-5-5", shell)
            self.assertIn("HELPER_POLICY_AGENT=codex", shell)

    def test_forbid_models_blocks_families_not_opus_55(self):
        with self.override(STRICT_OVERRIDE):
            merged = policy.load()
            for model in ("gpt-6-astra", "claude-fable-5-1", "claude-opus-5", "claude-opus-5-1",
                          "claude-opus-4-6-thinking", "claude-opus-5-high"):
                self.assertIsNotNone(policy.forbidden_model(merged, model), model)
            for model in ("claude-opus-5-5", "claude-opus-5-5[1m]", "gpt-6.1-sol", "claude-sonnet-5-5"):
                self.assertIsNone(policy.forbidden_model(merged, model), model)
            with patch.object(route, "run_catalog", return_value=SOL_CATALOG):
                with self.assertRaisesRegex(route.RouteError, "forbidden"):
                    route.codex_route("astra")
            with patch.object(route, "run_catalog", side_effect=claude_read):
                with self.assertRaisesRegex(route.RouteError, "forbidden"):
                    route.claude_route("fable high")
                # The fixture alias opus resolves to claude-opus-5: the ban
                # applies to the resolved id, not the alias.
                with self.assertRaisesRegex(route.RouteError, "claude-opus-5 is forbidden"):
                    route.claude_route("opus high")

            def opus_55(command, *, input_text=None):
                text = claude_read(command, input_text=input_text)
                return text.replace('"claude-opus-5"', '"claude-opus-5-5"') if input_text else text
            with patch.object(route, "run_catalog", side_effect=opus_55):
                self.assertEqual(route.claude_route("opus high")["model"], "claude-opus-5-5")

    def test_forbid_models_sees_through_dated_ids(self):
        self.assertEqual(policy.forbid_words("claude-opus-5-20261001"), {"claude", "opus", "5"})
        self.assertEqual(policy.forbid_words("claude-opus-4-6-20250929"), {"claude", "opus", "4.6"})
        self.assertEqual(policy.forbid_words("claude-opus-5-5"), {"claude", "opus", "5.5"})
        with self.override(STRICT_OVERRIDE):
            merged = policy.load()
            for model in ("claude-opus-5-20261001", "claude-opus-4-6-20250929", "claude-opus-5-1-20261001",
                          "claude-fable-5-1-20261001"):
                self.assertIsNotNone(policy.forbidden_model(merged, model), model)
            for model in ("claude-opus-5-5", "claude-opus-5-5-20261001", "claude-haiku-4-5-20251001"):
                self.assertIsNone(policy.forbidden_model(merged, model), model)
            self.assertTrue(policy.argv_violations(merged, "claude", ["--model", "claude-opus-5-20261001"]))

    def test_forbid_applies_to_resolved_dated_id_behind_an_alias(self):
        def dated(command, *, input_text=None):
            text = claude_read(command, input_text=input_text)
            return text.replace('"claude-opus-5"', '"claude-opus-5-20261001"') if input_text else text
        with self.override(STRICT_OVERRIDE):
            self.assertIsNone(policy.forbidden_phrase(policy.load(), "opus high"))
            with patch.object(route, "run_catalog", side_effect=dated):
                with self.assertRaisesRegex(route.RouteError, "claude-opus-5-20261001 is forbidden"):
                    route.claude_route("opus high")

    def test_forbid_fast_blocks_phrases_and_fast_ids(self):
        with self.override(STRICT_OVERRIDE):
            with patch.object(route, "run_catalog", return_value=SOL_CATALOG):
                with self.assertRaisesRegex(route.RouteError, "fast"):
                    route.codex_route("6.1 sol high fast")
            with patch.object(route, "run_catalog", return_value=CURSOR_DEFAULTS):
                with self.assertRaisesRegex(route.RouteError, "fast"):
                    route.cursor_route("5.6 sol high fast")
                # The default skips forbidden ids instead of failing on them.
                self.assertEqual(route.cursor_route("default")["model"], "claude-opus-5-5-high")
            with patch.object(preflight, "run", return_value=CURSOR_DEFAULTS):
                with self.assertRaisesRegex(preflight.CheckError, "fast"):
                    preflight.check("cursor", "grok-4.7-high-fast", "")
            merged = policy.load()
            self.assertTrue(policy.argv_violations(merged, "cursor", ["--model", "grok-4.7-high-fast"]))

    def test_check_argv_blocks_priority_tier(self):
        merged_shipped = policy.load()
        argv = ["-m", "gpt-6.1-sol", "-c", 'service_tier="priority"']
        self.assertEqual(policy.argv_violations(merged_shipped, "codex", argv), [])
        with self.override(STRICT_OVERRIDE):
            merged = policy.load()
            self.assertTrue(policy.argv_violations(merged, "codex", argv))
            self.assertTrue(policy.argv_violations(merged, "codex", ["--config=service_tier=fast"]))
            self.assertEqual(policy.argv_violations(
                merged, "codex", ["-m", "gpt-6.1-sol", "-c", 'service_tier="default"']), [])
            self.assertEqual(policy.main(["check-argv", "codex", "--", *argv]), 2)
            self.assertEqual(policy.main(["check-argv", "codex", "--", "-m", "gpt-6.1-sol"]), 0)

    def test_check_argv_every_option_spelling(self):
        # Review finding 1: attached short options bypassed the check.
        with self.override(STRICT_OVERRIDE):
            merged = policy.load()
            for argv in (["-mgpt-6-astra"], ["-m", "gpt-6-astra"], ["--model=gpt-6-astra"],
                         ["--model", "gpt-6-astra"], ["-cmodel=gpt-6-astra"],
                         ["-cservice_tier=priority"], ["-c", "service_tier='priority'"],
                         ["-c", 'service_tier="priority"'], ["--config", "service_tier=fast"],
                         ["--config=service_tier=priority"], ["--config=model=\"gpt-6-astra\""]):
                self.assertTrue(policy.argv_violations(merged, "codex", argv), argv)
                self.assertEqual(policy.main(["check-argv", "codex", "--", *argv]), 2, argv)
            for argv in (["-mgpt-6.1-sol"], ["-cservice_tier=default"], ["-cmodel_reasoning_effort=low"],
                         ["-m", "gpt-6.1-sol", "-c", 'service_tier="default"']):
                self.assertEqual(policy.argv_violations(merged, "codex", argv), [], argv)

    def test_nested_route_fields_are_validated(self):
        # Review finding 4: a wrongly typed nested route field loaded fine.
        broken_routes = (
            {"codex": {"default": {"model": "astra", "effort": 123}}},
            {"codex": {"default": {"model": 5}}},
            {"codex": {"default": "astra"}},
            {"claude": {"default": {"model": "opus", "effort": ["high"]}}},
            {"cursor": {"default": {"prefer": "gpt-5.6-sol-high-fast"}}},
            {"cursor": {"default": {"prefer": [], "fallback": {"require": "high"}}}},
            {"cursor": {"default": {"prefer": [], "effort": 1}}},
            {"cursor": {"helper_model": 7}},
            {"grok": {"default": {"prefer": [["grok-4.7"]]}}},
            {"grok": {"default": {"prefer": [["grok-4.7", 3]]}}},
            {"fugu": {"default": 1}},
        )
        for routes in broken_routes:
            with self.override({"schema": 1, "routes": routes}):
                with self.assertRaises(policy.PolicyError, msg=routes):
                    policy.load()
                self.assertEqual(policy.main(["check-argv", "codex", "--", "-m", "x"]), 2, routes)
                self.assertEqual(policy.main(["shell"]), 2, routes)
                with patch.object(route, "run_catalog", return_value=SOL_CATALOG):
                    with self.assertRaisesRegex(route.RouteError, "model policy", msg=routes):
                        route.codex_route("gpt-6.1 sol")
                with patch.object(preflight, "run", return_value=SOL_CATALOG):
                    with self.assertRaises(preflight.CheckError, msg=routes):
                        preflight.check("codex", "gpt-6.1-sol", "")
        with self.override({"schema": 1, "routes": {"codex": {"default": {"model": "gpt-6.1-sol", "effort": None}}}}):
            self.assertEqual(policy.load()["routes"]["codex"]["default"]["model"], "gpt-6.1-sol")

    def test_malformed_override_fails_loudly(self):
        for broken in ("{not json", json.dumps({"schema": 2}),
                       json.dumps({"schema": 1, "forbid": {"models": "astra"}}),
                       json.dumps({"schema": 1, "routes": {"gemini": {}}})):
            with self.override(broken):
                with self.assertRaises(policy.PolicyError):
                    policy.load()
                with patch.object(route, "run_catalog", return_value=SOL_CATALOG):
                    with self.assertRaisesRegex(route.RouteError, "model policy"):
                        route.codex_route("default")
                self.assertEqual(policy.main(["policy"]), 2)

    def test_user_named_model_outside_policy_still_routes(self):
        with self.override(STRICT_OVERRIDE):
            with patch.object(route, "run_catalog", return_value=SOL_CATALOG):
                result = route.codex_route("gpt-7 nova")
            self.assertEqual(result["model"], "gpt-7-nova")

    def test_preflight_drops_forbidden_substitute(self):
        def run(command, *, input_text=None):
            if command != ["claude", "/usage", "-p", "--output-format", "json"]:
                return claude_read(command, input_text=input_text)
            return json.dumps({"result": "Current session: 0% used\nCurrent week (Opus): 100% used"})
        override = dict(STRICT_OVERRIDE, forbid=dict(STRICT_OVERRIDE["forbid"], models=[["sonnet"]]))
        with self.override(override):
            with patch.object(preflight, "run", side_effect=run):
                output = io.StringIO()
                with redirect_stdout(output):
                    code = preflight.check("claude", "opus", "high")
            self.assertEqual(code, 3)
            self.assertIsNone(json.loads(output.getvalue())["substitute"])

    def test_policy_prompt_names_defaults_and_bans(self):
        shipped = policy.render_prompt(policy.load())
        self.assertIn("Forbidden: nothing", shipped)
        self.assertIn("Grok Build → `--kind grok` with no `--model`", shipped)
        with self.override(STRICT_OVERRIDE):
            text = policy.render_prompt(policy.load())
        self.assertIn("`gpt-6.1-sol` at effort low", text)
        self.assertIn("every fast route", text)
        self.assertIn("astra; fable; opus 4.6; opus 5; opus 5.1", text)
        self.assertIn("against the live catalog", text)
        self.assertIn("Antigravity is Gemini only.", text)
        self.assertNotIn("Codex Astra high", text)
        self.assertNotIn("high fast →", text)


CURSOR_DEFAULTS = """Available models
auto - Auto (default)
gpt-5.6-sol-high-fast - GPT-5.6 Sol High Fast
grok-4.7-high-fast - Grok 4.7 High Fast
composer-2-high-fast - Composer 2 High Fast
claude-opus-5-5-high-fast - Claude Opus 5.5 1M High Fast
claude-opus-5-5-high - Claude Opus 5.5 1M
"""


if __name__ == "__main__":
    unittest.main()
