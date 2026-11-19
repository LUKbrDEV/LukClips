"""
LukClips - motor de escolha dos melhores momentos
Feito por LukDev

Junta vários sinais, segundo a segundo, e escolhe os cortes:
  heatmap    gráfico de "mais repetidos" do YouTube (o que o público reviu)
  chat       explosões de mensagens, risadas e "?" no chat da live
  palavras   reações universais + pacote do jogo + bordões aprendidos do streamer
  audio      gritos, risadas e impactos (tiro/explosão), comparados com o normal do streamer
  movimento  quanto a tela muda (ação); telas paradas/escuras perdem pontos
  fala       fala acelerada
e aplica penalidades (sub/doação/publi, música, silêncio, alucinação do Whisper).

Depois acha os picos e monta cortes de tamanho flexível:
começa um pouco antes da situação e termina quando a reação acaba.
"""
import json
import math
import re
import subprocess
import time
import unicodedata
import urllib.request
import wave
from pathlib import Path

import numpy as np

# ============================================================================
# Palavras
# ============================================================================
UNIVERSAL = {
    # espanto
    "caraca": 3, "caralho": 3, "meu deus": 4, "mano do céu": 4, "nossa": 2,
    "não acredito": 4, "nao acredito": 4, "que isso": 3, "olha isso": 3, "mds": 3,
    "socorro": 3, "puta": 2, "porra": 2, "vish": 2, "rapaz": 2, "jesus": 2,
    "impossível": 3, "inacreditável": 3, "como assim": 3, "o que": 1, "eita": 2,
    "absurdo": 3, "bizarro": 3, "insano": 3, "surreal": 3, "cara": 1, "velho": 1,
    "para tudo": 3, "não não não": 4, "nao nao nao": 4, "não pode": 3, "sério": 1,
    # risada
    "kkk": 3, "haha": 3, "hahaha": 4, "rindo": 2, "que engraçado": 3, "morri de rir": 4,
    # vitória / derrota
    "ganhei": 3, "perdi": 3, "conseguimos": 3, "consegui": 2, "acabou": 2,
    "morri": 3, "morreu": 3, "de novo não": 3, "não acredito nisso": 4,
    # inglês (streamers e gringos)
    "oh my god": 4, "what the": 3, "no way": 4, "let's go": 3, "lets go": 3, "wtf": 3,
}

# cada pacote: palavras que identificam o jogo, gatilhos extras, títulos
PACOTES = {
    "gta": {
        "detecta": ["gta", "grand theft", "los santos", "vice city", "lucia", "jason"],
        "gatilhos": {"polícia": 2, "policia": 2, "estrela": 2, "explodiu": 3, "capotou": 3,
                     "bati": 2, "fugir": 2, "corre": 2, "tiro": 2, "assalto": 3, "roubar": 2,
                     "helicóptero": 2, "tanque": 3, "atropel": 2, "procurado": 2},
    },
    "terror": {
        "detecta": ["terror", "horror", "resident evil", "outlast", "fnaf", "five nights",
                    "phasmophobia", "silent hill", "dead by daylight", "amnesia"],
        "gatilhos": {"susto": 4, "assustei": 4, "fecha a porta": 3, "corre": 3, "monstro": 3,
                     "atrás de mim": 4, "o que é isso": 3, "não quero": 3, "medo": 2, "ai": 1},
    },
    "fps": {
        "detecta": ["valorant", "counter-strike", "cs2", "csgo", "warzone", "call of duty",
                    "fortnite", "apex", "free fire", "rainbow six", "battlefield", "pubg"],
        "gatilhos": {"matei": 3, "headshot": 3, "clutch": 4, "ace": 4, "um tiro": 2, "kill": 2,
                     "último": 2, "ultimo": 2, "derrubei": 3, "vitória": 3, "top 1": 4, "booyah": 4},
    },
    "minecraft": {
        "detecta": ["minecraft", "creeper", "nether", "enderman"],
        "gatilhos": {"creeper": 3, "diamante": 3, "lava": 3, "nether": 2, "explodiu": 3,
                     "perdi tudo": 4, "caí": 2, "cai": 1},
    },
    "futebol": {
        "detecta": ["fifa", "ea fc", "fc 25", "fc 26", "futebol", "efootball", "pes"],
        "gatilhos": {"gol": 3, "golaço": 4, "golaco": 4, "pênalti": 3, "penalti": 3,
                     "defendeu": 3, "na trave": 3, "virada": 3, "juiz": 2},
    },
    "corrida": {
        "detecta": ["forza", "gran turismo", "need for speed", "f1 2", "assetto"],
        "gatilhos": {"bati": 3, "capotou": 3, "ultrapassei": 3, "primeiro lugar": 3,
                     "rodou": 2, "voando": 2},
    },
}

