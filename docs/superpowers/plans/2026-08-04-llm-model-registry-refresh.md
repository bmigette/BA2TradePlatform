# LLM Model Registry Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Update the LLM model registry to the current Kimi/DeepSeek/xAI/OpenAI lineups (verified against official docs 2026-08-04), remove EOL models, and migrate orphaned DB settings.

**Architecture:** single-source registry edit (`packages/common/ba2_common/core/models_registry.py` — UI/ModelFactory derive from it), plus manual sync of two legacy `valid_values` lists, a DB migration script, and a `test_tools/test_models.py` preferred-order sync.

**Tech Stack:** Python 3.11+, pytest, SQLite (settings DB).

Spec: `docs/superpowers/specs/2026-08-04-llm-model-registry-refresh-design.md`

## Global Constraints

- Repo root: `C:/Users/basti/Documents/dev/BA2TradePlatform`. Venv python: `.venv/Scripts/python.exe` (Windows Git Bash).
- The in-tree `ba2_trade_platform/core/models_registry.py` is an alias shim — NEVER edit it; edit `packages/common/ba2_common/core/models_registry.py`.
- New registry entries are NATIVE-PROVIDER ONLY (no NagaAI/OpenRouter provider_names — availability unverifiable).
- Remove ONLY these friendly names — everything else stays: `kimi_k2`, `kimi_k2_thinking`, `kimi_k2_thinking_turbo`, `kimi_k2.5`, `kimi_k2.5-nonthinking`, `kimi_k1.5`, `deepseek_v3.2`, `deepseek_chat`, `deepseek_reasoner`, `deepseek_coder`, `grok4.1_fast`, `grok4.1_fast_reasoning`, `o1`, `o1_mini`.
- Do not bump `APP_VERSION` (handled separately before push).
- Commit after every task.

---

### Task 1: registry update (add 9, remove 14)

**Files:**
- Modify: `packages/common/ba2_common/core/models_registry.py` (`MODELS` dict; OpenAI section ~lines 110-330, xAI ~330-417, DeepSeek ~455-507, Moonshot ~508-618)
- Modify: `test_tools/test_models.py` (preferred_order dict, ~lines 123-133)
- Test: `tests/test_models_registry_refresh.py` (create)

**Interfaces:**
- Consumes: existing registry entry shape (`native_provider`, `display_name`, `description`, `provider_names`, `labels`, optional `supports_parameters`/`fixed_temperature`/`fixed_top_p`/`default_model_kwargs`, `context_size`).
- Produces: new friendly names `gpt5.6_sol`, `gpt5.6_terra`, `gpt5.6_luna`, `grok4.5`, `kimi_k3`, `kimi_k2.7_code`, `kimi_k2.7_code_highspeed`, `deepseek_v4_flash`, `deepseek_v4_pro` (relied on by Tasks 2-3).

- [ ] **Step 1: Write the failing test**

Create `tests/test_models_registry_refresh.py`:

