"""NiceGUI rendering of a FactorRanker MarketAnalysis (the ranked book)."""

from ba2_common.core.types import MarketAnalysisStatus


def render_market_analysis(expert, market_analysis) -> None:
    """Render the ranked book stored in ``market_analysis.state['factor_ranker']``.

    Mirrors FMPRating's status handling (PENDING/RUNNING/FAILED/SKIPPED/COMPLETED).
    """
    from nicegui import ui

    status = market_analysis.status
    state = market_analysis.state or {}
    book = state.get("factor_ranker", {})

    try:
        if status == MarketAnalysisStatus.PENDING:
            with ui.card().classes("w-full p-8 text-center"):
                ui.icon("schedule", size="3rem", color="grey").classes("mb-2")
                ui.label("Analysis Pending").classes("text-h5")
            return

        if status == MarketAnalysisStatus.RUNNING:
            with ui.card().classes("w-full p-8 text-center"):
                ui.spinner(size="3rem", color="primary").classes("mb-2")
                ui.label("Ranking universe…").classes("text-h5")
            return

        if status == MarketAnalysisStatus.FAILED:
            with ui.card().classes("w-full p-4"):
                ui.label("Analysis Failed").classes("text-h5 text-negative")
                ui.label(f"Error: {book.get('error', 'Unknown error')}").classes("text-grey-8")
            return

        if status == MarketAnalysisStatus.SKIPPED:
            with ui.card().classes("w-full p-4"):
                ui.label("Analysis Skipped").classes("text-h5 text-orange")
                ui.label(f"Reason: {book.get('reason', 'n/a')}").classes("text-grey-8")
            return

        if status != MarketAnalysisStatus.COMPLETED or not book:
            with ui.card().classes("w-full p-4"):
                ui.label("No analysis data available").classes("text-grey-7")
            return

        _render_book(ui, book)

    except Exception as e:
        expert.logger.error(f"FactorRanker: render failed: {e}", exc_info=True)
        with ui.card().classes("w-full p-8 text-center"):
            ui.icon("error", color="negative", size="3rem")
            ui.label("Rendering Error").classes("text-h5 text-negative")
            ui.label(str(e)).classes("text-grey-7")


def _render_book(ui, book: dict) -> None:
    ranking = book.get("ranking", [])
    factor_names = list(ranking[0]["factors"].keys()) if ranking else []

    # Header summary.
    with ui.card().classes("w-full"):
        with ui.card_section().style("background: linear-gradient(135deg, #00d4aa 0%, #00b894 100%)"):
            ui.label("FactorRanker — Ranked Book").classes("text-h5 text-weight-bold").style("color: white")
            ui.label(
                f"{book.get('held_count', 0)} held of {book.get('universe_size', 0)} ranked"
                f"  ·  gross {book.get('gross_exposure', 0):.2f}"
                f"  ·  rebalanced {book.get('rebalanced_at', '')}"
            ).style("color: rgba(255,255,255,0.85)")

        with ui.card_section():
            weights = book.get("weights", {})
            if weights:
                ui.label("Active factor weights: " + ", ".join(f"{k}={v}" for k, v in weights.items())).classes("text-grey-8")

            columns = [
                {"name": "rank", "label": "#", "field": "rank", "sortable": True, "align": "left"},
                {"name": "symbol", "label": "Symbol", "field": "symbol", "sortable": True, "align": "left"},
            ]
            for fname in factor_names:
                columns.append({"name": fname, "label": f"{fname} z", "field": fname, "sortable": True, "align": "right"})
            columns += [
                {"name": "composite", "label": "Composite", "field": "composite", "sortable": True, "align": "right"},
                {"name": "target_weight", "label": "Target wt", "field": "target_weight", "sortable": True, "align": "right"},
                {"name": "action", "label": "Action", "field": "action", "align": "center"},
            ]

            rows = []
            for row in ranking:
                flat = {
                    "rank": row["rank"],
                    "symbol": row["symbol"],
                    "composite": row["composite"],
                    "target_weight": row["target_weight"],
                    "action": row["action"],
                }
                for fname in factor_names:
                    flat[fname] = row["factors"].get(fname)
                rows.append(flat)

            ui.table(columns=columns, rows=rows, row_key="symbol").classes("w-full")
