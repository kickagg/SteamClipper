<#
    steam-export.ps1 - lista, previsualiza e exporta gravacoes do Steam Game Recording.

    COMO OS ARQUIVOS FUNCIONAM
    As gravacoes sao segmentos MPEG-DASH de 3s. init-stream0.m4s / init-stream1.m4s
    guardam o cabecalho (ftyp+moov) de video e audio; cada chunk-streamN-NNNNN.m4s
    guarda um fragmento (moof+mdat). init + chunks concatenados = fMP4 valido.

    POR QUE NAO USAR O session.mpd
    Quando o buffer circular poda os segmentos antigos, o manifesto continua dizendo
    startNumber="1" e anunciando a duracao original. Players que seguem o .mpd vao
    pedir chunks que nao existem mais. Ler os chunks reais do disco sempre funciona.

    DUAS CORRECOES QUE OS PRESETS APLICAM
      1. VFR -> CFR. A Steam grava com framerate variavel (frames de 14,7 a 15,9 ms).
         Premiere e After Effects dessincronizam audio com VFR; -fps_mode cfr resolve.
      2. Full range -> limited. O stream e yuvj420p / color_range=pc (luma 0-255).
         NLEs assumem 16-235 e estouram o contraste. Precisa de format=yuv420p JUNTO
         com scale=in_range=full:out_range=limited - o scale sozinho nao faz nada.

    USO
      .\steam-export.ps1                                        # lista as sessoes
      .\steam-export.ps1 -Session bg_.. -Preset raw             # remux HEVC, instantaneo
      .\steam-export.ps1 -Session bg_.. -Start 1800 -Duration 90
      .\steam-export.ps1 -Session bg_.. -Preset edit -Start 600 -Duration 30
      .\steam-export.ps1 -Session bg_.. -Start 1800 -Duration 60 -Play
#>

[CmdletBinding()]
param(
    [string] $Session,
    [double] $Start    = 0,       # segundos desde o inicio do material disponivel
    [double] $Duration = 0,       # 0 = ate o fim

    # raw     = remux HEVC, sem reencode. Instantaneo. Arquivar / assistir no mpv.
    # deliver = H.264 CFR 60fps, limited range. mpv, Premiere, AE, YouTube. (padrao)
    # edit    = DNxHR HQ .mov + audio PCM. Grading/efeitos pesados. ~5,6 GB/min.
    [ValidateSet('raw','deliver','edit')]
    [string] $Preset = 'deliver',

    [int]    $Quality = 19,       # cq do NVENC no preset deliver: menor = melhor
    [switch] $Play,               # abre no mpv.net ao terminar

    [string] $RecordRoot,         # vazio = descobre pelo localconfig.vdf do Steam
    [string] $OutDir,
    [string] $Mpv        = "$env:LOCALAPPDATA\Programs\mpv.net\mpvnet.exe"
)

# --- descoberta automatica dos caminhos -------------------------------------
# O Steam guarda a pasta escolhida em GameRecording/BackgroundRecordPath; ler de
# la faz o script acompanhar quando voce muda a pasta nas opcoes.
function Find-SteamPaths {
    $steam = $null
    foreach ($k in @("HKCU:\SOFTWARE\Valve\Steam", "HKLM:\SOFTWARE\WOW6432Node\Valve\Steam")) {
        if (Test-Path $k) {
            $p = Get-ItemProperty $k -ErrorAction SilentlyContinue
            foreach ($v in @($p.SteamPath, $p.InstallPath)) {
                if ($v -and (Test-Path $v)) { $steam = $v; break }
            }
        }
        if ($steam) { break }
    }
    if (-not $steam) { $steam = "C:\Program Files (x86)\Steam" }

    $rec = $null; $out = $null
    Get-ChildItem (Join-Path $steam "userdata") -Directory -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | ForEach-Object {
            $cfg = Join-Path $_.FullName "config\localconfig.vdf"
            if ((-not $rec) -and (Test-Path $cfg)) {
                $txt = Get-Content $cfg -Raw -ErrorAction SilentlyContinue
                if ($txt -match '"BackgroundRecordPath"\s+"([^"]+)"') {
                    $rec = $matches[1] -replace '\\\\', '\'
                }
                if ($txt -match '"ExportDirectory"\s+"([^"]+)"') {
                    $out = $matches[1] -replace '\\\\', '\'
                }
            }
        }
    [pscustomobject]@{ Record = $rec; Out = $out }
}

