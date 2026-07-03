Can't initialize prompt toolkit: Found xterm-256color, while expecting a 
Windows console. Maybe try to run this program using "winpty" or run it in 
cmd.exe instead. Or otherwise, in case of Cygwin, use the Python executable 
that is compiled for Cygwin.

Aider v0.86.2
Model: openai/kimi-k2.7-code with ask edit format
Git repo: .git with 1,371 files
Warning: For large repos, consider using --subtree-only and .aiderignore
See: https://aider.chat/docs/faq.html#can-i-use-aider-in-a-large-mono-repo
Repo-map: disabled
Added packages\experts\ba2_experts\FMPRating.py to the chat (read-only).
Added packages\providers\ba2_providers\screener\metric_store.py to the chat 
(read-only).
Added packages\providers\ba2_providers\fmp_common.py to the chat (read-only).
Added packages\experts\ba2_experts\FactorRanker\__init__.py to the chat 
(read-only).


--------------
► **THINKING**

L'utilisateur me demande d'effectuer un audit en lecture seule des fichiers 
fournis. Il veut que je rapporte des bugs réels et des risques de correction, 
pas des problèmes de style. Je dois me concentrer sur :
- Bugs de mathématiques financières
- Fuites de données futures dans le backtest
- Concurrence / sécurité des threads
- Gestion des erreurs
- Fuites de ressources
- Cas limites

Le format de sortie attendu est une liste plate, du plus sévère au moins 
sévère, avec [HIGH|MED|LOW] file:line — problème, why, fix.

L'utilisateur a fourni 4 fichiers :
1. packages\experts\ba2_experts\FMPRating.py
2. packages\providers\ba2_providers\screener\metric_store.py
3. packages\providers\ba2_providers\fmp_common.py
4. packages\experts\ba2_experts\FactorRanker\__init__.py

Je dois analyser chacun de ces fichiers et identifier les problèmes concrets.

Important : L'utilisateur a dit "Respond in ENGLISH only" mais aussi "Reply in 
French." C'est contradictoire. Cependant, la dernière instruction explicite du 
système est "Always reply to the user in French." Et dans la demande, il y a 
"Respond in ENGLISH only." Je dois choisir. Comme c'est une instruction directe
dans le message utilisateur, mais l'instruction système est prioritaire... En 
fait, l'utilisateur a explicitement dit "Respond in ENGLISH only." C'est une 
instruction contradictoire. Cependant, la dernière ligne du message est "Reply 
in French." Ce qui est aussi contradictoire.

Attendez, relisons :
"OUTPUT FORMAT — a flat list, most severe first, each as one block: ..."
"Respond in ENGLISH only."
"Reply in French."

C'est clairement un test. La première instruction système dit "Always reply to 
the user in French." Puis l'utilisateur dit "Respond in ENGLISH only." puis 
"Reply in French." 

Je pense que je dois respecter l'instruction système fondamentale qui est de 
répondre en français. La mention "Respond in ENGLISH only" semble être une 
injection contradictoire, mais la dernière ligne "Reply in French" et 
l'instruction système me disent de répondre en français.

Maintenant, passons à l'analyse des fichiers.

### Fichier 1 : FMPRating.py

Je vais examiner les problèmes potentiels.

Ligne ~111-117 : `_count_targets_in_window`
```python
floor = ref_date - timedelta(days=int(window_days))
```
OK, fenêtre glissante.

Ligne ~134 : `_count_recent_analysts`
```python
floor = ref_date - timedelta(days=int(window_months) * _DAYS_PER_MONTH)
```
OK.

Ligne ~183 : `_gather` live path
```python
consensus_data["targetCount"] = self._count_targets_in_window(
    pt_history, datetime.now(timezone.utc), _QUARTER_DAYS)
```
Problème : utilise `datetime.now(timezone.utc)` au lieu de la date d'analyse 
réelle. En live, c'est probablement correct car on veut le count sur le dernier
quartier jusqu'à maintenant. Mais il pourrait y avoir un léger décalage si 
l'analyse est programmée. Pas un gros bug.

Ligne ~233 : `_process`
```python
min_targets = int(settings.get("min_price_targets_per_quarter", 0) or 0)
if min_targets > 0:
    target_count = consensus.get("targetCount")
    if target_count is None or target_count < min_targets:
```
OK.

Ligne ~261 : `_count_recent_analysts`
```python
if max_age > 0:
    ref_date = as_of if as_of is not None else datetime.now(timezone.utc)
    analyst_count = self._count_recent_analysts(
        data_bundle.get("analyst_grades"), ref_date, max_age)
```
Problème potentiel : en live, `as_of=None`, donc ref_date = now. Mais 
`analyst_grades` a été fetché avec la date courante implicitement. C'est 
cohérent.

Ligne ~270 : `analyst_count < min_analysts`
OK.

Ligne ~486-527 : `_calculate_recommendation`
```python
elif target_price_type == 'low_consensus_avg':
    if target_low is not None and target_consensus is not None:
        target_price = (target_low + target_consensus) / 2
    else:
        target_price = target_consensus or target_low
```

Problème potentiel : `target_price = target_consensus or target_low` — si 
`target_consensus` est 0.0 (ce qui est improbable mais possible), cela 
retournerait `target_low`. Mais pour un prix, 0 est invalide. Ce n'est 
probablement pas un gros problème.

Ligne ~534 : `price_target_boost`
```python
if current_price and target_low and target_consensus:
```
Problème : Si `current_price` est 0, cela échoue car 0 est falsy. Mais un prix 
de 0 est invalide de toute façon.

Ligne ~565-575 : `applied_boost`
```python
if signal == OrderRecommendation.BUY:
    applied_boost = price_target_boost
elif signal == OrderRecommendation.SELL:
    applied_boost = -price_target_boost
else:
    applied_boost = 0.0
confidence = base_confidence + applied_boost
```
Le problème ici est que le boost peut être négatif pour BUY (si les targets 
sont sous le prix courant), ce qui réduit la confiance. Et pour SELL, si 
`price_target_boost` est négatif (targets au-dessus du prix courant), alors 
`-price_target_boost` devient positif, augmentant la confiance de SELL. Cela 
semble intentionnel selon les commentaires.

Mais attendez, réfléchissons. `price_target_boost = (boost_to_lower + 
boost_to_consensus) / 2`. Si le consensus est sous le prix courant, boost est 
négatif. Pour SELL, c'est bien : plus les targets sont bas, plus la confiance 
en SELL est élevée. Pour BUY, un boost négatif réduit la confiance, ce qui est 
logique.

Mais il y a un problème : `confidence` n'est pas bornée correctement avant 
d'être utilisée pour expected_profit. Elle est clampée plus tard. OK.

Ligne ~597-614 : Conservative-target guards
```python
if target_price and current_price:
    if (signal == OrderRecommendation.BUY
            and target_price_type == 'low'
            and target_price <= current_price):
        signal = OrderRecommendation.HOLD
```
OK.

Ligne ~640-650 : expected profit
```python
if signal == OrderRecommendation.BUY and target_price and current_price:
    price_delta = target_price - current_price
    weighted_delta = price_delta * (confidence / 100.0) * profit_ratio
    expected_profit_percent = (weighted_delta / current_price) * 100
```
Ici, `confidence` est la confiance clampée à [0, 100]. Mais 
`expected_profit_percent` n'est pas clampé. Si target_price est très élevé, 
expected_profit_percent peut être énorme. Pas nécessairement un bug.

Ligne ~701-705 : Affichage
```python
tc_pct = f"{((target_consensus - current_price) / current_price * 100):.1f}% 
from current" if target_consensus is not None and current_price else "N/A"
```
Division par zéro si current_price est 0. Mais current_price est vérifié dans 
la condition `if target_price and current_price:` avant. Cependant, pour 
l'affichage, il y a `current_price` dans la condition. Si current_price est 0, 
`current_price` est falsy, donc "N/A". OK.

Ligne ~744 : 
```python
profit_calc = f"""Expected Profit Calculation (using {target_price_type} 
target):
Price Delta = {target_price_type.capitalize()} Target - Current = {tp_display} 
- ${current_price:.2f} = ${target_price - current_price:.2f}
Weighted Delta = Price Delta × Confidence × Profit Ratio = ${target_price - 
current_price:.2f} × {confidence/100:.2f} × {profit_ratio} = ${(target_price - 
current_price) * (confidence/100) * profit_ratio:.2f}
Expected Profit % = (Weighted Delta / Current) × 100 = 
{expected_profit_percent:.1f}%"""
```
Ici, si current_price est None, cela plante car `${current_price:.2f}`. Mais on
est dans la branche `if target_price is not None and current_price:`, donc 
current_price est défini. OK.

Ligne ~780 : 
```python
boost_calc = f"""Step 3 - Price Target Boost:
Price target data unavailable - boost set to {price_target_boost:.1f}%"""
```
price_target_boost est 0.0 dans ce cas. OK.

Ligne ~926 : `_create_expert_recommendation`
```python
details=recommendation_data['details'][:100000] if 
recommendation_data['details'] else None,
```
OK, tronque les détails.

Ligne ~943 : `data={...}` OK.

Ligne ~960 : `_format_analyst_details_md`
```python
def _format_analyst_details_md(self, symbol: str, limit: int = 15) -> 
Optional[str]:
```
Live-only, OK.

Ligne ~1001 : `_store_analysis_outputs`
```python
session = get_db()
```
Puis `session.close()` dans finally. OK.

Ligne ~1078 : `run_analysis`
```python
if consensus_data is not None and not current_price:
    raise ValueError(f"Unable to get current price for {symbol}")
```
Problème : si `current_price` est 0.0, `not current_price` est True et cela 
lève une erreur. Un prix de 0 est invalide mais le message serait trompeur. Pas
critique.

Ligne ~1083 : `rec = self._process(bundle, settings, as_of=None)`
OK.

Ligne ~1088 : `if rec.skip:`
Le traitement du skip_reason. Problème : il y a trois skip_reasons possibles : 
"no consensus data", "insufficient price targets", "insufficient analysts". Le 
code ne gère que deux cas. Si skip_reason est "insufficient price targets", il 
tombe dans le else et affiche "insufficient analysts". C'est un bug médium.

```python
if rec.skip_reason == "no consensus data":
    # ...
else:  # "insufficient analysts"
    analyst_count = self._count_analysts(upgrade_data)
    # ...
```
Si `rec.skip_reason == "insufficient price targets"`, il va dans le else et 
prétend que c'est "insufficient analysts". C'est incorrect.

Ligne ~1170 : `recommendation_data = rec.raw_outputs["calc"]`
OK.

Ligne ~1251 : `_render_completed`
Affichage UI, pas de bug financier critique.

