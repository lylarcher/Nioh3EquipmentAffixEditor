<#
.SYNOPSIS
    Build Nioh3AccessoryEditor and stamp it with its build/version identity.

.DESCRIPTION
    Runs the whole build in one place:

      1. resolve the project Python interpreter,
      2. read the git facts (commit id, branch, dirty state),
      3. generate the build identity -- nioh3_accessory_editor/_buildinfo.py,
         BUILD-INFO.json and BUILD-INFO.txt -- via tools/make_build_info.py,
      4. build app-payload.zip -- the parameter configuration, affix catalogue,
         bundled crypto helper and original source data,
      5. build a SINGLE-FILE executable (PyInstaller onefile) that embeds the
         payload, so the release contains no .py files whatsoever,
      6. smoke-test that exe in a fresh directory: it must unpack the payload
         next to itself and report the frozen build identity,
      7. verify the shipped tree has no Python sources, then zip the exe.

    The unit-test suite is NOT part of a default build: it takes minutes and the
    packaging steps are independent of it.  Pass -Test to run it (or -TestPattern
    to run one module), which is what a release build should do.  The version
    report at the end always states whether the tests ran, so a build log cannot
    be mistaken for a verified one.

    The version information always reports these four facts:
      * commit   -- the LAST 8 characters of the git commit id (project rule),
      * from     -- where the build came from: the project root and the crypto
                    executable it uses (bin\Nioh_Savefile_decrypt.exe),
      * built    -- the local build timestamp (ISO-8601 with UTC offset),
      * language -- the language/runtime the build targets (CPython + stdlib).

    On first run the exe writes data/, config/, bin/ and third_party/ next to
    itself (see nioh3_accessory_editor/bootstrap.py); from then on those are
    ordinary user-editable files, and a file the user changed is never
    overwritten by a later build.

    Debug configuration generates the build identity only: no executable -- and,
    like every configuration, no tests unless -Test is given.

    NOTE: this file is UTF-8 **with BOM** on purpose. Windows PowerShell reads
    a BOM-less script using the ANSI code page (GBK on zh-CN), which would
    corrupt the Chinese messages and break parsing. Keep the BOM when editing.

.PARAMETER Python
    Python interpreter to use. Defaults to $env:NIOH3_PYTHON, then the known
    local install, then a .venv, then "python" from PATH.

.PARAMETER Configuration
    Release (default) builds + zips; Debug only stamps build info and tests.

.PARAMETER OutputDirectory
    Directory that receives the executable and the zip. Default: <root>\dist.

.PARAMETER PyInstallerPython
    Python interpreter used to run PyInstaller. Default: the build virtual
    environment <root>\.build-venv, created and populated automatically when it
    is missing (PyInstaller is the only build-time dependency).

.PARAMETER Test
    Run the unit-test suite (python tools/run_tests.py) before packaging.  Off by
    default: the suite takes minutes and the packaging steps do not depend on it.
    Use it for a release build, where a green suite is the point.

.PARAMETER SkipTests
    Legacy switch, kept so existing commands keep working.  Tests are already
    skipped unless -Test is given, so this is now a no-op.

.PARAMETER TestPattern
    Only run test modules matching this filename pattern (e.g. test_cli.py).
    Implies -Test, so it keeps the suite verifiable while iterating on one
    subsystem without paying for the whole run.

.PARAMETER SkipZip
    Build the executable but do not create the .zip archive.

.PARAMETER PureCryptoTests
    Also run the slow full-file pure-Python crypto round trip.  Implies -Test.

.PARAMETER Clean
    Remove previous Nioh3AccessoryEditor* artifacts from the output directory
    before building. Only entries matching this project's own artifact name are
    touched, so a custom -OutputDirectory is never wiped wholesale.

.PARAMETER Quiet
    Suppress per-step progress output (the version banner is still printed).

.EXAMPLE
    powershell -File .\build.ps1
    Release build: stamp, payload, single-file exe, verify, zip (tests skipped).

.EXAMPLE
    powershell -File .\build.ps1 -Test
    Release build that runs the whole suite first -- use this before releasing.

.EXAMPLE
    powershell -File .\build.ps1 -Configuration Debug -TestPattern test_cli.py
    Refresh the build identity and run only the CLI tests.

.EXAMPLE
    powershell -File .\build.ps1 -Clean -SkipZip
    Rebuild the exe after deleting older artifacts, without zipping.

