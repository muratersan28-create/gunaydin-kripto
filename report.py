#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Günlük Altın Raporu → Telegram Botu
=====================================

Üç adımda çalışır:
  Adım 1  Sabit piyasa verilerini ücretsiz API'lerden çeker (LLM YOK).
  Adım 2  Headless Claude Code (claude -p) ile haber/analiz bölümünü üretir.
  Adım 3  Raporu Telegram'a HTML formatında, 4096 karakter limitine uyarak gönderir.

Kullanım:
  python report.py            → Raporu kanala (TELEGRAM_CHAT_ID) gönderir.
  python report.py --test     → Raporu SADECE admin'e (TELEGRAM_ADMIN_CHAT_ID) gönderir.
  python report.py --onizleme → Sadece admin'e; [TEST] etiketi yok, gerçek rapor görünümü.

Kimlik doğrulama ve gizli anahtarlar ortam değişkenlerinden okunur (koda gömülmez):
  CLAUDE_CODE_OAUTH_TOKEN     → Claude aboneliği token'ı (claude setup-token çıktısı)
  TELEGRAM_BOT_TOKEN          → BotFather'dan alınan bot token'ı
  TELEGRAM_CHAT_ID            → Raporun gideceği kanal
  TELEGRAM_ADMIN_CHAT_ID      → Senin özel chat'in (hata bildirimleri + test)
"""

import os
import sys
import time
import html
import json
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

# --------------------------------------------------------------------------- #
# Sabitler
# --------------------------------------------------------------------------- #

def _env_yukle():
    """Yanında bir .env dosyası varsa değişkenleri ortama yükler (yerel test için).
    GitHub Actions'ta .env olmaz; değişkenler zaten secret'lardan gelir."""
    yol = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(yol):
        return
    with open(yol, encoding="utf-8") as f:
        for satir in f:
            satir = satir.strip()
            if not satir or satir.startswith("#") or "=" not in satir:
                continue
            anahtar, _, deger = satir.partition("=")
            # Ortamda zaten tanımlıysa üzerine yazma (secret'lar önceliklidir)
            os.environ.setdefault(anahtar.strip(), deger.strip())


_env_yukle()

IST = ZoneInfo("Europe/Istanbul")          # Tüm tarih/saatler İstanbul saatiyle
__imza__ = "DogukanLive"                    # gizli imza — aracın kökeni
TELEGRAM_LIMIT = 4096                        # Telegram tek mesaj karakter limiti
SAFE_LIMIT = 3800                            # HTML tag'leri için güvenli tampon bırakıyoruz
HTTP_TIMEOUT = 30                            # API çağrıları için saniye
MAX_RETRY = 3                                # Ağ hatalarında deneme sayısı
CLAUDE_TIMEOUT = 600                         # Claude Code üretimi için üst sınır (saniye)

# 1 ons (troy ounce) = 31.1034768 gram — ons fiyatından gram altın hesaplamak için
ONS_GRAM = 31.1034768

# Türkçe ay ve gün adları (tarih başlığı için)
TR_AYLAR = [
    "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
    "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık",
]
TR_GUNLER = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]


# RAPOR PROMPTU — Adım 2'de headless Claude Code'a verilir.
# {market_data} ve {tarih} çalışma anında doldurulur.
RAPOR_PROMPTU = """Sen günlük altın/emtia piyasa raporu hazırlayan, sabahları işe çıkmadan piyasayı 1 dakikada özetleyen bir analistsin. Web araması yaparak SON 24 SAATİN gelişmelerini araştır ve aşağıdaki formatta Türkçe rapor yaz.

SANA VERİLEN PİYASA VERİLERİ:
{market_data}
Fiyat ve değişim değerlerini YALNIZCA buradan kullan; bu sayıları kendin arama, değiştirme, uydurma.

ÇIKTI — Telegram HTML (<b>, <i>, <a href="">); markdown ve tablo KULLANMA. Rapor İKİ bölümdür; aralarına TAM olarak şu satırı koy: ---DETAY---

Önce BÖLÜM 1'i (60 SANİYE — kendi içinde tam, bir dakikada okunur, KISA tut) tam bu iskeletle yaz:

📊 <b>GÜNAYDIN — {tarih}</b>

