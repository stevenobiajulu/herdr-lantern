# Model policy

Lantern keeps every default model and every model ban in one data file, so
the resolver, the preflight, the `bin/herdr` wrapper, onboard, and the
launch appendix read the same answer.

- **Shipped:** `model-policy.json` at the plugin root. It reproduces the
  routes Lantern used before the file existed. Do not edit it in an
  installed copy; an update replaces it.
- **Override:** `$HERDR_PLUGIN_CONFIG_DIR/model-policy.json`
  (`herdr plugin config-dir aigora.lantern`). Lantern never writes it. A
  personal rule — a different default, a banned family, no fast tiers —
  belongs here, not in the shipped file.

Inspect the result:

```sh
bin/model-route policy          # effective merged policy as JSON
bin/model-route policy-prompt   # the Model policy section launch injects
bin/model-route check-phrase codex "astra high"            # exit 2 when forbidden
bin/model-route check-argv codex -- -m gpt-6-astra -c 'service_tier="priority"'
```

`check-argv` exits 0 when the argv is allowed and 2 with one message per
problem when it names a forbidden model, a forbidden service tier, or (under
`forbid.fast`) a fast model or any non-default tier. It reads every spelling:
`-m X`, `-mX`, `--model X`, `--model=X`, and the same for `-c`/`--config`
settings of `model` and `service_tier`. `bin/herdr` runs it on the argv after
`--` for every `agent start`, whatever the option order, and blocks the start
on exit 2.

## Schema 1

```json
{
  "schema": 1,
  "source": "free text provenance (optional)",
  "forbid": {
    "fast": false,
    "service_tiers": [],
    "models": [],
    "reason": ""
  },
  "routes": {
    "claude": {"default": {"model": "opus", "effort": "high"}},
    "codex":  {"default": {"model": "astra", "effort": null}},
    "cursor": {"default": {"prefer": ["gpt-5.6-sol-high-fast"],
                           "fallback": {"require": ["high", "fast"],
                                        "exclude": ["composer", "grok", "auto"]},
                           "effort": "high"},
               "helper_model": "grok-4.7-high-fast"},
    "grok":   {"default": {"prefer": [["grok-4.7-build-fast", "medium"], ["grok-4.7", "high"]]}},
    "fugu":   {}
  },
  "spawn":  {"kind": "claude", "model": "", "effort": ""},
  "helper": {"agent": "", "model": "", "effort": ""},
  "notes": []
}
```

| Field | Meaning |
| --- | --- |
| `forbid.fast` | `true` refuses every fast route, even when the user asks: a phrase containing `fast`, a model id whose words contain `fast`, or any service tier other than `default`. |
| `forbid.service_tiers` | Tier ids or names refused anywhere (`fast`, `priority`, …), case-insensitive. |
| `forbid.models` | Token lists. A **resolved** model id is refused when every token in a list appears in its words. Words follow `model_words`: `claude-opus-5-5` is `claude opus 5.5`, so `["opus", "5"]` refuses `claude-opus-5` and `claude-opus-5-high` but not `claude-opus-5-5`. An alias is checked after the live catalog resolves it. For this check only, a date component of six or more digits is dropped from a version, so `claude-opus-5-20261001` reads as `opus 5` and is refused too, while `claude-opus-5-5-20261001` stays `opus 5.5`. |
| `forbid.reason` | Shown in every refusal. |
| `routes.<kind>.default` | What `model-route <kind> default` resolves. Claude and Codex take a `model` phrase or catalog id plus an `effort` (`null` = the catalog default; Codex always sets `service_tier="default"`). Cursor takes `prefer` ids, then a `fallback` token rule. Grok takes `prefer` pairs of `[id, effort]`. A candidate the forbid list refuses is skipped; when none is left the route fails rather than guessing. |
| `routes.cursor.helper_model` | The Cursor helper chat model when `HELPER_MODEL` is empty. |
| `spawn` | Used when `helper.conf` leaves `HELPER_SPAWN_KIND` empty. The three values move together. |
| `helper` | `agent` fills an empty `HELPER_AGENT`. `model` and `effort` fill empty `HELPER_MODEL`/`HELPER_EFFORT` only when `HELPER_AGENT` is that agent. |
| `notes` | Extra lines rendered verbatim into the injected Model policy section. |

The policy never refuses a model because it is absent from the file. An
explicit user phrase resolves through the live catalog; only `forbid`
refuses.

## Merge rule

1. Load the shipped file.
2. If the override exists, lay it over the shipped file:
   - `forbid`, `spawn`, and `helper` merge key by key;
   - `routes` merge per kind, then per key, so an override
     `routes.codex.default` replaces only that entry and leaves
     `routes.cursor.helper_model` alone;
   - `notes` from the override are appended;
   - `source` from the override replaces the shipped one.
3. An unreadable override, invalid JSON, a `schema` other than `1`, an
   unknown kind or key under `routes`, or a field of the wrong type at any
   depth (for example a numeric `effort`, a string `prefer`, or a Grok
   `prefer` entry that is not an `[id, effort]` pair) is an error.
   Every reader stops with that error; none falls back to the shipped
   values. Launch shows it and asks you to fix the file.

Without Python 3 the policy cannot be read. Launch then injects a one-line
notice instead of the Model policy section, and `bin/herdr` allows agent
starts only when no override exists (with an override it fails closed).
The Cursor helper model falls back to the shipped literal in that case.

## Example override

```json
{
  "schema": 1,
  "source": "my preferences",
  "forbid": {"fast": true, "service_tiers": ["fast", "priority"],
             "models": [["fable"]], "reason": "cost"},
  "routes": {"codex": {"default": {"model": "gpt-6.1-sol", "effort": "low"}}},
  "spawn": {"kind": "claude", "model": "opus", "effort": "high"}
}
```
