from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
import pytest
from platform_communication.store import OutboxStore,Conflict
from platform_communication.worker import DeliveryWorker
from platform_communication.adapters import DeliveryError,render_template,ResendAdapter,TwilioAdapter,FCMAdapter,TelegramAdapter
from platform_communication.migrations import MIGRATIONS
import os
import pytest
from platform_backend_core import Database, MigrationRunner
from platform_backend_core.testing import temporary_database

@pytest.fixture
def database():
    dsn=os.environ.get('PLATFORM_TEST_DSN')
    if not dsn: pytest.skip('explicit disposable PostgreSQL DSN required')
    with temporary_database(dsn,prefix='msg_') as temp:
        MigrationRunner(temp.admin_dsn,namespace='communication').apply(MIGRATIONS)
        temp.grant_schema('public')
        db=Database(temp.runtime_dsn)
        yield db,temp
        db.close()

def test_outbox_concurrent_idempotency_and_tenants(database):
    db,_=database;s=OutboxStore(db);w=uuid4()
    def create(_):return s.create(w,'one','email','a@example.invalid','hello','plain')['id']
    with ThreadPoolExecutor(max_workers=4) as p:ids=list(p.map(create,range(4)))
    assert len(set(ids))==1 and s.get(uuid4(),ids[0]) is None
    with pytest.raises(Conflict):s.create(w,'one','email','a@example.invalid','changed','plain')
    assert len(s.list(w))==1

def test_claim_fencing_and_recovery(database):
    db,_=database;s=OutboxStore(db);w=uuid4()
    for i in range(4):s.create(w,str(i),'email','a@example.invalid','hi','plain')
    with ThreadPoolExecutor(max_workers=4) as p:leases=list(p.map(lambda _:s.claim(w),range(4)))
    assert len({x['id'] for x in leases})==4
    old=leases[0]
    with db.transaction(w) as c:c.execute("UPDATE message_outbox SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",(old['id'],))
    fresh=s.claim(w)
    assert fresh['id']==old['id'] and fresh['lease_token']!=old['lease_token']
    assert not s.mark_sending(w,old['id'],old['lease_token'])
    assert s.mark_sending(w,fresh['id'],fresh['lease_token'])
    with db.transaction(w) as c:c.execute("UPDATE message_outbox SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",(fresh['id'],))
    assert s.claim(w) is None
    assert s.get(w,fresh['id'])['status']=='uncertain'

def test_worker_persists_before_send_and_ambiguity_stops_retry(database):
    db,_=database;s=OutboxStore(db);w=uuid4();row=s.create(w,'send','email','a@example.invalid','hi','plain')
    class Adapter:
        configured=True
        def send(self,message):
            assert s.get(w,message['id'])['status']=='sending'
            raise DeliveryError('uncertain')
    worker=DeliveryWorker(s,{'email':Adapter()});assert worker.run_one(w)
    assert s.get(w,row['id'])['status']=='uncertain' and not worker.run_one(w)

def test_retry_dead_and_success(database):
    db,_=database;s=OutboxStore(db);w=uuid4();row=s.create(w,'retry','sms','+12025550123','hi','plain',max_attempts=2)
    class Rejected:
        configured=True
        def send(self,message):raise DeliveryError('retry')
    worker=DeliveryWorker(s,{'sms':Rejected()});worker.run_one(w)
    assert s.get(w,row['id'])['status']=='queued'
    with db.transaction(w) as c:c.execute('UPDATE message_outbox SET run_at=clock_timestamp()')
    worker.run_one(w);assert s.get(w,row['id'])['status']=='dead'
    class Delivered:
        configured=True
        def send(self,message):return 'provider-fixture-id'
    row=s.create(w,'ok','email','a@example.invalid','hi','plain')
    DeliveryWorker(s,{'email':Delivered()}).run_one(w)
    assert s.get(w,row['id'])['status']=='delivered'

def test_validation_templates_and_disabled_adapters(database):
    db,_=database;s=OutboxStore(db)
    assert render_template('html','<script>')=='<p>&lt;script&gt;</p>'
    with pytest.raises(ValueError):s.create(uuid4(),'x','email','https://evil','hi','plain')
    with pytest.raises(ValueError):render_template('../../file','hi')
    assert not ResendAdapter().configured and not TwilioAdapter().configured and not FCMAdapter().configured and not TelegramAdapter().configured

def test_disabled_provider_send_fails_generic():
    m={'id':uuid4(),'recipient':'a@example.invalid','body':'hi','template':'plain'}
    for adapter in [ResendAdapter(),TwilioAdapter(),FCMAdapter(),TelegramAdapter()]:
        with pytest.raises(DeliveryError,match='Delivery unavailable'):adapter.send(m)

def test_workspace_pending_queue_is_bounded_and_replay_survives_limit(database):
    from platform_communication.store import QueueFull
    db,_=database;w=uuid4();s=OutboxStore(db,max_pending=2)
    s.create(w,'one','email','a@example.invalid','hi','plain')
    s.create(w,'two','email','a@example.invalid','hi','plain')
    assert s.create(w,'one','email','a@example.invalid','hi','plain')['status']=='queued'
    with pytest.raises(QueueFull):s.create(w,'three','email','a@example.invalid','hi','plain')

def test_outbox_force_rls_rejects_foreign_write_and_stale_finish(database):
    import psycopg
    db,_=database;w=uuid4();s=OutboxStore(db)
    with db.transaction(w) as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("INSERT INTO message_outbox(workspace_id,id,event_key,fingerprint,channel,recipient,body,template,max_attempts) VALUES (%s,%s,'foreign','fixture','email','a@example.invalid','hi','plain',1)",(uuid4(),uuid4()))
    row=s.create(w,'fenced','email','a@example.invalid','hi','plain');lease=s.claim(w)
    assert s.mark_sending(w,row['id'],lease['lease_token'])
    assert not s.finish(w,row['id'],uuid4(),'delivered')
    assert s.get(w,row['id'])['status']=='sending'
    with db.transaction(w) as c:c.execute("UPDATE message_outbox SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",(row['id'],))
    assert not s.finish(w,row['id'],lease['lease_token'],'delivered')
