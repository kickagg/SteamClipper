# SteamClipper

Painel para revisar e recortar as gravações do **Steam Game Recording** sem passar
pelo cliente da Steam. Lê os segmentos DASH direto do disco, reproduz no mpv sem
recomprimir nada e exporta trechos prontos para editor.

Duas interfaces sobre o mesmo núcleo:

| | | |
|---|---|---|
| **Browser/** | painel no navegador, player em janela separada | funciona em qualquer lugar |
| **Desktop/** | janela única com o mpv renderizando dentro | vídeo embutido na interface |

## Requisitos

- Windows, Python 3.10+
- `ffmpeg` no PATH — `winget install Gyan.FFmpeg`
- `mpv.net` para a reprodução — `winget install mpvnet.mpvnet`
  (o que importa é a `libmpv-2.dll` que vem junto)

Nada de `pip install`: só a biblioteca padrão.

## Uso

```
Browser\Abrir.bat        painel em http://127.0.0.1:8777
Browser\Encerrar.bat     desliga o servidor e o player
Desktop\Abrir.bat        janela única com o vídeo dentro
```

Clique numa gravação, arraste na linha do tempo para marcar o trecho, escolha o
preset e exporte. Atalhos: `espaço` play, `i` / `o` marcam início e fim, `←` `→` 5s.

## Presets

| Preset | Saída | Para quê |
|---|---|---|
| **Entrega** | H.264 High, CFR 60 fps, resolução original | qualquer player, Premiere, After Effects, YouTube |
| **Original** | HEVC copiado, sem reprocessar | arquivar sem perda (mantém VFR — evite no Premiere) |
| **Compacto** | H.265 em 1080p, compressão alta | enviar por Discord ou WhatsApp (~4× menor) |
| **Edição** | DNxHR HQ `.mov` + PCM | grading pesado (~5,6 GB/min) |

O modal de exportação abre com o preset escolhido e deixa ajustar tudo à mão:
codec, contêiner, resolução, taxa de quadros, qualidade (CQ do NVENC, com nome)
ou bitrate fixo, além da pasta de destino — que fica guardada como padrão.

A estimativa de tamanho vem de medições reais do encoder nesta máquina, não de
chute: o erro fica abaixo de 7% nas combinações testadas. Uma exportação em
andamento pode ser cancelada; o ffmpeg é encerrado e o arquivo pela metade é
apagado.

## Como funciona

As gravações são segmentos MPEG-DASH de 3 segundos: `init-stream0/1.m4s` guardam o
cabeçalho e cada `chunk-streamN-NNNNN.m4s` guarda um fragmento. O projeto resolve
quatro problemas que aparecem ao lidar com esse material:

**O manifesto mente.** Quando o buffer circular apaga os segmentos antigos, o
`session.mpd` continua declarando `startNumber="1"` e a duração original — um player
que o siga pede arquivos que não existem mais. Por isso os chunks reais são lidos do
disco e o `.mpd` é ignorado.

**Sem índice não há navegação.** As durações no `moov` vêm zeradas e não há `sidx`,
então o player não sabe o tamanho do vídeo. O núcleo reescreve as durações e **gera
um `sidx`** a partir dos tamanhos reais dos chunks. Resultado: seek instantâneo mesmo
numa sessão de 27 GB, sem montar arquivo nenhum em disco.

**O vídeo é VFR.** O Steam grava com framerate variável (quadros de 14,7 a 15,9 ms).
Premiere e After Effects dessincronizam o áudio com isso, então os presets de edição
convertem para CFR.

**As cores são full range.** O stream é `yuvj420p` / `color_range=pc` (luma 0-255) e
os editores assumem 16-235, estourando o contraste. A conversão exige
`format=yuv420p` **junto** do `scale` — o filtro `scale` sozinho não converte nada.

O player é o mpv de verdade: `mpv.net` não é o `mpv.exe`, é um frontend .NET sobre
`libmpv-2.dll`, e essa DLL é conduzida por `ctypes`. Ele lê o HEVC 1440p original com
decodificação por hardware e o primeiro quadro aparece em ~0,4 s em qualquer ponto.

## Configuração

Nenhuma. Os caminhos saem do registro do Windows e do próprio `localconfig.vdf` do
Steam — inclusive o `BackgroundRecordPath`, então mudar a pasta de gravação nas
opções do Steam é acompanhado automaticamente. Os nomes dos jogos vêm do
`appinfo.vdf`, que cobre também os títulos desinstalados.

```
steamclipper/
  config.py    descoberta de caminhos
  steam.py     sessões, nomes dos jogos, marcadores
  media.py     fMP4 virtual (sidx + Range), miniaturas
  export.py    presets e fila de exportação
  player.py    mpv via libmpv
Browser/       servidor + UI web
Desktop/       app em tkinter com o mpv embutido
Cli/           script PowerShell original (ffmpeg direto)
```

## Onde os arquivos são salvos

Por padrão, o `ExportDirectory` do próprio Steam. Ao escolher outra pasta no modal
ela vira o padrão e fica em `%LOCALAPPDATA%\SteamClipper\settings.json`. Se a
pasta configurada no Steam não existir mais, o app cai em `Vídeos\SteamClipper`.

## Limitação conhecida

Na versão Browser o player abre **fora** da página. Não é escolha de design: uma aba
de navegador não pode hospedar uma janela nativa. Para o vídeo dentro da interface,
use a versão Desktop.