if (-not $RecordRoot -or -not $OutDir) {
    $found = Find-SteamPaths
    if (-not $RecordRoot) { $RecordRoot = $found.Record }
    if (-not $OutDir)     { $OutDir     = if ($found.Out) { $found.Out } else { Join-Path $HOME "Videos\SteamClipper" } }
}
if (-not $RecordRoot -or -not (Test-Path $RecordRoot)) {
    throw "Pasta de gravacoes nao encontrada. Passe -RecordRoot <caminho>."
}

$ErrorActionPreference = 'Stop'
$SEG = 3.0   # SegmentTemplate duration=3000000 / timescale=1000000

function Get-Chunks {
    param([string] $Dir, [int] $Stream)
    Get-ChildItem $Dir -Filter "chunk-stream$Stream-*.m4s" -ErrorAction SilentlyContinue |
        Sort-Object Name | Select-Object -ExpandProperty FullName
}

# --- listagem ---------------------------------------------------------------
if (-not $Session) {
    $videoRoot = Join-Path $RecordRoot 'video'
    $rows = foreach ($dir in Get-ChildItem $videoRoot -Directory -ErrorAction SilentlyContinue) {
        $chunks = @(Get-Chunks $dir.FullName 0)
        if (-not $chunks) { continue }

        $first = [int](([System.IO.Path]::GetFileNameWithoutExtension($chunks[0])) -split '-')[-1]
        $bytes = (Get-ChildItem $dir.FullName -Filter *.m4s | Measure-Object Length -Sum).Sum
        $secs  = $chunks.Count * $SEG
        $res   = '?'
        $mpd   = Join-Path $dir.FullName 'session.mpd'
        if (Test-Path $mpd) {
            $x = Get-Content $mpd -Raw
            if ($x -match 'width="(\d+)" height="(\d+)"') { $res = "$($matches[1])x$($matches[2])" }
        }

        [pscustomobject]@{
            Sessao     = $dir.Name
            AppID      = if ($dir.Name -match '^bg_(\d+)_') { $matches[1] } else { '?' }
            Disponivel = [timespan]::FromSeconds($secs).ToString('hh\:mm\:ss')
            Resolucao  = $res
            GB         = [math]::Round($bytes / 1GB, 2)
            'GB/h'     = if ($secs) { [math]::Round($bytes / 1GB / ($secs / 3600), 1) } else { 0 }
            Estado     = if ($first -eq 1) { 'intacta' } else { "podada (-$([math]::Round(($first-1)*$SEG/60)) min)" }
        }
    }

    if (-not $rows) { Write-Warning "Nenhuma sessao em $videoRoot"; return }
    $rows | Sort-Object Sessao | Format-Table -AutoSize
    Write-Host "Exportar:  .\steam-export.ps1 -Session <nome> [-Preset raw|deliver|edit] [-Start seg] [-Duration seg] [-Play]"
    return
}

# --- exportacao -------------------------------------------------------------
$srcDir = Join-Path (Join-Path $RecordRoot 'video') $Session
if (-not (Test-Path $srcDir)) { throw "Sessao nao encontrada: $srcDir" }

$vChunks = @(Get-Chunks $srcDir 0)
$aChunks = @(Get-Chunks $srcDir 1)
if (-not $vChunks) { throw "Nenhum chunk de video em $srcDir" }

# Recorta na granularidade do segmento; o ajuste fino (< 3s) fica com o ffmpeg.
# Sem isso, exportar 90s de uma sessao de 1h montaria os 9 GB inteiros.
$skip   = [math]::Max(0, [int][math]::Floor($Start / $SEG))
$fineSs = $Start - ($skip * $SEG)
$take   = if ($Duration -gt 0) { [int][math]::Ceiling(($fineSs + $Duration) / $SEG) + 1 } else { $vChunks.Count }

