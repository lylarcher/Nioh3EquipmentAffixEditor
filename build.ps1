<#
.SYNOPSIS
    Build Nioh3AccessoryEditor and stamp it with its build/version identity.

.DESCRIPTION
    Runs the whole build in one place:

      1. resolve the project Python interpreter,
      2. read the git facts (commit id, branch, dirty state),
      3. generate the build identity -- nioh3_accessory_editor/_buildinfo.py,
         BUILD-INFO.json and BUILD-INFO.txt -- via tools/make_build_info.py,
      4. run the unit-test suite (python tools/run_tests.py),
      5. stage a runnable Release tree under dist/ and zip it,
      6. smoke-test the staged copy so a packaged tree provably reports the
         frozen build identity instead of falling back to live git.

    The version information always reports these four facts:
      * commit   -- the LAST 8 characters of the git commit id (project rule),
      * from     -- where the build came from: the project root and the crypto
                    executable it uses (bin\Nioh_Savefile_decrypt.exe),
      * built    -- the local build timestamp (ISO-8601 with UTC offset),
      * language -- the language/runtime the build targets (CPython + stdlib).

    Debug configuration generates the build identity and runs the tests, but
    does not stage or zip a distribution.

    NOTE: this file is UTF-8 **with BOM** on purpose. Windows PowerShell reads
    a BOM-less script using the ANSI code page (GBK on zh-CN), which would
    corrupt the Chinese messages and break parsing. Keep the BOM when editing.

.PARAMETER Python
    Python interpreter to use. Defaults to $env:NIOH3_PYTHON, then the known
    local install, then a .venv, then "python" from PATH.

.PARAMETER Configuration
    Release (default) builds + zips; Debug only stamps build info and tests.

.PARAMETER OutputDirectory
    Directory that receives the staged tree and the zip. Default: <root>\dist.

.PARAMETER SkipTests
    Do not run the unit-test suite.

.PARAMETER SkipZip
    Stage the distribution tree but do not create the .zip archive.

.PARAMETER PureCryptoTests
    Also run the slow full-file pure-Python crypto round trip.

.PARAMETER Quiet
    Suppress per-step progress output (the version banner is still printed).

.EXAMPLE
    powershell -File .\build.ps1
    Full Release build: stamp, test, stage, zip.

.EXAMPLE
    powershell -File .\build.ps1 -Configuration Debug -SkipTests
    Only refresh the build identity of the working tree.
#>
[CmdletBinding()]
param(
    [string]$Python,
    [ValidateSet('Release', 'Debug')]
    [string]$Configuration = 'Release',
    [string]$OutputDirectory,
    [switch]$SkipTests,
    [switch]$SkipZip,
    [switch]$PureCryptoTests,
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'
$LASTEXITCODE = 0

$projectRoot = $PSScriptRoot
$packageRoot = Join-Path $projectRoot 'nioh3_accessory_editor'
$buildInfoScript = Join-Path $projectRoot 'tools\make_build_info.py'
$testScript = Join-Path $projectRoot 'tools\run_tests.py'
$cryptoExe = Join-Path $projectRoot 'bin\Nioh_Savefile_decrypt.exe'
$buildInfoJson = Join-Path $projectRoot 'BUILD-INFO.json'
$buildInfoText = Join-Path $projectRoot 'BUILD-INFO.txt'
$distRoot = if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    Join-Path $projectRoot 'dist'
} else {
    $OutputDirectory
}

# Top-level entries copied into a staged Release tree.
$stageItems = @(
    'launch_editor.py',
    'README.md',
    'nioh3_accessory_editor',
    'bin',
    'data',
    'tests',
    'tools'
)
$excludeDirectories = @('.git', '__pycache__', 'dist', '_nioh3_accessory_backup', 'state')
$excludeFilePatterns = @('*.pyc', '*.pyo', '*.tmp', '*.zip')


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

function Write-Step {
    param([string]$Message)
    if (-not $Quiet) {
        Write-Host ''
        Write-Host "==> $Message" -ForegroundColor Cyan
    }
}

function Write-Note {
    param([string]$Message)
    if (-not $Quiet) {
        Write-Host "    $Message" -ForegroundColor DarkGray
    }
}