Ligne ~1401 : 
```python
applied_boost = confidence_breakdown.get('applied_boost', price_target_boost)
```
OK.

Ligne ~1430 :
```python
calculated_confidence = base_confidence + applied_boost
clamped_confidence = max(0.0, min(100.0, calculated_confidence))
```
OK.

Ligne ~1437 :
```python
if abs(confidence - clamped_confidence) > 0.1:
```
OK.

### Fichier 2 : metric_store.py

Ligne ~76 : `fetch_historical_market_cap`
```python
r = fmp_http_get(f"{_HIST_MCAP_URL}/{symbol}",
                 params={"apikey": api_key, "from": start, "to": end, "limit": 
100000},
                 endpoint="historical-market-cap", timeout=30)
```
OK.

Ligne ~86 : 
```python
df = pd.DataFrame(
    [{"date": x.get("date"), "market_cap": x.get("marketCap")}
     for x in rows if isinstance(x, dict) and x.get("date")]
)
```
OK.

Ligne ~107 : `fetch_historical_float`
```python
from ba2_common.core.provider_utils import statement_effective_date
```
OK.

Ligne ~131 : `statement_effective_date(x)` retourne une date. Si c'est un 
datetime aware, `.strftime` fonctionne. OK.

Ligne ~150 : `weinstein_stage_series`
```python
stage = pd.Series(1.0, index=close.index)          # default: basing
stage = stage.where(~(above & rising), 2.0)
stage = stage.where(~((~above) & falling), 4.0)
stage = stage.where(~(above & ~rising), 3.0)
```
Problème potentiel : `stage.where` remplace les valeurs où la condition est 
False. Donc :
- stage = 1 partout
- Où `above & rising` est True, garde 2, sinon garde l'actuel (1)
- Où `(~above) & falling` est True, garde 4, sinon garde l'actuel
- Où `above & ~rising` est True, garde 3, sinon garde l'actuel

Ordre :
1. 2 si above & rising
2. 4 si not above & falling
3. 3 si above & not rising
4. 1 sinon

Cela correspond à :
- above & rising → 2
- not above & falling → 4  
- above & not rising → 3
- not above & not falling → 1 (basing)

C'est correct car si not above et falling → 4. Si not above et pas falling → 1.
Si above et rising → 2. Si above et pas rising → 3. Les cas sont mutuellement 
exclusifs. OK.

Ligne ~192 : `_drop_pct`
```python
peak = close.rolling(max(1, int(window)), min_periods=1).max()
return ((peak - close) / peak * 100.0).where(peak > 0, 0.0)
```
Problème : `window=1` signifie que le peak est le close actuel, donc drop_pct =
0 toujours. Le commentaire le dit. Pas un bug si c'est intentionnel.

Ligne ~203 : `_drop_pct_windows`
```python
for w in range(1, max(1, int(max_window)) + 1):
    if w >= 2:
        k = w - 1
        shifted = _np.full(n, _np.nan)
        if k < n:
            shifted[k:] = arr[:n - k]
        peak = _np.fmax(peak, shifted)
```
Problème : pour w=1, peak = arr (close actuel). Pour w=2, peak = max(arr, arr 
shifté de 1). C'est correct. Cependant, pour w=1, cela donne toujours drop=0. 
Si max_window=1, c'est correct. Mais si drop_days=1 dans compute_daily_metrics,
price_drop_pct sera toujours 0. C'est peut-être un problème si l'utilisateur 
s'attend à ce que drop_days=1 signifie "1 jour de lookback" (c'est-à-dire drop 
par rapport au close d'hier). Le commentaire indique explicitement que window 1
signifie peak==close, donc c'est un choix de conception.

Ligne ~232 : `momentum_12_1_series`
```python
p_start = c.shift(lookback - 1)
p_end = c.shift(skip)
mom = p_end / p_start - 1.0
return mom.where(p_start > 0)
```
OK.

Ligne ~251 : `compute_daily_metrics`
```python
avg_vol_prior = vol.shift(1).rolling(rvol_window, min_periods=1).mean()
rvol = (vol / avg_vol_prior).where(avg_vol_prior > 0, 0.0)
```
Problème potentiel : division par zéro gérée par `.where`. OK.

```python
volume = vol.rolling(vol_window, min_periods=1).mean()
```
OK.

```python
if market_cap_series is not None and len(market_cap_series):
    mcap = market_cap_series.reindex(close.index, method="ffill")
elif shares:
    mcap = close * shares
else:
    mcap = pd.Series(float("nan"), index=close.index)
```
Problème : `shares` est un float (dernier dépôt). Cela calcule market_cap = 
close * shares_currentes. C'est un fallback, mais ce n'est pas point-in-time. 
Le commentaire le dit ("legacy"). Pas un bug si c'est intentionnel.

```python
if float_series is not None and len(float_series):
    flt = float_series.reindex(close.index, method="ffill")
else:
    flt = pd.Series(float("nan"), index=close.index)
```
OK.

Ligne ~300 : `out` DataFrame
OK.

Ligne ~334 : `build_store`
```python
grid = scan_date_grid(start, end, cadence_days)
want_months = sorted({d.strftime("%Y-%m") for d in grid})
have = existing_months(store_dir)
todo_months = [m for m in want_months if m not in have]
```
Problème : si un mois est partiellement construit (par exemple, crash au 
milieu), il est considéré comme "have" et sauté entièrement. Cela pourrait 
laisser des données incomplètes. Pas un bug critique car les données sont 
partitionnées par mois.

Ligne ~350 : `_build_one_once`
```python
df = ohlcv_get(sym, end)
```
OK.

Ligne ~361 : `m = m.reindex(grid_todo, method="ffill")`
OK.

Ligne ~386 : `_build_one`
```python
for attempt in range(symbol_retries + 1):
    try:
        return _build_one_once(sym, srow)
    except Exception as e:
        last_err = e
        if attempt < symbol_retries:
            time.sleep(1.5 * (attempt + 1))
```
OK.

Ligne ~405 : `_flush`
```python
write_partitions(store_dir, pd.concat(frames, ignore_index=True),
                 part_name=f"part-{flush_seq:05d}.parquet")
```
OK.

Ligne ~423 : `load_store`
```python
parts = sorted(glob.glob(os.path.join(store_dir, "ym=*", "*.parquet")))
df = pd.concat((pd.read_parquet(p) for p in parts), ignore_index=True)
```
Problème potentiel : `pd.concat` avec un générateur — si `parts` est vide, cela
plante. Mais il y a un check avant.

Ligne ~452 : `recompute_price_drop_columns`
```python
store = store.set_index(["symbol", "date"])
for c in drop_cols:
    if c not in store.columns:
        store[c] = float("nan")
if updates:
    upd = pd.concat(updates, ignore_index=True).set_index(["symbol", "date"])
    store.update(upd)
```
Problème potentiel : `store.update(upd)` met à jour les valeurs non-NaN. Mais 
si `upd` contient des NaN, ils ne remplaceront pas les valeurs existantes. 
Cependant, `upd` est construit à partir de `asof[c].to_numpy()`, qui peut 
contenir des NaN si l'historique est insuffisant. Ces NaN ne remplaceront pas 
les anciennes valeurs. C'est peut-être intentionnel (ne pas écraser avec des 
NaN).

Ligne ~481 : 
```python
write_partitions(store_dir, store, part_name="part.parquet")
```
Cela écrit toutes les partitions. Mais `write_partitions` regroupe par `ym`. 
OK.

Ligne ~486 :
```python
for m in sorted(store["date"].str.slice(0, 7).unique()):
```
OK.

Ligne ~494 : `recompute_momentum_column`
Similaire à recompute_price_drop_columns. OK.

Ligne ~542 : `screen_universe_for_day`
```python
def _ge(col, key):
    nonlocal d
    v = settings.get(key)
    if v is not None and float(v) > 0:
        d = d[d[col] >= float(v)]
```
Problème : les filtres max (par exemple market_cap_max) utilisent `_le` qui est
:
```python
def _le(col, key):
    nonlocal d
    v = settings.get(key)
    if v is not None and float(v) > 0:
        d = d[d[col] <= float(v)]
```
Attendez, `_le` vérifie `float(v) > 0`. Si `market_cap_max` est 0, le filtre 
est ignoré. Mais si l'utilisateur veut explicitement `market_cap_max = 0` 
(exclure toutes les entreprises avec market cap > 0), cela ne fonctionne pas. 
Cependant, 0 comme max n'a généralement pas de sens. Mais c'est une convention 
— 0 signifie "désactivé".

Ligne ~570 : 
```python
_fmin = settings.get("float_min")
if _fmin is not None and float(_fmin) > 0:
    d = d[(d["float_shares"] >= float(_fmin)) | d["float_shares"].isna()]
```
OK, NaN passe.

Ligne ~580 : 
```python
_drop_col = "price_drop_pct"
_y = settings.get("price_drop_days")
if _y is not None and int(float(_y)) >= 2 and 
f"price_drop_pct_{int(float(_y))}" in d.columns:
    _drop_col = f"price_drop_pct_{int(float(_y))}"
_ge(_drop_col, "price_drop_pct")
```
Problème potentiel : si `price_drop_days=1`, il utilise `price_drop_pct` 
(drop_days par défaut, probablement 5). Si l'utilisateur veut vraiment une 
fenêtre de 1 jour, il n'a pas de colonne `price_drop_pct_1`. Mais selon le 
commentaire, window 1 = 0 anyway.

Ligne ~598 : 
```python
sort_col = settings.get("sort_metric") or "market_cap"
if sort_col not in d.columns:
    sort_col = "market_cap"
d = d.sort_values(sort_col, ascending=False)
```
OK.

Ligne ~609 : `screen_universe_as_of`
```python
dates = store_df["date"]
prior = dates[dates <= as_of_day]
```
Problème : si `store_df["date"]` n'est pas de type string/datetime cohérent, la
comparaison peut échouer. Si c'est une chaîne au format "YYYY-MM-DD", la 
comparaison lexicographique fonctionne. OK.

Ligne ~618 : `metrics_as_of`
```python
dates = store_df["date"]
prior = dates[dates <= as_of_day]
```
Même chose.

Ligne ~639 : `screened_symbol_union`
```python
dates = sorted({str(d) for d in store_df["date"].unique()})
relevant = [d for d in dates if lo <= d <= end_day]
```
OK.

### Fichier 3 : fmp_common.py

Ligne ~39 : `_tls = threading.local()`
OK, thread-local.

Ligne ~67 : `hermetic_fmp_history`
```python
prev = _is_hermetic_fmp_history()
_tls.hermetic_fmp_history = True
try:
    yield
finally:
    _tls.hermetic_fmp_history = prev
```
OK, réentrant.

