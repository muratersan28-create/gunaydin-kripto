# -*- coding: utf-8 -*-
"""Ons altın için çoklu zaman dilimli (M5–H4) teknik analiz grafiği
(destek/direnç + olası giriş/SL/TP). Ağ dışında LLM KULLANMAZ — strateji
tamamen kural bazlıdır:

  Trend    : SMA20 / SMA50 karşılaştırması (seçilen zaman diliminde)
  Destek/Direnç : son mumlardaki swing high/low'lar (fraktal yöntem)
  Giriş/SL/TP   : trend yönüne göre en yakın destek/dirence dayalı basit senaryo

Veri kaynağı: Twelve Data'nın XAU/USD SPOT geçmiş verisi (TWELVE_DATA_API_KEY
gerekir — ücretsiz üyelik). Anahtar yoksa/istek başarısız olursa Yahoo
Finance'in ücretsiz (anahtarsız) GC=F (COMEX altın vadeli işlem) uç noktasına
düşer (H4 hariç — Yahoo'da bu periyot yok); vadeli işlem spot'tan biraz
farklı olduğundan ("basis") güncel spot fiyatla kaydırılarak düzeltilir.

Yatırım tavsiyesi değildir — yalnızca teknik gösterim amaçlıdır.
"""
import io
import os

import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

TD_URL = "https://api.twelvedata.com/time_series"
YF_URL = "https://query1.finance.yahoo.com/v8/finance/chart/GC=F"
HTTP_TIMEOUT = 20

# Desteklenen zaman dilimleri: Twelve Data aralığı, kaç mum çekilecek
# (SMA50 + swing tespiti için pay bırakılır), grafikte gösterilecek mum
# sayısı, ve (varsa) Yahoo yedek kaynağının karşılığı.
TIMEFRAMES = {
    "M5":  {"td": "5min",  "fetch": 300, "gosterim": 80, "yahoo": "5m"},
    "M15": {"td": "15min", "fetch": 300, "gosterim": 80, "yahoo": "15m"},
    "M30": {"td": "30min", "fetch": 300, "gosterim": 80, "yahoo": "30m"},
    "H1":  {"td": "1h",    "fetch": 250, "gosterim": 72, "yahoo": "60m"},
    "H4":  {"td": "4h",    "fetch": 200, "gosterim": 60, "yahoo": None},
}
VARSAYILAN_ZD = "M5"

# kart.py ile aynı koyu tema
BG = "#0d121b"
INK = "#e9eef4"
MUTED = "#96a2b2"
ACCENT = "#f1c40f"
UP = "#34d399"
DOWN = "#f5787c"
LINE = "#23303c"
GIRIS_RENK = "#5aa9e6"


