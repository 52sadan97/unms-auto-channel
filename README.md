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

## 📦 Portainer ile Kurulum (Önerilen)

Projeyi çok kolay bir şekilde **Portainer Stacks (Yığınlar)** özelliği ile kurabilirsiniz. 

1. Portainer panelinize giriş yapın.
2. Sol menüden **Stacks** sekmesine tıklayın ve **Add stack** butonuna basın.
3. Stack metodunu **Repository** olarak seçin veya **Web editor** kısmına aşağıda bulunan standard `docker-compose.yml` dosyasını yapıştırın:

```yaml
services:
  unms-auto-channel:
    build: .
    container_name: unms-auto-channel
    restart: unless-stopped
    volumes:
      - ./config:/app/config
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
```

### 🗝️ Değişkenleri Ayarlama
Stack ayarınızı oluştururken, yukarıdaki `environment:` bloklarına kendi özel bilgilerinizi giriniz:
- `UISP_TOKEN`: UISP sisteminden aldığınız yönetici token'ı.
- `TELEGRAM_BOT_TOKEN`: BotFather üzerinden aldığınız bot token.
- `TELEGRAM_ADMIN_CHAT_ID`: Kendi kişisel Telegram ID'niz (Bot üzerinden kimlik yetkilendirmesi için gereklidir).

4. En alttan **Deploy the stack** diyerek sistemi başlatın.

Sistem ayağa kalktığında Telegram botunuza gidip `/start` yazmanız yeterlidir! Bot, sizin yetkinizi tanıyarak menüyü önünüze açacaktır.

## 🛠️ Manuel Docker-Compose Kurulumu
Eğer arayüzsüz bir Linux makinesindeyseniz, SSH üzerinden repo'yu klonlayıp çalıştırabilirsiniz:
```bash
git clone https://github.com/KULLANICI_ADINIZ/unms-auto-channel.git
cd unms-auto-channel
# docker-compose.yml dosyasındaki tokenları nano komutu ile düzenleyin:
nano docker-compose.yml 
# Sonra başlatın
docker-compose up -d --build
```
Logları canlı görmek için: 
`docker-compose logs -f`

## 🗂️ Yapılandırma ve Config (config.ini)
Sistem çalışmaya başladığında `/config` hacim (volume) dizini altında `config.ini` adında bir ayar dosyası oluşacaktır. Bot üzerinden de yönetebileceğiniz bu ayarlara, isterseniz doğrudan müdahale edebilirsiniz. Bot ilk açılışta erişebildiği antenleri algılayıp dosyaya örnek formatta sunar.