⚡ <b>60 SANİYE</b>
🌡️ Hava: [tek kelime: Temkinli / İyimser / Kararsız / Riskli]
🥇 ONS ALTIN [fiyat] ([24s %]) · 🪙 GRAM ALTIN [fiyat TL] ([24s %])
🔑 <b>Neden:</b> [Piyasa son 24 saatte NEDEN böyle hareket etti — Fed/faiz, dolar endeksi, jeopolitik risk gibi ana sebep, 1-2 cümle]
⏰ <b>Kritik:</b> [bugünün en önemli 2-3 olayı/saati, TSİ, çok kısa]
⚠️ <b>Risk:</b> [günün ana riski, tek cümle]

Sonra ayrı bir satıra ---DETAY--- koy. Sonra BÖLÜM 2'yi (DETAY, isteyen için) yaz:

📈 <b>PİYASA</b>
[her satır: varlık — fiyat ([24s %])]
Ons altın (XAU/USD), gram altın (TRY), gümüş (XAG/USD), USD/TRY ve EUR/TRY kurları — hepsini tek satır listele.

⏮️ <b>DÜNDEN</b> — Aşağıda "dünkü takip maddeleri" verildiyse, her birinin bugünkü SONUCUNU tek cümleyle yaz (isabet mi ıskaladık mı belli olsun). Madde verilmediyse bu bölümü TAMAMEN atla.

📰 <b>GÜNDEM</b> — en önemli 3 gelişme. Her biri: [🔴 kritik / 🟡 önemli / 🟢 bilgi] <b>başlık</b> — 1-2 cümle + neden önemli — <a href="URL">kaynak</a>

⏰ <b>BUGÜN TAKİPTE</b>
[zaman sıralı, her olay tek satır: "14:00 — ..." (TSİ) — Fed konuşmaları, ABD enflasyon/istihdam verileri, TCMB kararları gibi altını etkileyecek olaylar]

🇹🇷 <b>TÜRKİYE</b> — TCMB rezerv/faiz kararı, altın ithalat-ihracat mevzuatı veya BDDK'da YENİ gelişme varsa yaz; yoksa "Yeni gelişme yok". Asla uydurma.

⚠️ <b>RİSKLER</b> — en fazla 2 madde, kısa.

<i>Yatırım tavsiyesi değildir.</i>

KURALLAR:
- 60 SANİYE bölümündeki her satır bir bakışta okunmalı; kısa ve yoğun tut.
- "Neden" satırı en kritik kısımdır: hareketin gerçek sebebini araştır; net sebep yoksa "belirgin tek sebep yok" de.
- Doğrulayamadığın hiçbir sayıyı yazma; gerekiyorsa "doğrulanamadı" de. Boş/okunamayan veriyi sıfır sayma.
- Uydurma metrik (100 üzerinden puan, güven skoru) YOK. Önem için sadece 🔴🟡🟢.
- Al/sat/tut önerisi ve fiyat hedefi verme.
- Her gelişmede en az bir birincil/kurumsal kaynak linki; URL'lerdeki utm parametrelerini temizle.
- Tekrar eden uyarı ekleme. DETAY bölümü toplam 1500-3500 karakter olsun.
- Çıktı olarak SADECE raporu ver; "işte rapor" ya da "BÖLÜM 1/2" gibi ifade yazma. İlk satır doğrudan "📊 <b>GÜNAYDIN" ile başlasın.

RAPORUN EN SONUNA — Telegram'a GİTMEYECEK — bugün öne çıkan, YARIN sonucuna bakılacak 2-4 maddeyi tam bu formatta ekle:
===TAKIP===
["kısa madde 1", "kısa madde 2"]
===TAKIP-SON===
Geçerli JSON dizisi olsun; her madde kısa ve sonucu ölçülebilir olsun (ör. "ABD TÜFE verisi 15:30")."""


# --------------------------------------------------------------------------- #
# Yardımcı: ağ isteği için retry sarmalayıcı
# --------------------------------------------------------------------------- #

def _get_json(url, params=None):
    """Verilen URL'den JSON çeker; ağ hatalarında MAX_RETRY kez dener."""
    last_err = None
    for deneme in range(1, MAX_RETRY + 1):
        try:
            r = requests.get(url, params=params, timeout=HTTP_TIMEOUT,
                             headers={"User-Agent": "crypto-daily-report/1.0"})
            r.raise_for_status()
            return r.json()
        except Exception as e:                       # noqa: BLE001 (her ağ hatasını yakala)
            last_err = e
            print(f"[uyarı] İstek başarısız ({deneme}/{MAX_RETRY}): {url} -> {e}",
                  file=sys.stderr)
            if deneme < MAX_RETRY:
                time.sleep(2 * deneme)               # kademeli bekleme
    raise RuntimeError(f"API çağrısı {MAX_RETRY} denemede başarısız: {url} ({last_err})")


