$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
docker compose run --rm -T rag @args
exit $LASTEXITCODE
