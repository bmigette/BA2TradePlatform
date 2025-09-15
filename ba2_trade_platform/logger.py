import logging
from logging.handlers import RotatingFileHandler
from .config import STDOUT_LOGGING, FILE_LOGGING, HOME
import os
import io
import sys

logger = logging.getLogger("ba2_trade_platform")
logger.setLevel(logging.DEBUG)  

formatter = logging.Formatter(
    '%(asctime)s - %(name)s - %(module)s - %(levelname)s - %(message)s')

if STDOUT_LOGGING:
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG ) 
    handler.setFormatter(formatter)
    logger.addHandler(handler)

if FILE_LOGGING:
        
    handlerfile = RotatingFileHandler(
        os.path.join(HOME, "logs", "app.debug.log"), maxBytes=(1024*1024*10), backupCount=7
    )
    handlerfile.setFormatter(formatter)
    handlerfile.setLevel(logging.DEBUG)
    logger.addHandler(handlerfile)
    handlerfile2 = RotatingFileHandler(
        os.path.join(HOME, "logs", "app.log"), maxBytes=(1024*1024*10), backupCount=7
    )
    handlerfile2.setFormatter(formatter)
    handlerfile2.setLevel(logging.INFO)
    logger.addHandler(handlerfile2)


