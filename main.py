import configparser
import json
import logging
import re
import time
import random
from datetime import datetime, timezone
import socket
import shutil
import sys

import os
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackContext, CallbackQueryHandler, MessageHandler, filters, JobQueue
import asyncio
from threading import Thread

def get_api_credentials(config):
    """
    API kimlik bilgilerini alır. Güvenlik için token'ı ortam değişkenlerinden öncelikli olarak okur.
    (hostname, token, verify_ssl) tuple'ı döndürür.
    Kimlik bilgileri bulunamazsa (None, None, None) döndürür.
    """
    try:
        hostname = config.get('unms', 'hostname')
        verify_ssl = config.getboolean('unms', 'verify_ssl', fallback=True)
        # Token için ortam değişkenini önceliklendir
        token = os.getenv('UISP_TOKEN')
        if not token:
            # Ortam değişkeni ayarlı değilse yapılandırma dosyasına geri dön
            token = config.get('unms', 'token', fallback=None)

        if not token:
            logging.error("API token bulunamadı. 'UISP_TOKEN' ortam değişkenini ayarlayın veya config.ini dosyasındaki [unms] bölümüne 'token' ekleyin.")
            return None, None, None

        return hostname, token, verify_ssl
    except configparser.NoSectionError:
        logging.error("config.ini dosyasında '[unms]' bölümü bulunamadı.")
        return None, None, None

def build_api_url(config, *path_segments):
    """
    Constructs the full API URL from the base and path segments.
    """
    hostname, _, _ = get_api_credentials(config)
    if not hostname:
        return None

    # UISP Cloud için standart API yolu /nms/api/v2.1'dir.
    api_path = "/nms/api/v2.1"
    
    # URL'yi oluştur
    full_path = "/".join([api_path.strip('/')] + [str(s) for s in path_segments])
    
    return f"https://{hostname.strip('/')}/{full_path}"

def get_all_devices(config):
    """UISP'ten tüm cihazların listesini çeker."""
    hostname, token, verify_ssl = get_api_credentials(config)
    if not token:
        return None

    url = build_api_url(config, 'devices')
    headers = {"x-auth-token": token}

    try:
        response = requests.get(url, headers=headers, verify=verify_ssl, timeout=15)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logging.error(f"Tüm cihazlar alınamadı: {e}")
        return None


def get_device_details(config, device_id):
    """Tek bir cihazın tüm detaylarını çeker."""
    hostname, token, verify_ssl = get_api_credentials(config)
    if not token:
        return None

    url = build_api_url(config, 'devices', device_id)
    headers = {"x-auth-token": token}

    try:
        response = requests.get(url, headers=headers, verify=verify_ssl, timeout=15)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logging.error(f"{device_id} ID'li cihazın detayları alınamadı: {e}")
        return None


def get_available_frequencies(config, device_id):
    """Bir cihaz için mevcut frekansların listesini API'den çeker."""
    hostname, token, verify_ssl = get_api_credentials(config)
    if not token:
        return None

    # Yöntem 1: Standart /frequencies uç noktasını dene
    primary_url = build_api_url(config, 'devices', device_id, 'frequencies')
    headers = {"x-auth-token": token}

    try:
        response = requests.get(primary_url, headers=headers, verify=verify_ssl, timeout=15)
        response.raise_for_status()
        frequency_data = response.json()
        frequencies = [item['center'] for item in frequency_data if 'center' in item]
        logging.info(f"{device_id} ID'li cihaz için birincil uç noktadan {len(frequencies)} adet frekans çekildi.")
        return frequencies if frequencies else None # Boş liste yerine None döndür
    except requests.exceptions.RequestException as primary_error:
        if primary_error.response is not None and primary_error.response.status_code == 404:
            logging.warning(f"{device_id} için birincil uç nokta /frequencies başarısız oldu (404). Yedek /configuration uç noktası deneniyor.")
        else:
            logging.warning(f"{device_id} için birincil uç nokta /frequencies başarısız oldu: {primary_error}. Yedek /configuration uç noktası deneniyor.")

        # Yöntem 2: /frequencies başarısız olursa yapılandırmayı okuyarak scanlist'ten al
        config_data = get_device_configuration(config, device_id)
        if config_data and 'wireless' in config_data:
            scanlist = []
            if 'interfaces' in config_data['wireless'] and len(config_data['wireless']['interfaces']) > 0:
                scanlist = config_data['wireless']['interfaces'][0].get('frequency', {}).get('scanlist', {}).get('freq', [])
            else:
                scanlist = config_data['wireless'].get('frequency', {}).get('scanlist', {}).get('freq', [])
                
            if scanlist:
                logging.info(f"{device_id} ID'li cihaz için yapılandırmadan {len(scanlist)} adet frekans çekildi.")
                return scanlist
            else:
                logging.warning(f"{device_id} için yapılandırmada scanlist bulunamadı.")
                return None
        return None
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logging.error(f"{device_id} ID'li cihaz için frekans yanıtı ayrıştırılırken hata oluştu: {e}")
        return None


def get_device_configuration(config, device_id):
    """Tek bir cihazın tam yapılandırmasını çeker."""
    hostname, token, verify_ssl = get_api_credentials(config)
    if not token:
        return None

    # The endpoint for configuration seems to be /devices/airos/{id}/configuration
    url = build_api_url(config, 'devices', 'airos', device_id, 'configuration')
    headers = {"x-auth-token": token}

    try:
        response = requests.get(url, headers=headers, verify=verify_ssl, timeout=15)
        response.raise_for_status()
        config_data = response.json()
        logging.debug(f"{device_id} ID'li cihazın tam yapılandırması: {json.dumps(config_data, indent=2)}")
        return config_data
    except requests.exceptions.RequestException as e:
        logging.error(f"{device_id} ID'li cihazın yapılandırması alınamadı: {e}")
        return None


def update_device_frequency(config, device_id, device_name, frequency, dry_run):
    """Tek bir cihazın frekansını günceller."""
    hostname, token, verify_ssl = get_api_credentials(config)
    
    if not token:
        return False # Kimlik bilgileri olmadan güncelleme yapılamaz
    
    # Adım 1: Mevcut tam yapılandırmayı al
    current_config = get_device_configuration(config, device_id)
    if not current_config:
        logging.error("Mevcut yapılandırma alınamadığı için güncelleme işlemine devam edilemiyor.")
        return False

    # Adım 2: Yapılandırma nesnesindeki frekansı değiştir
    try:
        # Frekans ayarını bulmak için birden fazla olası yolu deneyelim.
        # Yol 1: Modern AirOS (örn: LTU, Wave)
        if 'wireless' in current_config and 'interfaces' in current_config['wireless'] and current_config['wireless']['interfaces']:
            current_config['wireless']['interfaces'][0]['frequency']['tx'] = frequency
            logging.debug("Frekans yolu bulundu ve güncellendi: wireless.interfaces[0].frequency.tx")
        # Yol 2: Eski AirOS (örn: airMAX M)
        elif 'wireless' in current_config and 'frequency' in current_config['wireless']:
            current_config['wireless']['frequency'] = frequency
            logging.debug("Frekans yolu bulundu ve güncellendi: wireless.frequency")
        else:
            # Eğer iki yol da bulunamazsa, hata ver ve yapılandırmayı logla.
            raise KeyError("Cihaz yapılandırmasında geçerli bir frekans yolu bulunamadı.")
    except (KeyError, IndexError) as e:
        logging.error(f"Cihaz yapılandırma nesnesinde frekans ayarı bulunamadı: {e}")
        logging.error(f"--- {device_id} ID'li cihaz için yapılandırma nesnesinin başlangıcı ---")
        logging.error(json.dumps(current_config, indent=2))
        logging.error(f"--- {device_id} ID'li cihaz için yapılandırma nesnesinin sonu ---")
        return False

    # Adım 3: Değiştirilmiş yapılandırmanın tamamını PUT ile geri gönder
    url = build_api_url(config, 'devices', 'airos', device_id, 'configuration')
    headers = {"x-auth-token": token}
    # Gönderilecek veri, değiştirilmiş yapılandırma nesnesinin tamamıdır
    data = current_config
    logging.debug(f"{device_id} ID'li cihazı güncellemek için PUT isteğinin içeriği: {json.dumps(data, indent=2)}")

    if dry_run:
        logging.info(f"DRY RUN: {device_id} ID'li cihazın frekansı {frequency} MHz olarak değiştirilecekti.")
        return True

    try:
        response = requests.put(url, headers=headers, json=data, verify=verify_ssl, timeout=15)
        response.raise_for_status()
        success_message = f"✅ **Frekans Değiştirildi**\n\nCihaz: `{device_name}`\nYeni Frekans: `{frequency} MHz`"
        logging.info(f"{device_name} cihazının frekansı başarıyla {frequency} MHz olarak değiştirildi.")
        # Telegram bildirimi, işlem oradan başlatıldıysa bot yöneticisi tarafından gönderilecektir
        return True
    except requests.exceptions.RequestException as e:
        error_message = f"❌ **Frekans Değiştirme BAŞARISIZ**\n\nCihaz: `{device_name}`\nHata: `{e}`"
        logging.error(f"{device_name} cihazının frekansı değiştirilemedi: {e} - Yanıt: {e.response.text if e.response else 'N/A'}")
        # Telegram bildirimi, işlem oradan başlatıldıysa bot yöneticisi tarafından gönderilecektir
        return False


def reboot_device(config, device_id, device_name):
    """Tek bir cihaza yeniden başlatma komutu gönderir."""
    hostname, token, verify_ssl = get_api_credentials(config)
    if not token:
        return False

    # Per official API documentation, the endpoint is /restart
    url = build_api_url(config, 'devices', device_id, 'restart')
    headers = {"x-auth-token": token}

    logging.info(f"{device_name} ({device_id}) cihazına yeniden başlatma komutu gönderiliyor.")

    try:
        # Çoğu UISP/AirOS versiyonu için POST metodu çalışır.
        response = requests.post(url, headers=headers, verify=verify_ssl, timeout=15)
        response.raise_for_status()
        # API genellikle hemen 200 OK döner, asıl yeniden başlatma arka planda olur.
        logging.info(f"{device_name} cihazına POST metodu ile yeniden başlatma komutu başarıyla gönderildi.")
        return True
    except requests.exceptions.RequestException as post_error:
        # Eğer POST başarısız olursa (örn: 405 Method Not Allowed), PUT ile dene
        logging.warning(f"POST ile yeniden başlatma başarısız: {post_error}. PUT ile deneniyor...")
        try:
            response = requests.put(url, headers=headers, verify=verify_ssl, timeout=15)
            response.raise_for_status()
            logging.info(f"{device_name} cihazına PUT metodu ile yeniden başlatma komutu başarıyla gönderildi.")
            return True
        except requests.exceptions.RequestException as put_error:
            logging.error(f"Yeniden başlatma komutu hem POST hem de PUT ile gönderilemedi.")
            logging.error(f"POST hatası: {post_error}")
            if post_error.response is not None:
                logging.error(f"POST Yanıt içeriği: {post_error.response.text}")
            logging.error(f"PUT hatası: {put_error}")
            if put_error.response is not None:
                logging.error(f"PUT Yanıt içeriği: {put_error.response.text}")
            return False


