import json
import os
import sys
import threading
from unittest.mock import MagicMock, patch
from io import BytesIO
import urllib.error

import pytest

import runner


@patch("runner.urllib.request.urlopen")
def test_post_json_success(mock_urlopen):
    mock_response = MagicMock()
    mock_response.read.return_value = b'{"id": "123"}'
    mock_response.__enter__.return_value = mock_response
    mock_urlopen.return_value = mock_response

    result = runner.post_json("http://test.com", "token", {"key": "value"})
    
    assert result == {"id": "123"}
    args, kwargs = mock_urlopen.call_args
    req = args[0]
    assert req.get_header("X-worker-token") == "token"
    assert req.data == b'{"key": "value"}'


@patch("runner.urllib.request.urlopen")
def test_post_json_http_error(mock_urlopen):
    mock_error = urllib.error.HTTPError(
        "http://test.com", 400, "Bad Request", {}, BytesIO(b"error body")
    )
    mock_urlopen.side_effect = mock_error
    
    with pytest.raises(urllib.error.HTTPError):
        runner.post_json("http://test.com", "token", {})


@patch("runner.post_json")
def test_emit(mock_post_json):
    runner.emit("http://test.com", "job1", "token", "test_event", "content", {"a": 1})
    args, kwargs = mock_post_json.call_args
    url, token, payload = args
    assert url == "http://test.com/deca-agents/v1/jobs/job1/events"
    assert token == "token"
    assert payload["event_type"] == "test_event"
    assert payload["content"] == "content"
    assert payload["payload"] == {"a": 1}


@patch("runner.post_json")
def test_command_poller_parses_prompt_and_abort(mock_post_json):
    mock_response = MagicMock()
    # Simulate two chunks from SSE
    mock_response.__iter__.return_value = [
        b"data: {\"command_type\": \"prompt\", \"payload\": {\"message\": \"test\"}}\n",
        b"data: {\"command_type\": \"abort\"}\n"
    ]
    mock_response.__enter__.return_value = mock_response
    
    with patch("runner.urllib.request.urlopen", return_value=mock_response):
        runner.command_poller("http://test.com", "job1", "token", "sess1")

    # 2 calls for actions (prompt/abort) + 2 calls for acking
    assert mock_post_json.call_count == 4
    # First call: prompt
    args1, _ = mock_post_json.call_args_list[0]
    assert args1[0] == "http://localhost:4096/session/sess1/prompt_async"
    assert args1[2] == {"parts": [{"type": "text", "text": "test"}]}
    # Second call: abort
    args2, _ = mock_post_json.call_args_list[1]
    assert args2[0] == "http://localhost:4096/session/sess1/abort"


@patch("runner.emit")
def test_event_streamer_forwards_events(mock_emit):
    mock_response = MagicMock()
    mock_response.__iter__.return_value = [
        b"data: {\"type\": \"message\", \"content\": \"hello\"}\n"
    ]
    mock_response.__enter__.return_value = mock_response
    
    with patch("runner.urllib.request.urlopen", return_value=mock_response):
        runner.event_streamer("http://test.com", "job1", "token")

    assert mock_emit.call_count == 1
    args, _ = mock_emit.call_args
    assert args[3] == "message"
    assert args[5] == {"type": "message", "content": "hello"}

@patch("runner.urllib.request.urlopen")
@patch("runner.subprocess.Popen")
def test_main_configures_opencode(mock_popen, mock_urlopen, monkeypatch):
    monkeypatch.setenv("DECA_AGENT_JOB_ID", "123")
    monkeypatch.setenv("DECA_AGENT_TASK", "task")
    monkeypatch.setenv("DECA_AGENT_API_BASE_URL", "http://api")
    monkeypatch.setenv("DECA_AGENT_WORKER_TOKEN", "token")
    monkeypatch.setenv("DECA_API_KEY", "secret-key")
    monkeypatch.setenv("DECA_AGENT_MODEL", "deca-2.5-mini")
    
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.read.return_value = b'{"id":"ses_123","status":"completed"}'
    mock_response.__enter__.return_value = mock_response
    mock_urlopen.return_value = mock_response
    mock_popen.return_value = MagicMock()
    
    with patch("runner.time.sleep"), patch("runner.emit"), patch("runner.patch_status"):
        runner.main()
        
    config_str = os.environ.get("OPENCODE_CONFIG_CONTENT")
    assert config_str is not None
    config = json.loads(config_str)
    assert config["model"] == "deca/deca-2.5-mini"
    assert config["provider"]["deca"]["options"]["apiKey"] == "{env:DECA_API_KEY}"
