import httpx
from fastapi.testclient import TestClient
from slack_ingress import app


def test_ingress_exposes_only_event_route():
    client=TestClient(app)
    for path in ('/agents','/chat','/docs','/openapi.json','/health'):
        assert client.get(path).status_code==404
    assert client.post('/slack/events',content=b'{}').status_code==401
    assert client.post('/slack/events',content=b'x'*65537).status_code==413


def test_ingress_preserves_signature_body(monkeypatch):
    calls=[]
    class Client:
        def __init__(self,**kwargs):
            assert kwargs['trust_env'] is False
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def post(self,url,**kwargs):
            calls.append((url,kwargs))
            return httpx.Response(200,json={'ok':True})
    monkeypatch.setattr(httpx,'AsyncClient',Client)
    raw=b'{ "type" : "event_callback" }'
    response=TestClient(app).post('/slack/events',content=raw,
        headers={'x-slack-signature':'v0=test','x-slack-request-timestamp':'123',
                 'authorization':'do-not-forward','x-api-key':'do-not-forward'})
    assert response.status_code==200
    assert calls[0][0]=='http://127.0.0.1:8129/slack/events'
    assert calls[0][1]['content']==raw
    assert calls[0][1]['headers']=={'x-slack-signature':'v0=test','x-slack-request-timestamp':'123'}


def test_ingress_failure_sanitized(monkeypatch):
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def post(self,*args,**kwargs):raise httpx.ReadTimeout('PRIVATE_DIAGNOSTIC')
    monkeypatch.setattr(httpx,'AsyncClient',Client)
    r=TestClient(app).post('/slack/events',content=b'{}',
        headers={'x-slack-signature':'v0=test','x-slack-request-timestamp':'123'})
    assert r.status_code==503 and 'PRIVATE' not in r.text
