import tempfile
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from refiner.matrix import Matrix

class MatrixProgressContextTests(unittest.TestCase):
    def test_each_executor_gets_real_suite_and_round_then_clears(self):
        seen=[]
        class Backend:
            evaluations=0
            def evaluate(self,target,cases,run_dir,selected=None):
                seen.append(dict(self.case_progress_context))
                self.evaluations+=1
                return {'passed':True,'cases':[{'id':cases[0]['id'],'status':'PASS','passed':True}]}
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'skill';source.mkdir();(source/'SKILL.md').write_text('test',encoding='utf8')
            m=Matrix.__new__(Matrix)
            m.work=root; m.engine={'matrix':[]};m.evaluations=0;m.max_evaluations=10;m.deadline=None
            m.progress_callback=None;m.case_progress_context={'suite':'regression','round':2}
            first,second=Backend(),Backend()
            m.backends=[({'id':'a','backend':'skillup'},first),({'id':'b','backend':'skillup'},second)]
            m.evaluate(source,[{'id':'same-case'}],root/'run',existing=False)
            self.assertEqual(seen,[{'suite':'regression','round':2,'executor':'a'},
                                   {'suite':'regression','round':2,'executor':'b'}])
            self.assertIsNone(first.case_progress_context)
            self.assertIsNone(second.case_progress_context)

if __name__=='__main__':unittest.main()