# --------------------------------------------------------------------------- #
# Adım 1 — Sabit piyasa verileri (LLM KULLANMADAN)
# --------------------------------------------------------------------------- #

def _yuzde_degisim(simdi, onceki):
    """(simdi - onceki) / onceki * 100; ikisinden biri yoksa/onceki sıfırsa None."""
    if simdi is None or onceki is None or onceki == 0:
        return None
    return (simdi - onceki) / onceki * 100


def piyasa_verilerini_cek():
    """
    gold-api.com (ons altın + gümüş, USD) + frankfurter.dev (USD/TRY) üzerinden
    ham sayıları çeker ve LLM'e verilecek okunabilir bir metin bloğu üretir.
    LLM bu sayıları asla değiştirmez. 24s değişim, dünün kapanışının
    state/takip.json'da saklanmasıyla hesaplanır (API geçmiş veri vermiyor).
    """
    # 1a) Ons altın + gümüş (USD)
    xau = _get_json("https://api.gold-api.com/price/XAU")
    xag = _get_json("https://api.gold-api.com/price/XAG")
    altin_ons = xau.get("price")
    gumus_ons = xag.get("price")

    # 1b) USD/TRY ve EUR/TRY kurları
    fx_usd = _get_json(
        "https://api.frankfurter.dev/v1/latest",
        params={"base": "USD", "symbols": "TRY"},
    )
    usdtry = fx_usd.get("rates", {}).get("TRY")
    fx_eur = _get_json(
        "https://api.frankfurter.dev/v1/latest",
        params={"base": "EUR", "symbols": "TRY"},
    )
    eurtry = fx_eur.get("rates", {}).get("TRY")

    # 1c) Gram altın (TRY) = ons fiyatı / 31.1034768 gram * USD/TRY kuru
    gram_altin = (altin_ons / ONS_GRAM * usdtry) if (altin_ons and usdtry) else None

    # 1d) Dünün kapanışıyla karşılaştırıp 24s değişim hesapla
    onceki = onceki_fiyatlari_oku()
    altin_ons_degisim = _yuzde_degisim(altin_ons, onceki.get("altin_ons"))
    gram_altin_degisim = _yuzde_degisim(gram_altin, onceki.get("gram_altin"))
    gumus_ons_degisim = _yuzde_degisim(gumus_ons, onceki.get("gumus_ons"))
    usdtry_degisim = _yuzde_degisim(usdtry, onceki.get("usdtry"))
    eurtry_degisim = _yuzde_degisim(eurtry, onceki.get("eurtry"))

    # --- LLM'e verilecek okunabilir metin bloğunu kur ---
    def satir(ad, fiyat, degisim, birim="$"):
        if fiyat is None:
            return f"  {ad}: doğrulanamadı"
        fiyat_str = f"{birim}{fiyat:,.2f}"
        degisim_str = f"{degisim:+.2f}%" if degisim is not None else "n/a (ilk çalışma)"
        return f"  {ad}: {fiyat_str} ({degisim_str})"

    satirlar = ["Piyasa fiyatları (24s değişim):"]
    satirlar.append(satir("Ons altın (XAU/USD)", altin_ons, altin_ons_degisim, "$"))
    satirlar.append(satir("Gram altın (TRY)", gram_altin, gram_altin_degisim, "₺"))
    satirlar.append(satir("Gümüş (XAG/USD)", gumus_ons, gumus_ons_degisim, "$"))
    satirlar.append(satir("USD/TRY", usdtry, usdtry_degisim, ""))
    satirlar.append(satir("EUR/TRY", eurtry, eurtry_degisim, ""))

    # --- Kart için yapılandırılmış ham veri ---
    veri = {
        "altin_ons": {"fiyat": altin_ons, "degisim": altin_ons_degisim},
        "gram_altin": {"fiyat": gram_altin, "degisim": gram_altin_degisim},
        "gumus_ons": {"fiyat": gumus_ons, "degisim": gumus_ons_degisim},
        "usdtry": {"fiyat": usdtry, "degisim": usdtry_degisim},
        "eurtry": {"fiyat": eurtry, "degisim": eurtry_degisim},
        # Yarının 24s değişimi için bugünün ham fiyatlarını da taşıyoruz
        # (takip_yaz() bunu state/takip.json'a yazar).
        "_kaydedilecek": {
            "altin_ons": altin_ons, "gram_altin": gram_altin,
            "gumus_ons": gumus_ons, "usdtry": usdtry, "eurtry": eurtry,
        },
    }

    return "\n".join(satirlar), veri


