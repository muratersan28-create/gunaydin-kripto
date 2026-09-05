# -*- coding: utf-8 -*-
"""Ons altın için çoklu zaman dilimli (M5–H4) teknik analiz grafiği
(olası giriş/SL/TP1-2-3, AL/SAT/BEKLE durumu, Güven %). Ağ dışında LLM
KULLANMAZ — strateji tamamen kural bazlıdır:

  Trend    : SMA20 / SMA50 karşılaştırması (seçilen zaman diliminde)
  Destek/Direnç (iç hesap) : son mumlardaki swing high/low'lar (fraktal yöntem)
  Giriş/SL/TP   : trend yönüne göre en yakın destek/dirence dayalı basit senaryo
  TP1/TP2/TP3   : giriş ile TP arası %45/%72/%100 kademeli hedefler
  Durum (AL/SAT/BEKLE) : fiyat girişin ATR*0.5 yakınına gelmiş mi
  Güven %      : trend gücü + risk/ödül oranı + girişe yakınlık (ağırlıklı)

Veri kaynağı: Twelve Data'nın XAU/USD SPOT geçmiş verisi (TWELVE_DATA_API_KEY
gerekir — ücretsiz üyelik). Anahtar yoksa/istek başarısız olursa Yahoo
Finance'in ücretsiz (anahtarsız) GC=F (COMEX altın vadeli işlem) uç noktasına
düşer (H4 hariç — Yahoo'da bu periyot yok); vadeli işlem spot'tan biraz
farklı olduğundan ("basis") güncel spot fiyatla kaydırılarak düzeltilir.

Yatırım tavsiyesi değildir — yalnızca teknik/eğitim amaçlı gösterimdir.
"""
import io
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, FancyBboxPatch
from matplotlib.dates import DateFormatter
import matplotlib.dates as mdates

IST = ZoneInfo("Europe/Istanbul")
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

# Açık (beyaz) tema — AI GOLD ANALYST stiline uygun
BG = "#ffffff"
INK = "#111318"
MUTED = "#5b6270"
GRID = "#e6e8ec"
UP = "#1e9e5a"
DOWN = "#d43a3a"
GIRIS_RENK = "#2a5bd7"
SL_RENK = "#d43a3a"
TP_RENK = "#1e9e5a"
KUTU_BG = "#f3f4f6"


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


def _durum_belirle(guncel, giris, atr, yon_uzun):
    """AL / SAT / BEKLE — fiyat girişin ATR*0.5 yakınına gelmiş mi (kural bazlı)."""
    if not atr:
        return "BEKLE"
    if abs(guncel - giris) > atr * 0.5:
        return "BEKLE"
    return "AL" if yon_uzun.startswith("LONG") else "SAT"


def _guven_hesapla(sma20, sma50, atr, rr, guncel, giris):
    """0-100 arası Güven % — trend gücü + risk/ödül oranı + girişe yakınlık
    (ağırlıklı ortalama, kural bazlı; LLM kullanmaz)."""
    if not sma20 or not sma50 or not atr:
        return 40
    trend_gucu = min(abs(sma20 - sma50) / atr, 3) / 3 * 100
    rr_puan = min((rr or 0) / 3, 1) * 100
    yakinlik = max(0, 100 - abs(guncel - giris) / atr * 25)
    guven = trend_gucu * 0.4 + rr_puan * 0.3 + yakinlik * 0.3
    return int(max(10, min(90, round(guven))))


