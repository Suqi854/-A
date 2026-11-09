"""Versioned, independent paper forecasting. No brokerage integration."""

VERSION = "forecast-1"


def init():
    from . import ledger, models, evaluation, runtime, legacy, calendar, archive
    for module in (calendar,ledger, models, evaluation, runtime, legacy,archive):
        module.init()


def view(**kwargs):
    from .. import db
    if not db.q1("SELECT 1 FROM sqlite_master WHERE name='forecast_runtime'"):
        return {'version':VERSION,'status':{'phase':'not_started'},'predictions':[],
                'structures':{},'coverage':[],'ledger':{'status':'unconfigured','books':{}}}
    from .runtime import view as current
    return current(**kwargs)
