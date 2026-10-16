import json
from uuid import UUID
import httpx
import pytest
from platform_communication.adapters import ResendAdapter,TwilioAdapter,FCMAdapter,TelegramAdapter,DeliveryError

def test_fixed_provider_wire_payloads_without_external_io():
    m={'id':UUID('00000000-0000-0000-0000-000000000001'),'recipient':'person@example.invalid','body':'<fixture>','template':'html'}
    requests=[]
    def fixture(request):
        requests.append(request)
        if request.url.host=='api.resend.com':return httpx.Response(200,json={'id':'resend-fixture'})
        if request.url.host=='api.twilio.com':return httpx.Response(201,json={'sid':'twilio-fixture'})
        if request.url.host=='fcm.googleapis.com':return httpx.Response(200,json={'name':'fcm-fixture'})
        return httpx.Response(200,json={'ok':True,'result':{'message_id':123}})
    transport=httpx.MockTransport(fixture)
    email=ResendAdapter('synthetic','from@example.invalid',transport=transport)
    assert email.send(m)=='resend-fixture' and email.send(m)=='resend-fixture'
    assert requests[0].headers['Idempotency-Key']==requests[1].headers['Idempotency-Key']=='message/00000000-0000-0000-0000-000000000001'
    assert json.loads(requests[0].content)['html']=='<p>&lt;fixture&gt;</p>'
    m.update(recipient='+12025550123',body='fixture',template='plain')
    assert TwilioAdapter('AC'+'a'*32,'synthetic','+12025550124',transport=transport).send(m)=='twilio-fixture'
    assert b'To=%2B12025550123' in requests[-1].content
    m['recipient']='synthetic_push_token'
    assert FCMAdapter('fixture-project','synthetic',transport=transport).send(m)=='fcm-fixture'
    assert json.loads(requests[-1].content)['message']['token']=='synthetic_push_token'
    m['recipient']='1234'
    assert TelegramAdapter('123456:'+('x'*30),transport=transport).send(m)=='123'
    assert json.loads(requests[-1].content)=={'chat_id':'1234','text':'fixture'}
    assert all(r.url.scheme=='https' for r in requests)

@pytest.mark.parametrize('status,outcome',[(302,'uncertain'),(429,'retry'),(500,'uncertain'),(401,'dead')])
def test_provider_failures_redacted_and_redirect_never_followed(status,outcome):
    calls=[]
    def fixture(request):
        calls.append(request)
        return httpx.Response(status,headers={'Location':'https://attacker.invalid'},text='SYNTHETIC_PRIVATE_RESPONSE')
    a=ResendAdapter('synthetic','from@example.invalid',transport=httpx.MockTransport(fixture))
    with pytest.raises(DeliveryError) as exc:a.send({'id':UUID(int=1),'recipient':'a@example.invalid','body':'hi','template':'plain'})
    assert str(exc.value)=='Delivery unavailable' and exc.value.outcome==outcome
    assert len(calls)==1

def test_provider_timeout_and_response_size_are_ambiguous():
    def fixture(request):raise httpx.ReadTimeout('SYNTHETIC_PRIVATE',request=request)
    for transport in [httpx.MockTransport(fixture),httpx.MockTransport(lambda r:httpx.Response(200,content=b'x'*65537))]:
        a=ResendAdapter('synthetic','from@example.invalid',transport=transport)
        with pytest.raises(DeliveryError) as exc:a.send({'id':UUID(int=1),'recipient':'a@example.invalid','body':'hi','template':'plain'})
        assert exc.value.outcome=='uncertain' and 'PRIVATE' not in str(exc.value)

def test_endpoint_identifiers_cannot_inject_paths():
    assert not TwilioAdapter('AC../evil','synthetic','+12025550123').configured
    assert not FCMAdapter('https://evil.invalid','synthetic').configured
    assert not TelegramAdapter('12345:abc/../../evil').configured

def test_elapsed_response_deadline_prevents_slow_chunk_delivery(monkeypatch):
    import platform_communication.adapters as module
    values=iter([0.0,6.0])
    monkeypatch.setattr(module,'monotonic',lambda:next(values),raising=False)
    transport=httpx.MockTransport(lambda request:httpx.Response(200,json={'id':'fixture'}))
    adapter=ResendAdapter('synthetic','from@example.invalid',transport=transport)
    with pytest.raises(DeliveryError) as exc:
        adapter.send({'id':UUID(int=1),'recipient':'a@example.invalid','body':'hi','template':'plain'})
    assert exc.value.outcome=='uncertain'
