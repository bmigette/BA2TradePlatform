# Revue de code — BA2TradePlatform
**Date :** 2026-08-04 · **Revueur :** subagent OpenClaw (lecture seule) · **Repo :** `C:\Users\basti\Documents\dev\BA2TradePlatform` @ `778d3c3`

---

## 1. Résumé exécutif

BA2TradePlatform est une base de code mature (~1 030 fichiers .py hors venv, 366 fichiers de tests, ~740 commits depuis juin 2026) couvrant le trading live (experts → règles → sizing → broker Alpaca/IBKR) et une plateforme de backtest/optimisation génétique. La qualité générale est **nettement au-dessus de la moyenne** : discipline anti-lookahead explicite (providers "as-of clampés", caches hermétiques, golden tests de parité live/backtest), corrections d'incidents documentées dans le code, gestion d'erreurs presque partout non silencieuse (aucun `except: pass` dans le code cœur). Les défauts trouvés se concentrent dans le **moteur backtest ML "legacy"** (`backtest_handler.py`, sous-maintenu par rapport au moteur daily), dans le **pipeline d'optimisation** (3 tests cassés connus, pas de validation out-of-sample) et dans quelques **cas limites d'exécution d'ordres live** (wash-trade OCO, clôture multi-transactions). Aucun bug critique de type "perte d'argent immédiate" n'a été identifié sur le chemin live principal, mais 4 points Hauts méritent une action rapide.

**Cartographie rapide :**
- `ba2_trade_platform/` + `packages/{common,experts,providers}` : app live (NiceGUI), experts de trading, exécution d'ordres, risk management.
- `testplatform/backend/` : plateforme de backtest — moteur daily (`app/services/backtest/`, très solide), moteur ML legacy (`backtest_handler.py`), optimisation GA (`strategy_optimization_handler.py`, `genetic.py`), workers distribués (`worker_server.py`, authentifié).
- Stratégies ("experts") : FMPRating, FMPEarningsDrift (PEAD), FMPInsiderClusterBuy, FMPSenateTrader{Copy,Weight}, FinHubRating, PennyMomentumTrader, FactorRanker, PremiumSeller (options).

---

## 2. Bugs trouvés par sévérité

### 🔴 HAUT

#### H1. Le slippage du moteur backtest ML est accepté… mais jamais appliqué
`testplatform/backend/app/services/backtest_handler.py:317,587,664-672`

