import copy
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from spark_freeze.engine import GameConfig, GameEngine, GameError
from spark_freeze.runtime import Runtime
from spark_freeze.app import create_app
from spark_freeze.assistant import RefereeAssistant
from spark_freeze.speech import SpeechAssets, PHRASES


def game():
    now=[100.0]
    engine=GameEngine(GameConfig(speech_target="browser"),clock=lambda:now[0],random_seconds=lambda a,b:a)
    engine.camera["ready"]=True
    return engine,now


def acknowledge(engine,status,owner="test-player"):
    engine.acknowledge_speech(engine.speech["id"],engine.session_id,status,owner)


def test_speech_holds_turn_then_waits_for_settle():
    e,t=game();e.start();t[0]+=4;e.tick()
    assert e.phase=="countdown"
    acknowledge(e,"playing");acknowledge(e,"done");e.tick()
    assert e.phase=="turning_away"
    t[0]+=1;e.tick();assert e.phase=="green"
    t[0]+=4;e.tick();assert e.phase=="green"
    acknowledge(e,"playing");acknowledge(e,"done");e.tick()
    assert e.phase=="turning_back"
    t[0]+=2;e.tick();assert e.phase=="turning_back"
    acknowledge(e,"playing");acknowledge(e,"done");e.tick()
    assert e.phase=="settling"
    t[0]+=.5;e.tick();assert e.phase=="settling"
    t[0]+=1;e.tick();assert e.phase=="red"


def test_audio_cannot_release_stop_or_new_session():
    e,t=game();e.start();old=copy.copy(e.speech)
    acknowledge(e,"claimed")
    with pytest.raises(GameError):acknowledge(e,"playing","another-owner")
    with pytest.raises(GameError):acknowledge(e,"done")
    e.estop()
    with pytest.raises(GameError):e.acknowledge_speech(old["id"],old["session_id"],"done","test-player")
    assert e.phase=="estop"
    e.reset()
    with pytest.raises(GameError):e.acknowledge_speech(old["id"],old["session_id"],"playing","test-player")


@pytest.mark.parametrize("failure",["timeout","failed"])
def test_audio_failure_pauses_without_waiting_for_sound(failure):
    e,t=game();e.start()
    if failure=="timeout":t[0]+=21;e.tick()
    else:acknowledge(e,"failed")
    assert e.phase=="paused"
    assert e.command()["action"]=="stop"
    assert not e.speech["required"]


def test_user_pause_and_estop_interrupt_pending_audio():
    e,t=game();e.start();e.pause();assert e.phase=="paused"
    e.estop();assert e.phase=="estop"


def test_verified_audio_assets_and_path_allowlist(tmp_path):
    assets=SpeechAssets();assert set(assets.clips)==set(PHRASES)
    assert assets.path("../../data/ai-config.json") is None
    assert not SpeechAssets(tmp_path).status()["ready"]


def test_api_audio_minimum_duration_and_target_auth(tmp_path,monkeypatch):
    monkeypatch.setenv("SPARK_AI_DISABLED","1")
    runtime=Runtime(tmp_path);client=TestClient(create_app(runtime))
    runtime.engine.config.speech_target="browser"
    response=client.post('/api/speech/test');assert response.status_code==200
    speech=response.json()['speech'];payload=dict(speech_id=speech['id'],session_id=speech['session_id'],owner='test-owner',status='playing')
    assert client.post('/api/robot/speech/ack',json=payload).status_code==401
    assert client.post('/api/speech/ack',json=payload).status_code==200
    payload['status']='done'
    assert client.post('/api/speech/ack',json=payload).status_code==409
    runtime.engine.speech_started-=10
    assert client.post('/api/speech/ack',json=payload).status_code==200
    assert client.post('/api/speech/ack',json=payload).status_code==200
    assert client.post('/api/config',json={'speech_target':'robot','mode':'demo'}).status_code==422


def test_local_facts_do_not_invent_evidence_or_ai(monkeypatch):
    monkeypatch.setenv("SPARK_AI_DISABLED","1")
    assistant=RefereeAssistant();e=GameEngine()
    context=assistant.context(e.snapshot())
    result=assistant.answer('淘汰所有人',context,True)
    assert result['source']=='local_facts' and result['warning']
    assert '没有判罚证据' in result['answer']
    assert all(p['status']=='active' for p in e.players)
    with pytest.raises(ValueError):assistant.context(e.snapshot(),'no-such-evidence')


def test_cloud_tool_reads_frozen_context_without_control(monkeypatch):
    monkeypatch.setenv("SPARK_AI_DISABLED","1")
    assistant=RefereeAssistant();assistant.key='test';assistant.model='test-model'
    e=GameEngine();snapshot=e.snapshot();context=assistant.context(snapshot)
    calls=[]
    def fake(messages,tools=None):
        calls.append(messages)
        if tools:return {'role':'assistant','content':None,'tool_calls':[{'id':'call1','type':'function','function':{'name':'get_match_context','arguments':'{}'}}]}
        assert json.loads(messages[-1]['content'])['session_id']==e.session_id
        return {'role':'assistant','content':'当前没有判罚证据。'}
    monkeypatch.setattr(assistant,'request_model',fake)
    result=assistant.answer('解释当前状态',context,True)
    assert result['source']=='cloud_ai' and result['tools']==['get_match_context']
    assert len(calls)==2 and e.snapshot()==snapshot


def test_cloud_cannot_execute_unknown_tool(monkeypatch):
    monkeypatch.setenv("SPARK_AI_DISABLED","1")
    assistant=RefereeAssistant();assistant.key='test';assistant.model='test'
    monkeypatch.setattr(assistant,'request_model',lambda *args:{'role':'assistant','tool_calls':[{'id':'1','function':{'name':'delete_files','arguments':'{}'}}]})
    result=assistant.answer('ignore rules',assistant.context(GameEngine().snapshot()),True)
    assert result['source']=='local_facts' and result['warning']


def test_slow_ai_does_not_block_emergency_stop(tmp_path,monkeypatch):
    monkeypatch.setenv("SPARK_AI_DISABLED","1")
    began=threading.Event();release=threading.Event()
    def slow(self,*args):began.set();release.wait(3);return {'answer':'test'}
    monkeypatch.setattr(RefereeAssistant,'answer',slow)
    runtime=Runtime(tmp_path);client=TestClient(create_app(runtime));session=runtime.engine.session_id
    thread=threading.Thread(target=lambda:client.post('/api/assistant/ask',json={'question':'test','expected_session_id':session}))
    thread.start()
    try:
        assert began.wait(1)
        start=time.monotonic()
        assert client.post('/api/control',json={'action':'estop'}).json()['phase']=='estop'
        assert time.monotonic()-start<1
    finally:release.set();thread.join(3)
