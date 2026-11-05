param([int]$Delay = 116)
$out = "results\step\step11_stats.txt"
"" | Out-File $out
Start-Sleep -Seconds $Delay

for ($i = 1; $i -le 3; $i++) {
    "--- замір $i, $(Get-Date -Format HH:mm:ss) ---" | Tee-Object -FilePath $out -Append

    $t1 = Get-Date
    $p1 = Get-Process python -ErrorAction SilentlyContinue | Select-Object Id, CPU
    docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}" |
        Tee-Object -FilePath $out -Append
    $p2 = Get-Process python -ErrorAction SilentlyContinue | Select-Object Id, CPU
    $dt = ((Get-Date) - $t1).TotalSeconds

    $d = 0
    foreach ($a in $p1) {
        $b = $p2 | Where-Object Id -eq $a.Id
        if ($b) { $d += ($b.CPU - $a.CPU) }
    }
    ("CPU генератора (python): {0:N0} % одного ядра" -f ($d / $dt * 100)) |
        Tee-Object -FilePath $out -Append

    $host_cpu = (Get-CimInstance Win32_Processor | Measure-Object LoadPercentage -Average).Average
    "CPU хоста загалом: $host_cpu %" | Tee-Object -FilePath $out -Append

    $q = 'sum by (instance) (rate(process_cpu_seconds_total{instance=~".*:8000"}[15s]))'
    try {
        $r = Invoke-RestMethod ("http://localhost:9090/api/v1/query?query=" + [uri]::EscapeDataString($q))
        foreach ($s in $r.data.result) {
            ("web {0}: {1:N2} ядра" -f $s.metric.instance, [double]$s.value[1]) |
                Tee-Object -FilePath $out -Append
        }
    } catch { "Prometheus недоступний" | Tee-Object -FilePath $out -Append }
}