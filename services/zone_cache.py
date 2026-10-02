"""Caching around services.zone_engine.detect_zones.

The engine itself is unchanged. Inside one detect_zones call it rebuilds the
same higher-timeframe candles and indicators for every candidate zone, so
for_timeframe and _indicators are memoised for the duration of that call.
Across calls, an identical candle frame returns the earlier result.
Both functions are pure and detect_zones only reads their output, so the
zones are the same as an uncached call.
"""

import copy
import threading
from collections import OrderedDict

import pandas as pd

from services import zone_engine

_original_for_timeframe = zone_engine.for_timeframe
_original_indicators = zone_engine._indicators
_scope = threading.local()
_results = OrderedDict()
_results_lock = threading.Lock()
_RESULT_LIMIT = 256


class Superseded(BaseException):
    """A newer request from the same page replaced this computation.

    BaseException, so the broad ``except Exception`` fallbacks in the analysis
    code cannot turn a cancelled run into a result with missing zones.
    """


def set_cancel_check(check):
    _scope.cancel = check


def check_superseded():
    check = getattr(_scope, "cancel", None)
    if check is not None and check():
        raise Superseded()


def _memo(kind, key_object, extra, compute):
    check_superseded()
    cache = getattr(_scope, "cache", None)
    if cache is None:
        return compute()
    key = (kind, id(key_object), extra)
    entry = cache.get(key)
    if entry is not None and entry[0] is key_object:
        return entry[1]
    value = compute()
    cache[key] = (key_object, value)
    return value


def _for_timeframe(df, timeframe):
    return _memo("tf", df, timeframe, lambda: _original_for_timeframe(df, timeframe))


def _indicators(frame):
    return _memo("ind", frame, None, lambda: _original_indicators(frame))


zone_engine.for_timeframe = _for_timeframe
zone_engine._indicators = _indicators


def _fingerprint(df, timeframe, max_zones):
    try:
        digest = int(pd.util.hash_pandas_object(df, index=True).sum())
    except Exception:
        return None
    return (timeframe, max_zones, len(df), tuple(df.columns), digest)


def detect_zones(df, timeframe="1d", max_zones=4):
    check_superseded()
    key = _fingerprint(df, timeframe, max_zones)
    if key is not None:
        with _results_lock:
            hit = _results.get(key)
            if hit is not None:
                _results.move_to_end(key)
                return copy.deepcopy(hit)
    outer = getattr(_scope, "cache", None)
    _scope.cache = {} if outer is None else outer
    try:
        zones = zone_engine.detect_zones(df, timeframe=timeframe, max_zones=max_zones)
    finally:
        if outer is None:
            _scope.cache = None
    if key is not None:
        with _results_lock:
            _results[key] = copy.deepcopy(zones)
            while len(_results) > _RESULT_LIMIT:
                _results.popitem(last=False)
    return zones