```python
"""Registry refresh 2026-08: new models present, EOL models gone, structure sane."""
import pytest

from ba2_common.core.models_registry import MODELS, PROVIDER_CONFIG

NEW = {
    "gpt5.6_sol": ("openai", "gpt-5.6-sol"),
    "gpt5.6_terra": ("openai", "gpt-5.6-terra"),
    "gpt5.6_luna": ("openai", "gpt-5.6-luna"),
    "grok4.5": ("xai", "grok-4.5"),
    "kimi_k3": ("moonshot", "kimi-k3"),
    "kimi_k2.7_code": ("moonshot", "kimi-k2.7-code"),
    "kimi_k2.7_code_highspeed": ("moonshot", "kimi-k2.7-code-highspeed"),
    "deepseek_v4_flash": ("deepseek", "deepseek-v4-flash"),
    "deepseek_v4_pro": ("deepseek", "deepseek-v4-pro"),
}

REMOVED = [
    "kimi_k2", "kimi_k2_thinking", "kimi_k2_thinking_turbo",
    "kimi_k2.5", "kimi_k2.5-nonthinking", "kimi_k1.5",
    "deepseek_v3.2", "deepseek_chat", "deepseek_reasoner", "deepseek_coder",
    "grok4.1_fast", "grok4.1_fast_reasoning",
    "o1", "o1_mini",
]

# Dated but NOT retired — must survive the cleanup.
KEPT = ["kimi_k2.6", "kimi_k2.6-nonthinking", "grok4", "grok4_fast",
        "grok4_fast_reasoning", "grok3", "grok3_mini", "gpt4o", "gpt4o_mini",
        "o3_mini", "o4_mini", "gpt5", "gpt5.4"]


@pytest.mark.parametrize("friendly,(provider, pname)", NEW.items())
def test_new_models_present(friendly, provider, pname):
    entry = MODELS[friendly]
    assert entry["native_provider"] == provider
    assert entry["provider_names"][provider] == pname
    # native-only until the gateways list them
    assert set(entry["provider_names"]) == {provider}


@pytest.mark.parametrize("friendly", REMOVED)
def test_eol_models_removed(friendly):
    assert friendly not in MODELS


@pytest.mark.parametrize("friendly", KEPT)
def test_dated_but_live_models_kept(friendly):
    assert friendly in MODELS


def test_registry_structure_sane():
    for friendly, entry in MODELS.items():
        assert entry["native_provider"] in PROVIDER_CONFIG, friendly
        for prov in entry["provider_names"]:
            assert prov in PROVIDER_CONFIG, (friendly, prov)
        assert entry["context_size"] > 0, friendly


def test_preferred_order_lists_have_no_removed_models():
    import test_tools.test_models as tm
    for provider, order in tm.PREFERRED_ORDER.items():
        for friendly in order:
            assert friendly in MODELS, f"{provider}: stale preferred name {friendly}"
```

NOTE for the implementer: check how `test_tools/test_models.py` exposes the preferred-order dict (it may be a function-local `preferred_order` inside `get_models_for_provider`). If so, hoist it to module level as `PREFERRED_ORDER` (used by the function) as part of this task and say so in the report.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_models_registry_refresh.py -v`
Expected: FAIL — new names missing (`KeyError`), removed names present.

- [ ] **Step 3: Edit the registry**

In `packages/common/ba2_common/core/models_registry.py`:

- DELETE the 14 entries listed in Global Constraints (whole `"key": { ... },` blocks, including their comments).
- ADD, in the appropriate family sections (mirror the exact entry shape of the nearest sibling — field order, comment style):

```python
    "gpt5.6_sol": {
        "native_provider": PROVIDER_OPENAI,
        "display_name": "GPT-5.6 Sol",
        "description": "OpenAI's GPT-5.6 flagship for complex reasoning and coding (alias gpt-5.6)",
        "provider_names": {
            PROVIDER_OPENAI: "gpt-5.6-sol",
        },
        "labels": [LABEL_HIGH_COST, LABEL_THINKING, LABEL_WEBSEARCH, LABEL_VISION, LABEL_TOOL_CALLING],
        "supports_parameters": ["reasoning_effort"],
        "context_size": 1050000,
    },
    "gpt5.6_terra": {
        "native_provider": PROVIDER_OPENAI,
        "display_name": "GPT-5.6 Terra",
        "description": "GPT-5.6 tier balancing intelligence and cost",
        "provider_names": {
            PROVIDER_OPENAI: "gpt-5.6-terra",
        },
        "labels": [LABEL_HIGH_COST, LABEL_THINKING, LABEL_WEBSEARCH, LABEL_VISION, LABEL_TOOL_CALLING],
        "supports_parameters": ["reasoning_effort"],
        "context_size": 1050000,
    },
    "gpt5.6_luna": {
        "native_provider": PROVIDER_OPENAI,
        "display_name": "GPT-5.6 Luna",
        "description": "GPT-5.6 tier for cost-sensitive, high-volume workloads",
        "provider_names": {
            PROVIDER_OPENAI: "gpt-5.6-luna",
        },
        "labels": [LABEL_LOW_COST, LABEL_FAST, LABEL_THINKING, LABEL_WEBSEARCH, LABEL_VISION, LABEL_TOOL_CALLING],
        "supports_parameters": ["reasoning_effort"],
        "context_size": 1050000,
    },
