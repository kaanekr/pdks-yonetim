# Kullanım ve kayıt güvenliği

Programı açınca cihaz IP'si, kullanıcı ve parola girip **Bağlan** seçin. Otomatik bağlantı yoktur.
Adres/kullanıcı hatırlanır; **Şifreyi hatırla** seçeneği Windows kasasını kullanır.
DEMO yalnız yapay verilerle çalışır ve gerçek firmware indirme/yükleme yapmaz.

- **Personel:** kimlik/ad/kart eşlemesi, aktif/pasif ve yeni personel ekleme.
  Kimlik sabittir; kartı değiştirmek eski geçmişi yeni kişiye dönüştürmez.
- **Mesai:** güncel durum, isimli geçmiş ve CSV. Yenilemede seçili satır korunur.
  Yeni olaylar olay anındaki adı saklar; eski kayıtlara geçmiş ad uydurulmaz.
- **Manuel mesai:** personel ve başlangıç/bitiş seçilir. Çakışan/tekrar süre reddedilir.
- **Saat:** yerel Türkiye takvimi kullanılır. Saat kurtarma açık oturumları süre
  eklemeden iptal eden yönetici olayıdır; geçmişi silmez, ayrıca onay ister.
- **Plan/Puantaj/Raporlar:** vardiyalar, gece vardiyası, tam gün izin/tatil,
  mola/tolerans, fazla mesai adayları, günlük/haftalık/aylık Excel/PDF çıktıları.
  Kaydetmeden bırakılan plan taslaktır. Fazla mesai adayı hak edilmiş ücret değildir.
- **PC arşivi:** cihazdan okunan durum/günlük kopyaları ve yerel puantaj ayarları.
  Çevrimdışı rapor alınabilir. Şifre/CSRF saklanmaz; ZIP yedeği doğrulanır.
- **Kalıcı silme:** yalnız desteklenen cihaz sürümünde ayrı onayla personel veya
  tamamlanmış giriş–çıkış çifti/manuel oturum silinebilir. Açık oturum korunur.
  Personel silinince isimli geçmiş korunabilir; geçmiş oturumu silmek ayrı işlemdir.

Silme cihazdaki desteklenen birincil/SD kayıtlarını hedefler; eski PC arşivleri,
ZIP'ler, CSV/Excel/PDF dosyaları kendiliğinden silinmez. Adli güvenli silme garantisi yoktur.
Kişisel verilerin erişim/saklama/yedek/silme yükümlülüğü kullanıcıya aittir.

En fazla 32 personel desteklenir. Eksik çıkışa otomatik süre yazılmaz. Kart UID'si
klonlanabilir. Tek başına resmi puantaj/ücret veya güvenlik kanıtı olarak kullanmayın.
Güncelleme için [ayrı kılavuz](GUNCELLEME_V1_5.md) içindeki adımları izleyin.
