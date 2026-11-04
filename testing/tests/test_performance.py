"""Catch missing observations, leaked inputs and metrics that change business behavior."""
import importlib
import json
from pathlib import Path

import pytest


def metrics():
    try:
        return importlib.import_module('dd_agent.performance')
    except ModuleNotFoundError:
        pytest.fail('RG-05a structured performance capture is not implemented')


def report(directory):
    files = list(Path(directory).glob('*.json'))
    assert len(files) == 1
    return json.loads(files[0].read_text(encoding='utf-8'))


def test_nested_spans_keep_counts_and_control_exceptions_without_logging_inputs(tmp_path):
    m = metrics()
    from dd_agent.control import ProcessingPaused
    with pytest.raises(ProcessingPaused):
        with m.capture(tmp_path, 'test'):
            with m.span('reading'):
                m.count('pages.read.reused', 2)
                with m.span('ocr'):
                    raise ProcessingPaused('private-document-and-password')
    value = report(tmp_path)
    assert value['status'] == 'paused'
    assert value['counters']['pages.read.reused'] == 2
    assert value['stages']['ocr']['paused'] == 1
    assert value['stages']['reading']['calls'] == 1
    assert value['wall_seconds'] >= 0
    assert 'private-document-and-password' not in json.dumps(value)


def test_metrics_output_failure_does_not_replace_success_or_original_exception(tmp_path):
    m = metrics()
    blocked = tmp_path / 'file'; blocked.write_text('not a directory')
    with m.capture(blocked, 'test'):
        m.count('pages.read.reused')
    with pytest.raises(ValueError, match='business failure'):
        with m.capture(blocked, 'test'):
            raise ValueError('business failure')


def test_capture_bounds_details_but_keeps_full_stage_totals(tmp_path):
    m = metrics()
    with m.capture(tmp_path, 'test', max_events=2):
        for _ in range(5):
            with m.span('ocr'):
                pass
    value = report(tmp_path)
    assert len(value['events']) == 2
    assert value['dropped_events'] == 3
    assert value['stages']['ocr']['calls'] == 5


def test_worker_metrics_do_not_escape_into_pdf_business_protocol(tmp_path):
    m = metrics()
    from test_reading import make_pdf
    from dd_agent.reading import PdfWorker
    source = tmp_path / 'private-filename.pdf'
    make_pdf(source, ['Company registered capital is 2000.', ''])
    with m.capture(tmp_path / 'metrics', 'test', sample_memory=True):
        result = PdfWorker().inspect(source)
    assert result['page_count'] == 2
    assert len(result['pages']) == 2
    value = report(tmp_path / 'metrics')
    assert value['counters']['pdf.preflight.pages'] == 2
    assert value['counters']['worker.pdf_worker.starts'] == 1
    assert value['stages']['pdf.preflight']['calls'] == 1
    assert value['memory']['scope'] == 'main_and_registered_workers'
    assert value['memory']['sampled_peak_rss_mb'] > 0
    assert 'private-filename' not in json.dumps(value)


def test_real_sql_and_incremental_index_produce_reuse_and_chunk_observations(tmp_path, database_url):
    m = metrics()
    from test_retrieval import setup
    _, p, d, service, _ = setup(tmp_path / 'data', database_url)
    with m.capture(tmp_path / 'metrics', 'test'):
        assert service.index(p, d, [1, 2])['indexed_pages'] == [1, 2]
        assert service.index(p, d, [1, 2])['skipped_pages'] == [1, 2]
        assert service.search(p, '注册资本')['results'][0]['page_number'] == 1
    value = report(tmp_path / 'metrics')
    assert value['counters']['pages.index.reused'] == 2
    assert value['counters']['chunks.created'] == 2
    assert value['counters']['sql.calls'] > 0
    assert value['stages']['index']['calls'] == 2
    assert value['stages']['chunk']['calls'] == 2
    assert value['stages']['search']['calls'] == 1
    assert 0 <= value['milestones']['index.first_searchable'] <= value['wall_seconds']
    assert '注册资本' not in json.dumps(value, ensure_ascii=False)