TITULOS = [
    # (categoria, palavras, títulos)  {S} = nome do streamer
    ("riso", ("kkk", "haha", "rindo", "engraçado"),
     ["NÃO AGUENTEI ESSA KKKK 😂", "{S} CHOROU DE RIR 😂", "ISSO FOI MUITO ENGRAÇADO 😂"]),
    ("susto", ("susto", "assustei", "socorro", "medo", "monstro", "atrás de mim"),
     ["O SUSTO QUE {S} LEVOU 😱", "ELE QUASE CAIU DA CADEIRA 😱"]),
    ("policia", ("polícia", "policia", "estrela", "fugir", "procurado"),
     ["FUGINDO DA POLÍCIA 🚔", "A PERSEGUIÇÃO MAIS LOUCA 🚔"]),
    ("caos", ("explodiu", "tanque", "helicóptero", "creeper", "lava"),
     ["O CAOS É OUTRO NÍVEL 💥", "EXPLODIU TUDO 💣"]),
    ("falha", ("morri", "morreu", "capotou", "bati", "perdi", "de novo não", "caí"),
     ["ISSO NÃO PODIA TER ACONTECIDO 💀", "O PIOR AZAR DE {S} 💀"]),
    ("vitoria", ("ganhei", "clutch", "ace", "matei", "golaço", "gol", "consegui", "vitória", "top 1"),
     ["QUE JOGADA FOI ESSA 🔥", "{S} CONSEGUIU NO ÚLTIMO SEGUNDO 😮"]),
    ("assalto", ("assalto", "roubar"),
     ["O ASSALTO MAIS INSANO 🔥", "DEU TUDO ERRADO NO ASSALTO 😬"]),
    ("grafico", ("gráfico", "grafico", "lindo", "realista"),
     ["OS GRÁFICOS SÃO SURREAIS 🤯", "O REALISMO DESSE JOGO ASSUSTA 😳"]),
    ("reacao", ("meu deus", "não acredito", "nao acredito", "caraca", "que isso", "mds", "no way"),
     ["{S} NÃO ACREDITOU NISSO 😳", "A REAÇÃO DE {S} FOI ÉPICA 😱"]),
]
GENERICOS = ["MOMENTO INSANO DA LIVE 🔥", "ISSO FOI SURREAL 🤯", "OLHA O QUE ACONTECEU 👀",
             "ESSE MOMENTO FOI DEMAIS 🔥", "NINGUÉM ESPERAVA ISSO 😳", "{S} NÃO ESPERAVA ESSA 😳"]

# trechos que derrubam a nota
RE_ANUNCIO = re.compile(
    r"\b(subs?|inscri\w*|inscrev\w*|doa(ção|cao|ções|coes|ou)|doou|pix|bits|prime|"
    r"patroc\w*|publi\w*|cupom|c[oó]digo|link na (descrição|bio)|segue l[aá]|loja|"
    r"obrigad[oa] pel[oa]s?|valeu pel[oa]s?|tmj|membro\w*|super ?chat)\b", re.I)
RE_LIXO_WHISPER = re.compile(
    r"(♪|\[m[uú]sica\]|amara\.org|obrigad[oa] por assistir|legendas? (pela|por)|"
    r"inscreva-se no canal|^\W*$)", re.I)

# chat
RE_RISO_CHAT = re.compile(r"(k{3,}|(ha){2,}|(rs){2,}|kekw|lul|omegalul|lmao|lol|😂|🤣|💀|icant)", re.I)
RE_CHOQUE_CHAT = re.compile(r"(^\?+$|\?{2,}|wtf|omg|mds|pog|monkas|😱|😳|🤯|que isso|caraca|nossa|"
                            r"meu deus|no way|holy|jesus)", re.I)

STOPWORDS = set("""
a o e é de do da dos das em no na nos nas um uma uns umas que não nao pra para por com sem
se mas mais menos muito muita isso isto esse essa este esta aquele aquela ele ela eles elas
eu você voce vocês voces nós nos me te lhe meu minha seu sua tá ta tô to vai vou foi era
ser ter tem tinha já ja aí ai lá la aqui ali então entao agora ainda só so tipo né ne cara
mano velho então assim aqui coisa gente porque pq como quando onde qual quem tudo nada
the and you that this what with have just like for are was but not
""".split())

