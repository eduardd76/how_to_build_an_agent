"""Chat client for any OpenAI-compatible endpoint, with tool calling. Uses the runtime's guarded HTTP client
(HTTPS only except loopback, no redirects, size cap)."""
import os

from .. import core


class ChatModel:
    def __init__(self, url, model, api_key_env=None):
        self.url = url.rstrip('/')
        self.name = model
        self.api_key_env = api_key_env

    @classmethod
    def from_agent_config(cls, cfg):
        url = cfg.get('model_url') or os.environ.get('LLM_BASE_URL', 'http://127.0.0.1:11434/v1')
        model = cfg.get('model') or os.environ.get('LLM_MODEL', '')
        if not model:
            raise ValueError('Set the agent\'s "model", or the LLM_MODEL environment variable.')
        return cls(url, model, cfg.get('api_key_env'))

    def chat(self, messages, tools, max_tokens=2000):
        body = {'model': self.name, 'messages': messages, 'max_tokens': max_tokens, 'temperature': 0.1}
        if tools:
            body['tools'] = tools
        headers = {}
        if self.api_key_env:
            key = os.environ.get(self.api_key_env)
            if not key:
                raise ValueError(f'Environment variable {self.api_key_env} is not set.')
            headers['Authorization'] = 'Bearer ' + key
        elif os.environ.get('LLM_API_KEY'):  # the default key from Settings, for agents without their own
            headers['Authorization'] = 'Bearer ' + os.environ['LLM_API_KEY']
        data = core.http(self.url + '/chat/completions', headers, body)
        try:
            choice = data['choices'][0]
            if not isinstance(choice['message'], dict):
                raise TypeError
        except (KeyError, IndexError, TypeError):
            raise ValueError('Model endpoint returned an unexpected response shape.') from None
        choice['usage'] = data.get('usage') or {}
        return choice