.EXAMPLE
    powershell -File .\build.ps1 -Clean -Test
    Release build that first drops older dist artifacts.
#>
[CmdletBinding()]
param(
    [string]$Python,
    [ValidateSet('Release', 'Debug')]
    [string]$Configuration = 'Release',
    [string]$OutputDirectory,
    [string]$PyInstallerPython,
    [switch]$Test,
    [switch]$SkipTests,
    [string]$TestPattern,
    [switch]$SkipZip,
    [switch]$PureCryptoTests,
    [switch]$Clean,
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'
$LASTEXITCODE = 0

#: Index used only to install the build-time PyInstaller into .build-venv.
$script:PipIndex = if ($env:NIOH3_PIP_INDEX) { $env:NIOH3_PIP_INDEX }
    else { 'https://pypi.tuna.tsinghua.edu.cn/simple' }

$projectRoot = $PSScriptRoot
$packageRoot = Join-Path $projectRoot 'nioh3_accessory_editor'
$buildInfoScript = Join-Path $projectRoot 'tools\make_build_info.py'
$testScript = Join-Path $projectRoot 'tools\run_tests.py'
$cryptoExe = Join-Path $projectRoot 'bin\Nioh_Savefile_decrypt.exe'
$payloadScript = Join-Path $projectRoot 'tools\make_payload.py'
$buildRoot = Join-Path $projectRoot 'build'
$buildVenvRoot = Join-Path $projectRoot '.build-venv'
$buildInfoJson = Join-Path $projectRoot 'BUILD-INFO.json'
$buildInfoText = Join-Path $projectRoot 'BUILD-INFO.txt'
$distRoot = if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    Join-Path $projectRoot 'dist'
} else {
    $OutputDirectory
}

# Payload contents are declared in tools/make_payload.py (single source of
# truth, covered by tests/test_payload.py).


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
        $script:LastNativeExitCode, returning its output as text lines.

        Why this is not just "& $exe @args 2>&1":
          * native stderr reaches PowerShell as error records, and with
            $ErrorActionPreference = 'Stop' a child writing a mere warning would
            abort the build;
          * on Windows PowerShell 5.1 the merged pipeline can complete with a
            stale $LASTEXITCODE of 0 even though the child exited non-zero, which
            silently disables every "did this step fail?" check.

        Start-Process -Wait -PassThru reports the real exit code, so it is the
        source of truth; stdout/stderr are captured to temporary files and
        returned as lines (UTF-8, the encoding this script forces on children).
    #>
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [switch]$MergeError,
        [switch]$DiscardError
    )

    # Start-Process joins -ArgumentList with spaces; quote anything that would
    # otherwise split (paths containing spaces, e.g. a checkout under "My Docs").
    $quoted = @()
    foreach ($argument in @($Arguments)) {
        $text = [string]$argument
        if ($text -match '[\s"]') {
            $quoted += '"' + ($text -replace '"', '\"') + '"'
        } else {
            $quoted += $text
        }
    }

    $stdoutFile = [System.IO.Path]::GetTempFileName()
    $stderrFile = [System.IO.Path]::GetTempFileName()
    try {
        $process = Start-Process -FilePath $FilePath -ArgumentList $quoted `
            -NoNewWindow -Wait -PassThru `
            -RedirectStandardOutput $stdoutFile -RedirectStandardError $stderrFile
        $script:LastNativeExitCode = $process.ExitCode

        $lines = @()
        if (Test-Path -LiteralPath $stdoutFile) {
            $lines += @(Get-Content -LiteralPath $stdoutFile -Encoding UTF8 -ErrorAction SilentlyContinue)
        }
        if ($MergeError -and (Test-Path -LiteralPath $stderrFile)) {
            $lines += @(Get-Content -LiteralPath $stderrFile -Encoding UTF8 -ErrorAction SilentlyContinue)
        }
        return $lines
    } finally {
        foreach ($temporary in @($stdoutFile, $stderrFile)) {
            if (Test-Path -LiteralPath $temporary) {
                Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
            }
        }
    }
}

