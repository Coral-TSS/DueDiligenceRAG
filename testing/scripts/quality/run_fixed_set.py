"""检查固定集清单并写出分开的质量报告。不调用模型，不连接数据库，不读取原件。"""
import argparse
import json
from pathlib import Path


FORMAT = 'rg04-manifest-v1'
FIELD_FORMAT = 'rg04-fields-v1'
KINDS = {'printed', 'handwritten', 'table', 'seal', 'mixed'}
CASES = {'exact', 'synonym', 'no_answer', 'unknown', 'cross_page', 'old_version', 'cross_project'}
BARS = {
    'readable_fields_at_least': 60,
    'holdout_strict_rate': 0.95,
    'handwriting_gain': 0.10,
    'handwriting_floor_when_baseline_at_least_0_90': 0.95,
    'printed_not_worse_than_baseline': True,
    'false_adoptions': 0,
    'source_consistency': 1.0,
}


def main():
    parser = argparse.ArgumentParser(description='固定集清单与分项质量报告')
    parser.add_argument('--fields', type=Path, required=True)
    parser.add_argument('--development', type=Path, required=True)
    parser.add_argument('--holdout', type=Path, required=True)
    parser.add_argument('--predictions', type=Path)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    try:
        fields = _fields(args.fields)
        development = _manifest(args.development, 'development', fields)
        holdout = _manifest(args.holdout, 'holdout', fields)
        _disjoint(development, holdout)
        predictions = _predictions(args.predictions, development, holdout, fields) if args.predictions else None
        baseline = json.loads(args.baseline.read_text(encoding='utf-8')) if args.baseline else None
    except ValueError as exc:
        print(str(exc))
        return 2
    report = _report(fields, development, holdout, predictions, baseline)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': report['status'], 'accepted': report['accepted'], 'out': str(args.out)}, ensure_ascii=False))
    return 0


def _fields(path):
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('format') != FIELD_FORMAT or not isinstance(data.get('fields'), list):
        raise ValueError('字段模板格式无效。')
    if len(data['fields']) < 60:
        raise ValueError('字段槽位少于 60 个。')
    seen = set()
    for row in data['fields']:
        if not isinstance(row, dict) or row.get('field_id') in seen or not row.get('name') or not row.get('group'):
            raise ValueError('字段槽位缺少标识、名称或分组，或标识重复。')
        if row.get('value_kind') not in {'text', 'identifier', 'date', 'amount', 'ratio', 'enum'}:
            raise ValueError('字段值类型无效。')
        seen.add(row['field_id'])
    return data


def _manifest(path, expected, fields):
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('format') != FORMAT or data.get('set') != expected:
        raise ValueError(f'{expected} 清单格式或集合名称无效。')
    if data.get('customer_data') is True:
        raise ValueError('清单声明含客户数据。不要把客户材料放进仓库或数据库；本机运行时请使用不入库的副本，并去掉该标记。')
    known = {row['field_id'] for row in fields['fields']}
    items = data.get('items')
    if not isinstance(items, list) or not items:
        raise ValueError(f'{expected} 清单没有条目。')
    seen = set()
    for item in items:
        if item.get('item_id') in seen or not item.get('content_family'):
            raise ValueError('清单条目缺少标识或内容族，或标识重复。')
        if item.get('kind') not in KINDS:
            raise ValueError('清单条目的版面种类无效。')
        if any(case not in CASES for case in item.get('cases') or []):
            raise ValueError('清单情形标签无效。')
        if any(field_id not in known for field_id in item.get('field_ids') or []):
            raise ValueError('清单引用了不存在的字段。')
        for banned in ('path', 'bytes', 'text', 'answer', 'gold', 'expected'):
            if item.get(banned):
                raise ValueError('清单不能携带原件路径、正文或参考答案。答案放在本机预测文件里。')
        seen.add(item['item_id'])
    return data


def _disjoint(development, holdout):
    dev_family = {item['content_family'] for item in development['items']}
    hold_family = {item['content_family'] for item in holdout['items']}
    shared = dev_family & hold_family
    if shared:
        raise ValueError('开发集与保留集共用了内容族：' + '、'.join(sorted(shared)))
    shared_ids = {item['item_id'] for item in development['items']} & {item['item_id'] for item in holdout['items']}
    if shared_ids:
        raise ValueError('开发集与保留集共用了条目标识。')


def _predictions(path, development, holdout, fields):
    items = {item['item_id']: item for item in development['items'] + holdout['items']}
    known = {row['field_id'] for row in fields['fields']}
    rows = []
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get('item_id') not in items or row.get('field_id') not in known:
            raise ValueError('预测行的条目或字段不在清单里。')
        rows.append(row)
    if not rows:
        raise ValueError('预测文件是空的。')
    return rows


