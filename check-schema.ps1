$ErrorActionPreference = "Stop"
Write-Host "Building site..." -ForegroundColor Cyan
hugo --cleanDestinationDir --quiet
$root = Join-Path (Get-Location) "public"
$rx = [regex]'(?s)<script[^>]*application/ld\+json[^>]*>(.*?)</script>'
$bad = 0
$total = 0
Get-ChildItem -Path $root -Filter *.html -Recurse | ForEach-Object {
    $f = $_
    $html = Get-Content -LiteralPath $f.FullName -Raw
    foreach ($m in $rx.Matches($html)) {
        $total++
        $json = $m.Groups[1].Value.Trim()
        try { $null = ConvertFrom-Json -InputObject $json -ErrorAction Stop }
        catch {
            $bad++
            Write-Host "BROKEN: $($f.FullName.Substring($root.Length))" -ForegroundColor Red
            Write-Host "ERROR:  $($_.Exception.Message)"
            Write-Host "JSON:   $($json -replace '\s+', ' ')"
            Write-Host ("-" * 70)
        }
    }
}
Write-Host ""
Write-Host "Scanned $total block(s), $bad invalid." -ForegroundColor Cyan
