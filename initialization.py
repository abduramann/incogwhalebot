import logging
from datetime import datetime
from pytz import timezone, utc
import sys
from logging import handlers
import threading


# *********************************************************************************************************************
# log configuration
class ContextFilter(logging.Filter):
    """
    This is a filter which injects contextual information into the log.
    """

    def filter(self, record):
        # record.orderCallIndex = orderCallIndex
        return True


log = logging.getLogger('')
log.setLevel(logging.DEBUG)
logFormat = logging.Formatter(
    "%(asctime)s UTC+3 [%(threadName)-12.12s] [%(levelname)-5.5s] %(message)s")
# "%(asctime)s UTC+3 [%(threadName)-12.12s] [%(levelname)-5.5s] [%(orderCallIndex)-4.4s] %(message)s")

cf = ContextFilter()

ch = logging.StreamHandler(sys.stdout)
ch.setFormatter(logFormat)
ch.addFilter(cf)
ch.setLevel(logging.INFO)
log.addHandler(ch)

fh = handlers.RotatingFileHandler("log/nitowhalebot.log", maxBytes=(1048576 * 5), backupCount=7)
fh.setFormatter(logFormat)
fh.addFilter(cf)
log.addHandler(fh)


def myTimezone(*args):
    utc_dt = utc.localize(datetime.utcnow())
    my_tz = timezone("Europe/Istanbul")
    converted = utc_dt.astimezone(my_tz)
    return converted.timetuple()


logging.Formatter.converter = myTimezone


# *********************************************************************************************************************

##### Global Exception Managament ###########
def install_thread_excepthook():
    """
    Install thread excepthook.
    By default, all exceptions raised in thread run() method are caught internally
    by the threading module (see _bootstrap_inner method). Current implementation
    simply dumps the traceback to stderr and did not exit the process.
    This change explicitly catches exceptions and invokes sys.excepthook handler.
    """
    _init = threading.Thread.__init__

    def init(self, *args, **kwargs):
        _init(self, *args, **kwargs)
        _run = self.run

        def run(*args, **kwargs):
            try:
                _run(*args, **kwargs)
            except:
                sys.excepthook(*sys.exc_info())

        self.run = run

    threading.Thread.__init__ = init


install_thread_excepthook()
##### /Global Exception Managament ###########
