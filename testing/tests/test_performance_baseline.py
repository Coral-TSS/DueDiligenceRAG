"""Repeatable native / optional real-model baseline, always in an isolated DB."""
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

import pytest

from dd_agent.config import Settings
from dd_agent.control import ProcessingPaused
from dd_agent.db import Database
from dd_agent.performance import capture
from dd_agent.pipeline import ProcessingPipeline
from dd_agent.retrieval import EvidenceService
from dd_agent.storage import Storage
from test_reading import make_pdf
from test_retrieval import TinyModels
from test_workspace import client


def test_small_repeatable_performance_baseline(tmp_path, database_url):
    real = os.environ.get('DDA_TEST_PERFORMANCE_MODELS') == '1'
    output = Path(os.environ.get('DDA_TEST_RUN_DIR', tmp_path)) / 'performance-baseline'
    output.mkdir(parents=True, exist_ok=True)
    settings = Settings(database_url=database_url, data_dir=tmp_path/'data')
    runtimes = {}
    for name, python, packages in (
        ('pdf', settings.pdf_python, ['pypdfium2', 'pillow']),
        ('ocr', settings.ocr_python, ['paddleocr', 'paddlepaddle']),
        ('retrieval', settings.retrieval_python, ['torch', 'transformers']),
        ('vision', settings.vision_python, ['torch', 'transformers'])):
        # paddlepaddle-gpu 的发行名不是 paddlepaddle，两种都要能读到版本。
        code = (
            'import importlib.metadata as m,json\n'
            'names=' + repr(packages) + '\n'
            'found={}\n'
            'for name in names:\n'
            '    options=(name,"paddlepaddle-gpu") if name=="paddlepaddle" else (name,)\n'
            '    for candidate in options:\n'
            '        try:\n'
            '            found[name]=m.version(candidate)\n'
            '            break\n'
            '        except m.PackageNotFoundError:\n'
            '            continue\n'
            '    else:\n'
            '        raise SystemExit("missing "+name)\n'
            'print(json.dumps(found))\n'
        )
        response = subprocess.run([str(python), '-c', code], capture_output=True, text=True, check=True, timeout=30)
        runtimes[name] = json.loads(response.stdout)
    cpu_name = platform.processor()
    if os.name == 'nt':
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'HARDWARE\DESCRIPTION\System\CentralProcessor\0') as key:
            cpu_name = winreg.QueryValueEx(key, 'ProcessorNameString')[0].strip()
    source = output/'synthetic.pdf'
    if real:
        helper = Path(__file__).resolve().parents[1]/'scripts/performance/make_sample.py'
        subprocess.run([str(settings.pdf_python), str(helper), str(source)], check=True, timeout=60)
    else:
        make_pdf(source, ['Company registered capital is 2000 and establishment date is 2014.',
                          'Company address is in Jilin province and history records are available.'])
    body = source.read_bytes()
    db = Database(database_url); db.migrate()
    project = db.create_project('合成性能基线')['id']
    stored = Storage(settings.data_dir, 1024*1024).save(project, 'synthetic.pdf', io.BytesIO(body))
    doc, _ = db.register(project, stored)
    def pipeline():
        models = None if real else TinyModels()
        return ProcessingPipeline(settings, evidence=EvidenceService(settings, models=models))
    def run(name, operation):
        with capture(output/name, name, sample_memory=True):
            result = operation()
        records = list((output/name).glob('*.json'))
        assert len(records) == 1
        return result, json.loads(records[0].read_text(encoding='utf-8'))
    first, cold = run('first', lambda: pipeline().run(project, doc['id'], [1, 2]))
    assert first['reading']['successful_pages'] == 2
    assert first['index']['indexed_pages'] == [1, 2]
    assert 0 <= cold['milestones']['index.first_searchable'] <= cold['wall_seconds']
    again, reused = run('reused', lambda: pipeline().run(project, doc['id'], [1, 2]))
    assert again['reading']['skipped_pages'] == [1, 2]
    assert again['index']['skipped_pages'] == [1, 2]
    assert reused['counters']['pages.read.reused'] == 2
    assert reused['counters'].get('ocr.page.attempts', 0) == 0
    assert reused['counters'].get('model.embedding.loads', 0) == 0
    # 在原生第 1 页保存后中断；新服务对象继续，只补第 2 页。
    recovery_project = db.create_project('合成恢复基线')['id']
    recovery_stored = Storage(settings.data_dir, 1024*1024).save(recovery_project, 'synthetic.pdf', io.BytesIO(body))
    recovery_doc, _ = db.register(recovery_project, recovery_stored)
    def pause(stage, number):
        if stage == 'reading' and number == 1 and db.reading(recovery_doc['id'])['successful_pages'] == 1:
            raise ProcessingPaused()
    with pytest.raises(ProcessingPaused):
        run('paused', lambda: pipeline().run(recovery_project, recovery_doc['id'], [1, 2], checkpoint=pause))
    resumed_result, resumed = run('resumed', lambda: pipeline().run(recovery_project, recovery_doc['id'], [1, 2]))
    assert resumed_result['reading']['skipped_pages'] == [1]
    assert resumed_result['index']['indexed_pages'] == [1, 2]
    _, search = run('search_first', lambda: pipeline().evidence.search(project, '注册资本'))
    _, repeated_search = run('search_repeat', lambda: pipeline().evidence.search(project, '注册资本'))
    from dd_agent.app import create_app
    with client(create_app(settings)) as c:
        def read_pages():
            response = c.get(f'/api/projects/{project}/documents/{doc["id"]}/pages')
            assert response.status_code == 200
            return response
        _, http = run('page_api', read_pages)
    result = dict(mode='real_local_models' if real else 'native_pdf_real_sql_synthetic_vectors',
                  sample_sha256=hashlib.sha256(body).hexdigest(), sample_pages=2,
                  python=platform.python_version(), os=platform.platform(), cpu_threads=os.cpu_count(),
                  cpu_name=cpu_name, runtimes=runtimes,
                  configuration=dict(render_scale=settings.pdf_render_scale, vision_regions=settings.vision_regions_per_page,
                      embedding=settings.embedding_dir.name, reranker=settings.reranker_dir.name,
                      vision=settings.vision_models_dir.name, device='cpu', dtype='float32', model_threads=4,
                      reading_version=first['reading']['pipeline_version'], index_version=first['index']['index_version']),
                  process_notes='首次处理不清空 OS 缓存；reused 为成果复用，不代表模型常驻。内存为 100ms 主进程及登记 worker RSS 采样。',
                  counters_expected=dict(first_ocr_pages=1 if real else 0, reused_ocr_pages=0),
                  traces=dict(first=cold, reused=reused, resumed=resumed,
                              search_first=search, search_repeat=repeated_search, page_api=http))
    if real:
        assert cold['counters']['ocr.page.attempts'] == 1
        assert cold['counters']['model.ocr.loads'] == 1
        assert cold['counters']['model.embedding.loads'] == 1
        assert cold['counters'].get('model.vision.loads', 0) >= 1
        expected_bytes = sum(p.stat().st_size for p in settings.vision_models_dir.iterdir()
                             if p.suffix in {'.safetensors', '.json'})
        assert cold['counters']['fingerprint.vision.worker.bytes'] >= expected_bytes > 0
    (output/'summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Performance baseline:', output)
