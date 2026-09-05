# -*- coding: utf-8 -*-
"""Telegram'dan gelen `/teknik <ZAMAN_DİLİMİ>` komutlarını işler.

Ayrı bir GitHub Actions cron'u (teknik-komut.yml) ile birkaç dakikada bir
çalıştırılır — daily-report.yml'den bağımsızdır. Şu an sadece ADMIN'in
botla özel sohbetindeki mesajları dinler (kanal/grup değil).

state/komut_takip.json'da işlenen son Telegram update_id saklanır, aynı
mesaj iki kez işlenmez (Telegram'ın standart long-polling offset yöntemi).

Desteklenen zaman dilimleri: M1, M5, M15, M30, H1, H4 (teknik.TIMEFRAMES).
Örnek: "/teknik", "/teknik M15", "/teknik h4"
"""
import json
import os
import re
import sys

import requests

import teknik
from report import foto_gonder, _gizle

STATE_YOL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "komut_takip.json")
KOMUT_DESENI = re.compile(
    r"^/teknik(?:@\w+)?\s*(" + "|".join(teknik.TIMEFRAMES) + r")?\s*$", re.IGNORECASE
)


def _son_update_id_oku():
    try:
        with open(STATE_YOL, encoding="utf-8") as f:
            return json.load(f).get("son_update_id", 0)
    except (FileNotFoundError, ValueError, OSError):
        return 0


def _son_update_id_yaz(update_id):
    os.makedirs(os.path.dirname(STATE_YOL), exist_ok=True)
    with open(STATE_YOL, "w", encoding="utf-8") as f:
        json.dump({"son_update_id": update_id}, f, ensure_ascii=False, indent=2)


def _teknik_gonder(bot_token, chat_id, zaman_dilimi):
    mumlar, analiz = teknik.analiz_uret(zaman_dilimi)
    png = teknik.grafik_olustur(mumlar, analiz)
    yon = "BUY" if analiz["yon"].startswith("LONG") else "SELL"
    altyazi = (
        f"📐 <b>{analiz['durum']}</b> — XAUUSD {zaman_dilimi} {yon} senaryosu "
        f"(Güven %{analiz['guven']})\n"
        f"Giriş {analiz['giris']:,.2f} · SL {analiz['sl']:,.2f}\n"
        f"TP1 {analiz['tp1']:,.2f} · TP2 {analiz['tp2']:,.2f} · TP3 {analiz['tp3']:,.2f}\n"
        "<i>Yatırım tavsiyesi değildir — sadece teknik/eğitim amaçlıdır.</i>"
    )
    foto_gonder(bot_token, chat_id, png, caption=altyazi)


def calistir():
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    admin_id = str(os.environ["TELEGRAM_ADMIN_CHAT_ID"])

    son_id = _son_update_id_oku()
    r = requests.get(
        f"https://api.telegram.org/bot{bot_token}/getUpdates",
        params={"offset": son_id + 1, "timeout": 0},
        timeout=20,
    )
    r.raise_for_status()
    cevap = r.json()
    if not cevap.get("ok"):
        raise RuntimeError(f"getUpdates hatası: {cevap.get('description')}")
    guncellemeler = cevap.get("result", [])

    if not guncellemeler:
        print("[bilgi] Yeni mesaj yok.")
        return

    en_son_id = son_id
    for g in guncellemeler:
        en_son_id = max(en_son_id, g["update_id"])
        mesaj = g.get("message")
        if not mesaj:
            continue
        chat_id = str(mesaj.get("chat", {}).get("id"))
        metin = (mesaj.get("text") or "").strip()
        if chat_id != admin_id:
            continue  # şimdilik sadece admin'in özel sohbeti dinleniyor
        eslesme = KOMUT_DESENI.match(metin)
        if not eslesme:
            continue
        zaman_dilimi = (eslesme.group(1) or teknik.VARSAYILAN_ZD).upper()
        print(f"[bilgi] Komut alındı: /teknik {zaman_dilimi}")
        try:
            _teknik_gonder(bot_token, chat_id, zaman_dilimi)
            print(f"[başarılı] {zaman_dilimi} grafiği gönderildi.")
        except Exception as e:                        # noqa: BLE001
            print(f"[HATA] Teknik grafik üretilemedi: {_gizle(e)}", file=sys.stderr)

    _son_update_id_yaz(en_son_id)


if __name__ == "__main__":
    calistir()
