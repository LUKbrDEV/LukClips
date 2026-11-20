#!/usr/bin/env python3
"""
LukClips - Fábrica de cortes automática
Feito por LukDev

Cola o link (YouTube, Twitch, Kick...) ou escolhe um vídeo do PC,
e o LukClips baixa, transcreve com Whisper, acha os melhores momentos
e gera cortes verticais 1080x1920 prontos para TikTok / Reels / Shorts.
"""
import json
import logging
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import wave
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))))
import momentos as M  # noqa: E402
import visual as V  # noqa: E402

APP = "LukClips"
VERSAO = "3.1.0"
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

# No .exe sem console, stdout/stderr não existem e barras de progresso de
# bibliotecas (ex.: download do modelo Whisper) quebrariam o programa.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

# ============================================================================
# Pastas e binários (ffmpeg, ffprobe, deno ficam ao lado do .exe)
# ============================================================================
if getattr(sys, "frozen", False):
    PASTA_APP = Path(sys.executable).parent
    PASTA_RECURSOS = Path(getattr(sys, "_MEIPASS", PASTA_APP))
else:
    PASTA_APP = Path(__file__).resolve().parent
    PASTA_RECURSOS = PASTA_APP

# coloca a pasta do app no PATH: assim o yt-dlp acha o deno e o ffmpeg embutidos
os.environ["PATH"] = str(PASTA_APP) + os.pathsep + os.environ.get("PATH", "")

if os.name == "nt":
    PASTA_CONFIG = Path(os.environ.get("APPDATA", Path.home())) / APP
else:
    PASTA_CONFIG = Path.home() / ".config" / APP
ARQ_CONFIG = PASTA_CONFIG / "config.json"
PASTA_CACHE_UI = PASTA_CONFIG / "ui"

SEM_JANELA = {"stdin": subprocess.DEVNULL}
if os.name == "nt":
    SEM_JANELA["creationflags"] = 0x08000000  # CREATE_NO_WINDOW


def _json_seguro(o):
    """Converte números do numpy (int64, float32...) para gravar em JSON."""
    if hasattr(o, "item"):
        return o.item()
    if hasattr(o, "tolist"):
        return o.tolist()
    return str(o)


def _registrar_cuda():
    """Se as bibliotecas da NVIDIA (pip nvidia-cublas-cu12 / nvidia-cudnn-cu12) estiverem
    instaladas, ou ao lado do .exe, avisa o Windows onde estão as DLLs."""
    if os.name != "nt":
        return
    pastas = [PASTA_APP, PASTA_APP / "cuda"]
    try:
        import site
        for base in site.getsitepackages() + [site.getusersitepackages()]:
            nv = Path(base) / "nvidia"
            if nv.is_dir():
                pastas += [p for p in nv.glob("*/bin") if p.is_dir()]
    except Exception:
        pass
    for p in pastas:
        try:
            if p.is_dir():
                os.add_dll_directory(str(p))
                os.environ["PATH"] = str(p) + os.pathsep + os.environ.get("PATH", "")
        except Exception:
            pass


_registrar_cuda()


def cuda_disponivel():
    """Placa NVIDIA presente E a biblioteca cuBLAS carrega? (senão nem tenta a GPU)."""
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() <= 0:
            return False
    except Exception:
        return False
    if os.name != "nt":
        return True
    import ctypes
    for nome in ("cublas64_12.dll", "cublas64_11.dll"):
        try:
            ctypes.WinDLL(nome)
            return True
        except OSError:
            continue
    logging.info("NVIDIA encontrada, mas sem cublas64_12.dll: usando o processador")
    return False


def achar_bin(nome):
    for cand in (PASTA_APP / f"{nome}.exe", PASTA_APP / nome):
        if cand.exists():
            return str(cand)
    return shutil.which(nome)


# ============================================================================
# Plataformas
# ============================================================================
PLATAFORMAS = [
    (r"(youtube\.com|youtu\.be)", "YouTube", "#FF0033"),
    (r"twitch\.tv", "Twitch", "#9146FF"),
    (r"kick\.com", "Kick", "#53FC18"),
    (r"tiktok\.com", "TikTok", "#25F4EE"),
    (r"instagram\.com", "Instagram", "#E1306C"),
    (r"(facebook\.com|fb\.watch)", "Facebook", "#1877F2"),
    (r"(twitter\.com|x\.com)", "X / Twitter", "#E7E9EA"),
    (r"vimeo\.com", "Vimeo", "#1AB7EA"),
]


def detectar_plataforma(texto):
    t = texto.strip().strip('"')
    if not t:
        return None, "#555"
    if re.match(r"^https?://", t, re.I):
        for padrao, nome, cor in PLATAFORMAS:
            if re.search(padrao, t, re.I):
                return nome, cor
        return "Link", "#AAAAAA"
    if Path(t).is_file():
        return "Arquivo do PC", "#FFB020"
    return "Inválido", "#FF5555"


# ============================================================================
# Motor: a fábrica de cortes
# ============================================================================
LARG, ALT = 1080, 1920
ALT_CAM = ALT // 2
ALT_GAME = ALT - ALT_CAM
FONTE = "Arial Black"
PALAVRAS_POR_LEGENDA = 3

# layout: (onde fica a câmera, fração da altura que ela ocupa)
LAYOUTS = {
    "Câmera em cima (50/50)": ("cima", 0.5),
    "Câmera em cima (40/60)": ("cima", 0.4),
    "Câmera embaixo (50/50)": ("baixo", 0.5),
    "Sem câmera (fundo desfocado)": (None, 0),
}
LAYOUT_PADRAO = "Câmera em cima (50/50)"

# cores no formato do ASS: &HAABBGGRR
ESTILOS_LEGENDA = {
    "Clássico (branco + amarelo)": dict(fonte="Arial Black", tam=86, cor="&H00FFFFFF", destaque="&H0000FFFF",
                                         contorno="&H00000000", borda=7, sombra=3, caixa=False,
                                         maiusc=True, palavras=3),
    "Neon roxo": dict(fonte="Arial Black", tam=84, cor="&H00FFFFFF", destaque="&H00FA8BA7",
                      contorno="&H00951D4C", borda=8, sombra=0, caixa=False, maiusc=True, palavras=3),
    "Verde gamer": dict(fonte="Arial Black", tam=86, cor="&H00FFFFFF", destaque="&H0018FC53",
                        contorno="&H00000000", borda=7, sombra=3, caixa=False, maiusc=True, palavras=3),
    "Caixa preta": dict(fonte="Arial Black", tam=74, cor="&H00FFFFFF", destaque="&H0000FFFF",
                        contorno="&H30000000", borda=14, sombra=0, caixa=True, maiusc=True, palavras=3),
    "Minimalista": dict(fonte="Segoe UI Semibold", tam=66, cor="&H00FFFFFF", destaque="&H0080E0FF",
                        contorno="&H00000000", borda=4, sombra=2, caixa=False, maiusc=False, palavras=5),
}
ESTILO_PADRAO = "Clássico (branco + amarelo)"

def detectar_camera(video, w, h, t_ini=0.0, t_fim=None, amostras=24, minimo=4, aviso=print):
    """Procura o rosto do streamer em vários frames (entre t_ini e t_fim) e devolve a
    caixa da facecam. Usa o layout que mais aparece no trecho, porque a live pode
    trocar de cena (câmera maior, menor ou em outro lugar)."""
    import cv2
    if not hasattr(cv2, "CascadeClassifier"):
        aviso("  ⚠ Seu OpenCV não tem detector de rosto: pip install \"opencv-python<5\"")
        return None
    casc = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    cap = cv2.VideoCapture(str(video))
    if t_fim is None:
        fps = cap.get(cv2.CAP_PROP_FPS) or 30
        t_fim = cap.get(cv2.CAP_PROP_FRAME_COUNT) / fps
    achados = []
    for k in range(amostras):
        t = t_ini + (t_fim - t_ini) * (k + 0.5) / amostras
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, f = cap.read()
        if not ok:
            continue
        g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
        for (x, y, fw, fh) in casc.detectMultiScale(g, 1.1, 6, minSize=(40, 40)):
            # facecam fica perto das bordas; personagens do jogo ficam mais no centro
            borda = min(x, w - x - fw, y, h - y - fh) < w * 0.12
            if borda and fw < w * 0.25:
                achados.append((x + fw / 2, y + fh / 2, fw))
    cap.release()
    if len(achados) < minimo:
        return None
    a = np.array(achados)
    # agrupa detecções parecidas e fica com o grupo maior (o layout mais usado)
    grupos = [(np.abs(a[:, 0] - x) < w * 0.06) & (np.abs(a[:, 1] - y) < h * 0.08)
              & (np.abs(a[:, 2] / fw - 1) < 0.3) for x, y, fw in a]
    melhor = max(grupos, key=lambda m: m.sum())
    if melhor.sum() < minimo:
        return None
    cx, cy, fw = np.median(a[melhor], axis=0)
    # caixa ~ 3.2x a largura do rosto, proporção 9:8 (= 1080x960)
    cw = min(w, fw * 3.2)
    ch = min(h, cw * ALT_CAM / LARG)
    x = int(np.clip(cx - cw / 2, 0, w - cw))
    y = int(np.clip(cy - ch * 0.45, 0, h - ch))
    return [x, y, int(cw), int(ch)]


def recorte_gameplay(w, h, cam, alt_game=None):
    """Recorte da gameplay, fugindo da câmera se ela estiver no meio."""
    alt_game = alt_game or ALT_GAME
    gh = h
    gw = int(gh * LARG / alt_game)
    if gw > w:
        gw, gh = w, int(w * alt_game / LARG)
    gx, gy = (w - gw) // 2, (h - gh) // 2
    if cam:
        c0, c1 = cam[0], cam[0] + cam[2]
        if c0 < gx + gw and c1 > gx:  # câmera aparece dentro da gameplay
            novo = c0 - gw if (c0 + c1) / 2 > w / 2 else c1
            if abs(novo - gx) <= w * 0.2:
                gx = int(np.clip(novo, 0, w - gw))
    return gx, gy, gw, gh


class Cancelado(Exception):
    pass