function Assert-ExitCodeDetection {
    <#
        Prove that a failing step is actually noticed before trusting the build.

        This guards the whole pipeline: if PowerShell ever changes how a child's
        exit code is reported, the build must refuse to run rather than report a
        green build over failed tests.
    #>
    $gateScript = Join-Path $projectRoot 'tools\check_build_gate.py'

    Invoke-NativeCommand -FilePath $script:PythonExe `
        -Arguments @($gateScript, '--exit-code', '7', '--stderr', '--lines', '3') `
        -MergeError | Out-Null
    $deliberate = $script:LastNativeExitCode
    if ($deliberate -ne 7) {
        throw ("构建脚本无法识别失败的子进程：期望退出码 7，实际 $deliberate。" +
            '构建已中止，避免在测试失败的情况下产出发行包。')
    }

    Invoke-NativeCommand -FilePath $script:PythonExe `
        -Arguments @($gateScript, '--exit-code', '0') -MergeError | Out-Null
    if ($script:LastNativeExitCode -ne 0) {
        throw ("构建脚本把成功的子进程判为失败：实际退出码 $script:LastNativeExitCode。")
    }
    Write-Note '退出码检测自检通过（失败会被中止）'
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

    Invoke-NativeCommand -FilePath $script:PythonExe -Arguments $Arguments -MergeError |
        ForEach-Object { Write-Host $_ }
    if ($script:LastNativeExitCode -ne 0) {
        $exitCode = $script:LastNativeExitCode
        $commandLine = $Arguments -join ' '
        throw "Python 步骤失败 (exit $exitCode): $script:PythonExe $commandLine"
    }
}

function Resolve-PackagingPython {
    <#
        Return a Python that can run PyInstaller, creating the build virtual
        environment on demand.  PyInstaller is a build-time-only dependency, so
        it never touches the project's own interpreter or the user's PATH.
    #>
    if ($PyInstallerPython) {
        if (-not (Test-Path -LiteralPath $PyInstallerPython -PathType Leaf)) {
            throw "指定的 PyInstaller Python 不存在: $PyInstallerPython"
        }
        return $PyInstallerPython
    }

    $venvPython = Join-Path $buildVenvRoot 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        Write-Note "创建构建虚拟环境 $buildVenvRoot"
        Invoke-NativeCommand -FilePath $script:PythonExe `
            -Arguments @('-m', 'venv', $buildVenvRoot) -MergeError
        if ($script:LastNativeExitCode -ne 0 -or
            -not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
            throw "无法创建构建虚拟环境: $buildVenvRoot"
        }
    }

    Invoke-NativeCommand -FilePath $venvPython `
        -Arguments @('-c', 'import PyInstaller') -DiscardError
    if ($script:LastNativeExitCode -ne 0) {
        Write-Note '安装 PyInstaller（仅构建期依赖，装入 .build-venv）'
        Invoke-NativeCommand -FilePath $venvPython `
            -Arguments @('-m', 'pip', 'install', '--no-input', '--disable-pip-version-check',
                '--progress-bar', 'off', '-i', $script:PipIndex, 'pyinstaller') -MergeError
        if ($script:LastNativeExitCode -ne 0) {
            throw ('PyInstaller 安装失败。可手动执行：' +
                "$venvPython -m pip install -i $script:PipIndex pyinstaller")
        }
    }

    $version = (Invoke-NativeCommand -FilePath $venvPython `
            -Arguments @('-c', 'import PyInstaller; print(PyInstaller.__version__)') |
        Out-String).Trim()
    Write-Note "PyInstaller $version ($venvPython)"
    return $venvPython
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

    Write-Step '自检：构建脚本能否发现失败的子步骤'
    Assert-ExitCodeDetection

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

    # 4. Tests (opt-in: -Test) ----------------------------------------------
    # Deliberately not part of a default build: the suite takes minutes and the
    # packaging steps below do not depend on it.  -TestPattern/-PureCryptoTests
    # imply -Test, so asking for test-related output always runs tests.
    $script:TestsRan = $false
    $runTests = [bool]($Test -or $TestPattern -or $PureCryptoTests)
    if ($runTests) {
        $label = if ($TestPattern) { "（仅 $TestPattern）" } else { '' }
        Write-Step "运行单元测试 (tools/run_tests.py)$label"
        $testArguments = @($testScript)
        if ($TestPattern) { $testArguments += @('-p', $TestPattern) }
        if ($PureCryptoTests) { $testArguments += '--pure-crypto' }
        Invoke-PythonStep -Arguments $testArguments
        $script:TestsRan = $true
    } else {
        Write-Step '跳过单元测试（默认；需要时加 -Test）'
        if ($SkipTests) {
            Write-Note '-SkipTests 现在是空操作：不写它也已经是默认行为'
        }
    }

    # 5. Single-file executable (Release only) ------------------------------
    $script:ExePath = $null
    $script:ZipPath = $null
    if ($Configuration -eq 'Debug') {
        Write-Step 'Debug 配置：只刷新构建信息（不生成 exe）'
    } else {
        if ($Clean -and (Test-Path -LiteralPath $distRoot -PathType Container)) {
            Write-Step "清理旧的发行产物 $distRoot"
            $previous = Get-ChildItem -LiteralPath $distRoot -Force |
                Where-Object { $_.Name -like 'Nioh3AccessoryEditor*' }
            foreach ($entry in $previous) {
                Remove-Item -LiteralPath $entry.FullName -Recurse -Force
                Write-Note "已删除 $($entry.Name)"
            }
            if ($previous.Count -eq 0) { Write-Note '没有需要清理的产物' }
        }

        # 5a. Payload: the files the exe unpacks next to itself --------------
        Write-Step '校验图标与 logo（assets/ 必须与 tools/make_icon.py 一致）'
        $iconScript = Join-Path $projectRoot 'tools\make_icon.py'
        Invoke-PythonStep -Arguments @($iconScript, '--check')

        Write-Step '生成随 exe 内嵌的载荷（配置 / 词条库 / 图标 / 加解密组件 / 原始数据）'
        $payloadPath = Join-Path $buildRoot 'app-payload.zip'
        Invoke-PythonStep -Arguments @(
            $payloadScript,
            '--output', $payloadPath,
            '--root', $projectRoot,
            '--version', $info.version,
            '--commit', $commitShort,
            '--created', $builtAt,
            '--verify'
        )
        if (-not (Test-Path -LiteralPath $payloadPath -PathType Leaf)) {
            throw "载荷未生成: $payloadPath"
        }

        # 5b. PyInstaller: one file, no .py sources --------------------------
        Write-Step '构建单文件 exe (PyInstaller onefile)'
        $script:PackPython = Resolve-PackagingPython
        $packLog = Join-Path $buildRoot 'pyinstaller.log'
        $packArguments = @(
            '-m', 'PyInstaller',
            '--noconfirm', '--clean',
            '--distpath', $distRoot,
            '--workpath', (Join-Path $buildRoot 'pyi'),
            (Join-Path $projectRoot 'Nioh3AccessoryEditor.spec')
        )
        $packOutput = Invoke-NativeCommand -FilePath $script:PackPython `
            -Arguments $packArguments -MergeError
        # PyInstaller is verbose: keep the full log on disk, show the tail.
        $packOutput | Set-Content -LiteralPath $packLog -Encoding UTF8
        $packOutput | Select-Object -Last 8 | ForEach-Object { Write-Host "    $_" }
        if ($script:LastNativeExitCode -ne 0) {
            throw "PyInstaller 失败 (exit $($script:LastNativeExitCode))；完整日志: $packLog"
        }
        $script:ExePath = Join-Path $distRoot 'Nioh3AccessoryEditor.exe'
        if (-not (Test-Path -LiteralPath $script:ExePath -PathType Leaf)) {
            throw "未生成 exe: $($script:ExePath)"
        }
        $exeSize = [math]::Round((Get-Item -LiteralPath $script:ExePath).Length / 1MB, 2)
        Write-Note "exe: $($script:ExePath) ($exeSize MB)"

        # 5b-bis. The icon must really be inside the PE resources ------------
        Write-Step '校验 exe 内嵌图标（PE 资源里的 RT_GROUP_ICON）'
        $iconCheckScript = Join-Path $projectRoot 'tools\check_exe_icon.py'
        Invoke-PythonStep -Arguments @(
            $iconCheckScript,
            '--exe', $script:ExePath,
            '--against', (Join-Path $projectRoot 'assets\app.ico'),
            '--expect', '16,20,24,32,40,48,64,128,256'
        )

        # 5c. Smoke test: fresh directory, must extract and report its commit -
        Write-Step '冒烟测试单文件 exe（解压附属文件 + 报告冻结的构建信息）'
        $smokeRoot = Join-Path $buildRoot 'smoke'
        if (Test-Path -LiteralPath $smokeRoot) {
            Remove-Item -LiteralPath $smokeRoot -Recurse -Force
        }
        New-Item -ItemType Directory -Path $smokeRoot -Force | Out-Null
        $smokeExe = Join-Path $smokeRoot 'Nioh3AccessoryEditor.exe'
        Copy-Item -LiteralPath $script:ExePath -Destination $smokeExe -Force

        $smokeOutput = Invoke-NativeCommand -FilePath $smokeExe `
            -Arguments @('--version') -MergeError
        $smokeOutput | ForEach-Object { Write-Host $_ }
        $smokeExit = $script:LastNativeExitCode
        if ($smokeExit -ne 0) {
            throw "exe 冒烟测试失败 (exit $smokeExit)"
        }
        $smokeText = ($smokeOutput | Out-String)
        if ($smokeText -notmatch [regex]::Escape($commitShort)) {
            throw "exe 未报告预期的 commit $commitShort"
        }

        foreach ($relative in @('config\editor.json', 'data\accessory_affixes.json',
                'data\grace_affixes.json', 'data\accessory_items.json',
                'assets\app.ico', 'assets\logo-32.png', 'assets\logo.png',
                'bin\Nioh_Savefile_decrypt.exe', 'README.md', 'CHANGELOG.md',
                'third_party\source-data')) {
            if (-not (Test-Path -LiteralPath (Join-Path $smokeRoot $relative))) {
                throw "exe 未在自身目录解压: $relative"
            }
        }
        if (-not (Test-Path -LiteralPath (Join-Path $smokeRoot '.extracted-manifest.json'))) {
            throw 'exe 未写入解压清单 .extracted-manifest.json'
        }
        Write-Note "冒烟测试通过：解压附属文件成功，报告 commit $commitShort"

        # 5d. The shipped tree must contain no Python sources ----------------
        Write-Step '校验发行内容（仅 exe，无 .py 文件）'
        $pyFiles = @(Get-ChildItem -LiteralPath $smokeRoot -Recurse -Force -File -Filter '*.py')
        if ($pyFiles.Count -gt 0) {
            $names = ($pyFiles | ForEach-Object { $_.Name }) -join ', '
            throw "发行目录出现 Python 源文件: $names"
        }
        $topLevel = @(Get-ChildItem -LiteralPath $distRoot -Force)
        Write-Note "发行目录顶层项: $(($topLevel | ForEach-Object { $_.Name }) -join ', ')"

        if ($SkipZip) {
            Write-Step '跳过打包 (-SkipZip)'
        } else {
            $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
            $script:ZipPath = Join-Path $distRoot `
                "Nioh3AccessoryEditor-v$($info.version)-$commitShort-$stamp.zip"
            Write-Step "打包单文件 exe -> $($script:ZipPath)"
            if (Test-Path -LiteralPath $script:ZipPath) {
                Remove-Item -LiteralPath $script:ZipPath -Force
            }
            Compress-Archive -LiteralPath $script:ExePath -DestinationPath $script:ZipPath `
                -CompressionLevel Optimal
            $zipSize = [math]::Round((Get-Item -LiteralPath $script:ZipPath).Length / 1MB, 2)
            Write-Note "压缩包: $zipSize MB（内含单个 exe，其余文件首次运行时自解压）"
        }
    }

    # 6. Version report ------------------------------------------------------
    Write-Step '版本信息（commit 后 8 位 / 来源 / 构建时间 / 语言）'
    Write-Host ''
    Get-Content -LiteralPath $buildInfoText -Encoding UTF8 | ForEach-Object { Write-Host $_ }
    Write-Host ''
    if ($Configuration -eq 'Release') {
        Write-Host "单文件 exe: $($script:ExePath)"
        if ($null -ne $script:ZipPath) { Write-Host "压缩包    : $($script:ZipPath)" }
    }
    Write-Host "完整 commit: $commitFull"
    Write-Host "构建信息  : $buildInfoJson"
    if ($script:TestsRan) {
        Write-Host '单元测试  : 已运行（tools/run_tests.py）' -ForegroundColor Green
    } else {
        Write-Host '单元测试  : 未运行（默认跳过；发版前请加 -Test）' -ForegroundColor Yellow
    }
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

# Reaching this point means every step succeeded (a failed step throws and the
# script exits non-zero), so report success explicitly instead of leaking
# whatever $LASTEXITCODE happened to hold.
exit 0