```

```python
    "grok4.5": {
        "native_provider": PROVIDER_XAI,
        "display_name": "Grok-4.5",
        "description": "xAI's Grok-4.5 flagship — agentic tool calling, configurable reasoning, 500k context",
        "provider_names": {
            PROVIDER_XAI: "grok-4.5",
        },
        "labels": [LABEL_HIGH_COST, LABEL_THINKING, LABEL_CODING, LABEL_WEBSEARCH, LABEL_TOOL_CALLING],
        "context_size": 500000,
    },
```

```python
    "deepseek_v4_flash": {
        "native_provider": PROVIDER_DEEPSEEK,
        "display_name": "DeepSeek V4 Flash",
        "description": "DeepSeek's V4 volume tier (thinking + non-thinking modes) — successor of deepseek-chat/deepseek-reasoner (retired 2026-07-24)",
        "provider_names": {
            PROVIDER_DEEPSEEK: "deepseek-v4-flash",
        },
        "labels": [LABEL_LOW_COST, LABEL_CODING, LABEL_THINKING, LABEL_TOOL_CALLING],
        "context_size": 1000000,
    },
    "deepseek_v4_pro": {
        "native_provider": PROVIDER_DEEPSEEK,
        "display_name": "DeepSeek V4 Pro",
        "description": "DeepSeek's V4 flagship with 1M context (thinking + non-thinking modes)",
        "provider_names": {
            PROVIDER_DEEPSEEK: "deepseek-v4-pro",
        },
        "labels": [LABEL_CODING, LABEL_THINKING, LABEL_TOOL_CALLING],
        "context_size": 1000000,
    },
```

```python
    "kimi_k3": {
        "native_provider": PROVIDER_MOONSHOT,
        "display_name": "Kimi K3",
        "description": "Moonshot AI's Kimi K3 flagship (2.8T params, native vision, 1M context)",
        "provider_names": {
            PROVIDER_MOONSHOT: "kimi-k3",
        },
        "labels": [LABEL_HIGH_COST, LABEL_THINKING, LABEL_VISION, LABEL_CODING, LABEL_WEBSEARCH, LABEL_TOOL_CALLING],
        "context_size": 1048576,
    },
    "kimi_k2.7_code": {
        "native_provider": PROVIDER_MOONSHOT,
        "display_name": "Kimi K2.7 Code",
        "description": "Moonshot AI's dedicated coding model (256K context)",
        "provider_names": {
            PROVIDER_MOONSHOT: "kimi-k2.7-code",
        },
        "labels": [LABEL_CODING, LABEL_TOOL_CALLING],
        "context_size": 262144,
    },
    "kimi_k2.7_code_highspeed": {
        "native_provider": PROVIDER_MOONSHOT,
        "display_name": "Kimi K2.7 Code Highspeed",
        "description": "High-speed variant of Kimi K2.7 Code (~180 tok/s output, 256K context)",
        "provider_names": {
            PROVIDER_MOONSHOT: "kimi-k2.7-code-highspeed",
        },
        "labels": [LABEL_CODING, LABEL_FAST, LABEL_TOOL_CALLING],
        "context_size": 262144,
    },
```

- In `test_tools/test_models.py`: update `preferred_order` — `PROVIDER_DEEPSEEK: ["deepseek_v4_flash", "deepseek_v4_pro"]`, `PROVIDER_MOONSHOT: ["kimi_k2.6", "kimi_k2.6-nonthinking", "kimi_k3"]`, `PROVIDER_XAI: ["grok3_mini", "grok3", "grok4_fast", "grok4.5"]`, `PROVIDER_OPENAI`: append `"gpt5.6_luna", "gpt5.6_terra", "gpt5.6_sol"` at the end (most-reliable-first ordering preserved). Hoist to module-level `PREFERRED_ORDER` if function-local (see Step 1 note).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_models_registry_refresh.py -v`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add packages/common/ba2_common/core/models_registry.py test_tools/test_models.py tests/test_models_registry_refresh.py
git commit -m "feat(models): add gpt-5.6/grok-4.5/kimi-k3/k2.7-code/deepseek-v4, remove EOL models"
```

---

### Task 2: legacy valid_values sync (MarketExpertInterface)

**Files:**
- Modify: `packages/common/ba2_common/core/interfaces/MarketExpertInterface.py` (`risk_manager_model` valid_values ~lines 228-267; `dynamic_instrument_selection_model` valid_values ~lines 275-312)
- Test: `tests/test_models_registry_refresh.py` (append)

**Interfaces:**
- Consumes: Task 1's added/removed provider-model names.
- Produces: nothing new (list contents only).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_models_registry_refresh.py`:

