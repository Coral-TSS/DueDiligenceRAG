"""User-visible packet counts must not masquerade as page coverage."""
import json
import subprocess

from dd_agent.config import ROOT,Settings


def view(job):
    code='const {processingView}=require(process.argv[1]); console.log(processingView(JSON.parse(process.argv[2])).text);'
    result=subprocess.run([str(Settings().harness_node),'-e',code,
        str(ROOT/'src/dd_agent/static/progress.js'),json.dumps(job)],
        capture_output=True,text=True,encoding='utf-8',timeout=10)
    assert result.returncode==0,result.stderr
    return result.stdout


def test_page_success_and_packet_failure_are_distinct_in_ui():
    text=view(dict(status='partial',progress=dict(scheduler='stages-v1',total_pages=2,
        stages=dict(embedding=dict(succeeded=2)),failed_work_count=1,
        parent_work=dict(succeeded=1,failed=1,cancelled=0,pending=0,running=0))))
    assert '向量入库 2/2 页' in text
    assert '跨页工作包 1/2' in text and '失败 1 包' in text
    assert '部分页面处理失败' not in text


def test_paused_packet_progress_shows_saved_and_pending_counts():
    text=view(dict(status='paused',progress=dict(scheduler='stages-v1',total_pages=2,
        stages=dict(embedding=dict(paused=True)),parent_work=dict(succeeded=2,pending=1,running=0,failed=0,cancelled=0))))
    assert '跨页工作包 2/3' in text and '待处理 1 包' in text