function Resolve-ProjectPython {
    param([string]$RequestedPython)

    $candidates = New-Object System.Collections.Generic.List[string]
    if (-not [string]::IsNullOrWhiteSpace($RequestedPython)) {
        $candidates.Add($RequestedPython)
    } elseif (-not [string]::IsNullOrWhiteSpace($env:NIOH3_PYTHON)) {
        $candidates.Add($env:NIOH3_PYTHON)
    } else {
        $candidates.Add('D:\SoftWareList\Profession\Python3.10\python.exe')
        $candidates.Add((Join-Path $projectRoot '.venv\Scripts\python.exe'))
        $candidates.Add((Join-Path $projectRoot '.codex_tmp\build-env\Scripts\python.exe'))
    }

    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }

    $onPath = Get-Command -Name 'python' -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -ne $onPath) {
        return $onPath.Source
    }

    throw @'
No usable Python interpreter was found. Pass -Python <path>, set NIOH3_PYTHON,
or put python.exe on PATH. This project needs Python 3.10+ (stdlib only).
'@
}

function Invoke-NativeCommand {
    <#
        Run a native executable and report its exit code via
        $script:LastNativeExitCode.

        Native stderr output reaches PowerShell as error records; with
        $ErrorActionPreference = 'Stop' (Windows PowerShell 5.1 and PowerShell
        7.4+ alike) a child writing a mere warning to stderr would abort this
        script.  The relaxations below are scoped to the call: the exit code
        stays the single source of truth.
    #>
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [switch]$MergeError,
        [switch]$DiscardError
    )

    $previousEap = $ErrorActionPreference
    $hasNativePreference = Test-Path variable:PSNativeCommandUseErrorActionPreference
    $previousNativePreference = $null
    if ($hasNativePreference) {
        $previousNativePreference = $PSNativeCommandUseErrorActionPreference
        $PSNativeCommandUseErrorActionPreference = $false
    }
    try {
        $ErrorActionPreference = 'Continue'
        if ($MergeError) {
            # Merge stderr into stdout as *plain text*: children legitimately
            # warn on stderr (e.g. "no exe, using the Python backend"), and an
            # unstringified ErrorRecord would be rendered as a red
            # NativeCommandError that looks like a build failure.
            & $FilePath @Arguments 2>&1 | ForEach-Object {
                $text = if ($_ -is [System.Management.Automation.ErrorRecord]) {
                    $_.Exception.Message
                } else {
                    "$_"
                }
                # PowerShell occasionally surfaces a bare type name instead of
                # the stderr text; it carries no information.
                if (-not [string]::IsNullOrWhiteSpace($text) -and
                    -not $text.StartsWith('System.Management.Automation.')) {
                    $text
                }
            }
            $script:LastNativeExitCode = $LASTEXITCODE
        } elseif ($DiscardError) {
            & $FilePath @Arguments 2>$null
            $script:LastNativeExitCode = $LASTEXITCODE
        } else {
            & $FilePath @Arguments
            $script:LastNativeExitCode = $LASTEXITCODE
        }
    } finally {
        $ErrorActionPreference = $previousEap
        if ($hasNativePreference) {
            $PSNativeCommandUseErrorActionPreference = $previousNativePreference
        }
    }
}