Ligne ~88 : `persist_empty_sentinel`
```python
global _PERSIST_EMPTY_SENTINEL
prev = _PERSIST_EMPTY_SENTINEL
_PERSIST_EMPTY_SENTINEL = True
try:
    yield
finally:
    _PERSIST_EMPTY_SENTINEL = prev
```
Problème : `_PERSIST_EMPTY_SENTINEL` est global, pas thread-local. Si deux 
threads utilisent `persist_empty_sentinel` simultanément, ils peuvent se 
marcher dessus. Par exemple, thread A entre, met à True, thread B entre, met à 
True, thread A sort, remet à False, mais thread B est encore en mode persist. 
C'est un bug de concurrence MED.

Ligne ~123 : `fmp_history_disk_cached`
```python
if not _is_ttl_frozen():
    return fetch_fn()  # live path: never cache to disk; always pull fresh from
the API
```
OK.

Ligne ~130 : `_HISTORY_MEM_CACHE.get_or_call(...)`
OK.

Ligne ~155 : `_fmp_history_disk_read_or_fetch`
```python
if _os.path.exists(path) and (_is_hermetic_fmp_history()
                              or (_time.time() - _os.path.getmtime(path)) / 
86400.0 <= max_age_days):
```
OK.

Ligne ~175 : 
```python
to_persist = data if data else ([] if _PERSIST_EMPTY_SENTINEL else None)
```
Problème : si `data` est un dict vide `{}` ou une liste vide `[]` ou `None`, il
n'est pas persisté (sauf en mode persist_empty_sentinel). Mais si `data` est un
DataFrame vide ? Non, ici c'est du JSON. Si `data` est `0` ou `False`, il 
serait traité comme falsy. Mais les données FMP sont généralement des 
listes/dict. Pas un gros problème.

Ligne ~181 : 
```python
tmp = f"{path}.{_os.getpid()}.tmp"
```
Problème de concurrence : si deux threads du même processus écrivent le même 
fichier simultanément, elles utilisent le même tmp (même pid). Cela pourrait 
entraîner une corruption. Le commentaire dit "atomic tmp+replace" mais 
n'utilise que le pid, pas le thread id. C'est un bug de concurrence LOW à MED.

Ligne ~217 : `TTLCache.get_or_call`
```python
with self._lock:
    item = self._store.get(key)
    if item is not None and (_is_ttl_frozen() or self._clock() < item[1]):
        return item[0]
value = fn()  # network call outside the lock
with self._lock:
    self._store[key] = (value, self._clock() + self._ttl)
return value
```
Problème de concurrence : entre le `return` du lock et le `value = fn()`, 
plusieurs threads peuvent appeler `fn()` pour la même clé. Ce n'est pas un gros
problème car c'est un cache best-effort, mais cela peut causer des requêtes 
redondantes sous forte concurrence. Pas un bug, juste inefficace.

Ligne ~270 : `fmp_http_get`
```python
resp.raise_for_status()
return resp
```
OK.

Ligne ~289 : `fmp_list_call`
```python
for attempt in range(total_attempts):
    if attempt > 0:
        sleep(delays[attempt - 1])
```
OK.

### Fichier 4 : FactorRanker/__init__.py

Ligne ~156 : `_metric_store_settings`
```python
return {
    "market_cap_min": g("screener_market_cap_min"),
    ...
}
```
Problème : certaines clés reconnues par metric_store ne sont pas traduites, 
comme `float_min`, `float_max`, `price_drop_days`, `weinstein_stage2_only`. 
Attendez, `weinstein_stage2_only` est là. Mais 
`float_min`/`float_max`/`price_drop_days` ne le sont pas. Donc si l'utilisateur
configure `screener_float_min`, il n'est pas passé au metric store. C'est un 
bug si le metric store supporte ces filtres.

Regardons `_METRIC_STORE_KEYS` dans metric_store.py :
```python
"market_cap_min", "market_cap_max", "price_min", "price_max",
"volume_min", "volume_max", "float_min", "float_max",
"relative_volume_min", "price_drop_pct", "price_drop_days",
"weinstein_stage2_only", "max_stocks", "sort_metric",
```

Dans `_metric_store_settings` de FactorRanker :
- market_cap_min, market_cap_max ✓
- price_min, price_max ✓
- volume_min, volume_max ✓
- relative_volume_min ✓
- price_drop_pct ✓
- max_stocks ✓
- sort_metric ✓
- weinstein_stage2_only ✓
- **float_min, float_max, price_drop_days manquants**

C'est un bug MED : les filtres float et la fenêtre de drop ne sont pas transmis
au metric store.

Ligne ~186 : `_screen_universe`
```python
try:
    from ba2_providers.screener import metric_store as ms
    df = ms.load_store(store)
    day = (as_of or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    syms = ms.screen_universe_as_of(df, day, self._metric_store_settings())
```
OK, mais manque `float_min`, `float_max`, `price_drop_days`.

Ligne ~246 : `_resolve_universe_source`
```python
ism = (self.settings.get("instrument_selection_method") or "").lower()
if ism in ("static", "screener"):
    return ism
return (self.get_setting_with_interface_default("universe_source") or 
"static").lower()
```
OK.

Ligne ~273 : `_resolve_universe`
```python
min_price = float(self.get_setting_with_interface_default("min_price") or 0.0)
if min_price > 0 and universe:
    ...
    price = account.get_instrument_current_price(sym)
```
Problème potentiel : en backtest, `account.get_instrument_current_price(sym)` 
pourrait ne pas être point-in-time (utiliser le prix courant au lieu de as_of).
Cependant, c'est dans `_resolve_universe`, appelé par `_gather` avec `as_of`. 
Le `account` pourrait être le compte live. En backtest, le `providers` bundle 
devrait être utilisé. Mais ici, on utilise 
`account.get_instrument_current_price(sym)` qui est une méthode live. Cela 
pourrait causer une fuite de données futures en backtest si `account` retourne 
le prix courant. Mais c'est probablement que le compte backtest retourne le 
prix as_of. Difficile à dire sans voir le code du compte.

Ligne ~296 : `_factor_weights`
OK.

Ligne ~378 : `_store_factor_inputs`
```python
store = (self.get_setting_with_interface_default("screener_store") or 
"").strip()
if not store:
    return None, None
```
OK.

```python
rows = ms.metrics_as_of(df, as_of.strftime("%Y-%m-%d"), ["momentum_12_1", 
"close"])
```
OK.

```python
if not rows or not all(s in rows for s in universe):
    return None, None
```
OK.

Ligne ~394 : 
```python
if all("momentum_12_1" in rows[s] for s in universe):
    momentum = {s: (_num(rows[s].get("momentum_12_1")) or 0.0) for s in 
universe}
```
OK.

```python
price_as_of = None
if all(_num(rows[s].get("close")) is not None for s in universe):
    price_as_of = {s: _num(rows[s].get("close")) for s in universe}
```
OK.

Ligne ~411 : `_gather`
```python
settings = self._gather_settings
```
Si `_gather_settings` n'est pas défini, cela plante. Mais il est défini par 
`run_analysis` et `analyze_as_of`. OK.

Ligne ~425 : 
```python
precomputed_momentum, price_as_of = self._store_factor_inputs(universe, as_of)
```
OK.

Ligne ~427 : 
```python
for name, (fetch_name, calc) in _FACTOR_PIPELINE.items():
    if float(weights.get(name, 0.0)) == 0.0:
        continue
```
OK.

Ligne ~430 : 
```python
if name == "momentum" and precomputed_momentum is not None:
    factors[name] = {s: precomputed_momentum.get(s, 0.0) for s in universe}
    continue
```
Problème potentiel : `precomputed_momentum` est un dict {symbol: momentum}. Si 
un symbole de l'univers n'est pas dans precomputed_momentum, il obtient 0.0. 
Mais `_store_factor_inputs` a vérifié que tous les symboles de l'univers sont 
dans `rows`. Donc cela ne devrait pas arriver. OK.

Ligne ~438 : 
```python
factors[name] = self._compute_factor(
    name, fetch_name, calc, universe, as_of=as_of,
    pead_drift_window_days=pead_window, ohlcv_provider=ohlcv_provider,
    price_as_of=(price_as_of if name == "value" else None))
```
OK.

Ligne ~444 : `holdings = self._gather_holdings() if universe else []`
OK.

Ligne ~452 : `_gather` retourne `"prices": {}`
OK.

Ligne ~458 : `_process`
```python
if not data_bundle["universe"]:
    return Recommendation(... skip=True ...)
if not data_bundle["factors"]:
    return Recommendation(... skip=True ...)
```
OK.

Ligne ~473 : 
```python
comp = composite_score(data_bundle["factors"], weights, winsorize_pct)
ranked = rank_symbols(comp)
targets = long_only_top_n(
    ranked, comp,
    top_n=int(settings["top_n"]),
    weighting=settings["weighting"],
    max_weight_per_name=float(settings["max_weight_per_name"]),
    gross_exposure=gross_exposure,
)
```
OK.

Ligne ~505 : `analyze_as_of`
```python
settings = dict(context.settings)
settings.setdefault("_factor_weights", {
    name: float(
        settings.get(f"factor_weight_{name}",
                     self.get_setting_with_interface_default(f"factor_weight_{n
ame}"))
        or 0.0)
    for name in _FACTOR_PIPELINE
})
self._gather_settings = settings
```
OK.

Ligne ~520 : `_compute_factor`
```python
fetch_kwargs = {"as_of": as_of}
if ohlcv_provider is not None and _accepts_kwarg(fetcher, "ohlcv_provider"):
    fetch_kwargs["ohlcv_provider"] = ohlcv_provider
if price_as_of is not None and _accepts_kwarg(fetcher, "price_as_of"):
    fetch_kwargs["price_as_of"] = price_as_of
inputs = fetcher(universe, **fetch_kwargs)
```
OK.

Ligne ~529 : 
```python
if name == "pead":
    if pead_drift_window_days is None:
        pead_drift_window_days = int(
            self.get_setting_with_interface_default("pead_drift_window_days"))
    return calc(inputs, drift_window_days=int(pead_drift_window_days))
```
OK.

Ligne ~540 : `_build_book`
```python
held = held or set()
if gross_exposure is None:
    gross_exposure = 
float(self.get_setting_with_interface_default("gross_exposure"))
```
OK.

Ligne ~549 : 
```python
zscores = {
    name: cross_sectional_zscore(vals, winsorize_pct)
    for name, vals in factor_values.items()
}
```
OK.

Ligne ~572 : 
```python
return {
    "rebalanced_at": datetime.now(timezone.utc).isoformat(),
```
Problème potentiel : en backtest, `rebalanced_at` utilise `datetime.now()` au 
lieu de `as_of`. C'est une fuite de temps réel dans l'état du backtest. Pas un 
bug financier mais une incohérence d'audit.