PESOS_PADRAO = {"heatmap": 3.0, "chat": 2.5, "palavras": 1.5, "audio": 1.2,
                "movimento": 0.5, "fala": 0.4}

NOMES_SINAIS = {"heatmap": "mapa de calor do YouTube", "chat": "chat da live",
                "palavras": "palavras", "audio": "áudio", "movimento": "movimento da tela",
                "fala": "ritmo da fala"}


# ============================================================================
# Utilidades numéricas
# ============================================================================
def suavizar(x, j):
    x = np.asarray(x, float)
    if j <= 1 or len(x) == 0:
        return x
    j = min(j, len(x))
    pad = np.pad(x, (j // 2, j - 1 - j // 2), mode="edge")
    return np.convolve(pad, np.ones(j) / j, mode="valid")


def maximo_local(x, j):
    """Máximo em janela deslizante (preenche buracos)."""
    x = np.asarray(x, float)
    if j <= 1 or len(x) == 0:
        return x
    pad = np.pad(x, (j // 2, j - 1 - j // 2), mode="edge")
    return np.lib.stride_tricks.sliding_window_view(pad, j).max(axis=1)


def z_rolante(x, janela=300):
    """Compara cada segundo com o 'normal' dos minutos em volta.
    Assim um streamer que grita sempre não ganha nota alta o tempo todo."""
    x = np.asarray(x, float)
    n = len(x)
    if n == 0:
        return x
    j = int(min(janela, max(5, n)))
    pad = np.pad(x, (j // 2, j - 1 - j // 2), mode="edge")
    k = np.ones(j) / j
    m = np.convolve(pad, k, mode="valid")
    v = np.convolve(pad ** 2, k, mode="valid") - m ** 2
    s = np.sqrt(np.maximum(v, 1e-9))
    s = np.maximum(s, np.std(x) * 0.5 + 1e-6)
    return (x - m) / s


def z_global(x):
    x = np.asarray(x, float)
    if len(x) == 0:
        return x
    return (x - np.median(x)) / (np.std(x) + 1e-9)


def ajustar_tamanho(x, n):
    x = np.asarray(x, float)
    if len(x) >= n:
        return x[:n]
    return np.pad(x, (0, n - len(x)), mode="edge" if len(x) else "constant")


def deslocar(x, k):
    """Puxa o sinal k segundos para trás (o chat reage alguns segundos depois)."""
    if k <= 0:
        return x
    return np.concatenate([x[k:], np.full(k, x[-1] if len(x) else 0)])


def slug(txt):
    t = unicodedata.normalize("NFKD", txt or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-") or "desconhecido"


def conta_gatilhos(texto, gatilhos):
    t = texto.lower()
    return sum(t.count(g) * p for g, p in gatilhos.items())


# ============================================================================
# 1. Mapa de calor do YouTube ("mais repetidos")
# ============================================================================
def sinal_heatmap(heatmap, n):
    if not heatmap:
        return None
    v = np.zeros(n)
    for h in heatmap:
        a, b = int(h.get("start_time", 0)), int(math.ceil(h.get("end_time", 0)))
        v[max(0, a):min(n, b)] = h.get("value", 0)
    if v.max() <= 0:
        return None
    # o começo do vídeo sempre é muito visto; tira esse efeito
    v[: min(n, 60)] *= 0.3
    return np.clip(z_global(suavizar(v, 5)), -2, 4)


# ============================================================================
# 2. Chat da live
# ============================================================================
TWITCH_GQL = "https://gql.twitch.tv/gql"
TWITCH_CLIENT = "kimne78kx3ncx6brgo4mv6wki5h1ko"
TWITCH_HASH = "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"


def chat_twitch(video_id, log, prog, cancelado, limite_seg=600):
    vid = str(video_id).lstrip("v")
    msgs, cursor, t0, pag = [], None, time.time(), 0
    while True:
        if cancelado():
            raise InterruptedError()
        var = {"videoID": vid, "cursor": cursor} if cursor else {"videoID": vid, "contentOffsetSeconds": 0}
        corpo = json.dumps([{"operationName": "VideoCommentsByOffsetOrCursor", "variables": var,
                             "extensions": {"persistedQuery": {"version": 1, "sha256Hash": TWITCH_HASH}}}])
        req = urllib.request.Request(TWITCH_GQL, data=corpo.encode(), headers={
            "Client-Id": TWITCH_CLIENT, "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            resp = json.loads(r.read().decode("utf-8"))
        com = (((resp[0] or {}).get("data") or {}).get("video") or {}).get("comments")
        if not com:
            erro = resp[0].get("errors") if isinstance(resp, list) else resp
            raise RuntimeError(f"Twitch não devolveu o chat ({str(erro)[:120]})")
        for e in com.get("edges", []):
            node = e.get("node") or {}
            frag = ((node.get("message") or {}).get("fragments")) or []
            msgs.append((float(node.get("contentOffsetSeconds", 0)),
                         "".join(f.get("text", "") for f in frag)))
        pag += 1
        if pag % 10 == 0 and msgs:
            prog(f"Baixando o chat da Twitch... {len(msgs)} mensagens ({msgs[-1][0] / 60:.0f} min)")
        if not com.get("pageInfo", {}).get("hasNextPage") or not com.get("edges"):
            break
        cursor = com["edges"][-1].get("cursor")
        if not cursor or time.time() - t0 > limite_seg:
            log("⚠ Chat da Twitch muito grande, usando só uma parte.")
            break
    return msgs


def ler_chat_youtube(arq):
    msgs = []
    with open(arq, encoding="utf-8") as f:
        for linha in f:
            try:
                d = json.loads(linha)
            except ValueError:
                continue
            a = d.get("replayChatItemAction") or {}
            t = int(a.get("videoOffsetTimeMsec", 0) or 0) / 1000
            for act in a.get("actions", []):
                item = (act.get("addChatItemAction") or {}).get("item") or {}
                r = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
                if not r:
                    continue
                txt = ""
                for run in (r.get("message") or {}).get("runs", []):
                    if "text" in run:
                        txt += run["text"]
                    else:
                        sc = (run.get("emoji") or {}).get("shortcuts") or [""]
                        txt += sc[0] or (run.get("emoji") or {}).get("emojiId", "")
                msgs.append((t, txt))
    return msgs


def sinal_chat(msgs, n, atraso=5):
    if not msgs or len(msgs) < 50:
        return None
    qtd = np.zeros(n)
    riso = np.zeros(n)
    choque = np.zeros(n)
    for t, txt in msgs:
        k = int(t)
        if 0 <= k < n:
            qtd[k] += 1
            if RE_RISO_CHAT.search(txt):
                riso[k] += 1
            if RE_CHOQUE_CHAT.search(txt):
                choque[k] += 1
    s = (np.clip(z_rolante(suavizar(qtd, 5)), -2, 5)
         + 0.7 * np.clip(z_rolante(suavizar(riso, 5)), -1, 5)
         + 0.5 * np.clip(z_rolante(suavizar(choque, 5)), -1, 5))
    return np.clip(deslocar(s, atraso) / 1.6, -2, 4)


# ============================================================================
# 3. Palavras (universal + pacote do jogo + bordões do streamer)
# ============================================================================
def detectar_pacotes(titulo, transcript):
    texto = (titulo or "").lower()
    falado = " ".join(s["text"] for s in transcript[:4000]).lower()
    achados = []
    for nome, p in PACOTES.items():
        no_titulo = any(d in texto for d in p["detecta"])
        na_fala = sum(falado.count(d) for d in p["detecta"])
        if no_titulo or na_fala >= 5:
            achados.append(nome)
    return achados


def montar_gatilhos(pacotes, perfil):
    g = dict(UNIVERSAL)
    for p in pacotes:
        for k, v in PACOTES[p]["gatilhos"].items():
            g[k] = max(g.get(k, 0), v)
    for k, v in (perfil.get("palavras") or {}).items():
        g[k] = max(g.get(k, 0), round(float(v), 2))
    return g


def sinal_palavras(transcript, n, gatilhos):
    s = np.zeros(n)
    for seg in transcript:
        i0, i1 = int(seg["start"]), min(n - 1, int(seg["end"]))
        if i1 < i0:
            continue
        t = seg["text"]
        pts = conta_gatilhos(t, gatilhos) + t.count("!") + t.count("?") * 0.5
        if pts:
            s[i0:i1 + 1] += pts / max(1, i1 - i0 + 1) * 2
    s = suavizar(s, 3)
    p95 = np.percentile(s[s > 0], 95) if (s > 0).any() else 1
    return np.clip(s / (p95 + 1e-9) * 2, 0, 4)


def sinal_fala(transcript, n):
    fala = np.zeros(n)
    for seg in transcript:
        for w in seg.get("words", []):
            k = int(w["start"])
            if 0 <= k < n:
                fala[k] += 1
    return np.clip(z_rolante(suavizar(fala, 3)), -1, 3), fala


# ============================================================================
# 4. Áudio: grito, risada, impacto
# ============================================================================
def sinal_audio(wav, n, cancelado=lambda: False):
    with wave.open(str(wav)) as w:
        sr = w.getframerate()
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    quadro = sr // 20  # 50 ms
    F = len(a) // quadro
    if F < 40:
        return None
    rms = np.empty(F, np.float32)
    cen = np.empty(F, np.float32)
    freqs = np.fft.rfftfreq(quadro, 1 / sr).astype(np.float32)
    jan = np.hanning(quadro).astype(np.float32)
    passo = 1200  # 60 s por vez (pouca memória)
    for i0 in range(0, F, passo):
        if cancelado():
            raise InterruptedError()
        m = min(passo, F - i0)
        blk = a[i0 * quadro:(i0 + m) * quadro].reshape(m, quadro).astype(np.float32) / 32768
        rms[i0:i0 + m] = np.sqrt((blk ** 2).mean(axis=1) + 1e-10)
        esp = np.abs(np.fft.rfft(blk * jan, axis=1))
        cen[i0:i0 + m] = (esp * freqs).sum(axis=1) / (esp.sum(axis=1) + 1e-9)
    db = 20 * np.log10(rms + 1e-5)
    m = F // 20
    db_s = db[: m * 20].reshape(m, 20)
    alto = db_s.max(axis=1)                                       # pico de volume no segundo
    peso = rms[: m * 20].reshape(m, 20)
    cen_s = (cen[: m * 20].reshape(m, 20) * peso).sum(axis=1) / (peso.sum(axis=1) + 1e-9)
    impacto = np.clip(np.diff(db, prepend=db[0]), 0, None)[: m * 20].reshape(m, 20).max(axis=1)

    # risada = volume subindo e descendo 3 a 7 vezes por segundo
    pad = np.pad(db, (10, 30), mode="edge")
    janelas = np.lib.stride_tricks.sliding_window_view(pad, 40)[::20][:m]
    janelas = janelas - janelas.mean(axis=1, keepdims=True)
    esp = np.abs(np.fft.rfft(janelas * np.hanning(40), axis=1)) ** 2
    f = np.fft.rfftfreq(40, 1 / 20)
    banda = esp[:, (f >= 3) & (f <= 7)].sum(axis=1)
    razao = banda / (esp[:, 1:].sum(axis=1) + 1e-9)

    z_alto = z_rolante(alto)
    z_cen = z_rolante(cen_s)
    grito = np.clip(z_alto - 1, 0, 3) * np.clip(z_cen, 0, 2)
    riso = np.clip((razao - 0.35) / 0.15, 0, 3) * (z_alto > 0.3)
    imp = np.clip(z_rolante(impacto) - 1.5, 0, 3)
    s = 0.6 * np.clip(z_alto, -1, 4) + 0.8 * grito + 0.8 * riso + 0.4 * imp
    return ajustar_tamanho(np.clip(suavizar(s, 3), -2, 4), n)


# ============================================================================
# 5. Imagem: movimento, telas paradas / escuras
# ============================================================================
def sinal_movimento(ffmpeg, video, n, cancelado=lambda: False, extra_popen=None):
    W, H = 64, 36
    cmd = [ffmpeg, "-v", "error", "-skip_frame", "nokey", "-i", str(video),
           "-vf", f"fps=1,scale={W}:{H},format=gray", "-f", "rawvideo", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         **(extra_popen or {}))
    quadros = []
    tam = W * H
    try:
        while True:
            if cancelado():
                p.kill()
                raise InterruptedError()
            b = p.stdout.read(tam * 60)
            if not b:
                break
            k = len(b) // tam
            quadros.append(np.frombuffer(b[: k * tam], np.uint8).reshape(k, H, W))
    finally:
        p.wait()
    if not quadros:
        return None, None
    q = np.concatenate(quadros).astype(np.float32)
    dif = np.abs(np.diff(q, axis=0)).mean(axis=(1, 2))
    dif = np.concatenate([[0], dif])
    dif = maximo_local(dif, 3)          # quadros repetidos (keyframes) viram 0: preenche
    brilho = q.mean(axis=(1, 2))
    mov = np.clip(z_rolante(suavizar(dif, 3)), -1, 3)
    parado = suavizar(dif, 20) < 0.8
    escuro = suavizar(brilho, 5) < 14
    penal = np.where(escuro, -1.5, 0) + np.where(parado & ~escuro, -0.8, 0)
    return ajustar_tamanho(mov, n), ajustar_tamanho(penal, n)


# ============================================================================
# 6. Penalidades
# ============================================================================
def penalidades(transcript, n, fala, pular_fim=180):
    pen = np.zeros(n)
    textos = [s["text"].strip().lower() for s in transcript]
    for i, seg in enumerate(transcript):
        i0, i1 = int(seg["start"]), min(n, int(math.ceil(seg["end"])) + 1)
        t = seg["text"]
        if RE_ANUNCIO.search(t):
            pen[i0:i1] -= 2.5
        if RE_LIXO_WHISPER.search(t):
            pen[i0:i1] -= 2.0
        if seg.get("nsp", 0) > 0.6:
            pen[i0:i1] -= 1.0
        if i >= 2 and textos[i] and textos[i] == textos[i - 1] == textos[i - 2]:
            pen[i0:i1] -= 2.0                    # Whisper repetindo (música/alucinação)
    pen[fala == 0] -= 0.4
    pen[max(0, n - pular_fim):] -= 1.5          # final da live (despedida)
    # espalha um pouco pra não cortar no meio da propaganda
    return np.minimum(pen, suavizar(pen, 7))


# ============================================================================
# 7. Perfil do streamer (bordões aprendidos e pesos ajustados)
# ============================================================================
def carregar_perfil(pasta_cfg, streamer):
    arq = Path(pasta_cfg) / "perfis" / f"{slug(streamer)}.json"
    try:
        return json.loads(arq.read_text(encoding="utf-8"))
    except Exception:
        return {"nome": streamer, "palavras": {}, "pesos": {}, "videos": 0}


def salvar_perfil(pasta_cfg, streamer, perfil):
    arq = Path(pasta_cfg) / "perfis" / f"{slug(streamer)}.json"
    try:
        arq.parent.mkdir(parents=True, exist_ok=True)
        arq.write_text(json.dumps(perfil, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def aprender_bordoes(transcript, referencia, n, perfil):
    """Vê quais palavras o streamer fala mais nos momentos em que o público reagiu
    (chat/mapa de calor) do que no resto da live, e guarda como bordões dele."""
    if referencia is None:
        return []
    corte = np.percentile(referencia, 90)
    quente = referencia >= corte
    cont_top, cont_all = {}, {}
    tot_top = tot_all = 0
    for seg in transcript:
        k = min(n - 1, int(seg["start"]))
        palavras = [p for p in re.findall(r"[\wáéíóúâêôãõç]+", seg["text"].lower())
                    if len(p) >= 4 and p not in STOPWORDS and not p.isdigit()]
        for p in palavras:
            cont_all[p] = cont_all.get(p, 0) + 1
            tot_all += 1
            if quente[max(0, k - 3):k + 8].any():
                cont_top[p] = cont_top.get(p, 0) + 1
                tot_top += 1
    if tot_top < 30:
        return []
    novos = {}
    for p, c in cont_top.items():
        if c < 3 or p in UNIVERSAL:
            continue
        lift = (c / tot_top) / (cont_all[p] / tot_all)
        if lift >= 2.0:
            novos[p] = min(3.0, 1.0 + math.log2(lift))
    melhores = dict(sorted(novos.items(), key=lambda x: -x[1])[:15])
    antigos = perfil.get("palavras") or {}
    for p in set(antigos) | set(melhores):
        perfil.setdefault("palavras", {})[p] = round(0.7 * antigos.get(p, 0) + 0.3 * melhores.get(p, 0)
                                                     if p in antigos else melhores[p], 2)
    # guarda só os 40 mais fortes
    perfil["palavras"] = dict(sorted(perfil["palavras"].items(), key=lambda x: -x[1])[:40])
    return list(melhores)


def recalcular_pesos(historico, streamer):
    """Com as views anotadas, vê quais sinais acertaram para esse streamer."""
    itens = [h for h in historico if slug(h.get("streamer", "")) == slug(streamer)
             and isinstance(h.get("views"), (int, float)) and h["views"] >= 0 and h.get("sinais")]
    if len(itens) < 6:
        return None, len(itens)
    y = np.log1p([h["views"] for h in itens])
    pesos = {}
    for s in PESOS_PADRAO:
        x = np.array([h["sinais"].get(s, 0) for h in itens], float)
        if np.std(x) < 1e-6 or np.std(y) < 1e-6:
            pesos[s] = 1.0
            continue
        corr = float(np.corrcoef(x, y)[0, 1])
        pesos[s] = round(float(np.clip(1 + corr, 0.5, 1.8)), 2)
    return pesos, len(itens)


# ============================================================================
# 8. Junta tudo e escolhe os cortes
# ============================================================================
def combinar(sinais, pesos_perfil, n):
    total = np.zeros(n)
    usados = []
    for nome, arr in sinais.items():
        if arr is None:
            continue
        p = PESOS_PADRAO[nome] * float((pesos_perfil or {}).get(nome, 1.0))
        total += p * ajustar_tamanho(arr, n)
        usados.append(nome)
    return total, usados


def tema_do_texto(txt):
    t = txt.lower()
    melhor, pts = None, 0
    for cat, chaves, _ in TITULOS:
        c = sum(t.count(k) for k in chaves)
        if c > pts:
            melhor, pts = cat, c
    return melhor


def gerar_titulo(txt, usados, streamer):
    nome = (streamer or "ELE").upper()
    t = txt.lower()
    ordem = sorted(TITULOS, key=lambda r: -sum(t.count(c) for c in r[1]))
    for _, chaves, opcoes in ordem:
        if sum(t.count(c) for c in chaves) == 0:
            break
        for op in opcoes:
            tit = op.replace("{S}", nome)
            if tit not in usados:
                usados.add(tit)
                return tit
    for op in GENERICOS:
        tit = op.replace("{S}", nome)
        if tit not in usados:
            usados.add(tit)
            return tit
    return f"MOMENTO #{len(usados) + 1} 🔥"


def escolher_cortes(score, transcript, n, qtd, dmin, dmax, pular_inicio=0, contexto=6,
                    minimo_relativo=0.15):
    """Acha os picos e monta cada corte em volta do pico:
    começa pouco antes da situação e termina quando a reação acaba."""
    sm = suavizar(score, 5)
    if n < dmin + 5:
        return [(0.0, float(n), float(sm.max() if n else 0))]
    base = np.percentile(sm, 60)
    inicios = sorted(s["start"] for s in transcript)
    fins = sorted(s["end"] for s in transcript)
    ini_arr, fim_arr = np.array(inicios or [0.0]), np.array(fins or [float(n)])

    def ajusta_ini(t):
        c = ini_arr[(ini_arr >= t - 4) & (ini_arr <= t + 2)]
        return float(c[np.argmin(np.abs(c - t))]) - 0.3 if len(c) else t

    def ajusta_fim(t):
        c = fim_arr[(fim_arr >= t - 2) & (fim_arr <= t + 5)]
        return float(c[np.argmin(np.abs(c - t))]) + 0.4 if len(c) else t

    candidatos = []
    ocupado = np.zeros(n, bool)
    for p in np.argsort(-sm):
        if len(candidatos) >= qtd * 4 or sm[p] <= base:
            break
        if p < pular_inicio or ocupado[p]:
            continue
        lo, hi = max(0, p - 7), min(n, p + 8)
        if sm[p] < sm[lo:hi].max():
            continue                                     # não é o topo do pico
        # fim: quando a nota volta ao normal por 3 s
        e, calma = p + 3, 0
        while e < min(n - 1, p + dmax):
            calma = calma + 1 if sm[e] < base else 0
            if calma >= 3:
                break
            e += 1
        # começo: volta enquanto a situação está "esquentando" + um pouco de contexto
        s = p - 1
        while s > max(0, p - dmax) and sm[s] > base:
            s -= 1
        s -= contexto
        # respeita mínimo e máximo
        dur = e - s
        if dur < dmin:
            falta = dmin - dur
            s -= falta * 0.6
            e += falta * 0.4
        if e - s > dmax:
            if p - s > dmax * 0.7:
                s = p - dmax * 0.7
            e = s + dmax
        s, e = max(0.0, s), min(float(n), e)
        s, e = ajusta_ini(s), ajusta_fim(e)
        s, e = max(0.0, s), min(float(n), e)
        if e - s < dmin * 0.8:
            continue
        a, b = int(s), int(math.ceil(e))
        if ocupado[max(0, a - 15):min(n, b + 15)].any():
            continue
        ocupado[a:b] = True
        valor = float(sm[p] + 0.3 * score[a:b].mean())
        candidatos.append([s, e, valor, p])

    # descarta os fracos demais (melhor entregar 4 bons do que 5 com um ruim)
    if candidatos:
        topo = max(c[2] for c in candidatos)
        candidatos = [c for c in candidatos if c[2] >= topo * minimo_relativo]

    # variedade: evita cortes colados e do mesmo assunto
    escolhidos, temas = [], {}
    restantes = list(candidatos)
    while restantes and len(escolhidos) < qtd:
        def nota(c):
            v = c[2]
            for s2, e2, *_ in escolhidos:
                if abs(c[0] - s2) < 300:
                    v *= 0.85
            txt = " ".join(x["text"] for x in transcript if x["end"] > c[0] and x["start"] < c[1])
            tema = tema_do_texto(txt)
            if tema and temas.get(tema, 0):
                v *= 0.8 ** temas[tema]
            return v
        melhor = max(restantes, key=nota)
        restantes.remove(melhor)
        txt = " ".join(x["text"] for x in transcript if x["end"] > melhor[0] and x["start"] < melhor[1])
        tema = tema_do_texto(txt)
        if tema:
            temas[tema] = temas.get(tema, 0) + 1
        escolhidos.append(melhor)
    return [(float(c[0]), float(c[1]), float(c[2]), float(c[3])) for c in escolhidos]


def resumo_sinais(sinais, a, b):
    """Média de cada sinal dentro do corte (vai pro histórico, para aprender depois)."""
    out = {}
    for nome, arr in sinais.items():
        if arr is not None and b > a:
            out[nome] = round(float(np.mean(arr[int(a):int(math.ceil(b))])), 3)
    return out


# ============================================================================
# 9. Edição: corrigir legenda, cortar silêncios
# ============================================================================
def aplicar_edicoes(transcript, edicoes):
    """edicoes = {índice do trecho: texto corrigido}. Mantém o tempo de cada palavra
    quando a quantidade de palavras é a mesma; senão distribui no tempo do trecho."""
    if not edicoes:
        return transcript
    novo = list(transcript)
    for k, texto in edicoes.items():
        i = int(k)
        if not (0 <= i < len(novo)):
            continue
        seg = dict(novo[i])
        palavras = texto.split()
        antigas = seg.get("words") or []
        if len(palavras) == len(antigas):
            seg["words"] = [dict(w, word=p) for w, p in zip(antigas, palavras)]
        elif palavras:
            ini, fim = seg["start"], seg["end"]
            passo = (fim - ini) / len(palavras)
            seg["words"] = [{"start": ini + j * passo, "end": ini + (j + 1) * passo - 0.02, "word": p}
                            for j, p in enumerate(palavras)]
        else:
            seg["words"] = []
        seg["text"] = texto.strip()
        novo[i] = seg
    return novo


def intervalos_com_fala(transcript, ini, fim, sinal_audio=None, pausa_min=0.7, folga=0.25):
    """Trechos (relativos ao início do corte) que ficam quando se cortam os silêncios.
    Mantém também segundos com áudio forte (risada/grito sem palavra)."""
    dur = fim - ini
    marcas = []
    for s in transcript:
        if s["end"] < ini or s["start"] > fim:
            continue
        for w in s.get("words", []):
            if w["end"] > ini and w["start"] < fim:
                marcas.append((max(0.0, w["start"] - ini - folga), min(dur, w["end"] - ini + folga)))
    if sinal_audio is not None:
        for k in range(int(ini), int(math.ceil(fim))):
            if 0 <= k < len(sinal_audio) and sinal_audio[k] > 0.8:
                marcas.append((max(0.0, k - ini - 0.2), min(dur, k + 1 - ini + 0.2)))
    if not marcas:
        return None
    marcas.sort()
    juntos = [list(marcas[0])]
    for a, b in marcas[1:]:
        if a - juntos[-1][1] < pausa_min:
            juntos[-1][1] = max(juntos[-1][1], b)
        else:
            juntos.append([a, b])
    if juntos[0][0] < 1.0:
        juntos[0][0] = 0.0
    if dur - juntos[-1][1] < 1.0:
        juntos[-1][1] = dur
    total = sum(b - a for a, b in juntos)
    if total > dur - 0.6 or total < 3:
        return None                      # quase nada pra cortar (ou sobraria pouco)
    return [(round(a, 2), round(b, 2)) for a, b in juntos]


def remapeador(manter):
    """Converte um tempo do corte original para o tempo depois de tirar os silêncios."""
    if not manter:
        return lambda t: max(0.0, t)

    def f(t):
        acc = 0.0
        for a, b in manter:
            if t < a:
                return acc
            if t <= b:
                return acc + (t - a)
            acc += b - a
        return acc
    return f
