param(
  [Parameter(Mandatory=$true)]
  [string]$Tag,

  # Self-signed signing (helps only on PCs that trust this cert)
  [switch]$SelfSign
)

$ErrorActionPreference = "Stop"

Write-Host "== RAVEN BOT release build ==" -ForegroundColor Cyan
Write-Host "Tag: $Tag"

# 1) Build exe (onedir)
Write-Host "`n[1/3] Building exe via PyInstaller..." -ForegroundColor Cyan
python -m PyInstaller --clean -y ".\RAVEN_BOT.spec"

# 2) Optional: self-sign exe (local trust only)
$distDir = Join-Path $PSScriptRoot "dist\RAVEN_BOT"
$zipPath = Join-Path $PSScriptRoot "RAVEN_BOT.zip"
$exePath = Join-Path $distDir "RAVEN_BOT.exe"

if (!(Test-Path $distDir)) {
  throw "dist folder not found: $distDir"
}

if ($SelfSign) {
  Write-Host "`n[2/4] Self-signing exe..." -ForegroundColor Cyan
  if (!(Test-Path $exePath)) {
    throw "exe not found: $exePath"
  }

  $subject = "CN=RAVEN BOT Self-Signed"
  $cert = Get-ChildItem -Path Cert:\CurrentUser\My | Where-Object { $_.Subject -eq $subject } | Select-Object -First 1
  if (-not $cert) {
    Write-Host "Creating new self-signed certificate in CurrentUser\\My..." -ForegroundColor Yellow
    $cert = New-SelfSignedCertificate `
      -Subject $subject `
      -Type CodeSigningCert `
      -KeyAlgorithm RSA `
      -KeyLength 2048 `
      -KeyExportPolicy Exportable `
      -CertStoreLocation "Cert:\\CurrentUser\\My" `
      -NotAfter (Get-Date).AddYears(5)
  }

  # Trust the cert locally so signature validates as "Valid" on this PC.
  # This does NOT make it trusted on other users' PCs.
  try {
    $cerPath = Join-Path $PSScriptRoot "RAVEN_BOT_selfsigned.cer"
    Export-Certificate -Cert $cert -FilePath $cerPath | Out-Null
    Import-Certificate -FilePath $cerPath -CertStoreLocation "Cert:\\CurrentUser\\Root" | Out-Null
    Import-Certificate -FilePath $cerPath -CertStoreLocation "Cert:\\CurrentUser\\TrustedPublisher" | Out-Null
    Write-Host "Local trust installed (CurrentUser\\Root + TrustedPublisher). Cert exported: $cerPath" -ForegroundColor Green
  } catch {
    Write-Host "Warning: could not install local trust: $($_.Exception.Message)" -ForegroundColor Yellow
  }

  $sig = Set-AuthenticodeSignature -FilePath $exePath -Certificate $cert
  if ($sig.Status -ne "Valid") {
    Write-Host "Warning: signature verification status on this PC: $($sig.Status)" -ForegroundColor Yellow
    Write-Host $sig.StatusMessage -ForegroundColor Yellow
  } else {
    Write-Host "Signature: Valid" -ForegroundColor Green
  }
  Write-Host "Signed: $exePath" -ForegroundColor Green
}

# 3) Create zip from dist\RAVEN_BOT
Write-Host "`n[3/4] Creating zip: $zipPath" -ForegroundColor Cyan
if (Test-Path $zipPath) { Remove-Item -Force $zipPath }

Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory($distDir, $zipPath, [System.IO.Compression.CompressionLevel]::Optimal, $false)

# 3) Show GH release command
Write-Host "`n[4/4] Next step (GitHub Release):" -ForegroundColor Cyan
Write-Host "gh release create $Tag .\RAVEN_BOT.zip --title `"$Tag`" --notes `"$Tag`""
Write-Host "`nDone." -ForegroundColor Green