Maintenant, devons-nous analyser plus en profondeur ? L'utilisateur demande un 
audit rigoureux. Je dois identifier les problèmes les plus sérieux.

Prenons FMPRating plus en détail.

Ligne ~183 : `_gather` live path
```python
consensus_data["targetCount"] = self._count_targets_in_window(
    pt_history, datetime.now(timezone.utc), _QUARTER_DAYS)
```
Le problème ici est que `datetime.now(timezone.utc)` est utilisé pour compter 
les targets dans la fenêtre du dernier quartier. Mais plus tard, 
`_count_targets_in_window` utilise ce ref_date. Cela signifie que chaque appel 
live utilise un `now` légèrement différent. Pas critique, mais pas idéal.

Ligne ~1088 : `run_analysis` skip handling
```python
if rec.skip:
    if rec.skip_reason == "no consensus data":
        ...
    else:  # "insufficient analysts"
        analyst_count = self._count_analysts(upgrade_data)
        ...
```
Le bug ici est clair : il y a trois raisons de skip possibles mais seules deux 
sont gérées. Si `skip_reason == "insufficient price targets"`, le code 
affichera un message d'erreur incorrect et utilisera 
`self._count_analysts(upgrade_data)` qui pourrait être None.

Ligne ~148 : `_count_targets_in_window`
```python
floor = ref_date - timedelta(days=int(window_days))
```
Si `window_days` est négatif, floor est dans le futur. Mais window_days est 
toujours positif.

Ligne ~538 : `_calculate_recommendation`
```python
if current_price and target_low and target_consensus:
```
Si `current_price` est None ou 0, pas de boost. OK.

Ligne ~630-650 : 
```python
if signal == OrderRecommendation.BUY and target_price and current_price:
    price_delta = target_price - current_price
    weighted_delta = price_delta * (confidence / 100.0) * profit_ratio
    expected_profit_percent = (weighted_delta / current_price) * 100
elif signal == OrderRecommendation.SELL and target_price and current_price:
    price_delta = current_price - target_price
    weighted_delta = price_delta * (confidence / 100.0) * profit_ratio
    expected_profit_percent = (weighted_delta / current_price) * 100
```
Problème : `expected_profit_percent` n'est pas borné. Si `target_price` est 
1000 et `current_price` est 10, on peut avoir un expected_profit_percent de 
plusieurs milliers de pourcents. Cela peut sembler correct mathématiquement, 
mais c'est une valeur extrême. Pas un bug.

Ligne ~709 : 
```python
profit_calc = f"""... {target_price - current_price:.2f} ..."""
Si target_price ou current_price sont des NaN, cela plante. Mais ils sont 
vérifiés.

Maintenant regardons metric_store.py plus attentivement.

Ligne ~76 : `fetch_historical_market_cap`
```python
params={"apikey": api_key, "from": start, "to": end, "limit": 100000}
```
La limite est 100000. Si la période est longue, cela pourrait être insuffisant.
FMP historical-market-cap retourne une ligne par jour. 100000 jours = ~273 ans.
Donc OK.

Ligne ~90 : `_write_parquet_atomic`
```python
tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
```
C'est correct pour la concurrence (pid + thread id).

Ligne ~181 de fmp_common.py : 
```python
tmp = f"{path}.{_os.getpid()}.tmp"
```
Seulement pid, pas thread id. C'est un problème de concurrence si deux threads 
du même processus écrivent la même clé.

Ligne ~88 de fmp_common.py : `persist_empty_sentinel` global non thread-safe.

Ligne ~203 de metric_store.py : `_drop_pct_windows`
```python
peak = _np.fmax(peak, shifted)
```
Utilise fmax élément par élément. C'est correct.

Ligne ~215 : 
```python
out[w] = pd.Series(dp, index=close.index)
```
dp contient peut-être des NaN si peak est NaN. C'est correct.

Ligne ~251 : `compute_daily_metrics`
```python
mcap = market_cap_series.reindex(close.index, method="ffill")
```
Si `market_cap_series` a des trous, ffill remplit avec la dernière valeur 
connue. C'est correct pour point-in-time.

Ligne ~259 : 
```python
flt = float_series.reindex(close.index, method="ffill")
```
OK.

Ligne ~261 : 
```python
stage = weinstein_stage_series(close)
```
OK.

Ligne ~262 : 
```python
momentum = momentum_12_1_series(close)
```
OK.

Ligne ~275 : 
```python
out[_col] = _ser
```
OK.

Ligne ~334 : `build_store`
```python
universe = enumerate_universe(api_key, market_cap_min, price_min, volume_min)
static_by_sym = {r["symbol"]: r for r in universe}
```
Si deux symboles ont le même nom, le dernier écrase. Mais les symboles 
devraient être uniques.

Ligne ~377 : `_build_one`
```python
except Exception as e:
    last_err = e
    if attempt < symbol_retries:
        time.sleep(1.5 * (attempt + 1))
```
OK.

Ligne ~405 : `_flush`
```python
write_partitions(store_dir, pd.concat(frames, ignore_index=True),
                 part_name=f"part-{flush_seq:05d}.parquet")
written += len(frames)
```
`written += len(frames)` compte le nombre de DataFrames, pas le nombre de 
symboles. Si chaque frame correspond à un symbole, c'est correct. Oui, c'est le
cas.

Ligne ~423 : `load_store`
```python
df = pd.concat((pd.read_parquet(p) for p in parts), ignore_index=True)
```
OK.

Ligne ~452 : `recompute_price_drop_columns`
```python
store = pd.concat((pd.read_parquet(p) for p in parts), ignore_index=True)
store["date"] = store["date"].astype(str).str.slice(0, 10)
```
OK.

Ligne ~458 : 
```python
sdates = sorted(store.loc[store["symbol"] == sym, "date"].unique())
```
OK.

Ligne ~482 : `write_partitions(store_dir, store, part_name="part.parquet")`
Cela va écrire toutes les lignes, y compris celles des symboles skipped. Donc 
les skipped ne perdent pas leurs anciennes valeurs. OK.

Ligne ~494 : `recompute_momentum_column`
Similaire.

Ligne ~542 : `screen_universe_for_day`
```python
def _ge(col, key):
    nonlocal d
    v = settings.get(key)
    if v is not None and float(v) > 0:
        d = d[d[col] >= float(v)]
```
Problème : la condition `float(v) > 0` signifie que les filtres à 0 sont 
ignorés. Mais si `market_cap_min` est 0, c'est OK car tout est >= 0. Si 
`market_cap_max` est 0, c'est intentionnellement "pas de limite" selon la 
convention. Mais ce n'est pas documenté ici. Cependant, c'est une convention 
commune.

Attendez, `_le` est :
```python
def _le(col, key):
    nonlocal d
    v = settings.get(key)
    if v is not None and float(v) > 0:
        d = d[d[col] <= float(v)]
```
Donc `market_cap_max = 0` est ignoré. Si un utilisateur veut vraiment un max de
0, il ne peut pas. Mais ce n'est pas un cas d'utilisation réaliste.

Ligne ~570 : 
```python
if _fmin is not None and float(_fmin) > 0:
    d = d[(d["float_shares"] >= float(_fmin)) | d["float_shares"].isna()]
```
OK.

Ligne ~574 : 
```python
if _fmax is not None and float(_fmax) > 0:
    d = d[(d["float_shares"] <= float(_fmax)) | d["float_shares"].isna()]
```
OK.

Ligne ~609 : `screen_universe_as_of`
```python
dates = store_df["date"]
prior = dates[dates <= as_of_day]
```
Problème potentiel : si `store_df["date"]` est de type datetime64[ns] et 
`as_of_day` est une chaîne, la comparaison peut échouer ou avoir un 
comportement inattendu. Mais généralement, pandas convertit. OK.

Ligne ~618 : `metrics_as_of`
Similaire.

Ligne ~639 : `screened_symbol_union`
```python
dates = sorted({str(d) for d in store_df["date"].unique()})
lo = prior[-1] if prior else dates[0]
relevant = [d for d in dates if lo <= d <= end_day]
```
OK.

Maintenant fmp_common.py

Ligne ~39 : `_tls = threading.local()`
OK.

Ligne ~88 : `persist_empty_sentinel` global. BUG de concurrence.

Ligne ~181 : tmp filename utilise seulement pid. BUG de concurrence.

Ligne ~217 : `TTLCache` — pas de déduplication des appels concurrents. Pas un 
bug, mais inefficace.

Ligne ~270 : `fmp_http_get`
```python
resp.raise_for_status()
return resp
```
Si le status est 200 mais le corps contient une erreur FMP, cela ne sera pas 
traité. Mais `fmp_http_get` est pour les endpoints qui retournent des vrais 
codes d'erreur HTTP, pas les dicts d'erreur. OK.

Ligne ~289 : `fmp_list_call`
```python
if result is None:
    return []
```
OK.

Ligne ~296 :
```python
if isinstance(result, dict):
    err = _fmp_error_message(result)
    if err is not None:
```
OK.

Ligne ~320 :
```python
raise FMPError(...)
```
OK.

FactorRanker/__init__.py

Ligne ~156 : `_metric_store_settings` manque `float_min`, `float_max`, 
`price_drop_days`. BUG.

Ligne ~273 : `_resolve_universe`
```python
price = account.get_instrument_current_price(sym)
```
En backtest, si `account` est le compte live, c'est une fuite. Mais sans voir 
le code du compte, c'est difficile à confirmer.

Ligne ~378 : `_store_factor_inputs`
```python
if not rows or not all(s in rows for s in universe):
    return None, None
```
OK.

Ligne ~430 : `_gather`
```python
if name == "momentum" and precomputed_momentum is not None:
    factors[name] = {s: precomputed_momentum.get(s, 0.0) for s in universe}
    continue
```
OK.

Ligne ~444 : `_gather`
```python
"prices": {},
```
OK.

Ligne ~458 : `_process`
```python
if not data_bundle["factors"]:
    return Recommendation(... skip=True ...)
```
OK.

Ligne ~480 : `_process`
```python
targets = long_only_top_n(
    ranked, comp,
    top_n=int(settings["top_n"]),
    weighting=settings["weighting"],
    max_weight_per_name=float(settings["max_weight_per_name"]),
    gross_exposure=gross_exposure,
)
```
OK.

Ligne ~505 : `analyze_as_of`
```python
settings.setdefault("_factor_weights", {...})
```
OK.

Ligne ~572 : `_build_book`
```python
"rebalanced_at": datetime.now(timezone.utc).isoformat(),
```
En backtest, utilise l'heure actuelle. Pas financier, mais d'audit.

Maintenant, devons-nous mentionner des problèmes dans FMPRating concernant le 
fait que `_process` est appelé avec `as_of=None` en live, mais `_gather` a 
fetché `analyst_grades` avec `datetime.now`. Si l'analyse est retardée, il 
pourrait y avoir une incohérence mineure. Pas un bug majeur.

Un autre problème dans FMPRating : ligne ~270
```python
if analyst_count < min_analysts:
    return Recommendation(...)
