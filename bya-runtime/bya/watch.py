"""Start saved agents when PRTG raises an alarm on the sensor they watch.

BYA polls PRTG (outbound only, so the server keeps listening on 127.0.0.1 alone). An agent is started once per
alarm: when its sensor enters a warning or down state, or changes between them. It is not started again while
the sensor stays in the same state, and is re-armed when the sensor leaves the alarm list. Every run stops at the
agent's human approval, so a watched agent never sends anything by itself; its draft waits in the Inbox.
"""
import datetime as dt
import threading
import time


class PrtgWatcher:
    def __init__(self, fetch_alarms, watched, start_run, poll_s=60, clock=time.monotonic):
        """fetch_alarms() -> [alarm]; watched() -> [(file, doc)]; start_run(file, doc, alarm) -> run id."""
        self.fetch_alarms, self.watched, self.start_run = fetch_alarms, watched, start_run
        self.poll_s, self.clock = poll_s, clock
        self.fired = {}          # (file, sensor_id) -> status that already started a run
        self.events = []         # recent activity for the Settings page, newest last
        self._stop = threading.Event()
        self._thread = None

    def log(self, text):
        self.events = (self.events + [{'at': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'), 'text': text}])[-50:]

    def tick(self):
        """One poll. Returns the run ids it started."""
        agents = [(f, d, _prtg_sensor(d)) for f, d in self.watched()]
        agents = [(f, d, s) for f, d, s in agents if s]
        if not agents:
            return []
        try:
            alarms = {a['sensor_id']: a for a in self.fetch_alarms()}
        except ValueError as e:
            self.log(f'PRTG check failed: {e}')
            return []
        started = []
        for file, doc, sensor in agents:
            key, alarm = (file, sensor), alarms.get(sensor)
            if alarm is None:
                self.fired.pop(key, None)       # back to normal: re-arm
                continue
            if self.fired.get(key) == alarm['status']:
                continue                        # same alarm, already handled
            self.fired[key] = alarm['status']
            try:
                rid = self.start_run(file, doc, alarm)
            except Exception as e:  # one failed agent never stops the watcher
                self.log(f'{file}: could not start for sensor {sensor} ({alarm["status"]}): {e}')
                continue
            started.append(rid)
            self.log(f'{file}: started for sensor {sensor} {alarm["device"]} · {alarm["sensor"]} ({alarm["status"]}); draft waits in the Inbox')
        return started

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name='prtg-watch')
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self.poll_s)


def _prtg_sensor(doc):
    for b in doc.get('blocks', []):
        if b.get('type') == 'trigger.alert' and (b.get('config') or {}).get('source') == 'prtg':
            return str(b['config'].get('sensor_id', ''))
    return ''


class FixedAlert:
    """Monitoring stand-in that hands the run the alarm the watcher already fetched (no second PRTG call)."""
    name = 'prtg-watch'

    def __init__(self, alarm, live):
        self.alarm, self.live = alarm, live

    def alert(self, sensor_id):
        return {'sensor_id': str(sensor_id), 'message': self.alarm.get('message', ''), 'status': self.alarm.get('status', ''),
                'lastvalue': self.alarm.get('lastvalue', ''), 'device': self.alarm.get('device', ''),
                'sensor': self.alarm.get('sensor', ''), 'collected_at': dt.datetime.now(dt.timezone.utc).isoformat()}

    def history(self, sensor, interval, now):
        return self.live.history(sensor, interval, now)
