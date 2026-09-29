"""Settings the engineer enters once, in the studio, instead of environment variables.

Plain settings live in config.local.json (the file the runtime already reads). Secrets live in secrets.local.json,
readable only by the owner, and are never sent back to the browser: the API only says whether each one is set.
A variable already present in the process environment when BYA started always wins, so operators who
configure BYA through the environment keep full control; the studio shows which values come from there.
"""
import json
import os
import stat
from pathlib import Path

# key, environment variable (or None), secret, section, label, help
FIELDS = [
    ('model_url', 'LLM_BASE_URL', False, 'Model', 'Model endpoint', 'OpenAI-compatible URL, e.g. http://127.0.0.1:11434/v1 (Ollama).'),
    ('model', 'LLM_MODEL', False, 'Model', 'Model name', 'As your server lists it, e.g. qwen2.5:7b.'),
    ('model_api_key', 'LLM_API_KEY', True, 'Model', 'API key', 'Only for hosted endpoints. Leave empty for Ollama.'),
    ('allow_http_hosts', 'BYA_ALLOW_HTTP_HOSTS', False, 'Model', 'Hosts allowed over plain HTTP',
     'Comma-separated. Everything else must use HTTPS. Example: host.orb.internal'),
    ('prtg_url', None, False, 'PRTG', 'PRTG URL', 'e.g. https://prtg.example.com'),
    ('prtg_api_token', 'PRTG_API_TOKEN', True, 'PRTG', 'API token', 'A read-only PRTG API key.'),
    ('prtg_timezone', None, False, 'PRTG', 'PRTG server time zone', 'e.g. Europe/Berlin. Default UTC.'),
    ('prtg_poll_s', None, False, 'PRTG', 'Check for alarms every (seconds)', '30 to 3600. Used when agents watch PRTG.'),
    ('netbox_url', 'NETBOX_URL', False, 'NetBox', 'NetBox URL', 'e.g. https://netbox.example.com'),
    ('netbox_token', 'NETBOX_TOKEN', True, 'NetBox', 'API token', 'A read-only NetBox token.'),
    ('slack_channel', None, False, 'Slack', 'Channel ID', 'The channel approved briefs go to.'),
    ('slack_bot_token', 'SLACK_BOT_TOKEN', True, 'Slack', 'Bot token', 'Needs chat:write only.'),
]
SECRET_KEYS = {f[0] for f in FIELDS if f[2]}
ENV_OF = {f[0]: f[1] for f in FIELDS if f[1]}
ORIGINAL_ENV = {env: os.environ[env] for env in ENV_OF.values() if os.environ.get(env)}  # set before BYA started


def _read(path):
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except ValueError:
        return {}


def load(root):
    root = Path(root)
    return {**_read(root / 'config.local.json'), **_read(root / 'secrets.local.json')}


def apply(root):
    """Put saved values into the process environment, where the runtime reads them. The original environment wins."""
    values = load(root)
    for key, env in ENV_OF.items():
        if env in ORIGINAL_ENV:
            continue
        if values.get(key):
            os.environ[env] = str(values[key])
        else:
            os.environ.pop(env, None)
    return values


def describe(root):
    """What the studio may see: plain values, and only whether each secret is set."""
    values = load(root)
    out = []
    for key, env, secret, section, label, help_text in FIELDS:
        from_env = env in ORIGINAL_ENV
        item = {'key': key, 'section': section, 'label': label, 'help': help_text, 'secret': secret, 'env': env,
                'source': 'environment' if from_env else ('settings' if values.get(key) else '')}
        if secret:
            item['set'] = from_env or bool(values.get(key))
        else:
            item['value'] = ORIGINAL_ENV[env] if from_env else str(values.get(key, '') or '')
        out.append(item)
    return out


def validate(values):
    known = {f[0] for f in FIELDS}
    unknown = set(values) - known
    if unknown:
        raise ValueError(f'Unknown settings: {", ".join(sorted(unknown))}.')
    for key, value in values.items():
        if not isinstance(value, str) or len(value) > 2000 or '\n' in value:
            raise ValueError(f'"{key}" must be a single line of text.')
    for key in ('model_url', 'prtg_url', 'netbox_url'):
        v = values.get(key, '')
        if v and not v.startswith(('https://', 'http://')):
            raise ValueError(f'"{key}" must start with https:// (or http:// for hosts you allow).')
    poll = values.get('prtg_poll_s', '')
    if poll and not (poll.isdigit() and 30 <= int(poll) <= 3600):
        raise ValueError('"prtg_poll_s" must be a whole number between 30 and 3600.')


def _check_plain_http(merged):
    """A plain-HTTP endpoint on another host only works when that host is allowed; say so at save time."""
    from urllib.parse import urlsplit
    allowed = {h.strip().lower() for h in (os.environ.get('BYA_ALLOW_HTTP_HOSTS', '') if 'BYA_ALLOW_HTTP_HOSTS' in ORIGINAL_ENV
                                           else merged.get('allow_http_hosts', '')).split(',') if h.strip()}
    for key in ('model_url', 'prtg_url', 'netbox_url'):
        parts = urlsplit(merged.get(key, '') or '')
        host = (parts.hostname or '').lower()
        if parts.scheme == 'http' and host and host not in ('localhost', '127.0.0.1', '::1') and host not in allowed:
            raise ValueError(f'"{key}" uses plain HTTP to {host}. Add {host} to "Hosts allowed over plain HTTP", or use https://.')


def save(root, values, clear=()):
    """Merge new values in. An empty secret means "keep the current one"; list keys in clear to remove them."""
    root = Path(root)
    validate(values)
    config_path, secrets_path = root / 'config.local.json', root / 'secrets.local.json'
    config, secrets_ = _read(config_path), _read(secrets_path)
    _check_plain_http({**config, **{k: v.strip() for k, v in values.items() if k not in SECRET_KEYS}})
    for key, value in values.items():
        value = value.strip()
        if key in SECRET_KEYS:
            if value:
                secrets_[key] = value
        else:
            config[key] = value
    for key in clear:
        config.pop(key, None)
        secrets_.pop(key, None)
    _write(config_path, config, private=False)
    _write(secrets_path, secrets_, private=True)
    return apply(root)


def _write(path, data, private):
    tmp = path.with_suffix('.pending')
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(tmp, flags, 0o600 if private else 0o644)
    with os.fdopen(fd, 'w') as f:
        json.dump(data, f, indent=2)
    if private:
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp, path)
