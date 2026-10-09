# Faz A — Fatura Verisi Üretimi

> Sentetik fatura kayıtları üretir, içine kontrollü anomaliler enjekte eder,
> bağımsız bir doğrulayıcıyla etiketler. Çıktısı Faz B ve Faz C'nin girdisidir.

![Faz A boru hattı](../diagrams/faz-a.png)

*`main.py` dört adımı sırayla yürütür: temiz fatura üretimi, anomali
enjeksiyonu, doğrulama ve açıklama kategorisi ataması. `schema.py` zincirin
tamamı tarafından import edilir. Çıktı ikilisi ayrılır: model girdisi ve
ground-truth etiketler.*

## Çalıştırma

```bash
python -m faz_a_fis_uretimi.main --count 120000 --anomali-orani 0.25 --output-dir data --filename faturalar
python -m faz_a_fis_uretimi.rapor_analiz --output-dir data --filename faturalar   # üretim sonrası teşhis
```

`--anomali-orani 0.25` bilinçli bir seçimdir: azınlık sınıflarını (reddedilen
ve manipülatif masraflar) kuralı bozmadan büyütmenin doğru kaldıracı budur.

Registry mimarisi geldikten sonra fatura **elenmiyor**, dolayısıyla `--count`
doğrudan nihai kayıt sayısıdır.

## Modüller ve akış

```
Veri kaynakları (CSV)
   firma_registry.csv · urun_verileri/ · hizmet_verileri/ · anomali_verileri/
        │
        ▼
  generators/field_generator.py     1. Temiz fatura oluşturur
        │
        ▼
  generators/anomaly_injector.py    2. Anomali enjekte eder
        │
        ▼
  validators.py                     3. Kontrol eder ve etiketler
        │
        ▼
  generators/aciklama_uretici.py    4. Açıklama kategorisi atar
        │
        ▼
  main.py                           faturalar.json + faturalar_etiketler.json
```

`schema.py` bu zincirin tamamı tarafından kullanılır: `Fatura`, `FaturaKalemi`
Pydantic modelleri, `IsKolu` ve `HarcamaKategorisi` enum'ları, anomalili alt
sınıflar (`AnomaliliFatura`, `AnomaliliFaturaKalemi` — sahte toplam ve KDV
taşırlar) ve politika sabitleri (`POLICY_YASAKLI_KATEGORILER`, `KDV_ORANI_MAP`,
`IS_KOLU_KATEGORILERI`).

## Harcama limitleri: `data/politika_limitleri.json`

Tutar limitleri şirketten şirkete değişen bir politika parametresi olduğu için
kodda değil veri dosyasında durur; okuyucusu **`politika.py`**'dir ve hem
`validators.kalem_limit_asimi_mi` hem `anomaly_injector.limit_asimi_anomali_uret`
aynı kaynaktan besleniyor. Limit `birim_fiyat`a uygulanır (satır ya da fatura
toplamına değil) ve iki katmanlıdır: `(kategori, birim)` → `kategori` → limit yok.
Birim katmanı, tek limitin anlamsız kaldığı kategoriler içindir; danışmanlıkta
saatlik iş ile aylık iş, ulaşımda Km ile Ton aynı sayıyla yönetilemez.

Limitli kategori sayısı 11'dir (yasaklı kategoriler hariç: onlar zaten
`yasakli_kategori` ile etiketlendiği için ikinci bir etikete gerek yok).

**Değiştirirken uyulacak kural:** limit, o kategorinin ürettiği en yüksek birim
fiyatın (`FIYAT_ARALIGI_*` + `FIYAT_TASMA_ORANI`) üstünde kalmalı. Altına inerse
hiç anomali enjekte edilmemiş temiz faturalar da `limit_asimi` etiketi alır ve
anomali oranı kendiliğinden şişer. `field_generator._politika_limitlerini_dogrula`
bunu import anında denetleyip ekrana yazar; dosya yoksa ya da bozuksa `politika.py`
`RuntimeError` fırlatır (sessizce limitsiz üretim yapmaz).

Limit dışarıda kaldığı için tek komutla üretim akışı değişmedi; `main.py`'nin
ayrıca bir politika parametresi yoktur.

## Firma kimliği: registry mimarisi

Satıcı kimliği tek bir kalıcı kaynaktan gelir: **`data/firma_registry.csv`**.
Bu dosya OpenStreetMap'ten çekilmiş gerçek işletme adlarını, sentetik dolguyu
ve şahıs şirketi havuzunu harmanlar; her kayda benzersiz bir VKN/TCKN atar.
Seed'lidir ve idempotenttir.

