import asyncio
import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

import main
import friday_slack as slack
from config import settings


@pytest.fixture
def configured(monkeypatch):
    for name,value in {
        'slack_enabled':True, 'slack_signing_secret':'offline-signature-secret',
        'slack_bot_token':'offline-bot-token', 'slack_team_id':'TTEST',
        'slack_app_id':'ATEST', 'slack_owner_user_id':'UOWNER',
        'slack_chat_channel_id':'CPRIVATE', 'slack_bot_user_id':'UBOT',
    }.items():
        monkeypatch.setattr(settings,name,value)
    records={}
    def enqueue(row):
        if row['event_id'] in records:return False
        records[row['event_id']]=row
        return True
    inbox=SimpleNamespace(enqueue=Mock(side_effect=enqueue))
    monkeypatch.setattr(main.app.state,'slack_inbox',inbox,raising=False)
    return TestClient(main.app), records, inbox


def envelope():
    return {'type':'event_callback','team_id':'TTEST','api_app_id':'ATEST','event_id':'EvTEST',
        'event':{'type':'app_mention','channel':'CPRIVATE','user':'UOWNER',
                 'ts':'1234567890.123456','text':'<@UBOT> Help plan my day'}}


def signed(body, timestamp=None, secret='offline-signature-secret'):
    raw=body if isinstance(body,bytes) else json.dumps(body).encode()
    ts=str(int(time.time()) if timestamp is None else timestamp)
    digest=hmac.new(secret.encode(),b'v0:'+ts.encode()+b':'+raw,hashlib.sha256).hexdigest()
    return raw,{'x-slack-request-timestamp':ts,'x-slack-signature':'v0='+digest,
                'content-type':'application/json'}


def send(client,body,**kwargs):
    raw,headers=signed(body,**kwargs)
    return client.post('/slack/events',content=raw,headers=headers)


def test_signed_challenge_and_enqueue_duplicate(configured):
    client,records,inbox=configured
    assert send(client,{'type':'url_verification','challenge':'abc'}).json()=={'challenge':'abc'}
    assert not records
    result=send(client,envelope())
    assert result.status_code==200 and result.json()['duplicate'] is False
    assert records['EvTEST']['message']=='Help plan my day'
    assert records['EvTEST']['thread_ts']=='1234567890.123456'
    assert send(client,envelope()).json()['duplicate'] is True
    assert len(records)==1


@pytest.mark.parametrize('offset',[-600,600])
def test_expired_or_future_signature(configured,offset):
    client,records,_=configured
    assert send(client,envelope(),timestamp=int(time.time())+offset).status_code==401
    assert not records


def test_signature_failure_and_body_tampering(configured):
    client,records,_=configured
    assert client.post('/slack/events',json=envelope()).status_code==401
    assert send(client,envelope(),secret='wrong').status_code==401
    raw,headers=signed(envelope())
    assert client.post('/slack/events',content=raw+b' ',headers=headers).status_code==401
    assert not records


@pytest.mark.parametrize('field', ['slack_signing_secret','slack_bot_token','slack_team_id',
    'slack_owner_user_id','slack_chat_channel_id','slack_app_id','slack_bot_user_id'])
def test_missing_configuration_fails_closed(configured,monkeypatch,field):
    client,records,_=configured
    monkeypatch.setattr(settings,field,None)
    assert send(client,envelope()).status_code==503
    assert not records


@pytest.mark.parametrize('field,value', [('team_id','TOTHER'),('api_app_id','AOTHER'),
    ('user','UOTHER'),('channel','COTHER')])
def test_authorization(configured,field,value):
    client,records,_=configured
    body=envelope()
    if field in body: body[field]=value
    else:body['event'][field]=value
    assert send(client,body).status_code==403
    assert not records


@pytest.mark.parametrize('changes', [{'bot_id':'BOTHER'}, {'subtype':'message_changed'},
    {'type':'message'}, {'text':'hello without mention'}, {'text':'<@UBOT>  '}])
def test_ignore_bots_edits_and_unmentioned_messages(configured,changes):
    client,records,_=configured
    body=envelope();body['event'].update(changes)
    assert send(client,body).json()['ignored']
    assert not records


@pytest.mark.parametrize('raw', [b'not-json',b'[]'])
def test_bad_json(configured,raw):
    assert send(configured[0],raw).status_code==400


