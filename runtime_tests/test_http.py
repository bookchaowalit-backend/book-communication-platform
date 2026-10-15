from platform_communication.app import create_app
from platform_communication.migrations import MIGRATIONS
import os
import time
from uuid import uuid4
import pytest
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from fastapi.testclient import TestClient
from platform_backend_core import Database,MigrationRunner,TokenVerifier
from platform_backend_core.testing import temporary_database

def auth():
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    public=key.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo)
    verifier=TokenVerifier(issuer='fixture',audience='fixture',public_key=public)
    def headers(scopes,workspace=None,actor=None):
        now=int(time.time())
        token=jwt.encode({'iss':'fixture','aud':'fixture','sub':str(actor or uuid4()),'ws':str(workspace or uuid4()),'iat':now,'exp':now+300,'jti':str(uuid4()),'scope':scopes},key,algorithm='RS256')
        return {'Authorization':'Bearer '+token,'Idempotency-Key':'fixture-key'}
    return verifier,headers

@pytest.fixture
def setup():
    dsn=os.environ.get('PLATFORM_TEST_DSN')
    if not dsn:pytest.skip('explicit PostgreSQL DSN required')
    with temporary_database(dsn,prefix='comh_') as temp:
        verifier,headers=auth()
        db=Database(temp.runtime_dsn)
        yield temp,db,verifier,headers
        db.close()

def test_missing_schema_is_not_ready(setup):
    temp,db,verifier,headers=setup
    with TestClient(create_app(db,verifier)) as c:
        assert c.get('/readyz').status_code==503
        assert c.get('/healthz').status_code==200

def test_message_api_auth_disabled_adapter_and_redaction(setup):
    temp,db,verifier,headers=setup
    MigrationRunner(temp.admin_dsn,namespace='communication').apply(MIGRATIONS);temp.grant_schema('public')
    class Adapter:configured=True
    w=uuid4();h=headers('messages:read messages:write',w)
    with TestClient(create_app(db,verifier,adapters={'email':Adapter()})) as c:
        assert c.get('/readyz').status_code==200
        body={'channel':'email','recipient':'person@example.invalid','body':'fixture'}
        response=c.post('/v1/messages',headers=h,json=body);assert response.status_code==202
        mid=response.json()['id']
        assert c.get('/v1/messages/'+mid,headers=headers('messages:read')).status_code==404
        assert c.post('/v1/messages',headers=headers(''),json=body).status_code==403
        body['body']='changed';assert c.post('/v1/messages',headers=h,json=body).status_code==409
        body['secret']='SYNTHETIC_VALUE';response=c.post('/v1/messages',headers=h,json=body)
        assert response.status_code==422 and 'SYNTHETIC_VALUE' not in response.text
    db=Database(temp.runtime_dsn)
    with TestClient(create_app(db,verifier,adapters={})) as c:
        assert c.get('/readyz').status_code==503


def test_applied_migration_checksum_required(setup):
    import psycopg
    from psycopg import sql
    temp,db,verifier,headers=setup
    MigrationRunner(temp.admin_dsn,namespace='communication').apply(MIGRATIONS);temp.grant_schema('public')
    with psycopg.connect(temp.admin_dsn) as admin:
        admin.execute("DELETE FROM platform_schema_migrations WHERE namespace=%s",('communication',))
    class Adapter:configured=True
    kwargs={'adapters':{'email':Adapter()}} if 'communication'=='communication' else {}
    with TestClient(create_app(db,verifier,**kwargs)) as c:
        assert c.get('/readyz').status_code==503


@pytest.mark.parametrize('kind,target,privilege',[('TABLE', 'message_outbox', 'SELECT'), ('TABLE', 'message_outbox', 'INSERT'), ('TABLE', 'message_outbox', 'UPDATE'), ('SCHEMA', 'public', 'USAGE'), ('TABLE', 'platform_schema_migrations', 'SELECT')])
def test_readiness_requires_each_effective_privilege(setup,kind,target,privilege):
    import psycopg
    from psycopg import sql
    temp,db,verifier,headers=setup
    MigrationRunner(temp.admin_dsn,namespace='communication').apply(MIGRATIONS)
    temp.grant_schema('public')
    with psycopg.connect(temp.admin_dsn) as admin:
        admin.execute('REVOKE USAGE ON SCHEMA public FROM PUBLIC')
    class Adapter:configured=True
    kwargs={'adapters':{'email':Adapter()}} if 'communication'=='communication' else {}
    with TestClient(create_app(db,verifier,**kwargs)) as c:
        assert c.get('/readyz').status_code==200
        with psycopg.connect(temp.admin_dsn) as admin:
            admin.execute(sql.SQL('REVOKE {} ON {} {} FROM {}').format(sql.SQL(privilege),sql.SQL(kind),sql.Identifier(target),sql.Identifier(temp.runtime_role)))
        assert c.get('/readyz').status_code==503
        with psycopg.connect(temp.admin_dsn) as admin:
            admin.execute(sql.SQL('GRANT {} ON {} {} TO {}').format(sql.SQL(privilege),sql.SQL(kind),sql.Identifier(target),sql.Identifier(temp.runtime_role)))
        assert c.get('/readyz').status_code==200