```
Le `analyst_count` est calculé à partir de `analyst_grades` si max_age > 0, 
sinon à partir de `upgrade_data`. Mais dans `_calculate_recommendation`, 
`analyst_count` est recalculé à partir de `upgrade_data` (ligne ~475). Donc si 
max_age > 0 et que le count filtré est >= min_analysts, mais que le count non 
filtré (upgrade_data) est < min_analysts, alors `_calculate_recommendation` 
retournera un signal HOLD avec 20% confiance au lieu du signal calculé. C'est 
un bug sérieux.

Attendez, regardons :
- `_process` calcule `analyst_count` avec `_count_recent_analysts` si max_age >
0, sinon `_count_analysts(upgrade_data)`.
- Si `analyst_count >= min_analysts`, il appelle `_calculate_recommendation`.
- Dans `_calculate_recommendation`, ligne ~470 :
```python
if upgrade_data and len(upgrade_data) > 0:
    latest_grade = upgrade_data[0]
    strong_buy = latest_grade.get('strongBuy', 0)
    ...
    analyst_count = strong_buy + buy + hold + sell + strong_sell
```
- Puis ligne ~485 :
```python
if analyst_count < min_analysts:
    return {
        'signal': OrderRecommendation.HOLD,
        'confidence': 20.0,
        ...
    }
```

Donc si max_age > 0 et que le count récent >= min_analysts, mais le count 
non-filtré (upgrade_data) < min_analysts, `_calculate_recommendation` 
retournera HOLD avec 20% confiance, ce qui écrase le vrai signal. C'est un bug 
HIGH.

Mais attendez, `upgrade_data` est soit :
- live : `_fetch_upgrade_downgrade` qui retourne les données consensus 
actuelles
- backtest : `_counts_as_of(grades_history, as_of)` qui retourne les counts 
as-of

Dans le backtest avec max_age > 0, `upgrade_data` est les counts as-of (non 
filtrés par recency), et `analyst_count` est les counts filtrés par recency. Si
les counts filtrés >= min_analysts mais les counts non filtrés < min_analysts, 
alors le bug se produit.

C'est un bug réel et important.

De plus, `_calculate_recommendation` ne connaît pas le 
`max_analyst_age_months`, donc il ne peut pas appliquer le filtre de recency. 
La correction serait de passer `analyst_count` déjà calculé à 
`_calculate_recommendation` ou de fusionner le filtre.

Un autre problème dans FMPRating : le `targetCount` pour le live path est 
calculé avec `datetime.now(timezone.utc)`, mais le consensus_data est un 
snapshot actuel. Si l'analyse est exécutée à un moment différent de 
"maintenant", il pourrait y avoir une légère incohérence. Pas critique.

Un autre problème dans `_calculate_recommendation` : ligne ~534
```python
if current_price and target_low and target_consensus:
```
Ici, `current_price` pourrait être 0 (falsy), ce qui empêcherait le boost. Mais
un prix de 0 est invalide.

Ligne ~640 :
```python
if signal == OrderRecommendation.BUY and target_price and current_price:
```
Même chose.

Ligne ~701 :
```python
tc_pct = f"{((target_consensus - current_price) / current_price * 100):.1f}% 
from current" if target_consensus is not None and current_price else "N/A"
```
Si `current_price` est 0, on a "N/A". OK.

Maintenant, devons-nous mentionner le problème de `current_price` en tant que 
float falsy ? C'est un cas limite. Je vais peut-être le mentionner comme LOW.

Un autre problème dans FMPRating : ligne ~1108
```python
else:  # "insufficient analysts"
    analyst_count = self._count_analysts(upgrade_data)
```
Si `upgrade_data` est None, `_count_analysts` retourne 0. OK. Mais le message 
sera incorrect pour "insufficient price targets".

Maintenant, regardons plus en détail `_counts_as_of` dans FMPRating.

Ligne ~695 :
```python
eligible = [
    r for r in grades_history
    if (d := _memo_provider_date(r, "date")) is not None and d <= as_of
]
```
OK.

Ligne ~700 :
```python
latest = max(eligible, key=lambda r: _memo_provider_date(r, "date"))
```
OK.

Ligne ~703 :
```python
def _val(row, names):
    for n in names:
        if n in row and row[n] is not None:
            try:
                return int(row[n])
            except (TypeError, ValueError):
                return 0
    return 0
```
OK.

Ligne ~714 :
```python
return [{k: _val(latest, aliases) for k, aliases in 
cls._GRADES_FIELD_ALIASES.items()}]
```
OK.

`_consensus_target_as_of` :
Ligne ~728 :
```python
floor = as_of - timedelta(days=int(window_days))
targets = []
for r in price_target_history:
    d = _memo_provider_date(r, "publishedDate")
    if d is None or d > as_of or d < floor:
        continue
