# Claude Code için Kurulum Rehberi (Otomatik Onboarding)

Bu dosya, bu repoyu **Claude Code**'da açan yeni bir kullanıcıyı sıfırdan kuruluma yönlendirmen içindir. Kullanıcı büyük ihtimalle teknik değil — sabırlı, adım adım ve Türkçe ilerle. Amacın: kullanıcının her sabah 08:00'de (TSİ) Telegram kanalına/grubuna otomatik altın/emtia raporu göndermesini sağlamak.

## Bu proje nedir?

Her sabah GitHub Actions cron ile çalışan bir bot. `report.py` üç adımda çalışır:
1. **Sabit veriler** — gold-api.com (ons altın + gümüş, USD) ve frankfurter.dev (USD/TRY, EUR/TRY) ücretsiz API'lerinden fiyatlar (LLM yok, halüsinasyon yok). 24s değişim, dünün kapanışının `state/takip.json`'da saklanmasıyla hesaplanır.
2. **Haber & analiz** — headless `claude -p` web araması yaparak raporu yazar (Anthropic API key YOK; `CLAUDE_CODE_OAUTH_TOKEN` ile abonelikten).
3. **Gönderim** — Telegram'a HTML formatında, 4096 karakter bölünerek.

Bilgisayar kapalıyken de çalışsın diye zamanlama buluttadır (GitHub Actions), yerel değil.

## Kurulumu şu sırayla yaptır

Kullanıcıya her adımda ne yapacağını net söyle. Mümkün olan her şeyi **sen** yap; sadece kullanıcının hesabına tarayıcı girişi gerektiren adımları ona bırak.

### 1. Telegram botu
Kullanıcıyı yönlendir: Telegram'da **@BotFather** → `/newbot` → isim ver → **token'ı** sana versin. Token'ı `getMe` ile doğrula.

### 2. Kanal/grup + chat_id'ler (SEN otomatik bul)
- Kullanıcı raporun gideceği kanalı/grubu oluştursun, botu **admin** yapsın (mesaj gönderme yetkisi), kanala bir mesaj atsın.
- Kullanıcı botuna Telegram'da **/start** yazsın (bot ona özel mesaj atabilsin diye).
- Sen `https://api.telegram.org/bot<TOKEN>/getUpdates`'i oku, kanal id'sini (channel/supergroup) ve admin id'sini (private) **otomatik çıkar**. Kullanıcıya JSON okutma.
- Her ikisine `sendMessage` ile test at, çalıştığını kanıtla.
- **Not:** getUpdates boşsa, kullanıcı mesajı botu admin yaptıktan SONRA atmamış olabilir — yeni mesaj attır, tekrar oku.

### 3. Claude token
Kullanıcı kendi terminalinde `claude setup-token` çalıştırsın (tarayıcı girişi — SEN yapamazsın), çıkan `sk-ant-oat...` token'ını sana versin. Bu, bulutta kimlik doğrulama için ŞART (sunucuda yerel oturum yoktur).

### 4. .env yaz
`.env.example`'ı örnek al, dört değeri (`CLAUDE_CODE_OAUTH_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_ADMIN_CHAT_ID`) yaz. `.env` gitignore'ludur, ASLA commit'leme.

### 5. Yerel test
`pip install -r requirements.txt` sonra `python report.py --test` — rapor kanala DEĞİL admin'e gider. Çalıştığını göster.

### 6. GitHub (SEN kur)
- `gh` CLI yoksa kur (Windows: `winget install --id GitHub.cli`).
- `gh auth login` — kullanıcının hesabına giriş. Headless/uzak makinedeyse **device-flow** kullan: `gh auth login --hostname github.com --git-protocol https --web` çalıştır, çıkan **tek kullanımlık kodu** ve `https://github.com/login/device` linkini kullanıcıya ver, o tarayıcıda girsin.
- Workflow dosyası push edileceği için token'da **`workflow` scope** gerekir. Yoksa `gh auth refresh --hostname github.com --scopes workflow` (yine device kod).
- `gh` git kimlik yardımcısını kur: `gh auth setup-git` (yoksa git push kimlik sorup takılır).
- Commit → `gh repo create <ad> --private --source=. --push`.
- 4 secret'ı ekle: `gh secret set <AD> --body "<deger>"` (koda gömme; Secrets şifrelidir).

### 7. Bulut testi
`gh workflow run daily-report.yml -f mode=test` ile Actions'ı test modunda tetikle, `gh run watch <id> --exit-status` ile bitişini izle. Yeşilse: token bulutta çalışıyor + Telegram gönderimi tamam demektir. Kullanıcıya "her sabah 08:00'de hazır" de.

## Kritik kurallar
- **Gizli anahtarları asla koda/commite yazma.** Sadece `.env` (gitignore) ve GitHub Secrets.
- Herkese açık yapma gibi geri dönüşü zor işlemleri kullanıcı onayı olmadan yapma.
- Windows'ta `subprocess` claude'u `claude.cmd` ile çağırmalı (uzantısız bash wrapper çalışmaz) — `report.py` bunu zaten halleder.
- Token'lar bir kez sohbete girdiyse, kurulum sonunda yenilemeyi öner (BotFather `/revoke`, `claude setup-token`).

## Kısa yol
Adım 1-6'nın çoğunu tek komutta toplayan bir sihirbaz var: **`python setup.py`**. Kullanıcı takılırsa ona bunu çalıştırmasını söyle; chat_id keşfi, test, `.env` ve GitHub otomasyonunu kendisi yapar.
