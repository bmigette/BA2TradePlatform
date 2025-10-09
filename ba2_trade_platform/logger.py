import logging
from logging.handlers import RotatingFileHandler
from .config import STDOUT_LOGGING, FILE_LOGGING, HOME, HOME_PARENT
import os
import io
import sys

logger = logging.getLogger("ba2_trade_platform")
logger.setLevel(logging.DEBUG)

# Clear any existing handlers to prevent duplicates
logger.handlers.clear()

# Prevent propagation to root logger to avoid duplicate logs
logger.propagate = False

# Configure our handlers
formatter = logging.Formatter(
    '%(asctime)s - %(name)s - %(module)s - %(levelname)s - %(message)s')

if STDOUT_LOGGING:
    # Create a safe StreamHandler that handles Unicode characters
    handler = logging.StreamHandler(io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace'))
    handler.setLevel(logging.DEBUG) 
    handler.setFormatter(formatter)
    logger.addHandler(handler)

if FILE_LOGGING:
    handlerfile = RotatingFileHandler(
        os.path.join(HOME_PARENT, "logs", "app.debug.log"), maxBytes=(1024*1024*10), backupCount=7, encoding='utf-8'
    )
    handlerfile.setFormatter(formatter)
    handlerfile.setLevel(logging.DEBUG)
    logger.addHandler(handlerfile)
    handlerfile2 = RotatingFileHandler(
        os.path.join(HOME_PARENT, "logs", "app.log"), maxBytes=(1024*1024*10), backupCount=7, encoding='utf-8'
    )
    handlerfile2.setFormatter(formatter)
    handlerfile2.setLevel(logging.INFO)
    logger.addHandler(handlerfile2)


