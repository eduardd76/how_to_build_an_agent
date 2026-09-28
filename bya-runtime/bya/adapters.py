"""Ports to the outside world. Sample and live modes differ only in which adapters are plugged in,
so sample runs exercise the same pipeline, prompts and guards as live runs."""
import datetime as dt
import json
import math
import os
import time
import urllib.parse
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from . import core
from .core import UTC, stamp


# --- Monitoring ---------------------------------------------------------------

class SampleMonitoring:
    name = 'sample-monitoring'

    def alert(self, sensor_id):
        return {'sensor_id': str(sensor_id), 'message': 'Interface utilization above configured threshold',
                'status': 'Warning', 'lastvalue': '87 %', 'collected_at': stamp()}

    def history(self, sensor, interval, now):
        values = [40 + 0.055 * i + 4 * math.sin(i * 2 * math.pi / 288) for i in range(576)]
        return [{'timestamp': (now - dt.timedelta(seconds=(575 - i) * interval)).isoformat(), 'value': v}
                for i, v in enumerate(values)]


class PrtgMonitoring:
    name = 'prtg'
    HISTORY_MIN_GAP = 12  # seconds; PRTG allows about 5 historic requests per minute
    _last_history = 0.0   # shared across instances: the limit is per PRTG server, not per run

    def __init__(self, config):
        self.config = config

    def _get(self, path, params):
        base = self.config.get('prtg_url', '').rstrip('/')
        token = os.environ.get('PRTG_API_TOKEN')
        if not base or not token:
            raise ValueError('Configure PRTG URL and PRTG_API_TOKEN locally.')
        if path == 'historicdata.json':
            if time.monotonic() - PrtgMonitoring._last_history < self.HISTORY_MIN_GAP:
                raise ValueError('PRTG historic requests are limited to one every 12 seconds. Try again shortly.')
            PrtgMonitoring._last_history = time.monotonic()
        return core.http(base + '/api/' + path + '?' + urllib.parse.urlencode({**params, 'apitoken': token}))

    def alert(self, sensor_id):
        doc = self._get('table.json', {'content': 'sensors', 'columns': 'objid,device,sensor,status,message,lastvalue',
                                       'filter_objid': sensor_id, 'count': 100})
        rows = [s for s in doc.get('sensors', []) if str(s.get('objid')) == str(sensor_id)]
        if len(rows) != 1:
            raise ValueError('PRTG did not return exactly one sensor.')
        return {**rows[0], 'collected_at': stamp(), 'sensor_id': sensor_id}

    def history(self, sensor, interval, now):
        hours = int(self.config.get('history_hours', 72))
        if not 12 <= hours <= 168:
            raise ValueError('History window must be 12–168 hours.')
        fmt = '%Y-%m-%d-%H-%M-%S'
        tz = ZoneInfo(self.config.get('prtg_timezone', 'UTC'))
        doc = self._get('historicdata.json', {
            'id': sensor['id'], 'avg': interval, 'usecaption': 1,
            'sdate': (now - dt.timedelta(hours=hours)).astimezone(tz).strftime(fmt),
            'edate': now.astimezone(tz).strftime(fmt),
        })
        points, key = [], sensor['channel'] + '_raw'
        for row in doc.get('histdata', []):
            if row.get('coverage_raw', 100) < 100:
                raise ValueError('Incomplete PRTG sampling coverage: forecast blocked.')
            if key not in row or 'datetime_raw' not in row:
                raise ValueError('Historic channel mapping does not match PRTG raw fields. Verify the exact caption in SSOT.')
            # PRTG returns OLE-automation dates in the server timezone. Around DST changes the local
            # wall clock repeats or skips an hour; series_check then blocks the run rather than guess.
            when = (dt.datetime(1899, 12, 30) + dt.timedelta(days=float(row['datetime_raw']))).replace(tzinfo=tz).astimezone(UTC)
            points.append({'timestamp': when.isoformat(), 'value': row[key]})
        return points


# --- Language model -----------------------------------------------------------

class LocalChatModel:
    """Any OpenAI-compatible chat endpoint (Ollama, vLLM). No tools are ever passed to the model."""

    def __init__(self, config):
        self.url = config.get('model_url', 'http://127.0.0.1:11434/v1')
        self.name = config.get('model', '')

    def complete(self, system, user):
        if not self.name:
            raise ValueError('Set an installed local model name in config.local.json.')
        response = core.http(self.url + '/chat/completions', body={
            'model': self.name, 'temperature': 0.1, 'max_tokens': 900,
            'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
        })
        try:
            content = response['choices'][0]['message']['content']
        except (KeyError, IndexError, TypeError):
            raise ValueError('Local model returned an unexpected response shape.') from None
        if not isinstance(content, str):
            raise ValueError('Local model returned no text.')
        return content.strip()


class TemplateModel:
    """Deterministic stand-in for sample mode. Reads the same payload a real model receives."""
    name = 'sample-template'

    def complete(self, system, user):
        payload = json.loads(user)
        if payload['kind'] == 'incident_brief':
            ev = payload['evidence']
            asset, alert = ev['asset'], ev.get('alert', {})
            ids = ', '.join(r['id'] for r in ev['runbooks']) or 'an approved procedure'
            # Report structured fields only; free-text alert messages are untrusted and never echoed.
            return (f"[SAMPLE] {asset['name']}: {alert.get('status', 'Alert')} at {alert.get('lastvalue', 'n/a')}. "
                    f"Potential service impact: {asset['service']}; owner: {asset['owner']}. "
                    f"Verify sustained load and interface errors using {ids}. "
                    'Root cause is unconfirmed. No changes executed.')
        if payload['kind'] == 'forecast_explanation':
            ids = ', '.join(r['id'] for r in payload['runbooks']) or 'an approved procedure'
            return f'[SAMPLE] Confirm units and recent growth against the same period on prior days using {ids}.'
        raise ValueError('Unknown prompt kind.')


# --- Delivery -----------------------------------------------------------------

class SlackDelivery:
    def __init__(self, config):
        self.channel = config.get('slack_channel')
        self.token = os.environ.get('SLACK_BOT_TOKEN')

    def require_ready(self):
        if not self.token or not self.channel:
            raise ValueError('Configure the approved Slack channel and bot token locally.')

    def send(self, text):
        self.require_ready()
        response = core.http('https://slack.com/api/chat.postMessage', {'Authorization': 'Bearer ' + self.token},
                             {'channel': self.channel, 'text': text, 'mrkdwn': False,
                              'unfurl_links': False, 'unfurl_media': False})
        if not response.get('ok'):
            raise ValueError('Slack rejected the message. Inspect Slack permissions; automatic retry disabled.')
        return response.get('ts')


# --- Wiring -------------------------------------------------------------------

@dataclass
class Adapters:
    label: str
    monitoring: object
    model: object
    forecast_backend: str
    allow_sample_assets: bool


def sample_adapters():
    return Adapters('sample', SampleMonitoring(), TemplateModel(), 'demo-trend', True)


def live_adapters(config):
    return Adapters('live', PrtgMonitoring(config), LocalChatModel(config), 'timesfm', False)
