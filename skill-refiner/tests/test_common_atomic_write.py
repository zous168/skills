"""Offline tests of atomic JSON writes and Windows share-lock handling."""
import contextlib,io,json,os,subprocess,sys,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import refiner.common as common

class AtomicJsonTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.p=self.root/'state.json';common.write_json(self.p,{'old':True})
 def test_real_windows_share_lock_eventually_released(self):
  if os.name!='nt':self.skipTest('Windows share semantics')
  import ctypes
  from ctypes import wintypes
  k=ctypes.WinDLL('kernel32',use_last_error=True);create=k.CreateFileW
  create.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.LPVOID,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE];create.restype=wintypes.HANDLE
  k.CloseHandle.argtypes=[wintypes.HANDLE];h=create(str(self.p),0x80000000,3,None,3,0x80,None)
  self.assertNotEqual(h,wintypes.HANDLE(-1).value)
  threading.Timer(.2,lambda:k.CloseHandle(h)).start()
  common.write_json(self.p,{'new':True});self.assertEqual(common.read_json(self.p),{'new':True})
  self.assertEqual([p.name for p in self.root.iterdir()],['state.json'])
 def test_permission_error_without_proven_sharing_is_not_retried(self):
  error=PermissionError(13,'fixture denied');error.winerror=5
  with patch('refiner.common.os.replace',side_effect=error) as replace, patch('refiner.common._windows_delete_share_conflict',return_value=False):
   with self.assertRaises(PermissionError) as caught:common.write_json(self.p,{'bad':True})
  self.assertIs(caught.exception,error);self.assertEqual(replace.call_count,1);self.assertEqual(common.read_json(self.p),{'old':True})
  self.assertEqual([p.name for p in self.root.iterdir()],['state.json'])
 def test_target_changed_during_retry_refuses_stale_update(self):
  if os.name!='nt':self.skipTest('Windows share semantics')
  original_replace=os.replace;error=PermissionError(13,'fixture share');error.winerror=5
  def first(src,dst):raise error
  def intervening(paths):
   replacement=self.root/'newer';replacement.write_text(json.dumps({'newer':True}),encoding='utf8');original_replace(replacement,self.p);return True
  with patch('refiner.common.os.replace',side_effect=first),patch('refiner.common._windows_delete_share_conflict',side_effect=intervening):
   with self.assertRaisesRegex(common.RefinerError,'Concurrent JSON target change'):common.write_json(self.p,{'stale':True})
  self.assertEqual(common.read_json(self.p),{'newer':True})
 def test_simultaneous_writers_do_not_share_temp_or_overwrite(self):
  wait=threading.Barrier(2);real=common._json_target_lock
  @contextlib.contextmanager
  def synchronized(path):
   wait.wait(timeout=5)
   with real(path):yield
  results=[]
  def worker(i):
   path=(Path('\\\\?\\'+str(self.p)) if os.name=='nt' and i==2 else self.p)
   try:common.write_json(path,{'writer':i,'payload':str(i)*20000});results.append('ok')
   except common.RefinerError:results.append('conflict')
  with patch('refiner.common._json_target_lock',side_effect=synchronized):
   ts=[threading.Thread(target=worker,args=(i,)) for i in (1,2)]
   [t.start() for t in ts];[t.join(10) for t in ts]
  self.assertEqual(sorted(results),['conflict','ok']);value=common.read_json(self.p);self.assertEqual(value['payload'],str(value['writer'])*20000)
  self.assertEqual([p.name for p in self.root.iterdir()],['state.json'])

 def test_cross_process_simultaneous_writes_do_not_overwrite(self):
  if os.name!='nt':self.skipTest('Windows named mutex scope')
  script='import contextlib,sys,time;from pathlib import Path;sys.path.insert(0,sys.argv[1]);import refiner.common as c\n'+\
   'p=Path(sys.argv[2]);d=Path(sys.argv[3]);i=sys.argv[4];real=c._json_target_lock\n'+\
   '@contextlib.contextmanager\ndef wait(path):\n (d/("ready"+i)).write_text("ready");end=time.monotonic()+8\n while not (d/"go").exists():\n  if time.monotonic()>end:raise RuntimeError("timeout")\n  time.sleep(.01)\n with real(path):yield\nc._json_target_lock=wait\n'+\
   'try:c.write_json(p,{"writer":i});print("ok",flush=True)\nexcept c.RefinerError:print("conflict",flush=True)\n'
  processes=[subprocess.Popen([sys.executable,'-B','-X','utf8','-c',script,str(ROOT/'scripts'),str(self.p),str(self.root),str(i)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for i in (1,2)]
  try:
   end=time.monotonic()+10
   while not all((self.root/('ready'+str(i))).exists() for i in (1,2)) and time.monotonic()<end:time.sleep(.02)
   self.assertTrue(all((self.root/('ready'+str(i))).exists() for i in (1,2)))
   (self.root/'go').write_text('go')
   out=[p.communicate(timeout=10) for p in processes]
   self.assertEqual(sorted(x[0].strip() for x in out),['conflict','ok'],str(out))
   self.assertIn(common.read_json(self.p)['writer'],('1','2'))
  finally:
   for p in processes:
    if p.poll() is None:p.kill();p.wait(timeout=10)
    if p.stdout:p.stdout.close()
    if p.stderr:p.stderr.close()

 def test_killed_writer_does_not_corrupt_target(self):
  script='import sys,time;from pathlib import Path;sys.path.insert(0,sys.argv[1]);import refiner.common as c;\n'+\
         'def stop(tmp,target,baseline):\n print("READY",flush=True);time.sleep(60)\nc._replace_json_atomically=stop\nc.write_json(Path(sys.argv[2]),{"large":"x"*1000000})\n'
  proc=subprocess.Popen([sys.executable,'-B','-X','utf8','-c',script,str(ROOT/'scripts'),str(self.p)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
  try:
   self.assertEqual(proc.stdout.readline().strip(),'READY');proc.kill();proc.wait(timeout=10)
   self.assertEqual(common.read_json(self.p),{'old':True})
   # OS process termination may leave only its unique orphan temp, never partial target.
   temps=[p for p in self.root.iterdir() if p!=self.p];self.assertEqual(len(temps),1)
   common.write_json(self.p,{'after_termination':True})
   self.assertEqual(common.read_json(self.p),{'after_termination':True})
  finally:
   if proc.poll() is None:proc.kill();proc.wait(timeout=10)
   proc.stdout.close();proc.stderr.close()

if __name__=='__main__':unittest.main()
