# İmzalı uygulama ve firmware güncellemesi

## Windows programı

1. İlk geçişte GitHub Releases'ten yeni `PDKS_Yonetim.exe` dosyasını indirin.
   Eski programı kapatın; yenisini aynı konuma koyup çalıştırın. İlk manuel geçişte
   kalan eski dosyayı kendiniz silebilirsiniz; sonraki otomatik güncellemelerde
   geçici eski EXE kendiliğinden silinir.
2. Sonraki sürümlerde **Güncelleme → Sürümleri kontrol et** seçin.
3. **Dosyaları indir / hazırla** dosyaları doğrular; hiçbir şey kurmaz.
4. **Windows programını güncelle** ayrıca onay ister. Kaydedilmemiş formları tamamlayın.
   Program kapanır; ayrı yardımcı eski işlemin kapanmasını doğrular, eski EXE'yi
   yalnız sağlık kontrolü boyunca geçici tutar ve atomik değiştirme yapar.
   Yeni EXE'nin izole sağlık kontrolü başarısızsa önceki EXE geri alınır.
   Sağlık kontrolü başarılıysa geçici eski EXE silinir; eski sürüm geride bırakılmaz.

Korumalı/izin verilmeyen klasörde işlem reddedilebilir; yönetici yetkisi kendiliğinden
istenmez. Otomatik temizlik yalnız `PDKS_Yonetim.previous.<kimlik>.exe`/`.tmp`
dosyalarını ve eski güncelleme iş klasörlerini siler; başka dosyalara dokunmaz.
PC arşivi ve bağlantı ayarları değiştirilmez. Yalnız geri alma da başarısız olursa
eski EXE yedeği manuel kurtarma için kalabilir; güncelleme iş klasöründeki
`update.log` incelenir.

## İlk OTA kurulumu — bir kez USB

Eski cihaz firmware'i kablosuz imzalı yükleme kabul etmez. İlk geçiş, mevcut cihaza
uygun **özel** `1.5.0` USB bootstrap firmware'i ve açık yükleme onayı gerektirir.
Bu imaj yerel cihaz/parola bilgilerini içerebilir; GitHub'a veya başkasına gönderilmez.
Normal program alanı güncellenir; dosya sistemi silinmez veya yüklenmez.

İlk özel açılış mevcut kullanıcı/AP bilgilerini özel `/device-auth.json` dosyasına
geçirir; mevcut geçerli dosyayı değiştirmez. Hatalı dosya varsa varsayılanlara dönmez.
Bu dosya yoksa herkese açık firmware yönetim erişimini kapalı tutar.
USB kurulumu öncesi güncel kayıt dışa aktarımı/yedek ve doğru hedef kontrolü zorunludur.
Yeni kayıt biçimleri kullanıldıktan sonra eski firmware'e dönmeyin.

## Sonraki kablosuz firmware güncellemeleri

1. İnternet varken sürümü kontrol edip dosyaları indirin.
2. Programı açık tutup cihazla aynı güvenilir Wi-Fi/LAN'a bağlanın.
   Cihazın kurulum AP'sinde internet olmayabilir.
3. Bağlanın, **Yenile** ile güncel durumu okuyun. Açık mesai oturumu bulunmamalıdır.
4. **Cihaz firmware'ini kablosuz güncelle** seçin ve ayrı onay verin.
5. Program önce tutarlı durum/günlük kopyasını PC arşivine alır ve seçtiğiniz ZIP'e
   yedekler. Bu adım başarısızsa firmware gönderilmez.
6. İmzalı dosya bir kez gönderilir. Cihaz hedefi, sürümü, mevcut kayıt sürümlerini,
   nonce/CSRF/onayı, tam boyutu, özeti ve imzayı kontrol eder. Doğrulama tamamlanmadan
   yeni firmware etkinleştirilmez. Yükleme sırasında kart okumaları durur.
7. Cihaz yeniden başlayınca **Yenile** ile firmware sürümünü kontrol edin.
   Elektriği kesmeyin. Belirsiz yanıt durumunda körlemesine yeniden yüklemeyin.

Yalnız uygulama/program alanı (`U_FLASH`) güncellenir; LittleFS, Wi-Fi, kimlik bilgileri,
personel ve günlük dosyaları korunur. PC ZIP yedeği cihazın tam flash/dosya sistemi
yedeği değildir ve cihaza geri yükleme özelliği yoktur. OTA için port yönlendirmesi açmayın.

## İmza protokolü

Depo: `kaanekr/pdks-yonetim`. Sürüm kataloğu tam baytları üzerinden imzalanır:
`update-manifest.json` + ham 256 bayt `update-manifest.sig`.
Varlıklar `PDKS_Yonetim.exe` ve `PDKS_Firmware_TX_SD.signed.bin` dosyalarıdır.
Firmware: ham BIN + RSA-2048 PKCS1v15/SHA256 imza + little-endian uint32(256).
Firmware metaverisi ayrıca hedef/sürüm/sıra/boyut/SHA256 alanlarına bağlanan imza taşır.
Güven anahtarı uygulama ve firmware'e gömülüdür; GitHub erişim anahtarı gerekmez.