# --------------------------------------------------------------------------- #
# Adım 2 — Headless Claude Code ile haber/analiz üretimi
# --------------------------------------------------------------------------- #

def tarih_basligi():
    """Bugünün tarihini '17 Temmuz 2026, Perşembe' biçiminde döndürür (TSİ)."""
    now = datetime.now(IST)
    return f"{now.day} {TR_AYLAR[now.month - 1]} {now.year}, {TR_GUNLER[now.weekday()]}"


def rapor_uret(market_data, dun_takip_str=""):
    """
    Headless Claude Code'u (claude -p) çağırır. Anthropic API KEY kullanmaz;
    kimlik doğrulama CLAUDE_CODE_OAUTH_TOKEN ile abonelikten yapılır.
    Sadece WebSearch/WebFetch araçlarına izin verilir.
    """
    # Windows'ta npm 'claude' (bash script) + 'claude.cmd' üretir; subprocess ancak
    # .cmd/.exe çalıştırabilir. Bu yüzden platforma göre uygun olanı seç.
    adaylar = ["claude.cmd", "claude.exe", "claude"] if os.name == "nt" else ["claude"]
    claude_bin = next((shutil.which(a) for a in adaylar if shutil.which(a)), None)
    if not claude_bin:
        raise RuntimeError(
            "'claude' komutu bulunamadı. Kurulum: npm install -g @anthropic-ai/claude-code"
        )
    # CI'da kimlik CLAUDE_CODE_OAUTH_TOKEN ile gelir. Yerelde bu değişken yoksa,
    # makinede zaten giriş yapılmış claude oturumu kullanılır (uyarı verip devam et).
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        print("[uyarı] CLAUDE_CODE_OAUTH_TOKEN yok; mevcut yerel claude oturumu "
              "kullanılacak (CI'da secret gereklidir).", file=sys.stderr)

    prompt = RAPOR_PROMPTU.format(market_data=market_data, tarih=tarih_basligi())
    if dun_takip_str:
        prompt += chr(10) * 2 + "DÜNKÜ TAKİP MADDELERİ (⏮️ DÜNDEN için sonuçlarını araştır): " + dun_takip_str
    else:
        prompt += chr(10) * 2 + "DÜNKÜ TAKİP MADDELERİ: yok (⏮️ DÜNDEN bölümünü atla)."

    # Çıktıyı düz metin olarak alıyoruz. --allowedTools ile sadece web araçlarına izin.
    # Repo dizininde çalıştırırsak model report.py'yi okuyup "ben bir kurulum
    # asistanıyım" moduna girebiliyor (CLAUDE.md + kod dosyalarını keşfediyor) —
    # bu yüzden izole, boş bir geçici dizinde çalıştırıyoruz; raporu yazmak için
    # repo dosyalarına ihtiyacı yok, sadece {market_data} ve web araması yeterli.
    izole_dizin = tempfile.mkdtemp(prefix="altin-rapor-")
    # ÖNEMLİ: -p/prompt argümanı EN SONDA olmalı — önce gelirse CLI parser
    # sonrasındaki --allowedTools/--output-format bayraklarını prompt'un bir
    # parçası sanıp yutuyor ve WebSearch hiç onaylanmamış oluyor.
    komut = [
        claude_bin,
        "--allowedTools", "WebSearch", "WebFetch",
        "--output-format", "text",
        "-p", prompt,
    ]

    print("[bilgi] Claude Code raporu üretiyor (web araması yapılıyor)...", file=sys.stderr)
    # Geçici claude hatalarına (rate/hiçkırık) karşı birkaç kez dene — 08:00 raporu
    # tek bir aksaklıkta atlanmasın.
    son_hata = None
    for deneme in range(1, MAX_RETRY + 1):
        try:
            sonuc = subprocess.run(
                komut, capture_output=True, text=True,
                encoding="utf-8", timeout=CLAUDE_TIMEOUT, cwd=izole_dizin,
            )
        except subprocess.TimeoutExpired:
            son_hata = f"{CLAUDE_TIMEOUT} sn'de yanıt yok"
            sonuc = None

        if sonuc is not None:
            if sonuc.returncode != 0:
                son_hata = "kod %d: %s" % (
                    sonuc.returncode,
                    (sonuc.stderr.strip() or sonuc.stdout.strip() or "(çıktı boş)")[:600])
            elif len((sonuc.stdout or "").strip()) < 200:
                son_hata = "beklenenden kısa çıktı: %r" % (sonuc.stdout or "").strip()
            else:
                shutil.rmtree(izole_dizin, ignore_errors=True)
                return sonuc.stdout.strip()

        print(f"[uyarı] Rapor üretimi başarısız ({deneme}/{MAX_RETRY}): {son_hata}",
              file=sys.stderr)
        if deneme < MAX_RETRY:
            time.sleep(8 * deneme)

    shutil.rmtree(izole_dizin, ignore_errors=True)
    raise RuntimeError(f"Claude Code {MAX_RETRY} denemede rapor üretemedi: {son_hata}")