def get_state(state_path):
    """Durum dosyasını okur."""
    if not os.path.exists(state_path):
        return {}
    try:
        with open(state_path, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return {}


def save_state(state_path, state):
    """Mevcut durumu dosyaya kaydeder."""
    with open(state_path, 'w') as f:
        json.dump(state, f, indent=2)

def prune_old_backups(backup_dir, pattern, retention_count):
    """
    Belirtilen klasördeki eski yedek dosyalarını temizler.
    En yeni `retention_count` adet dosyayı tutar.
    """
    try:
        # Dosyaları ve oluşturulma zamanlarını al
        backups = [
            (os.path.join(backup_dir, f), os.path.getmtime(os.path.join(backup_dir, f)))
            for f in os.listdir(backup_dir) if re.match(pattern, f)
        ]
        # Zamana göre sırala (en yeni en sonda)
        backups.sort(key=lambda x: x[1])

        # Silinecek dosyaları belirle
        files_to_delete = backups[:-retention_count]

        for file_path, _ in files_to_delete:
            os.remove(file_path)
            logging.info(f"Eski yedek temizlendi: {file_path}")

    except FileNotFoundError:
        logging.debug(f"{pattern} deseni için yedekleme dizini veya dosyaları bulunamadı. Temizlenecek bir şey yok.")
    except Exception as e:
        logging.error(f"Eski yedekler temizlenirken hata oluştu: {e}")

def create_backup(config_path, state_path, backup_dir, retention_count):
    """config.ini ve state.json dosyalarının zaman damgalı yedeklerini oluşturur ve eski yedekleri temizler."""
    if not os.path.exists(backup_dir):
        os.makedirs(backup_dir)
        logging.info(f"Yedekleme klasörü oluşturuldu: {backup_dir}")

    timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')

    # config.ini dosyasını yedekle
    if os.path.exists(config_path):
        backup_config_name = f"config_{timestamp}.ini.bak"
        backup_config_path = os.path.join(backup_dir, backup_config_name)
        shutil.copy2(config_path, backup_config_path)
        logging.info(f"'{config_path}' dosyası '{backup_config_path}' olarak yedeklendi.")

    # state.json dosyasını yedekle
    if os.path.exists(state_path):
        backup_state_name = f"state_{timestamp}.json.bak"
        backup_state_path = os.path.join(backup_dir, backup_state_name)
        shutil.copy2(state_path, backup_state_path)
        logging.info(f"'{state_path}' dosyası '{backup_state_path}' olarak yedeklendi.")

    # Eski yedekleri temizle
    prune_old_backups(backup_dir, r"config_.*\.ini\.bak", retention_count)
    prune_old_backups(backup_dir, r"state_.*\.json\.bak", retention_count)

def parse_time_interval(interval_str):
    """
    Zaman aralığı dizesini saniyeye çevirir.
    Desteklenen son ekler: 's' (saniye), 'm' (dakika), 'h' (saat).
    Son ek yoksa saniye olarak varsayılır.
    Örnekler: '1800', '1800s', '30m', '1h'
    """
    interval_str = interval_str.lower().strip()
    try:
        if interval_str.endswith('m'):
            return int(interval_str[:-1]) * 60
        elif interval_str.endswith('h'):
            return int(interval_str[:-1]) * 3600
        elif interval_str.endswith('s'):
            return int(interval_str[:-1])
        return int(interval_str)
    except ValueError:
        logging.warning(f"Geçersiz zaman aralığı formatı: '{interval_str}'. Varsayılan olarak 86400 saniye (24 saat) kullanılıyor.")
        return 86400


def is_time_in_window(allowed_hours_str):
    """
    Mevcut saatin izin verilen aralıkta olup olmadığını kontrol eder.
    Örnek: '02-05' veya '22-02' (gece yarısını aşan).
    """
    if not allowed_hours_str or not allowed_hours_str.strip():
        return True  # Kısıtlama yok, her zaman izinli.

    try:
        start_hour, end_hour = map(int, allowed_hours_str.split('-'))
        # docker-compose.yml dosyasındaki TZ ayarına göre yerel saati alır
        current_hour = datetime.now().hour

        if start_hour <= end_hour:
            # Normal aralık (örn: 02-05)
            return start_hour <= current_hour < end_hour
        else:
            # Gece yarısını aşan aralık (örn: 22-02)
            return current_hour >= start_hour or current_hour < end_hour
    except (ValueError, IndexError):
        logging.warning(f"Geçersiz 'allowed_hours' formatı: '{allowed_hours_str}'. HH-HH şeklinde olmalıdır. Kısıtlama yoksayılıyor.")
        return True


def process_device_task(config, config_path, task_name, state):
    """Yapılandırmadaki tek bir cihaz görevini işler."""
    logging.info(f"--- Görev işleniyor: {task_name} ---")

    try:
        if not config.getboolean(task_name, 'enabled', fallback=False):
            logging.info(f"'{task_name}' görevi devre dışı. Atlanıyor.")
            return

        device_id = config.get(task_name, 'device_id')
        frequencies_str = config.get(task_name, 'frequencies')
        run_interval_str = config.get(task_name, 'run_interval')
        run_interval = parse_time_interval(run_interval_str)
        # Göreve özel dry_run, yoksa global ayarı kullan
        dry_run = config.getboolean(task_name, 'dry_run', fallback=config.getboolean('global', 'dry_run', fallback=False))

        # Frekans seçim modu
        selection_mode = config.get(task_name, 'selection_mode', fallback=config.get('global', 'selection_mode', fallback='sequential'))

        logging.debug(f"'{task_name}' görevi parametreleri: device_id={device_id}, run_interval={run_interval}s, selection_mode='{selection_mode}'")

        # Saat aralığı kontrolü
        allowed_hours_str = config.get(task_name, 'allowed_hours', fallback=config.get('global', 'allowed_hours', fallback=None))
        if not is_time_in_window(allowed_hours_str):
            logging.info(f"Mevcut saat, izin verilen aralığın ('{allowed_hours_str}') dışında. '{task_name}' görevi atlanıyor.")
            return

        if not device_id or not frequencies_str:
            logging.error(f"'{task_name}' görevinde 'device_id' veya 'frequencies' eksik. Atlanıyor.")
            return

        # Zaman kontrolü
        task_state = state.get(task_name, {})
        last_run_str = task_state.get('last_run_utc')
        now_utc = datetime.now(timezone.utc)

        if last_run_str:
            last_run_utc = datetime.fromisoformat(last_run_str)
            elapsed_seconds = (now_utc - last_run_utc).total_seconds()
            if elapsed_seconds < run_interval:
                logging.info(f"'{task_name}' görevi {int(elapsed_seconds)}s önce çalıştı. Aralık {run_interval}s. Atlanıyor.")
                return

        frequencies = [int(f.strip()) for f in frequencies_str.split(',') if f.strip()]
        if len(frequencies) < 2:
            logging.error(f"'{task_name}' görevi için en az iki frekans gerekli. Atlanıyor.")
            return

    except (configparser.NoOptionError, ValueError) as e:
        logging.error(f"'{task_name}' görevinde yapılandırma hatası: {e}. Atlanıyor.")
        return

    if dry_run:
        logging.warning(f"'{task_name}' görevi DRY RUN (TEST) modunda.")

    # Cihaz detaylarını al
    device_details = get_device_details(config, device_id)
    if not device_details:
        return

    device_name = device_details.get('identification', {}).get('displayName', device_id)

    # Cihaz adının değişip değişmediğini kontrol et ve gerekirse config'i güncelle
    sanitized_name = sanitize_section_name(device_name)
    if sanitized_name != task_name:
        logging.warning(f"'{task_name}' görevi için cihaz adı uyuşmazlığı tespit edildi. Cihazın yeni adı '{device_name}'.")
        logging.info(f"Yapılandırma bölümü '[{task_name}]' adından '[{sanitized_name}]' adına güncelleniyor.")

        # Yeni bölüm adının zaten var olup olmadığını kontrol et (çok nadir bir durum)
        if config.has_section(sanitized_name):
            logging.error(f"Bölüm yeniden adlandırılamıyor: '[{sanitized_name}]' adında bir bölüm zaten var. Yeniden adlandırma atlanıyor.")
        else:
            # Eski bölümdeki verileri al
            items = config.items(task_name)
            # Yeni bölümü oluştur ve verileri kopyala
            config.add_section(sanitized_name)
            for item_key, item_value in items:
                config.set(sanitized_name, item_key, item_value)
            # Eski bölümü sil
            config.remove_section(task_name)

            # state.json dosyasındaki anahtarı da güncelle
            if task_name in state:
                state[sanitized_name] = state.pop(task_name)

            # Değişiklikleri config.ini dosyasına yaz
            with open(config_path, 'w') as configfile:
                config.write(configfile)
            task_name = sanitized_name # Görevin geri kalanında yeni adı kullan

    overview_data = device_details.get('overview')
    if not overview_data or 'frequency' not in overview_data:
        logging.error(f"{device_name} cihazının 'overview' bölümünde frekans verisi yok. Devam edilemiyor.")
        return

    current_frequency = overview_data.get('frequency')
    logging.info(f"{device_name} cihazı şu anda {current_frequency} MHz frekansında.")

    if selection_mode == 'random':
        # Rastgele seçim modu
        possible_next_frequencies = [f for f in frequencies if f != current_frequency]
        if not possible_next_frequencies:
            logging.warning(f"{current_frequency} frekansından geçiş yapılacak başka bir frekans listede bulunmuyor. Değişiklik mümkün değil.")
            next_frequency = current_frequency
        else:
            next_frequency = random.choice(possible_next_frequencies)
    elif selection_mode == 'sequential':
        # Sıralı seçim modu (varsayılan)
        try:
            current_index = frequencies.index(current_frequency)
            next_index = (current_index + 1) % len(frequencies)
            next_frequency = frequencies[next_index]
        except ValueError:
            logging.warning(f"Mevcut frekans {current_frequency}, hedef listede ({frequencies}) bulunmuyor. Listenin ilk frekansına ayarlanacak.")
            next_frequency = frequencies[0]

    logging.info(f"Bir sonraki döngü için hedef frekans {next_frequency} MHz.")

    # Gerekirse cihazı güncelle
    if current_frequency != next_frequency:
        # update_device_frequency fonksiyonuna dry_run parametresini de gönder
        success = update_device_frequency(config, device_id, device_name, next_frequency, dry_run)
        if success:
            # Başarılı olursa son çalışma zamanını güncelle ve bildirim gönder
            success_message = f"✅ **Otomatik Frekans Değiştirildi**\n\nCihaz: `{device_name}`\nYeni Frekans: `{next_frequency} MHz`"
            logging.info(f"{device_name} cihazının frekansı başarıyla {next_frequency} MHz olarak değiştirildi.")
            send_telegram_notification(success_message)
            state[task_name] = {'last_run_utc': now_utc.isoformat()}
    else:
        logging.info("Cihaz zaten hedef frekansta. Değişiklik gerekmiyor.")


def sanitize_section_name(name):
    """Bir cihaz adını geçerli bir config.ini bölüm adına dönüştürür."""
    # Boşlukları ve geçersiz karakterleri alt çizgi ile değiştir
    name = re.sub(r'\s+', '_', name)
    # Alfanümerik veya alt çizgi olmayan tüm karakterleri kaldır
    name = re.sub(r'[^\w]', '', name)
    return name if name else "Isimsiz_Cihaz"

def discover_and_update_config(config, config_path):
    """
    AP (Access Point) cihazlarını keşfeder ve bunları yapılandırma dosyasına ekler.
    Bir tuple döndürür: (yeni cihaz sayısı, otomatik olarak etkinleştirilip etkinleştirilmedikleri).
    """
    logging.info("--- AP Cihaz Keşfi Başlatılıyor ---")
    auto_enable = config.getboolean('global', 'auto_enable_new_devices', fallback=False)

    all_devices = get_all_devices(config)
    if not all_devices:
        logging.error("Cihaz listesi alınamadı. Keşif iptal ediliyor.")
        return -1, auto_enable # Hata durumunu belirtmek için -1 döndür

    logging.info(f"Toplam {len(all_devices)} cihaz bulundu. Access Point (ap-ptmp) olanlar filtreleniyor...")

    # Mevcut yapılandırmadaki cihaz ID'lerinin bir setini oluştur
    existing_device_ids = {
        config.get(s, 'device_id') for s in config.sections() if config.has_option(s, 'device_id')
    }

    ap_devices = [
        dev for dev in all_devices
        if dev.get('overview', {}).get('wirelessMode') == 'ap-ptmp'
    ]

    new_devices_added = 0
    for device in ap_devices:
        device_id = device.get('identification', {}).get('id')
        if device_id in existing_device_ids:
            continue

        device_name = device.get('identification', {}).get('displayName', 'Bilinmeyen Cihaz')
        section_name = sanitize_section_name(device_name)
        
        # Bölüm adının benzersiz olduğundan emin ol
        original_section_name = section_name
        counter = 1
        while config.has_section(section_name):
            section_name = f"{original_section_name}_{counter}"
            counter += 1

        logging.info(f"Yeni AP cihazı bulundu: '{device_name}'. Yapılandırmaya '[{section_name}]' bölümü olarak ekleniyor.")
        config.add_section(section_name)
        config.set(section_name, 'enabled', str(auto_enable).lower())
        config.set(section_name, 'device_id', device_id)
        config.set(section_name, 'frequencies', '5180, 5240') # Varsayılan frekanslar
        config.set(section_name, 'run_interval', '24h') # Varsayılan aralık
        new_devices_added += 1

    if new_devices_added > 0:
        with open(config_path, 'w') as configfile:
            config.write(configfile)
        if auto_enable:
            logging.info(f"'{config_path}' dosyasına {new_devices_added} yeni cihaz başarıyla eklendi ve etkinleştirildi.")
        else:
            logging.info(f"'{config_path}' dosyasına {new_devices_added} yeni cihaz başarıyla eklendi. Lütfen gözden geçirip etkinleştirin.")
    else:
        logging.info("Eklenecek yeni AP cihazı bulunamadı.")
    
    return new_devices_added, auto_enable


def monitor_device_health(config, task_name, state):
    """Cihazın online/offline durumunu izler ve bildirim atar."""
    try:
        device_id = config.get(task_name, 'device_id')
        dev = get_device_details(config, device_id)
        if not dev:
            return

        current_status = dev.get('overview', {}).get('status', 'unknown')
        dname = dev.get('identification', {}).get('displayName', task_name)

        task_state = state.setdefault(task_name, {})
        last_status = task_state.get('last_known_status')

        if last_status and last_status != current_status:
            if last_status == 'active' and current_status == 'disconnected':
                send_telegram_notification(f"🚨 **ALARM: Cihaz Düştü!**\n📡 `{dname}`\n⚠️ Durum: Bağlantı Kesildi")
                logging.warning(f"ALARM: {dname} düştü.")
            elif last_status == 'disconnected' and current_status == 'active':
                send_telegram_notification(f"✅ **BİLGİ: Cihaz Geldi**\n📡 `{dname}`\n🆗 Durum: Aktif")
                logging.info(f"INFO: {dname} geldi.")
        task_state['last_known_status'] = current_status
    except Exception as e:
        logging.error(f"{task_name} için monitor_device_health fonksiyonunda hata: {e}")


def check_api_health(config):
    """Bir TCP bağlantısı deneyerek UISP ana bilgisayarına temel ağ bağlantısını kontrol eder."""
    hostname, _, _ = get_api_credentials(config)
    if not hostname:
        return "❌ **Bağlantı Hatası**\n\nUISP sunucu adresi (`hostname`) `config.ini` dosyasında bulunamadı."

    port = 443 # Standard HTTPS port
    start_time = time.time()

    try:
        # Create a socket object and attempt to connect
        with socket.create_connection((hostname, port), timeout=5): # Bir soket nesnesi oluştur ve bağlanmayı dene
            # If the connection is successful, the host is reachable on that port
            end_time = time.time()
            response_time = round((end_time - start_time) * 1000)

            return (
                f"✅ **Sunucu Erişilebilir**\n\n"
                f"▫️ **UISP Sunucusu:** `{hostname}`\n"
                f"▫️ **Bağlantı Süresi (Port 443):** `{response_time} ms`"
            )
    except (socket.timeout, socket.gaierror, ConnectionRefusedError) as e:
        return f"❌ **Sunucuya Ulaşılamıyor**\n\nUISP sunucusuna (`{hostname}`) bağlanılamadı.\n`{e}`"


# --- Telegram Bot Functions ---

BOT_INSTANCE = None
BOT_APPLICATION = None
ADMIN_CHAT_ID = os.getenv('TELEGRAM_ADMIN_CHAT_ID')

def send_telegram_notification(message):
    """
    Bot aracılığıyla yönetici sohbetine bir mesaj gönderir.
    Bu fonksiyon thread-safe'dir ve ana senkron döngüden çağrılabilir.
    """
    if BOT_INSTANCE and ADMIN_CHAT_ID and BOT_APPLICATION:
        try:
            # Bu fonksiyon, botun asyncio döngüsünden farklı bir thread'den çağrıldığı için,
            # coroutine'i botun döngüsünde çalışacak şekilde zamanlamamız gerekir.
            loop = BOT_APPLICATION.bot_data.get('loop')
            if loop:
                # Mesajı göndermek için bir coroutine oluştur
                coro = BOT_INSTANCE.send_message(chat_id=ADMIN_CHAT_ID, text=message, parse_mode='Markdown')
                # Mevcut thread'imizden botun olay döngüsüne gönder
                import asyncio
                asyncio.run_coroutine_threadsafe(coro, loop)
        except Exception as e:
            logging.error(f"Telegram bildirimi gönderilemedi: {e}")
    else:
        if not hasattr(send_telegram_notification, 'warned'):
            logging.warning("Telegram botu yapılandırılmamış veya yönetici sohbet ID'si ayarlanmamış. Bildirimler atlanıyor.")
            send_telegram_notification.warned = True

def restricted(func):
    """Erişimi yalnızca yönetici kullanıcılara kısıtlayan dekoratör."""
    def wrapped(update: Update, context: CallbackContext, *args, **kwargs):
        if str(update.effective_user.id) != ADMIN_CHAT_ID:
            logging.warning(f"{update.effective_user.id} için yetkisiz erişim reddedildi.")
            update.message.reply_text("Bu botu kullanma yetkiniz yok.")
            return
        return func(update, context, *args, **kwargs)
    return wrapped

@restricted
async def health_command(update: Update, context: CallbackContext):
    """UISP API bağlantısının durumunu kontrol eder ve raporlar."""
    await update.message.reply_text("API bağlantı durumu kontrol ediliyor...")
    config = context.bot_data['config']
    health_message = check_api_health(config)
    await update.message.reply_text(health_message, parse_mode='Markdown')


@restricted
async def restart_command(update: Update, context: CallbackContext):
    """Botu düzgün bir şekilde kapatmak için /restart komutunu işler."""
    user_name = update.effective_user.first_name
    logging.warning(f"Yeniden başlatma komutu {user_name} ({update.effective_user.id}) tarafından verildi. Kapatılıyor.")
    await update.message.reply_text("Bot yeniden başlatılıyor... Kısa süre içinde tekrar çevrimiçi olacaktır.")

    # Send a final notification before shutting down
    shutdown_message = f"⚠️ *Bot Yeniden Başlatılıyor*\n\nKullanıcı: `{user_name}`\nBot betiği sonlandırılıyor. Docker yeniden başlatma politikası sayesinde tekrar aktif olacaktır."
    send_telegram_notification(shutdown_message)

    async def shutdown(context: CallbackContext):
        """Uygulamayı kapatır ve Docker'ın yeniden başlatmasını tetiklemek için sıfır olmayan bir kodla çıkar."""
        if context.bot_data.get('application'):
            await context.bot_data['application'].stop()
        os._exit(1) # Exit the entire process with a non-zero code to trigger Docker restart.

    context.application.create_task(shutdown(context))

async def stop_bot(context: CallbackContext, user_name: str):
    """Botu düzgün bir şekilde kapatmak için /stop komutunu işler."""
    if not context.bot_data.get('application'):
        return

    # Send a final notification before shutting down
    shutdown_message = f"🛑 *Bot Durduruldu*\n\nKullanıcı: `{user_name}`\nBot betiği sonlandırıldı. Yeniden başlatmak için `docker-compose up -d` komutunu kullanın."
    send_telegram_notification(shutdown_message)

    # Give a moment for the message to be sent (async version)
    import asyncio
    await asyncio.sleep(2)

    # Uygulamayı düzgünce durdur
    app = context.bot_data['application']
    if app.running:
        await app.stop()
        await app.shutdown()

@restricted
async def stop_command(update: Update, context: CallbackContext):
    """Botu düzgün bir şekilde kapatmak için /stop komutunu işler."""
    user_name = update.effective_user.first_name
    logging.warning(f"Durdurma komutu {user_name} ({update.effective_user.id}) tarafından verildi. Kapatılıyor.")
    await update.message.reply_text("Bot durduruluyor...")
    context.application.create_task(stop_bot(context, user_name))

@restricted
async def start_command(update: Update, context: CallbackContext):
    """/start komutunu işler."""
    # Bu fonksiyon hem CommandHandler hem de CallbackQueryHandler tarafından çağrılabilir.
    # Her iki durumu da ele alacak şekilde mesaj gönderme mantığını ayarlayalım.
    if update.callback_query:
        # Butondan geliyorsa
        chat_id = update.callback_query.message.chat_id
        send_message_func = update.callback_query.edit_message_text # Bu bir coroutine
    else:
        # /start komutundan geliyorsa
        chat_id = update.message.chat_id
        send_message_func = update.message.reply_text

    config = context.bot_data['config']

    # Cihazların online/offline durumunu almak için API'yi çağır
    all_devices_from_api = get_all_devices(config)
    online_count = 0
    offline_count = 0

    if all_devices_from_api:
        # API'den gelen cihazları ID'ye göre haritala
        api_device_statuses = {
            dev['identification']['id']: dev.get('overview', {}).get('status', 'unknown')
            for dev in all_devices_from_api if 'identification' in dev and 'id' in dev['identification']
        }
        
        # Config'deki cihazların durumunu kontrol et
        managed_device_ids = [config.get(s, 'device_id') for s in config.sections() if config.has_option(s, 'device_id')]
        for device_id in managed_device_ids:
            if api_device_statuses.get(device_id) == 'active':
                online_count += 1
            else:
                offline_count += 1

    # Buton metnini dinamik olarak oluştur
    list_button_text = f"Cihazları Listele"

    keyboard = [
        [InlineKeyboardButton(list_button_text, callback_data='list_devices')],
        [
            InlineKeyboardButton("🟢 Tümünü Etkinleştir", callback_data='enable_all'),
            InlineKeyboardButton("🔴 Tümünü Devre Dışı Bırak", callback_data='disable_all')
        ],
        [
            InlineKeyboardButton("⚙️ Genel Ayarlar", callback_data='global_settings'),
            InlineKeyboardButton("🔎 Cihazları Tara", callback_data='discover_devices')
        ],
        [
            InlineKeyboardButton("🩺 API Durumu", callback_data='health_check'),
            InlineKeyboardButton("🔄 Botu Yenile", callback_data='restart_bot')
        ],
        [
            InlineKeyboardButton("🛠️ Yönetim", callback_data='management_menu')
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Çalışma süresini hesapla ve formatla
    start_time = context.bot_data.get('start_time')
    uptime_str = "Hesaplanıyor..."
    if start_time:
        uptime_delta = datetime.now() - start_time
        days = uptime_delta.days
        hours, remainder = divmod(uptime_delta.seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        
        parts = []
        if days > 0:
            parts.append(f"{days} gün")
        if hours > 0:
            parts.append(f"{hours} saat")
        if minutes > 0 or (days == 0 and hours == 0):
            parts.append(f"{minutes} dakika")
        uptime_str = ", ".join(parts)

    # Otomasyon durumunu hesapla
    task_sections = [s for s in config.sections() if s not in ['unms', 'global']]
    automation_active_count = sum(1 for task in task_sections if config.getboolean(task, 'enabled', fallback=False))
    automation_passive_count = len(task_sections) - automation_active_count

    message = (
        "Merhaba! UISP Cihaz Yönetim Botu'na hoş geldiniz.\n"
        f"▫️ *Çalışma Süresi:* `{uptime_str}`\n\n"
        f"▫️ *Otomasyon Durumu:*\n"
        f"  - Aktif: `{automation_active_count}`\n"
        f"  - Pasif: `{automation_passive_count}`\n\n"
        f"▫️ *Verici Durumu:*\n"        
        f"  - Online: `{online_count}`\n"
        f"  - Offline: `{offline_count}`\n\n"
        "Aşağıdaki menüden işlem yapabilirsiniz."
    )

    await send_message_func(
        message,
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

def build_device_list_menu(config):
    """Cihaz listesi için inline klavyeyi oluşturur."""
    # Cihazların online/offline durumunu almak için API'yi çağır
    all_devices_from_api = get_all_devices(config)
    api_device_statuses = {}
    if all_devices_from_api:
        api_device_statuses = {
            dev['identification']['id']: dev.get('overview', {}).get('status', 'unknown')
            for dev in all_devices_from_api if 'identification' in dev and 'id' in dev['identification']
        }

    keyboard = []
    task_sections = [s for s in config.sections() if s not in ['unms', 'global']]
    # Cihazları alfabetik olarak sırala
    task_sections.sort()
    for task_name in task_sections:
        # Cihaz adını ve ID'yi al
        device_id = config.get(task_name, 'device_id')

        # Cihazın ağ durumunu (online/offline) al ve simgeyi belirle
        network_status = api_device_statuses.get(device_id, 'unknown')
        network_status_icon = "✅" if network_status == 'active' else "❌"

        # Cihazın otomasyon durumunu (aktif/pasif) al ve simgeyi belirle
        is_automation_enabled = config.getboolean(task_name, 'enabled', fallback=False)
        automation_status_icon = "🟢" if is_automation_enabled else "🔴"

        # Cihazın kilitli olup olmadığını kontrol et ve simgeyi belirle
        is_locked = config.getboolean(task_name, 'locked', fallback=False)
        lock_icon = " 🔒" if is_locked else ""

        button_text = f"{network_status_icon} {task_name} {automation_status_icon}{lock_icon}"
        button = InlineKeyboardButton(button_text, callback_data=f"device_{device_id}")
        keyboard.append([button])

    keyboard.append([InlineKeyboardButton("« Geri", callback_data='main_menu')])
    return InlineKeyboardMarkup(keyboard)

def build_device_action_menu(device_id, device_name, config):
    """Belirli bir cihaz için eylem menüsünü oluşturur."""
    # Cihazın 'enabled' durumunu kontrol etmek için görev adını bul
    task_name = None
    for section in config.sections():
        if config.has_option(section, 'device_id') and config.get(section, 'device_id') == device_id:
            task_name = section
            break

    is_enabled = False
    is_locked = False
    if task_name:
        is_enabled = config.getboolean(task_name, 'enabled', fallback=False)
        is_locked = config.getboolean(task_name, 'locked', fallback=False)

    toggle_button_text = "🔴 Otomasyon: Pasif" if not is_enabled else "🟢 Otomasyon: Aktif"
    toggle_callback_data = f"toggle_enabled_{device_id}"
    lock_button_text = "🔓 Kilidi Aç" if is_locked else "🔒 Kilitle (Bakım)"
    lock_callback_data = f"toggle_lock_{device_id}"

    keyboard = [
        [InlineKeyboardButton("📊 Durum Bilgisi", callback_data=f"status_{device_id}")],
        [
            InlineKeyboardButton(toggle_button_text, callback_data=toggle_callback_data),
            InlineKeyboardButton(lock_button_text, callback_data=lock_callback_data)
        ],
        [InlineKeyboardButton("🔄 Frekans Değiştir", callback_data=f"change_freq_{device_id}")],
        [InlineKeyboardButton("⏳ Aralığı Değiştir", callback_data=f"change_interval_{device_id}")],
        [InlineKeyboardButton("✏️ Yapılandırmayı Düzenle", callback_data=f"edit_device_config_{device_id}")],
        [InlineKeyboardButton("📄 Yapılandırmayı Göster", callback_data=f"show_config_{device_id}")],
        [InlineKeyboardButton("🔌 Cihazı Yeniden Başlat", callback_data=f"reboot_{device_id}")],
        [InlineKeyboardButton("« Cihaz Listesine Dön", callback_data='list_devices')]
    ]
    return InlineKeyboardMarkup(keyboard)

def build_frequency_menu(config: configparser.ConfigParser, device_id: str, page: int = 0):
    """
    Cihaz için frekans seçim menüsünü API'den anlık olarak alarak oluşturur.
    Çok fazla frekans olduğunda hatayı önlemek için sayfalama kullanır.
    """
    keyboard = []
    # Frekansları config dosyasından değil, doğrudan API'den çek
    available_freqs = get_available_frequencies(config, device_id)

    if available_freqs and isinstance(available_freqs, list):
        all_frequencies = sorted(available_freqs)
        if all_frequencies:
            items_per_page = 10  # Sayfa başına gösterilecek frekans sayısı
            start_index = page * items_per_page
            end_index = start_index + items_per_page
            paginated_frequencies = all_frequencies[start_index:end_index]

            # Frekansları 2'li gruplar halinde butonlara ekle
            for i in range(0, len(paginated_frequencies), 2):
                row = [InlineKeyboardButton(f"{paginated_frequencies[i]} MHz", callback_data=f"setfreq_{device_id}_{paginated_frequencies[i]}")]
                if i + 1 < len(paginated_frequencies):
                    row.append(InlineKeyboardButton(f"{paginated_frequencies[i+1]} MHz", callback_data=f"setfreq_{device_id}_{paginated_frequencies[i+1]}"))
                keyboard.append(row)

            # Sayfalama butonlarını ekle
            nav_buttons = []
            if page > 0:
                nav_buttons.append(InlineKeyboardButton("« Önceki", callback_data=f"change_freq_{device_id}_{page-1}"))
            
            # Sayfa numarasını gösteren bir buton (tıklanamaz)
            nav_buttons.append(InlineKeyboardButton(f"Sayfa {page + 1}", callback_data="noop"))

            if end_index < len(all_frequencies):
                nav_buttons.append(InlineKeyboardButton("Sonraki »", callback_data=f"change_freq_{device_id}_{page+1}"))
            
            if nav_buttons:
                keyboard.append(nav_buttons)

        else:
            keyboard.append([InlineKeyboardButton("API'den frekans listesi alınamadı!", callback_data="noop")])
    else:
        keyboard.append([InlineKeyboardButton("API'den frekans listesi alınamadı!", callback_data="noop")])

    keyboard.append([InlineKeyboardButton("⌨️ Frekans Yaz", callback_data=f"write_freq_{device_id}")])
    keyboard.append([InlineKeyboardButton("« Geri", callback_data=f"device_{device_id}")])
    return InlineKeyboardMarkup(keyboard)

def build_interval_menu(device_id: str):
    """Yeni bir çalışma aralığı seçmek için menüyü oluşturur."""
    intervals = {
        "30 Dakika": "30m",
        "1 Saat": "1h",
        "2 Saat": "2h",
        "6 Saat": "6h",
        "12 Saat": "12h",
        "24 Saat": "24h",
    }
    keyboard = []
    for text, value in intervals.items():
        callback_data = f"set_interval_{device_id}_{value}"
        keyboard.append([InlineKeyboardButton(text, callback_data=callback_data)])
    
    keyboard.append([InlineKeyboardButton("« Geri", callback_data=f"device_{device_id}")])
    return InlineKeyboardMarkup(keyboard)

def build_global_menu(config):
    """Genel ayarlar için menüyü oluşturur."""
    hours = config.get('global', 'allowed_hours', fallback="Yok") or "Yok (7/24)"
    dry_run_text = "✅ Açık" if config.getboolean('global', 'dry_run', fallback=False) else "❌ Kapalı"
    
    keyboard = [
        [InlineKeyboardButton(f"🕒 Çalışma Saatleri: {hours}", callback_data='edit_global_hours')],
        [InlineKeyboardButton(f"🧪 Test Modu: {dry_run_text}", callback_data='toggle_global_dry_run')],
        [InlineKeyboardButton("« Ana Menü", callback_data='main_menu')]
    ]
    return InlineKeyboardMarkup(keyboard)

def build_hour_menu(mode):
    """Saat seçimi için menüyü oluşturur."""
    keyboard = []
    row = []
    for i in range(24):
        row.append(InlineKeyboardButton(f"{i:02d}", callback_data=f"set_hour_{mode}_{i:02d}"))
        if len(row) == 6:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🗑️ Kısıtlamayı Kaldır", callback_data='clear_global_hours')])
    return InlineKeyboardMarkup(keyboard)

def build_management_menu():
    """Yönetim menüsü için inline klavyeyi oluşturur."""
    keyboard = [
        [InlineKeyboardButton("⚙️ Yapılandırmayı Düzenle", callback_data='edit_config')],
        [InlineKeyboardButton("💾 Yapılandırmayı Yedekle", callback_data='backup_config')],
        [InlineKeyboardButton("« Ana Menü", callback_data='main_menu')]
    ]
    return InlineKeyboardMarkup(keyboard)


async def button_handler(update: Update, context: CallbackContext):
    """Tüm inline buton basımlarını yönetir."""
    query = update.callback_query
    await query.answer()
    data = query.data
    # Her butona basıldığında config dosyasını sıfırdan yeniden oku.
    # Bu, ana döngüdeki veya manuel yapılan değişikliklerin bota anında yansımasını sağlar.
    context.bot_data['config'] = configparser.ConfigParser()
    config_path = os.path.join(os.path.dirname(__file__), 'config', 'config.ini')
    context.bot_data['config'].read(config_path)
    config = context.bot_data['config']

    if data == "noop":
        return # "no operation" (işlem yok) butonları için hiçbir şey yapma

    if data == 'main_menu':
        # start_command'ı çağırarak ana menüyü yeniden göster
        await start_command(update, context)

    elif data == 'list_devices':
        reply_markup = build_device_list_menu(config)
        await query.edit_message_text("Yönetmek için bir cihaz seçin:", reply_markup=reply_markup)

    elif data.startswith('device_'):
        device_id = data.split('_', 1)[1] # Sadece ilk '_' karakterinden böl
        device_details = get_device_details(config, device_id)
        if not device_details:
            await query.edit_message_text(f"❌ **Hata:** Cihaz detayları alınamadı (ID: `{device_id}`).\n\nCihaz UISP'ten silinmiş veya bir API bağlantı sorunu olabilir. Günlük (log) kayıtlarını kontrol edin.", reply_markup=query.message.reply_markup, parse_mode='Markdown')
            return

        device_name = device_details.get('identification', {}).get('displayName', device_id)
        reply_markup = build_device_action_menu(device_id, device_name, config)
        await query.edit_message_text(f"Cihaz: *{device_name}*\n\nNe yapmak istersiniz?", reply_markup=reply_markup, parse_mode='Markdown')

    elif data.startswith('status_'):
        device_id = data.split('_', 1)[1]
        details = get_device_details(config, device_id)
        if not details:
            await query.edit_message_text("Cihaz detayları alınamadı.", reply_markup=query.message.reply_markup)
            return
        
        name = details.get('identification', {}).get('displayName', 'N/A')
        status = details.get('overview', {}).get('status', 'N/A')
        freq = details.get('overview', {}).get('frequency', 'N/A')
        uptime = details.get('overview', {}).get('uptime', 0)
        uptime_days = uptime // (24 * 3600)
        uptime_hours = (uptime % (24 * 3600)) // 3600

        # Sinyal ve diğer detaylar
        signal = details.get('overview', {}).get('signal', 'N/A')
        model = details.get('identification', {}).get('model', 'N/A')

        message = (
            f"📊 *{name}* - Durum Bilgisi\n\n"
            f"▫️ *Durum:* `{status}`\n"
            f"▫️ *Frekans:* `{freq} MHz`\n"
            f"▫️ *Sinyal:* `{signal} dBm`\n"
            f"▫️ *Çalışma Süresi:* `{uptime_days} gün, {uptime_hours} saat`\n"
            f"▫️ *Model:* `{model}`"
        )
        # Menüyü yeniden oluşturarak geri dön
        await query.edit_message_text(message, reply_markup=build_device_action_menu(device_id, name, config), parse_mode='Markdown')

    elif data.startswith('edit_device_config_'):
        device_id = data[len('edit_device_config_'):]
        task_name = None
        for section in config.sections():
            if config.has_option(section, 'device_id') and config.get(section, 'device_id') == device_id:
                task_name = section
                break
        
        if not task_name:
            await query.edit_message_text("❌ Hata: Cihaz yapılandırmada bulunamadı.", reply_markup=query.message.reply_markup)
            return

        config_items = config.items(task_name)
        config_block = "\n".join([f"{key} = {value}" for key, value in config_items])

        message = (
            f"✏️ *{task_name}* - Yapılandırmayı Düzenle\n\n"
            "Aşağıdaki metni kopyalayın, düzenleyin ve **tek bir mesaj olarak geri gönderin.**\n\n"
            "⚠️ **DİKKAT:** `device_id` değerini değiştirmeyin. Diğer hatalı düzenlemeler cihaz otomasyonunu bozabilir."
        )
        await query.edit_message_text(message, parse_mode='Markdown')

        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"```ini\n[{task_name}]\n{config_block}\n```",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ İptal Et", callback_data=f"device_{device_id}")]])
        )

        # Kullanıcıdan bir sonraki metin mesajını beklediğimizi işaretle
        context.user_data['state'] = 'awaiting_device_config'
        context.user_data['editing_task_name'] = task_name

    elif data.startswith('show_config_'):
        device_id = data[len('show_config_'):] # 'show_config_' ön ekini kaldır

        # Find the task name for the device
        task_name = None
        for section in config.sections():
            if config.has_option(section, 'device_id') and config.get(section, 'device_id') == device_id:
                task_name = section
                break

        if not task_name:
            await query.edit_message_text("Hata: Cihaz yapılandırmada bulunamadı.", reply_markup=query.message.reply_markup)
            return

        config_items = config.items(task_name)
        message = f"📄 *{task_name}* - Yapılandırma\n\n"
        message += "```ini\n"
        for key, value in config_items:
            message += f"{key} = {value}\n"
        message += "```"

        await query.edit_message_text(message, reply_markup=build_device_action_menu(device_id, task_name, config), parse_mode='Markdown')

    elif data == 'cancel_edit':
        # Düzenleme modundan çık (Genel yapılandırma için)
        if context.user_data.get('state') == 'awaiting_config':
            context.user_data['state'] = None
            await query.edit_message_text("Yapılandırma düzenleme işlemi iptal edildi.", reply_markup=None) # Butonları kaldır
            await start_command(update, context)

    elif data.startswith('write_freq_'):
        device_id = data[len('write_freq_'):]
        
        # Kullanıcıya ne yapması gerektiğini söyle
        message = (
            "⌨️ Lütfen ayarlamak istediğiniz frekansı (sadece sayı olarak) yazın.\n\n"
            "Örnek: `5240`"
        )
        await query.edit_message_text(
            message, 
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ İptal Et", callback_data=f"device_{device_id}")]])
        )
        context.user_data['state'] = 'awaiting_frequency'
        context.user_data['editing_device_id'] = device_id

    elif data.startswith('change_interval_'):
        device_id = data[len('change_interval_'):]
        reply_markup = build_interval_menu(device_id)
        await query.edit_message_text("Yeni bir otomasyon aralığı seçin:", reply_markup=reply_markup)

    elif data.startswith('set_interval_'):
        parts_str = data[len('set_interval_'):]
        device_id, interval_str = parts_str.rsplit('_', 1)

        task_name = None
        for section in config.sections():
            if config.has_option(section, 'device_id') and config.get(section, 'device_id') == device_id:
                task_name = section
                break

        if not task_name:
            await query.edit_message_text("❌ Hata: Cihaz yapılandırmada bulunamadı.", reply_markup=query.message.reply_markup)
            return

        config.set(task_name, 'run_interval', interval_str)

        # Değişiklikleri config.ini dosyasına kaydet
        with open(config_path, 'w') as configfile:
            config.write(configfile)

        device_details = get_device_details(config, device_id)
        device_name = device_details.get('identification', {}).get('displayName', device_id)
        
        message = (
            f"✅ **Aralık Güncellendi**\n\n"
            f"▫️ **Cihaz:** `{device_name}`\n"
            f"▫️ **Yeni Aralık:** `{interval_str}`"
        )

        # İşlem sonrası cihaz aksiyon menüsüne geri dön
        reply_markup = build_device_action_menu(device_id, device_name, config)
        await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')


    elif data.startswith('change_freq_'):
        parts = data.split('_')
        device_id = parts[2]
        page = int(parts[3]) if len(parts) > 3 else 0
        reply_markup = build_frequency_menu(config, device_id, page)
        await query.edit_message_text("Yeni bir frekans seçin:", reply_markup=reply_markup)

    elif data.startswith('setfreq_'):
        # 'setfreq_' ön ekini kaldır
        parts_str = data[len('setfreq_'):]
        # Son '_' karakterinden bölerek ID ve frekansı ayır
        device_id, freq_str = parts_str.rsplit('_', 1)
        frequency = int(freq_str)
        device_details = get_device_details(config, device_id)
        device_name = device_details.get('identification', {}).get('displayName', device_id)

        await query.edit_message_text(f"`{device_name}` cihazının frekansı `{frequency} MHz` olarak ayarlanıyor... Lütfen bekleyin.", parse_mode='Markdown')

        success = update_device_frequency(config, device_id, device_name, frequency, dry_run=False)

        if success:
            message = f"✅ **Frekans Değiştirildi**\n\nCihaz: `{device_name}`\nYeni Frekans: `{frequency} MHz`"
        else:
            message = f"❌ **Frekans Değiştirme BAŞARISIZ**\n\nCihaz: `{device_name}`\nİşlem sırasında bir hata oluştu. Günlük (log) kayıtlarını kontrol edin."
        
        # İşlem sonrası cihaz aksiyon menüsüne geri dön
        reply_markup = build_device_action_menu(device_id, device_name, config)
        await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')

    elif data.startswith('toggle_lock_'):
        device_id = data[len('toggle_lock_'):]
        task_name = next((s for s in config.sections() if config.has_option(s, 'device_id') and config.get(s, 'device_id') == device_id), None)
        if not task_name: return

        current_lock_state = config.getboolean(task_name, 'locked', fallback=False)
        new_lock_state = not current_lock_state
        config.set(task_name, 'locked', str(new_lock_state).lower())
        with open(config_path, 'w') as configfile: config.write(configfile)

        device_details = get_device_details(config, device_id)
        device_name = device_details.get('identification', {}).get('displayName', device_id)
        await query.edit_message_text(f"Cihaz durumu güncellendi.", reply_markup=build_device_action_menu(device_id, device_name, config))

    elif data.startswith('toggle_enabled_'):
        device_id = data[len('toggle_enabled_'):]

        # Find the task name for the device
        task_name = None
        for section in config.sections():
            if config.has_option(section, 'device_id') and config.get(section, 'device_id') == device_id:
                task_name = section
                break

        if not task_name:
            await query.edit_message_text("Hata: Cihaz yapılandırmada bulunamadı.", reply_markup=query.message.reply_markup)
            return

        # Cihaz kilitliyse otomasyonu değiştirme
        if config.getboolean(task_name, 'locked', fallback=False):
            await query.answer("⚠️ Cihaz KİLİTLİ! Otomasyon durumu değiştirilemez.", show_alert=True)
            return

        current_state = config.getboolean(task_name, 'enabled', fallback=False)
        new_state = not current_state
        config.set(task_name, 'enabled', str(new_state).lower())

        # Değişiklikleri config.ini'ye geri kaydet
        with open(config_path, 'w') as configfile:
            config.write(configfile)

        device_details = get_device_details(config, device_id)
        device_name = device_details.get('identification', {}).get('displayName', device_id)
        
        # Menüyü yeni durumla yeniden oluştur
        reply_markup = build_device_action_menu(device_id, device_name, config)
        status_text = "Aktif" if new_state else "Pasif"
        user_name = query.from_user.first_name

        message = (
            f"⚙️ **Otomasyon Durumu Güncellendi**\n\n"
            f"▫️ **Cihaz:** `{device_name}`\n"
            f"▫️ **Yeni Durum:** `{status_text}`\n"
            f"▫️ **Değiştiren:** `{user_name}`"
        )

        await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')
    elif data.startswith('reboot_confirm_'):
        device_id = data[len('reboot_confirm_'):]
        device_details = get_device_details(config, device_id)
        device_name = device_details.get('identification', {}).get('displayName', device_id)

        await query.edit_message_text(f"`{device_name}` cihazına yeniden başlatma komutu gönderiliyor... Lütfen bekleyin.", parse_mode='Markdown')

        success = reboot_device(config, device_id, device_name)
        
        if success:
            message = f"✅ **Komut Gönderildi**\n\n`{device_name}` cihazı yeniden başlatılıyor."
        else:
            message = f"❌ **Yeniden Başlatma BAŞARISIZ**\n\n`{device_name}` cihazına komut gönderilemedi. Günlük (log) kayıtlarını kontrol edin."
        
        reply_markup = build_device_action_menu(device_id, device_name, config)
        await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')

    elif data.startswith('reboot_'):
        device_id = data[len('reboot_'):]
        device_details = get_device_details(config, device_id)
        device_name = device_details.get('identification', {}).get('displayName', device_id)

        keyboard = [
            [InlineKeyboardButton("EVET, YENİDEN BAŞLAT", callback_data=f"reboot_confirm_{device_id}")],
            [InlineKeyboardButton("HAYIR, İPTAL ET", callback_data=f"device_{device_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(f"⚠️ *EMİN MİSİNİZ?*\n\n`{device_name}` cihazı yeniden başlatılacak. Bu işlem cihaza bağlı kullanıcıların bağlantısını geçici olarak kesecektir.", reply_markup=reply_markup, parse_mode='Markdown')



    elif data == 'enable_all' or data == 'disable_all':
        new_state = (data == 'enable_all')
        status_text = "etkinleştirildi" if new_state else "devre dışı bırakıldı"
        user_name = query.from_user.first_name

        all_task_sections = [s for s in config.sections() if s not in ['unms', 'global']]
        unlocked_tasks = []
        locked_tasks = []

        # Cihazları kilitli ve kilitli olmayan olarak ayır
        for task_name in all_task_sections:
            if config.getboolean(task_name, 'locked', fallback=False):
                locked_tasks.append(task_name)
            else:
                unlocked_tasks.append(task_name)

        # Sadece kilitli olmayan cihazların durumunu değiştir
        for task_name in unlocked_tasks:
            config.set(task_name, 'enabled', str(new_state).lower())

        # Değişiklikleri config.ini dosyasına kaydet
        with open(config_path, 'w') as configfile:
            config.write(configfile)

        # Kullanıcıyı bilgilendirmek için mesajı oluştur
        message = f"✅ **Toplu İşlem Tamamlandı**\n\n"
        message += f"▫️ **Değiştiren:** `{user_name}`\n"

        if unlocked_tasks:
            unlocked_list_str = "\n".join([f"- `{task}`" for task in unlocked_tasks])
            message += f"▫️ **Durumu Değiştirilen Cihazlar ({len(unlocked_tasks)} adet):**\n{unlocked_list_str}\n\n"
        else:
            message += "Durumu değiştirilecek kilitli olmayan cihaz bulunamadı.\n\n"

        if locked_tasks:
            locked_list_str = "\n".join([f"- `{task}`" for task in locked_tasks])
            message += f"⚠️ **Atlanan Kilitli Cihazlar ({len(locked_tasks)} adet):**\n{locked_list_str}"

        await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Ana Menü", callback_data='main_menu')]]), parse_mode='Markdown')

    elif data == 'health_check':
        health_message = check_api_health(config)
        keyboard = [
            [InlineKeyboardButton("« Ana Menüye Dön", callback_data='main_menu')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(health_message, reply_markup=reply_markup, parse_mode='Markdown')

    
    elif data == 'restart_bot':
        keyboard = [
            [InlineKeyboardButton("EVET, YENİDEN BAŞLAT", callback_data="restart_bot_confirm")],
            [InlineKeyboardButton("HAYIR, İPTAL ET", callback_data="main_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "⚠️ *BOTU YENİDEN BAŞLATMAK İSTEDİĞİNİZE EMİN MİSİNİZ?*\n\n"
            "Bu işlem botu geçici olarak çevrimdışı yapacaktır. Docker tarafından otomatik olarak yeniden başlatılacaktır.",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

    elif data == 'restart_bot_confirm':
        user_name = query.from_user.first_name
        logging.warning(f"Buton aracılığıyla yeniden başlatma komutu {user_name} ({query.from_user.id}) tarafından verildi. Kapatılıyor.")
        await query.edit_message_text("Bot yeniden başlatılıyor... Kısa süre içinde tekrar çevrimiçi olacaktır.")

        shutdown_message = f"⚠️ *Bot Yeniden Başlatılıyor*\n\nKullanıcı: `{user_name}`\nBot betiği sonlandırılıyor. Docker yeniden başlatma politikası sayesinde tekrar aktif olacaktır."
        send_telegram_notification(shutdown_message)

        async def shutdown(context: CallbackContext):
            """Uygulamayı kapatır ve Docker'ın yeniden başlatmasını tetiklemek için sıfır olmayan bir kodla çıkar."""
            if context.bot_data.get('application'):
                await context.bot_data['application'].stop()
            os._exit(1) # Exit the entire process with a non-zero code to trigger Docker restart.

        context.application.create_task(shutdown(context))
    elif data == 'discover_devices':
        await query.edit_message_text("Cihaz keşfi başlatılıyor, lütfen bekleyin...")
        
        new_devices_count, auto_enabled = discover_and_update_config(config, config_path)

        if new_devices_count > 0:
            if auto_enabled:
                message = f"✅ Keşif tamamlandı!\n\n`{new_devices_count}` yeni cihaz bulundu, `config.ini` dosyasına eklendi ve **otomatik olarak etkinleştirildi**."
            else:
                message = f"✅ Keşif tamamlandı!\n\n`{new_devices_count}` yeni cihaz `config.ini` dosyasına eklendi. Lütfen ayarlarını kontrol edip etkinleştirin."
        elif new_devices_count == 0:
            message = "✅ Keşif tamamlandı, yeni cihaz bulunamadı."
        else: # new_devices_count == -1
            message = "❌ Keşif başarısız oldu. API bağlantısı veya yetkilerle ilgili bir sorun olabilir. Günlük (log) kayıtlarını kontrol edin."

        # Ana menüye dönmek için butonu hazırla
        keyboard = [[InlineKeyboardButton("« Ana Menüye Dön", callback_data='main_menu')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')

    elif data == 'global_settings':
        await query.edit_message_text("⚙️ **Genel Ayarlar**", reply_markup=build_global_menu(config), parse_mode='Markdown')

    elif data == 'toggle_global_dry_run':
        current_val = config.getboolean('global', 'dry_run', fallback=False)
        config.set('global', 'dry_run', str(not current_val).lower())
        with open(config_path, 'w') as f: config.write(f)
        await query.edit_message_text("✅ Test modu durumu güncellendi.", reply_markup=build_global_menu(config))

    elif data == 'edit_global_hours':
        await query.edit_message_text("🕒 Lütfen otomasyonun çalışacağı **başlangıç saatini** seçin:", reply_markup=build_hour_menu('start'))

    elif data.startswith('set_hour_start_'):
        start_hour = data.split('_')[-1]
        context.user_data['start_hour'] = start_hour
        await query.edit_message_text(f"🕒 Başlangıç saati `{start_hour}` olarak ayarlandı.\nŞimdi **bitiş saatini** seçin:", reply_markup=build_hour_menu('end'), parse_mode='Markdown')

    elif data.startswith('set_hour_end_'):
        start_hour = context.user_data.get('start_hour')
        end_hour = data.split('_')[-1]
        if start_hour is None:
            await query.edit_message_text("❌ Hata: Başlangıç saati seçilmemiş. Lütfen tekrar deneyin.", reply_markup=build_global_menu(config))
            return
        
        config.set('global', 'allowed_hours', f"{start_hour}-{end_hour}")
        with open(config_path, 'w') as f: config.write(f)
        await query.edit_message_text(f"✅ Çalışma saatleri `{start_hour}-{end_hour}` olarak ayarlandı.", reply_markup=build_global_menu(config), parse_mode='Markdown')

    elif data == 'clear_global_hours':
        config.set('global', 'allowed_hours', '')
        with open(config_path, 'w') as f: config.write(f)
        await query.edit_message_text("✅ Çalışma saati kısıtlaması kaldırıldı (7/24 aktif).", reply_markup=build_global_menu(config))

    elif data == 'edit_config':
        # Kullanıcıya mevcut yapılandırmayı gönder ve düzenleme moduna geç
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config_content = f.read()

            # Kullanıcıya ne yapması gerektiğini söyle
            message = (
                "📝 *Yapılandırmayı Düzenle*\n\n"
                "Aşağıdaki `config.ini` içeriğini kopyalayın, düzenleyin ve **tek bir mesaj olarak geri gönderin.**\n\n"
                "⚠️ **DİKKAT:** Hatalı bir düzenleme betiğin çalışmasını durdurabilir. Gönderdiğiniz metin, mevcut yapılandırmanın üzerine yazılacaktır."
            )
            await query.edit_message_text(message, parse_mode='Markdown')

            # Yapılandırma içeriğini ayrı bir mesaj olarak gönder
            # Bu, kullanıcının kopyalamasını kolaylaştırır
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"```ini\n{config_content}\n```",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ İptal Et", callback_data='cancel_edit')]])
            )

            # Kullanıcıdan bir sonraki metin mesajını beklediğimizi işaretle
            context.user_data['state'] = 'awaiting_config'

        except FileNotFoundError:
            await query.edit_message_text("❌ Hata: `config.ini` dosyası bulunamadı.", reply_markup=query.message.reply_markup)
        except Exception as e:
            await query.edit_message_text(f"❌ Hata: Yapılandırma dosyası okunurken bir sorun oluştu: {e}", reply_markup=query.message.reply_markup)

    elif data == 'cancel_edit':
        # Düzenleme modundan çık
        if context.user_data.get('state') == 'awaiting_config':
            context.user_data['state'] = None
            await query.edit_message_text("Yapılandırma düzenleme işlemi iptal edildi.", reply_markup=None) # Butonları kaldır
            # Ana menüyü tekrar göster
            await start_command(update, context) # query'yi update gibi kullanabiliriz
        else:
            await query.answer("Aktif bir düzenleme işlemi yok.")

    elif data == 'backup_config':
        await query.edit_message_text("Yapılandırma dosyaları için manuel yedekleme tetikleniyor...")
        create_backup(config_path, context.bot_data['state_path'], context.bot_data['backup_dir'], context.bot_data['backup_retention'])
        await query.edit_message_text("✅ Yedekleme tamamlandı. Yedekler `config/backups` klasörüne kaydedildi.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Ana Menü", callback_data='main_menu')]]))

    elif data == 'management_menu':
        await query.edit_message_text("🛠️ **Yönetim Menüsü**", reply_markup=build_management_menu(), parse_mode='Markdown')

def start_bot(token, config, bot_context_data):
    """Initializes and starts the Telegram bot."""
    import asyncio
    import time

    while True:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            global BOT_INSTANCE, BOT_APPLICATION
            application = Application.builder().token(token).build()
            BOT_INSTANCE = application.bot
            BOT_APPLICATION = application

            application.bot_data['loop'] = loop # Thread-safe çağrılar için döngüyü sakla
            application.bot_data['config'] = config
            application.bot_data['start_time'] = bot_context_data['script_start_time'] # Store the script start time
            # Diğer fonksiyonların erişebilmesi için bazı yolları bot_data'ya ekleyelim
            config_dir = os.path.join(os.path.dirname(__file__), 'config')
            application.bot_data['state_path'] = os.path.join(config_dir, 'state.json')
            application.bot_data['backup_dir'] = os.path.join(config_dir, 'backups')
            try:
                application.bot_data['backup_retention'] = config.getint('backup', 'retention_count', fallback=7)
            except configparser.NoSectionError:
                application.bot_data['backup_retention'] = 7

            application.bot_data['application'] = application # Restart/stop komutları için application'ı sakla
            application.add_handler(CommandHandler("start", start_command))
            application.add_handler(CommandHandler("restart", restart_command))
            application.add_handler(CommandHandler("stop", stop_command))
            application.add_handler(CommandHandler("health", health_command))
            application.add_handler(CallbackQueryHandler(button_handler))
            # Metin mesajlarını yakalamak için bir handler ekle
            application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

            logging.info("Telegram botu başlatıldı ve komutlar için dinlemede.")
            # run_polling() is a blocking call. It will run until the application is stopped.
            # stop_signals=None prevents it from trying to register signal handlers,
            # which is only allowed in the main thread.
            application.run_polling(allowed_updates=Update.ALL_TYPES, stop_signals=None)

            # Eğer buraya başarılı bir şekilde ulaşıldıysa (bot durdurulduysa) döngüden çık
            break
        except Exception as e:
            logging.error(f"Telegram bot iterasyonunda kritik bir hata oluştu: {e}. 15 saniye içinde yeniden başlatılacak...", exc_info=True)
            time.sleep(15)


@restricted
async def handle_text_message(update: Update, context: CallbackContext):
    """Gelen düz metin mesajlarını, özellikle yapılandırma güncellemeleri için işler."""
    # Sadece 'awaiting_config' durumundayken metinleri işle
    if context.user_data.get('state') == 'awaiting_config':
        new_config_content = update.message.text
        config_path = os.path.join(os.path.dirname(__file__), 'config', 'config.ini')

        # Adım 1: Gönderilen metnin geçerli bir INI formatı olup olmadığını kontrol et
        temp_config = configparser.ConfigParser()
        try:
            temp_config.read_string(new_config_content)
            logging.info("Alınan yeni yapılandırma geçerli bir INI formatında.")
        except configparser.Error as e:
            await update.message.reply_text(
                f"❌ **Geçersiz Yapılandırma!**\n\n"
                f"Gönderdiğiniz metin geçerli bir `.ini` dosyası formatında değil. Değişiklikler kaydedilmedi.\n\n"
                f"Hata: `{e}`\n\n"
                "Lütfen formatı düzeltip tekrar deneyin veya işlemi iptal edin."
            )
            return

        # Adım 2: Mevcut config dosyasını yedekle
        backup_dir = os.path.join(os.path.dirname(config_path), 'backups')
        if not os.path.exists(backup_dir):
            os.makedirs(backup_dir)
        timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        backup_file = os.path.join(backup_dir, f"config_manual-edit-backup_{timestamp}.ini.bak")
        shutil.copy2(config_path, backup_file)
        logging.info(f"Mevcut yapılandırmanın yedeği {backup_file} adresinde oluşturuldu.")

        # Adım 3: Yeni yapılandırmayı dosyaya yaz
        with open(config_path, 'w', encoding='utf-8') as f:
            f.write(new_config_content)

        # Adım 4: Durumu sıfırla ve kullanıcıyı bilgilendir
        context.user_data['state'] = None
        logging.info(f"Yapılandırma dosyası {update.effective_user.first_name} kullanıcısı tarafından güncellendi.")
        await update.message.reply_text("✅ **Yapılandırma Başarıyla Güncellendi!**\n\nDeğişiklikleriniz `config.ini` dosyasına kaydedildi. Betik, bir sonraki döngüde yeni ayarları kullanacaktır.")
    
    elif context.user_data.get('state') == 'awaiting_device_config':
        new_device_config_content = update.message.text
        task_name = context.user_data.get('editing_task_name')
        config_path = os.path.join(os.path.dirname(__file__), 'config', 'config.ini')

        if not task_name:
            await update.message.reply_text("❌ Hata: Hangi cihazın düzenlendiği bilgisi kayboldu. Lütfen işlemi yeniden başlatın.")
            context.user_data['state'] = None
            return

        try:
            # Kullanıcının gönderdiği metni işle
            config = configparser.ConfigParser()
            config.read(config_path)
            original_device_id = config.get(task_name, 'device_id')

            # Geçici bir parser ile kullanıcının gönderdiği metni oku
            temp_config = configparser.ConfigParser()
            temp_config.read_string(new_device_config_content)

            # Ana config üzerinde değişiklikleri yap
            for key, value in temp_config.items(task_name):
                if key.lower() == 'device_id' and value != original_device_id:
                    logging.warning(f"Kullanıcı '{task_name}' için device_id değiştirmeye çalıştı. Değişiklik yoksayılıyor.")
                    continue # device_id'nin değiştirilmesini engelle
                config.set(task_name, key, value)
            
            with open(config_path, 'w') as configfile: config.write(configfile)

            context.user_data['state'] = None
            context.user_data['editing_task_name'] = None
            await update.message.reply_text(f"✅ **Cihaz Yapılandırması Güncellendi!**\n\n`{task_name}` için yapılan değişiklikler kaydedildi.")
        except Exception as e:
            await update.message.reply_text(f"❌ **Hata:** Yapılandırma kaydedilirken bir sorun oluştu: `{e}`. Lütfen formatı kontrol edip tekrar deneyin.")
    elif context.user_data.get('state') == 'awaiting_device_config':
        new_device_config_content = update.message.text
        task_name = context.user_data.get('editing_task_name')
        config_path = os.path.join(os.path.dirname(__file__), 'config', 'config.ini')

        if not task_name:
            await update.message.reply_text("❌ Hata: Hangi cihazın düzenlendiği bilgisi kayboldu. Lütfen işlemi yeniden başlatın.")
            context.user_data['state'] = None
            return

        try:
            # Kullanıcının gönderdiği metni satır satır işle
            config = configparser.ConfigParser()
            config.read(config_path)
            original_device_id = config.get(task_name, 'device_id')

            for line in new_device_config_content.strip().split('\n'):
                if '=' in line:
                    key, value = map(str.strip, line.split('=', 1))
                    if key.lower() == 'device_id' and value != original_device_id:
                        logging.warning(f"Kullanıcı '{task_name}' için device_id değiştirmeye çalıştı. Değişiklik yoksayılıyor.")
                        continue # device_id'nin değiştirilmesini engelle
                    config.set(task_name, key, value)
            
            # Değişiklikleri kaydet
            with open(config_path, 'w') as configfile:
                config.write(configfile)

            context.user_data['state'] = None
            context.user_data['editing_task_name'] = None
            logging.info(f"'{task_name}' için cihaz yapılandırması {update.effective_user.first_name} kullanıcısı tarafından güncellendi.")
            await update.message.reply_text(f"✅ **Cihaz Yapılandırması Güncellendi!**\n\n`{task_name}` için yapılan değişiklikler kaydedildi.")

        except Exception as e:
            await update.message.reply_text(f"❌ **Hata:** Yapılandırma kaydedilirken bir sorun oluştu: `{e}`. Lütfen formatı kontrol edip tekrar deneyin.")
    
    elif context.user_data.get('state') == 'awaiting_frequency':
        device_id = context.user_data.get('editing_device_id')
        freq_input = update.message.text.strip()

        if not device_id:
            await update.message.reply_text("❌ Hata: Hangi cihazın düzenlendiği bilgisi kayboldu. Lütfen işlemi yeniden başlatın.")
            context.user_data['state'] = None
            return

        # Adım 1: Girdinin sayı olup olmadığını kontrol et
        try:
            frequency = int(freq_input)
        except ValueError:
            await update.message.reply_text(f"❌ **Geçersiz Giriş:** Lütfen sadece sayısal bir frekans değeri girin (Örn: `5240`).")
            return

        # Adım 2: Girilen frekansın cihaz tarafından desteklenip desteklenmediğini kontrol et
        await update.message.reply_text("Girilen frekans API üzerinden doğrulanıyor...")
        available_freqs = get_available_frequencies(context.bot_data['config'], device_id)

        if available_freqs and frequency in available_freqs:
            # Adım 3: Frekansı güncelle
            device_details = get_device_details(context.bot_data['config'], device_id)
            device_name = device_details.get('identification', {}).get('displayName', device_id)

            await update.message.reply_text(f"`{device_name}` cihazının frekansı `{frequency} MHz` olarak ayarlanıyor...")
            success = update_device_frequency(context.bot_data['config'], device_id, device_name, frequency, dry_run=False)

            if success:
                message = f"✅ **Frekans Değiştirildi**\n\nCihaz: `{device_name}`\nYeni Frekans: `{frequency} MHz`"
            else:
                message = f"❌ **Frekans Değiştirme BAŞARISIZ**\n\nCihaz: `{device_name}`\nİşlem sırasında bir hata oluştu. Günlük (log) kayıtlarını kontrol edin."
            
            await update.message.reply_text(message, parse_mode='Markdown')

        else:
            # Frekans listede yoksa veya liste alınamadıysa hata ver
            error_msg = f"❌ **Geçersiz Frekans:** `{frequency} MHz` bu cihaz için desteklenmiyor veya geçersiz bir değer."
            if available_freqs:
                 error_msg += f"\n\nDesteklenen bazı frekanslar: `{', '.join(map(str, available_freqs[:5]))}...`"
            await update.message.reply_text(error_msg, parse_mode='Markdown')

        # Durumu temizle
        context.user_data['state'] = None
        context.user_data['editing_device_id'] = None


if __name__ == "__main__":
    config_path = os.path.join(os.path.dirname(__file__), 'config', 'config.ini')
    config = configparser.ConfigParser()

    # Check if config exists before starting the loop
    log_level_str = os.getenv('LOG_LEVEL', 'INFO').upper()
    log_level = getattr(logging, log_level_str, logging.INFO)
    logging.basicConfig(level=log_level, format='%(asctime)s - %(levelname)s - %(message)s')

    # LOG_LEVEL DEBUG ise, kütüphaneler için de detaylı loglamayı etkinleştir.
    if log_level == logging.DEBUG:
        logging.getLogger("telegram").setLevel(logging.DEBUG)
        logging.getLogger("urllib3").setLevel(logging.DEBUG)
        logging.info("Detaylı kütüphane hata ayıklaması (telegram, urllib3) etkinleştirildi.")

    if not os.path.exists(config_path):
        logging.error(f"Yapılandırma dosyası şu yolda bulunamadı: {config_path}. Çıkılıyor.")
    else:
        config.read(config_path)

        # Betiğin başlangıç zamanını kaydet
        script_start_time = datetime.now()

        # Check for --discover argument
        if len(sys.argv) > 1 and sys.argv[1] == '--discover':
            discover_and_update_config(config, config_path)
            sys.exit(0)  # Exit after discovery

        # Telegram botunu başlat
        telegram_token = os.getenv('TELEGRAM_BOT_TOKEN')
        if telegram_token and ADMIN_CHAT_ID:
            bot_thread = Thread(target=start_bot, args=(telegram_token, config, {'script_start_time': script_start_time}), daemon=True)
            bot_thread.start()
        else:
            logging.warning("Telegram bot token'ı veya yönetici sohbet ID'si ortam değişkenlerinde bulunamadı. Etkileşimli bot başlatılmayacak.")

        state_path = os.path.join(os.path.dirname(config_path), 'state.json')
        
        try:
            check_interval_str = config.get('global', 'check_interval', fallback='300s')
            check_interval = parse_time_interval(check_interval_str)
        except (configparser.NoSectionError, configparser.NoOptionError):
            check_interval = 300  # 5 dakika

        logging.info(f"UISP Otomatik Kanal betiği başlatılıyor. Görevler her {check_interval} saniyede bir kontrol edilecek.")
        
        # Botun başlaması için kısa bir bekleme süresi
        time.sleep(5)
        start_message = "🚀 *UISP Otomatik Kanal betiği başarıyla başlatıldı.*"
        if telegram_token and ADMIN_CHAT_ID:
            start_message += "\nCihazları yönetmek için /start komutunu kullanabilirsiniz."
        send_telegram_notification(start_message)

        # Ana zamanlama döngüsü
        # Yedekleme ayarlarını oku
        try:
            backup_enabled = config.getboolean('backup', 'enabled', fallback=False)
            backup_interval_str = config.get('backup', 'interval', fallback='24h')
            backup_interval = parse_time_interval(backup_interval_str)
            backup_retention = config.getint('backup', 'retention_count', fallback=7)
            backup_dir = os.path.join(os.path.dirname(config_path), 'backups')
        except configparser.NoSectionError:
            backup_enabled = False

        if backup_enabled:
            logging.info(f"Otomatik yedekleme etkin. Aralık: {backup_interval}s, Saklama: {backup_retention} dosya.")

        # Yedekleme için son çalışma zamanını durumdan al
        state = get_state(state_path)
        backup_state = state.get('_backup', {})
        last_backup_run_str = backup_state.get('last_run_utc')
        if last_backup_run_str:
            last_backup_run_utc = datetime.fromisoformat(last_backup_run_str)
        else:
            # Eğer daha önce hiç yedekleme yapılmadıysa, döngünün bekleme süresini tamamlaması için
            # betiğin başlangıç zamanını ayarla. Bu, betik başlar başlamaz yedekleme yapmasını önler.
            last_backup_run_utc = datetime.now(timezone.utc)
        save_state(state_path, state)

        while True:
            logging.info("--- Görev kontrol döngüsü çalıştırılıyor ---")
            try:
                state = get_state(state_path)
                config.read(config_path) # Her döngüde config'i yeniden oku
                
                # 'unms' ve 'global' dışındaki tüm bölümleri görev olarak işle
                task_sections = [s for s in config.sections() if s not in ['unms', 'global']]
                for task_name in task_sections:
                    try:
                        monitor_device_health(config, task_name, state)
                        process_device_task(config, config_path, task_name, state)
                    except Exception as e:
                        logging.error(f"'{task_name}' görevi işlenirken beklenmeyen hata oluştu: {e}", exc_info=True)

                # Yedekleme zamanı gelip gelmediğini kontrol et
                if backup_enabled:
                    now_utc = datetime.now(timezone.utc)
                    if (now_utc - last_backup_run_utc).total_seconds() >= backup_interval:
                        logging.info("--- Yedekleme görevi çalıştırılıyor ---")
                        create_backup(config_path, state_path, backup_dir, backup_retention)
                        last_backup_run_utc = now_utc
                        # Durum dosyasındaki yedekleme zamanını güncelle
                        state.setdefault('_backup', {})['last_run_utc'] = now_utc.isoformat()
                    else:
                        logging.debug("Yedekleme aralığı henüz dolmadı. Yedekleme atlanıyor.")
                
                save_state(state_path, state)
            except Exception as e:
                logging.error(f"Ana döngüde genel bir hata oluştu: {e}", exc_info=True)
            logging.info(f"--- Döngü tamamlandı. {check_interval} saniye bekleniyor... ---")
            time.sleep(check_interval)