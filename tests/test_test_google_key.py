import os
import sys
import subprocess

import scripts.test_google_key as google_key


class FakeClient:
    def __init__(self, raise_exc=None):
        self._raise_exc = raise_exc
        self.models = self

    def list(self):
        if self._raise_exc:
            raise self._raise_exc
        return ['model']


def test_check_gemini_api_key_success(monkeypatch):
    fake_genai = type('FakeGenAI', (), {
        'Client': staticmethod(lambda api_key: FakeClient())
    })
    monkeypatch.setattr(google_key, 'genai', fake_genai)
    assert google_key.check_gemini_api_key('key') is True


def test_check_gemini_api_key_error(monkeypatch, capsys):
    class OtherError(Exception):
        pass

    fake_genai = type('FakeGenAI', (), {
        'Client': staticmethod(lambda api_key: FakeClient(raise_exc=OtherError('fail')))
    })
    monkeypatch.setattr(google_key, 'genai', fake_genai)
    assert google_key.check_gemini_api_key('key') is False
    out = capsys.readouterr().out
    assert 'Error:' in out


def _patched_google_script(tmp_path, return_value):
    """Create a temp copy of the script with check_gemini_api_key returning return_value."""
    orig = os.path.join(os.path.dirname(__file__), '../scripts/test_google_key.py')
    with open(orig, 'r') as f:
        code = f.read()
    code = code.replace(
        'def check_gemini_api_key(api_key):',
        f'def check_gemini_api_key(api_key):\n    return {return_value}\n#'
    )
    tmp_script = tmp_path / 'test_google_key_patch.py'
    tmp_script.write_text(code)
    return str(tmp_script)


def test_main_block_valid_key_subprocess(tmp_path):
    script = _patched_google_script(tmp_path, 'True')
    result = subprocess.run([sys.executable, script, 'valid'], capture_output=True, text=True)
    assert 'Valid Gemini API key.' in result.stdout


def test_main_block_invalid_key_subprocess(tmp_path):
    script = _patched_google_script(tmp_path, 'False')
    result = subprocess.run([sys.executable, script, 'invalid'], capture_output=True, text=True)
    assert 'Invalid Gemini API key.' in result.stdout


def test_main_block_missing_key_subprocess(tmp_path):
    script = _patched_google_script(tmp_path, 'True')
    env = os.environ.copy()
    env.pop('GEMINI_API_KEY', None)
    result = subprocess.run([sys.executable, script], capture_output=True, text=True, env=env)
    assert 'Usage:' in result.stdout
    assert result.returncode == 1