function Invoke-Git {
    param([string[]]$Arguments)

    $gitCommand = Get-Command -Name 'git' -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $gitCommand) {
        return $null
    }
    $output = Invoke-NativeCommand -FilePath $gitCommand.Source `
        -Arguments (@('-C', $projectRoot) + $Arguments) -DiscardError
    if ($script:LastNativeExitCode -ne 0) {
        return $null
    }
    return ($output | Out-String).Trim()
}

function Get-CommitShort {
    <# Project convention: the LAST 8 characters of the git commit id. #>
    param([string]$Commit)
    if ([string]::IsNullOrWhiteSpace($Commit)) {
        return 'unknown'
    }
    if ($Commit.Length -le 8) {
        return $Commit
    }
    return $Commit.Substring($Commit.Length - 8)
}

function Invoke-PythonStep {
    param([string[]]$Arguments)

    Invoke-NativeCommand -FilePath $script:PythonExe -Arguments $Arguments -MergeError
    if ($script:LastNativeExitCode -ne 0) {
        $exitCode = $script:LastNativeExitCode
        $commandLine = $Arguments -join ' '
        throw "Python 步骤失败 (exit $exitCode): $script:PythonExe $commandLine"
    }
}

function Copy-FilteredTree {
    param([string]$Source, [string]$Destination)

    $files = Get-ChildItem -LiteralPath $Source -Recurse -Force -File
    foreach ($file in $files) {
        $relative = $file.FullName.Substring($Source.Length).TrimStart('\', '/')
        $parts = $relative -split '[\\/]'
        $skip = $false
        foreach ($part in $parts) {
            if ($excludeDirectories -contains $part) { $skip = $true; break }
        }
        if (-not $skip) {
            foreach ($pattern in $excludeFilePatterns) {
                if ($file.Name -like $pattern) { $skip = $true; break }
            }
        }
        if ($skip) { continue }

        $target = Join-Path $Destination $relative
        $targetDirectory = Split-Path -Parent $target
        if (-not (Test-Path -LiteralPath $targetDirectory)) {
            New-Item -ItemType Directory -Path $targetDirectory -Force | Out-Null
        }
        Copy-Item -LiteralPath $file.FullName -Destination $target -Force
    }
}


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------

function Invoke-Build {

    # 1. Environment ---------------------------------------------------------
    Write-Step "解析 Python 解释器 (Configuration=$Configuration)"
    $script:PythonExe = Resolve-ProjectPython -RequestedPython $Python
    $pythonVersion = (Invoke-NativeCommand -FilePath $script:PythonExe `
            -Arguments @('-c', 'import platform; print(platform.python_version())') |
        Out-String).Trim()
    if ($script:LastNativeExitCode -ne 0) { throw "无法运行 Python: $script:PythonExe" }
    Write-Note "python: $($script:PythonExe) (v$pythonVersion)"

    if (-not (Test-Path -LiteralPath $buildInfoScript -PathType Leaf)) {
        throw "缺少构建信息脚本: $buildInfoScript"
    }
    if (-not (Test-Path -LiteralPath $cryptoExe -PathType Leaf)) {
        throw "缺少加密组件（版本信息中的来源之一）: $cryptoExe"
    }

    # 2. Git facts -----------------------------------------------------------
    Write-Step '读取 git 信息 (commit id / 分支 / 工作区状态)'
    $commitFull = Invoke-Git -Arguments @('rev-parse', 'HEAD')
    if ([string]::IsNullOrWhiteSpace($commitFull)) {
        $commitFull = 'unknown'
        Write-Warning '无法读取 git commit（非 git 仓库或缺少 git），commit 记为 unknown'
    }
    $commitShort = Get-CommitShort -Commit $commitFull
    $branch = Invoke-Git -Arguments @('rev-parse', '--abbrev-ref', 'HEAD')
    if ([string]::IsNullOrWhiteSpace($branch)) { $branch = 'unknown' }
    $status = Invoke-Git -Arguments @('status', '--porcelain')
    $dirty = -not [string]::IsNullOrWhiteSpace($status)
    $builtAt = (Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz')
    Write-Note "commit: $commitShort (完整 $commitFull)"
    Write-Note "branch: $branch, 工作区有改动: $dirty, 构建时间: $builtAt"

    # 3. Build identity ------------------------------------------------------
    Write-Step '生成构建/版本信息 (_buildinfo.py + BUILD-INFO.json/.txt)'
    $buildInfoArguments = @(
        $buildInfoScript,
        '--commit', $commitFull,
        '--branch', $branch,
        '--built-at', $builtAt,
        '--crypto-exe', $cryptoExe,
        '--built-from', $projectRoot,
        '--module-path', (Join-Path $packageRoot '_buildinfo.py'),
        '--out-dir', $projectRoot
    )
    if ($dirty) { $buildInfoArguments += '--dirty' } else { $buildInfoArguments += '--clean' }
    # Always quiet: the generator then prints only the paths it wrote, and the
    # banner is printed once, by the version report at the end of this script.
    $buildInfoArguments += '--quiet'
    Invoke-PythonStep -Arguments $buildInfoArguments

    if (-not (Test-Path -LiteralPath $buildInfoJson -PathType Leaf)) {
        throw "构建信息未生成: $buildInfoJson"
    }
    $info = Get-Content -LiteralPath $buildInfoJson -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($info.commit -ne $commitShort) {
        throw "构建信息中的 commit 不是 commit id 后 8 位: $($info.commit) != $commitShort"
    }
    if ([string]::IsNullOrWhiteSpace($info.language)) {
        throw '构建信息缺少语言字段'
    }

    # 4. Tests ---------------------------------------------------------------
    if ($SkipTests) {
        Write-Step '跳过单元测试 (-SkipTests)'
    } else {
        Write-Step '运行单元测试 (tools/run_tests.py)'
        $testArguments = @($testScript)
        if ($PureCryptoTests) { $testArguments += '--pure-crypto' }
        Invoke-PythonStep -Arguments $testArguments
    }

    # 5. Stage + zip (Release only) -----------------------------------------
    $script:StagePath = $null
    $script:ZipPath = $null
    if ($Configuration -eq 'Debug') {
        Write-Step 'Debug 配置：不生成发行目录（仅刷新构建信息并跑测试）'
    } else {
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $stageName = "Nioh3AccessoryEditor-v$($info.version)-$commitShort-$stamp"
        $script:StagePath = Join-Path $distRoot $stageName

        Write-Step "组装发行目录 $($script:StagePath)"
        if (Test-Path -LiteralPath $script:StagePath) {
            Remove-Item -LiteralPath $script:StagePath -Recurse -Force
        }
        New-Item -ItemType Directory -Path $script:StagePath -Force | Out-Null

        foreach ($item in $stageItems) {
            $source = Join-Path $projectRoot $item
            if (-not (Test-Path -LiteralPath $source)) {
                throw "缺少发行必需项: $source"
            }
            if (Test-Path -LiteralPath $source -PathType Container) {
                Copy-FilteredTree -Source $source -Destination (Join-Path $script:StagePath $item)
            } else {
                Copy-Item -LiteralPath $source -Destination (Join-Path $script:StagePath $item) -Force
            }
        }
        Copy-Item -LiteralPath $buildInfoJson -Destination $script:StagePath -Force
        Copy-Item -LiteralPath $buildInfoText -Destination $script:StagePath -Force
        Write-Note "已复制 $($stageItems.Count) 个顶层项 + BUILD-INFO.json/.txt"

        Write-Step '冒烟测试发行副本（必须报告冻结的构建信息）'
        $smokeOutput = Invoke-NativeCommand `
            -FilePath $script:PythonExe `
            -Arguments @((Join-Path $script:StagePath 'launch_editor.py'), '--version') `
            -MergeError
        $smokeExit = $script:LastNativeExitCode
        if ($smokeExit -ne 0) {
            throw "发行副本冒烟测试失败 (exit $smokeExit)"
        }
        $smokeText = ($smokeOutput | Out-String)
        if ($smokeText -notmatch [regex]::Escape($commitShort)) {
            throw "发行副本未报告预期的 commit $commitShort"
        }
        Write-Note "冒烟测试通过：发行副本报告 commit $commitShort"

        if ($SkipZip) {
            Write-Step '跳过打包 (-SkipZip)'
        } else {
            $script:ZipPath = "$($script:StagePath).zip"
            Write-Step "打包 $($script:ZipPath)"
            if (Test-Path -LiteralPath $script:ZipPath) {
                Remove-Item -LiteralPath $script:ZipPath -Force
            }
            Compress-Archive -LiteralPath $script:StagePath -DestinationPath $script:ZipPath `
                -CompressionLevel Optimal
            $zipSize = [math]::Round((Get-Item -LiteralPath $script:ZipPath).Length / 1KB, 1)
            Write-Note "压缩包大小: $zipSize KB"
        }
    }

    # 6. Version report ------------------------------------------------------
    Write-Step '版本信息（commit 后 8 位 / 来源 / 构建时间 / 语言）'
    Write-Host ''
    Get-Content -LiteralPath $buildInfoText -Encoding UTF8 | ForEach-Object { Write-Host $_ }
    Write-Host ''
    if ($Configuration -eq 'Release') {
        Write-Host "发行目录  : $($script:StagePath)"
        if ($null -ne $script:ZipPath) { Write-Host "压缩包    : $($script:ZipPath)" }
    }
    Write-Host "完整 commit: $commitFull"
    Write-Host "构建信息  : $buildInfoJson"
    Write-Host ''
    Write-Host '仅供测试学习用，不要用于联机影响游戏平衡。' -ForegroundColor Yellow
}


# --------------------------------------------------------------------------
# Entry: run with a UTF-8 console so the Chinese banner renders everywhere,
# then restore the caller's encodings.
# --------------------------------------------------------------------------

$previousPythonIoEncoding = $env:PYTHONIOENCODING
$previousConsoleEncoding = [Console]::OutputEncoding
try {
    $env:PYTHONIOENCODING = 'utf-8'
    try {
        [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
    } catch {
        Write-Verbose "无法设置控制台编码: $_"
    }
    Invoke-Build
} finally {
    $env:PYTHONIOENCODING = $previousPythonIoEncoding
    try {
        if ($null -ne $previousConsoleEncoding) {
            [Console]::OutputEncoding = $previousConsoleEncoding
        }
    } catch {
        Write-Verbose "无法恢复控制台编码: $_"
    }
}
