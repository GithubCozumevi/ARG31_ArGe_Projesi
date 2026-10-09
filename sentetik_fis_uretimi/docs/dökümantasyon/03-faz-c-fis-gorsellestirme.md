# Faz C — Fiş Görselleştirme

> Fatura verisinden gerçek bir fişe benzeyen PNG üretir. Görü tabanlı bir
> modelin girdisi olacağı için görselde yazan her şey veriyle tutarlı olmak
> zorundadır.

![Faz C boru hattı](../diagrams/faz-c.png)

*İki Jinja2 şablonundan biri faturaya deterministik olarak atanır, HTML
üretilir, Playwright/Chromium sayfayı açıp `.receipt-container` öğesini
kırparak PNG'yi yazar.*

## Çalıştırma

```bash
python -m faz_c_fis_gorsellestirme.fis_uret --input-json data/faturalar.json --output-dir data/fisler
```

Faz B'ye bağlı değildir; `faturalar.json` hazır olduğu anda çalıştırılabilir.
Açıklama metnini de içeren sürümü basmak istersen girdi olarak
`faturalar_aciklamali.json` verebilirsin (şablonlar açıklamayı basmaz, ama alt
kümeyi seçmiş olursun).

Yaklaşık **93 ms/fiş**; 25 bin fiş 40 dakikadan az sürer.

## Nasıl çalışır?

```
faturalar.json
     │
     ▼
 fis_uret.py
   ├─ baglam_kur()        şablona gidecek alanları hesaplar
   ├─ sablon_sec()        kayit_id'den deterministik şablon ataması
   ├─ Jinja2              HTML üretir
   └─ Playwright/Chromium sayfayı açar, .receipt-container'ı kırpar
     │
     ▼
 data/fisler/<kayit_id>.png
```

Şablonlar: `fis_sablon_1.html` (yazarkasa fişi görünümü) ve
`fis_sablon_2.html` (e-arşiv fatura görünümü). Her ikisinde de
**`.receipt-container` sınıfı zorunludur** — ekran görüntüsü bu seçiciyle
alınır.

Dosya adı `kayit_id`'dir. `fatura_no` tasarım gereği mükerrer olabildiği için
görseli etiket dosyasına bağlayan tek güvenli anahtar odur.

Var olan PNG atlanır (resume); `--yeniden` ile zorlanır. Her fiş kendi
try/except'i içindedir, tek bozuk kayıt koşuyu düşürmez.

## Gösterim aritmetiği — en kritik kısım

Fişin **kendi içinde tutarlı olması şarttır**:

```
brut_birim = birim_fiyat × (1 + kdv_oranı/100)          KDV dahil birim fiyat
brut_tutar = birim_fiyat × miktar × (1 + kdv/100)       iskonto öncesi satır
indirim    = brut_tutar × iskonto_oranı/100
brut_tutar − indirim = satir_toplam                     ve toplamı genel_toplam
```

Hesap `fis_uret.py:kalem_gosterimi` içindedir, şablonlarda değil. **İki şablon
aynı aritmetiği göstermek zorundadır**; farklı gösterirlerse model tutarsızlığı
fiş tipiyle karıştırır.

### Neden böyle yazıldı

Eski şablon fiyat sütununa `satir_toplam`'ı (iskonto zaten düşülmüş) basıp
altına bir de İNDİRİM satırı ekliyordu — aynı iskonto iki kez görünüyor, fiş
toplamı tutmuyordu. İndirim tutarı da yanlıştı, çünkü iskonto sonrası tutar
üzerinden hesaplanıyordu.

Ölçüldü: 1.685 iskontolu kalemde indirim ortalama **510,86 TL eksik**
yazılıyordu ve 120 bin faturanın **%56,8'i** iskontolu kalem içeriyor. Yani
temiz fişlerin yarısından fazlası görselde bozuk duruyordu. Bu bir görüntü
kusuru değil veri seti sorunudur: enjekte ettiğimiz gerçek anomaliler de tam
olarak "hesap tutmuyor" biçimindedir ve kendi ürettiğimiz sahte sinyalin içinde
boğuluyorlardı.

**Bilinçli bir karar:** indirim, "brüt eksi satır toplamı" olarak
hesaplanmıyor. Öyle yapılsaydı her fiş kusursuz denkleşirdi, ama enjekte edilen
tutarsızlık indirim satırının içine emilip görünmez olurdu. Dürüst formül temiz
veride birebir uzlaşır, kurcalanmış veride uzlaşmaz — istenen davranış budur.

