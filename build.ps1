$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $root

python -m pip install --disable-pip-version-check --target .build_tools -r requirements-build.txt
python -c "import sys; sys.path.insert(0, r'.build_tools'); from PyInstaller.__main__ import run; run(['--clean', '--noconfirm', 'PoTranslator.spec'])"

Write-Output "Executable created at: $root\dist\PoTranslator.exe"

