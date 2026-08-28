# LLM model registry refresh — latest Kimi/DeepSeek/xAI/OpenAI, remove EOL

Date: 2026-08-04
Status: approved design (pre-plan)

## Goal

Bring the supported-model registry up to date (verified against official provider docs on
2026-08-04) and remove end-of-life models, with a DB migration remapping orphaned settings.

Scope decision (user-approved): exactly the table below — only confirmed-EOL or
announced-sunset models are removed; dated-but-working models (grok-3/4, gpt-4o family,
o3_mini/o4_mini) stay.

## Verified facts (sources)

- Kimi ([platform.kimi.ai/docs/models](https://platform.kimi.ai/docs/models)): `kimi-k3`
  flagship (2.8T params, 1M context); `kimi-k2.7-code` / `kimi-k2.7-code-highspeed` (256k).
  `kimi-k2` series discontinued 2026-05-25. `kimi-k2.5` + `moonshot-v1` series sunset
  2026-08-31 (already unavailable to new users).
- DeepSeek ([deepseek.ai V4 GA](https://deepseek.ai/blog/deepseek-v4-ga-surge-pricing-migration)):
  `deepseek-chat` / `deepseek-reasoner` retired 2026-07-24 (hard errors). Current:
  `deepseek-v4-flash` (volume tier; non-thinking + thinking modes) and `deepseek-v4-pro`
  (flagship, 1M context).
- xAI ([docs.x.ai/docs/models](https://docs.x.ai/docs/models)): `grok-4.5` flagship (500k
  context, $2/$6 per MTok). `grok-4.1-fast` family retires 2026-08-15.
- OpenAI ([platform.openai.com/docs/models](https://platform.openai.com/docs/models)): GPT-5.6
  family current — `gpt-5.6-sol` (flagship, alias `gpt-5.6`), `gpt-5.6-terra`,
  `gpt-5.6-luna` (all 1.05M context). `gpt-5.2-chat-latest`/`gpt-5.3-chat-latest` shut down
  2026-08-10 (aliases only; dated snapshots unaffected). `o1` already marked deprecated in
  the registry.

## Changes

### `packages/common/ba2_common/core/models_registry.py` (the `MODELS` dict)

| Provider | Add (friendly name → provider name) | Remove |
|---|---|---|
| moonshot | `kimi_k3` → `kimi-k3`; `kimi_k2.7_code` → `kimi-k2.7-code`; `kimi_k2.7_code_highspeed` → `kimi-k2.7-code-highspeed` | `kimi_k2`, `kimi_k2_thinking`, `kimi_k2_thinking_turbo` (EOL 05-25); `kimi_k2.5`, `kimi_k2.5-nonthinking`, `kimi_k1.5` (sunset 08-31) |
| deepseek | `deepseek_v4_flash` → `deepseek-v4-flash`; `deepseek_v4_pro` → `deepseek-v4-pro` | `deepseek_v3.2`, `deepseek_chat`, `deepseek_reasoner`, `deepseek_coder` (all EOL 07-24) |
| xai | `grok4.5` → `grok-4.5` | `grok4.1_fast`, `grok4.1_fast_reasoning` (retire 08-15) |
| openai | `gpt5.6_sol` → `gpt-5.6-sol`; `gpt5.6_terra` → `gpt-5.6-terra`; `gpt5.6_luna` → `gpt-5.6-luna` | `o1`, `o1_mini` (deprecated) |

New entries are native-provider only (`provider_names` keyed by the native provider) —
NagaAI/OpenRouter availability of the brand-new models is not publicly verifiable. Context
windows/pricing metadata fields follow the existing entry shape (copy the nearest sibling:
kimi_k2.6 for kimi entries, deepseek_v3.2 for deepseek, grok4.1_fast for grok, gpt5.4 for
gpt5.6) with the documented values above.

Keep (dated but not retired): `kimi_k2.6`, `kimi_k2.6-nonthinking`, `grok4`, `grok4_fast`,
`grok4_fast_reasoning`, `grok3`, `grok3_mini`, `gpt4o`, `gpt4o_mini`, `o3_mini`, `o4_mini`,
all gpt-5.x dated snapshots (not the chat-latest aliases — those are not in the registry).

### Legacy valid_values sync

`packages/common/ba2_common/core/interfaces/MarketExpertInterface.py` — the two legacy
`valid_values` lists (`risk_manager_model` ~lines 228-267, `dynamic_instrument_selection_model`
~lines 275-312): remove the strings for every removed model
(`kimi-k2-thinking`, `deepseek-v3.2`, `deepseek-v3.2-speciale`, `deepseek-chat-v3.1`,
`deepseek-reasoner-0528` incl. `:free` variants, grok-4.1-fast entries), add the new NagaAI/
native strings matching the list's existing format. Default `"NagaAC/gpt-5.1-2025-11-13"`
stays (still live).

### DB migration script

New `test_tools/migrate_eol_models_2026_08.py`, modeled on
`test_tools/migrate_kimi_k2_models.py`: remap `ExpertSetting.value_str` (and any other
model-string columns it identifies, mirroring that script's coverage) for removed models:

- `kimi_k2` / `kimi_k2_thinking` / `kimi_k2_thinking_turbo` / `kimi_k2.5*` / `kimi_k1.5`
  (any provider prefix) → `kimi_k2.6` (thinking variants → `kimi_k2.6`, non-thinking →
  `kimi_k2.6-nonthinking`)
- `deepseek_v3.2` / `deepseek_chat` / `deepseek_coder` → `deepseek_v4_flash`;
  `deepseek_reasoner` → `deepseek_v4_flash` (its thinking mode is the reasoner successor;
  `deepseek_v4_pro` exists for users who want the flagship)
- `grok4.1_fast` / `grok4.1_fast_reasoning` → `grok4.5`
- `o1` / `o1_mini` → `gpt5.6_terra`

Same dry-run/apply CLI shape as the existing migration script.

## Testing

- Registry sanity: every MODELS entry's provider_names reference a PROVIDER_CONFIG provider;
  no entry references a removed model; the new friendly names resolve (there is an existing
  probe `test_files/test_model_registry.py` — check whether a durable test exists in
  `tests/` for the registry and extend/port it).
- Migration script: unit-test the remap function on sample rows (pure function, no DB);
  dry-run prints the intended remaps.
- Full live-app suite green.

## Out of scope

- NagaAI/OpenRouter entries for the new models (unverifiable; add when the gateways list them).
- Google/Anthropic/Bedrock/Qwen refresh (registry is current enough: gemini-3, claude 4.8/4.6).
- Vendored TradingAgents CLI pick-lists and `default_config.py` (legacy dev tooling).
- Aggressive cleanup of dated-but-working models.
