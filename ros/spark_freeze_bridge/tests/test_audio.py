from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from spark_freeze_bridge.audio import AudioWorker


def speech(id='first'):
    return dict(id=id,session_id='session',clip='ready',target='robot',status='pending')


def test_duplicate_command_does_not_replay_and_new_phase_terminates(tmp_path,monkeypatch):
    worker=AudioWorker('http://127.0.0.1:1',{},tmp_path,enabled=True)
    worker.paths={'ready':tmp_path/'ready.wav'}
    calls=[];worker.ack=lambda item,status,error=None:calls.append((item['id'],status))
    process=Mock();process.poll.return_value=None
    spawn=Mock(return_value=process)
    monkeypatch.setattr('spark_freeze_bridge.audio.subprocess.Popen',spawn)
    worker.handle(speech());worker.handle(speech())
    assert spawn.call_count==1 and calls==[('first','claimed'),('first','playing')]
    worker.handle(speech('second'))
    process.terminate.assert_called_once()
    assert spawn.call_count==2
    worker.handle(None)
    assert worker.process is None


def test_completed_or_interrupted_id_never_restarts(tmp_path,monkeypatch):
    worker=AudioWorker('http://127.0.0.1:1',{},tmp_path,enabled=True)
    worker.paths={'ready':tmp_path/'ready.wav'}
    calls=[];worker.ack=lambda item,status,error=None:calls.append(status)
    process=Mock();process.poll.return_value=0
    spawn=Mock(return_value=process)
    monkeypatch.setattr('spark_freeze_bridge.audio.subprocess.Popen',spawn)
    worker.handle(speech());worker.handle(speech());worker.handle(speech())
    assert calls==['claimed','playing','done'] and spawn.call_count==1
    worker.stop_playback();worker.handle(speech())
    assert spawn.call_count==1


def test_malformed_cache_name_never_downloads(tmp_path,monkeypatch):
    worker=AudioWorker('http://127.0.0.1:1',{},tmp_path,enabled=True)
    monkeypatch.setattr('spark_freeze_bridge.audio.shutil.which',lambda name:'/usr/bin/aplay')
    worker.json_request=lambda *args:{'ready':True,'clips':{'ready':{'filename':'../../.ssh/key.wav','sha256':'bad'}}}
    with pytest.raises(ValueError):worker.prepare()


def test_playback_start_failure_is_reported(tmp_path,monkeypatch):
    worker=AudioWorker('http://127.0.0.1:1',{},tmp_path,enabled=True)
    worker.paths={'ready':tmp_path/'ready.wav'}
    calls=[];worker.ack=lambda item,status,error=None:calls.append(status)
    monkeypatch.setattr('spark_freeze_bridge.audio.subprocess.Popen',Mock(side_effect=OSError('no soundcard')))
    worker.handle(speech())
    assert calls==['claimed','playing','failed']


def test_shutdown_stops_player_and_blocks_future_spawn(tmp_path,monkeypatch):
    worker=AudioWorker('http://127.0.0.1:1',{},tmp_path,enabled=True)
    worker.paths={'ready':tmp_path/'ready.wav'}
    worker.ack=Mock()
    process=Mock();process.poll.return_value=None
    spawn=Mock(return_value=process)
    monkeypatch.setattr('spark_freeze_bridge.audio.subprocess.Popen',spawn)
    worker.handle(speech());worker.close();worker.handle(speech('late'))
    assert spawn.call_count==1
    process.terminate.assert_called_once()


def test_server_timeout_cancels_same_audio_id(tmp_path,monkeypatch):
    worker=AudioWorker('http://127.0.0.1:1',{},tmp_path,enabled=True)
    worker.paths={'ready':tmp_path/'ready.wav'};worker.ack=Mock()
    process=Mock();process.poll.return_value=None
    monkeypatch.setattr('spark_freeze_bridge.audio.subprocess.Popen',Mock(return_value=process))
    worker.handle(speech());packet=speech();packet['status']='failed';worker.handle(packet)
    process.terminate.assert_called_once()
