"""Offline concurrency and budget tests for grouped Agent reservations."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from refiner.agent import Agent
from refiner.common import RefinerError

SCHEMA={'type':'object','required':['ok'],'properties':{'ok':{'type':'boolean'}}}

def spec(role, attempt=1):
    return {'role':role,'instruction':'fixture instruction','payload':{'case':role},'schema':SCHEMA,'_attempt':attempt}

def call(agent, item, ticket=None):
    return agent.ask(item['role'],item['instruction'],item['payload'],item['schema'],
                     _attempt=item['_attempt'],_reservation=ticket)

def good(*args, **kwargs):
    return 0, json.dumps({'ok':True}), '', 0.01

class CallGroupTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.agent=Agent(Path(temp.name),command=sys.executable)
        self.a,self.b=spec('analysis-review'),spec('plan-review')
        self.capture=contextlib.redirect_stdout(io.StringIO());self.capture.__enter__()
        self.addCleanup(self.capture.__exit__,None,None,None)

    def test_budget_atomic_and_unused_released(self):
        self.agent.max_calls=1
        with self.assertRaisesRegex(RefinerError,'before reserving'):
            with self.agent.reserve_call_group([self.a,self.b]):self.fail('must not enter')
        self.assertEqual((self.agent.calls,self.agent._reserved_calls),(0,0))
        self.agent.max_calls=2
        with self.agent.reserve_call_group([self.a,self.b]) as t:
            self.assertEqual(self.agent._reserved_calls,2)
            with patch('refiner.agent.run_process',side_effect=good):call(self.agent,self.a,t[0])
            self.assertEqual((self.agent.calls,self.agent._reserved_calls),(1,1))
        self.assertEqual((self.agent.calls,self.agent._reserved_calls),(1,0))
        with self.assertRaises(RefinerError):call(self.agent,self.b,t[1])

    def test_outside_thread_cannot_steal_and_two_workers_run(self):
        self.agent.max_calls=2; gate=threading.Event(); started=threading.Event(); errors=[]; result=[]
        def blocked(*args,**kwargs):
            started.set();gate.wait(5);return good()
        def worker(item,ticket):
            try:result.append(call(self.agent,item,ticket))
            except BaseException as e:errors.append(e)
        with patch('refiner.agent.run_process',side_effect=blocked):
            with self.agent.reserve_call_group([self.a,self.b]) as tickets:
                one=threading.Thread(target=worker,args=(self.a,tickets[0]));one.start()
                self.assertTrue(started.wait(4))
                with self.assertRaisesRegex(RefinerError,'budget exhausted'):call(self.agent,spec('outsider'))
                two=threading.Thread(target=worker,args=(self.b,tickets[1]));two.start()
                gate.set();one.join(6);two.join(6)
        self.assertFalse(errors,str(errors));self.assertEqual(len(result),2)
        self.assertEqual((self.agent.calls,self.agent._reserved_calls),(2,0))

    def test_one_cached_and_one_miss_in_either_order(self):
        for cached_index in (0,1):
            with self.subTest(cached_index=cached_index), tempfile.TemporaryDirectory() as temp:
                agent=Agent(Path(temp),command=sys.executable);items=[self.a,self.b]
                with patch('refiner.agent.run_process',side_effect=good):call(agent,items[cached_index])
                agent.max_calls=2
                with patch('refiner.agent.run_process',side_effect=good) as transport:
                    with agent.reserve_call_group(items) as tickets:
                        self.assertEqual(agent._reserved_calls,1)
                        for item,ticket in zip(items,tickets):call(agent,item,ticket)
                    self.assertEqual(transport.call_count,1)
                self.assertEqual((agent.calls,agent.cached_calls,agent._reserved_calls),(2,1,0))

    def test_cache_disappears_without_unreserved_execution(self):
        with patch('refiner.agent.run_process',side_effect=good):call(self.agent,self.a)
        self.agent.max_calls=3
        with self.agent.reserve_call_group([self.a,self.b]) as t:
            response=next((self.agent.work/'agent-calls').glob('*-analysis-review/response.json'))
            response.unlink()
            with patch('refiner.agent.run_process',side_effect=good) as transport:
                with self.assertRaisesRegex(RefinerError,'cache is unavailable'):call(self.agent,self.a,t[0])
                self.assertEqual(transport.call_count,0)
                call(self.agent,self.b,t[1])
        self.assertEqual(self.agent.calls,2)

    def test_exception_keeps_used_and_releases_unused(self):
        self.agent.max_calls=2
        with self.assertRaisesRegex(RuntimeError,'transport failure'):
            with self.agent.reserve_call_group([self.a,self.b]) as t:
                with patch('refiner.agent.run_process',side_effect=RuntimeError('transport failure')):
                    call(self.agent,self.a,t[0])
        self.assertEqual((self.agent.calls,self.agent._reserved_calls),(1,0))
        with patch('refiner.agent.run_process',side_effect=good):call(self.agent,self.b)
        self.assertEqual(self.agent.calls,2)

    def test_retry_cannot_spend_sibling_reservation(self):
        self.agent.max_calls=2;first=spec('analysis-review',0)
        def invalid(*args,**kwargs):return 0,'{}','',0.01
        with self.agent.reserve_call_group([first,self.b]) as t:
            with patch('refiner.agent.run_process',side_effect=invalid) as transport:
                with self.assertRaisesRegex(RefinerError,'budget exhausted'):call(self.agent,first,t[0])
                self.assertEqual(transport.call_count,1)
            with patch('refiner.agent.run_process',side_effect=good):call(self.agent,self.b,t[1])
        self.assertEqual((self.agent.calls,self.agent._reserved_calls),(2,0))

    def test_invalid_cache_retry_uses_ticket_not_extra(self):
        first=spec('analysis-review',0);self.agent.max_calls=1
        with patch('refiner.agent.run_process',return_value=(0,'{}','',0.01)):
            with self.assertRaisesRegex(RefinerError,'budget exhausted'):call(self.agent,first)
        self.agent.max_calls=3
        with patch('refiner.agent.run_process',side_effect=good) as transport:
            with self.agent.reserve_call_group([first,self.b]) as t:
                call(self.agent,first,t[0]);call(self.agent,self.b,t[1])
            self.assertEqual(transport.call_count,2)
        self.assertEqual((self.agent.calls,self.agent.cached_calls,self.agent._reserved_calls),(3,1,0))

    def test_all_cached_or_terminal_invalid_cache_need_no_slots(self):
        with patch('refiner.agent.run_process',side_effect=good):
            call(self.agent,self.a);call(self.agent,self.b)
        self.agent.max_calls=2
        with patch('refiner.agent.run_process',side_effect=good) as transport:
            with self.agent.reserve_call_group([self.a,self.b]) as tickets:
                call(self.agent,self.a,tickets[0]);call(self.agent,self.b,tickets[1])
            self.assertEqual(transport.call_count,0)
        self.assertEqual(self.agent.calls,2)
        with tempfile.TemporaryDirectory() as temp:
            agent=Agent(Path(temp),command=sys.executable);agent.max_calls=1
            with patch('refiner.agent.run_process',return_value=(0,'{}','',0.01)):
                with self.assertRaises(RefinerError):call(agent,self.a)
            with agent.reserve_call_group([self.a]) as tickets:
                self.assertEqual(agent._reserved_calls,0)
                with patch('refiner.agent.run_process',side_effect=good) as transport:
                    with self.assertRaisesRegex(RefinerError,'invalid contract'):call(agent,self.a,tickets[0])
                    self.assertEqual(transport.call_count,0)
            self.assertEqual(agent.calls,1)

    def test_forged_mutable_ticket_and_false_continuation_rejected(self):
        self.agent.max_calls=0
        *_,serialized=self.agent._prepare_request(self.a['role'],self.a['instruction'],self.a['payload'],SCHEMA,1)
        forged={'agent':self.agent,'identity':self.agent._request_identity(serialized,SCHEMA,1),
                'remaining':1,'spent':False,'begun':False,'owner':None,'closed':False}
        with patch('refiner.agent.run_process',side_effect=good) as transport:
            with self.assertRaisesRegex(RefinerError,'invalid or closed'):call(self.agent,self.a,forged)
            self.assertEqual((self.agent.calls,self.agent._reserved_calls,transport.call_count),(0,0,0))
        self.agent.max_calls=2
        with self.agent.reserve_call_group([self.a]) as tickets:
            with patch('refiner.agent.run_process',side_effect=good) as transport:
                call(self.agent,self.a,tickets[0])
                with self.assertRaisesRegex(RefinerError,'not authorized'):
                    self.agent.ask(self.a['role'],self.a['instruction'],{'different':True},SCHEMA,
                                   _attempt=1,_reservation=tickets[0],_reservation_continuation=True)
                self.assertEqual((self.agent.calls,transport.call_count),(1,1))

    def test_mutated_backend_refused_and_new_instance_cache_bound(self):
        with patch('refiner.agent.run_process',side_effect=good):call(self.agent,self.a)
        self.agent.command=['other-backend']
        with patch('refiner.agent.run_process',side_effect=good) as transport:
            with self.assertRaisesRegex(RefinerError,'changed after initialization'):call(self.agent,self.a)
            self.assertEqual(transport.call_count,0)
        other=Agent(self.agent.work,command='other-backend');other.max_calls=2
        with patch('refiner.agent.run_process',side_effect=good) as transport:call(other,self.a)
        self.assertEqual((other.calls,other.cached_calls,transport.call_count),(2,0,1))

    def test_legacy_cache_requires_persisted_config_and_matching_execution(self):
        config={'agents':{'default':{'backend':'command','command':['fixture']}}}
        agent=Agent(self.agent.work,config=config)
        with patch('refiner.agent.run_process',side_effect=good):call(agent,self.a)
        manifest=agent.work/'agent-calls/001-analysis-review/request-manifest.json'
        data=json.loads(manifest.read_text(encoding='utf8'));data.pop('backend_config_sha256')
        manifest.write_text(json.dumps(data),encoding='utf8')
        (agent.work/'backend-config.json').write_text(json.dumps(config),encoding='utf8')
        resumed=Agent(agent.work,config=config);resumed.max_calls=1
        with patch('refiner.agent.run_process',side_effect=good) as transport:call(resumed,self.a)
        self.assertEqual((resumed.calls,resumed.cached_calls,transport.call_count),(1,1,0))
        wrong={'agents':{'default':{'backend':'command','command':['different']}}}
        other=Agent(agent.work,config=wrong);other.max_calls=2
        with patch('refiner.agent.run_process',side_effect=good) as transport:call(other,self.a)
        self.assertEqual((other.calls,other.cached_calls,transport.call_count),(2,0,1))

    def test_context_preflight_rejection_does_not_count_or_create_call(self):
        item={**self.a,'payload':{'x':'x'*360000}};self.agent.max_calls=1
        state={'agent_calls':0};self.agent.bind_state(state,elapsed=lambda:0)
        with self.agent.reserve_call_group([item]) as tickets:
            with patch('refiner.agent.run_process',side_effect=good) as transport:
                with self.assertRaisesRegex(RefinerError,'exceeding the configured'):call(self.agent,item,tickets[0])
                self.assertEqual((self.agent.calls,self.agent._reserved_calls,state['agent_calls'],transport.call_count),(0,1,0,0))
        self.assertEqual(self.agent._reserved_calls,0)
        self.assertFalse((self.agent.work/'agent-calls').exists())
        rejected=list((self.agent.work/'rejected-agent-requests').glob('*/request-manifest.json'))
        self.assertEqual(len(rejected),1)
        self.assertEqual(json.loads(rejected[0].read_text(encoding='utf8'))['status'],'rejected_context_budget')
        self.assertEqual(Agent(self.agent.work,command=sys.executable).calls,0)

    def test_ticket_bound_to_exact_attempt_request_and_single_use(self):
        self.agent.max_calls=2
        with self.agent.reserve_call_group([self.a,self.b]) as t:
            changed={**self.a,'_attempt':0}
            with self.assertRaisesRegex(RefinerError,'does not match'):call(self.agent,changed,t[0])
            with patch('refiner.agent.run_process',side_effect=good):
                call(self.agent,self.a,t[0])
                with self.assertRaisesRegex(RefinerError,'already used'):call(self.agent,self.a,t[0])
                call(self.agent,self.b,t[1])

if __name__=='__main__':unittest.main()