if ($skip -ge $vChunks.Count) {
    throw "-Start $Start s passa do material disponivel ($([math]::Round($vChunks.Count * $SEG))s)."
}

$vSel = $vChunks | Select-Object -Skip $skip -First $take
$aSel = $aChunks | Select-Object -Skip $skip -First $take

$tmp = Join-Path $env:TEMP "steamexport_$([guid]::NewGuid().ToString('N').Substring(0,8))"
New-Item -ItemType Directory -Path $tmp -Force | Out-Null

function Join-Stream {
    param([string] $Init, [string[]] $Parts, [string] $Out)
    $fs = [System.IO.File]::Create($Out)
    try {
        foreach ($p in @($Init) + $Parts) {
            $b = [System.IO.File]::ReadAllBytes($p)
            $fs.Write($b, 0, $b.Length)
        }
    } finally { $fs.Dispose() }
}

try {
    Write-Host "Montando $($vSel.Count) segmentos (~$([math]::Round($vSel.Count * $SEG))s)..." -ForegroundColor DarkGray
    $vFile = Join-Path $tmp 'v.mp4'
    Join-Stream (Join-Path $srcDir 'init-stream0.m4s') $vSel $vFile

    $aFile = $null
    if ($aSel) {
        $aFile = Join-Path $tmp 'a.mp4'
        Join-Stream (Join-Path $srcDir 'init-stream1.m4s') $aSel $aFile
    }

    if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }

    $ext     = if ($Preset -eq 'edit') { 'mov' } else { 'mp4' }
    $tag     = if ($Duration -gt 0) { "_{0:d}s-{1:d}s" -f [int]$Start, [int]($Start + $Duration) } else { '' }
    $outFile = Join-Path $OutDir "$Session$tag`_$Preset.$ext"

    $a = @('-y', '-hide_banner', '-v', 'warning', '-stats')
    if ($Preset -ne 'edit') { $a += @('-hwaccel', 'cuda') }
    $a += @('-i', $vFile)
    if ($aFile) { $a += @('-i', $aFile) }
    if ($fineSs -gt 0.01) { $a += @('-ss', $fineSs) }
    if ($Duration -gt 0)  { $a += @('-t',  $Duration) }

    # format=yuv420p precisa vir ANTES do scale: sozinho o scale mantem yuvj420p
    # (full range) e a conversao de niveis nao acontece.
    $toLimited = 'format=yuv420p,scale=in_range=full:out_range=limited'

    switch ($Preset) {
        'raw' {
            $a += @('-c', 'copy')
        }
        'deliver' {
            $a += @('-fps_mode','cfr','-r','60','-vf',$toLimited,
                    '-c:v','h264_nvenc','-preset','p6','-rc','vbr','-cq',"$Quality",'-b:v','0',
                    '-profile:v','high','-color_range','tv','-colorspace','bt709',
                    '-color_primaries','bt709','-color_trc','bt709')
            if ($aFile) { $a += @('-c:a','aac','-b:a','192k') }
        }
        'edit' {
            $a += @('-fps_mode','cfr','-r','60','-vf',"$toLimited,format=yuv422p",
                    '-c:v','dnxhd','-profile:v','dnxhr_hq')
            if ($aFile) { $a += @('-c:a','pcm_s16le') }
        }
    }
    if ($ext -eq 'mp4') { $a += @('-movflags', '+faststart') }
    $a += $outFile

    Write-Host "-> $outFile" -ForegroundColor Cyan
    & ffmpeg @a
    if ($LASTEXITCODE -ne 0) { throw "ffmpeg falhou (exit $LASTEXITCODE)" }
    if (-not (Test-Path $outFile) -or (Get-Item $outFile).Length -lt 1024) { throw "Saida vazia." }

    Write-Host "OK - $([math]::Round((Get-Item $outFile).Length / 1MB, 1)) MB" -ForegroundColor Green
    if ($Play) {
        if (Test-Path $Mpv) { & $Mpv $outFile }
        else { Write-Warning "mpv nao encontrado em $Mpv" }
    }
}
finally {
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