def _ohlc_cek_twelvedata(zaman_dilimi):
    """Twelve Data'dan XAU/USD SPOT mum verisi çeker (TWELVE_DATA_API_KEY
    ortam değişkeni gerekir). En yeni mum en sonda olacak şekilde döner."""
    ayar = TIMEFRAMES[zaman_dilimi]
    api_key = os.environ.get("TWELVE_DATA_API_KEY")
    if not api_key:
        raise RuntimeError("TWELVE_DATA_API_KEY tanımlı değil")
    r = requests.get(
        TD_URL,
        params={"symbol": "XAU/USD", "interval": ayar["td"], "outputsize": ayar["fetch"],
                "apikey": api_key},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    veri = r.json()
    degerler = veri.get("values")
    if not degerler:
        raise RuntimeError(f"Twelve Data hatası: {veri.get('message', veri)}")
    mumlar = [
        {"t": v["datetime"], "o": float(v["open"]), "h": float(v["high"]),
         "l": float(v["low"]), "c": float(v["close"])}
        for v in degerler
    ]
    mumlar.reverse()  # Twelve Data en yeniden en eskiye döner; biz eskiden yeniye istiyoruz
    return mumlar


def _ohlc_cek_yahoo(zaman_dilimi):
    """Yahoo Finance'ten (GC=F, vadeli işlem) mum verisi çeker — yedek kaynak."""
    ayar = TIMEFRAMES[zaman_dilimi]
    yahoo_iv = ayar["yahoo"]
    if not yahoo_iv:
        raise RuntimeError(f"Yedek kaynak (Yahoo) {zaman_dilimi} zaman dilimini desteklemiyor")
    r = requests.get(
        YF_URL, params={"interval": yahoo_iv, "range": "60d"},
        headers={"User-Agent": "Mozilla/5.0"}, timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    sonuc = r.json()["chart"]["result"][0]
    ts = sonuc["timestamp"]
    q = sonuc["indicators"]["quote"][0]
    mumlar = []
    for i in range(len(ts)):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c):
            continue
        mumlar.append({"t": ts[i], "o": o, "h": h, "l": l, "c": c})
    return mumlar[-ayar["fetch"]:]


def _sma(degerler, pencere):
    if len(degerler) < pencere:
        return None
    return sum(degerler[-pencere:]) / pencere


def _atr(mumlar, pencere=14):
    """Ortalama Gerçek Aralık (ATR) — SL tamponunu zaman dilimine göre
    ölçeklemek için. Sabit yüzdelik tampon, M5 gibi küçük aralıklı
    zaman dilimlerinde SL'i gereksiz uzağa taşıyıp risk/ödül oranını bozuyordu."""
    if len(mumlar) < pencere + 1:
        return None
    gercek_araliklar = []
    for i in range(1, len(mumlar)):
        h, l, onceki_kapanis = mumlar[i]["h"], mumlar[i]["l"], mumlar[i - 1]["c"]
        gercek_araliklar.append(max(h - l, abs(h - onceki_kapanis), abs(l - onceki_kapanis)))
    return sum(gercek_araliklar[-pencere:]) / pencere


def _swing_noktalari(mumlar, kenar=3):
    """Fraktal yöntemiyle swing high/low indekslerini bulur: bir mum,
    kendinden önceki/sonraki `kenar` kadar mumdan daha yüksek/düşükse swing'dir."""
    yuksekler, dusukler = [], []
    for i in range(kenar, len(mumlar) - kenar):
        pencere = mumlar[i - kenar:i + kenar + 1]
        if mumlar[i]["h"] == max(m["h"] for m in pencere):
            yuksekler.append(i)
        if mumlar[i]["l"] == min(m["l"] for m in pencere):
            dusukler.append(i)
    return yuksekler, dusukler


def _spot_fiyat_cek():
    """gold-api.com'dan anlık ons altın SPOT fiyatını çeker (vadeli işlem değil)."""
    r = requests.get("https://api.gold-api.com/price/XAU", timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json().get("price")


def analiz_uret(zaman_dilimi=VARSAYILAN_ZD, spot_fiyat=None):
    """OHLC çeker, destek/direnç + giriş/SL/TP hesaplar.
    (mumlar, analiz) döndürür; analiz LLM'e ASLA gitmez, sadece kural bazlı hesap.

    `zaman_dilimi`: "M5", "M15", "M30", "H1" veya "H4".

    Önce Twelve Data'nın gerçek XAU/USD SPOT verisini dener. Anahtar yoksa ya
    da istek başarısız olursa Yahoo Finance'in GC=F (vadeli işlem) verisine
    düşer; bu durumda vadeli işlem ile rapordaki gerçek SPOT fiyat arasındaki
    normal farkı ("basis") gidermek için tüm mumlar, güncel spot fiyatla
    güncel vadeli işlem fiyatı arasındaki sabit farkla kaydırılır.
    `spot_fiyat` verilmezse gold-api.com'dan ayrıca çekilir.
    """
    zaman_dilimi = zaman_dilimi.upper()
    if zaman_dilimi not in TIMEFRAMES:
        raise ValueError(f"Bilinmeyen zaman dilimi: {zaman_dilimi} (geçerli: {', '.join(TIMEFRAMES)})")

    try:
        mumlar = _ohlc_cek_twelvedata(zaman_dilimi)
        kaynak = "twelvedata"
    except Exception as td_hata:                      # noqa: BLE001
        print(f"[uyarı] Twelve Data başarısız, Yahoo/futures'a düşülüyor: {td_hata}")
        mumlar = _ohlc_cek_yahoo(zaman_dilimi)
        kaynak = "yahoo"

    if len(mumlar) < 60:
        raise RuntimeError(f"Yeterli mum verisi yok ({len(mumlar)} mum, kaynak: {kaynak})")

    if kaynak == "yahoo":
        if spot_fiyat is None:
            spot_fiyat = _spot_fiyat_cek()
        if spot_fiyat:
            fark = spot_fiyat - mumlar[-1]["c"]
            for m in mumlar:
                m["o"] += fark
                m["h"] += fark
                m["l"] += fark
                m["c"] += fark

    kapanislar = [m["c"] for m in mumlar]
    guncel = kapanislar[-1]
    sma20 = _sma(kapanislar, 20)
    sma50 = _sma(kapanislar, 50)
    trend = "YUKARI" if (sma20 is not None and sma50 is not None and sma20 > sma50) else "AŞAĞI"

    yuksek_idx, dusuk_idx = _swing_noktalari(mumlar)
    pencere_baslangic = max(0, len(mumlar) - 60)  # son 60 mumdaki swing'lere bak

    direnc_adaylari = sorted({
        mumlar[i]["h"] for i in yuksek_idx
        if i >= pencere_baslangic and mumlar[i]["h"] > guncel
    })
    destek_adaylari = sorted({
        mumlar[i]["l"] for i in dusuk_idx
        if i >= pencere_baslangic and mumlar[i]["l"] < guncel
    }, reverse=True)

    son60 = mumlar[pencere_baslangic:]
    direnc = direnc_adaylari[0] if direnc_adaylari else max(m["h"] for m in son60)
    destek = destek_adaylari[0] if destek_adaylari else min(m["l"] for m in son60)

    # SL tamponu: sabit yüzde yerine ATR bazlı — zaman dilimi ne olursa olsun
    # (M5 ya da günlük) mumların gerçek volatilitesine göre ölçeklenir.
    atr = _atr(mumlar, 14) or (guncel * 0.001)
    sl_tamponu = atr * 0.5

    if trend == "YUKARI":
        yon = "LONG (olası)"
        # desteğe yakın bir "pullback" bölgesi — destek ile güncel fiyat arasında %15
        giris = destek + (guncel - destek) * 0.15
        sl = destek - sl_tamponu
        tp = direnc
    else:
        yon = "SHORT (olası)"
        giris = direnc - (direnc - guncel) * 0.15
        sl = direnc + sl_tamponu
        tp = destek

    risk = abs(giris - sl)
    odul = abs(tp - giris)
    rr = (odul / risk) if risk > 0 else None

    analiz = {
        "guncel": guncel, "trend": trend, "yon": yon,
        "destek": destek, "direnc": direnc,
        "giris": giris, "sl": sl, "tp": tp, "rr": rr,
        "sma20": sma20, "sma50": sma50, "zaman_dilimi": zaman_dilimi,
    }
    return mumlar, analiz


def grafik_olustur(mumlar, analiz, tarih, son_n=None):
    """PNG bytes döndürür: mum grafiği + destek/direnç/giriş/SL/TP çizgileri."""
    zaman_dilimi = analiz.get("zaman_dilimi", VARSAYILAN_ZD)
    if son_n is None:
        son_n = TIMEFRAMES[zaman_dilimi]["gosterim"]
    veri = mumlar[-son_n:]

    fig, ax = plt.subplots(figsize=(10.8, 10.8), dpi=100)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)

    genislik = 0.6
    for i, m in enumerate(veri):
        renk = UP if m["c"] >= m["o"] else DOWN
        ax.plot([i, i], [m["l"], m["h"]], color=renk, linewidth=1, zorder=2)
        alt = min(m["o"], m["c"])
        yukseklik = abs(m["c"] - m["o"]) or (m["h"] * 0.0006)
        ax.add_patch(Rectangle((i - genislik / 2, alt), genislik, yukseklik,
                                facecolor=renk, edgecolor=renk, zorder=3))

    x0, x1 = -1, len(veri)
    tum_y = [m["l"] for m in veri] + [m["h"] for m in veri] + [
        analiz["destek"], analiz["direnc"], analiz["sl"], analiz["tp"]]
    esik = (max(tum_y) - min(tum_y)) * 0.012  # bu kadar yakın değerler tek çizgide birleşir

    # DİRENÇ/TP (LONG'da eşit) ya da DESTEK/TP (SHORT'ta eşit) gibi çakışan
    # seviyeleri tek etikette birleştir, üst üste binmesinler.
    cizgiler = []  # [[deger, renk, [etiketler]]]
    def ekle(y, renk, etiket):
        for kayit in cizgiler:
            if abs(kayit[0] - y) < esik:
                kayit[2].append(etiket)
                return
        cizgiler.append([y, renk, [etiket]])

    ekle(analiz["direnc"], ACCENT, "DİRENÇ")
    ekle(analiz["destek"], ACCENT, "DESTEK")
    ekle(analiz["giris"], GIRIS_RENK, "GİRİŞ")
    ekle(analiz["sl"], DOWN, "SL")
    ekle(analiz["tp"], UP, "TP")

    for y, renk, etiketler in cizgiler:
        ax.hlines(y, x0, x1, colors=renk, linestyles="--", linewidth=1.4, zorder=1)
        ax.text(x1 - 0.4, y, f" {'/'.join(etiketler)} {y:,.1f}", color=renk, fontsize=11,
                va="center", ha="left", fontweight="bold")

    ax.set_xlim(x0, x1 + 7)
    ax.set_ylim(min(tum_y) * 0.995, max(tum_y) * 1.005)
    ax.set_xticks([])
    for taraf in ("top", "right", "left", "bottom"):
        ax.spines[taraf].set_color(LINE)
    ax.tick_params(colors=MUTED, labelsize=10)
    ax.grid(color=LINE, linewidth=0.5, alpha=0.5, axis="y")

    ax.set_title(f"ONS ALTIN (XAU) — {zaman_dilimi} TEKNİK GÖRÜNÜM · {tarih}",
                color=ACCENT, fontsize=15, fontweight="bold", loc="left", pad=14)

    rr_txt = f"  ·  R/R ~1:{analiz['rr']:.1f}" if analiz["rr"] else ""
    alt_metin = (
        f"Trend: {analiz['trend']}  ·  Olası yön: {analiz['yon']}{rr_txt}\n"
        f"Giriş {analiz['giris']:,.1f}  ·  SL {analiz['sl']:,.1f}  ·  TP {analiz['tp']:,.1f}\n"
        f"Yatırım tavsiyesi değildir — sadece teknik gösterimdir."
    )
    fig.text(0.02, 0.02, alt_metin, color=INK, fontsize=11, va="bottom", ha="left")

    buf = io.BytesIO()
    fig.tight_layout(rect=(0, 0.10, 1, 1))
    fig.savefig(buf, format="png", facecolor=BG)
    plt.close(fig)
    return buf.getvalue()


if __name__ == "__main__":
    import sys as _sys
    from datetime import datetime
    from zoneinfo import ZoneInfo
    zd = _sys.argv[1] if len(_sys.argv) > 1 else VARSAYILAN_ZD
    mumlar, analiz = analiz_uret(zd)
    print(analiz)
    tarih = datetime.now(ZoneInfo("Europe/Istanbul")).strftime("%d.%m.%Y %H:%M")
    png = grafik_olustur(mumlar, analiz, tarih)
    open("teknik_ornek.png", "wb").write(png)
    print("teknik_ornek.png yazildi,", len(png), "byte")
