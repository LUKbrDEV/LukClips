"""
LukClips - visual (fundo com brilhos + grade 3D, botão 3D)
Feito por LukDev

As imagens são desenhadas com numpy e salvas em PNG (o Tk abre PNG com
transparência), então não precisa de nenhuma biblioteca extra.
"""
import struct
import zlib
from pathlib import Path

import numpy as np


def salvar_png(arr, caminho):
    """Salva um array (h, w, 3 ou 4) uint8 como PNG."""
    arr = np.ascontiguousarray(arr, dtype=np.uint8)
    h, w, c = arr.shape
    tipo = 6 if c == 4 else 2
    linhas = np.concatenate([np.zeros((h, 1), np.uint8), arr.reshape(h, w * c)], axis=1)

    def bloco(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n"
           + bloco(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, tipo, 0, 0, 0))
           + bloco(b"IDAT", zlib.compress(linhas.tobytes(), 3))
           + bloco(b"IEND", b""))
    Path(caminho).write_bytes(png)
    return caminho


def _hex(c):
    c = c.lstrip("#")
    return np.array([int(c[i:i + 2], 16) for i in (0, 2, 4)], np.float32)


# ============================================================================
# Fundo
# ============================================================================
def gerar_fundo(w, h, caminho, base="#0B0B12"):
    w, h = max(2, int(w)), max(2, int(h))
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    img = np.empty((h, w, 3), np.float32)
    img[:] = _hex(base)

    def brilho(cx, cy, r, cor, forca):
        d = ((x - cx) ** 2 + (y - cy) ** 2) / (r * r)
        img[:] += np.exp(-d)[..., None] * _hex(cor) * forca

    brilho(w * 0.10, h * 0.02, w * 0.38, "#7C3AED", 0.55)   # roxo, canto de cima
    brilho(w * 0.95, h * 1.00, w * 0.42, "#EC4899", 0.30)   # rosa, canto de baixo
    brilho(w * 0.70, h * 0.10, w * 0.22, "#3B82F6", 0.18)   # azul, de leve

    # chão em grade com perspectiva (efeito 3D), na parte de baixo
    horizonte = h * 0.58
    prof = np.clip((y - horizonte) / (h - horizonte), 0, 1)
    abaixo = y > horizonte
    k = prof + 0.06
    # linhas horizontais: ficam mais juntas perto do horizonte
    z = 1.0 / k
    dz_dy = 1.0 / (k * k * (h - horizonte))
    dist_z = np.abs(z * 1.6 - np.round(z * 1.6)) / 1.6
    lin_h = np.exp(-((dist_z / (dz_dy + 1e-6)) ** 2) / 0.6)
    # linhas que fogem para o centro
    esp = 70.0
    xs = (x - w / 2) / k / esp
    dist_x = np.abs(xs - np.round(xs)) * esp * k
    lin_v = np.exp(-(dist_x ** 2) / 0.7)
    grade = np.clip(lin_h + lin_v, 0, 1) * abaixo * (prof ** 1.3) * 0.55
    img += grade[..., None] * _hex("#8B5CF6")
    # brilho no horizonte
    img += (np.exp(-((y - horizonte) / (h * 0.025)) ** 2) * 0.35)[..., None] * _hex("#A78BFA") * \
        np.exp(-((x - w / 2) / (w * 0.45)) ** 2)[..., None]

    # vinheta
    vin = 1 - 0.35 * (((x / w - 0.5) * 2) ** 2 + ((y / h - 0.5) * 2) ** 2) / 2
    img *= vin[..., None]
    return salvar_png(np.clip(img, 0, 255), caminho)


# ============================================================================
# Botão 3D
# ============================================================================
def _ret_arred(x, y, x0, y0, x1, y1, r):
    """Máscara de retângulo arredondado com borda suave (0..1)."""
    dx = np.maximum(np.maximum(x0 + r - x, 0), x - (x1 - r))
    dy = np.maximum(np.maximum(y0 + r - y, 0), y - (y1 - r))
    d = np.sqrt(dx * dx + dy * dy) - r
    return np.clip(0.5 - d, 0, 1)


def _clarear(c, f):
    return np.clip(c + (255 - c) * f, 0, 255)