**Yuvarlama:** brüt tutar tek seferde yuvarlanır. Birim fiyatı önce yuvarlayıp
miktarla çarpmak 5 kuruşa kadar sapma üretiyordu. Ölçüldü (8.358 temiz kalem):
sapma %87,2'de tam sıfır, %100'ünde ≤ 1 kuruş.

Ölçülen sonuç: temiz fişte yanlış "hesap tutmuyor" alarmı **%56,8 → %0,01**.

## Anomali görünürlüğü

Bir anomali etiketinin fişte görünür karşılığı yoksa o etiket öğrenilemez
gürültüdür. Şablonlar bu gözle tasarlanmıştır.

| durum | türler |
| --- | --- |
| Aritmetik iz bırakır (%100) | `ara_toplam`, `satir_toplami`, `kdv_tutari`, `genel_toplam`, `footer_kismi` |
| Başka alandan görünür | `gecersiz_kimlik_no` (VKN), `gelecek_tarihli` (tarih), `yasakli_kategori` ve `is_kolu_kategori_uyumsuzlugu` (kalem adı), `limit_asimi` (tutar), `mukerrer_fis_yukleme` / `fatura_no_cakismasi` (fiş no) |
| Aritmetik iz bırakmaz | `ondalik_kaymasi`, `dusuk_ondalik_kaymasi` — gerçek birim fiyatı değiştirdikleri için aşağısı tutarlı kalır; tespiti "bu ürüne bu fiyat olmaz" semantik ekseninde |

Bunu sağlayan iki şablon kararı:

- **ARA TOPLAM satırı zorunludur.** `footer_kismi` anomalisi KDV hariç toplam
  ile KDV toplamından **yalnızca birini** bozar. Vergisiz tutar basılmazsa o
  dalın yaklaşık yarısı fişte hiçbir iz bırakmaz — ve `footer_kismi` 25k
  setinin en büyük anomali türüdür.

- **Birim fiyat satırı her kalemde basılır.** Koşullu yapılıp tek adetli
  kalemlerde gizlenirse birim fiyat fişten tamamen kaybolur ve `ara_toplam` ile
  `ondalik_kaymasi` anomalileri görselde öğrenilemez hâle gelir.

## Şablon seçimi

`sablon_sec` şablonu `kayit_id`'nin md5 özetinden seçer. Rastgele değildir:
yarım kalan bir koşu devam ettirildiğinde aynı faturanın aynı şablonu alması
gerekir.

Seçim **etiketten bağımsızdır** ve bu ölçülmüştür (120k kayıt): dağılım
%49,94 / %50,06, anomali oranı iki grupta 0,2572 ve 0,2576, kategori kırılımı
dengeli. Fiş tipini anomaliye bağlamak modele kısayol öğretirdi.

Kalem sayısına veya tutara göre format atamak leakage **değildir** — ikisi de
fişin üstünde zaten yazar, model o bilgiyi görselden görür.

## Biçim ayrıntıları

- Tutarlar `tutar` filtresinden geçer: daima iki ondalık, Türkçe ayraç
  (`5.613,28`). Ham float basmak `*1101.0` gibi tek ondalıklı değerler
  üretiyordu ve bu, ondalık kaydırma anomalisinin görsel imzasını bulandırıyordu.
- Fiş saati veride yoktur, gerçek fişte vardır; `kayit_id`'den türetilir
  (08:00-20:59). Kararlıdır ve etiketle korelasyonsuzdur.
- `fis_sablon_1.html` büyük harf dönüşümü uygular; `lang="tr"` sayesinde
  Türkçe büyütme doğru çalışır (`hakiki` → `HAKİKİ`, noktasız I hatası yok).

## Yapılmayan: el yazısı şablon

Türkiye'de küçük esnafın elle doldurduğu "Perakende Satış Fişi" formu için
üçüncü bir şablon hazırlandı ama **eklenmedi**. Gerekçe sayısaldır: 25k
setinde tek kalemli taksi faturası yalnızca 88 adet (%0,35) ve bu formatta KDV
satırı bulunmadığı için 4 etiket görünmez kalacaktı. Kayıp önemsiz, ama kazanç
da öyle — 88 fiş yeni bir görsel mod öğretmez.

Doğru zamanı bir sonraki Faz A üretimidir: fiş formatı orada bir **alan**
olarak tutulursa (`dijital` / `el_yazisi`) enjektör el yazısı fişe gösterilemeyen
anomalileri hiç enjekte etmez ve sorun kökten çözülür.
