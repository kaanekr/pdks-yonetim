# Uygulama ve cihaz yazılımı güncellemesi

## Windows programı

1. İlk geçişte yeni `PDKS_Yonetim.exe` dosyasını indirin. Eski programı kapatın;
   yenisini aynı konuma koyup çalıştırın. İlk manuel geçişte kalan eski dosyayı
   kendiniz silebilirsiniz; sonraki otomatik güncellemelerde geçici eski program
   dosyası kendiliğinden silinir.
2. Sonraki sürümlerde **Güncelleme → Güncellemeleri kontrol et** seçin.
3. **Güncellemeyi indir** dosyayı doğrular; hiçbir şey kurmaz.
4. **Programı güncelle…** ayrıca onay ister. Kaydedilmemiş formları tamamlayın.
   Program kapanır; ayrı yardımcı eski işlemin kapanmasını doğrular, eski sürümü
   yalnız sağlık kontrolü boyunca geçici tutar ve yeni sürümü atomik olarak koyar.
   Yeni sürüm açılmazsa önceki sürüm geri alınır. Sağlık kontrolü başarılıysa geçici
   eski sürüm silinir; eski sürüm geride bırakılmaz.

Korumalı/izin verilmeyen klasörde işlem reddedilebilir; yönetici yetkisi kendiliğinden
istenmez. Otomatik temizlik yalnız eski geçici sürüm dosyalarını ve başarılı güncelleme
iş klasörlerini siler; başka dosyalara dokunmaz. PC arşivi ve bağlantı ayarları
değiştirilmez. Yalnız geri alma da başarısız olursa eski sürüm yedeği manuel kurtarma
için kalabilir; güncelleme iş klasöründeki `update.log` incelenir.

## İlk cihaz yazılımı kurulumu — bir kez USB

Eski cihaz yazılımı kablosuz güncelleme kabul etmez. İlk geçiş, cihaza uygun **özel**
USB kurulum yazılımı ve ayrıca açık yükleme onayı gerektirir. Bu özel yazılım yerel
cihaz/parola bilgilerini içerebilir; GitHub'a veya başkasına gönderilmez. Mevcut
yönetim bilgileri cihazda korunur. Normal cihaz yazılımı alanı güncellenir; kayıtlar
silinmez.

USB kurulumundan önce güncel kayıt dışa aktarımı/yedek ve doğru cihaz/hedef kontrolü
zorunludur. Yeni kayıt biçimleri kullanıldıktan sonra eski cihaz yazılımına dönmeyin.

## Sonraki kablosuz cihaz yazılımı güncellemeleri

1. İnternet varken **Güncellemeleri kontrol et** ve **Güncellemeyi indir** seçin.
2. Programı açık tutup cihazla aynı güvenilir Wi-Fi/LAN'a bağlanın. Cihazın kurulum
   ağında internet olmayabilir.
3. Bağlanın, **Yenile** ile güncel durumu okuyun. Açık mesai oturumu bulunmamalıdır.
4. **Cihaz yazılımını güncelle…** seçin ve ayrı onay verin.
5. Program önce tutarlı durum/günlük kopyasını PC arşivine alır ve seçtiğiniz yedeğe
   yazar. Bu adım başarısızsa cihaz yazılımı gönderilmez.
6. Yeni yazılım bir kez gönderilir. Cihaz hedefi, sürümü, mevcut kayıt durumunu,
   onayı, tam boyutu, özeti ve imzayı kontrol eder. Doğrulama tamamlanmadan yeni
   yazılım etkinleştirilmez. Yükleme sırasında kart okumaları durur.
7. Cihaz yeniden başlayınca **Yenile** ile sürümü kontrol edin. Elektriği kesmeyin.
   Sonuç belirsizse körlemesine yeniden yüklemeyin.

Yalnız cihaz yazılımı alanı güncellenir; kayıtlar, Wi-Fi, kimlik bilgileri ve diğer
dosyalar korunur. PC yedeği cihazın tam flash/dosya sistemi yedeği değildir ve cihaza
geri yükleme özelliği yoktur. Güncelleme için port yönlendirmesi açmayın.

Cihazın web paneli yeni sürümlerde cihaz yazılımının içinde gelir; kablosuz güncelleme
ile birlikte yenilenir ve kayıt alanına ayrıca dosya yüklenmez. 1.5.4 Windows ve cihaz
arayüzleri sadeleştirilmiştir; ham kart kimliği, olay sırası, sürüm/özet gibi teknik
alanlar ana ekranlarda gösterilmez.
