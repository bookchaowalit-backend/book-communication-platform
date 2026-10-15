"""Explicit service lifecycle; migrations never run at API startup."""
import argparse
import os
from platform_backend_core import Database,MigrationRunner,TokenVerifier
from .migrations import MIGRATIONS
from .app import create_app

def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=['serve','migrate','worker']);args=p.parse_args()
    try:
        if args.command=='migrate':
            MigrationRunner(os.environ['DATABASE_URL'],namespace='communication').apply(MIGRATIONS)
            return
        if args.command=='worker':
            import signal,time
            from uuid import UUID
            from .worker import DeliveryWorker
            from .store import OutboxStore
            from .adapters import configured_adapters
            workspace=UUID(os.environ['COMMUNICATION_WORKSPACE_ID'])
            adapters=configured_adapters()
            if not any(a.configured for a in adapters.values()):raise ValueError('No delivery adapters')
            database=Database(os.environ['DATABASE_URL'])
            worker=DeliveryWorker(OutboxStore(database),adapters)
            running=True
            def stop(*args):
                nonlocal running
                running=False
            signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
            try:
                while running:
                    if not worker.run_one(workspace):time.sleep(0.5)
            finally:database.close()
            return
        verifier=TokenVerifier(issuer=os.environ['JWT_ISSUER'],audience=os.environ['JWT_AUDIENCE'],public_key=os.environ.get('JWT_PUBLIC_KEY'),jwks_url=os.environ.get('JWT_JWKS_URL'))
        app=create_app(Database(os.environ['DATABASE_URL']),verifier)
        import uvicorn
        uvicorn.run(app,host='0.0.0.0',port=int(os.environ.get('PORT','8080')),access_log=False,limit_concurrency=100)
    except Exception:
        raise SystemExit('Service configuration or dependency unavailable') from None
if __name__=='__main__':main()
