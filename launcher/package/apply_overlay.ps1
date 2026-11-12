# Puts the package's overlay over .\source, copying only files whose contents differ, and gives each copied file the
# current time - so the next build recompiles exactly what changed (an unchanged file keeps its time and is skipped).
param([string]$Overlay, [string]$Repo)
$ErrorActionPreference = 'Stop'
$Overlay = (Resolve-Path -LiteralPath $Overlay).Path.TrimEnd('\')
$n = 0
foreach ($f in Get-ChildItem -LiteralPath $Overlay -Recurse -File) {
    if ($f.DirectoryName -eq $Overlay -and ($f.Name -eq 'VERSION.txt' -or $f.Name -eq 'BASE.txt')) { continue }
    $rel = $f.FullName.Substring($Overlay.Length + 1)
    $dst = Join-Path $Repo $rel
    if ((Test-Path -LiteralPath $dst) -and (Get-FileHash -LiteralPath $dst).Hash -eq (Get-FileHash -LiteralPath $f.FullName).Hash) { continue }
    New-Item -ItemType Directory -Force (Split-Path -Parent $dst) | Out-Null
    Copy-Item -LiteralPath $f.FullName $dst -Force
    (Get-Item -LiteralPath $dst).LastWriteTime = Get-Date
    Write-Host "  changed: $rel"
    $n++
}
Write-Host "$n file(s) changed"