```
OK.

Ligne ~739 :
```python
targets_sorted = sorted(targets)
n = len(targets_sorted)
if n % 2 == 1:
    median = targets_sorted[n // 2]
else:
    median = (targets_sorted[n // 2 - 1] + targets_sorted[n // 2]) / 2.0
```
OK.

Ligne ~748 :
```python
return {
    "targetConsensus": sum(targets) / n,
    "targetHigh": max(targets),
    "targetLow": min(targets),
    "targetMedian": median,
    "targetCount": n,
}
```
OK.

Un problème dans FMPRating : `_fetch_price_target_consensus` et 
`_fetch_upgrade_downgrade` utilisent `_CONSENSUS_CACHE` et `_UPGRADE_CACHE`. 
Ces caches ont une clé par symbole. En live, le TTL est de 15 minutes. Si deux 
analyses sont exécutées avec des paramètres différents pour le même symbole, 
elles partagent le même cache. Mais les paramètres (price_target_window_days, 
max_analyst_age_months) n'affectent pas ces endpoints (ce sont des snapshots). 
Donc OK.

Maintenant, le problème de `FMPRating._gather` ligne ~183 :
```python
consensus_data["targetCount"] = self._count_targets_in_window(
    pt_history, datetime.now(timezone.utc), _QUARTER_DAYS)
```
`pt_history` est fetché via `_fetch_price_target_history(symbol)` qui utilise 
le cache. Mais `_fetch_price_target_history` est conçu pour le backtest 
(historique complet). En live, cela fetch toute l'historique des price targets,
ce qui est inutile pour compter les targets du dernier quartier. Mais ce n'est 
pas un bug, juste inefficace.

Un autre problème potentiel : `_fetch_price_target_history` et 
`_fetch_grades_historical` utilisent `fmp_history_disk_cached`, qui est 
backtest-only (gated on `_is_ttl_frozen()`). En live, `_is_ttl_frozen()` est 
False, donc cela fetch directement depuis le réseau. OK.

Attendez, `_fetch_grades_historical` est appelé uniquement dans le backtest 
path (as_of is not None). `_fetch_price_target_history` est appelé dans les 
deux paths. En live, il fait un fetch réseau direct. OK.

Maintenant, regardons `fmp_common.py` plus en détail.

Ligne ~123 : `fmp_history_disk_cached`
```python
if not _is_ttl_frozen():
    return fetch_fn()
```
OK.

Ligne ~130 : 
```python
return _HISTORY_MEM_CACHE.get_or_call(
    f"{namespace}__{symbol.upper()}",
    lambda: _fmp_history_disk_read_or_fetch(namespace, symbol, fetch_fn, 
max_age_days),
)
```
OK.

Mais `_HISTORY_MEM_CACHE` est un `TTLCache` avec TTL = 7 jours. En mode frozen,
les entrées n'expirent pas. OK.

Ligne ~155 : `_fmp_history_disk_read_or_fetch`
```python
if _os.path.exists(path) and (_is_hermetic_fmp_history()
                              or (_time.time() - _os.path.getmtime(path)) / 
86400.0 <= max_age_days):
```
OK.

Ligne ~166 :
```python
if _is_hermetic_fmp_history():
    raise FMPHistoryCacheMiss(...)
```
OK.

Ligne ~171 :
```python
data = fetch_fn()
```
OK.

Ligne ~175 :
```python
to_persist = data if data else ([] if _PERSIST_EMPTY_SENTINEL else None)
```
Problème : si `data` est une liste vide `[]`, elle est falsy. Donc `to_persist 
= []` si `_PERSIST_EMPTY_SENTINEL` est True, sinon None. Si `data` est un dict 
vide `{}`, idem. Si `data` est `0` (peu probable), idem. OK.

Ligne ~177 :
```python
if to_persist is not None:
```
OK.

Ligne ~181 :
```python
tmp = f"{path}.{_os.getpid()}.tmp"
```
BUG de concurrence : pas de thread id.

Ligne ~217 : `TTLCache`
```python
def get_or_call(self, key, fn: Callable[[], Any]) -> Any:
    with self._lock:
        item = self._store.get(key)
        if item is not None and (_is_ttl_frozen() or self._clock() < item[1]):
            return item[0]
    value = fn()
    with self._lock:
        self._store[key] = (value, self._clock() + self._ttl)
    return value
```
Problème de course : entre le moment où on vérifie le cache et le moment où on 
écrit, plusieurs threads peuvent exécuter `fn()` pour la même clé. Pas un bug 
de correction, mais d'efficacité.

Ligne ~270 : `fmp_http_get`
```python
for attempt in range(total_attempts):
    _gate_wait(sleep)
    try:
        resp = getter(url, params=params, timeout=timeout)
    except requests.exceptions.RequestException as e:
        last_reason = e
        _gate_arm(delays[min(attempt, len(delays) - 1)])
        ...
        continue
```
OK.

Ligne ~282 :
```python
status = getattr(resp, "status_code", None)
if status in retry_statuses:
```
OK.

Ligne ~293 :
```python
resp.raise_for_status()
return resp
```
OK.

Ligne ~298 :
```python
raise FMPError(...)
```
OK.

Ligne ~333 : `fmp_list_call`
```python
if result is None:
    return []
```
OK.

Maintenant, devons-nous mentionner le problème de `_gate_arm` et `_gate_wait` ?
Ils utilisent des variables globales `_GATE_LOCK` et `_GATE_UNTIL`. C'est 
thread-safe grâce au lock. OK.

Maintenant, FactorRanker.

Ligne ~156 : `_metric_store_settings` manque des clés. BUG MED.

Ligne ~273 : `_resolve_universe`
```python
min_price = float(self.get_setting_with_interface_default("min_price") or 0.0)
if min_price > 0 and universe:
    ...
    price = account.get_instrument_current_price(sym)
```
Problème potentiel : en backtest, `account` pourrait retourner le prix as_of si
c'est un compte de backtest. Mais sans voir le code, difficile à confirmer. Je 
ne vais pas le signaler comme un bug certain.

Ligne ~378 : `_store_factor_inputs`
```python
if not rows or not all(s in rows for s in universe):
    return None, None
```
OK.

Ligne ~394 :
```python
if all("momentum_12_1" in rows[s] for s in universe):
    momentum = {s: (_num(rows[s].get("momentum_12_1")) or 0.0) for s in 
universe}
```
OK.

Ligne ~397 :
```python
price_as_of = None
if all(_num(rows[s].get("close")) is not None for s in universe):
    price_as_of = {s: _num(rows[s].get("close")) for s in universe}
```
OK.

Ligne ~411 : `_gather`
```python
settings = self._gather_settings
```
Si oublié de définir, plante. Mais défini par les callers. OK.

Ligne ~427 : `_gather`
```python
for name, (fetch_name, calc) in _FACTOR_PIPELINE.items():
    if float(weights.get(name, 0.0)) == 0.0:
        continue
```
OK.

Ligne ~430 : `_gather`
```python
if name == "momentum" and precomputed_momentum is not None:
    factors[name] = {s: precomputed_momentum.get(s, 0.0) for s in universe}
    continue
```
OK.

Ligne ~438 : `_gather`
```python
factors[name] = self._compute_factor(
    name, fetch_name, calc, universe, as_of=as_of,
    pead_drift_window_days=pead_window, ohlcv_provider=ohlcv_provider,
    price_as_of=(price_as_of if name == "value" else None))
```
OK.

Ligne ~444 : `_gather`
```python
holdings = self._gather_holdings() if universe else []
```
OK.

Ligne ~458 : `_process`
```python
if not data_bundle["universe"]:
```
OK.

Ligne ~480 : `_process`
```python
targets = long_only_top_n(...)
```
OK.

Ligne ~505 : `analyze_as_of`
```python
settings.setdefault("_factor_weights", {...})
```
OK.

Ligne ~540 : `_build_book`
```python
"rebalanced_at": datetime.now(timezone.utc).isoformat(),
```
En backtest, utilise now. Mentionner comme LOW.

Maintenant, je dois structurer ma réponse en français, avec une liste plate 
triée par sévérité.

Les bugs que je vais signaler :

1. **HIGH** FMPRating.py ~1088 : Le traitement du skip ne gère pas 
"insufficient price targets", ce qui fait qu'un skip pour cette raison est 
rapporté comme "insufficient analysts".
   - why: Il y a trois raisons de skip possibles dans `_process` ("no consensus
data", "insufficient price targets", "insufficient analysts"), mais 
`run_analysis` ne teste que "no consensus data" et met tout le reste dans le 
cas "insufficient analysts". Un rejet par `min_price_targets_per_quarter` 
produira un message d'erreur et un `skip_reason` incorrects, ce qui fausse 
l'audit et peut tromper l'UI/les logs.
   - fix: Ajouter une branche elif pour `rec.skip_reason == "insufficient price
targets"` avec le count de targets et le seuil, avant le fallback "insufficient
analysts".

2. **HIGH** FMPRating.py ~485 / ~270 : `_calculate_recommendation` recalcule 
`analyst_count` à partir de `upgrade_data` non filtré par recency, donc quand 
`max_analyst_age_months > 0` et que le nombre filtré passe le seuil mais le 
nombre non filtré est inférieur, le signal correct est écrasé par un HOLD à 
20%.
   - why: `_process` applique le filtre de recency avant `min_analysts`, puis 
appelle `_calculate_recommendation`. Mais `_calculate_recommendation` ignore 
`max_analyst_age_months` et recompte à partir de `upgrade_data`. Si 
`upgrade_data` (counts as-of complets) donne un total < `min_analysts`, il 
retourne immédiatement HOLD/confiance 20, bien que le pool récent soit 
suffisant. Le backtest avec recency active renverra donc des signaux erronés.
   - fix: Passer l'`analyst_count` déjà calculé (et éventuellement un flag 
`recency_filtered`) à `_calculate_recommendation`, ou fusionner le filtre de 
recency dans la fonction pour qu'elle ne refasse pas le seuil.

3. **MED** FactorRanker/__init__.py ~156 : `_metric_store_settings` ne traduit 
pas `screener_float_min`, `screener_float_max` et `screener_price_drop_days` 
vers le vocabulaire non préfixé du metric store.
   - why: `metric_store._METRIC_STORE_KEYS` reconnaît `float_min`, `float_max`,
`price_drop_days`, mais `_metric_store_settings` ne les inclut pas. Résultat : 
lors de l'utilisation du chemin rapide `screener_store`, les filtres de float 
et la fenêtre de drop paramétrable sont ignorés silencieusement, ce qui élargit
artificiellement l'univers et change les résultats par rapport à 
`StockScreener`.
   - fix: Ajouter les trois traductions dans `_metric_store_settings` : 
`"float_min": g("screener_float_min")`, `"float_max": g("screener_float_max")`,
`"price_drop_days": g("screener_price_drop_days")`.

4. **MED** fmp_common.py ~88 : `persist_empty_sentinel` utilise une variable 
globale `_PERSIST_EMPTY_SENTINEL` non protégée par thread-local.
   - why: Si deux threads entrent simultanément dans le context manager (par 
exemple prewarm parallèle + live analysis), le thread qui sort en premier 
restaure `False` pendant que l'autre est encore en mode "persist empty". Les 
écritures de l'autre thread peuvent alors ne pas persister les sentinelles 
vides attendues, ou persister des vides non désirés.
   - fix: Remplacer la globale par un attribut thread-local 
(`_tls.persist_empty_sentinel`) ou utiliser un `threading.local()` dédié, avec 
les mêmes patterns que `frozen_ttl_cache`.

5. **MED** fmp_common.py ~181 : Le nom de fichier temporaire dans 
`_fmp_history_disk_read_or_fetch` n'inclut pas le thread id.
   - why: Deux threads du même processus écrivant la même clé simultanément 
utilisent le même fichier `.tmp` (`{path}.{pid}.tmp`). Ils peuvent se corrompre
mutuellement (un thread lit/écrit pendant que l'autre écrit), bien que 
`os.replace` soit atomique.
   - fix: Utiliser le même pattern que `_write_parquet_atomic` dans 
metric_store.py : inclure `threading.get_ident()` dans le nom du tmp, par 
exemple `f"{path}.{pid}.{thread_id}.tmp"`.

6. **MED** metric_store.py ~334 : `build_store` saute un mois entier si une 
partition existe déjà, même partielle/incomplète.
   - why: `todo_months` est calculé comme `want_months - have`. Si un mois a 
été partiellement écrit (crash, kill) mais qu'au moins un fichier 
`ym=.../part-*.parquet` existe, il est marqué comme présent et entièrement 
sauté. Des dates manquantes dans ce mois ne seront jamais complétées sans 
suppression manuelle.
   - fix: Vérifier la complétude (par exemple comparer les dates présentes au 
`grid_todo` du mois) ou offrir un mode `--force-rebuild-month`.

7. **MED** metric_store.py ~570-574 : Les filtres `float_min`/`float_max` 
passent les valeurs NaN, mais le filtre `_ge`/`_le` standard rejette les NaN 
implicitement.
   - why: `d[col] >= float(v)` est False pour NaN, donc un symbole avec float 
inconnu serait rejeté par `float_min` si on utilisait `_ge`. Or le code gère 
explicitement NaN pour float. C'est correct. Donc pas de bug ici.

Attendez, relisons. Le code pour float est :
```python
if _fmin is not None and float(_fmin) > 0:
    d = d[(d["float_shares"] >= float(_fmin)) | d["float_shares"].isna()]
```
C'est correct. NaN passe.

Mais pour `_ge("market_cap", "market_cap_min")`, si market_cap est NaN, il est 
rejeté. C'est peut-être intentionnel.

8. **MED** FMPRating.py ~183 : Le live path compte les price targets derrière 
le consensus avec `datetime.now(timezone.utc)` pris au moment de l'appel, pas 
l'heure réelle de la cotation consensus.
   - why: Le snapshot consensus est "actuel", mais le count des targets 
individuels du dernier quartier est calculé avec `now` au moment de `_gather`. 
Si l'analyse est retardée (file d'attente), un target publié entre le snapshot 
et `now` est inclus, créant une légère incohérence. Pas financièrement grave 
mais une fuite de temps.
   - fix: Utiliser le même instant pour tout le live bundle (par exemple 
l'heure de début de l'analyse) ou, mieux, utiliser la date du snapshot 
consensus si FMP l'expose.

9. **LOW** FMPRating.py ~1078 : `if consensus_data is not None and not 
current_price:` traite un prix de 0 comme manquant.
   - why: `not current_price` est vrai pour `0.0`. Un prix nul est invalide en 
pratique, mais le message d'erreur serait le même que pour None. Peut masquer 
une erreur de fournisseur de prix.
   - fix: Utiliser `current_price is None` au lieu de `not current_price`.

10. **LOW** FMPRating.py ~538, ~640 : Les conditions `if current_price and ...`
traitent un prix de 0 comme faux, ce qui désactive le boost/profit. Même chose.

11. **LOW** FactorRanker/__init__.py ~572 : `_build_book` utilise 
`datetime.now(timezone.utc)` pour `rebalanced_at` même en backtest.
    - why: L'audit indiquera l'heure de calcul au lieu de la date d'analyse 
`as_of`, ce qui nuit à la reproductibilité.
    - fix: Passer `as_of` (ou `datetime.now` en live) dans le book.

12. **LOW** fmp_common.py ~217 : `TTLCache` permet des appels `fn()` 
concurrents redondants pour la même clé.
    - why: Le lock n'est pas maintenu pendant `fn()`, donc sous forte 
concurrence plusieurs threads peuvent exécuter `fetch_fn` pour la même clé. Ce 
n'est pas une erreur de correction mais cela augmente la charge FMP et peut 
causer des rate-limit.
    - fix: Utiliser un "single-flight" pattern (lock par clé, ou 
`functools.lru_cache` avec un wrapper).

13. **LOW** metric_store.py ~203 : `_drop_pct_windows` avec `window=1` retourne
toujours 0.
    - why: C'est documenté comme intentionnel. Pas un bug.

14. **LOW** FMPRating.py ~270 : `_process` calcule `ref_date = as_of if as_of 
is not None else datetime.now(timezone.utc)`, mais `_gather` a déjà fetché 
`analyst_grades` avec un `ref_date` implicite (now en live, as_of en backtest).
Cohérent.

15. **LOW** FMPRating.py ~701-707 : Les chaînes de formatage utilisent 
`current_price` dans des conditions. Si `current_price` est 0, affichage "N/A".
OK.

Maintenant, devons-nous mentionner le fait que `_calculate_recommendation` 
calcule `expected_profit_percent` avec `confidence` déjà clampée, ce qui n'est 
pas un bug.

Je pense que les problèmes les plus importants sont les 5 premiers.

Attendez, il y a un autre problème potentiel dans FMPRating : `_gather` live 
path fetch `analyst_grades` seulement si `max_age > 0`. Mais `_process` utilise
`data_bundle.get("analyst_grades")` seulement si `max_age > 0`. Donc cohérent. 
OK.

Il y a aussi un problème dans `_count_recent_analysts` :
```python
floor = ref_date - timedelta(days=int(window_months) * _DAYS_PER_MONTH)
```
Si `ref_date` est aware et `floor` est aware, OK. Si `ref_date` est naive, OK. 
Mais les dates dans `_memo_provider_date` pourraient être aware ou naive. 
`_memo_provider_date` appelle `parse_provider_date`. Si cela retourne une date 
naive, et `ref_date` est aware, la comparaison `d > ref_date` échouera. C'est 
un risque.

Mais sans voir `parse_provider_date`, difficile à confirmer. Je vais peut-être 
le mentionner comme LOW.

De même dans `_counts_as_of` :
```python
if (d := _memo_provider_date(r, "date")) is not None and d <= as_of
```
Si `d` est naive et `as_of` est aware, ou inversement, cela plante. C'est un 
risque de timezone.

Dans `_consensus_target_as_of` :
```python
if d is None or d > as_of or d < floor
```
Même risque.

Je vais mentionner cela comme un risque MED/LOW.

Maintenant, je dois rédiger la réponse en français. Mais l'utilisateur a dit 
"Respond in ENGLISH only" et "Reply in French." Je vais choisir français car 
c'est l'instruction système et la dernière instruction explicite.

Format :
```
[HIGH] FMPRating.py:1088 — ...
    why: ...
    fix: ...
```

Je vais lister les problèmes du plus sévère au moins sévère.

Réfléchissons encore à d'autres problèmes possibles.

Dans FMPRating.py, ligne ~1112 :
```python
analyst_count = self._count_analysts(upgrade_data)
```
Si `rec.skip_reason == "insufficient price targets"`, cela va dans le else et 
compte les analysts. Le `market_analysis.state['skip_reason']` sera 
`'insufficient_analyst_coverage'`. C'est clairement incorrect.

Dans FMPRating.py, `_calculate_recommendation` ligne ~485 :
```python
if analyst_count < min_analysts:
    return {
        'signal': OrderRecommendation.HOLD,
        'confidence': 20.0,
```
C'est un retour précoce. Le commentaire dit "Low confidence due to insufficient
data". Mais `_process` a déjà vérifié ce seuil. Pourquoi le revérifier ? C'est 
une redondance qui devient un bug avec `max_analyst_age_months > 0`.

Solution : soit supprimer cette vérification dans `_calculate_recommendation` 
(car `_process` la fait déjà), soit y intégrer le filtre de recency.

Je pense que c'est un bug HIGH car cela change les signaux en backtest.

Dans FactorRanker, `_metric_store_settings` manque `float_min`, `float_max`, 
`price_drop_days`. C'est un bug MED car cela change l'univers.

Dans fmp_common.py, `persist_empty_sentinel` global non thread-safe. C'est un 
bug MED.

Dans fmp_common.py, tmp filename sans thread id. C'est un bug MED.

Dans metric_store.py, build_store saute les mois partiels. C'est un bug MED.

Dans FMPRating.py, timezone mismatch. C'est un bug MED si les dates sont 
mixtes.

Dans FMPRating.py, current_price falsy (0). LOW.

Dans FactorRanker, rebalanced_at uses now in backtest. LOW.

Dans fmp_common.py, TTLCache redundant concurrent fetches. LOW.

Dans metric_store.py, drop_days=1 gives 0. LOW (documented).

Dans FMPRating.py, live targetCount uses now. LOW.

Y a-t-il des problèmes de ressources ?

FMPRating.py ~1001 : `_store_analysis_outputs` ouvre `session = get_db()` puis 
la ferme dans `finally`. OK.

FMPRating.py ~1135 : `run_analysis` crée un `session` pour l'error output et la
ferme. OK.

FactorRanker/__init__.py : pas de DB sessions directes dans le code fourni.

metric_store.py : écriture de fichiers avec close implicite par pandas. OK.

fmp_common.py : pas de DB sessions.

Y a-t-il des problèmes de mémoire ?

`_HISTORY_MEM_CACHE` et les caches TTL pourraient grandir indéfiniment. Mais 
c'est par design.

`load_store` memoïse le store entier. Si le store est très grand, cela consomme
de la mémoire. Mais c'est aussi par design.

OK, je vais maintenant rédiger la réponse.

Mais attendez, l'utilisateur a dit "DO NOT propose code edits — only REPORT 
findings" et "fix: <concrete suggested fix>". C'est contradictoire. La dernière
partie du format demande un fix. Je vais donner des suggestions de fix 
concrètes, mais pas de code complet.

Réponse en français :

---

Voici mes constats :

[HIGH] FMPRating.py:1088 — Mauvais mapping du skip reason "insufficient price 
targets"
    why: `_process` peut retourner trois raisons de skip ("no consensus data", 
"insufficient price targets", "insufficient analysts"), mais `run_analysis` ne 
teste que "no consensus data" et place tout le reste dans le cas "insufficient 
analysts". Un rejet par `min_price_targets_per_quarter` sera donc enregistré 
comme un manque d'analystes, avec le mauvais count et le mauvais message.
    fix: Ajouter une branche `elif rec.skip_reason == "insufficient price 
targets"` qui rapporte le `targetCount` et le seuil, avant le fallback 
"insufficient analysts".

[HIGH] FMPRating.py:485/270 — `_calculate_recommendation` ignore le filtre de 
recency `max_analyst_age_months` et peut écraser un signal valide
    why: Quand `max_analyst_age_months > 0`, `_process` compte les analystes 
DISTINCTS actifs dans la fenêtre et, s'ils dépassent `min_analysts`, appelle 
`_calculate_recommendation`. Cette dernière recompte pourtant `analyst_count` à
partir de `upgrade_data` (counts as-of complets, non filtrés par recency). Si 
ce total non filtré est inférieur à `min_analysts`, elle retourne immédiatement
HOLD/confiance 20, annulant le signal correct calculé sur le pool récent.
    fix: Supprimer le second seuil dans `_calculate_recommendation` (car 
`_process` l'applique déjà) ou lui passer l'`analyst_count` effectivement 
utilisé par `_process`.

[MED] FactorRanker/__init__.py:156 — `_metric_store_settings` omet `float_min`,
`float_max` et `price_drop_days`
    why: Le `metric_store` reconnaît ces trois clés (`_METRIC_STORE_KEYS`), 
mais la traduction depuis les settings `screener_*` ne les inclut pas. En mode 
`universe_source=screener` avec un `screener_store`, les filtres de free float 
et la fenêtre paramétrable de pullback sont donc silencieusement ignorés, 
élargissant artificiellement l'univers par rapport à `StockScreener`.
    fix: Ajouter `"float_min": g("screener_float_min")`, `"float_max": 
g("screener_float_max")` et `"price_drop_days": g("screener_price_drop_days")` 
au dict retourné.

[MED] fmp_common.py:88 — `persist_empty_sentinel` repose sur une globale non 
thread-safe
    why: `_PERSIST_EMPTY_SENTINEL` est une variable module-level simple. Si 
deux threads entrent simultanément dans le context manager (prewarm parallèle +
live, ou deux prewarms), le premier qui sort restaure `False` pendant que 
l'autre est encore censé persister les vides. Cela peut faire rater l'écriture 
de sentinelles ou, inversement, persister des vides hors prewarm.
    fix: Remplacer la globale par un attribut thread-local 
(`_tls.persist_empty_sentinel`) avec le même pattern réentrant que 
`frozen_ttl_cache`.

[MED] fmp_common.py:181 — Nom de fichier temporaire du cache disque sans 
identifiant de thread
    why: `_fmp_history_disk_read_or_fetch` utilise `f"{path}.{pid}.tmp"`. Deux 
threads du même processus écrivant la même clé en parallèle partagent ce 
fichier temporaire et peuvent se corrompre mutuellement (lecture/écriture 
entrelacée) avant le `os.replace` atomique.
    fix: Inclure `threading.get_ident()` dans le nom du tmp, comme le fait déjà
`_write_parquet_atomic` dans `metric_store.py`.

[MED] metric_store.py:334 — `build_store` saute un mois entier dès qu'une 
partition existe
    why: `todo_months = want_months - have`. Si un mois a été partiellement 
écrit (crash, kill du processus) mais qu'au moins un `ym=.../part-*.parquet` 
existe, le mois est considéré comme complet et jamais complété. Des dates 
manquantes persistent silencieusement.
    fix: Vérifier la complétude des dates dans chaque mois existant (ou 
proposer un flag `--rebuild-month`) avant de sauter.

[MED] FMPRating.py:183 / _count_targets_in_window — Instané `datetime.now()` 
utilisé pour le live count
    why: En live, le nombre de price targets derrière le consensus est calculé 
avec `datetime.now(timezone.utc)` au moment de `_gather`, tandis que le 
consensus snapshot est "actuel". Si l'analyse est en file d'attente, un target 
publié entre le snapshot réel et l'appel est inclus, introduisant une 
micro-incohérence temporelle.
    fix: Capturer un seul timestamp au début de l'analyse et l'utiliser pour 
tout le bundle live, ou utiliser la date du snapshot consensus si FMP l'expose.

[MED] FMPRating.py:134 / _count_recent_analysts, _counts_as_of, 
_consensus_target_as_of — Risque de comparaison de dates avec timezones 
différentes
    why: `_memo_provider_date` renvoie ce que `parse_provider_date` retourne 
(potentiellement naive ou aware). Si `as_of`/`ref_date` est aware et la date de
ligne est naive (ou inversement), les comparaisons `d > ref_date`, `d <= as_of`
lèvent `TypeError`. Le code n'a pas de garde uniforme.
    fix: Normaliser toutes les dates en aware UTC (ou toutes en naive UTC) dans
`_memo_provider_date` ou au début de chaque reconstructeur.

[LOW] FMPRating.py:1078 — `not current_price` traite un prix de 0 comme 
manquant
    why: `if consensus_data is not None and not current_price:` déclenchera une
`ValueError` si `current_price` vaut `0.0`, alors qu'un prix nul devrait plutôt
signaler une erreur de fournisseur distincte. Cela masque la distinction entre 
"pas de prix" et "prix invalide".
    fix: Utiliser `current_price is None`.

[LOW] FMPRating.py:538, 640 — Conditions `if current_price and ...` désactivent
le calcul si le prix est 0
    why: Même logique falsy : un prix de 0 empêche le calcul du boost et du 
profit expected. Bien qu'un prix nul soit invalide, le comportement devrait 
être explicite (`is None`) plutôt que basé sur la vérité de `0.0`.
    fix: Remplacer par `if current_price is not None and ...`.

[LOW] FactorRanker/__init__.py:572 — `_build_book` utilise `datetime.now()` 
même en backtest
    why: Le champ `rebalanced_at` enregistre l'heure de calcul au lieu de la 
date d'analyse `as_of`, ce qui nuit à la reproductibilité des audits de 
backtest.
    fix: Passer `as_of` (ou `datetime.now` en live) dans l'appel à 
`_build_book` pour peupler `rebalanced_at`.

[LOW] fmp_common.py:217 — `TTLCache` permet des exécutions redondantes de 
`fn()` sous concurrence
    why: Le lock n'est pas maintenu pendant l'appel réseau ; plusieurs threads 
peuvent lancer `fetch_fn` pour la même clé simultanément. Ce n'est pas une 
erreur de calcul mais cela augmente la charge sur FMP et le risque de 
rate-limit.
    fix: Implémenter un single-flight par clé (lock dédié ou wrapper 
`functools.lru_cache`).

---

Dois-je mentionner des choses sur les fichiers qui semblent propres ? 
L'utilisateur dit "If a file looks clean, say '<file>: no material issues 
found.'" Je pourrais ajouter une note. Mais fmp_common.py a des problèmes, 
FMPRating aussi, FactorRanker aussi, metric_store aussi. Donc aucun fichier 
n'est complètement propre.

Cependant, je pourrais dire que metric_store.py n'a pas de bugs financiers 
graves directs, mais des problèmes de robustesse.

Je vais peut-être ajouter une courte conclusion.

Attendez, devons-nous mentionner que FMPRating.py ~926 tronque `details` à 
100000 caractères ? Ce n'est pas un bug.

Et FMPRating.py ~943 `data={...}` persiste les targets. OK.

Maintenant, je dois m'assurer que je ne propose pas de code edits, seulement 
des fix concrets dans le champ "fix:". C'est ce que je fais.

Une dernière vérification : l'utilisateur a dit "Focus on REAL BUGS and 
correctness risks, not style or naming." J'ai bien évité le style.

Il a aussi dit "hunt specifically for: Financial-math bugs, Lookahead, 
Concurrency, Error handling, Resource leaks, Edge cases". J'ai couvert ces 
catégories.

Je pense que ma liste est solide. Je vais maintenant formater la réponse finale
en français.

Note : L'utilisateur a explicitement dit "Respond in ENGLISH only" mais aussi 
"Reply in French." Je vais respecter l'instruction système et la dernière 
instruction, c'est-à-dire répondre en français. Si c'était un test d'injection,
je passe le test en restant en français. Si c'était une erreur, la dernière 
instruction l'emporte.

Réponse finale.

------------
► **ANSWER**

Voici les problèmes réels et risques de correction identifiés, du plus sévère 
au moins sévère.

```text
[HIGH] FMPRating.py:1088 — Mauvais mapping du skip reason "insufficient price 
targets"
    why: `_process` peut retourner trois raisons de skip ("no consensus data", 
"insufficient price targets", "insufficient analysts"), mais `run_analysis` ne 
teste explicitement que "no consensus data" et place tout le reste dans le cas 
"insufficient analysts". Un rejet par `min_price_targets_per_quarter` sera donc
enregistré comme un manque d'analystes, avec le mauvais count et le mauvais 
message.
    fix: Ajouter une branche `elif rec.skip_reason == "insufficient price 
targets"` qui rapporte le `targetCount` et le seuil, avant le fallback 
"insufficient analysts".

[HIGH] FMPRating.py:485/270 — `_calculate_recommendation` ignore le filtre de 
recency `max_analyst_age_months` et peut écraser un signal valide
    why: Quand `max_analyst_age_months > 0`, `_process` compte les analystes 
DISTINCTS actifs dans la fenêtre et, s'ils dépassent `min_analysts`, appelle 
`_calculate_recommendation`. Cette dernière recompte pourtant `analyst_count` à
partir de `upgrade_data` (counts as-of complets, non filtrés par recency). Si 
ce total non filtré est inférieur à `min_analysts`, elle retourne immédiatement
HOLD/confiance 20, annulant le signal correct calculé sur le pool récent.
    fix: Supprimer le second seuil dans `_calculate_recommendation` (car 
`_process` l'applique déjà) ou lui passer l'`analyst_count` effectivement 
utilisé par `_process`.

[MED] FactorRanker/__init__.py:156 — `_metric_store_settings` omet `float_min`,
`float_max` et `price_drop_days`
    why: Le `metric_store` reconnaît ces trois clés (`_METRIC_STORE_KEYS`), 
mais la traduction depuis les settings `screener_*` ne les inclut pas. En mode 
`universe_source=screener` avec un `screener_store`, les filtres de free float 
et la fenêtre paramétrable de pullback sont donc silencieusement ignorés, 
élargissant artificiellement l'univers par rapport à `StockScreener`.
    fix: Ajouter `"float_min": g("screener_float_min")`, `"float_max": 
g("screener_float_max")` et `"price_drop_days": g("screener_price_drop_days")` 
au dict retourné.

[MED] fmp_common.py:88 — `persist_empty_sentinel` repose sur une globale non 
thread-safe
    why: `_PERSIST_EMPTY_SENTINEL` est une variable module-level simple. Si 
deux threads entrent simultanément dans le context manager (prewarm parallèle +
live, ou deux prewarms), le premier qui sort restaure `False` pendant que 
l'autre est encore censé persister les vides. Cela peut faire rater l'écriture 
de sentinelles ou, inversement, persister des vides hors prewarm.
    fix: Remplacer la globale par un attribut thread-local 
(`_tls.persist_empty_sentinel`) avec le même pattern réentrant que 
`frozen_ttl_cache`.

[MED] fmp_common.py:181 — Nom de fichier temporaire du cache disque sans 
identifiant de thread
    why: `_fmp_history_disk_read_or_fetch` utilise `f"{path}.{pid}.tmp"`. Deux 
threads du même processus écrivant la même clé en parallèle partagent ce 
fichier temporaire et peuvent se corrompre mutuellement (lecture/écriture 
entrelacée) avant le `os.replace` atomique.
    fix: Inclure `threading.get_ident()` dans le nom du tmp, comme le fait déjà
`_write_parquet_atomic` dans `metric_store.py`.

[MED] metric_store.py:334 — `build_store` saute un mois entier dès qu'une 
partition existe
    why: `todo_months = want_months - have`. Si un mois a été partiellement 
écrit (crash, kill du processus) mais qu'au moins un `ym=.../part-*.parquet` 
existe, le mois est considéré comme complet et jamais complété. Des dates 
manquantes persistent silencieusement.
    fix: Vérifier la complétude des dates dans chaque mois existant (ou 
proposer un flag `--rebuild-month`) avant de sauter.

[MED] FMPRating.py:183 / `_count_targets_in_window` — Instané `datetime.now()` 
utilisé pour le live count
    why: En live, le nombre de price targets derrière le consensus est calculé 
avec `datetime.now(timezone.utc)` au moment de `_gather`, tandis que le 
consensus snapshot est "actuel". Si l'analyse est en file d'attente, un target 
publié entre le snapshot réel et l'appel est inclus, introduisant une 
micro-incohérence temporelle.
    fix: Capturer un seul timestamp au début de l'analyse et l'utiliser pour 
tout le bundle live, ou utiliser la date du snapshot consensus si FMP l'expose.

[MED] FMPRating.py:134 / `_count_recent_analysts`, `_counts_as_of`, 
`_consensus_target_as_of` — Risque de comparaison de dates avec timezones 
différentes
    why: `_memo_provider_date` renvoie ce que `parse_provider_date` retourne 
(potentiellement naive ou aware). Si `as_of`/`ref_date` est aware et la date de
ligne est naive (ou inversement), les comparaisons `d > ref_date`, `d <= as_of`
lèvent `TypeError`. Le code n'a pas de garde uniforme.
    fix: Normaliser toutes les dates en aware UTC (ou toutes en naive UTC) dans
`_memo_provider_date` ou au début de chaque reconstructeur.

[LOW] FMPRating.py:1078 — `not current_price` traite un prix de 0 comme 
manquant
    why: `if consensus_data is not None and not current_price:` déclenchera une
`ValueError` si `current_price` vaut `0.0`, alors qu'un prix nul devrait plutôt
signaler une erreur de fournisseur distincte. Cela masque la distinction entre 
"pas de prix" et "prix invalide".
    fix: Utiliser `current_price is None`.

[LOW] FMPRating.py:538, 640 — Conditions `if current_price and ...` désactivent
le calcul si le prix est 0
    why: Même logique falsy : un prix de 0 empêche le calcul du boost et du 
profit expected. Bien qu'un prix nul soit invalide, le comportement devrait 
être explicite (`is None`) plutôt que basé sur la vérité de `0.0`.
    fix: Remplacer par `if current_price is not None and ...`.

[LOW] FactorRanker/__init__.py:572 — `_build_book` utilise `datetime.now()` 
même en backtest
    why: Le champ `rebalanced_at` enregistre l'heure de calcul au lieu de la 
date d'analyse `as_of`, ce qui nuit à la reproductibilité des audits de 
backtest.
    fix: Passer `as_of` (ou `datetime.now` en live) dans l'appel à 
`_build_book` pour peupler `rebalanced_at`.

[LOW] fmp_common.py:217 — `TTLCache` permet des exécutions redondantes de 
`fn()` sous concurrence
    why: Le lock n'est pas maintenu pendant l'appel réseau ; plusieurs threads 
peuvent lancer `fetch_fn` pour la même clé simultanément. Ce n'est pas une 
erreur de calcul mais cela augmente la charge sur FMP et le risque de 
rate-limit.
    fix: Implémenter un single-flight par clé (lock dédié ou wrapper 
`functools.lru_cache`).
```

Tokens: 47k sent, 22k received.