```bash
# BİR KEZ, ya da OSM verisi yenilendiğinde
python -m faz_a_fis_uretimi.firma_adlari_osm_cek --yeniden --hedef 3000 --bekleme 10
python -m faz_a_fis_uretimi.firma_registry_olustur --hedef-per-iskolu 1500 --osm-pay 0.8 --seed 42
```

Registry'nin taşıdığı değişmez: **bir VKN, tek bir iş koluna aittir.** Fatura
üretimi de anomali enjeksiyonu da bunu bozmaz.

Bu mimariden önce iş kolu, firma unvanına gömülü bir sektör kelimesinden geri
okunuyordu. O mekanizma **emeklidir**; `is_kolu` artık registry'de açık bir
kolondur. `rastgele_firma_adi` hâlâ duruyor ama yalnızca registry oluşturulurken
sentetik ad üretmek için çağrılıyor.

`is_kolu` model girdisine dahildir ve bu leakage değildir: gerçek bir masraf
sisteminde satıcının sektörü zaten bilinen bir bilgidir.

## Fatura içeriğinin tutarlılığı

Ürünler kategoriye göre havuzlardan seçilir, ancak birkaç kısıt gerçekçiliği
korur:

- **`MUTFAK_KISITLARI`** — restoranın mutfağıyla sattığı yemeğin uyumu. Nötr
  bir restoran sushi satmaz.
- **`FIRMA_ADI_KISITLARI`** — aynı fikrin restoran dışı iş kollarına
  genellenmesi: kuruyemişçi kendi yelpazesini satar, kırtasiyeci ofis masası
  satmaz.
- **`IS_KOLU_KALEM_TAVANI`** — kalem sayısı iş koluna bağlıdır. Ulaşımda tek
  kalem, markette sekize kadar.

Bu kısıtlar **yalnızca temiz üretimde** uygulanır. Anomali enjektörleri bunları
bilerek deler, çünkü "bu dükkân bunu satmaz" tespit edilmesi istenen bir
anomali türüdür.

Fiyatlar üç katmanlıdır: `(kategori, ürün tipi)` → `(kategori, birim)` →
`kategori`. Ürün tipi katmanı **kategori anahtarlı olmak zorundadır**; bkz.
aşağıdaki tuzaklar.

## Anomali türleri (14)

`generators/anomaly_injector.py` içinde `ANOMALI_FONKSIYONLARI` sözlüğünde
tanımlıdır. Kabaca üç eksende toplanırlar:

| eksen | türler |
| --- | --- |
| Aritmetik / belge | `ara_toplam`, `satir_toplami`, `kdv_tutari`, `genel_toplam`, `footer_kismi`, `ondalik_kaymasi`, `dusuk_ondalik_kaymasi` |
| Kimlik / tarih / tekrar | `gecersiz_kimlik_no`, `gelecek_tarihli`, `mukerrer_fis_yukleme`, `fatura_no_cakismasi` |
| Politika / makullük | `yasakli_kategori`, `limit_asimi`, `is_kolu_kategori_uyumsuzlugu` |

İki nokta özellikle önemli:

**Aynı fatura numarası iki senaryoya karşılık gelir.** `mukerrer_fis_yukleme`
aynı fişin ikinci kez yüklenmesidir; `fatura_no_cakismasi` iki farklı fişin
aynı numarayı taşımasıdır. Üreten fonksiyon `fatura_no_tekrari_uygula`, ürettiği
etiketler bu ikisidir — adları karıştırma. Çift **aynı iş kolundan** seçilir ki
registry değişmezi bozulmasın.

**Politika ekseni ile satıcı ekseni ayrıdır.** `yasakli_kategori` "şirket bu
gideri ödemez" demektir; `is_kolu_kategori_uyumsuzlugu` "bu dükkân bunu zaten
satmaz" demektir. Restoranda alkol, markette sigara yalnızca birincisini
tetikler. Makullük bilgisi `anomali_urunler.csv` içinde **ürün bazında**
(`makul_is_kollari` kolonu) tutulur; kategori bazlı muafiyet yapılmaz, yoksa
markette casino çipi de muaf olurdu.

## Doğrulama ve union etiketleme

`validators.py` enjektörden **bağımsız** çalışır: enjekte edilen anomaliyi
bilmez, faturaya bakıp hangi kuralların ihlal edildiğini kendisi tespit eder
(`kural_ihlali_turlerini_tespit_et`).

