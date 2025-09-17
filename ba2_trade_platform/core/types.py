from enum import Enum
from sqlmodel import  Field, Session, SQLModel, create_engine

class OrderStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    ALL = "all"
    NEW = "new"
    UNKNOWN = "unknown"
    CANCELED = "canceled"


class InstrumentType(str, Enum):
    STOCK = "stock"
    CRYPTO = "crypto"

class OrderType(str, Enum):
    MARKET = "market"
    BUY_LIMIT = "buy_limit"
    SELL_LIMIT = "sell_limit"
    BUY_STOP = "buy_stop"
    SELL_STOP = "sell_stop"


class OrderDirection(str, Enum):
    SELL = "sell"
    BUY = "buy"