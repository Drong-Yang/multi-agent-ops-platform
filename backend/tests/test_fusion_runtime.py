"""A failed queued item must not terminate the only persistent worker."""
import asyncio
import unittest
from unittest.mock import AsyncMock
from app.fusion.runtime import FusionRuntime


class RuntimeRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_continues_after_one_run_cannot_be_executed(self):
        class Repo:
            def __init__(self):self.scans=0
            def scan(self,kind):
                self.scans+=1
                return [{'id':name,'owner_id':'owner','state_version':1,
                         'payload':{'task_id':name+'-task','status':'queued'}} for name in ('bad','good')] if self.scans==1 else []
            def update_cas(self,identity,owner,version,payload):
                return {'id':identity,'owner_id':owner,'state_version':version+1,'payload':payload}
            def get(self,*args):raise LookupError('injected missing task')
        rt=FusionRuntime(Repo(),None)
        continued=asyncio.Event()
        async def execute(run):
            if run['id']=='bad':raise RuntimeError('injected pre-execution failure')
            continued.set()
        rt._execute=execute
        worker=asyncio.create_task(rt._loop())
        try:
            await asyncio.wait_for(continued.wait(),timeout=1)
            self.assertFalse(worker.done())
        finally:
            worker.cancel()
            try:await worker
            except (asyncio.CancelledError,RuntimeError):pass


if __name__=='__main__':unittest.main()