def ts(t):
    t = max(0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def nome_seguro(txt, maximo=50):
    txt = re.sub(r'[\\/:*?"<>|\n\r\t]+', " ", txt).strip()
    txt = re.sub(r"\s+", " ", txt)
    return (txt[:maximo].strip() or "video").rstrip(".")


class Fabrica:
    """Faz todo o trabalho. A interface só passa as funções de log/progresso."""

    def __init__(self, cfg, log=print, progresso=lambda p, s: None,
                 pedir_camera=None, cancel=None):
        self.cfg = cfg
        self.log = log
        self.prog = progresso
        self.pedir_camera = pedir_camera
        self.pedir_revisao = None
        self.cancel = cancel or threading.Event()
        self.proc = None
        self.ffmpeg = achar_bin("ffmpeg")
        self.ffprobe = achar_bin("ffprobe")

    # ---------------------------------------------------------------- util
    def checar(self):
        if self.cancel.is_set():
            raise Cancelado()

    def sh(self, args, cwd=None, progresso_dur=None, faixa=None):
        """Roda um comando sem abrir janela. Pode mostrar progresso do ffmpeg."""
        self.checar()
        if progresso_dur:
            args = args[:1] + ["-progress", "pipe:1", "-nostats"] + args[1:]
        self.proc = subprocess.Popen(args, cwd=cwd, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True,
                                     encoding="utf-8", errors="replace", **SEM_JANELA)
        erros = []
        t_err = threading.Thread(target=lambda: erros.extend(self.proc.stderr), daemon=True)
        t_err.start()
        for linha in self.proc.stdout:
            if self.cancel.is_set():
                self.proc.kill()
                raise Cancelado()
            if progresso_dur and linha.startswith("out_time_ms=") and faixa:
                try:
                    seg = int(linha.split("=")[1]) / 1e6
                    a, b, txt = faixa
                    self.prog(a + (b - a) * min(1, seg / progresso_dur), txt)
                except ValueError:
                    pass
        self.proc.wait()
        t_err.join(timeout=2)
        if self.cancel.is_set():
            raise Cancelado()
        if self.proc.returncode != 0:
            raise RuntimeError("Erro no ffmpeg:\n" + "".join(erros[-15:]))

    def duracao(self, video):
        out = subprocess.check_output([self.ffprobe, "-v", "error", "-show_entries",
                                       "format=duration", "-of", "default=nw=1:nk=1",
                                       str(video)], **SEM_JANELA)
        return float(out.strip())

    def possui_audio(self, video):
        try:
            out = subprocess.check_output([self.ffprobe, "-v", "error", "-select_streams", "a",
                                           "-show_entries", "stream=index", "-of", "csv=p=0",
                                           str(video)], **SEM_JANELA)
            return bool(out.strip())
        except Exception:
            return True

    def tamanho(self, video):
        out = subprocess.check_output([self.ffprobe, "-v", "error", "-select_streams", "v:0",
                                       "-show_entries", "stream=width,height", "-of", "csv=p=0",
                                       str(video)], **SEM_JANELA)
        w, h = out.decode().strip().split(",")[:2]
        return int(w), int(h)

    # ------------------------------------------------------------ download
    def baixar(self, url, pasta_trab_base):
        import yt_dlp

        class Logger:
            def __init__(s):
                s.ultimo = ""

            def debug(s, msg):
                if msg.startswith("[download]"):
                    return
                if not msg.startswith("[debug]"):
                    self.log(msg)

            def info(s, msg):
                s.debug(msg)

            def warning(s, msg):
                self.log("⚠ " + msg)

            def error(s, msg):
                self.log("❌ " + msg)

        opts = {"logger": Logger(), "quiet": True, "noplaylist": True}
        if self.cfg.get("navegador") and self.cfg["navegador"] != "Nenhum":
            opts["cookiesfrombrowser"] = (self.cfg["navegador"].lower(),)
        if self.ffmpeg:
            opts["ffmpeg_location"] = str(Path(self.ffmpeg).parent)

        self.prog(1, "Lendo informações do vídeo...")
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        vid = nome_seguro(f"{info.get('extractor_key', 'web')}_{info.get('id', 'video')}", 80)
        pasta = pasta_trab_base / vid
        pasta.mkdir(parents=True, exist_ok=True)
        meta = {"titulo": info.get("title") or "video",
                "canal": info.get("uploader") or info.get("channel") or "",
                "extrator": info.get("extractor_key", ""), "id": info.get("id", ""),
                "ao_vivo": info.get("live_status") in ("was_live", "post_live")
                or bool(info.get("was_live")) or info.get("extractor_key") == "TwitchVod",
                "heatmap": info.get("heatmap") or [], "url": url}
        (pasta / "info.json").write_text(json.dumps(meta, ensure_ascii=False, default=_json_seguro), encoding="utf-8")

        prontos = [p for p in pasta.glob("vod.*") if p.suffix in (".mp4", ".mkv", ".webm")
                   and ".part" not in p.name and not re.search(r"\.f\d+\.", p.name)]
        if prontos:
            self.log("✔ Vídeo já baixado antes, reaproveitando.")
            return prontos[0], pasta, meta

        def hook(d):
            if self.cancel.is_set():
                raise Cancelado()
            if d["status"] == "downloading":
                tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                feito = d.get("downloaded_bytes", 0)
                vel = d.get("speed") or 0
                if tot:
                    pct = feito / tot
                    self.prog(2 + 23 * pct, f"Baixando... {pct * 100:.0f}%  "
                                            f"({feito / 1e6:.0f}/{tot / 1e6:.0f} MB, {vel / 1e6:.1f} MB/s)")

        opts.update({
            "format": "bv*[height<=1080]+ba/b[height<=1080]/b",
            "merge_output_format": "mp4",
            "outtmpl": str(pasta / "vod.%(ext)s"),
            "progress_hooks": [hook],
            "concurrent_fragment_downloads": 4,
            "writesubtitles": False,
        })
        self.log(f"⬇ Baixando: {meta['titulo']}")
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        prontos = [p for p in pasta.glob("vod.*") if ".part" not in p.name
                   and not re.search(r"\.f\d+\.", p.name)]
        if not prontos:
            raise RuntimeError("O download terminou mas não achei o arquivo do vídeo.")
        return prontos[0], pasta, meta

    # ------------------------------------------------------------------ chat
    def baixar_chat(self, meta, pasta):
        """Baixa o chat da live (YouTube ou Twitch). Se não der, segue sem ele."""
        arq = pasta / "chat.json"
        if arq.exists():
            msgs = json.loads(arq.read_text(encoding="utf-8"))
            if msgs:
                self.log(f"✔ Chat já baixado antes ({len(msgs)} mensagens).")
            return msgs
        msgs = []
        ext = meta.get("extrator", "")
        try:
            if ext == "TwitchVod":
                self.prog(23, "Baixando o chat da Twitch...")
                msgs = M.chat_twitch(meta["id"], self.log, lambda t: self.prog(23, t),
                                     self.cancel.is_set)
            elif ext == "Youtube" and meta.get("ao_vivo"):
                self.prog(23, "Baixando o chat da live do YouTube...")
                import yt_dlp

                def hook(d):
                    if self.cancel.is_set():
                        raise Cancelado()
                    if d.get("status") == "downloading" and d.get("fragment_index"):
                        self.prog(23, f"Baixando o chat do YouTube... parte {d['fragment_index']}")

                opts = {"quiet": True, "no_warnings": True, "skip_download": True,
                        "writesubtitles": True, "subtitleslangs": ["live_chat"],
                        "outtmpl": str(pasta / "chat.%(ext)s"), "progress_hooks": [hook]}
                nav = self.cfg.get("navegador")
                if nav and nav != "Nenhum":
                    opts["cookiesfrombrowser"] = (nav.lower(),)
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([meta["url"]])
                achados = list(pasta.glob("chat*.live_chat.json"))
                if achados:
                    msgs = M.ler_chat_youtube(achados[0])
            else:
                return []
        except (Cancelado, InterruptedError):
            raise Cancelado()
        except Exception as e:
            self.log(f"⚠ Não consegui baixar o chat ({str(e)[:100]}). Seguindo sem ele.")
            return []
        if msgs:
            self.log(f"💬 Chat baixado: {len(msgs)} mensagens")
        arq.write_text(json.dumps(msgs, ensure_ascii=False, default=_json_seguro), encoding="utf-8")
        return msgs

    # ----------------------------------------------------------- transcrição
    def extrair_audio(self, video, pasta):
        wav = pasta / "audio.wav"
        if not wav.exists():
            self.prog(26, "Extraindo o áudio...")
            tmp = pasta / "audio_tmp.wav"
            self.sh([self.ffmpeg, "-y", "-loglevel", "error", "-i", str(video),
                     "-vn", "-ac", "1", "-ar", "16000", str(tmp)])
            tmp.replace(wav)
        return wav

    def transcrever(self, wav, pasta, dur_total):
        arq = pasta / f"transcript_{self.cfg['modelo']}.json"
        if arq.exists():
            self.log("✔ Transcrição já feita antes, reaproveitando.")
            return json.loads(arq.read_text(encoding="utf-8"))
        vizinho = getattr(self, "transcript_vizinho", None)
        if vizinho and vizinho.exists():
            self.log("✔ Achei uma transcrição pronta ao lado do vídeo, reaproveitando.")
            dados = json.loads(vizinho.read_text(encoding="utf-8"))
            arq.write_text(json.dumps(dados, ensure_ascii=False, default=_json_seguro), encoding="utf-8")
            return dados
        self.prog(28, "Carregando o Whisper (na 1ª vez ele baixa o modelo)...")
        from faster_whisper import WhisperModel
        with wave.open(str(wav)) as wf:
            audio = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
        audio = audio.astype(np.float32) / 32768.0

        usar_gpu = self.cfg.get("usar_gpu", True) and cuda_disponivel()
        m = None
        if usar_gpu:
            try:
                m = WhisperModel(self.cfg["modelo"], device="cuda", compute_type="float16")
                self.log("🚀 Usando placa de vídeo NVIDIA")
            except Exception as e:
                logging.warning("GPU indisponível: %s", e)
                m = None
        if m is None:
            self.log("🖥 Usando o processador para transcrever (mais lento que placa NVIDIA)")
            m = WhisperModel(self.cfg["modelo"], device="cpu", compute_type="int8")
            usar_gpu = False
        try:
            dados = self._rodar_whisper(m, audio, dur_total)
        except Cancelado:
            raise
        except Exception as e:
            erro = str(e).lower()
            if not usar_gpu or not any(k in erro for k in ("cublas", "cudnn", "cuda", ".dll", "cufft")):
                raise
            logging.warning("GPU falhou durante a transcrição: %s", e)
            self.log("⚠ A placa NVIDIA não tem as bibliotecas CUDA instaladas. "
                     "Recomeçando a transcrição pelo processador...")
            del m
            m = WhisperModel(self.cfg["modelo"], device="cpu", compute_type="int8")
            dados = self._rodar_whisper(m, audio, dur_total)
        arq.write_text(json.dumps(dados, ensure_ascii=False, default=_json_seguro), encoding="utf-8")
        return dados

    def _rodar_whisper(self, m, audio, dur_total):
        segs, _ = m.transcribe(audio, language=self.cfg.get("idioma", "pt"),
                               word_timestamps=True, vad_filter=True)
        dados = []
        inicio = time.time()
        for s in segs:
            self.checar()
            dados.append({"start": s.start, "end": s.end, "text": s.text.strip(),
                          "nsp": round(float(getattr(s, "no_speech_prob", 0) or 0), 3),
                          "words": [{"start": w.start, "end": w.end, "word": w.word.strip()}
                                    for w in (s.words or [])]})
            pct = min(1, s.end / max(1, dur_total))
            gasto = time.time() - inicio
            resta = gasto / pct - gasto if pct > 0.02 else 0
            falta = f" — faltam ~{resta / 60:.0f} min" if resta else ""
            self.prog(28 + 40 * pct, f"Transcrevendo... {pct * 100:.0f}%{falta}")
            self.log(f"  [{ts(s.start)[:-3]}] {s.text.strip()}")
        return dados

    # -------------------------------------------------------- melhores partes
    def analisar(self, video, wav, pasta, transcript, chat, meta, n, streamer):
        """Calcula todos os sinais (com cache) e devolve a nota de cada segundo."""
        perfil = M.carregar_perfil(PASTA_CONFIG, streamer)
        pacotes = M.detectar_pacotes(meta.get("titulo", ""), transcript)
        if pacotes:
            self.log(f"🎮 Pacotes de palavras: {', '.join(pacotes)}")
        if perfil.get("palavras"):
            self.log(f"🧠 Bordões conhecidos de {streamer}: {', '.join(list(perfil['palavras'])[:8])}")
        gatilhos = M.montar_gatilhos(pacotes, perfil)
        self.pacotes = pacotes

        sinais = {}
        sinais["heatmap"] = M.sinal_heatmap(meta.get("heatmap"), n)
        sinais["chat"] = M.sinal_chat(chat, n) if self.cfg.get("usar_chat", True) else None
        sinais["palavras"] = M.sinal_palavras(transcript, n, gatilhos)
        sinais["fala"], fala_bruta = M.sinal_fala(transcript, n)

        self.prog(69, "Analisando o áudio (gritos, risadas, impactos)...")
        cache_audio = pasta / "sinal_audio.npy"
        try:
            if cache_audio.exists():
                sinais["audio"] = M.ajustar_tamanho(np.load(cache_audio), n)
            else:
                sinais["audio"] = M.sinal_audio(wav, n, self.cancel.is_set)
                if sinais["audio"] is not None:
                    np.save(cache_audio, sinais["audio"])
        except InterruptedError:
            raise Cancelado()
        except Exception as e:
            self.log(f"⚠ Análise de áudio falhou: {e}")
            sinais["audio"] = None

        pen_tela = None
        if self.cfg.get("usar_movimento", True):
            self.prog(72, "Analisando o movimento da tela...")
            cache_mov = pasta / "sinal_movimento.npz"
            try:
                if cache_mov.exists():
                    d = np.load(cache_mov)
                    sinais["movimento"] = M.ajustar_tamanho(d["mov"], n)
                    pen_tela = M.ajustar_tamanho(d["pen"], n)
                else:
                    sinais["movimento"], pen_tela = M.sinal_movimento(
                        self.ffmpeg, video, n, self.cancel.is_set, SEM_JANELA)
                    if sinais["movimento"] is not None:
                        np.savez(cache_mov, mov=sinais["movimento"], pen=pen_tela)
            except InterruptedError:
                raise Cancelado()
            except Exception as e:
                self.log(f"⚠ Análise da imagem falhou: {e}")
                sinais["movimento"] = None
        else:
            sinais["movimento"] = None

        score, usados = M.combinar(sinais, perfil.get("pesos"), n)
        score += M.penalidades(transcript, n, fala_bruta)
        if pen_tela is not None:
            score += pen_tela
        self.log("📊 Sinais usados: " + ", ".join(M.NOMES_SINAIS[u] for u in usados))
        faltando = [M.NOMES_SINAIS[k] for k in ("heatmap", "chat") if sinais.get(k) is None]
        if faltando:
            self.log("   (sem " + " e sem ".join(faltando) + " neste vídeo)")

        # aprende bordões com o que o público reagiu
        ref = sinais["chat"] if sinais["chat"] is not None else sinais["heatmap"]
        novos = M.aprender_bordoes(transcript, ref, n, perfil)
        if novos:
            self.log(f"🧠 Aprendi bordões de {streamer}: {', '.join(novos[:8])}")
        perfil["videos"] = int(perfil.get("videos", 0)) + 1
        perfil["nome"] = streamer
        M.salvar_perfil(PASTA_CONFIG, streamer, perfil)
        return score, sinais, gatilhos

    @staticmethod
    def frase_destaque(segs, gatilhos):
        """A fala do corte com mais reação (por trecho do Whisper, não por pontuação)."""
        if not segs:
            return ""
        melhor = max(segs, key=lambda x: (M.conta_gatilhos(x["text"], gatilhos), len(x["text"])))
        return melhor["text"].strip()[:90]

    # --------------------------------------------------------------- câmera
    def frame_png(self, video, seg, saida):
        self.sh([self.ffmpeg, "-y", "-loglevel", "error", "-ss", f"{seg:.1f}", "-i", str(video),
                 "-frames:v", "1", "-vf", "scale=960:-2", str(saida)])
        return saida

    def detectar_camera(self, video, w, h, t_ini=0.0, t_fim=None, amostras=24):
        try:
            import cv2  # noqa: F401
        except ImportError:
            return None
        return detectar_camera(video, w, h, t_ini, t_fim, amostras, aviso=self.log)

    def obter_camera(self, video, pasta, dur_total, w, h):
        modo = self.cfg.get("camera", "Automática")
        if modo == "Sem câmera":
            return None
        self.cam_manual = False
        if self.cfg.get("cam_fixa"):
            self.cam_manual = True
            return list(self.cfg["cam_fixa"])
        salvo = pasta / "camera.json"
        if salvo.exists() and modo == "Automática":
            self.cam_manual = True
            cam = json.loads(salvo.read_text())
            self.log(f"✔ Usando a câmera marcada antes: {cam}")
            return cam
        cam = None
        if modo == "Automática":
            self.prog(77, "Procurando a câmera do streamer...")
            cam = self.detectar_camera(video, w, h, 0, dur_total, amostras=40)
            if cam:
                self.log(f"📷 Câmera encontrada em x={cam[0]} y={cam[1]} ({cam[2]}x{cam[3]})")
            else:
                self.log("⚠ Não achei a câmera sozinho. Marque ela na tela que abriu.")
        if not cam and self.pedir_camera:
            png = self.frame_png(video, dur_total * 0.35, pasta / "frame_camera.png")
            fw, fh = 960, round(960 * h / w)
            resp = self.pedir_camera(str(png), fw, fh)
            self.checar()
            if resp is None:
                self.log("➡ Seguindo sem câmera.")
                return None
            esc = w / fw
            cam = [int(resp[0] * esc), int(resp[1] * esc), int(resp[2] * esc), int(resp[3] * esc)]
            cam[2] = min(cam[2], w - cam[0])
            cam[3] = min(cam[3], h - cam[1])
            salvo.write_text(json.dumps(cam, default=_json_seguro))
            self.cam_manual = True
        return cam

    # -------------------------------------------------------- legenda (ASS)
    def gerar_ass(self, transcript, ini, fim, titulo, arq, y_leg, estilo, remap, dur_final):
        e = ESTILOS_LEGENDA.get(estilo) or ESTILOS_LEGENDA[ESTILO_PADRAO]
        palavras = [w for s in transcript for w in s.get("words", [])
                    if w["start"] >= ini and w["end"] <= fim and w["word"]]
        caixa = 3 if e["caixa"] else 1
        cab = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {LARG}
PlayResY: {ALT}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Leg,{e['fonte']},{e['tam']},{e['cor']},{e['destaque']},{e['contorno']},&H80000000,-1,0,0,0,100,100,0,0,{caixa},{e['borda']},{e['sombra']},5,60,60,0,1
Style: Tit,{FONTE},64,&H00FFFFFF,&H00FFFFFF,&H00000000,&HCC000000,-1,0,0,0,100,100,0,0,3,18,0,8,60,60,70,1
Style: Marca,Arial,26,&H90FFFFFF,&H90FFFFFF,&H90000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0,3,30,30,25,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        ev = [f"Dialogue: 2,{ts(0)},{ts(min(4, dur_final))},Tit,,0,0,0,,{{\\fad(150,300)}}{titulo}"]
        if self.cfg.get("marca", "").strip():
            ev.append(f"Dialogue: 1,{ts(0)},{ts(dur_final)},Marca,,0,0,0,,{self.cfg['marca'].strip()}")
        n = e["palavras"]
        cor_dest = e["destaque"].replace("&H00", "&H")
        maiusc = (lambda t: t.upper()) if e["maiusc"] else (lambda t: t)
        for i in range(0, len(palavras), n):
            grupo = palavras[i:i + n]
            fim_g = palavras[i + n]["start"] if i + n < len(palavras) else grupo[-1]["end"]
            fim_g = min(fim_g, grupo[-1]["end"] + 0.6)
            for j, w in enumerate(grupo):
                a = remap(w["start"] - ini)
                b = remap((grupo[j + 1]["start"] if j + 1 < len(grupo) else fim_g) - ini)
                if b - a < 0.04:
                    continue
                txt = " ".join(
                    (f"{{\\c{cor_dest}&\\fscx110\\fscy110}}{maiusc(x['word'])}{{\\r}}" if k == j
                     else maiusc(x["word"])) for k, x in enumerate(grupo))
                ev.append(f"Dialogue: 0,{ts(a)},{ts(b)},Leg,,0,0,0,,{{\\pos(540,{y_leg})}}{txt}")
        arq.write_text(cab + "\n".join(ev) + "\n", encoding="utf-8")

    # --------------------------------------------------------------- render
    def renderizar(self, video, ini, fim, cam, w, h, ass_nome, saida_nome, pasta, faixa,
                   layout=None, manter=None, pico=None, barra=False):
        """Monta o vertical. layout = chave de LAYOUTS; manter = trechos (relativos)
        que ficam quando corta silêncios; pico = segundo (já remapeado) do zoom."""
        pos, frac = LAYOUTS.get(layout or LAYOUT_PADRAO, LAYOUTS[LAYOUT_PADRAO])
        if manter:
            expr = "+".join(f"between(t,{a:.2f},{b:.2f})" for a, b in manter)
            vsel = f"select='{expr}',setpts=N/FRAME_RATE/TB,"
            asel = f"aselect='{expr}',asetpts=N/SR/TB,"
            dur_final = sum(b - a for a, b in manter)
        else:
            vsel = asel = ""
            dur_final = fim - ini
        if cam and pos:
            alt_cam = int(ALT * frac) // 2 * 2
            alt_game = ALT - alt_cam
            cx, cy, cw, ch = cam
            gx, gy, gw, gh = recorte_gameplay(w, h, cam, alt_game)
            ordem = "[cam][game]" if pos == "cima" else "[game][cam]"
            emenda = alt_cam if pos == "cima" else alt_game
            v = (f"[0:v]fps=30,{vsel}split=2[a][b];"
                 f"[a]crop={cw}:{ch}:{cx}:{cy},scale={LARG}:{alt_cam}:force_original_aspect_ratio=increase,"
                 f"crop={LARG}:{alt_cam},setsar=1[cam];"
                 f"[b]crop={gw}:{gh}:{gx}:{gy},scale={LARG}:{alt_game}:force_original_aspect_ratio=increase,"
                 f"crop={LARG}:{alt_game},setsar=1[game];"
                 f"{ordem}vstack=inputs=2,"
                 f"drawbox=x=0:y={emenda - 4}:w={LARG}:h=8:color=black@0.9:t=fill")
        else:
            v = (f"[0:v]fps=30,{vsel}split=2[a][b];"
                 f"[a]scale={LARG}:{ALT}:force_original_aspect_ratio=increase,crop={LARG}:{ALT},"
                 f"boxblur=25:4,eq=brightness=-0.12[bg];"
                 f"[b]scale={LARG}:-2,setsar=1[fg];"
                 f"[bg][fg]overlay=0:(H-h)/2")
        if pico is not None:
            v += (f",zoompan=z='1+0.10*exp(-pow((it-{pico:.2f})/0.45,2))':d=1:"
                  f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={LARG}x{ALT}:fps=30")
        v += f",ass={ass_nome}"
        if barra:
            v += (f"[v0];color=c=0xA78BFA:s={LARG}x12:r=30:d={dur_final:.2f}[barra];"
                  f"[v0][barra]overlay=x='(t/{dur_final:.2f}-1)*w':y=main_h-h:eval=frame:shortest=1[v]")
        else:
            v += "[v]"
        args = [self.ffmpeg, "-y", "-loglevel", "error",
                "-ss", f"{ini:.2f}", "-t", f"{fim - ini:.2f}", "-i", str(Path(video).resolve())]
        if getattr(self, "tem_audio", True):
            filtro = v + f";[0:a]{asel}loudnorm=I=-14:TP=-1.5:LRA=11[a]"
            args += ["-filter_complex", filtro, "-map", "[v]", "-map", "[a]",
                     "-c:a", "aac", "-b:a", "160k"]
        else:
            args += ["-filter_complex", v, "-map", "[v]"]
        args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-r", "30",
                 "-pix_fmt", "yuv420p", "-movflags", "+faststart", saida_nome]
        self.sh(args, cwd=pasta, progresso_dur=dur_final, faixa=faixa)
        return dur_final

    # ----------------------------------------------------------------- tudo
    def preparar(self):
        """Baixa, transcreve e analisa. Devolve tudo que a revisão e o render precisam."""
        if not self.ffmpeg or not self.ffprobe:
            raise RuntimeError("ffmpeg não encontrado. Clique em 'Instalar dependências'.")
        cfg = self.cfg
        fonte = cfg["fonte"].strip().strip('"')
        saida_base = Path(cfg["saida"]).expanduser()
        trab_base = saida_base / "_lukclips_cache"
        trab_base.mkdir(parents=True, exist_ok=True)

        if re.match(r"^https?://", fonte, re.I):
            video, pasta, meta = self.baixar(fonte, trab_base)
        else:
            video = Path(fonte).resolve()
            if not video.is_file():
                raise RuntimeError(f"Arquivo não encontrado: {fonte}")
            pasta = trab_base / nome_seguro(f"arquivo_{video.stem}_{int(video.stat().st_size)}", 80)
            pasta.mkdir(parents=True, exist_ok=True)
            meta = {"titulo": video.stem, "canal": ""}
            self.transcript_vizinho = video.parent / "transcript.json"

        streamer = cfg.get("streamer", "").strip() or meta.get("canal", "") or "streamer"
        dur_total = self.duracao(video)
        n = int(math.ceil(dur_total))
        w, h = self.tamanho(video)
        self.tem_audio = self.possui_audio(video)
        self.log(f"🎬 {meta['titulo']}  —  {w}x{h}, {dur_total / 60:.0f} min")
        if meta.get("heatmap"):
            self.log("🔥 Este vídeo tem o mapa de 'mais repetidos' do YouTube")

        chat = self.baixar_chat(meta, pasta) if cfg.get("usar_chat", True) else []
        wav = self.extrair_audio(video, pasta)
        transcript = self.transcrever(wav, pasta, dur_total)
        if not transcript:
            raise RuntimeError("O Whisper não encontrou fala nesse vídeo.")

        score, sinais, gatilhos = self.analisar(video, wav, pasta, transcript, chat, meta, n, streamer)

        self.prog(76, "Escolhendo os melhores momentos...")
        qtd = int(cfg["cortes"])
        dmin, dmax = int(cfg.get("dmin", 20)), int(cfg.get("dmax", 90))
        pular = int(cfg.get("pular_inicio", 0))
        if dur_total < pular + dmax * qtd:
            pular = 0
        extra = qtd * 2 if cfg.get("revisar") else qtd
        janelas = M.escolher_cortes(score, transcript, n, extra, dmin, dmax, pular)
        if not janelas:
            raise RuntimeError("Não achei nenhum momento forte nesse vídeo.")
        usados, cands = set(), []
        for ini, fim, nota, pico in janelas:
            txt = " ".join(s["text"] for s in transcript if s["end"] > ini and s["start"] < fim)
            cands.append({"ini": ini, "fim": fim, "nota": nota, "pico": pico,
                          "titulo": M.gerar_titulo(txt, usados, streamer), "edicoes": {},
                          "escolhido": len(cands) < qtd})
        achados = sum(c["escolhido"] for c in cands)
        if achados < qtd:
            self.log(f"✨ Só achei {achados} momentos realmente bons (pedido: {qtd}). "
                     "Preferi não completar com cortes fracos.")
        else:
            self.log(f"✨ Encontrei {achados} momentos bons"
                     + (f" (+{len(cands) - achados} reservas para a revisão)" if len(cands) > achados else ""))
        return {"video": video, "pasta": pasta, "meta": meta, "streamer": streamer,
                "dur_total": dur_total, "w": w, "h": h, "transcript": transcript,
                "sinais": sinais, "gatilhos": gatilhos, "cands": cands, "saida_base": saida_base}

    def gerar(self, P, cortes):
        """Renderiza os cortes escolhidos (depois da revisão, se houver)."""
        cfg = self.cfg
        video, w, h = P["video"], P["w"], P["h"]
        transcript, streamer, meta = P["transcript"], P["streamer"], P["meta"]
        layout = cfg.get("layout") or LAYOUT_PADRAO
        if cfg.get("camera") == "Sem câmera":
            layout = "Sem câmera (fundo desfocado)"
        pos, frac = LAYOUTS.get(layout, LAYOUTS[LAYOUT_PADRAO])
        cam = self.obter_camera(video, P["pasta"], P["dur_total"], w, h) if pos else None
        if cam is None:
            pos = None

        pasta_clips = P["saida_base"] / f"{nome_seguro(meta['titulo'], 45)} - {datetime.now():%Y-%m-%d %H%M}"
        pasta_clips.mkdir(parents=True, exist_ok=True)
        rel, gerados = [], []
        for k, c in enumerate(cortes, 1):
            self.checar()
            ini, fim, titulo = c["ini"], c["fim"], c["titulo"]
            tr = M.aplicar_edicoes(transcript, c.get("edicoes") or {})
            segs = [s for s in tr if s["end"] > ini and s["start"] < fim]
            nome = f"corte_{k:02d}"
            a = 80 + 20 * (k - 1) / len(cortes)
            b = 80 + 20 * k / len(cortes)
            cam_corte = cam
            if cam and not self.cam_manual:
                self.prog(a, f"Ajustando a câmera do corte {k}...")
                achada = self.detectar_camera(video, w, h, ini, fim, amostras=16)
                if achada:
                    cam_corte = achada
            manter = None
            if cfg.get("cortar_silencio"):
                manter = M.intervalos_com_fala(tr, ini, fim, P["sinais"].get("audio"))
            remap = M.remapeador(manter)
            dur_final = sum(y - x for x, y in manter) if manter else fim - ini
            pico = remap(c["pico"] - ini) if cfg.get("zoom_pico") and c.get("pico") is not None else None
            if pos and cam_corte:
                alt_cam = int(ALT * frac) // 2 * 2
                y_leg = alt_cam if pos == "cima" else ALT - alt_cam
            else:
                y_leg = 1480
            self.gerar_ass(tr, ini, fim, titulo, pasta_clips / f"{nome}.ass", y_leg,
                           cfg.get("estilo_legenda", ESTILO_PADRAO), remap, dur_final)
            info_sil = f", sem silêncios: {dur_final:.0f}s" if manter else ""
            self.log(f"✂ {nome}: {ts(ini)[:-3]} → {ts(fim)[:-3]} ({fim - ini:.0f}s{info_sil})  {titulo}")
            self.renderizar(video, ini, fim, cam_corte if pos else None, w, h, f"{nome}.ass", f"{nome}.mp4",
                            pasta_clips, (a, b, f"Gerando corte {k} de {len(cortes)}..."),
                            layout=layout, manter=manter, pico=pico, barra=cfg.get("barra_progresso"))
            (pasta_clips / f"{nome}.ass").unlink(missing_ok=True)
            destaque = self.frase_destaque(segs, P["gatilhos"])
            tag = re.sub(r"\W+", "", streamer.lower()) or "cortes"
            extras = " ".join(TAGS_PACOTE[p] for p in getattr(self, "pacotes", []) if p in TAGS_PACOTE)
            fixas = " ".join(t if t.startswith("#") else f"#{t}" for t in cfg.get("hashtags_fixas", "").split())
            hashtags = " ".join(f"#{tag} {extras} {fixas} #cortes #gameplay #fyp #viral".split())
            descricao = (f"\"{destaque}\"\n\n" if destaque else "") + \
                f"🎮 Créditos: {streamer} — assista a live completa no canal dele(a)!"
            escrever_post(pasta_clips / f"{nome}.txt", titulo, descricao, hashtags)
            resumo = M.resumo_sinais(P["sinais"], ini, fim)
            rel.append({"id": f"{pasta_clips.name}/{nome}", "streamer": streamer,
                        "arquivo": f"{nome}.mp4", "inicio": round(ini, 1), "fim": round(fim, 1),
                        "duracao": round(dur_final, 1), "nota": round(c["nota"], 2),
                        "titulo": titulo, "destaque": destaque, "sinais": resumo})
            gerados.append(pasta_clips / f"{nome}.mp4")
            registrar_historico({"id": f"{pasta_clips.name}/{nome}", "streamer": streamer,
                                 "arquivo": str(pasta_clips / f"{nome}.mp4"), "titulo": titulo,
                                 "video": meta.get("titulo", ""), "data": f"{datetime.now():%Y-%m-%d}",
                                 "sinais": resumo, "views": None})
        (pasta_clips / "cortes.json").write_text(
            json.dumps(rel, ensure_ascii=False, indent=2, default=_json_seguro), encoding="utf-8")
        self.prog(100, f"Pronto! {len(gerados)} cortes gerados 🎉")
        self.log(f"✅ Cortes salvos em: {pasta_clips}")
        return pasta_clips, gerados, rel

    def executar(self):
        P = self.preparar()
        cortes = [c for c in P["cands"] if c["escolhido"]]
        if self.cfg.get("revisar") and self.pedir_revisao:
            self.prog(78, "Esperando você revisar os momentos...")
            cortes = self.pedir_revisao(P)
            self.checar()
            if not cortes:
                raise Cancelado()
        return self.gerar(P, cortes)


# ============================================================================
# Registro de erros, atualização automática, utilidades
# ============================================================================
# Atualização automática: crie um repositório no GitHub (ex.: "seu-usuario/LukClips"),
# publique os instaladores em "Releases" com a tag v3.1, v3.2... e coloque o nome aqui.
REPO_GITHUB = "LUKbrDEV/LukClips"

PASTA_LOGS = PASTA_CONFIG / "logs"


def configurar_log():
    import logging.handlers
    try:
        PASTA_LOGS.mkdir(parents=True, exist_ok=True)
        h = logging.handlers.RotatingFileHandler(PASTA_LOGS / "lukclips.log", maxBytes=1_000_000,
                                                 backupCount=3, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        raiz = logging.getLogger()
        raiz.setLevel(logging.INFO)
        raiz.addHandler(h)
        logging.info("==== %s v%s iniciado (%s, Python %s) ====", APP, VERSAO, sys.platform,
                     sys.version.split()[0])
    except Exception:
        pass


def traduzir_erro(msg):
    if "Sign in to confirm" in msg or "not a bot" in msg:
        return ("O YouTube pediu login.\n\nNa aba ⚙ Análise, em 'Login do navegador', escolha o navegador "
                "em que você está logado no YouTube (Firefox funciona melhor) e tente de novo.")
    if "Only images are available" in msg or "Requested format is not available" in msg:
        return ("O YouTube bloqueou o download.\n\nClique no aviso amarelo do canto de cima para "
                "instalar o Deno e tente de novo.")
    if "cookie" in msg.lower() and "database" in msg.lower():
        return ("Não consegui ler o login do navegador.\n\nFeche o navegador por completo "
                "e tente de novo, ou escolha o Firefox.")
    if "No space left" in msg or "Espaço insuficiente" in msg or "WinError 112" in msg:
        return "O disco está cheio. Use '🧹 Limpar cache' ou escolha outra pasta para salvar."
    if "HTTP Error 404" in msg or "Video unavailable" in msg:
        return "Esse vídeo não está disponível (link errado, privado ou apagado)."
    return msg


def formatar_tamanho(b):
    for un in ("B", "KB", "MB", "GB"):
        if b < 1024 or un == "GB":
            return f"{b:.0f} {un}" if un in ("B", "KB") else f"{b:.1f} {un}"
        b /= 1024


def abrir_url(req, timeout=15):
    """urlopen com certificados do Windows; se falhar o certificado, tenta os do certifi."""
    import ssl
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.URLError as e:
        if not isinstance(getattr(e, "reason", None), ssl.SSLError):
            raise
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
        return urllib.request.urlopen(req, timeout=timeout, context=ctx)


def _versao_tupla(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v))[:3]) or (0,)