def _report(fields, development, holdout, predictions, baseline):
    sections = {
        'ocr': _empty_section('未运行真实模型，没有 OCR 字段结果。'),
        'recall': _empty_section('未运行检索，没有候选召回结果。重排数字不能写成召回提高。'),
        'rerank': _empty_section('未运行重排。这里只记录重排后的命中，不回写召回。'),
        'source': _empty_section('未核来源。来源一致才允许当成通过，缺结果时不填 100%。'),
        'unknown': _empty_section('未统计未知。不能靠把字段都标成未知来得到高准确率。'),
    }
    status = 'not_run'
    gaps = ['评测集和真实模型尚未在本机运行。']
    exploratory = True
    if predictions is not None:
        status = 'scored'
        gaps = []
        scored = [row for row in predictions if row.get('gold') is not None and row.get('unknown') is not True]
        matches = [row for row in scored if row.get('ocr_text') == row.get('gold')]
        sections['ocr'] = _rate('ocr', len(matches), len(scored), '严格一致只比较有参考答案、且未标为看不清的字段。')
        recalled = [row for row in predictions if row.get('recalled_at_12') is not None]
        sections['recall'] = _rate('recall_at_12', sum(row.get('recalled_at_12') is True for row in recalled), len(recalled),
                                   '候选召回。不能用重排命中填这个数。')
        reranked = [row for row in predictions if row.get('reranked_at_5') is not None]
        sections['rerank'] = _rate('recall_at_5', sum(row.get('reranked_at_5') is True for row in reranked), len(reranked),
                                   '重排后的命中。它不提高候选召回。')
        sourced = [row for row in predictions if row.get('source_match') is not None]
        sections['source'] = _rate('consistent', sum(row.get('source_match') is True for row in sourced), len(sourced),
                                   '来源必须与当前项目和当前版本一致。')
        unknown_rows = [row for row in predictions if row.get('unknown') is True]
        false_adopted = [row for row in predictions if row.get('auto_adopted') is True and row.get('adopted_correct') is not True]
        sections['unknown'] = {
            'compared': len(predictions),
            'unknown': len(unknown_rows),
            'unknown_rate': None if not predictions else round(len(unknown_rows) / len(predictions), 4),
            'false_adopted': len(false_adopted),
            'gap': '未知单列。自动采用里的错误不能靠提高未知比例消掉。',
        }
        if len(scored) < 60:
            gaps.append(f'有参考答案的可判读字段只有 {len(scored)} 个，少于 60。')
        handwriting = [row for row in scored if row.get('kind') == 'handwritten' or _kind(row, development, holdout) == 'handwritten']
        if not handwriting:
            gaps.append('手写类没有可评分样本，不能计算相对基线的提升。')
        elif not baseline or baseline.get('handwriting_strict_rate') is None:
            gaps.append('手写类基线未提供，不能计算提升百分点。')
        if sections['source']['rate'] != 1:
            gaps.append('来源一致性没有达到 100%。')
        if false_adopted:
            gaps.append(f'自动采用中有 {len(false_adopted)} 条已知错误。')
        if sections['ocr']['rate'] is None or sections['ocr']['rate'] < BARS['holdout_strict_rate']:
            gaps.append('严格一致率未达到 95% 的门槛，或还没有可计算的比率。')
        exploratory = len(scored) < 60
        if not gaps:
            gaps.append('已达到当前脚本里的门槛。这仍不是客户整卷验收。')
    accepted = status == 'scored' and not exploratory and len(gaps) == 1 and gaps[0].startswith('已达到')
    return {
        'format': 'rg04-report-v1',
        'status': status,
        'accepted': accepted,
        'models_invoked': False,
        'customer_data_loaded': False,
        'exploratory': exploratory if predictions is not None else True,
        'field_count': len(fields['fields']),
        'development_items': len(development['items']),
        'holdout_items': len(holdout['items']),
        'cases_present': sorted({case for item in development['items'] + holdout['items'] for case in item.get('cases') or []}),
        'thresholds': BARS,
        'sections': sections,
        'gaps': gaps,
        'note': '阈值是门槛，不是本次得分。mock 和单元测试通过都不表示 OCR 或检索质量达标。',
    }


def _empty_section(gap):
    return {'compared': 0, 'hits': None, 'rate': None, 'gap': gap}


def _rate(name, hits, compared, gap):
    return {'metric': name, 'compared': compared, 'hits': hits, 'rate': None if not compared else round(hits / compared, 4), 'gap': gap}


def _kind(row, development, holdout):
    for item in development['items'] + holdout['items']:
        if item['item_id'] == row.get('item_id'):
            return item.get('kind')
    return None


if __name__ == '__main__':
    raise SystemExit(main())
