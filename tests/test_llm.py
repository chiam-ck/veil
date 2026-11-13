"""Configuration and API boundary checks. Real provider coverage is in scripts/test_live_llm.py."""
import os

import pytest
from conftest import WorkspaceClient as TestClient

from veil.provider import DEFAULT_MODEL
from veil import config, llm, __version__
from veil.api import app


@pytest.mark.parametrize('host', ['127.0.0.1', '192.168.1.2', '100.64.0.0',
                                 '100.100.100.10', '100.127.255.255'])
def test_http_model_endpoint_accepts_private_and_tailnet_addresses(host):
    endpoint = f'http://{host}:8767/v1'
    assert llm.endpoint_url(endpoint) == endpoint + '/chat/completions'


@pytest.mark.parametrize('endpoint', [
    'http://100.63.255.255/v1', 'http://100.128.0.0/v1', 'http://8.8.8.8/v1',
    'http://key@100.100.100.10/v1', 'http://100.100.100.10/v1?key=secret',
])
def test_http_model_endpoint_rejects_public_addresses_and_embedded_credentials(endpoint):
    with pytest.raises(ValueError):
        llm.endpoint_url(endpoint)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'DATA', tmp_path / 'service')
    for name in list(os.environ):
        if name.startswith(('VEIL_LLM_', 'LLM_')) or name == 'VEIL_DETECTOR':
            monkeypatch.delenv(name)
    with TestClient(app) as client:
        yield client


def test_selected_model_and_secret_file_defaults(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith(('VEIL_LLM_', 'LLM_')):
            monkeypatch.delenv(name)
    settings = llm.LLMSettings.from_env()
    assert settings.model == DEFAULT_MODEL
    assert settings.endpoint == 'https://api.example.com/v1/chat/completions'
    assert not settings.allow_network
    monkeypatch.setenv('LLM_API_KEY', 'synthetic-legacy-key')
    monkeypatch.setenv('LLM_MODEL', 'synthetic/legacy-model')
    assert llm.LLMSettings.from_env().api_key == 'synthetic-legacy-key'
    assert llm.LLMSettings.from_env().model == 'synthetic/legacy-model'
    secret = tmp_path / 'key'
    secret.write_text('synthetic-file-key\n')
    monkeypatch.setenv('VEIL_LLM_API_KEY_FILE', str(secret))
    settings = llm.LLMSettings.from_env()
    assert settings.api_key == 'synthetic-file-key'
    assert 'synthetic-file-key' not in repr(settings)
    secret.unlink()
    with pytest.raises(ValueError, match='Unable to read'):
        llm.LLMSettings.from_env()


def test_public_config_uses_server_settings_without_credentials(client, monkeypatch):
    monkeypatch.setenv('VEIL_LLM_API_KEY', 'synthetic-private-key')
    monkeypatch.setenv('VEIL_LLM_MODEL', 'test/admin-model')
    monkeypatch.setenv('VEIL_LLM_ALLOW_NETWORK', 'true')
    monkeypatch.setenv('VEIL_DETECTOR', 'llm')
    response = client.get('/api/llm/config')
    assert response.status_code == 200
    result = response.json()
    assert result['model'] == 'test/admin-model'
    assert result['configured'] and result['key_configured']
    assert result['credentials'] == 'server-managed'
    assert 'synthetic-private-key' not in response.text
    assert response.headers['cache-control'] == 'no-store'
    assert client.get('/api/config').json()['detector']['mode'] == 'llm'
    assert client.get('/api/health').json()['version'] == __version__


def test_homepage_retention_uses_server_configuration(client, monkeypatch):
    monkeypatch.setattr(config, 'RETENTION', 6*3600)
    assert client.get('/api/config').json()['retention_hours'] == 6
    assert 'id="retention-period"' in client.get('/').text


@pytest.mark.parametrize('mode', ['presidio', 'hybrid'])
def test_non_llm_modes_cannot_be_enabled(client, monkeypatch, mode):
    monkeypatch.setenv('VEIL_DETECTOR', mode)
    with pytest.raises(ValueError, match='Only LLM'):
        llm.create_detector()
    assert client.get('/api/llm/config').json()['configured'] is False


def test_comparison_routes_and_ui_are_removed(client):
    assert client.get('/lab/').status_code == 404
    assert client.get('/lab/lab.js').status_code == 404
    assert client.post('/api/llm/compare', json={'text': 'PRIVATE-SOURCE'}).status_code in {404, 405}
    assert '/api/llm/compare' not in client.get('/openapi.json').json()['paths']
    workspace = client.get('/').text
    assert '/lab/' not in workspace and 'Presidio' not in workspace
    assert 'type="password"' not in workspace
    assert client.get('/server-config.css').status_code == 200
    guide = client.get('/developer-guide.html').text
    assert '<span class="pill">ON-PREM</span>' in guide
    assert 'SERVER MANAGED</span>' not in guide and 'hybrid' not in guide