def buscar_atualizacao():
    """Olha a última versão publicada no GitHub. Devolve {} se não há nada novo."""
    if not REPO_GITHUB:
        return {}
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO_GITHUB}/releases/latest",
                                 headers={"User-Agent": f"{APP}/{VERSAO}",
                                          "Accept": "application/vnd.github+json"})
    with abrir_url(req, timeout=15) as r:
        rel = json.loads(r.read().decode("utf-8"))
    versao = str(rel.get("tag_name", "")).lstrip("vV")
    if not versao or _versao_tupla(versao) <= _versao_tupla(VERSAO):
        return {}
    instalador = next((a["browser_download_url"] for a in rel.get("assets", [])
                       if a.get("name", "").lower().endswith(".exe")), None)
    return {"versao": versao, "notas": rel.get("body") or "", "instalador": instalador,
            "pagina": rel.get("html_url") or f"https://github.com/{REPO_GITHUB}/releases"}


# ============================================================================
# Interface
# ============================================================================
COR_FUNDO = "#0E0E13"
COR_CARD = "#181820"
COR_CAMPO = "#22222C"
COR_BORDA = "#2C2C38"
COR_TEXTO = "#EDEDF2"
COR_SUAVE = "#8A8A9A"
COR_DESTAQUE = "#8B5CF6"
COR_DESTAQUE2 = "#A78BFA"

MODELOS = {"Rápido (base)": "base", "Equilibrado (small)": "small",
           "Preciso (medium)": "medium", "Máximo (large-v3, precisa GPU)": "large-v3"}
DURACOES = {"Automática (20–90s)": (20, 90), "Curtos (15–40s)": (15, 40),
            "Médios (30–60s)": (30, 60), "Longos (45–120s)": (45, 120),
            "Fixa 30s": (30, 30), "Fixa 60s": (60, 60)}
IDIOMAS = {"Português": "pt", "Inglês": "en", "Espanhol": "es", "Detectar sozinho": None}
CONFIG_PADRAO = {
    "saida": str(Path.home() / "Videos" / "LukClips"),
    "cortes": 5, "duracao": "Automática (20–90s)", "modelo": "Equilibrado (small)",
    "usar_chat": True, "usar_movimento": True,
    "layout": "Câmera em cima (50/50)", "estilo_legenda": "Clássico (branco + amarelo)",
    "zoom_pico": False, "cortar_silencio": False, "barra_progresso": True, "revisar": True,
    "idioma": "Português", "usar_gpu": True, "hashtags_fixas": "",
    "camera": "Automática", "navegador": "Nenhum", "streamer": "",
    "pular_inicio": 300, "marca": "Feito por LukDev",
}


def carregar_config():
    cfg = dict(CONFIG_PADRAO)
    try:
        cfg.update(json.loads(ARQ_CONFIG.read_text(encoding="utf-8")))
    except Exception:
        pass
    if cfg.get("duracao") not in DURACOES:      # config da versão 1.0
        cfg["duracao"] = CONFIG_PADRAO["duracao"]
    if cfg.get("camera") == "Sem câmera":       # config da versão 2.0
        cfg["camera"] = "Automática"
        cfg["layout"] = "Sem câmera (fundo desfocado)"
    if cfg.get("layout") not in LAYOUTS:
        cfg["layout"] = CONFIG_PADRAO["layout"]
    if cfg.get("estilo_legenda") not in ESTILOS_LEGENDA:
        cfg["estilo_legenda"] = CONFIG_PADRAO["estilo_legenda"]
    return cfg


ARQ_HISTORICO = PASTA_CONFIG / "historico.json"


def carregar_historico():
    try:
        return json.loads(ARQ_HISTORICO.read_text(encoding="utf-8"))
    except Exception:
        return []


def salvar_historico(hist):
    try:
        PASTA_CONFIG.mkdir(parents=True, exist_ok=True)
        ARQ_HISTORICO.write_text(json.dumps(hist[-2000:], ensure_ascii=False, indent=1, default=_json_seguro), encoding="utf-8")
    except Exception:
        pass


def registrar_historico(item):
    hist = [h for h in carregar_historico() if h.get("id") != item["id"]]
    hist.append(item)
    salvar_historico(hist)


def buscar_historico(id_):
    for h in carregar_historico():
        if h.get("id") == id_:
            return h
    return None


def atualizar_historico(id_, campos):
    hist = carregar_historico()
    for h in hist:
        if h.get("id") == id_:
            h.update(campos)
    salvar_historico(hist)


def ler_post(arq_txt):
    """Lê o .txt do corte: título / descrição / hashtags (separados por linha em branco)."""
    try:
        blocos = [b.strip() for b in Path(arq_txt).read_text(encoding="utf-8").split("\n\n") if b.strip()]
    except Exception:
        return None
    if not blocos:
        return None
    tags = blocos.pop() if len(blocos) > 1 and blocos[-1].startswith("#") else ""
    titulo = blocos.pop(0) if blocos else ""
    return {"titulo": titulo, "descricao": "\n\n".join(blocos), "hashtags": tags}


def escrever_post(arq_txt, titulo, descricao, hashtags):
    partes = [p for p in (titulo.strip(), descricao.strip(), hashtags.strip()) if p]
    Path(arq_txt).write_text("\n\n".join(partes), encoding="utf-8")


def mostrar_no_explorador(arq):
    """Abre a pasta já com o vídeo selecionado (fácil de arrastar pro navegador)."""
    arq = Path(arq)
    if os.name == "nt":
        subprocess.Popen(f'explorer /select,"{arq}"')
    else:
        abrir(arq.parent)


TAGS_PACOTE = {"gta": "#gta6 #gta", "terror": "#terror", "fps": "#fps", "minecraft": "#minecraft",
               "futebol": "#futebol", "corrida": "#corrida"}


def salvar_config(cfg):
    try:
        PASTA_CONFIG.mkdir(parents=True, exist_ok=True)
        ARQ_CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2, default=_json_seguro), encoding="utf-8")
    except Exception:
        pass


def abrir(caminho):
    caminho = str(caminho)
    if os.name == "nt":
        os.startfile(caminho)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", caminho])
    else:
        subprocess.Popen(["xdg-open", caminho])


