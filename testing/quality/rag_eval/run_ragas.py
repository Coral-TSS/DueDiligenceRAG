"""用当前 RAGAS 集合指标对已收集的记录做离线批量打分。"""
from __future__ import annotations

import argparse
import inspect
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline import (  # noqa: E402
    REPO_ROOT,
    collect_records,
    completion_kwargs,
    format_judge_log,
    judge_llm_config,
    load_eval_env,
    load_score_records,
    metric_skip_message,
    openai_client,
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
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--dataset', help='黄金集 JSON。可以放在仓库外。')
    source.add_argument(
        '--score-from',
        help='已有 records.json。只打分，不检索、不生成，也不改写 records.json / traces.json。',
    )
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
    """OpenAI 兼容客户端 + llm_factory。DeepSeek 也走 provider=openai，以保留 base_url。

    集合指标的 score() 会 asyncio.run(ascore())，ascore 再 await llm.agenerate()。
    因此这里必须是异步客户端，而且 chat.completions.create 必须仍是协程函数。
    思考开关若用普通函数包住 AsyncCompletions.create，instructor 会把它当成同步调用，
    返回值是未等待的协程，读取 choices 失败。
    llm_factory 的 kwargs 再送一遍思考开关，用来盖过 RAGAS 默认的 max_tokens=1024；
    instructor 若丢掉 extra_body，客户端绑定仍会补上。
    """
    config = require_api_key(config or judge_llm_config())
    from ragas.llms import llm_factory

    client = openai_client(config, async_client=True)
    create = getattr(getattr(getattr(client, 'chat', None), 'completions', None), 'create', None)
    if not inspect.iscoroutinefunction(create):
        raise RuntimeError(
            'RAGAS 裁判需要异步客户端，但 chat.completions.create 不是协程函数。'
            '这样 Faithfulness 会在未等待的协程上读取 choices。'
        )
    factory_kwargs = {'provider': 'openai', 'client': client}
    factory_kwargs.update(completion_kwargs(config))
    mode = (os.environ.get('EVAL_RAGAS_INSTRUCTOR_MODE') or '').strip().lower()
    if mode in {'md_json', 'markdown'}:
        import instructor

        factory_kwargs['mode'] = instructor.Mode.MD_JSON
    return llm_factory(config['model'], **factory_kwargs)


def build_scorers(llm, selected):
    from ragas.metrics.collections import ContextRecall, Faithfulness

    from factual import build_factual_metric

    scorers = []
    if 'Faithfulness' in selected:
        scorers.append(('faithfulness', Faithfulness(llm=llm), _faithfulness_kwargs))
    if 'LLMContextRecall' in selected:
        # 集合 API 的类名是 ContextRecall，对应旧的 LLMContextRecall。
        scorers.append(('context_recall', ContextRecall(llm=llm), _context_recall_kwargs))
    if 'FactualCorrectness' in selected:
        # 不用 FactualCorrectness 的默认 ascore。ragas 0.4.4 的 recall / F1
        # 分子仍是 precision 方向的支持条数，mode="recall" 改不了这一点。
        scorers.append(('factual_correctness', build_factual_metric(llm), _factual_kwargs))
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


FACTUAL_NOTE = (
    '每条都有 reference，已计算 Faithfulness、ContextRecall 和 FactualCorrectness。'
    'FactualCorrectness 的 factual_correctness_precision 是回答中的事实被参考答案支持的比例，'
    'factual_correctness_recall 是参考答案中的事实被回答覆盖的比例，'
    'factual_correctness 与 factual_correctness_f1 是这两者的调和平均。'
    '条目计数写在 factual_judge_trace.json，与 ragas_scores 一起写出。'
)
FACTUAL_SCORE_FIELDS = (
    'factual_correctness',
    'factual_correctness_precision',
    'factual_correctness_recall',
    'factual_correctness_f1',
)


def metric_value(metric, **kwargs):
    result = metric.score(**kwargs)
    if hasattr(result, 'precision') and hasattr(result, 'recall'):
        f1 = getattr(result, 'f1', None)
        if f1 is None:
            f1 = getattr(result, 'value', 0.0)
        return {
            'precision': float(result.precision),
            'recall': float(result.recall),
            'f1': float(f1),
            'judge_trace': getattr(result, 'judge_trace', None),
        }
    value = getattr(result, 'value', result)
    if hasattr(value, 'item'):
        value = value.item()
    return float(value)


def _apply_factual_value(row, record, value, traces):
    from factual import compact_judge_trace

    row['factual_correctness_precision'] = value['precision']
    row['factual_correctness_recall'] = value['recall']
    row['factual_correctness_f1'] = value['f1']
    row['factual_correctness'] = value['f1']
    trace = compact_judge_trace(value.get('judge_trace'))
    if trace is None:
        trace = compact_judge_trace({
            'precision': value['precision'],
            'recall': value['recall'],
            'f1': value['f1'],
        })
    traces.append({'id': record['id'], **trace})


def score_records(records, llm=None, scorers=None):
    selection = select_metric_names(records)
    message = metric_skip_message(selection)
    if message:
        print(message)
    if scorers is None:
        llm = llm or make_ragas_llm()
        scorers = build_scorers(llm, selection['selected'])
    rows = []
    traces = []
    for record in records:
        row = {'id': record['id'], 'user_input': record['user_input']}
        for key, metric, arguments in scorers:
            try:
                value = metric_value(metric, **arguments(record))
            except Exception as exc:
                detail = redact_secrets(exc)
                raise RuntimeError(f"第 {record['id']} 条 {key} 打分失败：{detail}") from None
            if key == 'factual_correctness' and isinstance(value, dict):
                _apply_factual_value(row, record, value, traces)
            else:
                row[key] = value
        rows.append(row)
    aggregate_keys = []
    for key, _metric, _arguments in scorers:
        if key not in aggregate_keys:
            aggregate_keys.append(key)
    for key in FACTUAL_SCORE_FIELDS:
        if any(key in row for row in rows) and key not in aggregate_keys:
            aggregate_keys.append(key)
    aggregates = {}
    for key in aggregate_keys:
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
            'FactualCorrectnessPrecision': 'factual_correctness_precision',
            'FactualCorrectnessRecall': 'factual_correctness_recall',
            'FactualCorrectnessF1': 'factual_correctness_f1',
        },
        'note': message or FACTUAL_NOTE,
        'aggregates': aggregates,
        'rows': rows,
        'factual_judge_trace': {
            'metric': 'FactualCorrectness',
            'rows': traces,
        },
    }


