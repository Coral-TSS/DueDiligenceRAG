"""用 TruLens 追踪检索和生成，并计算幻觉三角指标。"""
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
    QueryApp,
    RagPipeline,
    RecordReplay,
    completion_kwargs,
    filter_payload,
    format_judge_log,
    judge_llm_config,
    load_score_records,
    openai_client,
    prepare_run,
    redact_secrets,
    require_api_key,
    write_json,
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='用 TruLens 追踪并评测本机尽调检索。')
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--dataset', help='黄金集 JSON。可以放在仓库外。')
    source.add_argument(
        '--score-from',
        help='已有 records.json。回放片段和答案后再打分，不检索、不生成。',
    )
    parser.add_argument('--out', default=str(ROOT / 'output'), help='结果目录')
    parser.add_argument('--project-id', default='', help='覆盖数据集里的 project_id')
    parser.add_argument('--dashboard', action='store_true', help='评测后打开本机 TruLens dashboard')
    parser.add_argument('--app-version', default='local', help='TruLens 中的应用版本名，用于对比多次运行')
    parser.add_argument(
        '--retrieve-only',
        action='store_true',
        help='只跑检索并写出痕迹，不生成答案，也不调用 TruLens',
    )
    return parser.parse_args(argv)


def sqlite_url(path):
    return 'sqlite:///' + Path(path).resolve().as_posix()


def make_provider(config=None):
    config = require_api_key(config or judge_llm_config())
    from trulens.providers.openai import OpenAI

    options = completion_kwargs(config)
    if options:
        # 把已经关掉思考的客户端交给 TruLens。extra_body 不是 OpenAI 构造参数，
        # 不能放进 provider 的 kwargs，否则会在建客户端时被拒绝。
        client = openai_client(config)
        return OpenAI(model_engine=config['model'], client=client)
    kwargs = {'model_engine': config['model'], 'api_key': config['api_key']}
    if config.get('base_url'):
        kwargs['base_url'] = config['base_url']
    return OpenAI(**kwargs)


def hallucination_metrics(provider):
    import numpy as np
    from trulens.core import Metric, Selector

    return [
        Metric(
            implementation=provider.groundedness_measure_with_cot_reasons_consider_answerability,
            name='Groundedness',
            selectors={
                'source': Selector.select_context(collect_list=True),
                'statement': Selector.select_record_output(),
                'question': Selector.select_record_input(),
            },
        ),
        Metric(
            implementation=provider.relevance_with_cot_reasons,
            name='Answer Relevance',
            selectors={
                'prompt': Selector.select_record_input(),
                'response': Selector.select_record_output(),
            },
        ),
        Metric(
            implementation=provider.context_relevance_with_cot_reasons,
            name='Context Relevance',
            selectors={
                'question': Selector.select_record_input(),
                'context': Selector.select_context(collect_list=False),
            },
            agg=np.mean,
        ),
    ]


def build_instrumented_app(pipeline, project_id):
    from trulens.core.otel.instrument import instrument
    from trulens.otel.semconv.trace import SpanAttributes

    class InstrumentedRag(QueryApp):
        @instrument(
            span_type=SpanAttributes.SpanType.RETRIEVAL,
            attributes={
                SpanAttributes.RETRIEVAL.QUERY_TEXT: 'query',
                SpanAttributes.RETRIEVAL.RETRIEVED_CONTEXTS: 'return',
            },
        )
        def retrieve(self, query: str, filters: dict | None = None) -> list:
            return super().retrieve(query, filters)

        @instrument(span_type=SpanAttributes.SpanType.GENERATION)
        def generate_completion(self, query: str, context_list: list) -> str:
            return super().generate_completion(query, context_list)

        @instrument(
            span_type=SpanAttributes.SpanType.RECORD_ROOT,
            attributes={
                SpanAttributes.RECORD_ROOT.INPUT: 'query',
                SpanAttributes.RECORD_ROOT.OUTPUT: 'return',
            },
        )
        def query(self, query: str, filters: dict | None = None) -> str:
            return super().query(query, filters)

    return InstrumentedRag(pipeline, project_id)


def open_session(database_url):
    from trulens.core import TruSession

    return TruSession(database_url=database_url)


def build_wrapped_app(project_id, app_version, database_url, pipeline=None):
    from trulens.apps.app import TruApp

    provider = make_provider()
    pipeline = pipeline if pipeline is not None else RagPipeline()
    app = build_instrumented_app(pipeline, project_id)
    # TruSession 是单例。必须先带数据库地址创建，TruApp 里再次调用才会沿用这个文件。
    session = open_session(database_url)
    tru_app = TruApp(
        app,
        app_name='DueDiligenceRAG',
        app_version=app_version,
        feedbacks=hallucination_metrics(provider),
    )
    return session, app, tru_app