```python
def test_legacy_valid_values_have_no_eol_strings():
    from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
    defs = MarketExpertInterface.get_settings_definitions()
    eol_fragments = ["kimi-k2-thinking", "kimi-k2.5", "moonshot-v1",
                     "deepseek-v3.2", "deepseek-chat", "deepseek-reasoner",
                     "deepseek-coder", "grok-4-1-fast", "/o1"]
    for setting in ("risk_manager_model", "dynamic_instrument_selection_model"):
        values = defs[setting]["valid_values"]
        for v in values:
            for frag in eol_fragments:
                assert frag not in v, f"{setting}: EOL model string {v}"


def test_legacy_valid_values_include_new_models():
    from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
    defs = MarketExpertInterface.get_settings_definitions()
    for setting in ("risk_manager_model", "dynamic_instrument_selection_model"):
        joined = " ".join(defs[setting]["valid_values"])
        for frag in ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
                     "grok-4.5", "kimi-k3", "deepseek-v4-flash", "deepseek-v4-pro"):
            assert frag in joined, f"{setting}: missing {frag}"
```

NOTE: `MarketExpertInterface.get_settings_definitions` may be an instance method or classmethod — check how existing tests call it (grep `tests/` for `risk_manager_model`) and adapt. If it needs an instance, use the minimal construction existing tests use.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_models_registry_refresh.py -k legacy -v`
Expected: FAIL — EOL strings present, new strings absent.

- [ ] **Step 3: Sync the lists**

In `MarketExpertInterface.py`, for BOTH `valid_values` lists (they are near-identical — apply the same edits to each):

- REMOVE every string containing: `kimi-k2-thinking`, `kimi-k2.5`, `moonshot-v1`, `deepseek-v3.2` (incl. `deepseek-v3.2-speciale`), `deepseek-chat`, `deepseek-reasoner`, `deepseek-coder`, `grok-4-1-fast`, and any `/o1` / `/o1-mini` entries (including `:free` variants).
- ADD, mirroring the list's existing prefix/format conventions (e.g. `NagaAI/<provider-model-name>`; match exactly how neighboring entries are written — read the list first): `NagaAI/gpt-5.6-sol`, `NagaAI/gpt-5.6-terra`, `NagaAI/gpt-5.6-luna`, `NagaAI/grok-4.5`, `NagaAI/kimi-k3`, `NagaAI/kimi-k2.7-code`, `NagaAI/deepseek-v4-flash`, `NagaAI/deepseek-v4-pro`. If the existing lists mix bare native names and prefixed names, follow the dominant local pattern per provider family.
- Do NOT change the defaults (`NagaAC/gpt-5.1-2025-11-13` stays — still live).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_models_registry_refresh.py -v`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add packages/common/ba2_common/core/interfaces/MarketExpertInterface.py tests/test_models_registry_refresh.py
git commit -m "feat(models): sync legacy valid_values with the registry refresh"
```

---

### Task 3: DB migration script for removed models

**Files:**
- Create: `test_tools/migrate_eol_models_2026_08.py`
- Test: `tests/test_migrate_eol_models.py` (create)

**Interfaces:**
- Consumes: the existing script `test_tools/migrate_kimi_k2_models.py` (read it first — mirror its CLI shape: dry-run by default, `--apply` to write, same DB resolution).
- Produces: `REMAP` (ordered list of (regex, replacement) pairs) and `remap_value(value: str) -> str | None` (pure function; returns the remapped string or None if unchanged) — the test imports these.

- [ ] **Step 1: Write the failing test**

Create `tests/test_migrate_eol_models.py`:

```python
"""EOL model migration remap — pure-function tests (no DB)."""
import pytest

