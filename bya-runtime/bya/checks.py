"""Connection checks behind the Settings page's Test buttons and `python start.py`. Each returns (ok, detail)."""
import os
import shutil
from pathlib import Path

from . import adapters, core


def model(values):
    url = (os.environ.get('LLM_BASE_URL') or values.get('model_url') or 'http://127.0.0.1:11434/v1').rstrip('/')
    name = os.environ.get('LLM_MODEL') or values.get('model') or ''
    key = os.environ.get('LLM_API_KEY')
    try:
        doc = core.http(url + '/models', headers={'Authorization': 'Bearer ' + key} if key else None)
    except ValueError as e:
        return False, f'{url}: {e}'
    names = [m.get('id', '') for m in doc.get('data', []) if isinstance(m, dict)]
    if not name:
        return False, f'Reached {url} ({len(names)} models), but no model name is set. Pick one: {", ".join(names[:8])}'
    if names and name not in names:
        return False, f'Reached {url}, but "{name}" is not one of its models: {", ".join(names[:8])}'
    return True, f'Reached {url}; "{name}" is available.'


def prtg(values):
    if not values.get('prtg_url') or not os.environ.get('PRTG_API_TOKEN'):
        return False, 'Set the PRTG URL and API token.'
    try:
        alarms = adapters.PrtgMonitoring(values).alarms(limit=50)
    except ValueError as e:
        return False, f'PRTG: {e}'
    shown = '; '.join(f'{a["sensor_id"]} {a["device"]} · {a["sensor"]} ({a["status"]})' for a in alarms[:5])
    return True, f'Connected. {len(alarms)} sensors in alarm' + (f': {shown}' if shown else '.')


def netbox(values):
    base = (os.environ.get('NETBOX_URL') or values.get('netbox_url') or '').rstrip('/')
    token = os.environ.get('NETBOX_TOKEN')
    if not base or not token:
        return False, 'Set the NetBox URL and token.'
    try:
        doc = core.http(base + '/api/status/', headers={'Authorization': f'Token {token}', 'Accept': 'application/json'})
    except ValueError as e:
        return False, f'NetBox: {e}'
    return True, f'Connected to NetBox {doc.get("netbox-version", "")}.'.replace('  ', ' ')


def lab(root):
    labs = sorted(Path(root).glob('containerlab/clab-*/topology-data.json'))
    if not labs:
        return False, 'No deployed containerlab lab found (containerlab/clab-*/topology-data.json).'
    if not shutil.which('docker'):
        return False, f'Found {labs[0].parent.name}, but the docker command is not on this machine.'
    return True, f'Found {", ".join(p.parent.name for p in labs)}; docker is available.'


CHECKS = {'model': model, 'prtg': prtg, 'netbox': netbox}
