"""Start and stop the official runtime without a model prompt or DB access."""
# Harness 基线探针：只启动 SDK、初始化连接和读取工作目录，不发送 session prompt。
# Windows 打包运行时曾有 native 模块加载问题，因此此基线指定同版本 npm CLI＋独立 Node。
from importlib.metadata import version
import json
import os
from pathlib import Path
import subprocess
import sys


SDK_VERSION='0.2.1rc2'
RUNTIME_VERSION='0.2.1-rc.2'


def main():
    payload=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    sdk=version('deepseek-harness-sdk')
    wheel=version('deepseek-harness-runtime-bin')
    if sdk!=SDK_VERSION or wheel!=SDK_VERSION:
        # 不跟随 latest 自动升级；变更版本需重新验证 profile 与会话／工具兼容性。
        return dict(status='error',error_code='version_mismatch')
    from deepseek_harness import HarnessClient,HarnessConfig
    home=Path(payload['home']).resolve();workspace=Path(payload['workspace']).resolve()
    if home==workspace or home in workspace.parents or workspace in home.parents:
        # 会话持久化目录与可操作工作目录分开，避免把内部运行记录当成业务文件编辑。
        return dict(status='error',error_code='overlapping_home_workspace')
    home.mkdir(parents=True,exist_ok=True);workspace.mkdir(parents=True,exist_ok=True)
    node=Path(payload['node']);dsh=Path(payload['dsh_bin'])
    if not node.is_file() or not dsh.is_file():return dict(status='error',error_code='npm_runtime_missing')
    os.environ['PATH']=str(node.parent)+os.pathsep+os.environ.get('PATH','')
    os.environ['DSH_HOME']=str(home)
    node_version=subprocess.run([str(node),'--version'],capture_output=True,text=True,check=True,timeout=10).stdout.strip()
    if node_version!='v22.19.0':return dict(status='error',error_code='node_version_mismatch')
    native=subprocess.run([str(dsh),'--version'],capture_output=True,
                          text=True,encoding='utf-8',timeout=10,check=True).stdout.strip()
    if native!=RUNTIME_VERSION:return dict(status='error',error_code='runtime_version_mismatch')
    config=HarnessConfig(profile='sdk-minimal',dsh_bin=str(dsh),dsh_home=str(home),cwd=str(workspace),
        patches=(payload['patch'],),initialize_timeout_seconds=25,request_timeout_seconds=10,
        shutdown_timeout_seconds=5)
    with HarnessClient(config) as client:
        # initialize 指定适配器元数据，不等于请求该外网模型；这里没有 prompt 调用。
        info=client.initialize(cwd=str(workspace),provider='deepseek-official',model='deepseek-v4-flash')
        cwd=client.get_working_directory('dd-runtime-check')
    return dict(status='ready',sdk_version=sdk,runtime_version=native,profile='sdk-minimal',
                runtime_backend='npm-cli',node_version=node_version,
                server=info.serverInfo.model_dump() if info.serverInfo else None,
                home=str(home),workspace=cwd,model_requests=0,
                report_tools_connected=False,report_generation_verified=False)


if __name__=='__main__':
    # stdout 只返回机器可读状态；错误只暴露类型／错误码，不转发环境或凭据内容。
    try:result=main()
    except Exception as exc:
        # No raw SDK errors or environment values in the model-facing output.
        print(type(exc).__name__,file=sys.stderr)
        result=dict(status='error',error_code='runtime_initialization_failed')
    print(json.dumps(result,ensure_ascii=False),flush=True)
