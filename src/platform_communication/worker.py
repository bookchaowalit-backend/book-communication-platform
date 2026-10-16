from .adapters import DeliveryError
class DeliveryWorker:
    """Bounded synchronous single-workspace worker, safe to run in separate processes."""
    def __init__(self,store,adapters):self.store=store;self.adapters=adapters
    def run_one(self,workspace):
        message=self.store.claim(workspace)
        if message is None:return False
        adapter=self.adapters.get(message['channel'])
        if not self.store.mark_sending(workspace,message['id'],message['lease_token']):return True
        outcome,provider_id='dead',None
        if adapter is not None and adapter.configured:
            try:provider_id=adapter.send(message);outcome='delivered'
            except DeliveryError as exc:outcome=exc.outcome
            except Exception:outcome='uncertain'
        self.store.finish(workspace,message['id'],message['lease_token'],outcome,provider_id)
        return True
