# Abre el Panel de Control de Maestro.
#
# Uso: clic derecho > "Ejecutar con PowerShell"
#      o desde una terminal:  .\panel\iniciar.ps1

$panel = $PSScriptRoot
$url   = "http://127.0.0.1:8770"

Write-Host ""
Write-Host "  Panel de Control de Maestro" -ForegroundColor Cyan
Write-Host "  ---------------------------" -ForegroundColor DarkGray

# Python es el unico requisito del panel (usa solo libreria estandar).
$python = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $python) {
  Write-Host "  No se encontro Python en el PATH." -ForegroundColor Red
  Write-Host "  Instalalo desde https://www.python.org/downloads/ y vuelve a intentar."
  Read-Host "`n  Enter para cerrar"
  exit 1
}

# Si el puerto ya esta ocupado, asumimos que el panel ya corre: solo abrimos.
$ocupado = Test-NetConnection -ComputerName 127.0.0.1 -Port 8770 -InformationLevel Quiet -WarningAction SilentlyContinue
if ($ocupado) {
  Write-Host "  El panel ya estaba abierto. Abriendo el navegador..." -ForegroundColor Yellow
  Start-Process $url
  exit 0
}

Write-Host "  Servidor:  $url"
Write-Host "  Proyecto:  $(Split-Path $panel -Parent)"
Write-Host "  Ctrl+C para detener el panel." -ForegroundColor DarkGray
Write-Host ""

# --abrir hace que el servidor lance el navegador cuando ya este escuchando.
& python "$panel\servidor.py" --abrir
