# Derleme ve yayıncı notları

## Yerel derleme

Windows x64 / Python 3.14 ve sabitlenmiş bağımlılıklar ile doğrulanmıştır.

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt -r windows_app/requirements-build.txt
$env:PLATFORMIO_CORE_DIR="$PWD\.platformio"
powershell -NoProfile -ExecutionPolicy Bypass -File tools/check.ps1 -Target Core
powershell -NoProfile -ExecutionPolicy Bypass -File tools/check.ps1 -Target PublicFirmware
.venv\Scripts\python.exe -m PyInstaller --onefile --windowed --name PDKS_Yonetim --add-data "windows_app/assets:windows_app/assets" windows_app_launcher.py
```

Native testler için `g++`, panel testi için Node.js gerekir. Derleme komutları cihazı
bağlamaz/yüklemez. Bazı native testler ArduinoJson'un özel TX hedefinin libdeps yolunu
kullanır; gerekirse önce `platformio pkg install -e nodemcuv2_sd_soft_tx` ile indirin.
`tools/check.ps1 -Target All` eski hedefleri de derler; bunlar mevcut kablolamaya yüklenmez.

Herkese açık kaynakta `Config.h` temizlenmiştir. İlk özel bootstrap için yerel,
git-ignored `include/Secrets.h` dosyasında `PDKS_LOCAL_CREDENTIALS_CONFIGURED=1` ve
`PDKS_ADMIN_USER`, `PDKS_ADMIN_PASSWORD`, `PDKS_AP_SSID`, `PDKS_AP_PASSWORD` tanımlanır.
Gerçek cihazın mevcut bilgileri korunur; bilinen örnek şifrelerle sahaya çıkılmaz.
Özel imaj yalnız `nodemcuv2_sd_soft_tx` hedefinde, ayrı USB yükleme onayıyla kullanılır.
Public hedefin parolaları boştur ve mevcut özel kimlik dosyası gerekir.

## İmza ve güven

Yayıncı özel anahtarı Windows DPAPI ile proje dışında korunur.
`tools/release_signing.py` kayıp anahtarı kendiliğinden yeniden üretmez.
Başka makinede yeni anahtar üretmek mevcut uygulama/firmware'in güvenini değiştirmez;
ilk güven kurulumu ve anahtar yedeği/taşıma planı ayrıca yönetilmelidir.
Özel anahtar GitHub/CI deposuna konmaz. Public PEM ve firmware anahtar başlığı aynı
RSA-2048 anahtarını temsil eder. Mevcut public PEM SHA256:
`fddf7d1e8a3377fe1e2400a0476cc37a946da67968eb6f9a44c81620cc2805d9`.

`tools/prepare_github_release.py` allowlist ile temiz kaynak ağacı ve imzalı varlıkları
hazırlar; kendiliğinden yayın veya cihaz yüklemesi yapmaz. Orijinal özel Config,
eski imajlar/teslim geçmişi, gerçek kayıtlar ve özel USB bootstrap dışarıda kalır.
Sürüm/sıra alanları uygulama ve public firmware'de birlikte artırılmalıdır.
`tools/publish_public.py --source <temiz-agac> --assets <dort-imzali-varlik> --publish`
ilk boş depoyu ve sürümü açıkça yayımlar; var olan kaynak geçmişini değiştirmeyi reddeder.
Sonraki kaynak commit'leri normal Git incelemesiyle, sürüm varlıkları `gh release`
ile ayrıca yönetilir; yayımlanmış sürüm dosyaları değiştirilmez.
Sürüm kataloğunun ve firmware metaverisinin doğrulayıcısı `windows_app/updates.py`
ve `update_contract.py` dosyalarıdır. Kataloğun URL'leri sürüm etiketine ve sabit
dosya adlarına bağlanır; değiştirilmiş/yönlendirilmiş HTTP varlıklarına güvenilmez.

Yerel testler gerçek donanım kabulü değildir. İlk USB, gerçek OTA, enerji kesintisi
ve kayıtların korunması saha kabulünde ayrıca doğrulanmadan bu iddia kurulmaz.