def test_limits_and_event_validation(configured):
    client,records,_=configured
    assert send(client,b'x'*65537).status_code==413
    body=envelope();body['event']['text']='<@UBOT> '+'x'*4001
    assert send(client,body).status_code==413
    body=envelope();body['event_id']='bad/id'
    assert send(client,body).status_code==400
    body=envelope();body['event']['thread_ts']='bad'
    assert send(client,body).status_code==400
    assert not records


def test_store_failure_not_acknowledged(configured):
    client,records,inbox=configured
    inbox.enqueue.side_effect=RuntimeError('PRIVATE_DB_DIAGNOSTIC')
    response=send(client,envelope())
    assert response.status_code==503
    assert 'PRIVATE_DB_DIAGNOSTIC' not in response.text
    assert not records


def record(state='queued'):
    return {'event_id':'EvTEST','team_id':'TTEST','app_id':'ATEST','channel_id':'CPRIVATE',
        'owner_id':'UOWNER','event_ts':'1234567890.123456','thread_ts':'1234567890.123456',
        'message':'hello','state':state,'reply':'Stored answer' if state=='reply_ready' else None}


def processing_app():
    return SimpleNamespace(state=SimpleNamespace(slack_inbox=SimpleNamespace(transition=Mock()),audit=Mock()))


def test_processing_and_reply_resume(configured,monkeypatch):
    chat=AsyncMock(return_value={'reply':'Advisory answer'})
    post=Mock(return_value='1234567891.000001')
    monkeypatch.setattr(slack,'handle_chat',chat)
    monkeypatch.setattr(slack,'post_reply',post)
    app=processing_app()
    asyncio.run(slack.process_record(app,record()))
    assert chat.await_count==1 and post.call_count==1
    states=[c.args[2] for c in app.state.slack_inbox.transition.call_args_list]
    assert states==['processing','reply_ready','delivering','delivered']
    chat.reset_mock();post.reset_mock()
    asyncio.run(slack.process_record(app,record('reply_ready')))
    chat.assert_not_called()
    assert post.call_args.args[0]['reply']=='Stored answer'


@pytest.mark.parametrize('uncertain,new_state',[(False,'failed'),(True,'uncertain')])
def test_delivery_failure_no_automatic_retry(configured,monkeypatch,uncertain,new_state):
    post=Mock(side_effect=slack.SlackDeliveryError('delivery_issue',uncertain))
    monkeypatch.setattr(slack,'post_reply',post)
    app=processing_app()
    asyncio.run(slack.process_record(app,record('reply_ready')))
    assert post.call_count==1
    assert app.state.slack_inbox.transition.call_args.args[2]==new_state


def test_persisted_destination_rechecked(configured,monkeypatch):
    chat=AsyncMock();monkeypatch.setattr(slack,'handle_chat',chat)
    row=record();row['owner_id']='UOTHER'
    app=processing_app();asyncio.run(slack.process_record(app,row))
    chat.assert_not_called()
    assert app.state.slack_inbox.transition.call_args.args[2]=='failed'


def test_delivery_limits_and_escaping(configured,monkeypatch):
    calls=[]
    class Client:
        def __init__(self,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def post(self,url,**kwargs):
            calls.append((url,kwargs))
            return httpx.Response(200,json={'ok':True,'channel':'CPRIVATE','ts':'123.456'})
    monkeypatch.setattr(httpx,'Client',Client)
    row=record('reply_ready');row['reply']='Hello <!channel> <@UOTHER> & review'
    assert slack.post_reply(row)=='123.456'
    body=calls[0][1]['json']
    assert body['channel']=='CPRIVATE' and body['thread_ts']==row['thread_ts']
    assert '<!channel>' not in body['text'] and '<@UOTHER>' not in body['text']
    assert body['unfurl_links'] is False and body['unfurl_media'] is False
    row['channel_id']='COTHER'
    with pytest.raises(slack.SlackDeliveryError):slack.post_reply(row)
    assert len(calls)==1


def test_ambiguous_transport_not_retried(configured,monkeypatch):
    calls=[]
    class Client:
        def __init__(self,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def post(self,*args,**kwargs):
            calls.append(1)
            raise httpx.ReadTimeout('PRIVATE_REQUEST_DETAIL')
    monkeypatch.setattr(httpx,'Client',Client)
    with pytest.raises(slack.SlackDeliveryError) as error:slack.post_reply(record('reply_ready'))
    assert error.value.uncertain and 'PRIVATE' not in str(error.value)
    assert len(calls)==1
