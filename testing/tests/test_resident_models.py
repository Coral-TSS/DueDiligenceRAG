"""逐模型真实小样本；只使用合成输入，不读取客户卷宗。"""
import os
from dataclasses import replace
from pathlib import Path
import pytest

from dd_agent.config import Settings
from dd_agent.model_runtime import ModelRuntime, WorkerSpec
from dd_agent.retrieval import LocalModels


def test_real_embedding_resident_matches_oneshot_and_loads_once(tmp_path):
    if os.getenv('DDA_TEST_LOCAL_MODELS')!='1':pytest.skip('Enable local model tests')
    settings=Settings(data_dir=tmp_path)
    texts=['注册资本为2000万元。']
    expected=LocalModels(replace(settings,model_runtime_enabled=False)).embed(texts)[0]
    script=Path(__file__).resolve().parents[2]/'src/dd_agent/workers/resident_worker.py'
    spec=WorkerSpec(settings.retrieval_python,script,'embedding-test',
        dict(kind='embedding',embedding=str(settings.embedding_dir),threads=4))
    broker=ModelRuntime(tmp_path)
    try:
        a=list(broker.infer(spec,'embed',dict(texts=texts),timeout=60))[0]['vectors'][0]
        pid=broker.snapshot()['pid']
        b=list(broker.infer(spec,'embed',dict(texts=texts),timeout=60))[0]['vectors'][0]
        assert broker.snapshot()['pid']==pid and broker.snapshot()['loads']==1
        assert len(a)==1024 and max(abs(x-y) for x,y in zip(a,expected,strict=True))<1e-6
        assert a==b
    finally:broker.close()


@pytest.mark.parametrize('kind',['reranker','ocr','vision'])
def test_real_resident_models_preserve_results(tmp_path,kind):
    if os.getenv('DDA_TEST_LOCAL_MODELS')!='1':pytest.skip('Enable local model tests')
    import subprocess
    from dd_agent.reading import worker_records
    settings=Settings(data_dir=tmp_path)
    if kind=='reranker':
        python=settings.retrieval_python
        config=dict(kind=kind,reranker=str(settings.reranker_dir),threads=4)
        payload=dict(query='注册资本是多少',texts=['注册资本为2000万元。'])
        expected=LocalModels(replace(settings,model_runtime_enabled=False)).rerank(payload['query'],payload['texts'])
        operation='rerank'
    else:
        image=tmp_path/'sample.png'
        code="from PIL import Image,ImageDraw,ImageFont; import sys; im=Image.new('RGB',(640,120),'white'); ImageDraw.Draw(im).text((20,30),'注册资本为2000万元',font=ImageFont.truetype('C:/Windows/Fonts/msyh.ttc',40),fill='black'); im.save(sys.argv[1])"
        subprocess.run([str(settings.vision_python),'-c',code,str(image)],check=True,timeout=30)
        operation='recognize'
        if kind=='ocr':
            python=settings.ocr_python
            config=dict(kind=kind,models=str(settings.ocr_models_dir),threads=4)
            payload=dict(models=str(settings.ocr_models_dir),pages=[dict(page_number=1,image_path=str(image))])
            expected=list(worker_records(python,'ocr_worker',payload,tmp_path/'oneshot'))[0]['payload']['rec_texts']
        else:
            python=settings.vision_python
            config=dict(kind=kind,models=str(settings.vision_models_dir),threads=4,max_new_tokens=32)
            payload=dict(image=str(image),bounds=[0,0,1,1],models=str(settings.vision_models_dir),max_new_tokens=32)
            expected=list(worker_records(python,'vision_worker',payload,tmp_path/'oneshot'))[0]
    script=Path(__file__).resolve().parents[2]/'src/dd_agent/workers/resident_worker.py'
    spec=WorkerSpec(python,script,kind+'-test',config)
    broker=ModelRuntime(tmp_path)
    try:
        a=list(broker.infer(spec,operation,payload,timeout=300))[0]
        pid=broker.snapshot()['pid']
        b=list(broker.infer(spec,operation,payload,timeout=300))[0]
        assert pid==broker.snapshot()['pid'] and broker.snapshot()['loads']==1
        if kind=='reranker':
            assert a['scores']==b['scores']
            assert max(abs(x-y) for x,y in zip(expected,a['scores'],strict=True))<1e-6
        elif kind=='ocr':assert a['payload']['rec_texts']==b['payload']['rec_texts']==expected
        else:
            for key in ('text','model_version','image_sha256','crop_box','truncated'):
                assert a[key]==b[key]==expected[key]
    finally:broker.close()
