# -*- coding: utf-8 -*-
"""Gün içi haber kontrolü: her birkaç saatte bir (haber-kontrol.yml cron'u)
çalışır, altın/emtia piyasasını etkileyebilecek GERÇEKTEN yeni ve önemli bir
gelişme olup olmadığını headless Claude Code ile araştırır.

Önemli bir şey yoksa TAMAMEN SESSİZ kalır — hiçbir mesaj gönderilmez, sadece
log'a "yeni bir şey yok" yazar. Böylece kanal gereksiz yere spam'lenmez.
Önceki bildirilen gelişmeler state/haber_takip.json'da saklanır ve modele
"bunları tekrar bildirme" diye verilir.

İKİ AŞAMALI YAKLAŞIM (denenip elenen tek-çağrılık yöntemlerden sonra):
  1) Araştırma çağrısı — WebSearch açık, model SERBESTÇE analiz yazar
     (kısıt yok; model bunu güvenilir yapıyor, RAPOR_PROMPTU'ndaki gibi).
  2) Sınıflandırma çağrısı — araç yok, --append-system-prompt ile katı
     format zorlanır (kullanıcı promptunda format istemek modeli sohbete
     sürüklüyordu; sistem promptu çok daha güvenilir çalıştı).

ÖNEMLİ (Windows): claude.cmd bir batch dosyası olduğu için, prompt/sistem
promptu içinde `|` (pipe) geçerse Windows'un cmd.exe'si bunu komut ayracı
sanıp argümanı bozuyor — model "içerik eksik geldi" gibi anlamsız yanıtlar
veriyordu. Bu yüzden alan ayracı olarak `|` DEĞİL `::` kullanılıyor.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

from report import telegram_gonder, admin_hata_bildir, _gizle

IST = ZoneInfo("Europe/Istanbul")
STATE_YOL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "haber_takip.json")
CLAUDE_TIMEOUT = 300
MAX_TAKIP = 12  # geçmişte en fazla bu kadar başlık saklanır (prompt şişmesin diye)

ARASTIRMA_PROMPTU = """Sen altın/emtia piyasasını gün içinde saatlik takip eden bir analistsin. Hemen web araması yaparak SON BİRKAÇ SAATTE piyasayı etkileyebilecek GERÇEKTEN yeni ve önemli bir gelişme olup olmadığını araştır (şu an: {tarih_saat} TSİ). Piyasa kapalıysa (hafta sonu/tatil) ya da yeni bir şey bulamazsan bunu da bir sonuç olarak say.

DAHA ÖNCE BİLDİRİLEN GELİŞMELER (bunları TEKRAR bildirme; sadece bunlardan gerçekten FARKLI, YENİ bir şey varsa bildir):
{onceki_gelismeler}

Bulgularını serbestçe, istediğin uzunlukta analiz et ve yaz. Kaynak linklerini mutlaka belirt."""

SINIFLANDIRMA_SISTEM_PROMPTU = (
    "Sen bir sınıflandırma motorusun, sohbet etmezsin, soru sormazsın. Kullanıcı "
    "sana bir piyasa araştırma bulgusu ve önem ölçütü verecek; bulguyu SORGULAMA/"
    "DOĞRULAMA, zaten araştırılmış kabul et. Cevabın HER ZAMAN, istisnasız, ya tam "
    "olarak 'HAYIR' ya da 'EVET::başlık::özet::url' formatında TEK SATIR olmalı. "
    "Başka HİÇBİR şey yazma — açıklama yok, markdown yok, soru yok."
)

SINIFLANDIRMA_PROMPTU = """Bulgu: {bulgu}

Daha önce bildirilenler (bunlardan farklı değilse önemli sayma): {onceki_gelismeler}

Önemli sayılacak: Fed/merkez bankası açıklaması veya kararı, beklenmedik ekonomik veri (enflasyon, istihdam, faiz), önemli jeopolitik gelişme, ani/sert fiyat hareketi ve nedeni.
Önemli sayılmayacak: piyasa kapalı/hareket yok bilgisi, rutin/küçük haberler, zaten bilinen bir konunun tekrarı, yorum/spekülasyon.