def iniciar_interface():
    configurar_log()
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    class SeletorCamera(tk.Toplevel):
        """Mostra um frame do vídeo e deixa a pessoa arrastar um retângulo na câmera."""

        def __init__(self, master, png, fw, fh, ao_fim):
            super().__init__(master)
            self.title("Marque a câmera do streamer")
            self.configure(bg=COR_FUNDO)
            self.resizable(False, False)
            self.ao_fim = ao_fim
            self.resultado = None
            self.img = tk.PhotoImage(file=png)
            tk.Label(self, text="Arraste o mouse por cima da câmera do streamer (a webcam).",
                     bg=COR_FUNDO, fg=COR_TEXTO, font=("Segoe UI", 11, "bold")).pack(pady=(12, 2))
            tk.Label(self, text="Ela vai ocupar a metade de cima do corte.",
                     bg=COR_FUNDO, fg=COR_SUAVE, font=("Segoe UI", 9)).pack(pady=(0, 8))
            self.cv = tk.Canvas(self, width=self.img.width(), height=self.img.height(),
                                highlightthickness=0, cursor="crosshair", bg="black")
            self.cv.pack(padx=12)
            self.cv.create_image(0, 0, image=self.img, anchor="nw")
            self.ret = None
            self.p0 = None
            self.cv.bind("<ButtonPress-1>", self._ini)
            self.cv.bind("<B1-Motion>", self._mov)
            barra = tk.Frame(self, bg=COR_FUNDO)
            barra.pack(fill="x", padx=12, pady=12)
            self.lbl = tk.Label(barra, text="Nenhuma área marcada", bg=COR_FUNDO, fg=COR_SUAVE,
                                font=("Segoe UI", 9))
            self.lbl.pack(side="left")
            ttk.Button(barra, text="Confirmar", style="Acao.TButton",
                       command=self._ok).pack(side="right")
            ttk.Button(barra, text="Seguir sem câmera", style="Sec.TButton",
                       command=self._sem).pack(side="right", padx=8)
            self.protocol("WM_DELETE_WINDOW", self._sem)
            self.grab_set()
            self.focus_force()

        def _ini(self, e):
            self.p0 = (e.x, e.y)
            if self.ret:
                self.cv.delete(self.ret)
            self.ret = self.cv.create_rectangle(e.x, e.y, e.x, e.y, outline=COR_DESTAQUE2, width=3,
                                                dash=(6, 3))

        def _mov(self, e):
            if not self.p0:
                return
            x0, y0 = self.p0
            x1 = min(max(e.x, 0), self.img.width())
            y1 = min(max(e.y, 0), self.img.height())
            self.cv.coords(self.ret, x0, y0, x1, y1)
            self.resultado = (min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))
            self.lbl.config(text=f"Área: {self.resultado[2]}x{self.resultado[3]}", fg=COR_TEXTO)

        def _ok(self):
            if not self.resultado or self.resultado[2] < 20 or self.resultado[3] < 20:
                messagebox.showwarning(APP, "Arraste o mouse para marcar a câmera primeiro.", parent=self)
                return
            self.destroy()
            self.ao_fim(self.resultado)

        def _sem(self):
            self.destroy()
            self.ao_fim(None)

    # ------------------------------------------------------------------ componentes
    def desenhar_icone(cv, nome, cor):
        """Ícones simples desenhados (emoji não aparece direito em todo Windows)."""
        cv.delete("all")
        if nome == "novo":
            cv.create_oval(2, 2, 8, 8, outline=cor, width=2)
            cv.create_oval(2, 12, 8, 18, outline=cor, width=2)
            cv.create_line(7, 7, 18, 16, fill=cor, width=2)
            cv.create_line(7, 13, 18, 4, fill=cor, width=2)
        elif nome == "fila":
            for y in (5, 10, 15):
                cv.create_line(7, y, 18, y, fill=cor, width=2)
                cv.create_oval(2, y - 1, 4, y + 1, fill=cor, outline=cor)
        elif nome == "biblioteca":
            for x0, y0, x1, y1 in ((2, 2, 9, 10), (11, 2, 18, 7), (11, 9, 18, 18), (2, 12, 9, 18)):
                cv.create_rectangle(x0, y0, x1, y1, outline=cor, width=2)
        elif nome == "desempenho":
            cv.create_line(2, 2, 2, 18, 18, 18, fill=cor, width=2)
            cv.create_line(5, 14, 9, 9, 12, 12, 18, 5, fill=cor, width=2)
        elif nome == "config":
            cv.create_oval(6, 6, 14, 14, outline=cor, width=2)
            for k in range(8):
                a = math.radians(k * 45)
                cv.create_line(10 + 6 * math.cos(a), 10 + 6 * math.sin(a),
                               10 + 9 * math.cos(a), 10 + 9 * math.sin(a), fill=cor, width=2)

    def ass_para_hex(c):
        """'&H00BBGGRR' (cor do ASS) -> '#RRGGBB'."""
        c = c.replace("&H", "").rjust(8, "0")[-6:]
        return f"#{c[4:6]}{c[2:4]}{c[0:2]}"

    class Chave(tk.Frame):
        """Chave liga/desliga ligada a um BooleanVar."""

        def __init__(self, master, texto, var, bg=COR_CARD):
            super().__init__(master, bg=bg)
            self.var = var
            self.cv = tk.Canvas(self, width=40, height=24, bg=bg, highlightthickness=0, cursor="hand2")
            self.cv.pack(side="left")
            self.lbl = tk.Label(self, text=texto, bg=bg, fg=COR_TEXTO, font=("Segoe UI", 10), cursor="hand2")
            self.lbl.pack(side="left", padx=(8, 0))
            for w in (self.cv, self.lbl):
                w.bind("<Button-1>", self._alternar)
            var.trace_add("write", lambda *_: self._desenhar())
            self._desenhar()

        def _alternar(self, _e=None):
            self.var.set(not self.var.get())

        def _desenhar(self):
            if not self.winfo_exists():
                return
            c = self.cv
            c.delete("all")
            on = bool(self.var.get())
            cor = COR_DESTAQUE if on else "#2E2A44"
            c.create_oval(1, 1, 23, 23, fill=cor, outline=cor)
            c.create_oval(17, 1, 39, 23, fill=cor, outline=cor)
            c.create_rectangle(12, 1, 28, 23, fill=cor, outline=cor)
            x = 28 if on else 12
            c.create_oval(x - 8, 4, x + 8, 20, fill="white" if on else "#8D88A3", outline="")
            self.lbl.config(fg=COR_TEXTO if on else "#B9B4CC")

    class Segmentos(tk.Frame):
        """Botões lado a lado em que só um fica marcado (ligado a um StringVar)."""

        def __init__(self, master, var, opcoes, bg=COR_CARD):
            super().__init__(master, bg="#0E0D16", highlightthickness=1, highlightbackground="#2E2A44")
            self.var = var
            self.bts = {}
            for valor, rot in opcoes:
                b = tk.Label(self, text=rot, bg="#0E0D16", fg="#B9B4CC", font=("Segoe UI", 9, "bold"),
                             padx=9, pady=7, cursor="hand2")
                b.pack(side="left", padx=2, pady=2, fill="x", expand=True)
                b.bind("<Button-1>", lambda e, v=valor: var.set(v))
                self.bts[valor] = b
            var.trace_add("write", lambda *_: self._atualizar())
            self._atualizar()

        def _atualizar(self):
            if not self.winfo_exists():
                return
            for v, b in self.bts.items():
                sel = v == self.var.get()
                b.config(bg=COR_DESTAQUE if sel else "#0E0D16", fg="white" if sel else "#B9B4CC")

    class EscolhaLayout(tk.Frame):
        """Mini desenhos clicáveis de cada layout."""
        CURTOS = {"Câmera em cima (50/50)": "Em cima", "Câmera em cima (40/60)": "40/60",
                  "Câmera embaixo (50/50)": "Embaixo", "Sem câmera (fundo desfocado)": "Sem câmera"}

        def __init__(self, master, var, bg=COR_CARD):
            super().__init__(master, bg=bg)
            self.var = var
            self.cvs = {}
            for nome, (pos, frac) in LAYOUTS.items():
                f = tk.Frame(self, bg=bg)
                f.pack(side="left", padx=(0, 10))
                cv = tk.Canvas(f, width=54, height=90, bg=bg, highlightthickness=0, cursor="hand2")
                cv.pack()
                tk.Label(f, text=self.CURTOS.get(nome, nome), bg=bg, fg=COR_SUAVE,
                         font=("Segoe UI", 8)).pack(pady=(3, 0))
                cv.bind("<Button-1>", lambda e, n=nome: var.set(n))
                self.cvs[nome] = (cv, pos, frac)
            var.trace_add("write", lambda *_: self._desenhar())
            self._desenhar()

        def _desenhar(self):
            if not self.winfo_exists():
                return
            for nome, (cv, pos, frac) in self.cvs.items():
                cv.delete("all")
                sel = nome == self.var.get()
                cv.create_rectangle(1, 1, 53, 89, outline=COR_DESTAQUE2 if sel else "#2E2A44",
                                    width=2 if sel else 1, fill="#1D1730" if sel else "#0E0D16")
                x0, y0, x1, y1 = 6, 6, 48, 84
                h = y1 - y0
                if pos == "cima":
                    m = y0 + h * frac
                    cv.create_rectangle(x0, y0, x1, m - 1, fill="#5B4A8A", outline="")
                    cv.create_rectangle(x0, m + 1, x1, y1, fill="#2A3A4D", outline="")
                elif pos == "baixo":
                    m = y0 + h * (1 - frac)
                    cv.create_rectangle(x0, y0, x1, m - 1, fill="#2A3A4D", outline="")
                    cv.create_rectangle(x0, m + 1, x1, y1, fill="#5B4A8A", outline="")
                else:
                    cv.create_rectangle(x0, y0, x1, y1, fill="#1A2230", outline="")
                    cv.create_rectangle(x0, y0 + h * 0.34, x1, y0 + h * 0.66, fill="#2A3A4D", outline="")

    def _texto_legenda(cv, cx, cy, e, tam, cima="OLHA ", baixo="ISSO"):
        """Desenha 'OLHA ISSO' no estilo da legenda (contorno preto e palavra em destaque)."""
        if not e["maiusc"]:
            cima, baixo = cima.capitalize(), baixo.lower()
        fonte = (e["fonte"] if e["fonte"] != "Segoe UI Semibold" else "Segoe UI", tam, "bold")
        t1 = cv.create_text(0, 0, text=cima, font=fonte, anchor="w")
        t2 = cv.create_text(0, 0, text=baixo, font=fonte, anchor="w")
        w1 = cv.bbox(t1)[2] - cv.bbox(t1)[0]
        w2 = cv.bbox(t2)[2] - cv.bbox(t2)[0]
        cv.delete(t1, t2)
        x = cx - (w1 + w2) / 2
        if e["caixa"]:
            cv.create_rectangle(x - 5, cy - tam, x + w1 + w2 + 5, cy + tam, fill="#000000", outline="")
        else:
            for dx, dy in ((-1, -1), (1, -1), (-1, 1), (1, 1), (0, 2)):
                cv.create_text(x + dx, cy + dy, text=cima, font=fonte, anchor="w", fill="#000000")
                cv.create_text(x + w1 + dx, cy + dy, text=baixo, font=fonte, anchor="w", fill="#000000")
        cv.create_text(x, cy, text=cima, font=fonte, anchor="w", fill=ass_para_hex(e["cor"]))
        cv.create_text(x + w1, cy, text=baixo, font=fonte, anchor="w", fill=ass_para_hex(e["destaque"]))

    class EscolhaLegenda(tk.Frame):
        """Amostras clicáveis de cada estilo de legenda."""

        def __init__(self, master, var, bg=COR_CARD):
            super().__init__(master, bg=bg)
            self.var = var
            self.cvs = {}
            for i, nome in enumerate(ESTILOS_LEGENDA):
                cv = tk.Canvas(self, width=112, height=40, bg=bg, highlightthickness=0, cursor="hand2")
                cv.grid(row=i // 3, column=i % 3, padx=(0, 8), pady=(0, 8))
                cv.bind("<Button-1>", lambda e, n=nome: var.set(n))
                self.cvs[nome] = cv
            self.lbl = tk.Label(self, text="", bg=bg, fg=COR_SUAVE, font=("Segoe UI", 8))
            self.lbl.grid(row=9, column=0, columnspan=3, sticky="w")
            var.trace_add("write", lambda *_: self._desenhar())
            self.after(30, self._desenhar)

        def _desenhar(self):
            if not self.winfo_exists():
                return
            for nome, cv in self.cvs.items():
                cv.delete("all")
                sel = nome == self.var.get()
                cv.create_rectangle(1, 1, 111, 39, outline=COR_DESTAQUE2 if sel else "#2E2A44",
                                    width=2 if sel else 1, fill="#1D1730" if sel else "#0E0D16")
                _texto_legenda(cv, 56, 20, ESTILOS_LEGENDA[nome], 9)
            self.lbl.config(text=f"Estilo: {self.var.get()}")

    class Previa(tk.Canvas):
        """Celular com o layout e a legenda escolhidos (redesenha quando muda)."""

        def __init__(self, master, app, bg=COR_CARD):
            super().__init__(master, width=156, height=278, bg=bg, highlightthickness=0)
            self.app = app
            for v in (app.var_layout, app.var_estilo, app.var_barra):
                v.trace_add("write", lambda *_: self.desenhar())
            self.after(40, self.desenhar)

        def desenhar(self):
            if not self.winfo_exists():
                return
            self.delete("all")
            x0, y0, x1, y1 = 6, 6, 150, 272
            self.create_rectangle(x0 - 5, y0 - 5, x1 + 5, y1 + 5, fill="#050508", outline="#2B2840")
            pos, frac = LAYOUTS.get(self.app.var_layout.get(), LAYOUTS[LAYOUT_PADRAO])
            h = y1 - y0

            def cam(a, b):
                self.create_rectangle(x0, a, x1, b, fill="#2A1F45", outline="")
                cx, alt = (x0 + x1) / 2, b - a
                r = alt * 0.17
                self.create_oval(cx - r, a + alt * 0.42 - r, cx + r, a + alt * 0.42 + r, fill="#5B4A8A", outline="")
                topo = a + alt * 0.66   # ombros: metade de cima de uma elipse centrada na base
                self.create_arc(cx - r * 2.3, topo, cx + r * 2.3, b + (b - topo), start=0, extent=180,
                                fill="#5B4A8A", outline="", style="pieslice")

            def jogo(a, b):
                self.create_rectangle(x0, a, x1, b, fill="#1B2A3A", outline="")
                cx = (x0 + x1) / 2
                self.create_polygon(cx - 10, a + 4, cx + 10, a + 4, cx + 34, b, cx - 34, b, fill="#2A3A4D", outline="")
                self.create_rectangle(cx - 12, b - (b - a) * 0.38, cx + 12, b - (b - a) * 0.22, fill="#EC4899",
                                      outline="")

            if pos == "cima":
                m = y0 + h * frac
                jogo(m, y1)
                cam(y0, m)
                emenda = m
            elif pos == "baixo":
                m = y0 + h * (1 - frac)
                cam(m, y1)
                jogo(y0, m)
                emenda = m
            else:
                self.create_rectangle(x0, y0, x1, y1, fill="#141A24", outline="")
                jogo(y0 + h * 0.34, y0 + h * 0.66)
                emenda = y0 + h * 0.79
            self.create_rectangle(x0, y1, x1, y1 + 5, fill="#050508", outline="")
            # título no topo
            self.create_rectangle(x0 + 22, y0 + 8, x1 - 22, y0 + 22, fill="#000000", outline="")
            self.create_text((x0 + x1) / 2, y0 + 15, text="TÍTULO DO CORTE", fill="white", font=("Segoe UI", 6, "bold"))
            e = ESTILOS_LEGENDA.get(self.app.var_estilo.get(), ESTILOS_LEGENDA[ESTILO_PADRAO])
            _texto_legenda(self, (x0 + x1) / 2, emenda, e, 10, "ELE NÃO ", "ACREDITOU")
            if self.app.var_barra.get():
                self.create_rectangle(x0, y1 - 4, x0 + (x1 - x0) * 0.55, y1, fill=COR_DESTAQUE2, outline="")

    ETAPAS = [("Baixar o vídeo", 22), ("Chat da live", 25.5), ("Transcrever a fala", 68.5),
              ("Achar os melhores momentos", 80), ("Gerar os cortes", 100.01)]

    class Etapas(tk.Frame):
        """Lista de etapas: ✓ feitas, a atual girando, as próximas apagadas."""

        def __init__(self, master, bg=COR_CARD):
            super().__init__(master, bg=bg)
            self.linhas = []
            self.atual = -1
            self.ativo = False
            self.ang = 90
            for nome, _ in ETAPAS:
                f = tk.Frame(self, bg=bg)
                f.pack(fill="x", pady=4)
                cv = tk.Canvas(f, width=22, height=22, bg=bg, highlightthickness=0)
                cv.pack(side="left")
                lb = tk.Label(f, text=nome, bg=bg, fg="#8D88A3", font=("Segoe UI", 10))
                lb.pack(side="left", padx=(10, 0))
                dt = tk.Label(f, text="", bg=bg, fg=COR_DESTAQUE2, font=("Segoe UI", 9, "bold"))
                dt.pack(side="right")
                self.linhas.append((cv, lb, dt))
            self.resetar()
            self._animar()

        def _icone(self, cv, est):
            cv.delete("all")
            if est == "feito":
                cv.create_oval(1, 1, 21, 21, fill="#14532D", outline="")
                cv.create_line(6, 11, 10, 15, 16, 7, fill="#86EFAC", width=2.5)
            elif est == "atual":
                cv.create_oval(3, 3, 19, 19, outline="#3B3456", width=3)
                cv.create_arc(3, 3, 19, 19, start=self.ang, extent=110, style="arc", outline=COR_DESTAQUE2, width=3)
            elif est == "erro":
                cv.create_oval(1, 1, 21, 21, fill="#5A1D1D", outline="")
                cv.create_line(7, 7, 15, 15, fill="#FCA5A5", width=2.5)
                cv.create_line(15, 7, 7, 15, fill="#FCA5A5", width=2.5)
            else:
                cv.create_oval(2, 2, 20, 20, outline="#34304A", width=2)

        def _estado(self, i, est, det=""):
            cv, lb, dt = self.linhas[i]
            self._icone(cv, est)
            lb.config(fg=COR_TEXTO if est in ("feito", "atual", "erro") else "#8D88A3",
                      font=("Segoe UI", 10, "bold" if est == "atual" else "normal"))
            dt.config(text=det)

        def resetar(self):
            self.atual = -1
            self.ativo = False
            for i in range(len(ETAPAS)):
                self._estado(i, "pend")

        def atualizar(self, p, texto=""):
            self.ativo = True
            idx = next((i for i, (_, fim) in enumerate(ETAPAS) if p < fim), len(ETAPAS))
            m = re.search(r"(\d+)%", texto or "")
            for i in range(len(ETAPAS)):
                est = "feito" if i < idx else ("atual" if i == idx else "pend")
                self._estado(i, est, (m.group(1) + "%") if (i == idx and m) else "")
            self.atual = idx

        def concluir(self):
            self.ativo = False
            for i in range(len(ETAPAS)):
                self._estado(i, "feito")

        def parar(self, erro=False):
            self.ativo = False
            if 0 <= self.atual < len(ETAPAS):
                self._estado(self.atual, "erro" if erro else "pend")

        def _animar(self):
            if not self.winfo_exists():
                return
            if self.ativo and 0 <= self.atual < len(ETAPAS):
                self.ang = (self.ang - 24) % 360
                self._icone(self.linhas[self.atual][0], "atual")
            self.after(70, self._animar)

    class Rolagem(tk.Frame):
        """Área com barra de rolagem (e rodinha do mouse)."""

        def __init__(self, master, bg=COR_FUNDO):
            super().__init__(master, bg=bg)
            self.cv = tk.Canvas(self, bg=bg, highlightthickness=0)
            self.sb = ttk.Scrollbar(self, orient="vertical", command=self.cv.yview)
            self.dentro = tk.Frame(self.cv, bg=bg)
            self.item = self.cv.create_window(0, 0, window=self.dentro, anchor="nw")
            self.cv.configure(yscrollcommand=self.sb.set)
            self.cv.pack(side="left", fill="both", expand=True)
            self.sb.pack(side="right", fill="y")
            self.dentro.bind("<Configure>", lambda e: self.cv.configure(scrollregion=self.cv.bbox("all")))
            self.cv.bind("<Configure>", lambda e: self.cv.itemconfigure(self.item, width=e.width))
            self.bind("<Enter>", lambda e: self.bind_all("<MouseWheel>", self._roda))
            self.bind("<Leave>", lambda e: self.unbind_all("<MouseWheel>"))

        def _roda(self, e):
            self.cv.yview_scroll(int(-e.delta / 120) or (-1 if e.delta > 0 else 1), "units")

    class PainelDesempenho(tk.Frame):
        """Anota as views de cada corte postado; com isso o LukClips ajusta
        o peso de cada sinal para cada streamer. (Agora é uma página da janela.)"""

        def __init__(self, master):
            super().__init__(master, bg=COR_FUNDO)
            self.hist = carregar_historico()
            filtro = tk.Frame(self, bg=COR_FUNDO)
            filtro.pack(fill="x")
            tk.Label(filtro, text="Streamer:", bg=COR_FUNDO, fg=COR_SUAVE).pack(side="left")
            self.var_st = tk.StringVar(value="Todos")
            self.cb = ttk.Combobox(filtro, textvariable=self.var_st, state="readonly", values=["Todos"], width=28)
            self.cb.pack(side="left", padx=8)
            self.cb.bind("<<ComboboxSelected>>", lambda e: self._preencher())
            tk.Label(filtro, text="Dê dois cliques numa linha para digitar as views. Com 6 ou mais cortes "
                                  "anotados do mesmo streamer, o LukClips aprende o que dá certo com ele.",
                     bg=COR_FUNDO, fg=COR_SUAVE, font=("Segoe UI", 9), wraplength=560,
                     justify="left").pack(side="left", padx=(16, 0))
            cols = ("data", "streamer", "titulo", "views")
            self.tv = ttk.Treeview(self, columns=cols, show="headings")
            for c, t, w in zip(cols, ("Data", "Streamer", "Título", "Views"), (90, 140, 460, 90)):
                self.tv.heading(c, text=t)
                self.tv.column(c, width=w, anchor="w" if c != "views" else "e", stretch=(c == "titulo"))
            self.tv.pack(fill="both", expand=True, pady=12)
            self.tv.bind("<Double-1>", self._editar)
            base = tk.Frame(self, bg=COR_FUNDO)
            base.pack(fill="x")
            self.lbl = tk.Label(base, text="", bg=COR_FUNDO, fg=COR_SUAVE, font=("Segoe UI", 9))
            self.lbl.pack(side="left")
            ttk.Button(base, text="Salvar e aprender", style="Acao.TButton",
                       command=self._salvar).pack(side="right")
            ttk.Button(base, text="Postar selecionado", style="Sec.TButton",
                       command=self._postar).pack(side="right", padx=8)
            self.recarregar()

        def recarregar(self):
            self.hist = carregar_historico()
            nomes = sorted({h.get("streamer", "") for h in self.hist if h.get("streamer")})
            self.cb.config(values=["Todos"] + nomes)
            self._preencher()

        def _preencher(self):
            self.tv.delete(*self.tv.get_children())
            alvo = self.var_st.get()
            for i in range(len(self.hist) - 1, -1, -1):
                h = self.hist[i]
                if alvo != "Todos" and h.get("streamer") != alvo:
                    continue
                v = h.get("views")
                self.tv.insert("", "end", iid=str(i), values=(
                    h.get("data", ""), h.get("streamer", ""), h.get("titulo", ""),
                    f"{v:,}".replace(",", ".") if isinstance(v, (int, float)) else "—"))

        def _editar(self, _e=None):
            from tkinter import simpledialog
            sel = self.tv.selection()
            if not sel:
                return
            i = int(sel[0])
            atual = self.hist[i].get("views")
            v = simpledialog.askinteger("Views", f"Quantas visualizações teve?\n\n{self.hist[i].get('titulo', '')}",
                                        parent=self.winfo_toplevel(), minvalue=0, initialvalue=atual or 0)
            if v is not None:
                self.hist[i]["views"] = v
                self._preencher()
                self.tv.selection_set(str(i))

        def _postar(self):
            sel = self.tv.selection()
            if not sel:
                messagebox.showinfo(APP, "Selecione um corte na lista.", parent=self.winfo_toplevel())
                return
            h = self.hist[int(sel[0])]
            if not Path(h.get("arquivo", "")).exists():
                messagebox.showwarning(APP, "Não achei o arquivo desse corte (foi movido ou apagado?).",
                                       parent=self.winfo_toplevel())
                return
            salvar_historico(self.hist)
            JanelaPostar(self.winfo_toplevel(), h)

        def _salvar(self):
            salvar_historico(self.hist)
            msgs = []
            for nome in sorted({h.get("streamer", "") for h in self.hist if h.get("streamer")}):
                pesos, qtd = M.recalcular_pesos(self.hist, nome)
                if pesos:
                    perfil = M.carregar_perfil(PASTA_CONFIG, nome)
                    perfil["pesos"] = pesos
                    M.salvar_perfil(PASTA_CONFIG, nome, perfil)
                    fortes = [M.NOMES_SINAIS[k] for k, v in sorted(pesos.items(), key=lambda x: -x[1])[:2]]
                    msgs.append(f"{nome}: o que mais funciona é {' e '.join(fortes)}")
                elif qtd:
                    msgs.append(f"{nome}: {qtd} de 6 cortes anotados")
            self.lbl.config(text="  |  ".join(msgs) or "Salvo.", fg=COR_TEXTO)

    class Botao3D:
        """Botão desenhado no fundo: tem lateral (profundidade) e afunda ao clicar."""
        CORES = {"Grande.TButton": ("#A78BFA", "#7C3AED", "#4C1D95", "#2A0E5C"),
                 "Parar.TButton": ("#F87171", "#DC2626", "#7F1D1D", "#4A0E0E")}

        def __init__(self, cv, texto, comando):
            self.cv, self.texto, self.cmd = cv, texto, comando
            self.estilo = "Grande.TButton"
            self.hover = self.apertado = False
            self.geo = None
            self.imgs = {}
            fonte = ("Segoe UI", 14, "bold")
            cv.create_image(0, 0, anchor="nw", tags=("btn3d", "btn3d_img"))
            cv.create_text(0, 0, text=texto, fill="#2A0E5C", font=fonte, tags=("btn3d", "btn3d_sombra"))
            cv.create_text(0, 0, text=texto, fill="white", font=fonte, tags=("btn3d", "btn3d_txt"))
            cv.tag_bind("btn3d", "<Enter>", self._entrar)
            cv.tag_bind("btn3d", "<Leave>", self._sair)
            cv.tag_bind("btn3d", "<ButtonPress-1>", self._apertar)
            cv.tag_bind("btn3d", "<ButtonRelease-1>", self._soltar)

        def posicionar(self, x, y, w, h):
            self.geo = (int(x), int(y), int(w), int(h))
            self._desenhar()

        def config(self, text=None, style=None, **_):
            if text is not None:
                self.texto = text
            if style is not None and style in self.CORES:
                self.estilo = style
            self._desenhar()

        configure = config

        def _imagem(self):
            _, _, w, h = self.geo
            chave = (w, h, self.estilo, self.hover, self.apertado)
            if chave not in self.imgs:
                if len(self.imgs) > 16:
                    self.imgs.clear()
                arq = PASTA_CACHE_UI / f"btn_{w}x{h}_{self.estilo[:5]}_{int(self.hover)}{int(self.apertado)}.png"
                cima, baixo, lado, _ = self.CORES[self.estilo]
                _, meio = V.gerar_botao(w, h, arq, cima, baixo, lado, apertado=self.apertado,
                                        hover=self.hover)
                self.imgs[chave] = (tk.PhotoImage(file=str(arq)), meio)
            return self.imgs[chave]

        def _desenhar(self):
            if not self.geo:
                return
            x, y, w, h = self.geo
            try:
                img, meio = self._imagem()
                self.cv.itemconfigure("btn3d_img", image=img)
            except Exception:
                meio = h / 2
            self.cv.coords("btn3d_img", x, y)
            self.cv.coords("btn3d_sombra", x + w / 2 + 1, y + meio + 2)
            self.cv.coords("btn3d_txt", x + w / 2, y + meio)
            self.cv.itemconfigure("btn3d_sombra", text=self.texto, fill=self.CORES[self.estilo][3])
            self.cv.itemconfigure("btn3d_txt", text=self.texto)
            self.cv.tag_raise("btn3d")

        def _entrar(self, _e):
            self.hover = True
            self.cv.configure(cursor="hand2")
            self._desenhar()

        def _sair(self, _e):
            self.hover = self.apertado = False
            self.cv.configure(cursor="")
            self._desenhar()

        def _apertar(self, _e):
            self.apertado = True
            self._desenhar()

        def _soltar(self, _e):
            if self.apertado:
                self.apertado = False
                self._desenhar()
                self.cv.after(60, self.cmd)

    class JanelaPostar(tk.Toplevel):
        """Edita título/descrição/hashtags e ajuda a postar: copia o texto,
        abre a página de envio no navegador e a pasta com o vídeo selecionado."""
        PLATAFORMAS = [
            ("YouTube Shorts", "youtube", "#FF0033", "white", "https://www.youtube.com/upload",
             "Título copiado! Na página que abriu, arraste o vídeo da pasta e cole o título (Ctrl+V). "
             "Depois clique em 'Copiar descrição' e cole no campo de descrição."),
            ("TikTok", "tiktok", "#25F4EE", "#111111", "https://www.tiktok.com/upload",
             "Legenda copiada! Na página que abriu, arraste o vídeo da pasta e cole a legenda (Ctrl+V)."),
            ("Instagram Reels", "instagram", "#E1306C", "white", "https://www.instagram.com/",
             "Legenda copiada! No Instagram clique em '+ Criar', arraste o vídeo da pasta e cole a "
             "legenda (Ctrl+V)."),
        ]

        def __init__(self, master, corte):
            super().__init__(master)
            self.title("Postar corte")
            self.configure(bg=COR_FUNDO)
            self.resizable(False, False)
            self.corte = corte
            self.arq = Path(corte["arquivo"])
            self.arq_txt = self.arq.with_suffix(".txt")
            d = ler_post(self.arq_txt) or {"titulo": corte.get("titulo", ""), "descricao": "", "hashtags": ""}

            raiz = tk.Frame(self, bg=COR_FUNDO)
            raiz.pack(padx=16, pady=14)
            # miniatura
            esq = tk.Frame(raiz, bg=COR_FUNDO)
            esq.grid(row=0, column=0, sticky="n", padx=(0, 16))
            self.thumb = None
            try:
                png = PASTA_CACHE_UI / "miniatura.png"
                subprocess.run([achar_bin("ffmpeg"), "-y", "-loglevel", "error", "-ss", "1.5",
                                "-i", str(self.arq), "-frames:v", "1", "-vf", "scale=162:288", str(png)],
                               timeout=20, **SEM_JANELA)
                self.thumb = tk.PhotoImage(file=str(png))
                tk.Label(esq, image=self.thumb, bg="#000", bd=0).pack()
            except Exception:
                tk.Label(esq, text="(sem prévia)", bg=COR_CARD, fg=COR_SUAVE, width=22, height=14).pack()
            ttk.Button(esq, text="▶ Assistir", style="Sec.TButton",
                       command=lambda: abrir(self.arq)).pack(fill="x", pady=(8, 0))

            # campos
            dir_ = tk.Frame(raiz, bg=COR_FUNDO)
            dir_.grid(row=0, column=1, sticky="n")

            def rot(t):
                tk.Label(dir_, text=t, bg=COR_FUNDO, fg=COR_SUAVE, font=("Segoe UI", 9)).pack(anchor="w", pady=(6, 2))

            topo = tk.Frame(dir_, bg=COR_FUNDO)
            topo.pack(fill="x")
            tk.Label(topo, text="Título", bg=COR_FUNDO, fg=COR_SUAVE, font=("Segoe UI", 9)).pack(side="left")
            self.lbl_cont = tk.Label(topo, text="", bg=COR_FUNDO, fg=COR_SUAVE, font=("Segoe UI", 8))
            self.lbl_cont.pack(side="right")
            self.var_tit = tk.StringVar(value=d["titulo"])
            self.var_tit.trace_add("write", lambda *_: self._contar())
            ttk.Entry(dir_, textvariable=self.var_tit, width=58, font=("Segoe UI", 10)).pack(fill="x")
            rot("Descrição")
            self.txt_desc = tk.Text(dir_, height=4, width=58, bg=COR_CAMPO, fg=COR_TEXTO, relief="flat",
                                    font=("Segoe UI", 10), wrap="word", insertbackground=COR_TEXTO,
                                    padx=8, pady=6, highlightthickness=1, highlightbackground=COR_BORDA,
                                    highlightcolor=COR_DESTAQUE)
            self.txt_desc.insert("1.0", d["descricao"])
            self.txt_desc.pack(fill="x")
            rot("Hashtags")
            self.var_tags = tk.StringVar(value=d["hashtags"])
            ttk.Entry(dir_, textvariable=self.var_tags, width=58).pack(fill="x")
            ttk.Button(dir_, text="💾 Salvar texto", style="Sec.TButton",
                       command=self._salvar).pack(anchor="e", pady=(8, 4))

            tk.Label(dir_, text="Postar em", bg=COR_FUNDO, fg=COR_TEXTO,
                     font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(8, 4))
            self.postado = (buscar_historico(corte.get("id")) or {}).get("postado") or {}
            self.vars_post = {}
            for nome, chave, cor, fg, url, dica in self.PLATAFORMAS:
                lin = tk.Frame(dir_, bg=COR_CARD, padx=10, pady=8)
                lin.pack(fill="x", pady=3)
                tk.Label(lin, text=f" {nome} ", bg=cor, fg=fg, font=("Segoe UI", 9, "bold")).pack(side="left")
                var = tk.BooleanVar(value=chave in self.postado)
                self.vars_post[chave] = var
                tk.Checkbutton(lin, text="já postei", variable=var, bg=COR_CARD, fg=COR_SUAVE,
                               selectcolor=COR_CAMPO, activebackground=COR_CARD, font=("Segoe UI", 9),
                               command=lambda c=chave: self._marcar(c), highlightthickness=0,
                               bd=0).pack(side="right")
                ttk.Button(lin, text="Abrir e copiar", style="Acao.TButton",
                           command=lambda c=chave, u=url, t=dica: self._postar(c, u, t)).pack(side="right", padx=6)
                if chave == "youtube":
                    ttk.Button(lin, text="Copiar descrição", style="Sec.TButton",
                               command=lambda: self._copiar(self._texto("youtube_desc"),
                                                            "Descrição copiada! Cole no campo de descrição.")
                               ).pack(side="right")
            self.lbl_dica = tk.Label(dir_, text="Clique em 'Abrir e copiar': o texto vai pra área de "
                                                "transferência, a página de envio abre e a pasta mostra o vídeo.",
                                     bg=COR_FUNDO, fg=COR_SUAVE, font=("Segoe UI", 9), wraplength=440,
                                     justify="left")
            self.lbl_dica.pack(anchor="w", pady=(8, 0))
            self._contar()
            self.transient(master)
            self.focus_force()

        def _contar(self):
            n = len(self.var_tit.get())
            self.lbl_cont.config(text=f"{n}/100 (limite do YouTube)", fg="#FF6B6B" if n > 100 else COR_SUAVE)

        def _dados(self):
            return {"titulo": self.var_tit.get().strip(),
                    "descricao": self.txt_desc.get("1.0", "end").strip(),
                    "hashtags": self.var_tags.get().strip()}

        def _texto(self, tipo):
            d = self._dados()
            if tipo == "youtube":
                return d["titulo"][:100]
            if tipo == "youtube_desc":
                tags = d["hashtags"] + ("" if "#shorts" in d["hashtags"].lower() else " #shorts")
                return f"{d['descricao']}\n\n{tags.strip()}".strip()
            return "\n\n".join(p for p in (d["titulo"], d["descricao"], d["hashtags"]) if p)

        def _salvar(self):
            escrever_post(self.arq_txt, **self._dados())
            self.lbl_dica.config(text="💾 Texto salvo.", fg="#3DD68C")

        def _copiar(self, texto, aviso):
            self.clipboard_clear()
            self.clipboard_append(texto)
            self.update()
            self.lbl_dica.config(text="📋 " + aviso, fg="#3DD68C")

        def _postar(self, chave, url, dica):
            import webbrowser
            self._salvar()
            self._copiar(self._texto(chave), dica)
            mostrar_no_explorador(self.arq)
            webbrowser.open(url)

        def _marcar(self, chave):
            if self.vars_post[chave].get():
                self.postado[chave] = f"{datetime.now():%Y-%m-%d}"
            else:
                self.postado.pop(chave, None)
            atualizar_historico(self.corte.get("id"), {"postado": self.postado})

    class JanelaErro(tk.Toplevel):
        """Erro com botão de copiar os detalhes (pra mandar pro suporte)."""

        def __init__(self, master, msg, detalhes=""):
            super().__init__(master)
            self.title(f"{APP} — erro")
            self.configure(bg=COR_FUNDO)
            self.resizable(False, False)
            self.texto = (f"{APP} v{VERSAO} | {sys.platform} | Python {sys.version.split()[0]}\n"
                          f"{datetime.now():%Y-%m-%d %H:%M:%S}\n\n{msg}\n\n{detalhes}").strip()
            tk.Label(self, text="😕  Deu um problema", bg=COR_FUNDO, fg=COR_TEXTO,
                     font=("Segoe UI", 13, "bold")).pack(anchor="w", padx=18, pady=(16, 6))
            tk.Label(self, text=msg, bg=COR_FUNDO, fg=COR_TEXTO, font=("Segoe UI", 10), wraplength=480,
                     justify="left").pack(anchor="w", padx=18)
            if detalhes:
                caixa = tk.Text(self, height=8, width=72, bg=COR_CARD, fg=COR_SUAVE, relief="flat",
                                font=("Consolas", 8), wrap="none", padx=8, pady=6)
                caixa.insert("1.0", detalhes.strip()[-3000:])
                caixa.configure(state="disabled")
                caixa.pack(padx=18, pady=(10, 0))
            bar = tk.Frame(self, bg=COR_FUNDO)
            bar.pack(fill="x", padx=18, pady=14)
            self.lbl = tk.Label(bar, text="", bg=COR_FUNDO, fg="#3DD68C", font=("Segoe UI", 9))
            self.lbl.pack(side="left")
            ttk.Button(bar, text="Fechar", style="Sec.TButton", command=self.destroy).pack(side="right")
            ttk.Button(bar, text="📂 Pasta de logs", style="Sec.TButton",
                       command=lambda: abrir(PASTA_LOGS)).pack(side="right", padx=6)
            ttk.Button(bar, text="📋 Copiar detalhes", style="Acao.TButton",
                       command=self._copiar).pack(side="right")
            self.transient(master)
            self.focus_force()

        def _copiar(self):
            self.clipboard_clear()
            self.clipboard_append(self.texto)
            self.update()
            self.lbl.config(text="Copiado! Cole na conversa com quem vai te ajudar.")

    class JanelaRevisao(tk.Toplevel):
        """Mostra os momentos escolhidos (e reservas) antes de gerar: marcar/desmarcar,
        ajustar início e fim, trocar o título, corrigir a legenda e ver uma prévia."""

        def __init__(self, master, P, ao_fim):
            super().__init__(master)
            self.title("Revisar os momentos")
            self.configure(bg=COR_FUNDO)
            self.P, self.ao_fim = P, ao_fim
            self.cands = P["cands"]
            import copy
            self.originais = copy.deepcopy(self.cands)
            self.tr = P["transcript"]
            self.atual = None
            self.linhas_idx = []
            self.respondido = False
            esc = max(1.0, self.winfo_fpixels("1i") / 96)
            self.geometry(f"{int(1060 * esc)}x{min(int(640 * esc), self.winfo_screenheight() - 80)}")
            self.protocol("WM_DELETE_WINDOW", self._cancelar)

            tk.Label(self, text="Revise os momentos antes de gerar", bg=COR_FUNDO, fg=COR_TEXTO,
                     font=("Segoe UI", 13, "bold")).pack(anchor="w", padx=16, pady=(12, 0))
            tk.Label(self, text="Clique na coluna ✓ para escolher quais viram corte. Os que vêm desmarcados "
                                "são reservas. Selecione um para ajustar.",
                     bg=COR_FUNDO, fg=COR_SUAVE, font=("Segoe UI", 9)).pack(anchor="w", padx=16, pady=(2, 8))
            corpo = tk.Frame(self, bg=COR_FUNDO)
            corpo.pack(fill="both", expand=True, padx=16)
            corpo.columnconfigure(0, weight=5)
            corpo.columnconfigure(1, weight=6)
            corpo.rowconfigure(0, weight=1)

            s = ttk.Style(self)
            s.configure("Treeview", background=COR_CARD, fieldbackground=COR_CARD,
                        foreground=COR_TEXTO, rowheight=28, borderwidth=0)
            s.configure("Treeview.Heading", background=COR_CAMPO, foreground=COR_TEXTO)
            s.map("Treeview", background=[("selected", COR_DESTAQUE)])
            cols = ("usar", "nota", "inicio", "dur", "titulo")
            self.tv = ttk.Treeview(corpo, columns=cols, show="headings", selectmode="browse")
            for c, t, w in zip(cols, ("✓", "Nota", "Início", "Dur.", "Título"), (36, 50, 64, 50, 260)):
                self.tv.heading(c, text=t)
                self.tv.column(c, width=w, anchor="center" if c != "titulo" else "w", stretch=(c == "titulo"))
            self.tv.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
            self.tv.bind("<<TreeviewSelect>>", lambda e: self._selecionar())
            self.tv.bind("<Button-1>", self._clique)

            d = tk.Frame(corpo, bg=COR_CARD, padx=14, pady=12)
            d.grid(row=0, column=1, sticky="nsew")
            tk.Label(d, text="Título", bg=COR_CARD, fg=COR_SUAVE, font=("Segoe UI", 9)).pack(anchor="w")
            self.var_tit = tk.StringVar()
            ttk.Entry(d, textvariable=self.var_tit).pack(fill="x", pady=(2, 10))
            tempos = tk.Frame(d, bg=COR_CARD)
            tempos.pack(fill="x")
            self.lbl_ini = tk.Label(tempos, text="", bg=COR_CARD, fg=COR_TEXTO, font=("Segoe UI", 10, "bold"))
            self.lbl_fim = tk.Label(tempos, text="", bg=COR_CARD, fg=COR_TEXTO, font=("Segoe UI", 10, "bold"))
            self.lbl_ini.destroy()
            self.lbl_fim.destroy()
            rotulos = {}
            for rot, campo in (("Início", "ini"), ("Fim", "fim")):
                f = tk.Frame(tempos, bg=COR_CARD)
                f.pack(side="left", padx=(0, 22))
                tk.Label(f, text=rot, bg=COR_CARD, fg=COR_SUAVE, font=("Segoe UI", 9)).pack(anchor="w")
                lin = tk.Frame(f, bg=COR_CARD)
                lin.pack()
                for txt, dlt in (("−5", -5), ("−1", -1)):
                    ttk.Button(lin, text=txt, width=3, style="Sec.TButton",
                               command=lambda c=campo, x=dlt: self._mover(c, x)).pack(side="left")
                rotulos[campo] = tk.Label(lin, text="", bg=COR_CARD, fg=COR_TEXTO, width=9,
                                          font=("Segoe UI", 10, "bold"))
                rotulos[campo].pack(side="left", padx=6)
                for txt, dlt in (("+1", 1), ("+5", 5)):
                    ttk.Button(lin, text=txt, width=3, style="Sec.TButton",
                               command=lambda c=campo, x=dlt: self._mover(c, x)).pack(side="left")
            self.lbl_ini, self.lbl_fim = rotulos["ini"], rotulos["fim"]
            self.lbl_dur = tk.Label(tempos, text="", bg=COR_CARD, fg=COR_SUAVE, font=("Segoe UI", 9))
            self.lbl_dur.pack(side="left", anchor="s")
            ttk.Button(d, text="▶ Ver prévia do trecho", style="Acao.TButton",
                       command=self._previa).pack(anchor="w", pady=(10, 8))
            tk.Label(d, text="Legenda (corrija palavras que o Whisper errou — uma linha por fala):",
                     bg=COR_CARD, fg=COR_SUAVE, font=("Segoe UI", 9)).pack(anchor="w")
            self.txt = tk.Text(d, height=8, bg=COR_CAMPO, fg=COR_TEXTO, relief="flat", font=("Segoe UI", 10),
                               wrap="word", insertbackground=COR_TEXTO, padx=8, pady=6, undo=True,
                               highlightthickness=1, highlightbackground=COR_BORDA, highlightcolor=COR_DESTAQUE)
            self.txt.pack(fill="both", expand=True, pady=(4, 0))
            self.lbl_aviso = tk.Label(d, text="", bg=COR_CARD, fg="#FFB020", font=("Segoe UI", 9))
            self.lbl_aviso.pack(anchor="w", pady=(4, 0))

            base = tk.Frame(self, bg=COR_FUNDO)
            base.pack(fill="x", padx=16, pady=12)
            self.lbl_qtd = tk.Label(base, text="", bg=COR_FUNDO, fg=COR_TEXTO, font=("Segoe UI", 10))
            self.lbl_qtd.pack(side="left")
            self.var_nao_mostrar = tk.BooleanVar(value=False)
            tk.Checkbutton(base, text="Não mostrar esta tela de novo", variable=self.var_nao_mostrar,
                           bg=COR_FUNDO, fg=COR_SUAVE, selectcolor=COR_CAMPO, activebackground=COR_FUNDO,
                           activeforeground=COR_TEXTO, font=("Segoe UI", 9), highlightthickness=0,
                           bd=0).pack(side="left", padx=(18, 0))
            ttk.Button(base, text="✂ Gerar os cortes marcados", style="Acao.TButton",
                       command=self._confirmar).pack(side="right")
            ttk.Button(base, text="⏭ Pular revisão", style="Sec.TButton",
                       command=self._pular).pack(side="right", padx=8)
            ttk.Button(base, text="Cancelar", style="Sec.TButton",
                       command=self._cancelar).pack(side="right")
            self._preencher()
            if self.cands:
                self.tv.selection_set("0")
            self.grab_set()
            self.focus_force()

        # -- lista
        def _preencher(self):
            """Atualiza as linhas no lugar. Não mexe na seleção: re-selecionar disparava
            o evento de seleção de novo e travava a janela num laço sem fim."""
            existentes = set(self.tv.get_children())
            for i, c in enumerate(self.cands):
                valores = ("✅" if c["escolhido"] else "⬜", f"{c['nota']:.1f}",
                           ts(c["ini"])[:-3].lstrip("0:") or "0", f"{c['fim'] - c['ini']:.0f}s", c["titulo"])
                if str(i) in existentes:
                    self.tv.item(str(i), values=valores)
                else:
                    self.tv.insert("", "end", iid=str(i), values=valores)
            n = sum(c["escolhido"] for c in self.cands)
            self.lbl_qtd.config(text=f"{n} corte(s) marcado(s)")

        def _clique(self, e):
            if self.tv.identify_region(e.x, e.y) == "cell" and self.tv.identify_column(e.x) == "#1":
                iid = self.tv.identify_row(e.y)
                if iid:
                    c = self.cands[int(iid)]
                    c["escolhido"] = not c["escolhido"]
                    self._preencher()
                    return "break"

        def _selecionar(self):
            sel = self.tv.selection()
            if not sel or int(sel[0]) == self.atual:
                return
            self._guardar()
            self.atual = int(sel[0])
            c = self.cands[self.atual]
            self.var_tit.set(c["titulo"])
            self._mostrar_tempos()
            self._mostrar_legenda()

        def _mostrar_tempos(self):
            c = self.cands[self.atual]
            self.lbl_ini.config(text=ts(c["ini"])[:-3])
            self.lbl_fim.config(text=ts(c["fim"])[:-3])
            self.lbl_dur.config(text=f"duração {c['fim'] - c['ini']:.0f}s")

        def _mostrar_legenda(self):
            c = self.cands[self.atual]
            self.linhas_idx = [i for i, s in enumerate(self.tr) if s["end"] > c["ini"] and s["start"] < c["fim"]]
            self.txt.delete("1.0", "end")
            linhas = [c["edicoes"].get(i, self.tr[i]["text"]) for i in self.linhas_idx]
            self.txt.insert("1.0", "\n".join(linhas))
            self.txt.edit_reset()
            self.lbl_aviso.config(text="")

        def _guardar(self):
            """Guarda título e correções da legenda do corte que estava aberto."""
            if self.atual is None:
                return
            c = self.cands[self.atual]
            c["titulo"] = self.var_tit.get().strip() or c["titulo"]
            linhas = self.txt.get("1.0", "end").rstrip("\n").split("\n")
            if len(linhas) != len(self.linhas_idx):
                self.lbl_aviso.config(text="⚠ Mantenha uma linha por fala (não junte nem separe linhas). "
                                           "A correção dessa vez não foi salva.")
                return
            for i, texto in zip(self.linhas_idx, linhas):
                if texto.strip() != self.tr[i]["text"].strip():
                    c["edicoes"][i] = texto.strip()
                else:
                    c["edicoes"].pop(i, None)
            self._preencher()

        def _mover(self, campo, delta):
            if self.atual is None:
                return
            self._guardar()
            c = self.cands[self.atual]
            dur_total = self.P["dur_total"]
            if campo == "ini":
                c["ini"] = float(min(max(0.0, c["ini"] + delta), c["fim"] - 5))
            else:
                c["fim"] = float(max(min(dur_total, c["fim"] + delta), c["ini"] + 5))
            self._mostrar_tempos()
            self._mostrar_legenda()
            self._preencher()

        def _previa(self):
            if self.atual is None:
                return
            c = self.cands[self.atual]
            video = str(self.P["video"])
            ffplay = achar_bin("ffplay")
            if ffplay:
                subprocess.Popen([ffplay, "-hide_banner", "-loglevel", "error", "-autoexit",
                                  "-window_title", f"Prévia — {c['titulo']}", "-x", "800", "-y", "450",
                                  "-ss", f"{c['ini']:.2f}", "-t", f"{c['fim'] - c['ini']:.2f}", video],
                                 **SEM_JANELA)
                return
            self.lbl_aviso.config(text="Gerando prévia...", fg=COR_SUAVE)
            destino = PASTA_CACHE_UI / "previa.mp4"

            def tarefa():
                try:
                    subprocess.run([achar_bin("ffmpeg"), "-y", "-loglevel", "error", "-ss", f"{c['ini']:.2f}",
                                    "-t", f"{c['fim'] - c['ini']:.2f}", "-i", video, "-vf", "scale=-2:480",
                                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-c:a", "aac",
                                    str(destino)], check=True, timeout=300, **SEM_JANELA)
                    self.after(0, lambda: (self.lbl_aviso.config(text=""), abrir(destino)))
                except Exception as e:
                    self.after(0, lambda: self.lbl_aviso.config(text=f"Não deu para gerar a prévia: {e}",
                                                                 fg="#FFB020"))
            threading.Thread(target=tarefa, daemon=True).start()

        def _lembrar_escolha(self):
            """Se marcou 'não mostrar de novo', desliga a revisão nas opções (aba ⚙ Análise)."""
            if self.var_nao_mostrar.get():
                try:
                    self.master.var_revisar.set(False)
                    self.master._coletar_cfg()
                    self.master._log("ℹ Revisão desligada. Para ligar de novo: aba ⚙ Análise → "
                                     "'Revisar antes de gerar'.")
                except Exception:
                    logging.exception("salvar opção de revisão")

        def _pular(self):
            """Gera direto os momentos que o LukClips escolheu, sem as mudanças feitas aqui."""
            escolhidos = [c for c in self.originais if c["escolhido"]]
            if not escolhidos:
                escolhidos = self.originais[:max(1, int(self.P.get("qtd", 5)))]
            self._lembrar_escolha()
            self.respondido = True
            self.grab_release()
            self.destroy()
            self.ao_fim(escolhidos)

        def _confirmar(self):
            self._lembrar_escolha()
            self._guardar()
            escolhidos = [c for c in self.cands if c["escolhido"]]
            if not escolhidos:
                messagebox.showwarning(APP, "Marque pelo menos um momento (coluna ✓).", parent=self)
                return
            self.respondido = True
            self.grab_release()
            self.destroy()
            self.ao_fim(escolhidos)

        def _cancelar(self):
            if not self.respondido and not messagebox.askyesno(APP, "Cancelar e não gerar nenhum corte?",
                                                                parent=self):
                return
            self.respondido = True
            self.grab_release()
            self.destroy()
            self.ao_fim(None)

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.cfg = carregar_config()
            PASTA_CACHE_UI.mkdir(parents=True, exist_ok=True)
            self.title(f"{APP} v{VERSAO} — Fábrica de cortes")
            self.configure(bg=COR_FUNDO)
            # ajusta o tamanho da janela ao zoom do Windows (125%, 150%...)
            esc = max(1.0, self.winfo_fpixels("1i") / 96)
            alt_tela = self.winfo_screenheight()
            larg_tela = self.winfo_screenwidth()
            self.geometry(f"{min(int(1280 * esc), larg_tela - 40)}x{min(int(800 * esc), alt_tela - 80)}")
            self.minsize(min(int(1180 * esc), larg_tela - 40), min(int(700 * esc), alt_tela - 80))
            try:
                self.icone = tk.PhotoImage(file=str(PASTA_RECURSOS / "lukclips.png"))
                self.iconphoto(True, self.icone)
            except Exception:
                pass
            self.fila = queue.Queue()
            self.cancel = threading.Event()
            self.rodando = False
            self.ultima_pasta = None
            self.cortes_info, self.arquivos, self.fila_itens = [], [], []
            self.thumbs, self.thumb_lbls = {}, {}
            self._cache_txt = "calculando..."
            self._registro_grande = False
            self._estilos()
            self._vars()
            self._montar()
            self._mostrar("novo")
            self.after(80, self._processar_fila)
            self.after(300, self._checar_dependencias)
            self.after(600, self._calcular_cache)
            self.after(900, self._checar_gpu)
            self.after(2500, self._procurar_atualizacao)
            self._ja_avisou = set()
            self.after(2 * 3600 * 1000, self._procurar_periodico)
            self.after(800, self._avisar_se_atualizou)
            self.report_callback_exception = self._erro_inesperado
            threading.excepthook = lambda a: self.fila.put(
                ("erro", (f"Erro inesperado: {a.exc_value}",
                          "".join(traceback.format_exception(a.exc_type, a.exc_value, a.exc_traceback)))))
            self.protocol("WM_DELETE_WINDOW", self._fechar)

        # ------------------------------------------------------ visual
        def _estilos(self):
            s = ttk.Style(self)
            s.theme_use("clam")
            s.configure(".", background=COR_FUNDO, foreground=COR_TEXTO, font=("Segoe UI", 10))
            s.configure("Card.TFrame", background=COR_CARD)
            s.configure("TLabel", background=COR_CARD, foreground=COR_TEXTO)
            s.configure("Rot.TLabel", background=COR_CARD, foreground=COR_SUAVE, font=("Segoe UI", 9))
            s.configure("TEntry", fieldbackground=COR_CAMPO, foreground=COR_TEXTO,
                        bordercolor=COR_BORDA, lightcolor=COR_BORDA, darkcolor=COR_BORDA,
                        insertcolor=COR_TEXTO, padding=8)
            s.map("TEntry", bordercolor=[("focus", COR_DESTAQUE)], lightcolor=[("focus", COR_DESTAQUE)])
            s.configure("TCombobox", fieldbackground=COR_CAMPO, background=COR_CAMPO,
                        foreground=COR_TEXTO, arrowcolor=COR_TEXTO, bordercolor=COR_BORDA,
                        lightcolor=COR_BORDA, darkcolor=COR_BORDA, padding=6)
            s.map("TCombobox", fieldbackground=[("readonly", COR_CAMPO)],
                  foreground=[("readonly", COR_TEXTO)], selectbackground=[("readonly", COR_CAMPO)],
                  selectforeground=[("readonly", COR_TEXTO)])
            self.option_add("*TCombobox*Listbox.background", COR_CAMPO)
            self.option_add("*TCombobox*Listbox.foreground", COR_TEXTO)
            self.option_add("*TCombobox*Listbox.selectBackground", COR_DESTAQUE)
            s.configure("TSpinbox", fieldbackground=COR_CAMPO, foreground=COR_TEXTO,
                        arrowcolor=COR_TEXTO, background=COR_CAMPO, bordercolor=COR_BORDA,
                        lightcolor=COR_BORDA, darkcolor=COR_BORDA, padding=6)
            s.configure("Acao.TButton", background=COR_DESTAQUE, foreground="white",
                        font=("Segoe UI", 10, "bold"), padding=(16, 8), borderwidth=0)
            s.map("Acao.TButton", background=[("active", COR_DESTAQUE2), ("disabled", "#3A3A48")])
            s.configure("MiniAcao.TButton", background=COR_DESTAQUE, foreground="white",
                        font=("Segoe UI", 9, "bold"), padding=(8, 5), borderwidth=0)
            s.map("MiniAcao.TButton", background=[("active", COR_DESTAQUE2)])
            s.configure("Sec.TButton", background=COR_CAMPO, foreground=COR_TEXTO,
                        padding=(12, 7), borderwidth=0)
            s.map("Sec.TButton", background=[("active", COR_BORDA)])
            s.configure("Mini.TButton", background=COR_CAMPO, foreground=COR_TEXTO,
                        font=("Segoe UI", 9), padding=(8, 5), borderwidth=0)
            s.map("Mini.TButton", background=[("active", COR_BORDA)])
            s.configure("Horizontal.TProgressbar", troughcolor="#262338", background=COR_DESTAQUE2,
                        bordercolor="#262338", lightcolor=COR_DESTAQUE2, darkcolor=COR_DESTAQUE2,
                        thickness=8)
            s.configure("Vertical.TScrollbar", background=COR_CAMPO, troughcolor=COR_FUNDO,
                        bordercolor=COR_FUNDO, arrowcolor=COR_SUAVE, lightcolor=COR_CAMPO, darkcolor=COR_CAMPO)
            s.configure("Treeview", background=COR_CARD, fieldbackground=COR_CARD,
                        foreground=COR_TEXTO, rowheight=28, borderwidth=0)
            s.configure("Treeview.Heading", background=COR_CAMPO, foreground=COR_TEXTO)
            s.map("Treeview", background=[("selected", COR_DESTAQUE)])

        def _vars(self):
            c = self.cfg
            self.var_saida = tk.StringVar(value=c["saida"])
            self.var_cortes = tk.StringVar(value=str(c["cortes"]))
            self.var_dur = tk.StringVar(value=c["duracao"])
            self.var_chat = tk.BooleanVar(value=c.get("usar_chat", True))
            self.var_mov = tk.BooleanVar(value=c.get("usar_movimento", True))
            self.var_modelo = tk.StringVar(value=c["modelo"])
            self.var_cam = tk.StringVar(value=c["camera"])
            self.var_nav = tk.StringVar(value=c["navegador"])
            self.var_streamer = tk.StringVar(value=c["streamer"])
            self.var_pular = tk.StringVar(value=str(c["pular_inicio"] // 60))
            self.var_marca = tk.StringVar(value=c["marca"])
            self.var_layout = tk.StringVar(value=c["layout"])
            self.var_estilo = tk.StringVar(value=c["estilo_legenda"])
            self.var_zoom = tk.BooleanVar(value=c["zoom_pico"])
            self.var_silencio = tk.BooleanVar(value=c["cortar_silencio"])
            self.var_barra = tk.BooleanVar(value=c["barra_progresso"])
            self.var_revisar = tk.BooleanVar(value=c["revisar"])
            self.var_desligar = tk.BooleanVar(value=False)
            self.var_idioma = tk.StringVar(value=c.get("idioma", "Português"))
            self.var_gpu = tk.BooleanVar(value=c.get("usar_gpu", True))
            self.var_hashtags = tk.StringVar(value=c.get("hashtags_fixas", ""))

        # ---------------- pedacinhos reutilizados
        def _card(self, pai, titulo=None):
            f = tk.Frame(pai, bg=COR_CARD, highlightthickness=1, highlightbackground="#262338",
                         padx=18, pady=16)
            if titulo:
                tk.Label(f, text=titulo, bg=COR_CARD, fg=COR_TEXTO,
                         font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(0, 8))
            return f

        def _rotulo(self, pai, texto, topo=0):
            tk.Label(pai, text=texto.upper(), bg=COR_CARD, fg="#9D98B3",
                     font=("Segoe UI", 8, "bold")).pack(anchor="w", pady=(topo, 6))

        def _cabecalho(self, pg, titulo, sub):
            f = tk.Frame(pg, bg=COR_FUNDO)
            f.pack(fill="x", pady=(0, 16))
            tk.Label(f, text=titulo, bg=COR_FUNDO, fg="#F4F2FF", font=("Segoe UI", 20, "bold")).pack(anchor="w")
            tk.Label(f, text=sub, bg=COR_FUNDO, fg="#9D98B3", font=("Segoe UI", 10)).pack(anchor="w")
            return f

        def _link(self, pai, texto, cmd, bg=COR_CARD):
            lb = tk.Label(pai, text=texto, bg=bg, fg=COR_DESTAQUE2, cursor="hand2",
                          font=("Segoe UI", 9, "underline"))
            lb.bind("<Button-1>", lambda e: cmd())
            return lb

        # ---------------- estrutura
        def _montar(self):
            self._lateral()
            tk.Frame(self, bg="#1F1D2C", width=1).pack(side="left", fill="y")
            self.area = tk.Frame(self, bg=COR_FUNDO)
            self.area.pack(side="left", fill="both", expand=True)
            self.paginas = {k: tk.Frame(self.area, bg=COR_FUNDO)
                            for k in ("novo", "fila", "biblioteca", "desempenho", "config")}
            self._pagina_novo(self.paginas["novo"])
            self._pagina_fila(self.paginas["fila"])
            self._pagina_biblioteca(self.paginas["biblioteca"])
            self._cabecalho(self.paginas["desempenho"], "Desempenho",
                            "Anote as views dos cortes postados e o LukClips aprende com cada streamer.")
            self.painel_desemp = PainelDesempenho(self.paginas["desempenho"])
            self.painel_desemp.pack(fill="both", expand=True)
            self._pagina_config(self.paginas["config"])

        def _lateral(self):
            lat = tk.Frame(self, bg="#0F0E17", width=236)
            lat.pack(side="left", fill="y")
            lat.pack_propagate(False)
            topo = tk.Frame(lat, bg="#0F0E17")
            topo.pack(fill="x", padx=20, pady=(22, 26))
            try:
                V.gerar_logo(34, PASTA_CACHE_UI / "logo_menu.png")
                self.img_logo = tk.PhotoImage(file=str(PASTA_CACHE_UI / "logo_menu.png"))
                tk.Label(topo, image=self.img_logo, bg="#0F0E17").pack(side="left")
            except Exception:
                pass
            tk.Label(topo, text="LukClips", bg="#0F0E17", fg="#F4F2FF",
                     font=("Segoe UI", 16, "bold")).pack(side="left", padx=(10, 0))
            self.nav = {}
            for chave, texto in (("novo", "Novo corte"), ("fila", "Fila"), ("biblioteca", "Biblioteca"),
                                 ("desempenho", "Desempenho"), ("config", "Configurações")):
                row = tk.Frame(lat, bg="#0F0E17", cursor="hand2")
                row.pack(fill="x", padx=12, pady=2)
                icv = tk.Canvas(row, width=20, height=20, bg="#0F0E17", highlightthickness=0)
                icv.pack(side="left", padx=(12, 12), pady=12)
                lbl = tk.Label(row, text=texto, bg="#0F0E17", fg="#B9B4CC", font=("Segoe UI", 10))
                lbl.pack(side="left")
                if chave == "fila":
                    self.badge_fila = tk.Label(row, text="", bg="#2E2747", fg="#DDD6FE",
                                               font=("Segoe UI", 8, "bold"), padx=7)
                for w in (row, icv, lbl):
                    w.bind("<Button-1>", lambda e, k=chave: self._mostrar(k))
                    w.bind("<Enter>", lambda e, k=chave: self._hover(k, True))
                    w.bind("<Leave>", lambda e, k=chave: self._hover(k, False))
                self.nav[chave] = (row, icv, lbl)
            base = tk.Frame(lat, bg="#0F0E17")
            base.pack(side="bottom", fill="x", padx=20, pady=18)
            self.lbl_ver = tk.Label(base, text=f"v{VERSAO} · procurando atualização...", bg="#17151F",
                                    fg=COR_SUAVE, font=("Segoe UI", 9), padx=10, pady=6, cursor="hand2",
                                    wraplength=180, justify="left")
            self.lbl_ver.pack(anchor="w", fill="x")
            self.lbl_ver.bind("<Button-1>", lambda e: self._procurar_atualizacao(manual=True))
            tk.Label(base, text="Feito por LukDev", bg="#0F0E17", fg="#77728D",
                     font=("Segoe UI", 8)).pack(anchor="w", pady=(10, 0))

        def _hover(self, k, dentro):
            if getattr(self, "pagina_atual", None) == k:
                return
            row, icv, lbl = self.nav[k]
            cor = "#17151F" if dentro else "#0F0E17"
            for w in (row, icv, lbl):
                w.config(bg=cor)

        def _mostrar(self, k):
            for pg in self.paginas.values():
                pg.pack_forget()
            self.paginas[k].pack(fill="both", expand=True, padx=28, pady=22)
            self.pagina_atual = k
            for nome, (row, icv, lbl) in self.nav.items():
                ativo = nome == k
                cor = "#221B3A" if ativo else "#0F0E17"
                for w in (row, icv, lbl):
                    w.config(bg=cor)
                lbl.config(fg="#F4F2FF" if ativo else "#B9B4CC",
                           font=("Segoe UI", 10, "bold" if ativo else "normal"))
                desenhar_icone(icv, nome, COR_DESTAQUE2 if ativo else "#8D88A3")
            if k == "biblioteca":
                self.after(10, self._carregar_biblioteca)
            elif k == "desempenho":
                self.painel_desemp.recarregar()
            elif k == "config":
                self._calcular_cache()

        # ---------------- página: Novo corte
        def _pagina_novo(self, pg):
            cab = self._cabecalho(pg, "Novo corte", "Cole o link, escolha o estilo e gere.")
            self.lbl_dep = tk.Label(cab, text="", bg=COR_FUNDO, fg=COR_SUAVE, font=("Segoe UI", 9))
            self.lbl_dep.place(relx=1.0, rely=0.0, anchor="ne")

            c = self._card(pg)
            c.pack(fill="x", pady=(0, 16))
            lin = tk.Frame(c, bg=COR_CARD)
            lin.pack(fill="x")
            caixa = tk.Frame(lin, bg="#0E0D16", highlightthickness=1, highlightbackground=COR_DESTAQUE,
                             highlightcolor=COR_DESTAQUE)
            caixa.pack(side="left", fill="x", expand=True)
            self.badge = tk.Label(caixa, text="  aguardando link  ", bg=COR_CAMPO, fg=COR_SUAVE,
                                  font=("Segoe UI", 9, "bold"), padx=6, pady=2)
            self.badge.pack(side="left", padx=(8, 4), pady=8)
            self.var_fonte = tk.StringVar()
            self.var_fonte.trace_add("write", lambda *_: self._atualizar_plataforma())
            ent = tk.Entry(caixa, textvariable=self.var_fonte, bg="#0E0D16", fg=COR_TEXTO,
                           insertbackground=COR_TEXTO, relief="flat", font=("Segoe UI", 11),
                           highlightthickness=0)
            ent.pack(side="left", fill="x", expand=True, padx=(4, 8), ipady=6)
            ent.bind("<Button-3>", lambda e: self._colar())
            for txt, cmd in (("Colar", self._colar), ("Arquivo", self._escolher_arquivo),
                             ("+ Fila", self._adicionar_fila)):
                ttk.Button(lin, text=txt, style="Sec.TButton", command=cmd).pack(side="left", padx=(8, 0), ipady=4)
            info = tk.Frame(c, bg=COR_CARD)
            info.pack(fill="x", pady=(10, 0))
            self.lbl_salvar = tk.Label(info, text="", bg=COR_CARD, fg="#8D88A3", font=("Segoe UI", 9))
            self.lbl_salvar.pack(side="left")
            self._link(info, "trocar pasta", self._escolher_pasta).pack(side="left", padx=(10, 0))
            self._link(info, "limpar cache", self._limpar_cache).pack(side="left", padx=(10, 0))
            self.var_saida.trace_add("write", lambda *_: self._texto_salvar())
            self._texto_salvar()

            corpo = tk.Frame(pg, bg=COR_FUNDO)
            corpo.pack(fill="both", expand=True)
            corpo.columnconfigure(0, weight=5)
            corpo.columnconfigure(1, weight=0)
            corpo.columnconfigure(2, weight=4, minsize=300)
            corpo.rowconfigure(0, weight=1)

            # estilo
            est = self._card(corpo)
            est.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
            f1 = tk.Frame(est, bg=COR_CARD)
            f1.pack(fill="x")
            col_q = tk.Frame(f1, bg=COR_CARD)
            col_q.pack(side="left", padx=(0, 22), anchor="n")
            self._rotulo(col_q, "Cortes")
            stp = tk.Frame(col_q, bg="#0E0D16", highlightthickness=1, highlightbackground="#2E2A44")
            stp.pack(anchor="w")
            for txt, d in (("−", -1), ("+", 1)):
                if d == 1:
                    tk.Label(stp, textvariable=self.var_cortes, width=3, bg="#0E0D16", fg=COR_TEXTO,
                             font=("Segoe UI", 11, "bold")).pack(side="left")
                b = tk.Label(stp, text=txt, bg="#0E0D16", fg=COR_DESTAQUE2, font=("Segoe UI", 13, "bold"),
                             padx=12, pady=3, cursor="hand2")
                b.pack(side="left")
                b.bind("<Button-1>", lambda e, d=d: self._mudar_cortes(d))
            col_d = tk.Frame(f1, bg=COR_CARD)
            col_d.pack(side="left", fill="x", expand=True, anchor="n")
            self._rotulo(col_d, "Duração")
            curtos = {"Automática (20–90s)": "Auto", "Curtos (15–40s)": "Curtos", "Médios (30–60s)": "Médios",
                      "Longos (45–120s)": "Longos", "Fixa 30s": "30s", "Fixa 60s": "60s"}
            Segmentos(col_d, self.var_dur, [(k, curtos.get(k, k)) for k in DURACOES]).pack(fill="x")
            self._rotulo(est, "Layout", topo=16)
            EscolhaLayout(est, self.var_layout).pack(anchor="w")
            self._rotulo(est, "Legenda", topo=14)
            EscolhaLegenda(est, self.var_estilo).pack(anchor="w")
            chaves = tk.Frame(est, bg=COR_CARD)
            chaves.pack(fill="x", pady=(10, 0))
            for i, (t, v) in enumerate((("Barra de progresso", self.var_barra), ("Zoom no pico", self.var_zoom),
                                        ("Cortar silêncios", self.var_silencio),
                                        ("Revisar antes de gerar", self.var_revisar))):
                Chave(chaves, t, v).grid(row=i // 2, column=i % 2, sticky="w", padx=(0, 24), pady=4)
            self._link(est, "Mais opções em Configurações →",
                       lambda: self._mostrar("config")).pack(anchor="w", pady=(12, 0))

            # prévia
            prev = self._card(corpo)
            prev.grid(row=0, column=1, sticky="nsew", padx=(0, 16))
            self._rotulo(prev, "Prévia")
            self.previa = Previa(prev, self)
            self.previa.pack(pady=(4, 10))
            tk.Label(prev, text="Muda na hora quando\nvocê troca o estilo", bg=COR_CARD, fg="#8D88A3",
                     font=("Segoe UI", 8), justify="center").pack()

            # gerar + progresso
            dirc = tk.Frame(corpo, bg=COR_FUNDO)
            dirc.grid(row=0, column=2, sticky="nsew")
            self.cv_btn = tk.Canvas(dirc, height=84, bg=COR_FUNDO, highlightthickness=0)
            self.cv_btn.pack(fill="x")
            self.btn = Botao3D(self.cv_btn, "✂  GERAR CORTES", self._iniciar_ou_parar)
            self.cv_btn.bind("<Configure>", lambda e: self.btn.posicionar(0, 2, e.width, 76))
            prog = self._card(dirc)
            prog.pack(fill="both", expand=True, pady=(6, 0))
            self.lbl_status = tk.Label(prog, text="Pronto para começar.", bg=COR_CARD, fg=COR_TEXTO,
                                       font=("Segoe UI", 10, "bold"), justify="left", anchor="w", wraplength=300)
            self.lbl_status.pack(fill="x")
            prog.bind("<Configure>", lambda e: self.lbl_status.config(wraplength=max(160, e.width - 40)))
            self.barra = ttk.Progressbar(prog, mode="determinate", maximum=100)
            self.barra.pack(fill="x", pady=(8, 12))
            self.etapas = Etapas(prog)
            self.etapas.pack(fill="x")
            reg = tk.Frame(prog, bg=COR_CARD)
            reg.pack(fill="both", expand=True, pady=(12, 0))
            self.txt = tk.Text(reg, height=5, bg="#0E0D16", fg="#B8B8C8", relief="flat",
                               font=("Consolas", 9), wrap="word", padx=8, pady=6,
                               insertbackground=COR_TEXTO, highlightthickness=0)
            sb = ttk.Scrollbar(reg, command=self.txt.yview)
            self.txt.configure(yscrollcommand=sb.set, state="disabled")
            self.txt.pack(side="left", fill="both", expand=True)
            sb.pack(side="left", fill="y")
            self.lnk_reg = self._link(prog, "Aumentar o registro", self._alternar_registro)
            self.lnk_reg.pack(anchor="w", pady=(6, 0))

        def _mudar_cortes(self, d):
            try:
                v = int(self.var_cortes.get())
            except ValueError:
                v = 5
            self.var_cortes.set(str(max(1, min(30, v + d))))

        def _texto_salvar(self):
            self.lbl_salvar.config(text=f"Salvar em {self.var_saida.get()}  ·  cache {self._cache_txt}")

        def _alternar_registro(self):
            self._registro_grande = not self._registro_grande
            self.txt.config(height=18 if self._registro_grande else 5)
            self.lnk_reg.config(text="Diminuir o registro" if self._registro_grande else "Aumentar o registro")

        # ---------------- página: Fila
        def _pagina_fila(self, pg):
            self._cabecalho(pg, "Fila", "Vários vídeos em sequência: o GERAR processa todos, um depois do outro.")
            c = self._card(pg)
            c.pack(fill="both", expand=True)
            self.lbl_fila = tk.Label(c, text="", bg=COR_CARD, fg=COR_TEXTO, font=("Segoe UI", 10, "bold"))
            self.lbl_fila.pack(anchor="w")
            self.lst_fila = tk.Listbox(c, height=12, bg="#0E0D16", fg=COR_TEXTO, relief="flat",
                                       highlightthickness=0, font=("Segoe UI", 10), selectmode="extended",
                                       selectbackground=COR_DESTAQUE, activestyle="none")
            self.lst_fila.pack(fill="both", expand=True, pady=10)
            bt = tk.Frame(c, bg=COR_CARD)
            bt.pack(fill="x")
            ttk.Button(bt, text="Remover selecionado", style="Sec.TButton",
                       command=self._remover_fila).pack(side="left")
            ttk.Button(bt, text="Limpar fila", style="Sec.TButton",
                       command=self._limpar_fila).pack(side="left", padx=8)
            ttk.Button(bt, text="✂ Gerar a fila", style="Acao.TButton",
                       command=self._gerar_fila).pack(side="right")
            Chave(c, "Desligar o PC no fim da fila", self.var_desligar).pack(anchor="w", pady=(14, 0))
            tk.Label(c, text="Para adicionar: em Novo corte, cole o link e clique em “+ Fila”. Com vários vídeos, "
                             "a revisão fica desligada para tudo rodar sozinho.", bg=COR_CARD, fg="#8D88A3",
                     font=("Segoe UI", 9), wraplength=720, justify="left").pack(anchor="w", pady=(8, 0))
            self._atualizar_fila_ui()

        def _gerar_fila(self):
            if not self.fila_itens:
                messagebox.showinfo(APP, "A fila está vazia. Adicione vídeos em Novo corte → “+ Fila”.")
                return
            self._mostrar("novo")
            self._iniciar_ou_parar()

        # ---------------- página: Biblioteca
        def _pagina_biblioteca(self, pg):
            self._cabecalho(pg, "Biblioteca", "Todos os cortes gerados, prontos para postar.")
            filtros = tk.Frame(pg, bg=COR_FUNDO)
            filtros.pack(fill="x", pady=(0, 12))
            self.var_filtro = tk.StringVar(value="todos")
            Segmentos(filtros, self.var_filtro, [("todos", "Todos"), ("nao", "Não postados"),
                                                 ("postados", "Postados")]).pack(side="left")
            self.var_filtro.trace_add("write", lambda *_: self._carregar_biblioteca())
            tk.Label(filtros, text="Streamer:", bg=COR_FUNDO, fg=COR_SUAVE).pack(side="left", padx=(18, 6))
            self.var_filtro_st = tk.StringVar(value="Todos")
            self.cb_st = ttk.Combobox(filtros, textvariable=self.var_filtro_st, state="readonly",
                                      width=22, values=["Todos"])
            self.cb_st.pack(side="left")
            self.cb_st.bind("<<ComboboxSelected>>", lambda e: self._carregar_biblioteca())
            self._link(filtros, "Abrir a pasta dos cortes", self._abrir_pasta_saida,
                       bg=COR_FUNDO).pack(side="right")
            self.rol_bib = Rolagem(pg)
            self.rol_bib.pack(fill="both", expand=True)
            self._colunas_bib = 0
            self.rol_bib.cv.bind("<Configure>", self._bib_redimensionar, add="+")

        def _bib_redimensionar(self, e):
            cols = max(1, e.width // 196)
            if cols != self._colunas_bib and getattr(self, "pagina_atual", "") == "biblioteca":
                if getattr(self, "_job_bib", None):
                    self.after_cancel(self._job_bib)
                self._job_bib = self.after(250, self._carregar_biblioteca)

        def _img_vazia(self):
            if not hasattr(self, "_vazia"):
                self._vazia = tk.PhotoImage(width=144, height=256)
            return self._vazia

        def _carregar_biblioteca(self):
            hist = carregar_historico()
            nomes = sorted({h.get("streamer", "") for h in hist if h.get("streamer")})
            self.cb_st.config(values=["Todos"] + nomes)
            filtro, st = self.var_filtro.get(), self.var_filtro_st.get()
            itens = []
            for h in reversed(hist):
                arq = h.get("arquivo", "")
                if not arq or not Path(arq).exists():
                    continue
                post = h.get("postado") or {}
                if (filtro == "nao" and post) or (filtro == "postados" and not post):
                    continue
                if st != "Todos" and h.get("streamer") != st:
                    continue
                itens.append(h)
                if len(itens) >= 80:
                    break
            d = self.rol_bib.dentro
            for w in d.winfo_children():
                w.destroy()
            self.thumb_lbls = {}
            if not itens:
                tk.Label(d, text="Nenhum corte por aqui ainda.\nGere seus primeiros cortes em “Novo corte”.",
                         bg=COR_FUNDO, fg=COR_SUAVE, font=("Segoe UI", 11), justify="center").pack(pady=60)
                return
            larg = self.rol_bib.cv.winfo_width()
            cols = max(1, (larg if larg > 50 else 900) // 196)
            self._colunas_bib = cols
            for i, h in enumerate(itens):
                self._card_corte(d, h).grid(row=i // cols, column=i % cols, padx=(0, 14), pady=(0, 14), sticky="n")
            pend = [(h["id"], h["arquivo"]) for h in itens if h.get("id")]
            threading.Thread(target=self._gerar_miniaturas, args=(pend,), daemon=True).start()

        def _card_corte(self, pai, h):
            f = tk.Frame(pai, bg=COR_CARD, highlightthickness=1, highlightbackground="#262338")
            lbl = tk.Label(f, image=self._img_vazia(), bg="#1B1928", bd=0, cursor="hand2")
            lbl.pack(padx=10, pady=(10, 8))
            lbl.bind("<Button-1>", lambda e, a=h["arquivo"]: abrir(a))
            if h.get("id") in self.thumbs:
                lbl.config(image=self.thumbs[h["id"]])
            self.thumb_lbls[h.get("id")] = lbl
            tk.Label(f, text=h.get("titulo", ""), bg=COR_CARD, fg=COR_TEXTO, font=("Segoe UI", 9, "bold"),
                     wraplength=150, justify="left").pack(anchor="w", padx=10)
            tk.Label(f, text=f"{h.get('streamer', '')} · {h.get('data', '')}", bg=COR_CARD, fg="#9D98B3",
                     font=("Segoe UI", 8)).pack(anchor="w", padx=10)
            post = h.get("postado") or {}
            nomes = {"youtube": "YouTube", "tiktok": "TikTok", "instagram": "Instagram"}
            st = ("Postado: " + ", ".join(nomes.get(k, k) for k in post)) if post else "Não postado"
            tk.Label(f, text=st, bg=COR_CARD, fg="#3DD68C" if post else "#B9B4CC", font=("Segoe UI", 8),
                     wraplength=150, justify="left").pack(anchor="w", padx=10, pady=(2, 8))
            bt = tk.Frame(f, bg=COR_CARD)
            bt.pack(fill="x", padx=10, pady=(0, 10))
            ttk.Button(bt, text="Postar", style="MiniAcao.TButton",
                       command=lambda h=h: self._postar_corte(h)).pack(side="left", fill="x", expand=True)
            ttk.Button(bt, text="▶", width=2, style="Mini.TButton",
                       command=lambda a=h["arquivo"]: abrir(a)).pack(side="left", padx=(5, 0))
            ttk.Button(bt, text="Pasta", style="Mini.TButton",
                       command=lambda a=h["arquivo"]: mostrar_no_explorador(a)).pack(side="left", padx=(5, 0))
            return f

        def _postar_corte(self, h):
            janela = JanelaPostar(self, h)
            janela.bind("<Destroy>", lambda e: self.after(50, self._carregar_biblioteca)
                        if e.widget is janela and self.pagina_atual == "biblioteca" else None)

        def _gerar_miniaturas(self, pend):
            import hashlib
            pasta = PASTA_CACHE_UI / "miniaturas"
            pasta.mkdir(parents=True, exist_ok=True)
            ff = achar_bin("ffmpeg")
            for id_, arq in pend:
                try:
                    p = Path(arq)
                    chave = hashlib.sha1(f"{p}|{p.stat().st_mtime}".encode("utf-8")).hexdigest()[:16]
                    dest = pasta / f"{chave}.png"
                    if not dest.exists() and ff:
                        subprocess.run([ff, "-y", "-loglevel", "error", "-ss", "1.5", "-i", str(p),
                                        "-frames:v", "1", "-vf", "scale=144:256", str(dest)],
                                       timeout=30, **SEM_JANELA)
                    if dest.exists():
                        self.fila.put(("thumb", (id_, str(dest))))
                except Exception:
                    logging.exception("miniatura")

        def _abrir_pasta_saida(self):
            p = Path(self.var_saida.get().strip() or ".").expanduser()
            if p.exists():
                abrir(p)
            else:
                messagebox.showinfo(APP, "A pasta dos cortes ainda não existe. Gere um corte primeiro.")

        # ---------------- página: Configurações
        def _pagina_config(self, pg):
            self._cabecalho(pg, "Configurações", "Ajustes que você faz uma vez.")
            rol = Rolagem(pg)
            rol.pack(fill="both", expand=True)
            d = rol.dentro
            d.columnconfigure(0, weight=1, uniform="g")
            d.columnconfigure(1, weight=1, uniform="g")

            def grupo(lin, col, titulo):
                g = self._card(d, titulo)
                g.grid(row=lin, column=col, sticky="nsew", padx=(0, 16) if col == 0 else (0, 6), pady=(0, 16))
                return g

            def campo(g, rotulo, widget_fn, ajuda=None):
                tk.Label(g, text=rotulo, bg=COR_CARD, fg=COR_SUAVE, font=("Segoe UI", 9)).pack(anchor="w", pady=(8, 3))
                w = widget_fn(g)
                w.pack(fill="x")
                if ajuda:
                    tk.Label(g, text=ajuda, bg=COR_CARD, fg="#8D88A3", font=("Segoe UI", 8), wraplength=420,
                             justify="left").pack(anchor="w", pady=(3, 0))
                return w

            def pasta_saida(g):
                f = tk.Frame(g, bg=COR_CARD)
                ttk.Entry(f, textvariable=self.var_saida).pack(side="left", fill="x", expand=True)
                ttk.Button(f, text="Escolher", style="Sec.TButton",
                           command=self._escolher_pasta).pack(side="left", padx=(8, 0))
                return f

            g = grupo(0, 0, "Geral")
            campo(g, "Pasta onde salvar os cortes", pasta_saida)
            self.lbl_cache = tk.Label(g, text="Cache: calculando...", bg=COR_CARD, fg=COR_SUAVE, font=("Segoe UI", 9))
            self.lbl_cache.pack(anchor="w", pady=(12, 4))
            ttk.Button(g, text="Limpar cache", style="Sec.TButton", command=self._limpar_cache).pack(anchor="w")
            Chave(g, "Desligar o PC no fim da fila", self.var_desligar).pack(anchor="w", pady=(14, 0))

            g = grupo(0, 1, "Transcrição")
            campo(g, "Qualidade (Whisper)", lambda f: ttk.Combobox(f, textvariable=self.var_modelo,
                                                                   state="readonly", values=list(MODELOS)),
                  "Rápido para PC fraco. Preciso e Máximo ficam bem melhores com placa NVIDIA.")
            campo(g, "Idioma da live", lambda f: ttk.Combobox(f, textvariable=self.var_idioma,
                                                              state="readonly", values=list(IDIOMAS)))
            Chave(g, "Usar a placa de vídeo NVIDIA", self.var_gpu).pack(anchor="w", pady=(14, 4))
            self.lbl_gpu = tk.Label(g, text="Verificando a placa de vídeo...", bg=COR_CARD, fg=COR_SUAVE,
                                    font=("Segoe UI", 9), wraplength=420, justify="left")
            self.lbl_gpu.pack(anchor="w")
            self.btn_gpu = ttk.Button(g, text="Como ativar a placa NVIDIA", style="Sec.TButton",
                                      command=self._ajuda_gpu)

            g = grupo(1, 0, "Análise dos momentos")
            Chave(g, "Usar o chat da live", self.var_chat).pack(anchor="w", pady=3)
            Chave(g, "Analisar o movimento da tela", self.var_mov).pack(anchor="w", pady=3)
            Chave(g, "Revisar os momentos antes de gerar", self.var_revisar).pack(anchor="w", pady=3)
            campo(g, "Ignorar os primeiros minutos da live",
                  lambda f: ttk.Spinbox(f, from_=0, to=120, textvariable=self.var_pular, width=6))
            campo(g, "Achar a câmera do streamer", lambda f: ttk.Combobox(
                f, textvariable=self.var_cam, state="readonly", values=["Automática", "Marcar manualmente"]))

            g = grupo(1, 1, "Contas e downloads")
            campo(g, "Login do navegador (YouTube)", lambda f: ttk.Combobox(
                f, textvariable=self.var_nav, state="readonly",
                values=["Nenhum", "Firefox", "Chrome", "Edge", "Brave", "Opera"]),
                "Escolha o navegador em que você está logado no YouTube e feche-o antes de gerar. "
                "O Firefox costuma funcionar melhor.")

            g = grupo(2, 0, "Marca e créditos")
            campo(g, "Marca d'água (pequena)", lambda f: ttk.Entry(f, textvariable=self.var_marca))
            campo(g, "Nome padrão do streamer", lambda f: ttk.Entry(f, textvariable=self.var_streamer),
                  "Vazio = usa o nome do canal do vídeo.")
            campo(g, "Hashtags fixas", lambda f: ttk.Entry(f, textvariable=self.var_hashtags),
                  "Entram em todo corte. Ex.: #meucanaldecortes")

            g = grupo(2, 1, "Sobre")
            self.lbl_ver_cfg = tk.Label(g, text=f"LukClips v{VERSAO}", bg=COR_CARD, fg=COR_TEXTO,
                                        font=("Segoe UI", 11, "bold"))
            self.lbl_ver_cfg.pack(anchor="w")
            for txt, cmd in (("Procurar atualização", lambda: self._procurar_atualizacao(manual=True)),
                             ("Abrir pasta de logs", lambda: abrir(PASTA_LOGS)),
                             ("Copiar informações para suporte", self._copiar_suporte)):
                ttk.Button(g, text=txt, style="Sec.TButton", command=cmd).pack(anchor="w", pady=(8, 0))
            tk.Label(g, text="Feito por LukDev", bg=COR_CARD, fg="#8D88A3",
                     font=("Segoe UI", 8)).pack(anchor="w", pady=(14, 0))

        def _checar_gpu(self):
            def tarefa():
                try:
                    import ctranslate2
                    n = ctranslate2.get_cuda_device_count()
                except Exception:
                    n = 0
                pronta = cuda_disponivel() if n > 0 else False
                self.fila.put(("gpu", (n, pronta)))
            threading.Thread(target=tarefa, daemon=True).start()

        def _ajuda_gpu(self):
            if getattr(sys, "frozen", False):
                messagebox.showinfo(APP, "Para usar a placa NVIDIA no LukClips instalado, instale o "
                                         "“CUDA Toolkit 12” do site da NVIDIA e reinicie o computador.\n\n"
                                         "Sem isso, o LukClips continua funcionando pelo processador.")
                return
            cmd = 'pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"'
            j = tk.Toplevel(self)
            j.title("Ativar a placa NVIDIA")
            j.configure(bg=COR_FUNDO)
            j.resizable(False, False)
            tk.Label(j, text="Ativar a placa NVIDIA", bg=COR_FUNDO, fg=COR_TEXTO,
                     font=("Segoe UI", 12, "bold")).pack(anchor="w", padx=18, pady=(16, 4))
            tk.Label(j, text="Instala as bibliotecas da NVIDIA (uns 700 MB). Depois feche e abra o LukClips.",
                     bg=COR_FUNDO, fg=COR_SUAVE, font=("Segoe UI", 9)).pack(anchor="w", padx=18)
            var = tk.StringVar(value=cmd)
            ent = tk.Entry(j, textvariable=var, state="readonly", readonlybackground="#0E0D16", fg=COR_TEXTO,
                           relief="flat", font=("Consolas", 10), width=58, highlightthickness=1,
                           highlightbackground=COR_BORDA)
            ent.pack(padx=18, pady=12, ipady=6)
            ent.bind("<FocusIn>", lambda e: ent.select_range(0, "end"))
            aviso = tk.Label(j, text="", bg=COR_FUNDO, fg="#3DD68C", font=("Segoe UI", 9))
            aviso.pack(anchor="w", padx=18)

            def copiar():
                self.clipboard_clear()
                self.clipboard_append(cmd)
                self.update()
                aviso.config(text="Copiado! Cole no PowerShell com Ctrl+V e aperte Enter.")

            def instalar():
                py = sys.executable.replace("pythonw.exe", "python.exe")
                linha = f'"{py}" -m pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*" & echo. & ' \
                        'echo Pronto! Feche esta janela e abra o LukClips de novo. & pause'
                subprocess.Popen(["cmd", "/c", linha], creationflags=0x00000010)  # janela nova
                aviso.config(text="Instalando numa janela nova... quando terminar, reabra o LukClips.")

            bar = tk.Frame(j, bg=COR_FUNDO)
            bar.pack(fill="x", padx=18, pady=(8, 16))
            ttk.Button(bar, text="Fechar", style="Sec.TButton", command=j.destroy).pack(side="right")
            ttk.Button(bar, text="Copiar", style="Sec.TButton", command=copiar).pack(side="right", padx=8)
            ttk.Button(bar, text="Instalar agora", style="Acao.TButton", command=instalar).pack(side="right")
            j.transient(self)
            j.focus_force()

        def _copiar_suporte(self):
            info = (f"{APP} v{VERSAO}\nSistema: {sys.platform}\nPython: {sys.version.split()[0]}\n"
                    f"Instalado (.exe): {bool(getattr(sys, 'frozen', False))}\n"
                    f"ffmpeg: {achar_bin('ffmpeg')}\ndeno: {achar_bin('deno')}\n"
                    f"Placa: {self.lbl_gpu.cget('text')}\nLogs: {PASTA_LOGS}")
            self.clipboard_clear()
            self.clipboard_append(info)
            self.update()
            messagebox.showinfo(APP, "Informações copiadas. Cole na conversa com quem vai te ajudar.")

        def _postar_selecionado(self):
            if self.cortes_info:
                JanelaPostar(self, self.cortes_info[0])

        def _ao_redimensionar(self, e=None):
            """(Compatibilidade: a janela nova se ajusta sozinha.)"""
            return

        # ------------------------------------------------------ ações
        def _atualizar_plataforma(self):
            nome, cor = detectar_plataforma(self.var_fonte.get())
            if not nome:
                self.badge.config(text="  aguardando link  ", bg=COR_CAMPO, fg=COR_SUAVE)
            else:
                escuro = nome in ("Kick", "TikTok", "X / Twitter", "Arquivo do PC", "Link")
                self.badge.config(text=f"  ● {nome}  ", bg=cor, fg="#111" if escuro else "white")

        def _colar(self):
            try:
                self.var_fonte.set(self.clipboard_get().strip())
            except tk.TclError:
                pass

        def _escolher_arquivo(self):
            p = filedialog.askopenfilename(title="Escolha o vídeo", filetypes=[
                ("Vídeos", "*.mp4 *.mkv *.mov *.webm *.avi *.flv *.ts"), ("Todos", "*.*")])
            if p:
                self.var_fonte.set(p)

        def _escolher_pasta(self):
            p = filedialog.askdirectory(title="Onde salvar os cortes",
                                        initialdir=self.var_saida.get() or str(Path.home()))
            if p:
                self.var_saida.set(p)
                self._calcular_cache()

        def _abrir_pasta(self):
            if self.ultima_pasta:
                abrir(self.ultima_pasta)

        def _log(self, msg):
            logging.info(msg)
            self.txt.configure(state="normal")
            self.txt.insert("end", msg + "\n")
            linhas = int(self.txt.index("end-1c").split(".")[0])
            if linhas > 1500:
                self.txt.delete("1.0", f"{linhas - 1500}.0")
            self.txt.see("end")
            self.txt.configure(state="disabled")

        def _checar_dependencias(self):
            falta = [n for n in ("ffmpeg", "ffprobe") if not achar_bin(n)]
            sem_deno = not achar_bin("deno")
            if falta:
                self.lbl_dep.config(text="⚠ ffmpeg não encontrado — clique aqui para instalar",
                                    fg="#FFB020", cursor="hand2")
                self.lbl_dep.bind("<Button-1>", lambda e: self._instalar())
            elif sem_deno:
                self.lbl_dep.config(text="⚠ Deno ausente (YouTube pode falhar) — instalar",
                                    fg="#FFB020", cursor="hand2")
                self.lbl_dep.bind("<Button-1>", lambda e: self._instalar())
            else:
                self.lbl_dep.config(text="● tudo pronto", fg="#3DD68C", cursor="")
                self.lbl_dep.unbind("<Button-1>")

        def _instalar(self):
            if os.name != "nt":
                messagebox.showinfo(APP, "Instale ffmpeg e deno pelo gerenciador do seu sistema.")
                return
            if not messagebox.askyesno(APP, "Vou abrir uma janela que instala o ffmpeg e o Deno "
                                            "pelo winget (do próprio Windows).\n\nDepois de terminar, "
                                            "feche e abra o LukClips de novo. Continuar?"):
                return
            cmd = ("winget install -e --id Gyan.FFmpeg --accept-source-agreements "
                   "--accept-package-agreements & winget install -e --id DenoLand.Deno "
                   "--accept-source-agreements --accept-package-agreements & pause")
            subprocess.Popen(["cmd", "/c", cmd], creationflags=0x00000010)  # nova janela

        def _avisar_se_atualizou(self):
            """Mostra uma vez 'atualizado' quando a versão muda."""
            ja_usava = ARQ_CONFIG.exists()
            cfg = carregar_config()
            anterior = cfg.get("ultima_versao") or ("3.0" if ja_usava else None)
            cfg["ultima_versao"] = VERSAO
            salvar_config(cfg)
            if anterior and anterior != VERSAO:
                self._log(f"⬆ Atualizado da v{anterior} para a v{VERSAO}")
                messagebox.showinfo(APP, f"LukClips atualizado para a v{VERSAO}! 🎉")

        def _erro_inesperado(self, tipo, valor, tb):
            detalhes = "".join(traceback.format_exception(tipo, valor, tb))
            logging.error("Erro na interface\n%s", detalhes)
            try:
                JanelaErro(self, f"Erro inesperado: {valor}", detalhes)
            except Exception:
                pass

        def _coletar_cfg(self):
            try:
                cortes = max(1, min(30, int(self.var_cortes.get())))
                pular = max(0, int(self.var_pular.get())) * 60
            except ValueError:
                raise ValueError("Quantidade e minutos precisam ser números.")
            dur = self.var_dur.get() if self.var_dur.get() in DURACOES else CONFIG_PADRAO["duracao"]
            cfg = {
                "saida": self.var_saida.get().strip(), "cortes": cortes, "duracao": dur,
                "usar_chat": bool(self.var_chat.get()), "usar_movimento": bool(self.var_mov.get()),
                "modelo": self.var_modelo.get(), "camera": self.var_cam.get(),
                "navegador": self.var_nav.get(), "streamer": self.var_streamer.get().strip(),
                "pular_inicio": pular, "marca": self.var_marca.get(),
                "layout": self.var_layout.get(), "estilo_legenda": self.var_estilo.get(),
                "zoom_pico": bool(self.var_zoom.get()), "cortar_silencio": bool(self.var_silencio.get()),
                "barra_progresso": bool(self.var_barra.get()), "revisar": bool(self.var_revisar.get()),
                "idioma": self.var_idioma.get() if self.var_idioma.get() in IDIOMAS else "Português",
                "usar_gpu": bool(self.var_gpu.get()), "hashtags_fixas": self.var_hashtags.get().strip(),
            }
            cfg["ultima_versao"] = VERSAO
            salvar_config(cfg)
            run = dict(cfg)
            run["fonte"] = self.var_fonte.get()
            run["modelo"] = MODELOS.get(cfg["modelo"], "small")
            run["dmin"], run["dmax"] = DURACOES[dur]
            run["idioma"] = IDIOMAS.get(cfg["idioma"], "pt")
            return run

        # ---------------- fila
        def _adicionar_fila(self):
            fonte = self.var_fonte.get().strip().strip('"')
            nome, _ = detectar_plataforma(fonte)
            if nome in (None, "Inválido"):
                messagebox.showwarning(APP, "Cole um link válido ou escolha um arquivo antes de adicionar.")
                return
            if any(i["fonte"] == fonte for i in self.fila_itens):
                messagebox.showinfo(APP, "Esse vídeo já está na fila.")
                return
            self.fila_itens.append({"fonte": fonte, "plataforma": nome,
                                    "streamer": self.var_streamer.get().strip()})
            self.var_fonte.set("")
            self._atualizar_fila_ui()
            self._log(f"➕ Adicionado à fila ({len(self.fila_itens)}): {fonte}")

        def _remover_fila(self):
            for i in reversed(self.lst_fila.curselection()):
                del self.fila_itens[i]
            self._atualizar_fila_ui()

        def _limpar_fila(self):
            self.fila_itens = []
            self._atualizar_fila_ui()

        def _atualizar_fila_ui(self):
            self.lst_fila.delete(0, "end")
            for it in self.fila_itens:
                quem = f" — {it['streamer']}" if it["streamer"] else ""
                curto = it["fonte"] if len(it["fonte"]) < 90 else "…" + it["fonte"][-88:]
                self.lst_fila.insert("end", f"[{it['plataforma']}] {curto}{quem}")
            n = len(self.fila_itens)
            self.lbl_fila.config(text=f"{n} vídeo(s) na fila" if n else "A fila está vazia")
            if n:
                self.badge_fila.config(text=str(n))
                self.badge_fila.pack(side="right", padx=(0, 12))
            else:
                self.badge_fila.pack_forget()

        # ---------------- cache
        def _pasta_cache(self):
            return Path(self.var_saida.get().strip() or ".").expanduser() / "_lukclips_cache"

        def _calcular_cache(self):
            def tarefa():
                total = 0
                try:
                    for f in self._pasta_cache().rglob("*"):
                        if f.is_file():
                            total += f.stat().st_size
                except Exception:
                    pass
                self.fila.put(("cache", total))
            threading.Thread(target=tarefa, daemon=True).start()

        def _limpar_cache(self):
            if self.rodando:
                messagebox.showinfo(APP, "Espere terminar de gerar para limpar o cache.")
                return
            r = messagebox.askyesnocancel(
                APP, "Limpar o cache?\n\n"
                     "SIM = apaga só os vídeos e áudios baixados (guarda as transcrições; "
                     "gerar de novo do mesmo vídeo vai baixar outra vez, mas não transcreve).\n\n"
                     "NÃO = apaga tudo (vídeos, transcrições e análises).")
            if r is None:
                return
            base = self._pasta_cache()
            apagados = 0
            try:
                if r:
                    for f in base.rglob("*"):
                        if f.is_file() and (f.name.startswith("vod.") or f.suffix in (".wav", ".part")):
                            f.unlink(missing_ok=True)
                            apagados += 1
                elif base.exists():
                    shutil.rmtree(base, ignore_errors=True)
                    apagados = 1
            except Exception as e:
                logging.exception("limpar cache")
                messagebox.showerror(APP, f"Não consegui apagar tudo: {e}")
            self._log("🧹 Cache limpo." if apagados else "🧹 Nada para limpar.")
            self._calcular_cache()

        # ---------------- gerar
        def _iniciar_ou_parar(self):
            if self.rodando:
                if messagebox.askyesno(APP, "Parar a geração dos cortes?"):
                    self.cancel.set()
                    self.lbl_status.config(text="Parando...")
                return
            if not self.var_saida.get().strip():
                messagebox.showwarning(APP, "Escolha a pasta onde salvar os cortes.")
                return
            try:
                cfg = self._coletar_cfg()
            except ValueError as e:
                messagebox.showwarning(APP, str(e))
                return
            itens = list(self.fila_itens)
            atual = self.var_fonte.get().strip().strip('"')
            nome, _ = detectar_plataforma(atual)
            if atual and nome not in (None, "Inválido") and not any(i["fonte"] == atual for i in itens):
                itens.append({"fonte": atual, "plataforma": nome, "streamer": cfg["streamer"]})
            if not itens:
                messagebox.showwarning(APP, "Cole um link válido, escolha um arquivo ou adicione vídeos na fila.")
                return
            if len(itens) > 1 and cfg["revisar"]:
                cfg["revisar"] = False
                self._log("ℹ Com vários vídeos na fila, a revisão fica desligada para rodar sozinho.")
            self.rodando = True
            self.cancel.clear()
            self.btn.config(text="■  PARAR", style="Parar.TButton")
            self.barra["value"] = 0
            self.arquivos, self.cortes_info = [], []
            self.etapas.resetar()
            self.etapas.ativo = True
            self._mostrar("novo")
            self._log(f"——— {datetime.now():%H:%M:%S} — começando ({len(itens)} vídeo(s)) ———")
            desligar = bool(self.var_desligar.get())
            threading.Thread(target=self._trabalho, args=(cfg, itens, desligar), daemon=True).start()

        def _esperar_gui(self, tipo, dados):
            """Abre uma janela na interface e espera a resposta (chamado pela thread de trabalho)."""
            pronto = threading.Event()
            caixa = {}

            def fim(r):
                caixa["r"] = r
                pronto.set()

            self.fila.put((tipo, (dados, fim)))
            while not pronto.wait(0.2):
                if self.cancel.is_set():
                    return None
            return caixa.get("r")

        def _pedir_camera_thread(self, png, fw, fh):
            return self._esperar_gui("camera", (png, fw, fh))

        def _pedir_revisao_thread(self, P):
            return self._esperar_gui("revisao", P)

        def _trabalho(self, cfg_base, itens, desligar):
            resultados, falhas = [], []
            for k, it in enumerate(itens, 1):
                if self.cancel.is_set():
                    break
                cfg = dict(cfg_base, fonte=it["fonte"], streamer=it.get("streamer") or cfg_base["streamer"])
                pref = f"[{k}/{len(itens)}] " if len(itens) > 1 else ""
                if pref:
                    self.fila.put(("log", f"▶ {pref}{it['fonte']}"))
                fab = Fabrica(cfg,
                              log=lambda m: self.fila.put(("log", m)),
                              progresso=lambda p, s, pref=pref: self.fila.put(("prog", (p, pref + s))),
                              pedir_camera=self._pedir_camera_thread,
                              cancel=self.cancel)
                fab.pedir_revisao = self._pedir_revisao_thread
                try:
                    resultados.append(fab.executar())
                except Cancelado:
                    self.fila.put(("cancelado", None))
                    return
                except Exception as e:
                    detalhes = traceback.format_exc()
                    logging.error("Falha em %s\n%s", it["fonte"], detalhes)
                    msg = traduzir_erro(str(e))
                    if len(itens) == 1:
                        self.fila.put(("erro", (msg, detalhes)))
                        return
                    falhas.append((it["fonte"], msg))
                    self.fila.put(("log", f"❌ {pref}falhou: {msg.splitlines()[0]} — seguindo para o próximo"))
            self.fila.put(("fim", (resultados, falhas, desligar)))

        def _processar_fila(self):
            try:
                while True:
                    tipo, dado = self.fila.get_nowait()
                    if tipo == "log":
                        self._log(dado)
                    elif tipo == "prog":
                        p, s = dado
                        self.barra["value"] = p
                        self.lbl_status.config(text=s)
                        if self.rodando:
                            self.etapas.atualizar(p, s)
                    elif tipo == "thumb":
                        id_, caminho = dado
                        lbl = self.thumb_lbls.get(id_)
                        if lbl is not None and lbl.winfo_exists():
                            img = tk.PhotoImage(file=caminho)
                            self.thumbs[id_] = img
                            lbl.config(image=img)
                    elif tipo == "gpu":
                        n, pronta = dado
                        if pronta:
                            self.lbl_gpu.config(text="✓ Placa NVIDIA pronta: transcrição rápida.", fg="#3DD68C")
                        elif n:
                            self.lbl_gpu.config(text="⚠ Placa NVIDIA encontrada, mas faltam as bibliotecas CUDA. "
                                                     "Por enquanto usa o processador.", fg="#FFB020")
                            self.btn_gpu.pack(anchor="w", pady=(8, 0))
                        else:
                            self.lbl_gpu.config(text="Nenhuma placa NVIDIA encontrada: usa o processador.",
                                                fg=COR_SUAVE)
                    elif tipo == "camera":
                        (png, fw, fh), fim = dado
                        SeletorCamera(self, png, fw, fh, fim)
                    elif tipo == "revisao":
                        P, fim = dado
                        JanelaRevisao(self, P, fim)
                    elif tipo == "cache":
                        self._cache_txt = formatar_tamanho(dado)
                        self.lbl_cache.config(text=f"Cache: {self._cache_txt} (vídeos baixados e transcrições)")
                        self._texto_salvar()
                    elif tipo == "atualizacao":
                        self._oferecer_atualizacao(dado)
                    elif tipo == "fim":
                        self._ao_terminar(*dado)
                    elif tipo == "cancelado":
                        self.etapas.parar()
                        self.lbl_status.config(text="Cancelado.")
                        self._log("⏹ Cancelado.")
                        self._terminar()
                    elif tipo == "erro":
                        msg, detalhes = dado
                        self.etapas.parar(erro=True)
                        self.lbl_status.config(text="Deu erro")
                        self._log("❌ " + msg)
                        self._terminar()
                        JanelaErro(self, msg, detalhes)
            except queue.Empty:
                pass
            self.after(80, self._processar_fila)

        def _ao_terminar(self, resultados, falhas, desligar):
            for pasta, arquivos, rel in resultados:
                self.ultima_pasta = pasta
                self.arquivos += arquivos
                for r in rel:
                    self.cortes_info.append({"arquivo": str(Path(pasta) / r["arquivo"]), "id": r["id"],
                                             "titulo": r["titulo"]})
            total = len(self.cortes_info)
            self._terminar()
            self.etapas.concluir()
            self._calcular_cache()
            self.lbl_status.config(text=f"Pronto! {total} corte(s) gerado(s)" +
                                   (f" de {len(resultados)} vídeos" if len(resultados) > 1 else "") +
                                   (f", {len(falhas)} com erro" if falhas else ""))
            if desligar and os.name == "nt":
                self._log("🌙 Desligando o PC em 2 minutos (para cancelar: shutdown /a no Executar).")
                subprocess.Popen(["shutdown", "/s", "/t", "120", "/c", "LukClips terminou a fila de cortes."],
                                 **SEM_JANELA)
                return
            self.bell()
            if falhas:
                lista = "\n".join(f"• {f[:60]}: {m.splitlines()[0]}" for f, m in falhas[:6])
                messagebox.showwarning(APP, f"Alguns vídeos deram erro:\n\n{lista}\n\nDetalhes no registro.")
            if total:
                self.var_filtro.set("todos")
                self._mostrar("biblioteca")
                if messagebox.askyesno(APP, f"{total} cortes prontos! 🎉\n\nQuer revisar e postar o primeiro agora?"):
                    self._postar_selecionado()

        def _terminar(self):
            self.rodando = False
            self.btn.config(text="✂  GERAR CORTES", style="Grande.TButton")

        # ---------------- atualização
        def _procurar_atualizacao(self, manual=False):
            def tarefa():
                try:
                    info = buscar_atualizacao()
                except Exception as e:
                    logging.warning("atualização: %s", e)
                    info = {"erro": str(e)}
                self.fila.put(("atualizacao", dict(info or {}, manual=manual)))
            threading.Thread(target=tarefa, daemon=True).start()

        def _procurar_periodico(self):
            if not self.rodando:
                self._procurar_atualizacao()
            self.after(2 * 3600 * 1000, self._procurar_periodico)

        def _oferecer_atualizacao(self, info):
            if info.get("erro"):
                self.lbl_ver.config(text=f"v{VERSAO} · sem conexão (clique p/ tentar)", fg="#FFB020")
            elif info.get("versao"):
                self.lbl_ver.config(text=f"⬆ v{info['versao']} disponível — clique", fg="#FFB020")
            elif REPO_GITHUB:
                self.lbl_ver.config(text=f"✓ v{VERSAO} · atualizado", fg="#3DD68C")
            else:
                self.lbl_ver.config(text=f"v{VERSAO}", fg=COR_SUAVE)
            self.lbl_ver_cfg.config(text=f"LukClips v{VERSAO} — " + self.lbl_ver.cget("text").split("·")[-1].strip()
                                    if "·" in self.lbl_ver.cget("text") else f"LukClips v{VERSAO}")
            if info.get("erro"):
                if info.get("manual"):
                    messagebox.showinfo(APP, f"Não consegui procurar atualização agora.\n\n{info['erro']}")
                return
            if not info.get("versao"):
                if info.get("manual"):
                    msg = (f"Você já está na versão mais nova (v{VERSAO})." if REPO_GITHUB else
                           "A atualização automática ainda não foi configurada (REPO_GITHUB vazio).")
                    messagebox.showinfo(APP, msg)
                return
            if not info.get("manual") and info["versao"] in self._ja_avisou:
                return
            self._ja_avisou.add(info["versao"])
            logging.info("Atualização disponível: v%s", info["versao"])
            if not messagebox.askyesno(APP, f"Tem uma versão nova do LukClips: v{info['versao']} "
                                            f"(você tem a v{VERSAO}).\n\n{info.get('notas', '')[:400]}\n\n"
                                            "Atualizar agora?"):
                return
            if not getattr(sys, "frozen", False) or not info.get("instalador"):
                import webbrowser
                webbrowser.open(info["pagina"])
                return
            self.lbl_status.config(text="Baixando a atualização...")

            def baixar():
                try:
                    destino = Path(os.environ.get("TEMP", PASTA_CONFIG)) / f"LukClips_Setup_{info['versao']}.exe"
                    with abrir_url(info["instalador"], timeout=60) as r, open(destino, "wb") as f:
                        tot = int(r.headers.get("Content-Length") or 0)
                        feito = 0
                        while True:
                            b = r.read(1 << 20)
                            if not b:
                                break
                            f.write(b)
                            feito += len(b)
                            if tot:
                                self.fila.put(("prog", (feito / tot * 100,
                                                        f"Baixando a atualização... {feito / tot * 100:.0f}%")))
                    subprocess.Popen([str(destino), "/SILENT", "/SUPPRESSMSGBOXES", "/CLOSEAPPLICATIONS",
                                      "/RESTARTAPPLICATIONS"])
                    self.after(500, self.destroy)
                except Exception as e:
                    logging.exception("baixar atualização")
                    self.fila.put(("erro", (f"Não consegui baixar a atualização: {e}", traceback.format_exc())))
            threading.Thread(target=baixar, daemon=True).start()

        def _fechar(self):
            if self.rodando and not messagebox.askyesno(APP, "Ainda está gerando cortes. Fechar mesmo assim?"):
                return
            self.cancel.set()
            try:
                self._coletar_cfg()
            except Exception:
                pass
            self.destroy()

    if os.name == "nt":
        try:  # deixa a janela nítida em telas com zoom
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("LukDev.LukClips")
        except Exception:
            pass
    App().mainloop()


# ============================================================================
# Modo linha de comando (útil pra testar):  lukclips.py --cli FONTE PASTA [--cam x,y,w,h]
# ============================================================================
def modo_cli(argv):
    configurar_log()
    import argparse
    ap = argparse.ArgumentParser(prog="lukclips --cli")
    ap.add_argument("fonte")
    ap.add_argument("saida")
    ap.add_argument("--cortes", type=int, default=5)
    ap.add_argument("--dmin", type=int, default=20, help="duração mínima (s)")
    ap.add_argument("--dmax", type=int, default=90, help="duração máxima (s)")
    ap.add_argument("--sem-chat", action="store_true")
    ap.add_argument("--sem-movimento", action="store_true")
    ap.add_argument("--layout", default=LAYOUT_PADRAO)
    ap.add_argument("--estilo", default=ESTILO_PADRAO)
    ap.add_argument("--zoom", action="store_true", help="zoom no pico")
    ap.add_argument("--silencio", action="store_true", help="cortar silêncios")
    ap.add_argument("--barra", action="store_true", help="barra de progresso")
    ap.add_argument("--modelo", default="small")
    ap.add_argument("--camera", default="Automática")
    ap.add_argument("--cam", help="x,y,w,h da câmera")
    ap.add_argument("--navegador", default="Nenhum")
    ap.add_argument("--streamer", default="")
    ap.add_argument("--pular", type=int, default=300)
    a = ap.parse_args(argv)
    cfg = {"fonte": a.fonte, "saida": a.saida, "cortes": a.cortes, "dmin": a.dmin, "dmax": a.dmax,
           "usar_chat": not a.sem_chat, "usar_movimento": not a.sem_movimento,
           "layout": a.layout, "estilo_legenda": a.estilo, "zoom_pico": a.zoom,
           "cortar_silencio": a.silencio, "barra_progresso": a.barra, "revisar": False,
           "modelo": a.modelo, "camera": a.camera, "navegador": a.navegador,
           "streamer": a.streamer, "pular_inicio": a.pular, "marca": "Feito por LukDev"}
    ultimo = [-1]

    def prog(p, s):
        if int(p) != ultimo[0]:
            ultimo[0] = int(p)
            print(f"[{p:5.1f}%] {s}")

    cam = [int(v) for v in a.cam.split(",")] if a.cam else None
    if cam:
        cfg["cam_fixa"] = cam
    fab = Fabrica(cfg, log=print, progresso=prog)
    fab.executar()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        modo_cli(sys.argv[2:])
    else:
        iniciar_interface()