def write_score_bundle(out, scores):
    """写出分数，并把事实正确性的计数痕迹放在旁边。痕迹里没有陈述原文。"""
    trace = scores.get('factual_judge_trace') or {
        'metric': 'FactualCorrectness',
        'rows': [],
    }
    body = {key: value for key, value in scores.items() if key != 'factual_judge_trace'}
    write_json(out / 'ragas_scores.json', body)
    write_csv(out / 'ragas_scores.csv', body['rows'])
    write_json(out / 'factual_judge_trace.json', trace)


def score_from(args, out):
    """只对已有记录打分。不调用检索和生成，也不改写 records.json / traces.json。"""
    if args.retrieve_only:
        raise ValueError('--score-from 只做打分，不能与 --retrieve-only 同时使用。')
    if str(args.project_id or '').strip():
        raise ValueError('--score-from 不检索，不能同时覆盖 --project-id。')
    require_api_key()
    records = load_score_records(args.score_from)
    print(format_judge_log(suffix='根据已有记录打分，不检索、不生成'))
    scores = score_records(records)
    write_score_bundle(out, scores)
    print(f'已根据已有记录打分，未改写 records.json 或 traces.json。结果已写入：{out}')


def main(argv=None):
    # 裁判密钥在构造检索管道之前就检查。仓库 .env 必须先读进来，且不覆盖进程环境。
    load_eval_env()
    args = parse_args(argv)
    out = Path(args.out)
    if args.score_from:
        score_from(args, out)
        return
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
    write_json(out / 'records.json', records)
    write_json(out / 'traces.json', traces)
    if args.retrieve_only:
        print(f'已按 --retrieve-only 写出检索记录，未调用 RAGAS。输出目录：{out}')
        return
    print(format_judge_log())
    scores = score_records(records)
    write_score_bundle(out, scores)
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
