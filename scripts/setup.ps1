# 尽调材料工作区：Windows 一条命令安装。
# 找到或安装 Python 3.12（不用 3.13，也不调用裸 pip），探测显卡后交给 Python 安装脚本。
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup.ps1
#   .\scripts\setup.ps1 -Device gtx10 -ModelSource hf-mirror
[CmdletBinding()]
param(
    [ValidateSet('auto', 'cpu', 'gtx10', 'rtx40')]
    [string]$Device = 'auto',
    [ValidateSet('cu126', 'cu129')]
    [string]$PaddleCuda = 'cu126',
    [ValidateSet('huggingface', 'hf-mirror', 'modelscope')]
    [string]$ModelSource = '',
    [string]$DatabaseUrl = '',
    [ValidateSet('ask', 'docker', 'existing', 'skip')]
    [string]$Postgres = 'ask',
    [int]$PostgresPort = 54329,
    [string]$Python = '',
    [string]$GpuName = '',
    [string]$ComputeCapability = '',
    [switch]$SkipVision,
    [switch]$SkipOcr,
    [switch]$SkipRetrieval,
    [switch]$SkipPdf,
    [switch]$SkipModels,
    [switch]$SkipLibreOffice,
    [switch]$SkipDatabase,
    [switch]$NonInteractive,
    [switch]$NoMcpKey,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
if (Test-Path variable:PSNativeCommandUseErrorActionPreference) {
    $PSNativeCommandUseErrorActionPreference = $false
}

$Root = Split-Path -Parent $PSScriptRoot
$Package = Join-Path $Root 'scripts\local_setup\__main__.py'

function Test-IsPython312 {
    param([string]$Exe)
    if (-not $Exe) { return $false }
    $path = $Exe.Trim()
    if (-not (Test-Path -LiteralPath $path)) { return $false }
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $version = & $path -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>$null
    $code = $LASTEXITCODE
    $ErrorActionPreference = $previous
    if ($code -ne 0 -or -not $version) { return $false }
    return $version.Trim().StartsWith('3.12.')
}

function Get-CommandSource {
    param([string]$Name)
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Find-Uv {
    $found = Get-CommandSource 'uv'
    if ($found) { return $found }
    $local = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
    if (Test-Path -LiteralPath $local) { return $local }
    return $null
}

function Install-Uv {
    Write-Host '正在安装 uv，用来取得 Python 3.12。'
    $winget = Get-CommandSource 'winget'
    if ($winget) {
        & $winget install --id astral-sh.uv -e --accept-package-agreements --accept-source-agreements --disable-interactivity
    }
    $uv = Find-Uv
    if ($uv) { return $uv }
    Write-Host 'winget 没有装好 uv，改用官方安装脚本。'
    $install = Join-Path $env:TEMP 'dd-agent-uv-install.ps1'
    Invoke-WebRequest -Uri 'https://astral.sh/uv/install.ps1' -OutFile $install
    & $install
    $env:Path = (Join-Path $env:USERPROFILE '.local\bin') + ';' + $env:Path
    return Find-Uv
}

function Find-Python312 {
    param([string]$Requested)
    if ($Requested) {
        if (Test-IsPython312 $Requested) { return $Requested.Trim() }
        throw "指定的解释器不是 Python 3.12：$Requested"
    }
    $bare = Get-CommandSource 'python'
    if ($bare) {
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        $shown = & $bare -c "import sys; print(sys.version.split()[0])" 2>$null
        $ErrorActionPreference = $previous
        if ($shown -and $shown.Trim().StartsWith('3.13')) {
            Write-Host "PATH 上的 python 是 $($shown.Trim())。本项目不能使用 3.13，将继续寻找 3.12。"
        }
    }
    $py = Get-CommandSource 'py'
    if ($py) {
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        $exe = & $py -3.12 -c "import sys; print(sys.executable)" 2>$null
        $code = $LASTEXITCODE
        $ErrorActionPreference = $previous
        if ($code -eq 0 -and (Test-IsPython312 $exe)) { return $exe.Trim() }
    }
    foreach ($name in @('python3.12', 'python')) {
        $candidate = Get-CommandSource $name
        if ($candidate -and (Test-IsPython312 $candidate)) { return $candidate }
    }
    $userPython = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
    if (Test-IsPython312 $userPython) { return $userPython }
    $uv = Find-Uv
    if ($uv) {
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        $found = & $uv python find 3.12 2>$null
        $code = $LASTEXITCODE
        $ErrorActionPreference = $previous
        if ($code -eq 0 -and (Test-IsPython312 $found)) { return $found.Trim() }
    }
    return $null
}

function Install-Python312 {
    Write-Host '未找到 Python 3.12。开始安装 3.12，不会卸载已有的其他版本。'
    $uv = Find-Uv
    if (-not $uv) { $uv = Install-Uv }
    if ($uv) {
        $env:Path = (Join-Path $env:USERPROFILE '.local\bin') + ';' + $env:Path
        & $uv python install 3.12
        if ($LASTEXITCODE -eq 0) {
            $found = & $uv python find 3.12
            if ($LASTEXITCODE -eq 0 -and (Test-IsPython312 $found)) { return $found.Trim() }
        }
        Write-Host 'uv 未能提供可用的 Python 3.12。'
    }
    $winget = Get-CommandSource 'winget'
    if ($winget) {
        Write-Host '改用 winget 安装 Python 3.12。'
        & $winget install --id Python.Python.3.12 -e --scope user --accept-package-agreements --accept-source-agreements --disable-interactivity
        $candidate = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
        if (Test-IsPython312 $candidate) { return $candidate }
    }
    throw '仍未找到 Python 3.12。请安装 3.12 后重新运行。不要使用 3.13，也不要使用指向 3.13 的 pip。'
}

if (-not (Test-Path -LiteralPath $Package)) {
    throw "找不到安装脚本：$Package"
}

$python = Find-Python312 $Python
if (-not $python) { $python = Install-Python312 }
Write-Host "使用 Python 3.12：$python"

$arguments = @('-m', 'local_setup', '--device', $Device, '--paddle-cuda', $PaddleCuda, '--postgres', $Postgres, '--postgres-port', "$PostgresPort")
if ($ModelSource) { $arguments += @('--model-source', $ModelSource) }
if ($DatabaseUrl) { $arguments += @('--database-url', $DatabaseUrl) }
if ($SkipVision) { $arguments += '--skip-vision' }
if ($SkipOcr) { $arguments += '--skip-ocr' }
if ($SkipRetrieval) { $arguments += '--skip-retrieval' }
if ($SkipPdf) { $arguments += '--skip-pdf' }
if ($SkipModels) { $arguments += '--skip-models' }
if ($SkipLibreOffice) { $arguments += '--skip-libreoffice' }
if ($SkipDatabase) { $arguments += '--skip-database' }
if ($NonInteractive) { $arguments += '--non-interactive' }
if ($NoMcpKey) { $arguments += '--no-mcp-key' }
if ($DryRun) { $arguments += '--dry-run' }

if (-not $GpuName -and -not $ComputeCapability -and $Device -eq 'auto') {
    $smi = Get-CommandSource 'nvidia-smi'
    if ($smi) {
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        $line = & $smi --query-gpu=name,compute_cap --format=csv,noheader 2>$null | Select-Object -First 1
        $code = $LASTEXITCODE
        $ErrorActionPreference = $previous
        if ($code -eq 0 -and $line) {
            $parts = $line -split ',', 2
            $GpuName = $parts[0].Trim()
            if ($parts.Count -gt 1) { $ComputeCapability = $parts[1].Trim() }
        }
    }
}
if ($GpuName) { $arguments += @('--gpu-name', $GpuName) }
if ($ComputeCapability) { $arguments += @('--compute-capability', $ComputeCapability) }

$env:PYTHONUTF8 = '1'
$env:PYTHONPATH = (Join-Path $Root 'scripts') + [IO.Path]::PathSeparator + $env:PYTHONPATH
& $python @arguments
exit $LASTEXITCODE
