# Makes the mod package (mod_package\Maximo-GTG-Mods.zip) from this working tree: the launcher, BUILD_GAME.cmd and
# every source file changed from the upstream Maximo-GTG commit this tree is based on - no game code or data.
# Run it with make_mod_package.cmd in the project folder.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $root
$out = Join-Path $root 'mod_package'
$pkg = Join-Path $out 'Maximo-GTG-Mods'
$overlay = Join-Path $pkg 'overlay'

function Run-Git { $r = & git.exe @args; if ($LASTEXITCODE -ne 0) { throw "git $args failed" }; $r }

# The newest upstream commit this tree contains: what BUILD_GAME.cmd checks out on the player's PC.
Run-Git fetch origin --quiet
$base = (Run-Git merge-base HEAD origin/main).Trim()
Write-Host "Based on upstream commit $base"

$allowedExt = '.cpp','.c','.cc','.h','.hpp','.inl','.cs','.cmd','.bat','.py','.md','.txt','.comp','.frag','.vert','.glsl','.csv','.toml','.cmake','.ps1'
$blockedDirs = 'build','dist','generated','deps','disc','game_files','output','research','textures','memcard','logs',
               'screenshots','__pycache__','mod_package','source','app image','background','logo','.git'
# Untracked files are only taken from these places (other folders can hold game files).
$untrackedOk = '^(tools/|launcher/|shaders/|hdtex/|data/|maximo1randomizer/randomizer/|maximo1randomizer/README\.md$|[^/]+\.(cpp|h|hpp|cmd)$)'

function Allowed([string]$rel, [string]$full) {
    if (-not (Test-Path -LiteralPath $full -PathType Leaf)) { return $false }        # deleted
    $parts = $rel.Split('/')
    for ($i = 0; $i -lt $parts.Length - 1; $i++) { if ($blockedDirs -contains $parts[$i].ToLower()) { return $false } }
    $ext = [IO.Path]::GetExtension($rel).ToLower()
    $art = $rel -like 'launcher/art/*.png'
    if (-not $art -and $allowedExt -notcontains $ext) { return $false }
    if ((Get-Item -LiteralPath $full).Length -gt 4MB) { Write-Warning "skipped (over 4 MB): $rel"; return $false }
    return $true
}

$files = New-Object System.Collections.Generic.List[string]
foreach ($f in (Run-Git -c core.quotepath=off diff --name-only $base)) {
    if ($f -like 'external/*') { continue }
    if (Allowed $f (Join-Path $root $f)) { $files.Add($f) }
}
foreach ($f in (Run-Git -c core.quotepath=off ls-files --others --exclude-standard)) {
    if ($f -notmatch $untrackedOk) { continue }
    if (Allowed $f (Join-Path $root $f)) { $files.Add($f) }
}
# The submodules: changed against the commit the base pins them at.
foreach ($sub in 'external/PS2Recomp', 'external/parallel-gs') {
    $pin = ((Run-Git ls-tree $base $sub) -split '\s+')[2]
    $dir = Join-Path $root $sub
    $changed = @(& git.exe -C $dir -c core.quotepath=off diff --name-only $pin) + @(& git.exe -C $dir -c core.quotepath=off ls-files --others --exclude-standard)
    foreach ($f in $changed) {
        if (-not $f) { continue }
        $rel = "$sub/$f"
        if (Allowed $rel (Join-Path $root $rel)) { $files.Add($rel) }
    }
}
$files = $files | Sort-Object -Unique

if (Test-Path $out) { Remove-Item -Recurse -Force $out }
New-Item -ItemType Directory -Force $overlay | Out-Null
foreach ($f in $files) {
    $dst = Join-Path $overlay $f
    New-Item -ItemType Directory -Force (Split-Path -Parent $dst) | Out-Null
    Copy-Item -LiteralPath (Join-Path $root $f) $dst
    Write-Host "  $f"
}
Set-Content -Encoding ascii (Join-Path $overlay 'BASE.txt') $base
Set-Content -Encoding ascii (Join-Path $overlay 'VERSION.txt') (Get-Date -Format 'yyyyMMdd-HHmmss')
Copy-Item (Join-Path $PSScriptRoot 'BUILD_GAME.cmd') $pkg
Copy-Item (Join-Path $PSScriptRoot 'apply_overlay.ps1') $pkg
Copy-Item (Join-Path $PSScriptRoot 'README.txt') $pkg
Copy-Item (Join-Path $root 'LICENSE') $pkg

& cmd /c "`"$root\launcher\build.cmd`" `"$pkg`""
if ($LASTEXITCODE -ne 0 -or -not (Test-Path (Join-Path $pkg 'MaximoLauncher.exe'))) { throw 'the launcher did not build' }

$zip = Join-Path $out 'Maximo-GTG-Mods.zip'
Compress-Archive -Path (Join-Path $pkg '*') -DestinationPath $zip
Write-Host ""
Write-Host "$($files.Count) changed files packed. Send this file: $zip"