# --------------------------------------------------------------------------- #
# Adım 3 — Telegram'a gönderim
# --------------------------------------------------------------------------- #

def mesaji_bol(metin, limit=SAFE_LIMIT):
    """
    Uzun raporu, bölüm sınırlarını mümkün olduğunca koruyarak <limit karakterlik
    parçalara böler. Önce paragraf (çift satır), gerekirse satır, en son
    zorunlu olarak karakter bazında böler.
    """
    if len(metin) <= limit:
        return [metin]

    parcalar = []
    tampon = ""

    def akit():
        nonlocal tampon
        if tampon.strip():
            parcalar.append(tampon.strip())
        tampon = ""

    for paragraf in metin.split("\n\n"):
        # Paragraf tek başına limitten büyükse satır bazında böl
        if len(paragraf) > limit:
            akit()
            for satir in paragraf.split("\n"):
                while len(satir) > limit:
                    parcalar.append(satir[:limit])
                    satir = satir[limit:]
                if len(tampon) + len(satir) + 1 > limit:
                    akit()
                tampon = (tampon + "\n" + satir) if tampon else satir
            continue

        # Normal durum: paragrafı tampona ekle, taşarsa akıt
        if len(tampon) + len(paragraf) + 2 > limit:
            akit()
        tampon = (tampon + "\n\n" + paragraf) if tampon else paragraf

    akit()
    return parcalar


def _html_temizle(metin):
    """HTML etiketlerini kaldırıp düz metne çevirir (fallback için)."""
    metin = re.sub(r"<[^>]+>", "", metin)
    return (metin.replace("&lt;", "<").replace("&gt;", ">")
                 .replace("&quot;", chr(34)).replace("&amp;", "&"))


def _gizle(metin):
    """Log'a yazılacak metinden Telegram bot token'ını siler.

    Telegram uç noktası token'ı URL'in içinde taşır (…/bot<TOKEN>/sendMessage).
    Ağ hatasında `requests` bu URL'i hata mesajına gömer; mesaj da Actions
    loguna düşer. Repo public olduğu için o log herkese açıktır. GitHub zaten
    secret'ları maskeler, bu ikinci savunma katmanı: maskeleme yalnız birebir
    eşleşmeyi yakalar, burada kaynağında temizliyoruz.
    """
    s = str(metin)
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if tok:
        s = s.replace(tok, "***")
    return re.sub(r"bot\d{6,12}:[A-Za-z0-9_-]{10,}", "bot***", s)