def json_ready(board):
    if hasattr(board, 'to_dict'):
        return board.to_dict(orient='records')
    return board


def retrieve_only(dataset, project_id, out):
    pipeline = RagPipeline()
    traces = []
    for item in dataset['items']:
        contexts, found = pipeline.retrieve(project_id, item['question'], item)
        traces.append({
            'id': item['id'],
            'question': item['question'],
            'search_status': found.get('status') if isinstance(found, dict) else None,
            'context_count': len(contexts),
            'coverage': found.get('coverage') if isinstance(found, dict) else None,
            'filters': filter_payload(item),
        })
        print(f"{item['id']}: status={traces[-1]['search_status']} contexts={len(contexts)}")
    write_json(out / 'traces.json', traces)
    print(f'已按 --retrieve-only 写出检索记录，未生成答案，也未调用 TruLens。输出目录：{out}')


def write_leaderboard(session, out):
    try:
        board = session.get_leaderboard()
        if board is not None:
            write_json(out / 'trulens_leaderboard.json', json_ready(board))
            print(board)
    except Exception as exc:
        print(f'尚未生成 leaderboard：{redact_secrets(exc)}')


def maybe_dashboard(args, session):
    if args.dashboard:
        from trulens.dashboard import run_dashboard

        run_dashboard(session)


def score_from(args, out):
    """回放已有记录再打分。不检索、不生成，也不改写 records.json / traces.json。"""
    if args.retrieve_only:
        raise ValueError('--score-from 只做打分，不能与 --retrieve-only 同时使用。')
    if str(args.project_id or '').strip():
        raise ValueError('--score-from 不检索，不能同时覆盖 --project-id。')
    require_api_key()
    records = load_score_records(args.score_from)
    replay = RecordReplay(records)
    print(format_judge_log(
        suffix=f'应用版本：{args.app_version}，根据已有记录回放片段和答案，不检索、不生成',
    ))
    out.mkdir(parents=True, exist_ok=True)
    session, app, tru_app = build_wrapped_app(
        'score-from',
        args.app_version,
        sqlite_url(out / 'trulens.sqlite'),
        pipeline=replay,
    )
    answers = []
    with tru_app:
        for record in records:
            answer = app.query(record['user_input'], filters=None)
            preview = answer[:120].replace('\n', ' ')
            print(f"{record['id']}: {preview}")
            answers.append({
                'id': record['id'],
                'question': record['user_input'],
                'answer': answer,
                'context_count': len(app.last_contexts),
                'search_status': (app.last_found or {}).get('status') if isinstance(app.last_found, dict) else None,
                'filters': {},
                'app_version': args.app_version,
                'replayed': True,
            })
    write_json(out / 'trulens_answers.json', answers)
    write_leaderboard(session, out)
    print(f'已根据已有记录打分，未改写 records.json 或 traces.json。结果已写入：{out}')
    maybe_dashboard(args, session)


def main(argv=None):
    args = parse_args(argv)
    out = Path(args.out)
    if args.score_from:
        score_from(args, out)
        return
    dataset, project_id = prepare_run(args.dataset, args.project_id)
    out.mkdir(parents=True, exist_ok=True)
    if args.retrieve_only:
        retrieve_only(dataset, project_id, out)
        return

    require_api_key()
    print(format_judge_log(suffix=f'应用版本：{args.app_version}'))
    session, app, tru_app = build_wrapped_app(
        project_id,
        args.app_version,
        sqlite_url(out / 'trulens.sqlite'),
    )
    answers = []
    with tru_app:
        for item in dataset['items']:
            filters = filter_payload(item)
            answer = app.query(item['question'], filters=filters)
            preview = answer[:120].replace('\n', ' ')
            print(f"{item['id']}: {preview}")
            answers.append({
                'id': item['id'],
                'question': item['question'],
                'answer': answer,
                'context_count': len(app.last_contexts),
                'search_status': (app.last_found or {}).get('status') if isinstance(app.last_found, dict) else None,
                'filters': filters,
                'app_version': args.app_version,
            })
    write_json(out / 'trulens_answers.json', answers)
    write_leaderboard(session, out)
    print(f'结果已写入：{out}')
    maybe_dashboard(args, session)


if __name__ == '__main__':
    try:
        os.chdir(REPO_ROOT)
    except OSError:
        pass
    try:
        main()
    except (ValueError, RuntimeError, FileNotFoundError) as exc:
        raise SystemExit(redact_secrets(exc)) from None
