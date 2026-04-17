# UISP Auto-Channel Manager 🚀

Bu proje, **UISP (UNMS)** üzerinde bulunan Access Point cihazlarının frekanslarını otomatik ve akıllı bir şekilde değiştirmeyi sağlayan bir otomasyon botudur. Aynı zamanda gelişmiş bir Telegram entegrasyonuna sahiptir. Ağınızdaki antenlerin parazit durumlarına göre otomatik olarak frekans değiştirmesini, cihazlarınızın çevrimiçi/çevrimdışı (online/offline) durumlarının anlık olarak izlenmesini sağlar.

## 🌟 Özellikler
- **Otomatik Frekans Değişimi:** Cihazların frekanslarını, belirttiğiniz listeye göre sırayla veya rastgele periyotlarla değiştirir.
- **Telegram Bot Entegrasyonu:** Tümüyle Telegram üzerinden yönetim sağlar.
  - Cihazları ve API durumunu Telegram üzerinden kontrol etme.
  - Anlık aktif/pasif bildirimleri alma (Cihaz koptuğunda/geldiğinde ping bilgisi).
  - Telegram chat üzerinden gelişmiş menülerle frekans veya config değiştirme.
- **Güvenli & Konteynerize:** Docker ve Portainer ile tek tıkla kurulum yapılabilir formda.
- **Akıllı Hata Yönetimi:** Ağ kesintilerinde veya API cevap vermediğinde askıda kalmaz, süreci kendi kendine hatasız sürdürür.

## 📦 Portainer ile Kurulum (Web Editor)

Projeyi doğrudan **Portainer Stacks (Yığınlar)** özelliği ile tek tıkla kurabilirsiniz. Projemiz **Github Container Registry (GHCR)** üzerinde güvenle yayınlanmış hazır ve derlenmiş bir Docker imajına sahiptir.

1. Portainer panelinize giriş yapın.
2. Sol menüden **Stacks** sekmesine tıklayın ve **Add stack** butonuna basın.
3. İçeriği doğrudan **Web editor** kısmına yapıştırarak şu `docker-compose.yml` kodunu kullanın:

```yaml
version: '3.8'
services:
  unms-auto-channel:
    image: ghcr.io/52sadan97/unms-auto-channel:latest
    container_name: unms-auto-channel
    restart: unless-stopped
    volumes:
      # Alt satırdaki /root/unms-bot/config klasörünüz eski yedekleriniz için sabittir!
      - /root/unms-bot/config:/app/config
    environment:
      - TZ=Europe/Istanbul
      - UISP_TOKEN=your_uisp_token_here
      - TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
      - TELEGRAM_ADMIN_CHAT_ID=your_chat_id_here
      - LOG_LEVEL=INFO
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"

  # Opsiyonel: İzleme kulesi ile Github'da kod güncellendiğinde botun kendini güncellemesini saglar
  watchtower:
    image: containrrr/watchtower
    container_name: watchtower-unms
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    environment:
      - WATCHTOWER_CLEANUP=true
      - WATCHTOWER_POLL_INTERVAL=120
    restart: always
```

### 🗝️ Değişkenleri Ayarlama (Şifreleriniz)
Kodu Web Editor'e yapıştırdıktan sonra lütfen aşağıdaki satırları bulup kendi özel değerlerinizle değiştirin:
- `UISP_TOKEN`: UISP panelinden aldığınız API yetkilendirme şifreniz.
- `TELEGRAM_BOT_TOKEN`: BotFather üzerinden aldığınız bot tokenı.
- `TELEGRAM_ADMIN_CHAT_ID`: Telegram chat id numaranız.

4. Tüm ayarlar tamamsa, en alttan **Deploy the stack** butonuna basarak sistemi başlatın. Sistem ilk yönergelerde GHCR üzerinden imajı çekecek ve botunuz ayaklanıp Telegram da canlanacaktır!

## 🛠️ Manuel Terminal Kurulumu
Eğer arayüzsüz bir Linux makinesindeyseniz, artık repo klonlamanıza bile gerek yok! Sadece şu komutu girerek imajı direkt başlatabilirsiniz:

```bash
# İlk olarak gerekli config depolama klasörünü oluşturalım:
mkdir -p /root/unms-bot/config

# Tek satırlık komutla sistemi başlatın (Yıldızlı yerlere şifrelerinizi yazın):
docker run -d \
  --name unms-auto-channel \
  --restart unless-stopped \
  -v /root/unms-bot/config:/app/config \
  -e TZ=Europe/Istanbul \
  -e UISP_TOKEN=*** \
  -e TELEGRAM_BOT_TOKEN=*** \
  -e TELEGRAM_ADMIN_CHAT_ID=*** \
  ghcr.io/52sadan97/unms-auto-channel:latest
```
Logları canlı izlemek için: `docker logs -f unms-auto-channel`

## 🗂️ Yapılandırma ve Config (config.ini)
Sistem çalışmaya başladığında `/config` hacim (volume) dizini altında `config.ini` adında bir ayar dosyası oluşacaktır. Bot üzerinden de yönetebileceğiniz bu ayarlara, isterseniz doğrudan müdahale edebilirsiniz. Bot ilk açılışta erişebildiği antenleri algılayıp dosyaya örnek formatta sunar.
