"""Blocking checks on agent config, SSOT identity and telemetry. Every failure stops the run."""
import datetime as dt
import math

from .core import UTC, date

MAX_SSOT_AGE = 30 * 86400
MAX_CLOCK_SKEW = 300


def validate_agent(a, required_nodes):
    if a.get('approval_required') is not True:
        raise ValueError('This release requires human approval for delivery.')
    if not required_nodes.issubset(set(a.get('nodes', []))):
        raise ValueError('Restore the required blocks before running.')
    if not str(a.get('sensor_id', '')).isdigit():
        raise ValueError('Provide a numeric PRTG sensor ID.')
    if a['template'] != 'forecast':
        return
    if not isinstance(a.get('horizon'), int) or not 1 <= a['horizon'] <= 96:
        raise ValueError('Forecast horizon must be 1–96 samples.')
    if not isinstance(a.get('threshold'), (float, int)) or not math.isfinite(a['threshold']):
        raise ValueError('Threshold must be a finite number.')
    if a.get('direction', 'above') not in ('above', 'below'):
        raise ValueError('Unknown threshold direction.')
    if not isinstance(a.get('period'), int) or not 1 <= a['period'] <= 2048:
        raise ValueError('Baseline period must be 1–2048 samples.')
    if not isinstance(a.get('interval'), int) or not 60 <= a['interval'] <= 86400:
        raise ValueError('Sample interval must be 60–86400 seconds.')


def validate_ssot(doc):
    rows = doc.get('assets', [])
    if not isinstance(rows, list) or not rows:
        raise ValueError('SSOT must contain assets.')
    ids, sensors = set(), set()
    for row in rows:
        for field in ['id', 'name', 'owner', 'service', 'source', 'verified_at', 'sensors']:
            if not row.get(field):
                raise ValueError('SSOT asset is missing ' + field)
        date(row['verified_at'])
        if row['id'] in ids:
            raise ValueError('Duplicate SSOT asset ID.')
        ids.add(row['id'])
        for sensor in row['sensors']:
            sid = str(sensor.get('id', ''))
            if not sid.isdigit() or sid in sensors or not sensor.get('channel') or not sensor.get('unit'):
                raise ValueError('Sensor mappings need unique numeric IDs, exact channel names and units.')
            sensors.add(sid)
    return doc


def resolve(doc, sid, now=None):
    validate_ssot(doc)
    matches = [(r, s) for r in doc['assets'] for s in r['sensors'] if str(s['id']) == str(sid)]
    if len(matches) != 1:
        raise ValueError('Sensor has no unique SSOT mapping. An owner must map it before investigation.')
    asset, sensor = matches[0]
    age = ((now or dt.datetime.now(UTC)) - date(asset['verified_at'])).total_seconds()
    if age < -MAX_CLOCK_SKEW or age > MAX_SSOT_AGE:
        raise ValueError('SSOT record is stale or future-dated. Reverify it before running.')
    return asset, sensor


def series_check(points, interval, now=None):
    if not 60 <= interval <= 86400 or len(points) < 48:
        raise ValueError('Need at least 48 samples and a 60–86400 second interval.')
    values, prev = [], None
    for p in points:
        t = date(p['timestamp'])
        v = p['value']
        if not isinstance(v, (float, int)) or not math.isfinite(v):
            raise ValueError('Missing or nonnumeric readings: forecast blocked.')
        if prev is not None and abs((t - prev).total_seconds() - interval) > 1:
            raise ValueError('Duplicate, unordered or missing time buckets: forecast blocked.')
        if p.get('maintenance'):
            raise ValueError('Maintenance samples require review before forecasting.')
        values.append(float(v))
        prev = t
    age = ((now or dt.datetime.now(UTC)) - prev).total_seconds()
    if age < -MAX_CLOCK_SKEW or age > interval * 3:
        raise ValueError('Telemetry is stale or future-dated.')
    return values
