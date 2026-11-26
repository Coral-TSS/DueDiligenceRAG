"""事实正确性：中文拆句和对齐提示，并分开记录 precision 与 recall。

ragas 0.4.4 的 FactualCorrectness.ascore 把 precision 方向的支持条数同时当作
recall 和 F1 的分子。只把 mode 改成 "recall" 得到的仍不是
“参考答案里的事实有多少被回答覆盖”。这里两个方向分开计数：
precision 用回答陈述对参考答案的判决，recall 用参考答案陈述对回答的判决。
F1 只是这两个比例的调和平均。

本模块在导入时不加载 ragas。构造指标时才导入。
"""
from __future__ import annotations


CLAIM_INSTRUCTION = """把输入中的每个句子拆成一条或多条可以单独核查的陈述。每条陈述只写一个可核对的事实，并按照示例给出的原子度和覆盖度来拆。
日期、数字和百分数保留原数值。同一数值的不同写法仍是同一个事实，不要拆成两条。等价写法包括：2018年3月15日、2018-03-15、2018/03/15；100万元、1,000,000元；30%、百分之三十。
“根据材料”“如下”“该公司的”这类没有新事实的套话不要单独成条。用稍长的句子把同一个事实包起来时，只保留这一条事实。
不要补充输入里没有的字段。
输出 JSON 的键名使用 claims，不要改键名。"""

NLI_INSTRUCTION = """根据上下文判断每条陈述是否得到支持。verdict 为 1 表示支持，为 0 表示不支持。
同一个事实的等价写法算支持，包括日期（2018年3月15日、2018-03-15、2018/03/15）、数字和金额（100万元、1,000,000元）、百分数（30%、百分之三十）。
陈述只用稍长的句子把上下文里已经有的事实包起来，而且没有加入与上下文矛盾的内容时，判为 1。多出来的非矛盾措辞不要因此判 0。
与上下文矛盾的陈述判为 0。说到另一个字段的陈述也判为 0，例如上下文给出的是成立日期，陈述却是法定代表人或注册资本。上下文没有的事实判为 0。
不要因为句子比上下文略长，就套用“必须逐字、直接推断才算支持”的规则把同一事实判成 0。
输出 JSON 的键名使用 statements、statement、reason、verdict，不要改键名。reason 用中文简述。"""

# (atomicity, coverage) -> [(response, claims)]。都是合成例子，不是客户材料。
CLAIM_EXAMPLES = {
    ('low', 'low'): [
        (
            '根据材料，示例主体的成立日期为2018-03-15。',
            ['示例主体的成立日期为2018-03-15。'],
        ),
        (
            '示例主体是示例行业的公司，法定代表人是甲，注册资本为100万元。',
            ['示例主体的法定代表人是甲，注册资本为100万元。'],
        ),
    ],
    ('low', 'high'): [
        (
            '根据材料，示例主体的成立日期为2018-03-15。',
            ['示例主体的成立日期为2018-03-15。'],
        ),
        (
            '示例主体的法定代表人是甲。注册资本为100万元，持股比例为30%。',
            [
                '示例主体的法定代表人是甲。',
                '示例主体的注册资本为100万元，持股比例为30%。',
            ],
        ),
    ],
    ('high', 'low'): [
        (
            '根据材料，示例主体的成立日期为2018-03-15。',
            ['示例主体的成立日期为2018-03-15。'],
        ),
        (
            '示例主体是示例行业的公司，法定代表人是甲，注册资本为100万元。',
            [
                '示例主体的法定代表人是甲。',
                '示例主体的注册资本为100万元。',
            ],
        ),
    ],
    ('high', 'high'): [
        (
            '根据材料，示例主体成立于2018年3月15日，注册资本为100万元。',
            [
                '示例主体成立于2018年3月15日。',
                '示例主体的注册资本为100万元。',
            ],
        ),
        (
            '示例主体的持股比例为30%，法定代表人是甲。',
            [
                '示例主体的持股比例为30%。',
                '示例主体的法定代表人是甲。',
            ],
        ),
    ],
}

