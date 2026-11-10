"""固定集脚本只检查清单和报告格式。不调用模型，也不把样例写进数据库。"""
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'testing/scripts/quality/run_fixed_set.py'
FIELDS = ROOT / 'testing/quality/fixed_set/field-template.json'
DEVELOPMENT = ROOT / 'testing/quality/fixed_set/development.example.json'
HOLDOUT = ROOT / 'testing/quality/fixed_set/holdout.example.json'


def _run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, encoding='utf-8')


def test_template_has_sixty_empty_slots_and_examples_do_not_overlap():
    template = json.loads(FIELDS.read_text(encoding='utf-8'))
    assert len(template['fields']) >= 60
    assert all(row['expected'] is None and row['field_id'] and row['group'] and row['name'] for row in template['fields'])
    assert template['customer_data'] is False
    development = json.loads(DEVELOPMENT.read_text(encoding='utf-8'))
    holdout = json.loads(HOLDOUT.read_text(encoding='utf-8'))
    assert development['customer_data'] is False and holdout['customer_data'] is False
    assert {item['content_family'] for item in development['items']}.isdisjoint(
        {item['content_family'] for item in holdout['items']})
    source = SCRIPT.read_text(encoding='utf-8')
    assert 'psycopg' not in source and 'dd_agent' not in source


def test_report_splits_sections_and_does_not_invent_a_pass(tmp_path):
    out = tmp_path / 'not-run.json'
    dry = _run('--fields', str(FIELDS), '--development', str(DEVELOPMENT), '--holdout', str(HOLDOUT), '--out', str(out))
    assert dry.returncode == 0, dry.stdout + dry.stderr
    report = json.loads(out.read_text(encoding='utf-8'))
    assert report['status'] == 'not_run' and report['accepted'] is False and report['models_invoked'] is False
    assert set(report['sections']) == {'ocr', 'recall', 'rerank', 'source', 'unknown'}
    assert all(section['rate'] is None for section in report['sections'].values())
    assert report['thresholds']['holdout_strict_rate'] == 0.95
    assert report['sections']['ocr']['rate'] != 0.95

    predictions = tmp_path / 'predictions.jsonl'
    predictions.write_text(
        '\n'.join([
            json.dumps({'item_id': 'dev-synthetic-1', 'field_id': 'f001', 'kind': 'printed', 'ocr_text': '甲', 'gold': '甲',
                        'recalled_at_12': True, 'reranked_at_5': False, 'source_match': True, 'unknown': False,
                        'auto_adopted': True, 'adopted_correct': True}, ensure_ascii=False),
            json.dumps({'item_id': 'dev-synthetic-1', 'field_id': 'f012', 'kind': 'printed', 'ocr_text': '错', 'gold': '对',
                        'recalled_at_12': False, 'reranked_at_5': False, 'source_match': False, 'unknown': False,
                        'auto_adopted': True, 'adopted_correct': False}, ensure_ascii=False),
        ]) + '\n',
        encoding='utf-8')
    scored_path = tmp_path / 'scored.json'
    scored = _run('--fields', str(FIELDS), '--development', str(DEVELOPMENT), '--holdout', str(HOLDOUT),
                  '--predictions', str(predictions), '--out', str(scored_path))
    assert scored.returncode == 0, scored.stdout + scored.stderr
    body = json.loads(scored_path.read_text(encoding='utf-8'))
    assert body['accepted'] is False and body['exploratory'] is True
    assert body['sections']['ocr']['rate'] == 0.5
    assert body['sections']['recall']['rate'] == 0.5
    assert body['sections']['rerank']['rate'] == 0.0
    assert body['sections']['recall']['hits'] != body['sections']['rerank']['hits']
    assert body['sections']['source']['rate'] == 0.5
    assert body['sections']['unknown']['false_adopted'] == 1
    assert '0.95' not in json.dumps(body['sections'])
    assert any('少于 60' in gap for gap in body['gaps'])


def test_overlapping_families_are_rejected(tmp_path):
    development = json.loads(DEVELOPMENT.read_text(encoding='utf-8'))
    holdout = json.loads(HOLDOUT.read_text(encoding='utf-8'))
    holdout['items'][0]['content_family'] = development['items'][0]['content_family']
    dev_path = tmp_path / 'development.json'
    hold_path = tmp_path / 'holdout.json'
    dev_path.write_text(json.dumps(development, ensure_ascii=False), encoding='utf-8')
    hold_path.write_text(json.dumps(holdout, ensure_ascii=False), encoding='utf-8')
    out = tmp_path / 'should-not-exist.json'
    result = _run('--fields', str(FIELDS), '--development', str(dev_path), '--holdout', str(hold_path), '--out', str(out))
    assert result.returncode == 2
    assert '内容族' in result.stdout
    assert not out.exists()
    assert 'accepted' not in result.stdout
