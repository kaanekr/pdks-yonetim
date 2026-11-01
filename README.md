# PDKS Yönetim

ESP8266 RFID mesai terminali ve yerel Windows yönetim programı. Personel/kart yönetimi,
isimli giriş–çıkış kayıtları, manuel mesai, puantaj, vardiya/izin/tatil planlama,
Excel/PDF raporları, PC arşivi ve ayrı onaylı kalıcı silme desteklenir.

## İndir ve çalıştır

[Son sürüm](https://github.com/kaanekr/pdks-yonetim/releases/latest) içindeki
`PDKS_Yonetim.exe` Windows 10/11 x64 üzerinde Python kurulmadan çalışır.
`--demo` ile gerçek cihaza bağlanmadan deneyebilirsiniz.

**Güncelleme** sekmesi GitHub sürümünü yalnız düğmeye bastığınızda kontrol eder.
Uygulama ve firmware dosyaları yayıncı RSA-2048/SHA256 imzası ve SHA256 özetiyle
doğrulanır. İndirme ve kurulum ayrıdır; kendiliğinden güncelleme veya POST tekrarı yoktur.
Bu uygulama içi imzadır; EXE için Windows Authenticode sertifikası değildir.

**Mevcut eski firmware için ilk OTA kurulumu bir kez özel USB kurulumu gerektirir.**
Herkese açık firmware boş/yeni cihazın ilk kurulum imajı değildir. Sonraki firmware
güncellemeleri program üzerinden cihazla aynı güvenilir Wi-Fi/LAN üzerindeyken yapılır.

- [Güncelleme ve ilk kurulum](docs/GUNCELLEME_V1_5.md)
- [Kullanım ve kayıt güvenliği](docs/KULLANIM_PUBLIC.md)
- [Yayıncı / derleme kılavuzu](docs/YAYINCI.md)

## Donanım hedefi

Yalnız `nodemcuv2_sd_soft_tx`: NodeMCU ESP8266 4 MB, RC522, PCF8563 RTC,
SSD1306 128×32 OLED, microSD, TX buzzer. SD MISO **D0/GPIO16**, RFID MISO
**D6/GPIO12**, buzzer **TX/GPIO1**, UART kapalıdır. SCK D5 ve MOSI D7 ortaktır;
RFID CS D8, SD CS D3, I2C SDA D2/SCL D1, yeşil LED direnç üzerinden D4.
RC522/OLED/RTC/buzzer 3,3 V ister. Besleme türünü modülünüzden doğrulayın.
Eski D0-buzzer hedeflerini bu kablolamaya yüklemeyin.

## Güvenlik ve sınırlar

- Program açılışta GitHub'a veya cihaza bağlanmaz. Otomatik yenileme isteğe bağlı, salt okunurdur.
- IP/kullanıcı hatırlanır; isteğe bağlı şifre Windows Kimlik Bilgisi Yöneticisi'nde saklanır.
- Cihaz HTTP/Basic kullanır, TLS yoktur. Yalnız güvenilen yerel ağ kullanın;
  yönetim portunu internete açmayın. İmzalı firmware aktarımı yerel HTTP parolasını şifrelemez.
- UID klonlanabilir; bu prototip sertifikalı PDKS/kapı güvenliği veya maaş hesaplama sistemi değildir.
- Gerçek personel/veri, bağlantı ayarları, özel anahtar ve özel USB imajı bu depoda yoktur.
- Güncellemeler LittleFS'i biçimlendirmez. `uploadfs`, erase ve reset imajları normal güncelleme değildir.
- Donanım üzerinde OTA/elektrik kesintisi kabul testi ayrıca yapılmalıdır; yerel testler bunun yerine geçmez.

Kaynak derleme için sabitlenen bağımlılıklar `requirements-dev.txt`,
`windows_app/requirements-build.txt` ve `platformio.ini` dosyalarındadır.
Üçüncü taraf yazı tipi lisansı `windows_app/assets/OFL.txt` içindedir.
Paketlenen bağımlılıkların lisansları `docs/THIRD_PARTY_LICENSES/` altındadır.