from test_tools.migrate_eol_models_2026_08 import remap_value

CASES = {
    # thinking variants keep thinking on the successor
    "moonshot/kimi_k2_thinking": "moonshot/kimi_k2.6",
    "moonshot/kimi_k2_thinking_turbo": "moonshot/kimi_k2.6",
    "moonshot/kimi_k2.5": "moonshot/kimi_k2.6",
    # non-thinking / base variants land on the instant successor
    "moonshot/kimi_k2": "moonshot/kimi_k2.6-nonthinking",
    "moonshot/kimi_k2.5-nonthinking": "moonshot/kimi_k2.6-nonthinking",
    "moonshot/kimi_k1.5": "moonshot/kimi_k2.6-nonthinking",
    # provider-name forms (legacy valid_values format) also remap
    "NagaAI/kimi-k2-thinking": "NagaAI/kimi-k2.6",
    "NagaAI/kimi-k2.5": "NagaAI/kimi-k2.6",
    "NagaAI/moonshot-v1-128k": "NagaAI/kimi-k2.6-nonthinking",
    # deepseek: everything lands on v4-flash (its thinking mode succeeds reasoner)
    "deepseek/deepseek_v3.2": "deepseek/deepseek_v4_flash",
    "deepseek/deepseek_chat": "deepseek/deepseek_v4_flash",
    "deepseek/deepseek_reasoner": "deepseek/deepseek_v4_flash",
    "deepseek/deepseek_coder": "deepseek/deepseek_v4_flash",
    "NagaAI/deepseek-v3.2": "NagaAI/deepseek-v4-flash",
    "NagaAI/deepseek-reasoner-0528": "NagaAI/deepseek-v4-flash",
    "OpenRouter/deepseek/deepseek-chat": "OpenRouter/deepseek/deepseek-v4-flash",
    # xai
    "xai/grok4.1_fast": "xai/grok4.5",
    "xai/grok4.1_fast_reasoning": "xai/grok4.5",
    "NagaAI/grok-4-1-fast-reasoning": "NagaAI/grok-4.5",
    # openai o1 family
    "openai/o1": "openai/gpt5.6_terra",
    "OpenRouter/openai/o1-mini": "OpenRouter/openai/gpt-5.6-terra",
    # untouched
    "moonshot/kimi_k2.6": None,
    "openai/gpt5.4": None,
    "NagaAC/gpt-5.1-2025-11-13": None,
    "openai/gpt4o_mini": None,
}


@pytest.mark.parametrize("value,expected", CASES.items())
def test_remap(value, expected):
    assert remap_value(value) == expected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_migrate_eol_models.py -v`
Expected: FAIL — `ImportError` (module doesn't exist).

- [ ] **Step 3: Implement the migration script**

Create `test_tools/migrate_eol_models_2026_08.py`, modeled on `test_tools/migrate_kimi_k2_models.py` (read it first; mirror its DB access, dry-run default, `--apply` flag, and row-reporting). Core:

```python
"""Migrate ExpertSetting model strings off EOL models (2026-08 registry refresh).

Dry-run by default; pass --apply to write. Mirrors migrate_kimi_k2_models.py.
Remaps BOTH friendly-name forms (moonshot/kimi_k2_thinking) and provider-name
forms (NagaAI/kimi-k2-thinking), preserving the provider prefix.
"""
import argparse
import re

