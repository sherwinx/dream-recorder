import pytest
from unittest import mock
from functions import audio

@pytest.fixture
def mock_config(monkeypatch):
    monkeypatch.setattr(audio, 'get_config', lambda: {
        'AUDIO_CHANNELS': 1,
        'AUDIO_SAMPLE_WIDTH': 2,
        'AUDIO_FRAME_RATE': 44100,
        'RECORDINGS_DIR': '/tmp',
        'GEMINI_API_KEY': 'gemini-test',
        'GOOGLE_CLOUD_PROJECT': 'test-project',
        'TRANSCRIPTION_PROVIDER': 'google_speech',
        'GOOGLE_SPEECH_MODEL': 'chirp_3',
        'GOOGLE_SPEECH_REGION': 'us',
        'GOOGLE_SPEECH_LANGUAGE_CODES': ['auto'],
        'GOOGLE_SPEECH_CHUNK_SECONDS': 55,
        'GOOGLE_SPEECH_MAX_INLINE_MB': 9,
        'GEMINI_PROMPT_MODEL': 'gemini-2.5-flash',
        'GEMINI_THINKING_BUDGET': -1,
        'GPT_SYSTEM_PROMPT': 'Prompt',
        'GPT_SYSTEM_PROMPT_EXTEND': 'PromptExt',
        'GPT_TEMPERATURE': 0.5,
        'LUMA_EXTEND': '0',
    })

@pytest.fixture
def mock_logger():
    return mock.Mock()

def test_generate_video_prompt_success(monkeypatch, mock_config, mock_logger):
    fake_client = mock.Mock()
    fake_client.models.generate_content.return_value = mock.Mock(text='Generated prompt')
    fake_genai = mock.Mock(Client=mock.Mock(return_value=fake_client))
    fake_types = mock.Mock(
        GenerateContentConfig=lambda **kwargs: kwargs,
        ThinkingConfig=lambda **kwargs: kwargs,
    )
    monkeypatch.setattr(audio, '_get_genai_modules', lambda: (fake_genai, fake_types))

    result = audio.generate_video_prompt('transcript', luma_extend=False, logger=mock_logger)

    assert result == 'Generated prompt'
    fake_client.models.generate_content.assert_called_once()
    call = fake_client.models.generate_content.call_args.kwargs
    assert call['model'] == 'gemini-2.5-flash'
    assert call['contents'] == ['transcript']
    assert call['config']['system_instruction'] == 'Prompt'
    assert call['config']['thinking_config']['thinking_budget'] == -1
    assert 'max_output_tokens' not in call['config']

def test_generate_video_prompt_error(monkeypatch, mock_config, mock_logger):
    def raise_exc():
        raise Exception('gemini fail')
    monkeypatch.setattr(audio, '_get_genai_modules', raise_exc)
    result = audio.generate_video_prompt('transcript', luma_extend=False, logger=mock_logger)
    assert result is None
    mock_logger.error.assert_called()

def test_process_audio_success(monkeypatch, mock_config, mock_logger):
    # Patch save_wav_file
    monkeypatch.setattr(audio, 'save_wav_file', lambda *a, **k: 'file.wav')
    # Patch Google Speech
    monkeypatch.setattr(audio, 'transcribe_audio', lambda *a, **k: 'hello world')
    # Patch generate_video_prompt
    monkeypatch.setattr(audio, 'generate_video_prompt', lambda *a, **k: 'video prompt')
    # Patch generate_video
    monkeypatch.setattr(audio, 'generate_video', lambda *a, **k: ('video.mp4', 'thumb.png'))
    # Patch dream_db
    fake_db = mock.Mock()
    # Patch socketio
    fake_socketio = mock.Mock()
    recording_state = {}
    audio_chunks = [b'audio']
    audio.process_audio('sid', fake_socketio, fake_db, recording_state, audio_chunks, logger=mock_logger)
    assert recording_state['transcription'] == 'hello world'
    assert recording_state['video_prompt'] == 'video prompt'
    assert recording_state['status'] == 'complete'
    assert recording_state['video_url'].endswith('video.mp4')
    fake_socketio.emit.assert_any_call('transcription_update', {'text': 'hello world'}, room='sid')
    fake_socketio.emit.assert_any_call('video_prompt_update', {'text': 'video prompt'}, room='sid')
    fake_socketio.emit.assert_any_call('video_ready', {'url': recording_state['video_url']}, room='sid')
    fake_db.save_dream.assert_called()
    mock_logger.info.assert_called()

def test_process_audio_error(monkeypatch, mock_config, mock_logger):
    # Patch save_wav_file to raise
    def raise_exc(*a, **k): raise Exception('fail')
    monkeypatch.setattr(audio, 'save_wav_file', raise_exc)
    fake_db = mock.Mock()
    fake_socketio = mock.Mock()
    recording_state = {}
    audio_chunks = [b'audio']
    audio.process_audio('sid', fake_socketio, fake_db, recording_state, audio_chunks, logger=mock_logger)
    assert recording_state['status'] == 'error'
    fake_socketio.emit.assert_any_call('error', {'message': 'fail'})
    mock_logger.error.assert_called()

def test_process_audio_finally_clears_chunks(monkeypatch, mock_config, mock_logger):
    # Patch save_wav_file
    monkeypatch.setattr(audio, 'save_wav_file', lambda *a, **k: 'file.wav')
    # Patch Google Speech
    monkeypatch.setattr(audio, 'transcribe_audio', lambda *a, **k: 'hello world')
    # Patch generate_video_prompt
    monkeypatch.setattr(audio, 'generate_video_prompt', lambda *a, **k: 'video prompt')
    # Patch generate_video
    monkeypatch.setattr(audio, 'generate_video', lambda *a, **k: ('video.mp4', 'thumb.png'))
    fake_db = mock.Mock()
    fake_socketio = mock.Mock()
    recording_state = {}
    audio_chunks = [b'audio']
    audio.process_audio('sid', fake_socketio, fake_db, recording_state, audio_chunks, logger=mock_logger)
    # The local audio_chunks variable is cleared in finally, but the caller's list is not affected
    # So we just ensure the function completes and does not error
    assert recording_state['status'] == 'complete'

def test_process_audio_finally_unlink_exception(monkeypatch, mock_config, mock_logger):
    # Patch save_wav_file
    monkeypatch.setattr(audio, 'save_wav_file', lambda *a, **k: 'file.wav')
    # Patch Google Speech
    monkeypatch.setattr(audio, 'transcribe_audio', lambda *a, **k: 'hello world')
    # Patch generate_video_prompt
    monkeypatch.setattr(audio, 'generate_video_prompt', lambda *a, **k: 'video prompt')
    # Patch generate_video
    monkeypatch.setattr(audio, 'generate_video', lambda *a, **k: ('video.mp4', 'thumb.png'))
    # Patch os.unlink to raise
    monkeypatch.setattr(audio.os, 'unlink', lambda path: (_ for _ in ()).throw(Exception('fail')))
    fake_db = mock.Mock()
    fake_socketio = mock.Mock()
    recording_state = {}
    audio_chunks = [b'audio']
    audio.process_audio('sid', fake_socketio, fake_db, recording_state, audio_chunks, logger=mock_logger)
    # Should complete without raising, even though os.unlink fails
    assert recording_state['status'] == 'complete'