Bu bulgu önemli/yeni midir?"""


def _son_gelismeler_oku():
    try:
        with open(STATE_YOL, encoding="utf-8") as f:
            veri = json.load(f)
        gelismeler = veri.get("gelismeler", [])
        return gelismeler if isinstance(gelismeler, list) else []
    except (FileNotFoundError, ValueError, OSError):
        return []


def _gelismeler_yaz(gelismeler):
    os.makedirs(os.path.dirname(STATE_YOL), exist_ok=True)
    with open(STATE_YOL, "w", encoding="utf-8") as f:
        json.dump({"gelismeler": gelismeler[-MAX_TAKIP:]}, f, ensure_ascii=False, indent=2)


def _satir_ayikla(metin):
    """Sınıflandırma çağrısının çıktısından HAYIR ya da EVET::... satırını
    bulur (model önüne/arkasına yine de bir şeyler eklemiş olabilir, o
    yüzden tüm satırları tarayıp uyan ilkini alıyoruz)."""
    for satir in metin.splitlines():
        s = satir.strip().strip("*").strip()
        if s.upper() == "HAYIR":
            return {"yeni_gelisme_var": False}
        if s.upper().startswith("EVET::"):
            parcalar = [p.strip() for p in s.split("::")]
            if len(parcalar) >= 3:
                return {
                    "yeni_gelisme_var": True,
                    "baslik": parcalar[1],
                    "ozet": parcalar[2],
                    "kaynak_url": parcalar[3] if len(parcalar) > 3 else "",
                }
    raise ValueError(f"Geçerli HAYIR/EVET satırı bulunamadı: {metin[:300]!r}")


def _claude_sor(prompt, arama_izinli=True, sistem_prompt=None):
    """report.py'deki rapor_uret ile aynı desen: izole dizin, doğru argüman
    sırası (flag'ler -p'den ÖNCE — aksi halde WebSearch izni garip şekilde
    reddedilebiliyor, bkz. report.py yorumları)."""
    adaylar = ["claude.cmd", "claude.exe", "claude"] if os.name == "nt" else ["claude"]
    claude_bin = next((shutil.which(a) for a in adaylar if shutil.which(a)), None)
    if not claude_bin:
        raise RuntimeError("'claude' komutu bulunamadı. Kurulum: npm install -g @anthropic-ai/claude-code")

    izole_dizin = tempfile.mkdtemp(prefix="haber-kontrol-")
    komut = [claude_bin]
    if arama_izinli:
        komut += ["--allowedTools", "WebSearch", "WebFetch"]
    if sistem_prompt:
        komut += ["--append-system-prompt", sistem_prompt]
    komut += ["--output-format", "text", "-p", prompt]
    try:
        sonuc = subprocess.run(
            komut, capture_output=True, text=True,
            encoding="utf-8", timeout=CLAUDE_TIMEOUT, cwd=izole_dizin,
        )
    finally:
        shutil.rmtree(izole_dizin, ignore_errors=True)

    if sonuc.returncode != 0:
        raise RuntimeError(f"claude kod {sonuc.returncode}: {(sonuc.stderr or sonuc.stdout)[:500]}")
    return sonuc.stdout.strip()


def _siniflandir(bulgu, onceki_str):
    prompt = SINIFLANDIRMA_PROMPTU.format(bulgu=bulgu, onceki_gelismeler=onceki_str)
    cikti = _claude_sor(prompt, arama_izinli=False, sistem_prompt=SINIFLANDIRMA_SISTEM_PROMPTU)
    return _satir_ayikla(cikti)


def calistir():
    onceki = _son_gelismeler_oku()
    onceki_str = "\n".join(f"- {g}" for g in onceki) if onceki else "(yok — ilk kontrol)"
    tarih_saat = datetime.now(IST).strftime("%d.%m.%Y %H:%M")

    # 1. adım: serbestçe araştır (format kısıtı yok — model bunu güvenilir yapıyor)
    arastirma_prompt = ARASTIRMA_PROMPTU.format(tarih_saat=tarih_saat, onceki_gelismeler=onceki_str)
    bulgu = _claude_sor(arastirma_prompt, arama_izinli=True)

    # 2. adım: bulguyu kesin formata sınıflandır (araç yok, sadece metin sınıflandırma)
    try:
        veri = _siniflandir(bulgu, onceki_str)
    except ValueError:
        print("[uyarı] Sınıflandırma çıktısı beklenen formatta değil, tekrar deneniyor.")
        try:
            veri = _siniflandir(bulgu, onceki_str)
        except (ValueError, Exception) as e2:          # noqa: BLE001
            # Bu sessiz/arka plan bir kontrol — ikinci denemede de olmazsa
            # sistemi "hata" sayıp admin'i rahatsız etmek yerine bu turu
            # sessizce atlıyoruz.
            print(f"[uyarı] İkinci deneme de başarısız, bu tur atlanıyor: {_gizle(e2)}")
            return

    if not veri.get("yeni_gelisme_var"):
        print("[bilgi] Yeni/önemli bir gelişme yok, sessiz geçiliyor.")
        return

    baslik = str(veri.get("baslik", "")).strip()
    ozet = str(veri.get("ozet", "")).strip()
    kaynak = str(veri.get("kaynak_url", "")).strip()
    if not baslik or not ozet:
        print(f"[uyarı] Model 'yeni_gelisme_var: true' dedi ama başlık/özet eksik, atlanıyor: {veri}")
        return

    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    metin = (
        f"🔔 <b>GÜN İÇİ GELİŞME</b> — {tarih_saat} TSİ\n\n"
        f"<b>{baslik}</b>\n{ozet}"
    )
    if kaynak:
        metin += f'\n\n<a href="{kaynak}">Kaynak</a>'
    telegram_gonder(bot_token, chat_id, metin)
    print(f"[başarılı] Gönderildi: {baslik}")

    onceki.append(baslik)
    _gelismeler_yaz(onceki)


if __name__ == "__main__":
    try:
        calistir()
    except Exception as e:                                # noqa: BLE001
        print(f"[HATA] {_gizle(e)}", file=sys.stderr)
        admin_hata_bildir(f"haber_kontrol.py hatası: {_gizle(e)}")
        sys.exit(1)