def _t_parse(t):
    """mumlar[i]['t'] değerini datetime'a çevirir (Twelve Data: string, Yahoo: unix ts)."""
    if isinstance(t, (int, float)):
        return datetime.fromtimestamp(t, tz=timezone.utc).astimezone(IST)
    try:
        return datetime.strptime(t, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return datetime.strptime(t, "%Y-%m-%d")


def analiz_uret(zaman_dilimi=VARSAYILAN_ZD, spot_fiyat=None):
    """OHLC çeker, giriş/SL/TP1-2-3 + durum + güven hesaplar.
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
    # (M5 ya da H4) mumların gerçek volatilitesine göre ölçeklenir.
    atr = _atr(mumlar, 14) or (guncel * 0.001)
    sl_tamponu = atr * 0.5

    if trend == "YUKARI":
        yon_uzun = "LONG (olası)"
        # desteğe yakın bir "pullback" bölgesi — destek ile güncel fiyat arasında %15
        giris = destek + (guncel - destek) * 0.15
        sl = destek - sl_tamponu
        tp = direnc
    else:
        yon_uzun = "SHORT (olası)"
        giris = direnc - (direnc - guncel) * 0.15
        sl = direnc + sl_tamponu
        tp = destek

    tp1 = giris + (tp - giris) * 0.45
    tp2 = giris + (tp - giris) * 0.72
    tp3 = tp

    risk = abs(giris - sl)
    odul = abs(tp - giris)
    rr = (odul / risk) if risk > 0 else None

    durum = _durum_belirle(guncel, giris, atr, yon_uzun)
    guven = _guven_hesapla(sma20, sma50, atr, rr, guncel, giris)

    # Sentetik BID/ASK: gerçek bir ücretsiz bid/ask beslemesi yok, o yüzden
    # gerçek spot fiyatın etrafına küçük (gerçekçi) bir spread koyuyoruz.
    yayilim = max(guncel * 0.00003, 0.05)

    analiz = {
        "guncel": guncel, "trend": trend, "yon": yon_uzun,
        "destek": destek, "direnc": direnc,
        "giris": giris, "sl": sl, "tp": tp, "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "rr": rr, "sma20": sma20, "sma50": sma50, "atr": atr,
        "zaman_dilimi": zaman_dilimi, "durum": durum, "guven": guven,
        "bid": guncel - yayilim, "ask": guncel + yayilim,
    }
    return mumlar, analiz


def _etiketleri_ayir(seviyeler, min_bosluk):
    """[(deger, ad), ...] -> {ad: etiket_y} — değerleri sırayı koruyarak
    min_bosluk kadar ayırır (üst üste binmesinler diye).

    İleri+geri geçişli, sabit tekrar sayılı bir düzleştirme kullanır — bazı
    değer kombinasyonlarında komşu çiftleri sırayla düzeltmeye çalışan bir
    while-döngüsü birbirini bozup SONSUZA KADAR salınabiliyordu (gerçek bug,
    H1 verisiyle ortaya çıktı). Bu yöntem sabit sayıda geçiş yapar, her
    zaman sonlanması garantidir.
    """
    sirali = sorted(seviyeler, key=lambda x: x[0])
    y = [s[0] for s in sirali]
    n = len(y)
    for _ in range(4):
        for i in range(1, n):                    # ileri geçiş
            if y[i] - y[i - 1] < min_bosluk:
                y[i] = y[i - 1] + min_bosluk
        for i in range(n - 2, -1, -1):            # geri geçiş
            if y[i + 1] - y[i] < min_bosluk:
                y[i] = y[i + 1] - min_bosluk
    return {sirali[i][1]: y[i] for i in range(n)}


def grafik_olustur(mumlar, analiz, son_n=None):
    """PNG bytes döndürür: AI GOLD ANALYST stilinde mum grafiği —
    başlık + CANLI BID/ASK (içeride), GİRİŞ/SL/TP1-2-3 çizgileri (sona doğru
    etiketlere hafifçe açılır), alt bilgi kutusu, dış çerçeve."""
    zd = analiz.get("zaman_dilimi", VARSAYILAN_ZD)
    if son_n is None:
        son_n = TIMEFRAMES[zd]["gosterim"]
    veri = mumlar[-son_n:]
    yon = "BUY" if analiz["yon"].startswith("LONG") else "SELL"
    giris, sl = analiz["giris"], analiz["sl"]
    tp1, tp2, tp3 = analiz["tp1"], analiz["tp2"], analiz["tp3"]

    fig, ax = plt.subplots(figsize=(13.2, 7.0), dpi=100)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)

    zamanlar = [_t_parse(m["t"]) for m in veri]
    if len(zamanlar) > 1:
        adim = zamanlar[1] - zamanlar[0]
    else:
        adim = mdates.timedelta(minutes=5)
    genislik_gun = abs(adim.total_seconds()) / 86400 * 0.88
    min_govde = (max(m["h"] for m in veri) - min(m["l"] for m in veri)) * 0.006

    for i, m in enumerate(veri):
        renk = UP if m["c"] >= m["o"] else DOWN
        ax.plot([zamanlar[i], zamanlar[i]], [m["l"], m["h"]], color=renk, linewidth=1.3, zorder=2)
        alt = min(m["o"], m["c"])
        yukseklik = max(abs(m["c"] - m["o"]), min_govde)
        ax.add_patch(Rectangle((mdates.date2num(zamanlar[i]) - genislik_gun / 2, alt),
                                genislik_gun, yukseklik, facecolor=renk, edgecolor="none", zorder=3))

    son_mum_num = mdates.date2num(zamanlar[-1])
    x1_num = son_mum_num + abs(adim.total_seconds()) / 86400 * 9
    x1_dt = mdates.num2date(x1_num).replace(tzinfo=None)
    kirilma_num = son_mum_num + (x1_num - son_mum_num) * 0.15

    tum_y_araligi = max(m["h"] for m in veri) - min(m["l"] for m in veri)
    min_bosluk = tum_y_araligi * 0.045
    etiket_y = _etiketleri_ayir(
        [(giris, "GİRİŞ"), (sl, "SL"), (tp1, "TP1"), (tp2, "TP2"), (tp3, "TP3")], min_bosluk)

    def seviye_ciz(gercek_y, ad, renk, stil, kalinlik):
        ax.hlines(gercek_y, zamanlar[0], mdates.num2date(kirilma_num).replace(tzinfo=None),
                  colors=renk, linestyles=stil, linewidth=kalinlik, zorder=1)
        ax.plot([mdates.num2date(kirilma_num).replace(tzinfo=None), x1_dt],
                [gercek_y, etiket_y[ad]], color=renk, linestyle=stil, linewidth=kalinlik, zorder=1)

    seviye_ciz(giris, "GİRİŞ", GIRIS_RENK, "-", 1.6)
    seviye_ciz(sl, "SL", SL_RENK, "-", 1.6)
    ax.hlines(sl + (giris - sl) * 0.08, zamanlar[0], mdates.num2date(kirilma_num).replace(tzinfo=None),
              colors="#9aa1ac", linewidth=1, linestyles=":", zorder=1)
    seviye_ciz(tp1, "TP1", TP_RENK, "--", 1.3)
    seviye_ciz(tp2, "TP2", TP_RENK, "--", 1.3)
    seviye_ciz(tp3, "TP3", TP_RENK, "--", 1.3)

    def etiket_kutusu(ad, gercek_deger, renk):
        ax.annotate(f"{ad} {yon}  {gercek_deger:,.2f}", xy=(x1_dt, etiket_y[ad]),
                    xytext=(5, 0), textcoords="offset points",
                    va="center", ha="left", fontsize=9.5, fontweight="bold", color="white",
                    bbox=dict(boxstyle="round,pad=0.3", facecolor=renk, edgecolor="none"), zorder=5)

    etiket_kutusu("TP3", tp3, TP_RENK)
    etiket_kutusu("TP2", tp2, TP_RENK)
    etiket_kutusu("TP1", tp1, TP_RENK)
    etiket_kutusu("GİRİŞ", giris, GIRIS_RENK)
    etiket_kutusu("SL", sl, SL_RENK)

    ax.set_xlim(zamanlar[0], x1_dt)
    tum_y = [m["l"] for m in veri] + [m["h"] for m in veri] + list(etiket_y.values())
    araci = max(tum_y) - min(tum_y)
    # Üstte CANLI BID/ASK yazısı için, altta açıklama kutusu için pay bırak —
    # yoksa SL/GİRİŞ/TP çizgileri (senaryoya göre üstte ya da altta kalabilir)
    # bu sabit-konumlu öğelerin üzerine biniyordu.
    ax.set_ylim(min(tum_y) - araci * 0.22, max(tum_y) + araci * 0.16)

    ax.yaxis.tick_right()
    ax.tick_params(colors=MUTED, labelsize=8.5)
    for taraf in ("top", "left"):
        ax.spines[taraf].set_visible(False)
    for taraf in ("right", "bottom"):
        ax.spines[taraf].set_color(GRID)
    ax.grid(color=GRID, linewidth=0.6, axis="x", zorder=0)
    ax.xaxis.set_major_formatter(DateFormatter("%d %b\n%H:%M"))
    fig.autofmt_xdate(rotation=0, ha="center")

    ax.text(0.008, 0.965, f"CANLI BID: {analiz['bid']:,.2f}   |   ASK: {analiz['ask']:,.2f}",
            transform=ax.transAxes, color=INK, fontsize=9.5, va="top", ha="left", zorder=6)

    kutu = FancyBboxPatch((0.008, 0.008), 0.30, 0.115, transform=ax.transAxes,
                          boxstyle="round,pad=0.01,rounding_size=0.012",
                          facecolor=KUTU_BG, edgecolor="#c9cdd4", linewidth=1, zorder=10)
    ax.add_patch(kutu)
    ax.text(0.018, 0.088, f"{zd} {giris:,.2f} tetik seviyesinde kapanış beklenir.",
            transform=ax.transAxes, color=INK, fontsize=8.7, va="top", zorder=11)
    ax.text(0.018, 0.048, "Sadece giriş, SL ve hedefler gösterilir. Eğitim/test amaçlıdır.",
            transform=ax.transAxes, color=INK, fontsize=8.7, va="top", zorder=11)

    fig.subplots_adjust(left=0.008, right=0.855, top=0.905, bottom=0.10)

    baslik = (f"AI GOLD ANALYST  |  XAUUSD {zd} {yon} TETİK SENARYOSU  |  "
              f"{analiz['durum']}  |  Güven %{analiz['guven']}")
    fig.text(0.012, 0.965, baslik, color=INK, fontsize=14, fontweight="bold", va="top", ha="left")

    fig.add_artist(plt.Rectangle((0.002, 0.002), 0.996, 0.996, transform=fig.transFigure,
                   fill=False, edgecolor="#1a1d23", linewidth=1.3, zorder=20))

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG)
    plt.close(fig)
    return buf.getvalue()


if __name__ == "__main__":
    import sys as _sys
    zd = _sys.argv[1] if len(_sys.argv) > 1 else VARSAYILAN_ZD
    mumlar, analiz = analiz_uret(zd)
    print(analiz)
    png = grafik_olustur(mumlar, analiz)
    open("teknik_ornek.png", "wb").write(png)
    print("teknik_ornek.png yazildi,", len(png), "byte")