def telegram_gonder(bot_token, chat_id, metin, html_modu=True):
    """Tek bir Telegram mesajı gönderir. HTML ayrıştırma hatasında (ör. bölme bir
    etiketi bozmuşsa) düz metne düşer; ağ hatalarında retry yapar."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": metin,
        "disable_web_page_preview": True,
    }
    if html_modu:
        payload["parse_mode"] = "HTML"
    last_err = None
    for deneme in range(1, MAX_RETRY + 1):
        try:
            r = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
            data = r.json()
            if not data.get("ok"):
                aciklama = str(data.get("description", ""))
                # HTML ayrıştırma hatasıysa: etiketleri temizleyip düz metin olarak
                # TEK sefer yeniden dene (mesajın hiç gitmemesindense düz gitsin).
                if html_modu and any(k in aciklama.lower()
                                     for k in ("parse", "entit", "tag")):
                    print(f"[uyarı] HTML hatası, düz metne düşülüyor: {aciklama}",
                          file=sys.stderr)
                    return telegram_gonder(bot_token, chat_id,
                                           _html_temizle(metin), html_modu=False)
                raise RuntimeError(f"Telegram API hatası: {aciklama}")
            return data
        except Exception as e:                       # noqa: BLE001
            last_err = e
            print(f"[uyarı] Telegram gönderimi başarısız ({deneme}/{MAX_RETRY}): {_gizle(e)}",
                  file=sys.stderr)
            if deneme < MAX_RETRY:
                time.sleep(2 * deneme)
    raise RuntimeError(f"Telegram gönderimi {MAX_RETRY} denemede başarısız: {_gizle(last_err)}")


def raporu_yolla(bot_token, chat_id, rapor):
    """Raporu parçalara bölüp sırayla gönderir (mesajlar arası 1 sn bekler)."""
    bolumler = [b.strip() for b in rapor.split("---DETAY---") if b.strip()] or [rapor]
    parcalar = []
    for bolum in bolumler:
        parcalar.extend(mesaji_bol(bolum))
    toplam = len(parcalar)
    for i, parca in enumerate(parcalar, 1):
        # Birden fazla parça varsa küçük bir sayfa göstergesi ekle
        if toplam > 1:
            parca = f"{parca}\n\n<i>({i}/{toplam})</i>"
        telegram_gonder(bot_token, chat_id, parca)
        if i < toplam:
            time.sleep(1)
    return toplam


# --------------------------------------------------------------------------- #
# Hata bildirimi
# --------------------------------------------------------------------------- #

def admin_hata_bildir(mesaj):
    """Üretim/gönderim hatasında admin'e kısa bir özet gönderir (best-effort)."""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    admin_id = os.environ.get("TELEGRAM_ADMIN_CHAT_ID")
    if not (bot_token and admin_id):
        return
    now = datetime.now(IST).strftime("%d.%m.%Y %H:%M")
    guvenli = html.escape(mesaj[:1000])
    metin = f"⚠️ <b>Altın rapor botu HATASI</b>\n{now} (TSİ)\n\n<code>{guvenli}</code>"
    try:
        telegram_gonder(bot_token, admin_id, metin)
    except Exception as e:                           # noqa: BLE001
        print(f"[uyarı] Admin'e hata bildirimi de gönderilemedi: {_gizle(e)}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Ana akış
# --------------------------------------------------------------------------- #

# Rapor, teslim saatinden en fazla bu kadar önce üretilir (veri taze kalsın diye).
GONDERIM_URETIM_BUTCESI = 720  # saniye (~12 dk; Claude'un 10 dk timeout'unu aşacak pay)


def _hedef_zaman_ist(hhmm):
    """Bugün için Europe/Istanbul HH:MM zaman damgasını döndürür."""
    saat, dakika = hhmm.split(":")
    return datetime.now(IST).replace(hour=int(saat), minute=int(dakika),
                                     second=0, microsecond=0)


def _bekle_kadar(hedef_dt, aciklama):
    """hedef_dt'ye kadar bekler; zaman geçmişse hiç beklemez."""
    kalan = hedef_dt.timestamp() - datetime.now(IST).timestamp()
    if kalan > 0:
        print(f"[bilgi] {aciklama} ({int(kalan)} sn bekleniyor)...", file=sys.stderr)
        time.sleep(kalan)


STATE_YOL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "takip.json")


def dunku_takip_oku():
    """state/takip.json varsa dünkü takip maddelerini (liste) döndürür; yoksa []."""
    try:
        with open(STATE_YOL, encoding="utf-8") as f:
            t = json.load(f).get("takip", [])
        return t if isinstance(t, list) else []
    except (FileNotFoundError, ValueError, OSError):
        return []


def takip_ayikla(rapor):
    """Rapordan ===TAKIP===...===TAKIP-SON=== bloğunu ayıklar (Telegram'a gitmez).
    (temiz_rapor, liste) döndürür."""
    import re as _re
    m = _re.search(r"===TAKIP===\s*(.*?)\s*===TAKIP-SON===", rapor, _re.S)
    takip = []
    if m:
        try:
            v = json.loads(m.group(1).strip())
            if isinstance(v, list):
                takip = [str(x).strip() for x in v if str(x).strip()]
        except ValueError:
            takip = []
    temiz = _re.split(r"===TAKIP===", rapor, maxsplit=1)[0].strip()
    return temiz, takip


