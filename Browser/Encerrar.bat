@echo off
title Encerrar SteamClipper
cd /d "%~dp0"

echo.
echo   Encerrando o SteamClipper...
echo.

REM 1) Pedido educado: o servidor fecha o player e sai sozinho.
REM    Precisa ser POST - e assim que a rota esta registrada.
powershell -NoProfile -Command ^
  "try { Invoke-RestMethod 'http://127.0.0.1:8777/api/shutdown' -Method Post -TimeoutSec 3 | Out-Null; '  painel encerrado' } catch { '  painel nao respondeu (pode ja estar fechado)' }"

REM ping no lugar de timeout: 'timeout' aborta quando a entrada esta redirecionada.
ping -n 2 127.0.0.1 >nul

REM 2) Rede de seguranca: mata so o servidor, se ele nao tiver saido sozinho.
REM    A janela do mpv vive DENTRO deste processo (libmpv), entao fecha junto.
REM    De proposito nao mexemos em mpvnet.exe: seria o seu mpv aberto a parte.
powershell -NoProfile -Command ^
  "$n=0; Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" | Where-Object { $_.CommandLine -like '*run.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -EA SilentlyContinue; $n++ }; if ($n) { \"  $n processo(s) encerrado(s) a forca\" } else { \"  nada sobrando para encerrar\" }"

powershell -NoProfile -Command ^
  "if ((Get-NetTCPConnection -LocalPort 8777 -State Listen -EA SilentlyContinue | Measure-Object).Count -eq 0) { '  porta 8777 liberada' } else { '  ATENCAO: a porta 8777 continua ocupada' }"

echo.
echo   Pronto.
ping -n 3 127.0.0.1 >nul
