"""Hand checked original text, source offsets and resumable window boundaries."""
import importlib
import json
import pytest


def api():
    try:return importlib.import_module('dd_agent.assembly')
    except ModuleNotFoundError:pytest.fail('RG-06a assembly module is missing')


def page(n,text,**kwargs):
    return api().PageSnapshot('project-a','document-a',n,f'rev-{n}',text,**kwargs)


def check_sources(parents,pages):
    originals={p.page_number:p for p in pages}
    for parent in parents:
        covered=set()
        for span in parent['spans']:
            original=originals[span['page_number']]
            assert span['source_revision']==original.source_revision
            assert parent['text'][span['derived_start']:span['derived_end']]==original.text[span['start']:span['end']]
            covered.update(range(span['derived_start'],span['derived_end']))
        for separator in parent['separators']:
            assert parent['text'][separator['start']:separator['end']]==separator['text']
            covered.update(range(separator['start'],separator['end']))
        assert covered==set(range(len(parent['text'])))


def test_page_10_11_continuation_survives_serialized_checkpoint():
    a,b=page(10,'公司应提供下列资料，'),page(11,'并说明注册资本的变化。')
    first=api().assemble([a])
    assert first['parents']==[]
    restored=json.loads(json.dumps(first['state'],ensure_ascii=False))
    result=api().assemble([b],state=restored,final=True)
    assert [p['text'] for p in result['parents']]==['公司应提供下列资料，并说明注册资本的变化。']
    assert [s['page_number'] for s in result['parents'][0]['spans']]==[10,11]
    assert result['state']['tail'] is None and result['state']['closed']
    check_sources(result['parents'],[a,b])
    assert result==api().assemble([a,b],final=True)


@pytest.mark.parametrize('text,options,reason',[
    ('第十二条 注册资本',{},'heading'),
    ('营业执照\n统一社会信用代码',{},'form'),
    ('并说明下列字段\n股东\t出资',{},'table_uncertain'),
    ('并说明情况。',{'quality':'uncertain'},'quality_uncertain'),
    ('公司住所位于吉林。',{},'page_boundary'),
])
def test_uncertain_pages_and_new_sections_are_not_joined(text,options,reason):
    a,b=page(1,'公司应提供相关材料，'),page(2,text,**options)
    parents=api().assemble([a,b],final=True)['parents']
    assert parents[0]['text']==a.text and parents[0]['boundary']==reason
    assert any(d['page_number']==2 for d in parents[0]['dependencies'])
    assert all(len({s['page_number'] for s in p['spans']})==1 for p in parents)
    check_sources(parents,[a,b])


@pytest.mark.parametrize('status',['missing','failed','pending'])
def test_gap_cannot_be_skipped_and_is_a_boundary_dependency(status):
    a,g,b=page(1,'相关资料，'),page(2,'',readiness=status),page(3,'并说明原因。')
    parents=api().assemble([a,g,b],final=True)['parents']
    assert [p['text'] for p in parents]==[a.text,b.text]
    assert any(d['page_number']==2 and d['readiness']==status for d in parents[0]['dependencies'])
    assert not any(s['page_number']==2 for p in parents for s in p['spans'])
    reverse=api().reverse_dependencies(parents)
    assert parents[0]['id'] in reverse[('document-a',2)]


def test_source_spans_distinguish_inserted_separator_and_preserve_unicode():
    p=page(1,'  公司😀的股东。\n\n  注册资本2000万元。  ')
    parents=api().assemble([p],final=True)['parents']
    assert parents[0]['text']=='公司😀的股东。\n\n注册资本2000万元。'
    assert parents[0]['separators']==[dict(start=7,end=9,text='\n\n')]
    check_sources(parents,[p])


def test_long_clause_has_bounded_linked_blocks_without_losing_characters():
    original='甲'*9501
    p=page(1,original)
    parents=api().assemble([p],final=True)['parents']
    assert ''.join(v['text'] for v in parents)==original
    assert all(0<len(v['text'])<=4000 for v in parents)
    assert parents[1]['continuation_from']==parents[0]['id']
    assert parents[0]['boundary']=='character_limit'
    check_sources(parents,[p])


def test_continuous_clause_never_grows_beyond_three_pages():
    pages=[page(n,('正文，' if n==1 else '并补充，')) for n in range(1,6)]
    parents=api().assemble(pages,final=True)['parents']
    assert [len({s['page_number'] for s in p['spans']}) for p in parents]==[3,2]
    assert parents[0]['boundary']=='page_limit'
    assert parents[1]['continuation_from']==parents[0]['id']


def test_input_and_state_are_bounded_scoped_and_versioned():
    m=api();p=page(1,'原文')
    state=m.assemble([p])['state']
    with pytest.raises(ValueError):m.assemble([page(3,'跳页')],state=state)
    with pytest.raises(ValueError):m.assemble([m.PageSnapshot('other','document-a',2,'r','跨项目')],state=state)
    with pytest.raises(ValueError):m.assemble([page(n,'字') for n in range(1,22)])
    with pytest.raises(ValueError):m.assemble([page(1,'字'*100001)])
    state['version']='old'
    with pytest.raises(ValueError):m.assemble([page(2,'下一页')],state=state)


def test_revision_changes_derived_identity_and_boundary_dependency():
    a,b=page(1,'第一段，'),page(2,'第二页。')
    old=api().assemble([a,b],final=True)['parents']
    changed=api().PageSnapshot('project-a','document-a',2,'new-revision',b.text)
    new=api().assemble([a,changed],final=True)['parents']
    assert old[0]['text']==new[0]['text'] and old[0]['id']!=new[0]['id']


def test_empty_tail_can_be_finalized_but_closed_stream_cannot_append():
    result=api().assemble([page(1,'')],final=True)
    assert result['parents']==[]
    with pytest.raises(ValueError):api().assemble([page(2,'新内容')],state=result['state'])


def test_form_page_cannot_be_used_as_previous_narrative_continuation():
    a,b=page(1,'营业执照\n经营范围：销售，'),page(2,'并说明出资来源。')
    parents=api().assemble([a,b],final=True)['parents']
    assert len(parents)==2 and parents[0]['boundary']=='form'


def test_ten_page_window_produces_identical_blocks_to_one_continuous_input():
    pages=[page(n,f'完整第{n}页。') for n in range(1,10)]+[page(10,'应提供材料，'),page(11,'并说明其来源。')]
    first=api().assemble(pages[:10])
    second=api().assemble(pages[10:],state=first['state'],final=True)
    whole=api().assemble(pages,final=True)
    assert first['parents']+second['parents']==whole['parents']
    assert second['state']==whole['state']


def test_form_starting_inside_page_also_blocks_next_page_join():
    a,b=page(1,'附页说明。\n\n营业执照\n经营范围：销售，'),page(2,'并说明出资来源。')
    parents=api().assemble([a,b],final=True)['parents']
    assert [p['text'] for p in parents]==['附页说明。','营业执照\n经营范围：销售，','并说明出资来源。']
