"""Metric forecasting with a baseline gate: a model that cannot beat seasonal-naive cannot be delivered."""
import math
import os
import statistics


def seasonal(values, horizon, period):
    if len(values) < period:
        raise ValueError('Not enough history for the baseline period.')
    return [values[-period + i % period] for i in range(horizon)]


def trend(values, horizon):
    tail = values[-24:]
    n = len(tail)
    x = (n - 1) / 2
    y = statistics.mean(tail)
    slope = sum((i - x) * (v - y) for i, v in enumerate(tail)) / sum((i - x) ** 2 for i in range(n))
    return [y + slope * (n + i - x) for i in range(horizon)]


_times_model = None


def timesfm(values, horizon):
    global _times_model
    try:
        import numpy as np
        import timesfm as tfm
    except ImportError:
        raise ValueError('TimesFM is not installed. Follow README setup; no baseline substitution was made.') from None
    if _times_model is None:
        checkpoint = os.environ.get('BYA_TIMESFM_PATH')
        if not checkpoint:
            raise ValueError('Set BYA_TIMESFM_PATH to your downloaded TimesFM 2.5 checkpoint directory.')
        _times_model = tfm.TimesFM_2p5_200M_torch.from_pretrained(checkpoint)
        _times_model.compile(tfm.ForecastConfig(max_context=2048, max_horizon=96, normalize_inputs=True,
                                                use_continuous_quantile_head=True, fix_quantile_crossing=True))
    point, quantiles = _times_model.forecast(horizon=horizon, inputs=[np.asarray(values[-2048:], dtype=np.float32)])
    return point[0].tolist(), quantiles[0, :, 1].tolist(), quantiles[0, :, 9].tolist()


def _predict(backend, values, horizon):
    if backend == 'timesfm':
        return timesfm(values, horizon)
    if backend == 'demo-trend':
        return trend(values, horizon), None, None
    raise ValueError('Unknown forecast backend.')


def forecast(values, horizon, threshold, direction, backend, period):
    # Three chronological holdouts; no future observations enter a training window.
    fold = min(horizon, 12)
    if len(values) < max(period, 48) + 3 * fold:
        raise ValueError('Need more history for three rolling holdouts and the selected baseline period.')
    errors, baseline_errors, coverage = [], [], []
    for k in (3, 2, 1):
        end = len(values) - k * fold
        training, actual = values[:end], values[end:end + fold]
        p, lo, hi = _predict(backend, training, fold)
        b = seasonal(training, fold, period)
        errors.extend(abs(a - v) for a, v in zip(actual, p))
        baseline_errors.extend(abs(a - v) for a, v in zip(actual, b))
        if lo is not None:
            coverage.extend(l <= v <= h for l, v, h in zip(lo, actual, hi))
    p, lo, hi = _predict(backend, values, horizon)
    if not all(math.isfinite(x) for x in p + (lo or []) + (hi or [])):
        raise ValueError('Forecast returned nonfinite values.')
    crossing = next((i + 1 for i, v in enumerate(p) if (v >= threshold if direction == 'above' else v <= threshold)), None)
    mae, baseline_mae = statistics.mean(errors), statistics.mean(baseline_errors)
    return {
        'point': p, 'lower': lo, 'upper': hi, 'crossing_step': crossing,
        'mae': mae, 'baseline_mae': baseline_mae,
        'empirical_coverage': statistics.mean(coverage) if coverage else None,
        'baseline_period': period, 'evaluation_samples': len(errors),
        'beats_baseline': mae < baseline_mae, 'backend': backend,
        'interval_note': 'q10–q90; not a calibrated outage probability' if lo else 'No uncertainty interval for demonstration baseline',
    }