NLI_EXAMPLES = [
    {
        'context': '示例主体成立于2018年3月15日。',
        'statements': [
            {
                'statement': '根据材料，示例主体的成立日期为2018-03-15。',
                'reason': '这是同一个成立日期，只是写法不同，并用稍长的句子包住该事实，没有矛盾内容。',
                'verdict': 1,
            },
            {
                'statement': '示例主体的法定代表人是甲。',
                'reason': '上下文只给出成立日期，法定代表人是另一个字段。',
                'verdict': 0,
            },
            {
                'statement': '示例主体成立于2019年3月15日。',
                'reason': '成立日期与上下文矛盾。',
                'verdict': 0,
            },
        ],
    },
    {
        'context': '示例主体的持股比例为30%，注册资本是100万元。',
        'statements': [
            {
                'statement': '示例主体持股百分之三十。',
                'reason': '百分之三十和 30% 是同一个比例。',
                'verdict': 1,
            },
            {
                'statement': '示例主体的注册资本为1,000,000元。',
                'reason': '1,000,000元和 100万元是同一个金额。',
                'verdict': 1,
            },
            {
                'statement': '示例主体的持股比例为13%。',
                'reason': '13% 与上下文中的 30% 矛盾。',
                'verdict': 0,
            },
        ],
    },
]

_NLI_TAGS = ('supported', 'unsupported', 'other')
_METRIC_CLASS = None


def _supported(flag):
    return flag is True or flag == 1


def verdict_counts(flags):
    """只数判决标签，不保留陈述文本。"""
    counts = {tag: 0 for tag in _NLI_TAGS}
    for flag in flags or []:
        if _supported(flag):
            counts['supported'] += 1
        elif flag is False or flag == 0:
            counts['unsupported'] += 1
        else:
            counts['other'] += 1
    return counts


def _rate(flags):
    flags = list(flags or [])
    if not flags:
        return 0.0
    return sum(1 for flag in flags if _supported(flag)) / len(flags)


def _round_score(value):
    return float(round(float(value) + 1e-12, 2))


def scores_from_verdicts(precision_flags, recall_flags):
    """precision_flags：回答陈述是否被参考答案支持。

    recall_flags：参考答案陈述是否被回答支持。
    recall 的分子是 recall_flags 里的支持条数，不用 precision 方向的支持条数。
    """
    precision = _rate(precision_flags)
    recall = _rate(recall_flags)
    if precision == 0 and recall == 0:
        f1 = 0.0
    else:
        f1 = (2 * precision * recall) / (precision + recall)
    return {
        'precision': _round_score(precision),
        'recall': _round_score(recall),
        'f1': _round_score(f1),
    }