def test_http_records_actual_response_bytes_without_body_or_url(tmp_path):
    metrics()
    from test_workspace import application, client
    app = application(tmp_path)
    with client(app) as c:
        response = c.get('/api/health?private=secret-value')
    value = report(tmp_path / 'performance')
    assert value['counters']['http.response_bytes'] == len(response.content)
    assert value['counters']['http.status.200'] == 1
    assert 'secret-value' not in json.dumps(value)


def test_worker_nonzero_exit_is_measured_as_failure(tmp_path):
    m = metrics()
    from dd_agent.reading import PdfWorker, ReadingError
    source = tmp_path/'bad.pdf'; source.write_bytes(b'%PDF-invalid')
    with pytest.raises(ReadingError):
        with m.capture(tmp_path/'metrics', 'test'):
            PdfWorker().inspect(source)
    value = report(tmp_path/'metrics')
    assert value['stages']['worker.pdf_worker']['error'] == 1


def test_malformed_worker_observations_cannot_break_business_capture(tmp_path):
    m = metrics()
    with m.capture(tmp_path, 'test'):
        for value in ({'counter': None, 'value': 1}, {'stage': []},
                      {'stage': 'ocr', 'seconds': float('nan'), 'status': 'ok'}):
            m.accept_worker(value)
        m.count('pages.read.reused', 1)
    assert report(tmp_path)['counters']['pages.read.reused'] == 1


def test_job_capture_records_queue_age_and_checkpoints_without_idle_traces(tmp_path, database_url):
    metrics()
    from test_retrieval import setup
    from dd_agent.pipeline import JobManager
    _, p, d, service, _ = setup(tmp_path, database_url)
    class Work:
        def run(self, project_id, document_id, pages, checkpoint=None):
            checkpoint('embedding', 1)
            return {'automatic': {'failed_pages': []}, 'index': {'failed_pages': []}}
    manager = JobManager(service.settings, pipeline_factory=Work)
    manager.enqueue(p, d, wake=False)
    manager.drain(); manager.drain()
    assert manager.latest(p, d)['status'] == 'completed'
    traces = [json.loads(path.read_text(encoding='utf-8')) for path in (tmp_path/'performance').glob('*.json')]
    jobs = [value for value in traces if value['operation'] == 'job']
    assert len(jobs) == 1
    assert jobs[0]['counters']['job.queue_age_seconds'] >= 0
    assert jobs[0]['stages']['job.checkpoint']['calls'] >= 3


def test_busy_model_resource_is_recorded_as_rejected_not_queued(tmp_path):
    m = metrics()
    from dd_agent.retrieval import model_slot, RetrievalError
    with m.capture(tmp_path/'metrics', 'test'):
        with model_slot(tmp_path/'data'):
            with pytest.raises(RetrievalError):
                with model_slot(tmp_path/'data'): pass
    value = report(tmp_path/'metrics')
    assert value['counters']['resource.busy'] == 1
    assert value['stages']['resource.acquire']['error'] == 1


def test_memory_sampler_follows_real_interpreter_behind_venv_launcher(tmp_path):
    m = metrics()
    import subprocess
    import sys
    fixture = Path(__file__).resolve().parents[1]/'scripts/performance/memory_fixture.py'
    with m.capture(tmp_path, 'test', sample_memory=True):
        process = subprocess.Popen([sys.executable, str(fixture)], stdout=subprocess.PIPE, text=True)
        try:
            with m.worker_process(process.pid) as register:
                register(json.loads(process.stdout.readline())['pid'])
                allocated_rss = json.loads(process.stdout.readline())['rss_mb']
                assert process.wait(timeout=10) == 0
        finally:
            if process.poll() is None: process.kill()
            process.wait(); process.stdout.close()
    value = report(tmp_path)
    assert value['memory']['sampled_peak_rss_mb'] >= allocated_rss*.9