Nihai etiket, enjekte edilen tür ile doğrulayıcının bulduğu türlerin
**birleşimidir**. Bu kasıtlıdır: bir anomali enjekte ederken yan etkiyle başka
bir kural da ihlal edilebilir ve etiket bunu yansıtmalıdır. Bu yüzden bir
faturanın birden fazla anomali etiketi olabilir.

`kalem_is_kolu_uyumu_dogrula` iki ayrı şeye bakar: kategorinin iş koluna izinli
olup olmadığı ve `yasakli_kalem_saticiya_makul_mu`.

## Leakage ayrımı — `main.py`

```python
fatura_to_dict()   →  MODEL GİRDİSİ   (kayit_id, fatura_no, tarih, satıcı,
                                       is_kolu, alıcı, toplamlar, kalemler)
etiket_to_dict()   →  GROUND TRUTH    (is_anomali, anomali_turleri,
                                       aciklama_kategorisi)
```

Bu ikisini birleştirme. `onay_durumu` burada **yoktur**; o etiket Faz B'nin
sonunda ayrı bir modülde üretilir (bkz. `04-etiketler.md`).

`main()` içindeki adım sırası anlamlıdır: önce temiz üretim, sonra enjeksiyon,
sonra doğrulama, en son açıklama kategorisi ataması. Sırayı değiştirme.

## Tuzaklar

- **CSV yolu değişirse üretim sessizce bozulur.** Bir dizin yeniden
  adlandırıldığında iki yükleyici sessizce boş dönmüş, yasaklı ürün havuzları
  22 kalemden 4 kaleme düşmüş ve satıcı ekseni ayrımı devre dışı kalmıştı.
  Artık `_csv_yok_uyar` gürültülü uyarır. **Her yeniden üretimden sonra**
  yasaklı ürün çeşitliliğini ve `yasakli_kategori` ile
  `is_kolu_kategori_uyumsuzlugu` örtüşmesini (~%82 olmalı, %100 değil) doğrula.

- **Fiyat aralığı ile anomali eşiği aynı sayı ekseninde yaşar.** Kategoriden
  bağımsız yazılan bir ürün tipi bandı, elektroniğin düşük tutar eşiğini
  delmiş ve temiz faturalara sahte anomali etiketi kazandırmıştı (anomali oranı
  0,2586 → 0,2764). `_fiyat_araliklarini_dogrula()` import anında denetler;
  yeni aralık yazarken çıktısına bak.

- **Doğal fatura no çakışması olabilir.** 120k üretimde birkaç tane beklenir
  (doğum günü paradoksu). Tespit `(vkn, fatura_no)` çiftine baktığı için
  bunları anomali saymaz — doğrusu budur, farklı satıcılar aynı numarayı
  kullanabilir.

- **Dar mutfak ürünleri genel havuzda yoktur.** Bu kasıtlıdır: hem nötr
  restoranın sushi satmasını önler hem de havuz uzunluğuna bağlı iş kolu
  ağırlıklarını sabit tutar. Dar bölümü genel havuza eklersen fatura dağılımı
  kayar.

- **Firma adı kısaltma yalnızca sentetik adlara uygulanır.** Kısaltma regex'i
  sektör kelimelerini de sökebildiği için OSM'den gelen gerçek adları
  bozuyordu. OSM adı olduğu gibi kullanılır.

- **Türkçe desen yazarken ek ve yumuşama biçimlerini düşün.** `çerçeve` deseni
  `Çerçevesi`ni yakalamaz. Bir günde üç kez ısırdı.

## Ürün havuzu bakımı

Ham ürün verisi tüketiciye yönelik bir kaynaktan geldiği için kurumsal masraf
fişinde bulunmayacak ürünler içerir. `urun_kurumsal_filtre.py` bunları
kategori bazlı kara/beyaz listelerle ayıklar (24.665 → ~4.100 satır).

Varsayılan olarak **rapor** modunda çalışır; `--uygula` yedek alıp yazar.
Havuz değiştikten sonra **yeniden üretim gerekir**, aksi halde
`faturalar.json` eski havuzu yansıtmaya devam eder.

Bu iş neden önemli: pilot çalışmalarda üretilen açıklamaların kalitesizliği
prompt'tan değil faturadan geliyordu. Model kurumsal bir gerekçe yazamıyordu
çünkü ortada kurumsal bir harcama yoktu. **Yeni bir "saçma açıklama"
gördüğünde önce fişe bak, prompt'a yama yapma.**