def compact_judge_trace(trace):
    """留下条目计数和分数。丢掉陈述、答案、参考答案和材料片段。"""
    if not isinstance(trace, dict):
        return None

    def count(value):
        try:
            number = int(value)
        except (TypeError, ValueError):
            return 0
        return number if number >= 0 else 0

    def score(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def nli(block):
        block = block if isinstance(block, dict) else {}
        return {tag: count(block.get(tag)) for tag in _NLI_TAGS}

    return {
        'response_claim_count': count(trace.get('response_claim_count')),
        'reference_claim_count': count(trace.get('reference_claim_count')),
        'precision_nli': nli(trace.get('precision_nli')),
        'recall_nli': nli(trace.get('recall_nli')),
        'precision': score(trace.get('precision')),
        'recall': score(trace.get('recall')),
        'f1': score(trace.get('f1')),
    }


def chinese_claim_prompt():
    from ragas.metrics.collections.factual_correctness.util import (
        ClaimDecompositionInput,
        ClaimDecompositionOutput,
        ClaimDecompositionPrompt,
    )

    class ChineseClaimDecompositionPrompt(ClaimDecompositionPrompt):
        instruction = CLAIM_INSTRUCTION
        language = 'chinese'

    prompt = ChineseClaimDecompositionPrompt()
    all_examples = {}
    for key, pairs in CLAIM_EXAMPLES.items():
        atomicity, coverage = key
        all_examples[key] = [
            (
                ClaimDecompositionInput(
                    response=response,
                    atomicity=atomicity,
                    coverage=coverage,
                ),
                ClaimDecompositionOutput(claims=list(claims)),
            )
            for response, claims in pairs
        ]
    prompt._all_examples = all_examples
    prompt.examples = all_examples[('low', 'low')]
    return prompt


def chinese_nli_prompt():
    from ragas.metrics.collections.factual_correctness.util import (
        NLIStatementInput,
        NLIStatementOutput,
        NLIStatementPrompt,
        StatementFaithfulnessAnswer,
    )

    class ChineseNLIStatementPrompt(NLIStatementPrompt):
        instruction = NLI_INSTRUCTION
        language = 'chinese'

    prompt = ChineseNLIStatementPrompt()
    prompt.examples = [
        (
            NLIStatementInput(
                context=item['context'],
                statements=[row['statement'] for row in item['statements']],
            ),
            NLIStatementOutput(
                statements=[
                    StatementFaithfulnessAnswer(
                        statement=row['statement'],
                        reason=row['reason'],
                        verdict=row['verdict'],
                    )
                    for row in item['statements']
                ]
            ),
        )
        for item in NLI_EXAMPLES
    ]
    return prompt


def build_factual_metric(llm):
    """中文提示的事实正确性。分数方向与 ragas 0.4.4 的默认 ascore 不同。"""
    cls = _due_diligence_metric_cls()
    return cls(llm)


def _due_diligence_metric_cls():
    global _METRIC_CLASS
    if _METRIC_CLASS is not None:
        return _METRIC_CLASS

    from ragas.metrics.collections import FactualCorrectness
    from ragas.metrics.result import MetricResult

    class DueDiligenceFactualCorrectness(FactualCorrectness):
        """拆句和 NLI 仍走 ragas，分数按两个方向分别计算。"""

        def __init__(self, llm):
            super().__init__(llm=llm, mode='f1', name='factual_correctness')
            self.prompt = chinese_claim_prompt()
            self.nli_prompt = chinese_nli_prompt()
            self.judge_trace = None

        async def ascore(self, response: str, reference: str) -> MetricResult:
            if not response:
                raise ValueError('response is missing. Please add response to the test sample.')
            if not reference:
                raise ValueError('reference is missing. Please add reference to the test sample.')
            response_claims, precision_flags = await self._claims_and_verdicts(response, reference)
            reference_claims, recall_flags = await self._claims_and_verdicts(reference, response)
            scored = scores_from_verdicts(precision_flags, recall_flags)
            trace = compact_judge_trace({
                'response_claim_count': len(response_claims),
                'reference_claim_count': len(reference_claims),
                'precision_nli': verdict_counts(precision_flags),
                'recall_nli': verdict_counts(recall_flags),
                **scored,
            })
            self.judge_trace = trace
            result = MetricResult(value=scored['f1'])
            result.precision = scored['precision']
            result.recall = scored['recall']
            result.f1 = scored['f1']
            result.judge_trace = trace
            return result

        async def _claims_and_verdicts(self, text, against):
            claims = await self._decompose_claims(text)
            claims = [claim for claim in (claims or []) if isinstance(claim, str) and claim.strip()]
            if not claims:
                return [], []
            verdicts = await self._verify_claims(claims, against)
            statements = list(getattr(verdicts, 'statements', None) or [])
            flags = [getattr(item, 'verdict', None) for item in statements]
            return claims, flags

    _METRIC_CLASS = DueDiligenceFactualCorrectness
    return _METRIC_CLASS