# Ordered: longer/more-specific patterns first (thinking before base, nonthinking explicit).
REMAP = [
    # moonshot friendly names
    (r"kimi_k2_thinking(_turbo)?$", "kimi_k2.6"),
    (r"kimi_k2\.5$", "kimi_k2.6"),
    (r"kimi_k2\.5-nonthinking$", "kimi_k2.6-nonthinking"),
    (r"kimi_k2$", "kimi_k2.6-nonthinking"),
    (r"kimi_k1\.5$", "kimi_k2.6-nonthinking"),
    # moonshot provider names
    (r"kimi-k2-thinking(-turbo)?$", "kimi-k2.6"),
    (r"kimi-k2\.5$", "kimi-k2.6"),
    (r"kimi-k2(-0905-preview|-0711-preview|-turbo-preview)?$", "kimi-k2.6-nonthinking"),
    (r"moonshot-v1-(8k|32k|128k)(-vision-preview)?$", "kimi-k2.6-nonthinking"),
    # deepseek friendly names
    (r"deepseek_(v3\.2|chat|reasoner|coder)$", "deepseek_v4_flash"),
    # deepseek provider names
    (r"deepseek-(v3\.2|v3\.2-speciale|chat|chat-v3\.1|reasoner|reasoner-0528|coder)(:free)?$",
     "deepseek-v4-flash"),
    # xai friendly + provider names
    (r"grok4\.1_fast(_reasoning)?$", "grok4.5"),
    (r"grok-4-1-fast(-non)?-reasoning$", "grok-4.5"),
    # openai o1 family — dash = provider-name form, underscore = friendly form;
    # a bare trailing "/o1" defaults to the friendly successor (ModelSelector stores friendly names)
    (r"o1-mini(-\d{4}-\d{2}-\d{2})?$", "gpt-5.6-terra"),
    (r"o1_mini$", "gpt5.6_terra"),
    (r"(^|/)o1-\d{4}-\d{2}-\d{2}$", r"\g<1>gpt-5.6-terra"),
    (r"(^|/)o1$", r"\g<1>gpt5.6_terra"),
]


def remap_value(value: str) -> "str | None":
    """Return the remapped model string, or None when the value is untouched."""
    for pattern, replacement in REMAP:
        new = re.sub(pattern, replacement, value)
        if new != value:
            return new
    return None
```

Then the DB walk: mirror `migrate_kimi_k2_models.py` — find every `ExpertSetting.value_str` (and any other column that script covers) whose value remaps non-None, print the planned changes, apply only with `--apply`. Reuse its DB-path resolution exactly.

Watch out: dash vs underscore distinguishes the forms — `o1-mini` (provider-name form) maps to `gpt-5.6-terra`, `o1_mini` (friendly form) to `gpt5.6_terra`, and a bare trailing `/o1` defaults to the friendly successor (ModelSelector stores friendly names). Order in REMAP wins (first match applies), so the dash/dated rules come before the bare-`o1` rule. Verify against the test cases (especially `OpenRouter/openai/o1-mini` → `OpenRouter/openai/gpt-5.6-terra`: the value ends with `o1-mini`, the `openai/` in the middle must survive — a `$`-anchored sub on the LAST path segment does this).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_migrate_eol_models.py -v`
Expected: all passed.

- [ ] **Step 5: Dry-run smoke check**

Run: `.venv/Scripts/python.exe test_tools/migrate_eol_models_2026_08.py`
Expected: prints the dry-run report (0 or more rows depending on the local DB) and writes nothing.

- [ ] **Step 6: Commit**

```bash
git add test_tools/migrate_eol_models_2026_08.py tests/test_migrate_eol_models.py
git commit -m "feat(models): DB migration for EOL model settings (dry-run default)"
```

---

### Task 4: regression sweep

- [ ] **Step 1: Run the suites**

```bash
.venv/Scripts/python.exe -m pytest tests/ -q
.venv/Scripts/python.exe -m pytest packages/common/tests/ -q
```

Expected: live-app suite all passed (was 1096 before this branch; +~30 new here); packages/common has 5 PRE-EXISTING order-dependent failures in `test_new_option_actions.py` (documented, unrelated — `packages/common/tests/test_new_option_actions.py` standalone passes 18/18).

- [ ] **Step 2: Registry import smoke check**

```bash
.venv/Scripts/python.exe -c "from ba2_common.core.models_registry import MODELS; print(len(MODELS), 'models'); from ba2_trade_platform.core.models_registry import MODELS as M2; assert M2 is MODELS; print('shim OK')"
```

Expected: prints the model count and `shim OK`.

- [ ] **Step 3: Commit (only if the sweep produced fixes)**

```bash
git add -A
git commit -m "test: regression sweep for model registry refresh"
```
