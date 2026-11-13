"""Negotiated OpenAI-chat options; no document logic or provider-specific models."""
from dataclasses import asdict, dataclass
import re


@dataclass
class ChatCapabilities:
    response_format: bool = True
    temperature: bool = True
    token_parameter: str = 'max_tokens'

    def public(self):
        return asdict(self)

    @classmethod
    def restore(cls, values):
        if (not isinstance(values, dict) or type(values.get('response_format')) is not bool or
                type(values.get('temperature')) is not bool or
                values.get('token_parameter') not in {'max_tokens', 'max_completion_tokens'}):
            return cls()
        return cls(**{k: values[k] for k in ('response_format', 'temperature', 'token_parameter')})

    def apply(self, payload):
        if not self.response_format:
            payload.pop('response_format', None)
        if not self.temperature:
            payload.pop('temperature', None)
        if self.token_parameter == 'max_completion_tokens' and 'max_tokens' in payload:
            payload['max_completion_tokens'] = payload.pop('max_tokens')

    def negotiate(self, status, error, payload):
        """Change only options explicitly rejected by the endpoint, once each."""
        if status not in {400, 422, 500, 501}:
            return None
        message = error.decode('utf8', errors='replace').casefold()
        rejected = any(word in message for word in (
            'not supported', 'unsupported', 'does not support', 'not implemented',
            'unrecognized parameter', 'unknown parameter', 'only the default'))
        if not rejected:
            return None
        if 'response_format' in payload and any(name in message for name in (
                'response_format', 'response format', 'json_schema', 'json schema', 'json_object', 'json object')):
            self.response_format = False
            return 'response_format'
        if 'temperature' in payload and 'temperature' in message:
            self.temperature = False
            return 'temperature'
        if 'max_tokens' in payload and 'max_tokens' in message and 'max_completion_tokens' in message:
            self.token_parameter = 'max_completion_tokens'
            return 'token_parameter'
        return None

    def unwrap(self, content):
        if not self.response_format and isinstance(content, str):
            match = re.fullmatch(r'```(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n```', content.strip(), flags=re.I)
            if match:
                return match[1], True
        return content, False
