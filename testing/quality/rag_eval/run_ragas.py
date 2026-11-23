"""用当前 RAGAS 集合指标对已收集的记录做离线批量打分。"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline import (  # noqa: E402
    REPO_ROOT,
    collect_records,
    describe_judge_llm,
    judge_llm_config,
    metric_skip_message,
    prepare_run,
    RagPipeline,
    redact_secrets,
    require_api_key,
    select_metric_names,
    write_csv,
    write_json,
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='用 RAGAS 评测本机尽调检索（及可选生成）。')
    parser.add_argument('--dataset', required=True, help='黄金集 JSON。可以放在仓库外。')
    parser.add_argument(
        '--out',
        default=str(ROOT / 'output'),
        help='结果目录，默认 testing/quality/rag_eval/output',
    )
    parser.add_argument('--project-id', default='', help='覆盖数据集里的 project_id')
    parser.add_argument(
        '--retrieve-only',
        action='store_true',
        help='只跑检索，不生成答案，也不调用裁判模型',
    )
    return parser.parse_args(argv)


def make_ragas_llm(config=None):
    """OpenAI 兼容客户端 + llm_factory。DeepSeek 也走 provider=openai，以保留 base_url。"""
    config = require_api_key(config or judge_llm_config())
    from openai import AsyncOpenAI
    from ragas.llms import llm_factory

    kwargs = {'api_key': config['api_key']}
    if config.get('base_url'):
        kwargs['base_url'] = config['base_url']
    client = AsyncOpenAI(**kwargs)
    factory_kwargs = {'provider': 'openai', 'client': client}
    mode = (os.environ.get('EVAL_RAGAS_INSTRUCTOR_MODE') or '').strip().lower()
    if mode in {'md_json', 'markdown'}:
        import instructor

        factory_kwargs['mode'] = instructor.Mode.MD_JSON
    return llm_factory(config['model'], **factory_kwargs)


def build_scorers(llm, selected):
    from ragas.metrics.collections import ContextRecall, Faithfulness, FactualCorrectness

    scorers = []
    if 'Faithfulness' in selected:
        scorers.append(('faithfulness', Faithfulness(llm=llm), _faithfulness_kwargs))
    if 'LLMContextRecall' in selected:
        # 集合 API 的类名是 ContextRecall，对应旧的 LLMContextRecall。
        scorers.append(('context_recall', ContextRecall(llm=llm), _context_recall_kwargs))
    if 'FactualCorrectness' in selected:
        scorers.append(('factual_correctness', FactualCorrectness(llm=llm), _factual_kwargs))
    return scorers


def _faithfulness_kwargs(record):
    return {
        'user_input': record['user_input'],
        'response': record['response'],
        'retrieved_contexts': record['retrieved_contexts'],
    }


def _context_recall_kwargs(record):
    return {
        'user_input': record['user_input'],
        'retrieved_contexts': record['retrieved_contexts'],
        'reference': record['reference'],
    }


def _factual_kwargs(record):
    return {
        'response': record['response'],
        'reference': record['reference'],
    }


def metric_value(metric, **kwargs):
    result = metric.score(**kwargs)
    value = getattr(result, 'value', result)
    if hasattr(value, 'item'):
        value = value.item()
    return float(value)


def score_records(records, llm=None):
    selection = select_metric_names(records)
    message = metric_skip_message(selection)
    if message:
        print(message)
    llm = llm or make_ragas_llm()
    scorers = build_scorers(llm, selection['selected'])
    rows = []
    for record in records:
        row = {'id': record['id'], 'user_input': record['user_input']}
        for key, metric, arguments in scorers:
            try:
                row[key] = metric_value(metric, **arguments(record))
            except Exception as exc:
                detail = redact_secrets(exc)
                raise RuntimeError(f"第 {record['id']} 条 {key} 打分失败：{detail}") from None
        rows.append(row)
    aggregates = {}
    for key, _metric, _arguments in scorers:
        numbers = [row[key] for row in rows if isinstance(row.get(key), (int, float))]
        aggregates[key] = (sum(numbers) / len(numbers)) if numbers else None
    return {
        'metrics_run': selection['selected'],
        'metrics_skipped': selection['skipped'],
        'missing_reference_ids': selection['missing_reference_ids'],
        'metric_keys': {
            'Faithfulness': 'faithfulness',
            'LLMContextRecall': 'context_recall',
            'FactualCorrectness': 'factual_correctness',
        },
        'note': message or '每条都有 reference，已计算 Faithfulness、ContextRecall 和 FactualCorrectness。',
        'aggregates': aggregates,
        'rows': rows,
    }


def main(argv=None):
    args = parse_args(argv)
    dataset, project_id = prepare_run(args.dataset, args.project_id)
    if not args.retrieve_only:
        require_api_key()
    pipeline = RagPipeline()
    records, traces = collect_records(
        pipeline,
        project_id,
        dataset['items'],
        generate=not args.retrieve_only,
    )
    out = Path(args.out)
    write_json(out / 'records.json', records)
    write_json(out / 'traces.json', traces)
    if args.retrieve_only:
        print(f'已按 --retrieve-only 写出检索记录，未调用 RAGAS。输出目录：{out}')
        return
    info = describe_judge_llm()
    print(
        f"裁判模型：{info['model']}，接口：{info['base_url'] or '默认 OpenAI'}，"
        f"已配置密钥：{info['api_key_set']}"
    )
    scores = score_records(records)
    write_json(out / 'ragas_scores.json', scores)
    write_csv(out / 'ragas_scores.csv', scores['rows'])
    print(f'结果已写入：{out}')


if __name__ == '__main__':
    try:
        os.chdir(REPO_ROOT)
    except OSError:
        pass
    try:
        main()
    except (ValueError, RuntimeError, FileNotFoundError) as exc:
        raise SystemExit(redact_secrets(exc)) from None