def onceki_fiyatlari_oku():
    """state/takip.json varsa dünün ham fiyat kaydını (dict) döndürür; yoksa {}.
    24 saatlik değişim yüzdesini hesaplamak için kullanılır."""
    try:
        with open(STATE_YOL, encoding="utf-8") as f:
            f = json.load(f).get("fiyatlar", {})
        return f if isinstance(f, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def takip_yaz(takip, fiyatlar=None):
    """Bugünün takip listesini ve fiyat kaydını state/takip.json'a yazar
    (yarın 'DÜNDEN' ve 24s değişim hesabı için)."""
    os.makedirs(os.path.dirname(STATE_YOL), exist_ok=True)
    with open(STATE_YOL, "w", encoding="utf-8") as f:
        json.dump({"tarih": datetime.now(IST).strftime("%Y-%m-%d"), "takip": takip,
                   "fiyatlar": fiyatlar or {}},
                  f, ensure_ascii=False, indent=2)


def bugun_gonderildi_mi():
    """Bugünün raporu gönderildi mi? (nöbetçi koruması)

    GitHub cron'u saatlerce geciktirebildiği için gün içinde birden fazla
    "nöbetçi" tetiklemesi var. İlk uyanan raporu gönderir ve takip.json'a
    bugünün tarihini yazar; geç uyanan nöbetçiler burayı görüp hiç
    çalışmadan çıkar. Böylece teslim saati sabit kalır, çift rapor gitmez.
    """
    try:
        with open(STATE_YOL, encoding="utf-8") as f:
            veri = json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return False
    return veri.get("tarih") == datetime.now(IST).strftime("%Y-%m-%d")


def _brief_ayikla(rapor):
    """Brief'ten Hava (mood) ve Risk satırlarını ayıklar (kart için)."""
    duz = _html_temizle(rapor)
    hava, risk = "—", ""
    for satir in duz.splitlines():
        s = satir.strip()
        if hava == "—" and "Hava:" in s:
            sonra = s.split("Hava:", 1)[1]
            hava = sonra.split("·")[0].split("F&G")[0].strip() or "—"
        if not risk and "Risk:" in s:
            risk = s.split("Risk:", 1)[1].strip()
    if not risk:
        risk = "Bugün belirgin tek risk öne çıkmıyor."
    return hava, risk


def foto_gonder(bot_token, chat_id, png_bytes, caption="", html_modu=True):
    """Telegram'a fotoğraf (kart) gönderir. Altyazı HTML ayrıştırma hatası verirse düz
    metne düşer; ağ hatasında retry yapar."""
    url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    veri = {"chat_id": chat_id, "caption": caption}
    if caption and html_modu:
        veri["parse_mode"] = "HTML"
    last_err = None
    for deneme in range(1, MAX_RETRY + 1):
        try:
            r = requests.post(url, data=veri,
                              files={"photo": ("kart.png", png_bytes, "image/png")},
                              timeout=HTTP_TIMEOUT)
            cevap = r.json()
            if not cevap.get("ok"):
                aciklama = str(cevap.get("description", ""))
                if caption and html_modu and any(k in aciklama.lower()
                                                 for k in ("parse", "entit", "tag")):
                    return foto_gonder(bot_token, chat_id, png_bytes,
                                       _html_temizle(caption), html_modu=False)
                raise RuntimeError(f"sendPhoto hatası: {aciklama}")
            return cevap
        except Exception as e:                       # noqa: BLE001
            last_err = e
            print(f"[uyarı] Kart gönderimi başarısız ({deneme}/{MAX_RETRY}): {_gizle(e)}",
                  file=sys.stderr)
            if deneme < MAX_RETRY:
                time.sleep(2 * deneme)
    raise RuntimeError(f"Kart gönderimi {MAX_RETRY} denemede başarısız: {_gizle(last_err)}")


def main():
    test_modu = "--test" in sys.argv
    onizleme = "--onizleme" in sys.argv  # sadece admin, etiketsiz (önizleme)
    both_modu = "--both" in sys.argv    # Ayni raporu hem admin'e hem kanala gonder

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    kanal_id = os.environ.get("TELEGRAM_CHAT_ID")
    admin_id = os.environ.get("TELEGRAM_ADMIN_CHAT_ID")

    if not bot_token:
        print("HATA: TELEGRAM_BOT_TOKEN tanımlı değil.", file=sys.stderr)
        sys.exit(1)

    if both_modu:
        hedefler = [("admin", admin_id), ("kanal", kanal_id)]
    elif test_modu or onizleme:
        hedefler = [("admin", admin_id)]
    else:
        hedefler = [("kanal", kanal_id)]

    for ad, hid in hedefler:
        if not hid:
            degisken = "TELEGRAM_ADMIN_CHAT_ID" if ad == "admin" else "TELEGRAM_CHAT_ID"
            print(f"HATA: {degisken} tanımlı değil.", file=sys.stderr)
            sys.exit(1)

    # Nöbetçi koruması: rapor bugün zaten gönderildiyse boşuna çalışma.
    if not test_modu and not onizleme and bugun_gonderildi_mi():
        print("[bilgi] Rapor bugün zaten gönderilmiş — nöbetçi çıkıyor.", file=sys.stderr)
        return

    # SABİT TESLİM SAATİ: DELIVER_AT_TR (ör. "08:00") tanımlıysa rapor HER GÜN tam bu
    # saatte gider. GitHub cron erken tetikler; biz tam saate kadar bekleriz. Böylece
    # tetikleme kaysa bile teslim saati sabit kalır (alışkanlık için).
    teslim = os.environ.get("DELIVER_AT_TR", "").strip()
    zamanli = bool(teslim)
    hedef_dt = _hedef_zaman_ist(teslim) if zamanli else None

    try:
        # Veri taze kalsın diye üretimi teslim saatinden hemen önce başlat
        if zamanli:
            uret_penceresi = datetime.fromtimestamp(
                hedef_dt.timestamp() - GONDERIM_URETIM_BUTCESI, IST)
            _bekle_kadar(uret_penceresi, "Üretim penceresine")

        print("[bilgi] Adım 1: Piyasa verileri çekiliyor...", file=sys.stderr)
        market_data, veri = piyasa_verilerini_cek()

        print("[bilgi] Adım 2: Rapor üretiliyor...", file=sys.stderr)
        dun_takip = dunku_takip_oku()
        rapor = rapor_uret(market_data, "; ".join(dun_takip))
        rapor, bugun_takip = takip_ayikla(rapor)

        if test_modu:
            rapor = "🧪 <b>[TEST]</b>" + chr(10) * 2 + rapor

        # Tam teslim saatine kadar bekle (dakikası dakikasına gönderim)
        if zamanli:
            _bekle_kadar(hedef_dt, f"Teslim saati {teslim} TSİ'ye")

        print("[bilgi] Adım 3: Telegram'a gönderiliyor...", file=sys.stderr)

        # Rapor bölümleri: brief (60 SANİYE) + detay(lar)
        bolumler = [b.strip() for b in rapor.split("---DETAY---") if b.strip()]
        brief = bolumler[0] if bolumler else rapor
        detaylar = bolumler[1:]

        # Kartı bir kez üret (best-effort — hata olsa rapor yine gider)
        png = None
        try:
            import kart
            hava, risk = _brief_ayikla(rapor)
            png = kart.kart_olustur(veri, hava, risk, tarih_basligi())
        except Exception as kart_hata:               # noqa: BLE001
            print(f"[uyarı] Kart oluşturulamadı: {kart_hata}", file=sys.stderr)

        for ad, hid in hedefler:
            # 1) İLK MESAJ = kart + brief (görsel ilk mesaja bağlı). Kart yoksa brief metin.
            if png is not None and len(brief) <= 1024:
                foto_gonder(bot_token, hid, png, caption=brief)
            else:
                raporu_yolla(bot_token, hid, brief)
                if png is not None:
                    foto_gonder(bot_token, hid, png)
            # 2) Detay mesaj(lar)ı
            for detay in detaylar:
                time.sleep(1)
                raporu_yolla(bot_token, hid, detay)
            print(f"[başarılı] '{ad}' hedefine gönderildi (kart + brief + detay).", file=sys.stderr)

        # Bugünün takip listesini yarın için kaydet (test modunda kaydetme)
        if not test_modu and not onizleme:
            try:
                takip_yaz(bugun_takip, veri.get("_kaydedilecek"))
                print(f"[bilgi] {len(bugun_takip)} takip maddesi kaydedildi.", file=sys.stderr)
            except Exception as se:                   # noqa: BLE001
                print(f"[uyarı] Takip kaydedilemedi: {se}", file=sys.stderr)

    except Exception as e:                           # noqa: BLE001
        print(f"[HATA] {_gizle(e)}", file=sys.stderr)
        admin_hata_bildir(_gizle(e))
        sys.exit(1)


if __name__ == "__main__":
    main()


# ──────────────────────────────────────────────────────────────────
#  🌅 Günaydın Kripto — topluluğa ÜCRETSİZ sunulmuştur.
#  Hazırlayan / imza: DogukanLive · youtube.com/@DogukanLive
#  Bu imzayı koru: aracın kökeni buradan bilinir.
# ──────────────────────────────────────────────────────────────────
