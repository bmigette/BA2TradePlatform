"""
Standardized data models for financial statements.

These dataclasses define the canonical field names that all providers
should map their data to, ensuring compatibility across providers.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional, List, Dict, Any


@dataclass
class BalanceSheetPeriod:
    """Standardized balance sheet data for a single period."""

    # Period identification
    fiscal_date: str  # YYYY-MM-DD format
    reported_currency: str = "USD"

    # Assets
    total_assets: Optional[float] = None
    total_current_assets: Optional[float] = None
    cash_and_cash_equivalents: Optional[float] = None
    short_term_investments: Optional[float] = None
    cash_and_short_term_investments: Optional[float] = None
    net_receivables: Optional[float] = None
    inventory: Optional[float] = None
    other_current_assets: Optional[float] = None

    total_non_current_assets: Optional[float] = None
    property_plant_equipment: Optional[float] = None
    goodwill: Optional[float] = None
    intangible_assets: Optional[float] = None
    long_term_investments: Optional[float] = None
    other_non_current_assets: Optional[float] = None

    # Liabilities
    total_liabilities: Optional[float] = None
    total_current_liabilities: Optional[float] = None
    accounts_payable: Optional[float] = None
    short_term_debt: Optional[float] = None
    deferred_revenue: Optional[float] = None
    other_current_liabilities: Optional[float] = None

    total_non_current_liabilities: Optional[float] = None
    long_term_debt: Optional[float] = None
    other_non_current_liabilities: Optional[float] = None

    # Equity
    total_stockholders_equity: Optional[float] = None
    common_stock: Optional[float] = None
    retained_earnings: Optional[float] = None
    treasury_stock: Optional[float] = None

    # Calculated metrics
    total_debt: Optional[float] = None
    net_debt: Optional[float] = None
    working_capital: Optional[float] = None
    book_value: Optional[float] = None
    tangible_book_value: Optional[float] = None
    shares_outstanding: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary, excluding None values."""
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class IncomeStatementPeriod:
    """Standardized income statement data for a single period."""

    # Period identification
    fiscal_date: str  # YYYY-MM-DD format
    reported_currency: str = "USD"

    # Revenue
    total_revenue: Optional[float] = None
    cost_of_revenue: Optional[float] = None
    gross_profit: Optional[float] = None

    # Operating expenses
    operating_expenses: Optional[float] = None
    research_and_development: Optional[float] = None
    selling_general_administrative: Optional[float] = None
    depreciation_and_amortization: Optional[float] = None

    # Operating income
    operating_income: Optional[float] = None
    ebit: Optional[float] = None
    ebitda: Optional[float] = None

    # Other income/expenses
    interest_income: Optional[float] = None
    interest_expense: Optional[float] = None
    other_income_expense: Optional[float] = None

    # Net income
    income_before_tax: Optional[float] = None
    income_tax_expense: Optional[float] = None
    net_income: Optional[float] = None
    net_income_continuing_operations: Optional[float] = None

    # Per share data
    basic_eps: Optional[float] = None
    diluted_eps: Optional[float] = None
    basic_shares_outstanding: Optional[float] = None
    diluted_shares_outstanding: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary, excluding None values."""
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class CashFlowPeriod:
    """Standardized cash flow statement data for a single period."""

    # Period identification
    fiscal_date: str  # YYYY-MM-DD format
    reported_currency: str = "USD"

    # Operating activities
    operating_cash_flow: Optional[float] = None
    net_income: Optional[float] = None
    depreciation_and_amortization: Optional[float] = None
    stock_based_compensation: Optional[float] = None
    deferred_income_tax: Optional[float] = None
    change_in_working_capital: Optional[float] = None
    change_in_receivables: Optional[float] = None
    change_in_inventory: Optional[float] = None
    change_in_payables: Optional[float] = None
    other_operating_activities: Optional[float] = None

    # Investing activities
    investing_cash_flow: Optional[float] = None
    capital_expenditure: Optional[float] = None
    acquisitions: Optional[float] = None
    purchases_of_investments: Optional[float] = None
    sales_of_investments: Optional[float] = None
    other_investing_activities: Optional[float] = None

    # Financing activities
    financing_cash_flow: Optional[float] = None
    debt_repayment: Optional[float] = None
    debt_issuance: Optional[float] = None
    stock_repurchased: Optional[float] = None
    stock_issued: Optional[float] = None
    dividends_paid: Optional[float] = None
    other_financing_activities: Optional[float] = None

    # Net change
    net_change_in_cash: Optional[float] = None
    beginning_cash_position: Optional[float] = None
    ending_cash_position: Optional[float] = None

    # Calculated metrics
    free_cash_flow: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary, excluding None values."""
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class EarningsPeriod:
    """Standardized earnings data for a single period."""

    fiscal_date: str  # YYYY-MM-DD format
    report_date: Optional[str] = None  # Actual report date

    reported_eps: Optional[float] = None
    estimated_eps: Optional[float] = None
    surprise: Optional[float] = None
    surprise_percent: Optional[float] = None

    # Additional earnings data (if available)
    revenue: Optional[float] = None
    revenue_estimated: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary, excluding None values."""
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class FinancialStatementResponse:
    """Standardized response wrapper for financial statements."""

    symbol: str
    provider: str
    statement_type: str  # 'balance_sheet', 'income_statement', 'cash_flow', 'earnings'
    frequency: str  # 'quarterly' or 'annual'

    # Date range
    start_date: Optional[str] = None
    end_date: Optional[str] = None

    # Data - only one will be populated based on statement_type
    periods: List[Dict[str, Any]] = field(default_factory=list)

    # Metadata
    retrieved_at: str = field(default_factory=lambda: datetime.now().isoformat())
    period_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "symbol": self.symbol,
            "provider": self.provider,
            "statement_type": self.statement_type,
            "frequency": self.frequency,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "periods": self.periods,
            "retrieved_at": self.retrieved_at,
            "period_count": self.period_count or len(self.periods)
        }


# Field name mappings from various providers to standardized names
# These help convert provider-specific field names to our canonical format

YFINANCE_BALANCE_SHEET_MAPPING = {
    # Assets
    "Total Assets": "total_assets",
    "Current Assets": "total_current_assets",
    "Total Current Assets": "total_current_assets",
    "Cash And Cash Equivalents": "cash_and_cash_equivalents",
    "Cash Cash Equivalents And Short Term Investments": "cash_and_short_term_investments",
    "Short Term Investments": "short_term_investments",
    "Net Receivables": "net_receivables",
    "Receivables": "net_receivables",
    "Inventory": "inventory",
    "Other Current Assets": "other_current_assets",
    "Total Non Current Assets": "total_non_current_assets",
    "Net PPE": "property_plant_equipment",
    "Gross PPE": "property_plant_equipment",
    "Goodwill": "goodwill",
    "Goodwill And Other Intangible Assets": "intangible_assets",
    "Other Intangible Assets": "intangible_assets",
    "Long Term Investments": "long_term_investments",
    "Investments And Advances": "long_term_investments",
    "Other Non Current Assets": "other_non_current_assets",

    # Liabilities
    "Total Liabilities Net Minority Interest": "total_liabilities",
    "Total Liabilities": "total_liabilities",
    "Current Liabilities": "total_current_liabilities",
    "Total Current Liabilities": "total_current_liabilities",
    "Accounts Payable": "accounts_payable",
    "Payables": "accounts_payable",
    "Current Debt": "short_term_debt",
    "Current Debt And Capital Lease Obligation": "short_term_debt",
    "Deferred Revenue": "deferred_revenue",
    "Other Current Liabilities": "other_current_liabilities",
    "Total Non Current Liabilities Net Minority Interest": "total_non_current_liabilities",
    "Total Non Current Liabilities": "total_non_current_liabilities",
    "Long Term Debt": "long_term_debt",
    "Long Term Debt And Capital Lease Obligation": "long_term_debt",
    "Other Non Current Liabilities": "other_non_current_liabilities",

    # Equity
    "Stockholders Equity": "total_stockholders_equity",
    "Total Stockholders Equity": "total_stockholders_equity",
    "Total Equity Gross Minority Interest": "total_stockholders_equity",
    "Common Stock Equity": "total_stockholders_equity",
    "Common Stock": "common_stock",
    "Retained Earnings": "retained_earnings",
    "Treasury Stock": "treasury_stock",
    "Treasury Shares Number": "treasury_stock",

    # Calculated
    "Total Debt": "total_debt",
    "Net Debt": "net_debt",
    "Working Capital": "working_capital",
    "Tangible Book Value": "tangible_book_value",
    "Ordinary Shares Number": "shares_outstanding",
    "Share Issued": "shares_outstanding",
}

YFINANCE_INCOME_STATEMENT_MAPPING = {
    "Total Revenue": "total_revenue",
    "Operating Revenue": "total_revenue",
    "Cost Of Revenue": "cost_of_revenue",
    "Reconciled Cost Of Revenue": "cost_of_revenue",
    "Gross Profit": "gross_profit",

    "Operating Expense": "operating_expenses",
    "Total Operating Expenses": "operating_expenses",
    "Research And Development": "research_and_development",
    "Selling General And Administration": "selling_general_administrative",
    "Selling General And Administrative": "selling_general_administrative",
    "Reconciled Depreciation": "depreciation_and_amortization",

    "Operating Income": "operating_income",
    "EBIT": "ebit",
    "EBITDA": "ebitda",
    "Normalized EBITDA": "ebitda",

    "Interest Income": "interest_income",
    "Interest Expense": "interest_expense",
    "Net Interest Income": "interest_income",
    "Other Non Operating Income Expenses": "other_income_expense",

    "Pretax Income": "income_before_tax",
    "Tax Provision": "income_tax_expense",
    "Net Income": "net_income",
    "Net Income Common Stockholders": "net_income",
    "Net Income From Continuing Operations": "net_income_continuing_operations",
    "Net Income From Continuing Operation Net Minority Interest": "net_income_continuing_operations",

    "Basic EPS": "basic_eps",
    "Diluted EPS": "diluted_eps",
    "Basic Average Shares": "basic_shares_outstanding",
    "Diluted Average Shares": "diluted_shares_outstanding",
}

YFINANCE_CASH_FLOW_MAPPING = {
    # Operating
    "Operating Cash Flow": "operating_cash_flow",
    "Cash Flow From Continuing Operating Activities": "operating_cash_flow",
    "Net Income From Continuing Operations": "net_income",
    "Depreciation And Amortization": "depreciation_and_amortization",
    "Depreciation Amortization Depletion": "depreciation_and_amortization",
    "Stock Based Compensation": "stock_based_compensation",
    "Deferred Income Tax": "deferred_income_tax",
    "Deferred Tax": "deferred_income_tax",
    "Change In Working Capital": "change_in_working_capital",
    "Change In Receivables": "change_in_receivables",
    "Change In Inventory": "change_in_inventory",
    "Change In Payables And Accrued Expense": "change_in_payables",
    "Change In Payable": "change_in_payables",
    "Other Non Cash Items": "other_operating_activities",

    # Investing
    "Investing Cash Flow": "investing_cash_flow",
    "Cash Flow From Continuing Investing Activities": "investing_cash_flow",
    "Capital Expenditure": "capital_expenditure",
    "Purchase Of Investment": "purchases_of_investments",
    "Sale Of Investment": "sales_of_investments",
    "Net Business Purchase And Sale": "acquisitions",
    "Net Investment Purchase And Sale": "other_investing_activities",

    # Financing
    "Financing Cash Flow": "financing_cash_flow",
    "Cash Flow From Continuing Financing Activities": "financing_cash_flow",
    "Repayment Of Debt": "debt_repayment",
    "Issuance Of Debt": "debt_issuance",
    "Long Term Debt Issuance": "debt_issuance",
    "Long Term Debt Payments": "debt_repayment",
    "Repurchase Of Capital Stock": "stock_repurchased",
    "Common Stock Issuance": "stock_issued",
    "Common Stock Dividend Paid": "dividends_paid",
    "Cash Dividends Paid": "dividends_paid",

    # Net change
    "Changes In Cash": "net_change_in_cash",
    "Change In Cash Supplemental As Reported": "net_change_in_cash",
    "Beginning Cash Position": "beginning_cash_position",
    "End Cash Position": "ending_cash_position",
    "Free Cash Flow": "free_cash_flow",
}

FMP_BALANCE_SHEET_MAPPING = {
    "fiscal_date_ending": "fiscal_date",
    "reported_currency": "reported_currency",
    "total_assets": "total_assets",
    "total_current_assets": "total_current_assets",
    "cash_and_cash_equivalents": "cash_and_cash_equivalents",
    "cash_and_short_term_investments": "cash_and_short_term_investments",
    "short_term_investments": "short_term_investments",
    "current_net_receivables": "net_receivables",
    "inventory": "inventory",
    "other_current_assets": "other_current_assets",
    "total_non_current_assets": "total_non_current_assets",
    "property_plant_equipment": "property_plant_equipment",
    "goodwill": "goodwill",
    "intangible_assets": "intangible_assets",
    "long_term_investments": "long_term_investments",
    "other_non_current_assets": "other_non_current_assets",
    "total_liabilities": "total_liabilities",
    "total_current_liabilities": "total_current_liabilities",
    "current_accounts_payable": "accounts_payable",
    "short_term_debt": "short_term_debt",
    "deferred_revenue": "deferred_revenue",
    "other_current_liabilities": "other_current_liabilities",
    "total_non_current_liabilities": "total_non_current_liabilities",
    "long_term_debt": "long_term_debt",
    "other_non_current_liabilities": "other_non_current_liabilities",
    "total_shareholder_equity": "total_stockholders_equity",
    "common_stock": "common_stock",
    "retained_earnings": "retained_earnings",
    "common_stock_shares_outstanding": "shares_outstanding",
    "net_debt": "net_debt",
    "working_capital": "working_capital",
}

FMP_INCOME_STATEMENT_MAPPING = {
    "fiscal_date_ending": "fiscal_date",
    "reported_currency": "reported_currency",
    "total_revenue": "total_revenue",
    "cost_of_revenue": "cost_of_revenue",
    "gross_profit": "gross_profit",
    "operating_expenses": "operating_expenses",
    "research_and_development": "research_and_development",
    "selling_general_administrative": "selling_general_administrative",
    "depreciation_and_amortization": "depreciation_and_amortization",
    "operating_income": "operating_income",
    "interest_income": "interest_income",
    "interest_expense": "interest_expense",
    "other_income_expense": "other_income_expense",
    "income_before_tax": "income_before_tax",
    "income_tax_expense": "income_tax_expense",
    "net_income": "net_income",
    "eps": "basic_eps",
    "eps_diluted": "diluted_eps",
    "ebitda": "ebitda",
    "weighted_average_shares_outstanding": "basic_shares_outstanding",
    "weighted_average_shares_diluted": "diluted_shares_outstanding",
}

FMP_CASH_FLOW_MAPPING = {
    "fiscal_date_ending": "fiscal_date",
    "reported_currency": "reported_currency",
    "operating_cash_flow": "operating_cash_flow",
    "net_income": "net_income",
    "depreciation_and_amortization": "depreciation_and_amortization",
    "stock_based_compensation": "stock_based_compensation",
    "deferred_income_tax": "deferred_income_tax",
    "change_in_working_capital": "change_in_working_capital",
    "change_in_receivables": "change_in_receivables",
    "change_in_inventory": "change_in_inventory",
    "change_in_payables": "change_in_payables",
    "other_operating_activities": "other_operating_activities",
    "investing_cash_flow": "investing_cash_flow",
    "capital_expenditures": "capital_expenditure",
    "acquisitions": "acquisitions",
    "investments": "purchases_of_investments",
    "other_investing_activities": "other_investing_activities",
    "financing_cash_flow": "financing_cash_flow",
    "debt_repayment": "debt_repayment",
    "common_stock_issued": "stock_issued",
    "common_stock_repurchased": "stock_repurchased",
    "dividends_paid": "dividends_paid",
    "other_financing_activities": "other_financing_activities",
    "net_change_in_cash": "net_change_in_cash",
    "free_cash_flow": "free_cash_flow",
}

# AlphaVantage mappings (camelCase field names)
ALPHAVANTAGE_BALANCE_SHEET_MAPPING = {
    "fiscalDateEnding": "fiscal_date",
    "reportedCurrency": "reported_currency",
    "totalAssets": "total_assets",
    "totalCurrentAssets": "total_current_assets",
    "cashAndCashEquivalentsAtCarryingValue": "cash_and_cash_equivalents",
    "cashAndShortTermInvestments": "cash_and_short_term_investments",
    "shortTermInvestments": "short_term_investments",
    "currentNetReceivables": "net_receivables",
    "inventory": "inventory",
    "otherCurrentAssets": "other_current_assets",
    "totalNonCurrentAssets": "total_non_current_assets",
    "propertyPlantEquipment": "property_plant_equipment",
    "goodwill": "goodwill",
    "intangibleAssets": "intangible_assets",
    "intangibleAssetsExcludingGoodwill": "intangible_assets",
    "longTermInvestments": "long_term_investments",
    "otherNonCurrentAssets": "other_non_current_assets",
    "totalLiabilities": "total_liabilities",
    "totalCurrentLiabilities": "total_current_liabilities",
    "currentAccountsPayable": "accounts_payable",
    "shortTermDebt": "short_term_debt",
    "currentLongTermDebt": "short_term_debt",
    "deferredRevenue": "deferred_revenue",
    "otherCurrentLiabilities": "other_current_liabilities",
    "totalNonCurrentLiabilities": "total_non_current_liabilities",
    "longTermDebt": "long_term_debt",
    "longTermDebtNoncurrent": "long_term_debt",
    "otherNonCurrentLiabilities": "other_non_current_liabilities",
    "totalShareholderEquity": "total_stockholders_equity",
    "commonStock": "common_stock",
    "retainedEarnings": "retained_earnings",
    "treasuryStock": "treasury_stock",
    "commonStockSharesOutstanding": "shares_outstanding",
}

ALPHAVANTAGE_INCOME_STATEMENT_MAPPING = {
    "fiscalDateEnding": "fiscal_date",
    "reportedCurrency": "reported_currency",
    "totalRevenue": "total_revenue",
    "costOfRevenue": "cost_of_revenue",
    "costofGoodsAndServicesSold": "cost_of_revenue",
    "grossProfit": "gross_profit",
    "operatingExpenses": "operating_expenses",
    "researchAndDevelopment": "research_and_development",
    "sellingGeneralAndAdministrative": "selling_general_administrative",
    "depreciationAndAmortization": "depreciation_and_amortization",
    "operatingIncome": "operating_income",
    "interestIncome": "interest_income",
    "interestExpense": "interest_expense",
    "otherNonOperatingIncome": "other_income_expense",
    "incomeBeforeTax": "income_before_tax",
    "incomeTaxExpense": "income_tax_expense",
    "netIncome": "net_income",
    "netIncomeFromContinuingOperations": "net_income_continuing_operations",
    "ebit": "ebit",
    "ebitda": "ebitda",
}

ALPHAVANTAGE_CASH_FLOW_MAPPING = {
    "fiscalDateEnding": "fiscal_date",
    "reportedCurrency": "reported_currency",
    "operatingCashflow": "operating_cash_flow",
    "netIncome": "net_income",
    "depreciationDepletionAndAmortization": "depreciation_and_amortization",
    "deferredIncomeTax": "deferred_income_tax",
    "changeInOperatingLiabilities": "change_in_payables",
    "changeInOperatingAssets": "change_in_receivables",
    "changeInReceivables": "change_in_receivables",
    "changeInInventory": "change_in_inventory",
    "cashflowFromInvestment": "investing_cash_flow",
    "capitalExpenditures": "capital_expenditure",
    "cashflowFromFinancing": "financing_cash_flow",
    "dividendPayout": "dividends_paid",
    "dividendPayoutCommonStock": "dividends_paid",
    "paymentsForRepurchaseOfCommonStock": "stock_repurchased",
    "proceedsFromIssuanceOfCommonStock": "stock_issued",
    "proceedsFromRepaymentOfShortTermDebt": "debt_repayment",
    "changeInCashAndCashEquivalents": "net_change_in_cash",
}


def normalize_field_name(raw_name: str, mapping: Dict[str, str]) -> Optional[str]:
    """Convert a raw field name to standardized name using mapping."""
    return mapping.get(raw_name)


def parse_numeric_value(value: Any) -> Any:
    """
    Parse a value that might be a formatted string into a numeric value.

    Handles formats like:
    - "$27.47B" -> 27470000000.0
    - "$1.5M" -> 1500000.0
    - "$250K" -> 250000.0
    - "27.47B" -> 27470000000.0
    - "-$1.5M" -> -1500000.0
    - Already numeric values -> returned as-is
    - None/empty -> None

    Args:
        value: The value to parse (string, number, or None)

    Returns:
        Parsed numeric value or original value if not parseable
    """
    if value is None:
        return None

    # Already a number
    if isinstance(value, (int, float)):
        return value

    # Not a string, return as-is
    if not isinstance(value, str):
        return value

    # Empty string
    value = value.strip()
    if not value or value == '-' or value.lower() == 'none':
        return None

    try:
        # Remove currency symbols and commas
        clean = value.replace('$', '').replace(',', '').strip()

        # Check for negative sign
        negative = False
        if clean.startswith('-'):
            negative = True
            clean = clean[1:].strip()
        elif clean.startswith('(') and clean.endswith(')'):
            negative = True
            clean = clean[1:-1].strip()

        # Handle suffixes (B, M, K, T)
        multiplier = 1.0
        if clean.endswith('T') or clean.endswith('t'):
            multiplier = 1e12
            clean = clean[:-1]
        elif clean.endswith('B') or clean.endswith('b'):
            multiplier = 1e9
            clean = clean[:-1]
        elif clean.endswith('M') or clean.endswith('m'):
            multiplier = 1e6
            clean = clean[:-1]
        elif clean.endswith('K') or clean.endswith('k'):
            multiplier = 1e3
            clean = clean[:-1]

        # Handle percentage
        if clean.endswith('%'):
            clean = clean[:-1]
            multiplier = 0.01

        # Parse the number
        result = float(clean) * multiplier

        if negative:
            result = -result

        return result

    except (ValueError, AttributeError):
        # Could not parse, return original
        return value


def apply_mapping(data: Dict[str, Any], mapping: Dict[str, str], strict: bool = True) -> Dict[str, Any]:
    """
    Apply field name mapping to a data dictionary.

    Args:
        data: Raw data dictionary from provider
        mapping: Field name mapping (raw -> canonical)
        strict: If True, only include fields that have mappings. If False, include unmapped fields with original names.

    Returns:
        Dictionary with normalized field names and parsed numeric values
    """
    result = {}
    for raw_key, value in data.items():
        normalized_key = mapping.get(raw_key)

        if normalized_key:
            # Parse the value to numeric if possible
            parsed_value = parse_numeric_value(value)
            if parsed_value is not None:
                result[normalized_key] = parsed_value
        elif not strict and value is not None:
            # In non-strict mode, keep unmapped fields with parsed values
            parsed_value = parse_numeric_value(value)
            if parsed_value is not None:
                result[raw_key] = parsed_value

    return result
