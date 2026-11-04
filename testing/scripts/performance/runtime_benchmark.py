"""合成输入下的冷／热请求与实际解释器内存；不打开业务数据库。"""
from datetime import datetime
import json
from pathlib import Path
import subprocess
from time import perf_counter
from uuid import uuid4

from dd_agent.config import Settings
from dd_agent.model_runtime import ModelRuntime, WorkerSpec
from dd_agent.performance import capture


def main():
    root=Path(__file__).resolve().parents[3]
    output=root/'testing/runs'/('runtime-benchmark-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid4().hex[:6])
    output.mkdir(parents=True)
    settings=Settings(data_dir=output)
    image=output/'synthetic.png'
    code="from PIL import Image,ImageDraw,ImageFont; import sys; im=Image.new('RGB',(640,120),'white'); ImageDraw.Draw(im).text((20,30),'注册资本为2000万元',font=ImageFont.truetype('C:/Windows/Fonts/msyh.ttc',40),fill='black'); im.save(sys.argv[1])"
    subprocess.run([str(settings.vision_python),'-c',code,str(image)],check=True,timeout=30)
    script=root/'src/dd_agent/workers/resident_worker.py'
    results=[]
    for kind in ('embedding','reranker','ocr','vision'):
        directory=output/kind;directory.mkdir()
        config=dict(kind=kind,threads=4)
        operation='recognize'
        if kind in ('embedding','reranker'):
            python=settings.retrieval_python
            config[kind]=str(settings.embedding_dir if kind=='embedding' else settings.reranker_dir)
            payload=dict(texts=['注册资本为2000万元。'])
            operation='embed' if kind=='embedding' else 'rerank'
            if kind=='reranker':payload['query']='注册资本是多少'
        elif kind=='ocr':
            python=settings.ocr_python;config['models']=str(settings.ocr_models_dir)
            payload=dict(pages=[dict(page_number=1,image_path=str(image))])
        else:
            python=settings.vision_python;config.update(models=str(settings.vision_models_dir),max_new_tokens=32)
            payload=dict(image=str(image),bounds=[0,0,1,1],max_new_tokens=32)
        broker=ModelRuntime(directory)
        try:
            spec=WorkerSpec(python,script,'benchmark-'+kind,config)
            elapsed=[]
            with capture(directory/'metrics','benchmark',sample_memory=True):
                for _ in range(2):
                    start=perf_counter()
                    list(broker.infer(spec,operation,payload,timeout=300))
                    elapsed.append(round(perf_counter()-start,4))
            metric=json.loads(next((directory/'metrics').glob('*.json')).read_text(encoding='utf-8'))
            results.append(dict(kind=kind,cold_seconds=elapsed[0],hot_seconds=elapsed[1],
                **broker.snapshot(),memory=metric.get('memory'),counters=metric.get('counters')))
            (output/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(results[-1],ensure_ascii=False),flush=True)
        finally:broker.close()
    print(str(output),flush=True)


if __name__=='__main__':main()
