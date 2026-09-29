"""Shared primitives: time handling and the single guarded HTTP client."""
import datetime as dt
import json
import os
import urllib.error
import urllib.parse
import urllib.request

UTC = dt.timezone.utc
MAX_RESPONSE = 8_000_000


def stamp():
    return dt.datetime.now(UTC).isoformat()


def date(value):
    out = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if out.tzinfo is None:
        raise ValueError('Timestamps must include a timezone.')
    return out


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Connector redirects are disabled. Configure the final trusted URL.')


def plain_http_hosts():
    """Hosts the operator explicitly allows over plain HTTP, e.g. BYA_ALLOW_HTTP_HOSTS=host.orb.internal."""
    return tuple(h.strip().lower() for h in os.environ.get('BYA_ALLOW_HTTP_HOSTS', '').split(',') if h.strip())


def http(url, headers=None, body=None):
    """Every outbound call goes through here: HTTPS-only (loopback excepted), no redirects, size-capped."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ('https', 'http') or parsed.username or parsed.password:
        raise ValueError('Use an HTTP(S) endpoint without embedded credentials.')
    if parsed.scheme == 'http' and parsed.hostname not in ('localhost', '127.0.0.1', '::1') + plain_http_hosts():
        raise ValueError(f'Remote connectors require HTTPS. To allow plain HTTP to "{parsed.hostname}" (for example a model '
                         f'on your own machine or LAN), list it in BYA_ALLOW_HTTP_HOSTS.')
    req = urllib.request.Request(
        url,
        data=None if body is None else json.dumps(body).encode(),
        headers={'Content-Type': 'application/json; charset=utf-8', **(headers or {})},
    )
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=90) as res:
            raw = res.read(MAX_RESPONSE + 1)
    except (urllib.error.URLError, TimeoutError):
        raise ValueError('Connector request failed. Check the configured endpoint, certificate, permissions and service availability.') from None
    if len(raw) > MAX_RESPONSE:
        raise ValueError('Connector response exceeds 8 MB; request a smaller interval.')
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError('Connector returned a non-JSON response.') from None