```python
def run_backtest(..., slippage: float = 0.0, ...) -> Dict[str, Any]:
    ...
    bt = Backtest(
        bt_data, MLStrategy,
        cash=initial_capital,
        commission=commission / 100,
        exclusive_orders=True,
        trade_on_close=True,
        hedging=False,
    )   # <- aucun slippage passé ; backtesting.py n'en a pas nativement
```
Le paramètre `slippage` est threadé depuis l'API jusqu'à `run_backtest`/`_run_chronos_backtest`/`_run_strategy_backtest` (lignes 317, 359, 570, 587, 713, 797, 1150) mais n'est **jamais utilisé**. L'utilisateur croit modéliser le slippage ; les résultats ML sont systématiquement optimistes. Le moteur daily, lui, a bien `slippage_bps` (`backtest/backtest_account.py:336,1597,3111`).
**Fix :** soit appliquer le slippage manuellement (décaler les prix d'exécution dans `MLStrategy`), soit retirer le paramètre de l'UI/API du moteur ML et afficher un avertissement explicite.

#### H2. État partagé au niveau classe dans `MLStrategy` → course si essais parallèles
`testplatform/backend/app/services/backtest_handler.py:51-80,640-660` + `genetic.py:453-455`

```python
class MLStrategy(Strategy):
    predictions = None            # attributs de CLASSE
    buy_entry_conditions = None
    _exit_reasons_result = None
    ...
MLStrategy.predictions = pred_lookup   # muté globalement avant chaque bt.run()
```
Et dans `genetic.py:451-455` :
```python
elif self.parallel_individuals > 1:
    # Thread pool — only useful for I/O-bound or GPU work (the ML engine) ...
    with ThreadPoolExecutor(max_workers=self.parallel_individuals) as executor:
        fitnesses = list(executor.map(self.toolbox.evaluate, invalid_ind))
```
Le chemin GA du moteur ML évalue des essais **en parallèle dans des threads**, mais toute la configuration de la stratégie est mutée au niveau classe : deux backtests concurrents s'écrasent mutuellement (`predictions`, conditions, dicts `_exit_reasons_result`/`_pending_trades_result` réinitialisés dans `init()`). Résultat : métriques faussement attribuées, résultats silencieusement erronés.
**Fix :** instancier les paramètres via des attributs d'instance alimentés par `bt.run(**kwargs)` ou une factory de classe par essai (`type("MLStrategyN", (MLStrategy,), {...})`), ou forcer `parallel_individuals=1` sur le moteur ML.

#### H3. Optimisation GA sans validation out-of-sample
`testplatform/backend/app/services/strategy_optimization_handler.py` + `robustness_handler.py`

Le GA optimise sur une unique fenêtre `[start_date, end_date]` (`backtest_cfg`, lignes 346-465) ; aucune coupure train/test, aucun walk-forward. La "robustesse" (`robustness_handler.py`) ne fait que du Monte-Carlo et des variantes de calendrier (décalage des jours/heures d'analyse, `_schedule_variants`). Avec des grilles massives (cf. `grid_goal2020*`, `tools/grid_goal2020.sh`) et des fitness rewardant le retour, **le risque d'overfitting est structurel** : le meilleur individu d'une grille à plusieurs centaines d'essais est presque toujours flatteusement biaisé in-sample.
**Fix :** imposer un segment de validation aveugle (ex. optimisation sur 2020-2023, classement final sur 2024-2026 jamais touché par le GA), ou a minima un walk-forward k-fold dans `strategy_optimization_handler` ; compléter avec un ajustement type Deflated Sharpe pour les comparaisons de grille.

#### H4. 3 tests cassés (pré-existants) dans le module d'optimisation
`testplatform/backend/tests/test_strategy_optimization_handler.py`

Confirmé par exécution (venv du repo) :
```
FAILED test_trial_worker_default_omits_full_results
FAILED test_trial_worker_want_full_flag_attaches_results_and_is_stripped_from_config
FAILED test_max_remote_slots_for_experts_reads_senate_cap_and_defaults_uncapped
3 failed, 32 passed
```
Le commit `778d3c3` les mentionne comme "pre-existing". Des tests rouges dans le module d'optimisation — la zone la plus récemment modifiée — masquent des régressions futures.
**Fix :** réparer ou supprimer ces 3 tests, et ajouter une garde CI (suite verte obligatoire) sur `testplatform/backend/tests`.

---

### 🟠 MOYEN

#### M1. Sorties conditionnelles évaluées seulement aux barres de prédiction (moteur ML)
`testplatform/backend/app/services/backtest_handler.py:138-150` (docstring lignes 22-30 contradictoire)

La docstring promet "TP/SL and exit conditions are checked on every execution bar", mais le code court-circuite :
```python
if has_position and has_exit_conditions and not is_new_prediction_bar:
    self.bar_idx += 1
    return
```
En dual-timeframe (ex. prédictions 1h, exécution 5min), une condition de sortie basée sur le prix/P&L n'est évaluée qu'une fois par heure. Seuls les TP/SL numériques de backtesting.py restent actifs à chaque barre. **Fix :** évaluer les exit conditions à chaque barre d'exécution (coût acceptable), ou corriger la docstring + documenter la granularité dans l'UI.

#### M2. Exécution same-bar au close dans le moteur ML (`trade_on_close=True`)
`testplatform/backend/app/services/backtest_handler.py:670`

Le signal est calculé avec `self.data.Close[-1]` et exécuté au close de la même barre : hypothèse optimiste (en réel on ne décide et n'exécute qu'au mieux à l'open suivant). Le moteur daily utilise `next_bar_open` par défaut (`backtest/backtest_account.py:382-385,3100-3102`). Les résultats des deux moteurs ne sont pas comparables. **Fix :** option `fill_model` dans le moteur ML aussi, défaut `next_bar_open`, note dans la doc.

#### M3. Claim de tâches non atomique entre processus
`testplatform/backend/app/services/task_queue.py:561-600`

```python
with self._lock:                      # verrou THREAD uniquement
    task = db.query(TaskQueue).filter(... QUEUED ...).first()
    if task:
        task.status = TaskStatus.RUNNING.value
        db.commit()
```
Sûr pour des threads d'un même processus, mais si deux processus de queue (app principale + worker fleet, ou deux instances) partagent la même DB SQLite, deux workers peuvent lire la même tâche QUEUED et la traiter deux fois (dernier commit gagnant, pas de compare-and-set). **Fix :** update atomique conditionnel (`UPDATE task_queue SET status='running', worker_name=? WHERE id=? AND status='queued'` puis vérifier `rowcount`) ou `with_for_update()` selon le SGBD.

#### M4. Wash-trade : les ordres OCO ne sont ni bloquants ni candidats
`packages/common/ba2_common/core/interfaces/AccountInterface.py:285-349`

`_WASHTRADE_BLOCKING_ORDER_TYPES = {MARKET, BUY_STOP, SELL_STOP}` exclut les OCO. Un OCO au repos chez le broker contient pourtant une jambe stop ; si Alpaca la considère comme un ordre stop opposé, une nouvelle entrée MARKET opposée serait rejetée en 40310000 — exactement la classe d'incident corrigée le 2026-08-03 pour les jambes SELL_STOP (commit `8e0a9f8`, 8 jambes rejetées, tx 273 UBER restée sans stop). À vérifier avec les logs live ; si confirmé, ajouter OCO (et ses jambes) à la détection.
**Fix :** vérifier empiriquement le comportement Alpaca avec un OCO au repos + MARKET opposé, et intégrer le cas échéant OCO dans `_WASHTRADE_BLOCKING_ORDER_TYPES` / `_is_washtrade_lock_candidate`.

#### M5. FactorRanker : vente multi-transactions rattachée à la seule première transaction
`packages/experts/ba2_experts/FactorRanker/portfolio.py:340-353`

```python
def _submit_sell(self, symbol, qty, transactions):
    ...
    order = TradingOrder(..., transaction_id=transactions[0].id, ...)
```
Si un symbole est détenu via plusieurs transactions OPENED (ajouts successifs liés à `transactions[0]` à chaque buy — même travers côté buy ligne 318), un delta de vente supérieur à l'`open_qty` de la transaction 0 sera intégralement imputé à celle-ci : clôture "dépassée" dans le ledger, P&L réalisé mal attribué. **Fix :** répartir le qty sur les transactions (FIFO) ou clôturer transaction par transaction.

#### M6. PennyMomentumTrader : RVOL comparant barre en cours vs barres complétées
`packages/experts/ba2_experts/PennyMomentumTrader/conditions.py:798-873` (`_get_rvol`)

`current_bar = df.iloc[-1]` est la barre 30 min **en cours**, comparée à la moyenne historique des barres **complètes** du même créneau. En début de créneau, le volume accumulé est mécaniquement faible → RVOL systématiquement sous-estimé (jusqu'à ~0 au tout début), et le signal "volume spike" rate les premières minutes — précisément là où les breakouts penny sont les plus forts. La docstring affirme à tort que l'approche "works correctly at any time". **Fix :** normaliser par la fraction écoulée du créneau, ou n'utiliser que la dernière barre 30 min **complétée**.

#### M7. PennyMomentumTrader : fallback silencieux quand la session du jour est vide
`packages/experts/ba2_experts/PennyMomentumTrader/conditions.py:718-741` (`_filter_today_session`)

```python
return filtered if not filtered.empty else df
```
Avant l'ouverture (ou jour férié), VWAP/OR/volume sont calculés sur plusieurs jours au lieu de la session du jour : une condition `price_above_vwap` "intraday" peut déclencher sur une valeur multi-jours. **Fix :** retourner un signal "non évaluable" (None) hors session plutôt que des données multi-jours, ou filtrer aussi la veille pour les indicateurs explicitement intraday.

#### M8. `pred_lookup` construit sur index après tri — fragile
`testplatform/backend/app/services/backtest_handler.py:478-482`

```python
pred_timestamps = sorted([pd.Timestamp(d) for d in pred_dates])
pred_lookup = {ts: predictions[i] for i, ts in enumerate(pred_timestamps)}
```
La correspondance timestamp↔probabilités repose sur l'hypothèse que `pred_dates` est déjà strictement croissant sans doublon. Un dataset désordonné ou dupliqué décalerait silencieusement les signaux. **Fix :** construire le lookup avant le tri (`zip(pred_dates, predictions)`) puis trier la liste de clés.

#### M9. Opérateurs `is_true`/`is_false` incorrects sur chaînes
`testplatform/backend/app/services/strategy_executor.py:180-185`

```python
elif operator == "is_true":
    return bool(left) and left != 0      # bool("false") == True !
elif operator == "is_false":
    return not left or left == 0
```
Une valeur chaîne `"false"`/`"true"` issue d'un dataset ou d'un provider est évaluée truthy. **Fix :** normaliser les chaînes ("true"/"1"/"yes") avant le test booléen.

---

### 🟡 BAS

| # | Fichier:lig | Problème | Fix |
|---|---|---|---|
| B1 | `backtest_handler.py:638` | `position_sizing_pct = (position_sizing_value / initial_capital) * 100` — ZeroDivisionError si `initial_capital=0` | garde `initial_capital > 0` |
| B2 | `backtest_handler.py:1223-1233` | `except:` nu avec `pass` dans le chemin d'erreur final de `handle_backtest` | logger l'erreur de persistance |
| B3 | `worker_fleet.py:52`, `distributed_eval.py:183` | `datetime.utcnow()` (déprécié 3.12, naive) mélangé avec `datetime.now()` naïf ailleurs ; comparaison naive/aware possible | uniformiser `datetime.now(timezone.utc)` |
| B4 | `strategy_executor.py:81` (`_eval_stats`), `evaluate_condition._warned_fields` | État mutable au niveau module, partagé entre backtests successifs dans un même processus ; `reset_evaluation_stats()` existe mais les warned-sets de `evaluate_condition` ne sont pas réinitialisés | tout regrouper dans le reset |
| B5 | `strategy_executor.py:155` (`Position.unrealized_pnl_pct`) | propriété placeholder renvoyant toujours 0.0 | supprimer ou implémenter |
| B6 | `strategy_executor.py:233-244` | `confirmationRequired` utilisé à la fois comme booléen et comme compteur (`true_count >= True` ⇒ ≥1) ; si l'UI envoie `true`, la confirmation exige seulement 1 occurrence sur N barres | séparer flag et `requiredTimes` |
| B7 | Hygiène repo | Fichiers de travail énormes non suivis à la racine (`logs.bak.*.tar.gz` 157 Mo, `grid_prepare_2020_stress.log` 9,6 Mo, `mem_probe_*`, `.aider.chat.history.md` 200 Ko) ; `db/`, `OTHER_DB`, `testplatform/backend/db` non suivis | nettoyer/déplacer dans `logs/` ignoré ; vérifier qu'aucun ne sera commité par accident |
| B8 | `creds.env` à la racine | Contient des secrets (non tracké, correctement gitignoré via `*.env` — vérifié). Risque uniquement local | OK en l'état ; ne jamais committer |
| B9 | `AlpacaAccount.py` (271 Ko), `FMPSenateTraderWeight.py` (225 Ko), `job_handler.py` (153 Ko), `TradeManager.py` (123 Ko) | Fichiers monolithiques difficiles à revoir/tester | découpage progressif (déjà amorcé : extraction `_stage_recommendation_candidate`, etc.) |

---

## 3. Revue des stratégies (experts)

Remarque générale : le cadre est sain — séparation `_gather` (I/O, point-in-time via `as_of`) / `_process` (pur), mêmes chemins live et backtest, guards anti-lookahead explicites (`AsOfClampedOHLCVProvider`, `_counts_as_of`, `hermetic_fmp_history`). Les biais classiques de backtest sont bien mieux traités que dans la plupart des plateformes retail. Les remarques ci-dessous portent sur la logique financière.

### FMPEarningsDrift (`packages/experts/ba2_experts/FMPEarningsDrift.py`)
- **Forces :** anomalie PEAD documentée ; calcul de surprise robuste (`abs(estimated)`, fallback si `surprise_percent` absent) ; fenêtre de fraîcheur ; rejet des rapports futurs (`days_since < 0`) → pas de look-ahead.
- **Limites :** aucune distinction BMO/AMC : un rapport après clôture (AMC) déclenche un BUY dès le lendemain… mais le gap post-earnings a souvent déjà eu lieu à l'open ; l'entrée se fait alors sur le prix ajusté. La littérature PEAD suggère que le drift net persiste, mais la taille du gain dépend fortement du moment d'entrée.
- **Suggestions :** (a) ajouter un filtre de régime de marché (déjà en cours d'intégration plateforme, commit `6615ac5`) ; (b) calibrer empiriquement la formule de confiance ad hoc (55 + 2×surprise + bonus fraîcheur) sur l'historique plutôt que des constantes arbitraires ; (c) `expected_profit_percent=8%` statique devrait être confronté au drift réel observé par décile de surprise.

### FMPRating (`packages/experts/ba2_experts/FMPRating.py`)
- **Forces :** reconstruction as-of soignée (grades-historical + price-target-history avec `publishedDate <= as_of`), garde anti-consensus dégénéré (`min_price_targets_per_quarter`), filtre de récence des analystes.
- **Limites :** les price targets sont connus pour être ancrés/laggards (révision lente après momentum) ; raisonner sur le *niveau* du consensus plutôt que sa *révision* expose à acheter après que l'upside est déjà intégré.
- **Suggestions :** (a) tester une variante "révision de consensus" (Δ target 30-90 j) plutôt que niveau ; (b) haircut systématique sur le target (les targets FMP sont optimistes) ; (c) vérifier la sensibilité au paramètre `min_price_targets_per_quarter` — un consensus à 2-3 analystes reste bruité même au-dessus du seuil.

### FactorRanker (`packages/experts/ba2_experts/FactorRanker/`)
- **Forces :** rebalance déterministe (tri des symboles pour la reproductibilité numérique, commentaires sur l'incident 2026-07-14 de duplication de position corrigée via `include_waiting=True`).
- **Limites :** (a) le stop-loss par nom est défini comme *perte en $ ≥ risk_pct% de l'équité totale* (`stop_loss_sells`, portfolio.py:62-88) — c'est un stop de portefeuille déguisé : sur un livre de 20 positions, une position doit perdre ~20×risk_pct% pour déclencher ; probablement trop lâche pour un stop individuel ; (b) achats MARKET sans contrainte de participation au volume ni limite de spread ; (c) bug M5 de rattachement multi-transactions.
- **Suggestions :** stop par position en % du prix ou en % de la valeur de la position (paramètre distinct), ordres limit IOC proches du mid pour réduire le coût d'exécution sur small/mid caps.

### PennyMomentumTrader (`packages/experts/ba2_experts/PennyMomentumTrader/`)
- **Forces :** trailing ratchet monotone bien conçu (post-mortem SKYQ documenté), stops structurés évalués à chaque tick.
- **Limites :** (a) conditions générées/éditées par LLM → risque d'overfitting narratif et de conditions non reproductibles ; (b) bugs M6/M7 sur RVOL et session ; (c) univers penny = risque de liquidité/survivorship : les gardes de participation au volume du moteur de backtest daily existent (`_OPTION_FILL_MAX_VOLUME_PARTICIPATION`) mais vérifier qu'un équivalent s'applique aux entrées equity penny (sinon le backtest remplit des ordres impossibles en live) ; (d) `_check_volume_spike` compare les N dernières minutes à la moyenne de 5 jours toutes sessions confondues (pré/post-market inclus).
- **Suggestions :** geler un jeu de conditions validées par backtest plutôt que laisser le LLM les réécrire en continu ; ajouter un slippage/spread explicite pour les penny ; hard cap de taille en % du volume journalier.

### PremiumSeller (`packages/experts/ba2_experts/PremiumSeller/`)
- **Forces :** modélisation prudente exemplaire — fill au bid/ask (`_mid_credit`), None = pas de trade (jamais de valeur fabriquée), sizing à risque défini, garde de liquidité ajoutée le 2026-07-25, exclusion earnings.
- **Limites :** (a) `earnings_within` utilise les dates *passées* de résultats comme proxy des dates *futures* — dérive possible de quelques jours, acceptable pour une fenêtre 30-45 DTE mais à surveiller ; (b) pas de gestion de sortie anticipée visible (prendre 50% du crédit max améliore nettement le Sharpe des stratégies premium) ; (c) le short strangle utilise `per_risk = max(strike_put, strike_call) × 100`, estimation de stress raisonnable mais pas une marge réelle (vérifier l'alignement avec les exigences broker en live).
- **Suggestions :** règle de sortie à 50% du profit max + stop à ~2× le crédit ; suivre l'IV rank réalisé vs ex-ante pour valider l'edge.

### FMPSenateTraderWeight / Copy
Non auditées en profondeur (fichiers énormes, 225 Ko / 90 Ko). Le dispatch basket a été récemment sécurisé (garde de type `isinstance(recs, list)` dans `daily_engine._run_basket_expert_bar`). Recommandation : découper en modules et ajouter des tests de parité gather/process comme les autres experts (déjà fait pour FMPRating/FinHub/EarningsDrift).

### Fitness GA (`strategy_fitness.py`)
Très bon design : sentinelles distinctes (0 trade / crash / compte explosé), `consistent_annual_return` avec garde de drawdown continue (fix du 2026-08-04 documenté), rampe proportionnelle de fréquence de trades plutôt que falaise, facteurs appliqués seulement aux fitness positives (pas d'inversion de signe). **Reste le vrai problème : tout ceci optimise in-sample (H3).**

---

## 4. Qualité / tests / dette technique

- **Tests : 366 fichiers**, dont 75 dédiés au backtest (golden regressions, parité live/backtest, hermetic cache miss "loud", no-lookahead, fills d'options, margin/liquidation). Couverture des zones critiques excellente côté moteur daily et exécution. Côté moteur ML legacy : couverture plus mince — c'est là que vivent H1/H2/M1/M2.
- **Sécurité :** worker_server authentifié (Bearer, `hmac.compare_digest`, fail-closed 503) ; pas de SQL brut f-stringué (ORM SQLModel/SQLAlchemy partout) ; `creds.env` correctement ignoré ; `.env.example` sans vrais secrets ; pas de `except: pass` dans le code critique.
- **Timezones :** le code live utilise `datetime.now(timezone.utc)` de façon disciplinée dans les chemins d'ordres ; la session de marché est gérée en `US/Eastern` (pytz) dans les conditions intraday. Les naive `datetime.now()` restants sont dans du code non financier (billing, exports), sauf B3.
- **Dette :** deux moteurs de backtest parallèles dont un legacy sous-maintenu (source des bugs Hauts) ; fichiers monolithiques (B9) ; 3 tests rouges (H4) ; logs volumineux à la racine (B7).

---

## 5. Recommandations prioritaires (top 5)

1. **Corriger le moteur ML legacy ou le condamner** : appliquer réellement le slippage (H1) et éliminer l'état de classe partagé (H2). Si le moteur daily est l'avenir, marquer le moteur ML "legacy/non supporté" dans l'UI et bloquer l'optimisation GA dessus.
2. **Ajouter une validation out-of-sample au pipeline d'optimisation** (H3) : fenêtre de holdout intouchable par le GA, ou walk-forward ; sans cela, les résultats des grilles `goal2020` ne sont pas fiables pour le live.
3. **Repasser la suite de tests d'optimisation au vert** (H4) et ajouter une garde CI — la zone la plus activement développée a précisément les tests cassés.
4. **Vérifier le cas wash-trade OCO** (M4) avec les logs live Alpaca (rechercher `40310000` avec un OCO au repos) : c'est la même classe d'incident que celui du 2026-08-03 qui a laissé une position UBER sans stop.
5. **Sécuriser le claim de tâches multi-processus** (M3) par un update conditionnel atomique, avant d'étendre la flotte de workers.

---

## Annexe — Méthodologie
- Cartographie (`git log -20`, arborescences), puis bug hunt ciblé par grep (exceptions silencieuses, SQL, datetimes, divisions, état partagé) sur les zones à risque : moteurs de backtest, GA, exécution d'ordres live, sizing, providers OHLCV, experts.
- Lecture approfondie : `backtest_handler.py`, `strategy_executor.py`, `strategy_fitness.py`, `daily_engine.py` (extraits), `backtest_account.py` (fill engine), `price_source.py`, `task_queue.py`, `AccountInterface.py` (wash-trade), `TradeManager.py` (retry), `AlpacaAccount.py` (TP/SL/OCO), `position_sizing.py`, `FactorRanker/portfolio.py`, `PennyMomentumTrader/{trailing,conditions}.py`, `PremiumSeller/{signals,structures}.py`, `FMPEarningsDrift.py`, `FMPRating.py` (extraits), `worker_server.py` (auth).
- Exécution : `pytest tests/test_strategy_optimization_handler.py` (venv repo) → 3 failed / 32 passed. Aucune modification du repo.
