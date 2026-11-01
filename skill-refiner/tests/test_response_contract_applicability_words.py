import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from refiner.common import RefinerError
from refiner.pipeline import response_contract_applicability


class ResponseApplicabilityWordsTests(unittest.TestCase):
    def setUp(self):
        self.plan = {'cases': [{'id': 'normal', 'kind': 'normal'},
                               {'id': 'error', 'kind': 'error'},
                               {'id': 'boundary', 'kind': 'boundary'}]}

    def applicability(self, text, files=None):
        return response_contract_applicability({'requirements': [{'source': 'req', 'content': text}],
                                                'files': files or []}, self.plan)

    def test_inverted_json_output_requires_contract_on_each_case(self):
        for text in ('请以 JSON 格式返回结果。', '回复需要符合 JSON 格式。',
                     'JSON 是最终响应格式。', '最终输出 JSON。'):
            with self.subTest(text=text):
                self.assertEqual(set(self.applicability(text)['required_case_ids']),
                                 {'normal', 'error', 'boundary'})

    def test_unknown_conditional_output_needs_independent_classification(self):
        for text in ('不确定时返回 JSON，其他情况返回纯文本。',
                     '只有用户明确提出时输出 JSON。',
                     '当用户授权且发生错误时返回 JSON。'):
            with self.subTest(text=text), self.assertRaisesRegex(RefinerError, 'Conditional JSON'):
                self.applicability(text)

    def test_explicit_error_branch_and_text_branch_are_distinct(self):
        result = self.applicability('如果发生错误，返回 JSON；正常时返回自然语言。')
        self.assertEqual(result['required_case_ids'], ['error'])

    def test_scoped_json_without_matching_case_does_not_pass_empty(self):
        self.plan['cases'] = [case for case in self.plan['cases'] if case['kind'] != 'error']
        with self.assertRaisesRegex(RefinerError, 'no matching case'):
            self.applicability('如果发生错误，返回 JSON；正常时返回自然语言。')

    def test_input_schema_and_internal_log_are_not_output_contracts(self):
        files = [{'path': 'schemas/v1/input.schema.json', 'content': '{}'}]
        for text in ('使用 schemas/v1/input.schema.json 验证输入。最终输出纯文本。',
                     '使用 schemas/v1/input.schema.json 验证输入。日志输出 JSON。',
                     '输入使用 schemas/v1/input.schema.json，最终输出纯文本。'):
            with self.subTest(text=text):
                self.assertEqual(self.applicability(text, files)['required_case_ids'], [])

    def test_unknown_schema_role_stays_blocked(self):
        with self.assertRaisesRegex(RefinerError, 'role is not established'):
            self.applicability('根据要求处理。', [{'path': 'schemas/v1/unknown.schema.json', 'content': '{}'}])

    def test_explicit_chinese_and_english_json_output(self):
        for text in ('最终答复必须是 JSON 格式。', '请使用 JSON 对象作答。',
                     'The final answer must be JSON.', 'Return a valid JSON object.',
                     '最终答案必须为 JSON。', '输出格式：JSON。'):
            with self.subTest(text=text):
                self.assertEqual(set(self.applicability(text)['required_case_ids']),
                                 {'normal', 'error', 'boundary'})

    def test_negation_and_input_json_do_not_impose_output_contract(self):
        for text in ('不要返回 JSON，直接输出自然语言。', '不得将结果输出为 JSON 格式。',
                     "Don't return JSON; reply in plain text.", '输出不应是 JSON，请直接给出中文说明。',
                     'JSON 只用于输入，输出应为自然语言。', '请求输入为 JSON 且输出使用纯文本。',
                     '请说明如何把普通文本转换为 JSON 格式。', '用户上传 JSON 数据，请给出纯文字摘要。',
                     '日志输出 JSON。', '请输出非 JSON 格式。'):
            with self.subTest(text=text):
                self.assertEqual(self.applicability(text)['required_case_ids'], [])

    def test_negation_after_json_does_not_erase_positive_requirement(self):
        self.assertEqual(set(self.applicability('输出 JSON 不得包含 Markdown。')['required_case_ids']),
                         {'normal', 'error', 'boundary'})
        with self.assertRaisesRegex(RefinerError, 'Conditional JSON'):
            self.applicability('如果不需要澄清则输出 JSON。')

    def test_unclassified_conditions_do_not_default_to_global(self):
        for text in ('JSON should be returned only on failure; otherwise return plain text.',
                     'If there is an error, return JSON; otherwise return plain text.',
                     '输出 JSON，只在失败时。', 'JSON 不仅用于输入，也用于输出。',
                     '不允许非 JSON 格式的最终输出。'):
            with self.subTest(text=text), self.assertRaisesRegex(RefinerError, 'Conditional JSON'):
                self.applicability(text)
        self.assertEqual(self.applicability('成功时请输出 JSON，失败时请输出自然语言。')['required_case_ids'],
                         ['normal'])

    def test_input_schema_mentions_in_different_positions(self):
        path = 'schemas/v1/request.schema.json'
        files = [{'path': path, 'content': '{}'}]
        for text in (f'使用 {path} 校验输入，回答为自然语言。',
                     f'{path} 是输入的约束。请以中文作答。',
                     f'把输入按 {path} 校验；输出文字摘要。'):
            with self.subTest(text=text):
                self.assertEqual(self.applicability(text, files)['required_case_ids'], [])

    def test_double_negative_is_unresolved_and_never_passes_null(self):
        for text in ('Never omit the JSON response.', '不得不返回 JSON 格式结果。',
                     '请勿省略 JSON 输出。', '不允许非 JSON 格式的最终输出。'):
            with self.subTest(text=text), self.assertRaisesRegex(RefinerError, 'needs evidence-based classification'):
                self.applicability(text)

    def test_complex_conditions_never_become_global_json(self):
        for text in ('除了失败情形之外，均返回 JSON；失败请输出自然语言。',
                     '无错误时返回 JSON，错误时返回自然语言。',
                     '只要没有错误，就返回 JSON；出错时返回自然语言。',
                     '仅在成功时返回 JSON，其他情况为纯文本。',
                     'Always output JSON unless an error occurs.'):
            with self.subTest(text=text), self.assertRaisesRegex(RefinerError, 'needs evidence-based classification'):
                self.applicability(text)

    def test_file_json_is_not_automatically_a_json_text_response(self):
        text = '输出 JSON 文件供程序消费，最终回复用户使用自然语言。'
        self.assertEqual(self.applicability(text)['required_case_ids'], [])
        with self.assertRaisesRegex(RefinerError, 'needs evidence-based classification'):
            self.applicability('输出 JSON 文件供程序消费。')

    def test_plain_text_and_unambiguous_non_response_json(self):
        for text in ('请提供纯中文说明，不使用 JSON。',
                     '配置示例是 JSON，不要求以 JSON 作答。',
                     '将内部报表保存为 JSON，用户只需看到自然语言答案。'):
            with self.subTest(text=text):
                self.assertEqual(self.applicability(text)['required_case_ids'], [])

    def test_conflicting_global_formats_are_unresolved(self):
        with self.assertRaisesRegex(RefinerError, 'needs evidence-based classification'):
            self.applicability('最终输出 JSON。最终输出纯文本。')


if __name__ == '__main__':
    unittest.main()
