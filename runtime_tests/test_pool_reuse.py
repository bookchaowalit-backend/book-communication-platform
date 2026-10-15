"""Real single-connection pool regression after successful and conflicting traffic."""
import os
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest
from fastapi.testclient import TestClient
from platform_backend_core import Database, MigrationRunner
from platform_backend_core.testing import temporary_database
from test_http import auth

from platform_communication.app import create_app
from platform_communication.migrations import MIGRATIONS
from platform_communication.store import OutboxStore, Conflict


@pytest.fixture
def pooled():
    with temporary_database(os.environ['PLATFORM_TEST_DSN'],prefix='reuse_comm_') as temp:
        MigrationRunner(temp.admin_dsn,namespace='communication').apply(MIGRATIONS)
        temp.grant_schema()
        database=Database(temp.runtime_dsn,min_size=1,max_size=1)
        verifier,headers=auth()
        try:
            yield database,verifier,headers
        finally:
            database.close()


def assert_repeated_ready(database,store):
    for _ in range(3):
        assert database.ready() is True
        assert store.ready() is True


def test_store_pool_reuse_after_delivery_conflict_and_isolation(pooled):
    database,_,_=pooled
    store=OutboxStore(database);workspace,foreign=uuid4(),uuid4()
    assert_repeated_ready(database,store)
    row=store.create(workspace,'fixture','email','fixture@example.invalid','fixture','plain')
    assert store.get(workspace,row['id'])['id']==row['id']
    assert len(store.list(workspace))==1 and store.list(foreign)==[]
    with pytest.raises(Conflict):
        store.create(workspace,'fixture','email','fixture@example.invalid','changed','plain')
    lease=store.claim(workspace)
    assert store.mark_sending(workspace,row['id'],lease['lease_token'])
    assert store.finish(workspace,row['id'],lease['lease_token'],'delivered','fixture-provider')
    assert store.get(workspace,row['id'])['status']=='delivered'
    assert store.get(foreign,row['id']) is None
    assert_repeated_ready(database,store)


def test_http_pool_reuse_after_success_and_conflict(pooled):
    database,verifier,headers=pooled
    class Adapter:configured=True
    own=headers('messages:read messages:write',uuid4());foreign=headers('messages:read',uuid4())
    with TestClient(create_app(database,verifier,adapters={'email':Adapter()})) as client:
        assert client.get('/readyz').status_code==200
        body={'channel':'email','recipient':'fixture@example.invalid','body':'fixture'}
        row=client.post('/v1/messages',headers=own,json=body)
        assert row.status_code==202
        assert len(client.get('/v1/messages',headers=own).json())==1
        assert client.get('/v1/messages',headers=foreign).json()==[]
        assert client.get('/v1/messages/'+row.json()['id'],headers=foreign).status_code==404
        assert client.post('/v1/messages',headers=own,json=body|{'body':'changed'}).status_code==409
        assert client.post('/v1/messages',headers=own,json=body|{'unknown':True}).status_code==422
        for _ in range(3):
            assert client.get('/readyz').status_code==200
            assert database.ready() is True
