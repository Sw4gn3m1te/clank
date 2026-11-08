$ErrorActionPreference = 'Stop'

$python = Join-Path $PSScriptRoot 'server\.venv\Scripts\python.exe'
$modelHealth = 'http://127.0.0.1:8080/health'
$modelArgs = @(
    '-hf', 'unsloth/DeepSeek-Prover-V2-7B-GGUF:Q8_0',
    '--port', '8080',
    '-np', '8',
    '-c', '16384',
    '-ngl', '99',
    '-fa', 'on',
    '-ctk', 'q8_0',
    '-ctv', 'q8_0',
    '--jinja'
)

function Test-ModelReady {
    try {
        $response = Invoke-WebRequest -Uri $modelHealth -UseBasicParsing -TimeoutSec 2
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

try {
    if (-not (Test-Path $python)) {
        throw "Python venv not found at $python. Run the setup steps in README.md first."
    }

    if (Test-ModelReady) {
        Write-Host 'Model already running on port 8080.'
    } else {
        if (-not (Get-Command 'llama-server' -ErrorAction SilentlyContinue)) {
            throw 'llama-server not found on PATH.'
        }

        Write-Host 'Starting model...'
        $model = Start-Process -FilePath 'llama-server' -ArgumentList $modelArgs -PassThru

        # No deadline: the first launch downloads the model, which can take a long time.
        while (-not (Test-ModelReady)) {
            if ($model.HasExited) {
                throw "llama-server exited with code $($model.ExitCode)."
            }
            Start-Sleep -Seconds 2
        }
        Write-Host 'Model ready.'
    }

    Write-Host 'Starting Clank server...'
    Push-Location (Join-Path $PSScriptRoot 'server')
    try {
        & $python -m clank_server --lean-project ..
    } finally {
        Pop-Location
    }
} catch {
    Write-Host "Error: $_" -ForegroundColor Red
}

pause
