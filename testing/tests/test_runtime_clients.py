from dataclasses import replace
from pathlib import Path

from dd_agent.config import Settings
from dd_agent.retrieval import LocalModels, RetrievalError
from dd_agent.review import LocalVision
import pytest


def test_clients_use_shared_runtime_and_preserve_operations(tmp_path, monkeypatch):
    from dd_agent import model_runtime as runtime
    calls=[]
    def records(settings,kind,operation,payload,**kwargs):
        calls.append((kind,operation,payload))
        yield {'vectors':[[1.]],'scores':[.8],'text':'合成 OCR'}
    monkeypatch.setattr(runtime,'runtime_records',records)
    settings=replace(Settings(data_dir=tmp_path),model_runtime_enabled=True)
    models=LocalModels(settings)
    assert models.managed
    assert models.embed(['test'])==[[1.]]
    assert models.rerank('question',['text'])==[.8]
    vision=LocalVision(settings)
    assert vision.managed
    assert vision.recognize(image_path=tmp_path/'image.png',bounds=[0,0,1,1],directory=tmp_path)['text']=='合成 OCR'
    from dd_agent.reading import ocr_records
    assert list(ocr_records(settings,{'models':'local','pages':[]},tmp_path))
    assert [(k,o) for k,o,_ in calls]==[('embedding','embed'),('reranker','rerank'),('vision','recognize'),('ocr','recognize')]


def test_runtime_failure_maps_to_existing_model_errors(tmp_path, monkeypatch):
    from dd_agent import model_runtime as runtime
    from dd_agent.review import ReviewError
    from dd_agent.reading import ReadingError, ocr_records
    def records(*args,**kwargs):raise runtime.ModelRuntimeError('test_failure')
    monkeypatch.setattr(runtime,'runtime_records',records)
    settings=replace(Settings(data_dir=tmp_path),model_runtime_enabled=True)
    with pytest.raises(RetrievalError):LocalModels(settings).embed(['text'])
    with pytest.raises(ReviewError):LocalVision(settings).recognize(image_path=Path('a'),bounds=[0,0,1,1],directory=tmp_path)
    with pytest.raises(ReadingError):list(ocr_records(settings,{'pages':[]},tmp_path))


def test_application_shutdown_closes_only_its_model_runtime(tmp_path,monkeypatch):
    from dd_agent import model_runtime as runtime
    from dd_agent.app import create_app
    from test_workspace import client
    calls=[]
    monkeypatch.setattr(runtime,'close_runtime',lambda settings=None:calls.append(settings.data_dir))
    settings=Settings(data_dir=tmp_path)
    with client(create_app(settings)):pass
    assert calls==[tmp_path]


def test_unknown_environment_cannot_reuse_resident_profile(tmp_path,monkeypatch):
    from dd_agent import model_runtime as runtime, cache
    profiles=[]
    monkeypatch.setattr(cache,'venv_runtime_descriptor',lambda *a,**k:None)
    def infer(self,spec,*args,**kwargs):
        profiles.append(spec.profile);yield {'ok':True}
    monkeypatch.setattr(runtime.ModelRuntime,'infer',infer)
    settings=Settings(data_dir=tmp_path,embedding_dir=tmp_path/'missing-model')
    try:
        list(runtime.runtime_records(settings,'embedding','embed',{'texts':['x']}))
        list(runtime.runtime_records(settings,'embedding','embed',{'texts':['x']}))
        assert profiles[0]!=profiles[1]
    finally:runtime.close_runtime(settings)


def test_embedding_stream_releases_admission_before_page_publication(tmp_path,monkeypatch):
    models=LocalModels(Settings(data_dir=tmp_path));active=[];calls=[]
    def records(mode,**payload):
        active.append(True);calls.append(payload)
        try:
            for page in payload['pages']:
                yield {'page_number':page['page_number'],'vectors':[]}
        finally:active.pop()
    monkeypatch.setattr(models,'records',records)
    result=models.embed_pages([{'page_number':1,'texts':[]},{'page_number':2,'texts':[]}])
    try:
        assert next(result)['page_number']==1 and not active
        assert next(result)['page_number']==2 and not active
        assert len(calls)==1 and [page['page_number'] for page in calls[0]['pages']]==[1,2]
        assert calls[0]['token_budget']==2048 and calls[0]['batch_limit']==32
    finally:result.close()


def test_ocr_stream_releases_admission_at_each_saved_page(tmp_path,monkeypatch):
    from dd_agent import model_runtime, reading
    settings=Settings(data_dir=tmp_path,ocr_models_dir=tmp_path/'weights',ocr_page_batch=1)
    for name in reading.OCR_MODEL_NAMES:
        folder=settings.ocr_models_dir/name;folder.mkdir(parents=True)
        (folder/'inference.pdiparams').write_bytes(b'fixture')
    active=[];pages=[]
    def records(settings,kind,operation,payload,**kwargs):
        active.append(True);pages.append(payload['pages'])
        try:yield dict(page_number=payload['pages'][0]['page_number'])
        finally:active.pop()
    monkeypatch.setattr(model_runtime,'runtime_records',records)
    result=reading.ocr_records(settings,dict(pages=[{'page_number':1},{'page_number':2}]),tmp_path)
    try:
        assert next(result)['page_number']==1 and not active
        assert next(result)['page_number']==2 and not active
        assert [len(p) for p in pages]==[1,1]
    finally:result.close()


def test_background_contention_is_deferred_not_a_recognition_failure(tmp_path,monkeypatch):
    from dd_agent import model_runtime, reading
    from dd_agent.model_admission import request_scope
    from dd_agent.control import ProcessingDeferred
    def busy(*a,**k):raise model_runtime.ModelRuntimeError('model_busy')
    monkeypatch.setattr(model_runtime,'runtime_records',busy)
    settings=Settings(data_dir=tmp_path)
    with request_scope('background',defer_busy=True):
        with pytest.raises(ProcessingDeferred):LocalModels(settings).embed(['test'])
        with pytest.raises(ProcessingDeferred):LocalVision(settings).recognize(image_path=tmp_path/'image',bounds=[0,0,1,1],directory=tmp_path)
        with pytest.raises(ProcessingDeferred):list(reading.ocr_records(settings,{'pages':[]},tmp_path))
