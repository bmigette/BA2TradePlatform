"""Interface for default-assumption valuation snapshot compute providers.

A valuation-snapshot provider computes (never fetches beyond its composed
fundamentals providers) a WACC + DCF + sensitivity snapshot for a symbol from
fundamentals data. Every assumption is a constructor parameter of the
implementation and is printed verbatim in the rendered report. Output follows
the standard format_type contract (markdown / dict / both).
"""

from abc import abstractmethod
from typing import Annotated, Any, Dict, Literal
from datetime import datetime

from ba2_common.core.interfaces.DataProviderInterface import DataProviderInterface


class ValuationSnapshotInterface(DataProviderInterface):
    """Interface for valuation-snapshot compute providers."""

    @abstractmethod
    def get_valuation_snapshot(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        as_of_date: Annotated[datetime, "Analysis date — nothing after this date may be read"],
        format_type: Literal["markdown", "dict", "both"] = "markdown",
    ) -> Dict[str, Any] | str:
        """Compute a DEFAULT-ASSUMPTION valuation snapshot (WACC + DCF + sensitivity).

        Every assumption is a constructor parameter of the implementation and must
        be printed verbatim in the rendered report. When required fundamentals are
        missing the report says 'not computable: <reason>' — never an exception,
        never a fabricated number.
        """
