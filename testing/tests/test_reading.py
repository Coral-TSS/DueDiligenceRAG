import importlib
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from test_workspace import application, client, project, upload


def reading_module():
    try:
        return importlib.import_module('dd_agent.reading')
    except ModuleNotFoundError:
        pytest.fail('PDF preflight and resumable page reading are not implemented yet')


def make_pdf(path, pages):
    """Small, valid uncompressed test PDF; blank page tests the OCR route."""
    objects = [b'', b'']
    kids=[]
    for text in pages:
        page_id=len(objects)+1
        stream_id=page_id+1
        kids.append(f'{page_id} 0 R')
        objects.append(f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >> /Contents {stream_id} 0 R >>'.encode())
        content=f'BT /F1 12 Tf 50 750 Td ({text}) Tj ET'.encode() if text else b''
        objects.append(b'<< /Length '+str(len(content)).encode()+b' >>\nstream\n'+content+b'\nendstream')
    objects[0]=b'<< /Type /Catalog /Pages 2 0 R >>'
    objects[1]=f'<< /Type /Pages /Count {len(pages)} /Kids [{" ".join(kids)}] >>'.encode()
    data=b'%PDF-1.4\n'; offsets=[0]
    for i,obj in enumerate(objects,1):
        offsets.append(len(data)); data+=f'{i} 0 obj\n'.encode()+obj+b'\nendobj\n'
    xref=len(data)
    data+=f'xref\n0 {len(objects)+1}\n0000000000 65535 f \n'.encode()
    data+=b''.join(f'{o:010d} 00000 n \n'.encode() for o in offsets[1:])
    data+=f'trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF'.encode()
    path.write_bytes(data)
    return data


def test_page_ranges_are_unique_bounded_and_one_based():
    parse=reading_module().parse_pages
    assert parse('3,1-3,5', total=8) == [1,2,3,5]
    for text in ('', '0', '9', '3-1', '-2', '1-21', 'a', '1,,2'):
        with pytest.raises(ValueError):
            parse(text,total=8 if text!='1-21' else 30)


def test_preflight_distinguishes_readable_text_from_ocr_needed_pages(tmp_path):
    m=reading_module()
    source=tmp_path/'mixed.pdf'
    make_pdf(source,['Registered capital is 2000 and establishment date is 2014-01-26.',''])
    profile=m.PdfWorker().inspect(source)
    assert profile['page_count']==2
    assert profile['pages'][0]['route']=='text'
    assert profile['pages'][1]['route']=='ocr'


def test_corrupt_pdf_has_actionable_failure(tmp_path):
    m=reading_module()
    source=tmp_path/'broken.pdf'; source.write_bytes(b'%PDF-invalid')
    with pytest.raises(m.ReadingError,match='PDF'):
        m.PdfWorker().inspect(source)


def test_partial_chinese_headings_do_not_make_garbled_text_layer_readable():
    from dd_agent.workers.pdf_worker import text_route
    assert text_route('注册资本出资额比例股东名称登记情况'+'00.000^∞Φ丬'*100)=='ocr'
    assert text_route('公司登记注册资本2000万元，设立日期2014年1月26日。')=='text'


def test_partial_reading_survives_restart_and_retry_does_not_duplicate(tmp_path,database_url):
    m=reading_module()
    from dd_agent.config import Settings
    source=tmp_path/'source.pdf'
    body=make_pdf(source,['Registered capital is 2000 and establishment date is 2014-01-26.','Second historical event occurred in 2018 and capital became 5000.'])
    app=application(tmp_path/'data',database_url,migrate=True,limit=10000)
    with client(app) as c:
        p=project(c)
        doc=upload(c,p,body=body).json()['document']['id']
        settings=Settings(database_url=database_url,data_dir=tmp_path/'data',max_upload_bytes=10000)
        reader=m.Reader(settings)
        first=reader.read(p,doc,[1])
        assert first['status']=='partial'
        assert first['successful_pages']==1
        listing=c.get(f'/api/projects/{p}/documents').json()[0]
        assert listing['reading']['status']=='partial'
        assert listing['reading']['successful_pages']==1
        second=m.Reader(settings).read(p,doc,[1,2])
        assert second['status']=='complete'
        assert second['successful_pages']==2
        assert second['skipped_pages']==[1]
        again=m.Reader(settings).read(p,doc,[1,2])
        assert again['skipped_pages']==[1,2]
    with client(application(tmp_path/'data',database_url,limit=10000)) as c:
        summary=c.get(f'/api/projects/{p}/documents/{doc}/reading')
        assert summary.status_code==200
        assert summary.json()['successful_pages']==2
        pages=c.get(f'/api/projects/{p}/documents/{doc}/pages').json()
        assert len(pages)==2
        assert 'capital is 2000' in pages[0]['text']
        assert pages[0]['page_number']==1
        assert pages[0]['method']=='text'
        assert c.get(f'/api/projects/{p}/documents/{doc}/pages/1/image').status_code==200
        other=project(c,'其他项目')
        assert c.get(f'/api/projects/{other}/documents/{doc}/pages').status_code==404
        assert c.get(f'/api/projects/{other}/documents/{doc}/pages/1/image').status_code==404


def test_missing_ocr_weights_records_page_failure_without_fabricating_text(tmp_path,database_url):
    m=reading_module()
    from dd_agent.config import Settings
    source=tmp_path/'scan.pdf'; body=make_pdf(source,[''])
    app=application(tmp_path/'data',database_url,migrate=True,limit=10000)
    with client(app) as c:
        p=project(c); doc=upload(c,p,body=body).json()['document']['id']
        settings=Settings(database_url=database_url,data_dir=tmp_path/'data',ocr_models_dir=tmp_path/'absent')
        result=m.Reader(settings).read(p,doc,[1])
        assert result['status']=='failed'
        page=c.get(f'/api/projects/{p}/documents/{doc}/pages').json()[0]
        assert page['status']=='failed'
        assert page['text']==''
        assert page['error_code']=='ocr_models_missing'


def test_ocr_lines_keep_boxes_scores_and_do_not_invent_blank_text():
    normalize=reading_module().normalize_ocr
    payload={'rec_texts':['Registered capital 2000',''], 'rec_scores':[0.92,0.1], 'rec_polys':[[[1,2],[90,2],[90,12],[1,12]],[[0,0],[1,0],[1,1],[0,1]]]}
    result=normalize(payload)
    assert result['text']=='Registered capital 2000'
    assert result['lines']==[{'text':'Registered capital 2000','score':0.92,'box':[[1,2],[90,2],[90,12],[1,12]]}]


@pytest.mark.parametrize('payload',[
    {'rec_texts':['capital 2000'],'rec_scores':[],'rec_polys':[]},
    {'rec_texts':['capital 2000'],'rec_scores':[float('nan')],'rec_polys':[[[0,0],[1,0],[1,1],[0,1]]]},
    {'rec_texts':['capital 2000'],'rec_scores':[1.1],'rec_polys':[[[0,0],[1,0],[1,1],[0,1]]]},
])
def test_malformed_ocr_payload_is_a_reading_failure(payload):
    m=reading_module()
    with pytest.raises(m.ReadingError):m.normalize_ocr(payload)


def test_cli_import_is_persistent_and_deduplicates(tmp_path,database_url):
    import os,subprocess,json,sys
    app=application(tmp_path/'data',database_url,migrate=True,limit=10000)
    with client(app) as c:
        p=project(c)
        source=tmp_path/'cli-source.pdf';make_pdf(source,['Company history includes an establishment event in 2014.'])
        environment={**os.environ,'DDA_DATABASE_URL':database_url,'DDA_DATA_DIR':str(tmp_path/'data'),'PYTHONIOENCODING':'utf-8'}
        command=[sys.executable,'-m','dd_agent','import-file','--project-id',p,'--file',str(source)]
        responses=[subprocess.run(command,capture_output=True,text=True,encoding='utf-8',env=environment,timeout=30) for _ in range(2)]
        assert [r.returncode for r in responses]==[0,0]
        first,second=[json.loads(r.stdout) for r in responses]
        assert first['duplicate'] is False and second['duplicate'] is True
        assert first['document_id']==second['document_id']
        assert len(c.get(f'/api/projects/{p}/documents').json())==1


def test_changed_ocr_weights_do_not_reuse_old_page_results(tmp_path,database_url):
    m=reading_module()
    from dd_agent.config import Settings
    source=tmp_path/'version.pdf';body=make_pdf(source,['The company establishment date is 2014-01-26 and capital is 2000.'])
    models=tmp_path/'models';models.mkdir()
    detector=models/'PP-OCRv5_mobile_det';detector.mkdir()
    weights=detector/'inference.pdiparams';weights.write_bytes(b'weight-version-one')
    with client(application(tmp_path/'data',database_url,migrate=True,limit=10000)) as c:
        p=project(c);doc=upload(c,p,body=body).json()['document']['id']
        settings=Settings(database_url=database_url,data_dir=tmp_path/'data',ocr_models_dir=models)
        first=m.Reader(settings).read(p,doc,[1])
        weights.write_bytes(b'weight-version-two')
        second=m.Reader(settings).read(p,doc,[1])
        assert second['skipped_pages']==[]
        assert first['pipeline_version']!=second['pipeline_version']
