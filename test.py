from ba2_trade_platform.config import load_config_from_env
load_config_from_env()
from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
from ba2_trade_platform.logger import logger
from ba2_trade_platform.core.db import init_db, get_db

acc = AlpacaAccount()
orders = acc.get_orders()
logger.info(f"Retrieved {len(orders)} orders from Alpaca.")
logger.debug(orders)
positions = acc.get_positions()
logger.info(f"Retrieved:\n {positions}.")

init_db()
db = get_db()