def gerar_botao(w, h, caminho, cor_cima, cor_baixo, cor_lado, apertado=False, hover=False,
                raio=16, prof=7):
    """Botão com lateral (profundidade), degradê, brilho em cima e sombra.
    Apertado = a face desce e a lateral some, como se afundasse."""
    w, h = max(10, int(w)), max(10, int(h))
    y, x = np.mgrid[0:h, 0:w].astype(np.float32) + 0.5
    cima, baixo, lado = _hex(cor_cima), _hex(cor_baixo), _hex(cor_lado)
    if hover:
        cima, baixo, lado = _clarear(cima, 0.12), _clarear(baixo, 0.10), _clarear(lado, 0.08)

    rgba = np.zeros((h, w, 4), np.float32)

    def por_cima(cor, alfa):
        a0 = rgba[..., 3:4] / 255
        a1 = alfa[..., None]
        out_a = a1 + a0 * (1 - a1)
        cor = cor if cor.ndim == 3 else np.broadcast_to(cor, (h, w, 3))
        rgba[..., :3] = (cor * a1 + rgba[..., :3] * a0 * (1 - a1)) / np.maximum(out_a, 1e-6)
        rgba[..., 3:4] = out_a * 255

    m = 4  # margem para a sombra
    topo_face = m + (prof - 2 if apertado else 0)
    base_face = h - m - prof + (prof - 2 if apertado else 0)
    # sombra suave
    sombra = _ret_arred(x, y, m + 2, m + prof, w - m - 2, h - 1, raio + 4)
    sombra = np.clip(sombra * 0.45, 0, 1)
    por_cima(np.array([0, 0, 0], np.float32), sombra * (0.4 if apertado else 1))
    # lateral (profundidade 3D)
    por_cima(lado, _ret_arred(x, y, m, m + prof, w - m, h - m, raio))
    # face com degradê
    t = np.clip((y - topo_face) / max(1, base_face - topo_face), 0, 1)[..., None]
    face = cima * (1 - t) + baixo * t
    por_cima(face, _ret_arred(x, y, m, topo_face, w - m, base_face, raio))
    # brilho na metade de cima
    meio = (topo_face + base_face) / 2
    brilho = _ret_arred(x, y, m + 3, topo_face + 2, w - m - 3, meio, raio - 3)
    grad = np.clip(1 - (y - topo_face) / max(1, meio - topo_face), 0, 1)
    por_cima(np.array([255, 255, 255], np.float32), brilho * grad * 0.16)
    # filete claro na borda de cima
    filete = _ret_arred(x, y, m + 1, topo_face, w - m - 1, topo_face + 3, raio) * \
        (1 - _ret_arred(x, y, m + 1, topo_face + 1.5, w - m - 1, topo_face + 6, raio))
    por_cima(np.array([255, 255, 255], np.float32), filete * 0.35)
    salvar_png(np.clip(rgba, 0, 255), caminho)
    return caminho, (topo_face + base_face) / 2


# ============================================================================
# Ícone com profundidade (para o cabeçalho)
# ============================================================================
def gerar_logo(tam, caminho):
    """Ladrilho roxo com 'play cortado' e lateral 3D."""
    s = int(tam)
    prof = max(3, s // 12)
    h = s + prof
    y, x = np.mgrid[0:h, 0:s].astype(np.float32) + 0.5
    rgba = np.zeros((h, s, 4), np.float32)

    def por_cima(cor, alfa):
        a0 = rgba[..., 3:4] / 255
        a1 = alfa[..., None]
        out_a = a1 + a0 * (1 - a1)
        cor = cor if cor.ndim == 3 else np.broadcast_to(cor, (h, s, 3))
        rgba[..., :3] = (cor * a1 + rgba[..., :3] * a0 * (1 - a1)) / np.maximum(out_a, 1e-6)
        rgba[..., 3:4] = out_a * 255

    r = s * 0.24
    por_cima(_hex("#3B1475"), _ret_arred(x, y, 0, prof, s, h, r))           # lateral
    t = np.clip(y / s, 0, 1)[..., None]
    face = _hex("#8B5CF6") * (1 - t) + _hex("#EC4899") * t
    por_cima(face, _ret_arred(x, y, 0, 0, s, s, r))                           # face
    # play
    cx, cy = s * 0.53, s * 0.5
    a = (x - (cx - s * 0.17))
    dentro = (a >= 0) & (np.abs(y - cy) <= (s * 0.24) * (1 - a / (s * 0.40)))
    play = dentro.astype(np.float32)
    # corte diagonal
    linha = np.abs((y - cy) + (x - cx) * 0.75 - s * 0.03) < s * 0.035
    play[linha] = 0
    por_cima(np.array([255, 255, 255], np.float32), play)
    brilho = _ret_arred(x, y, s * 0.06, s * 0.05, s * 0.94, s * 0.45, r * 0.8)
    por_cima(np.array([255, 255, 255], np.float32), brilho * 0.12)
    return salvar_png(np.clip(rgba, 0, 255), caminho)
