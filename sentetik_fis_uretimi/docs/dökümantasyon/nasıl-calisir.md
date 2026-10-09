# Sıfırdan veri üretimi

Projeyi hiç bilmeyen birinin baştan sona veri seti üretebilmesi için yazıldı.
Dokuz adım var, her adım bir öncekinin çıktısını okur.

Üretim tek bir dizine yapılır (`$D`). Böylece eski koşuların üzerine yazılmaz,
beğenmezsen dizini silmen yeter.

## Önce

```bash
source venv/bin/activate
D=data/deneme          # koşu dizinin; istediğin adı ver
```

`data/firma_registry.csv` gerekli (git'te İZLENMİYOR, klondan sonra yok). Yoksa
bir kez üret (yeni firmalar eklenmedikçe/dosya zaten varsa gereksiz, ATLA):


```bash
#YENİ FİRMA VERİSİ ÇEKİLMEDİYSE BU AŞAMAYI ATLA MEVCUT FİRMALARI BOZAR.
python -m faz_a_fis_uretimi.firma_registry_olustur --hedef-per-iskolu 1500 --osm-pay 0.8 --seed 42
```

`data/companies/firma_adlari_osm.csv` (OSM'den çekilmiş gerçek firma adları) da
yoksa uyarıp **tamamen sentetik adlarla** üretime devam eder, durmaz. Gerçek
adlarla üretmek istiyorsan önce (ağ gerektirir, dakikalar sürer):

```bash
#AYNI SEBEPTEN ATLAYABİLİRSİN
python -m faz_a_fis_uretimi.firma_adlari_osm_cek --yeniden --hedef 3000 --bekleme 10
```

3. adımda açıklama üretmek için ya yerelde Ollama açık olmalı ya da Kaggle
   kullanılmalı (bkz. `05-kaggle-calistirma.md`).

## Hangi dosya nereye gidiyor

Numaralar aşağıdaki adımlarla aynı.

```
data/
├── firma_registry.csv           bir kez üretilir, Faz A bunu okur
├── companies/firma_adlari_osm.csv   opsiyonel, yoksa sentetik ada düşer
├── politika_limitleri.json      harcama limitlerinin tek kaynağı
├── urun_verileri/  hizmet_verileri/  anomali_verileri/    ham ürün havuzları
│
└── deneme/                      <- $D
    ├── faturalar.json           1  faz_a_fis_uretimi/main.py
    ├── faturalar.csv            1
    ├── faturalar_etiketler.json 1
    ├── faturalar_rapor.json     1
    │
    ├── aciklama/                <- --cikti-dizini (batch ve çıktılar AYNI dizinde)
    │   ├── batch_0001.json          2  faz_b_aciklama_uretimi/batch_hazirla
    │   ├── durum.json               2
    │   └── batch_0001_ciktilar.json 3  faz_b_aciklama_uretimi/aciklama_toplu_uret (ya da Kaggle)
    │
    ├── faturalar_aciklamali.json            4  faz_b_aciklama_uretimi/aciklama_birlestir  -> MODEL GİRDİSİ
    ├── faturalar_aciklamali_etiketler.json  5  faz_b_aciklama_uretimi/onay_durumu_ata     -> ETİKET
    │
    └── final_veriler/           6  faz_c_fis_gorsellestirme/veri_bol
        ├── egitim_girdi.json      + egitim_etiket.json
        ├── dogrulama_girdi.json   + dogrulama_etiket.json
        ├── test_girdi.json        + test_etiket.json
        ├── bolme_raporu.json
        └── egitim_gorsel/  dogrulama_gorsel/  test_gorsel/   7  faz_c_fis_gorsellestirme/fis_uret
```

> Kod dosyaları da artık faz bazlı klasörlerde: `ortak/`, `faz_a_fis_uretimi/` (+
> `faz_a_fis_uretimi/generators/`), `faz_b_aciklama_uretimi/`, `faz_c_fis_gorsellestirme/`, `arsiv/` (bkz. CLAUDE.md §6 başı).
> Tüm komutlar `python -m <klasör>.<modül>` ile repo kökünden çalıştırılır.

Nihai ürün `final_veriler/` altındaki ikililerdir: `*_girdi.json` modele verilir,
`*_etiket.json` doğru cevaptır. İkisi `kayit_id` ile eşleşir.

---

## 1) Faz A: fatura üret

```bash
python -m faz_a_fis_uretimi.main --count 5000 --anomali-orani 0.25 --output-dir $D --filename faturalar
python -m faz_a_fis_uretimi.rapor_analiz --output-dir $D --filename faturalar
```

Anomali oranı 0,25 istenir ama ~0,26 çıkar; mükerrer çiftin iki üyesi de
etiketlendiği için normal.

Raporun sonunda `[+] Veri seti invariant kontrolü temiz (uyari yok).` yazmalı.
Yerine `[!] VERİ SETİ UYARILARI` çıkarsa durup bak: boş `yukleme_zamani`/`saat`
bir enjektörün alanı taşımadığını, asimetrik çift ise çift etiketlemesinin
bozulduğunu söyler. Uyarılar `faturalar_rapor.json`'a da yazılır
(`veri_seti_uyarilari`); üretim durmaz, uzun koşu tek alan yüzünden çöpe gitmesin.

## 2) Faz B: batch hazırla

```bash
python -m faz_b_aciklama_uretimi.batch_hazirla \
  --input-json $D/faturalar.json --etiket-json $D/faturalar_etiketler.json \
  --toplam 1000 --batch-size 500 \
  --tur-taban 16 --tur-tavan 24 --iliskisel-taban 12 --iliskisel-tavan 16 \
  --cikti-dizini $D/aciklama
```

`--toplam` kaç açıklama üretileceğidir, havuzun tamamı değil. Bu adım hangi
faturaların açıklama alacağını seçer.

```
#  Not: Bu sayılar %25 anomali oranı içindir!
#  Sınırın sebebi manipulatif: havuzun ancak %6,5'i manipulatif, batch ise %20 istiyor.
#  Batch havuzun dörtte birini geçince yetişmiyor (%30'da %16,9'a düşüyor).
#  Kısacası: istediğin batch'in en az 4 katı fatura üret.
#
#  Kota parametreleri HAVUZA değil --toplam'a bağlıdır ve doğrusal ölçeklenir.
#  Aşağıdaki kota sütunları "rahat batch %20" değerini --toplam kabul eder:
#     --tur-taban       = toplam × 0,016      --tur-tavan       = toplam × 0,024
#     --iliskisel-taban = toplam × 0,012      --iliskisel-tavan = toplam × 0,016
#  Güvenli %25 batch'i seçersen kota sütunlarını da aynı oranla büyüt.
#  ≤10.000 havuzda nadir türler (fatura_no_cakismasi vb.) tabanın altında kalır;
#  rapor işaretler, beklenen durumdur.
┌────────────────┬────────────────┬─────────────────┬─────────────┬─────────────┬───────────────────┬───────────────────┐
│ havuz (fatura) │ maks batch %25 │ rahat batch %20 │ --tur-taban │ --tur-tavan │ --iliskisel-taban │ --iliskisel-tavan │
├────────────────┼────────────────┼─────────────────┼─────────────┼─────────────┼───────────────────┼───────────────────┤
│ 5.000          │ 1.250          │ 1.000           │ 16          │ 24          │ 12                │ 16                │
│ 10.000         │ 2.500          │ 2.000           │ 32          │ 48          │ 24                │ 32                │
│ 25.000         │ 6.250          │ 5.000           │ 80          │ 120         │ 60                │ 80                │
│ 50.000         │ 12.500         │ 10.000          │ 160         │ 240         │ 120               │ 160               │
│ 100.000        │ 25.000         │ 20.000          │ 320         │ 480         │ 240               │ 320               │
│ 120.000        │ 30.000         │ 24.000          │ 384         │ 576         │ 288               │ 384               │
│ 150.000        │ 37.500         │ 30.000          │ 480         │ 720         │ 360               │ 480               │
│ 200.000        │ 50.000         │ 40.000          │ 640         │ 960         │ 480               │ 640               │
└────────────────┴────────────────┴─────────────────┴─────────────┴─────────────┴───────────────────┴───────────────────┘
```

Oranı aşarsan script uyarır. Yukarıdaki örnek komut, tablodaki **5.000 havuz →
--toplam 1.000** satırıdır. Gerçek üretimde (120.000 fatura → 25.000 açıklama)
formül gereği `--tur-taban 400 --tur-tavan 600 --iliskisel-taban 300
--iliskisel-tavan 400` kullanılır (25.000 × 0,016 = 400 …).

İlişkisel türlerde (`mukerrer_fis_yukleme`, `fatura_no_cakismasi`) birim kayıt
değil **olaydır**: bir olay iki kayıt tutar, çünkü çiftin iki üyesi de seçilir.

## 3) Faz B: açıklamaları üret

Yerelde, Ollama açıkken:

```bash
python -m faz_b_aciklama_uretimi.aciklama_toplu_uret --cikti-dizini $D/aciklama \
  --burst-size 200 --cooldown-min 15 --workers 2
```

Kaggle'da (ücretsiz GPU, çok daha hızlı): `faz_b_aciklama_uretimi/aciklama_uretim_core.py`,
`faz_b_aciklama_uretimi/aciklama_toplu_uret.py`, `faz_b_aciklama_uretimi/aciklama_analiz.py`, `ortak/cift_grup.py`
(klasör yapısı KORUNARAK, her ikisinde `__init__.py` ile) ve batch dosyalarını
zipleyip notebook'a dataset olarak yükle. Hücreler `05-kaggle-calistirma.md`
bölüm 3'te.

**Hücrelerdeki sayılar 25 batch / 25.000 kayıtlık koşuya göre yazılı, kendi
koşunla değiştir.** Dört yer var:

| hücre | ne yazıyor            | ne olmalı                                        |
| ----- | --------------------- | ------------------------------------------------ |
| 3     | `assert len(b) == 25` | kaç batch dosyan varsa (2 batch → `== 2`)        |
| 5     | `--batch 1-25`        | `1-<batch sayın>`                                |
| 5     | `--ilerleme 1000`     | batch'ten küçük olsun, yoksa hiç ilerleme basmaz |
| 6     | `"/ 25000"`           | toplam kayıt sayın                               |

`--ilerleme`'yi atlama: 1.000 kayıtta bir basacak şekilde bırakırsan küçük
koşuda ekran sessiz kalır ve hücrenin çalışıp çalışmadığını anlayamazsın.

Kesinti veri kaybettirmez, aynı komut kaldığı yerden devam eder.

## 4) Faz B: birleştir

```bash
python -m faz_b_aciklama_uretimi.aciklama_birlestir --cikti-dizini $D/aciklama \
  --input-json $D/faturalar.json --etiket-json $D/faturalar_etiketler.json \
  --output-json $D/faturalar_aciklamali.json --sadece-uretilenler
```

`--sadece-uretilenler` yalnız açıklaması üretilmiş kayıtları yazar. Kaldırırsan
havuzun tamamı yazılır ve açıklamasız kayıtlar boş metinle kalır.

## 5) Faz B: onay_durumu

```bash
python -m faz_b_aciklama_uretimi.onay_durumu_ata --cikti-dizini $D/aciklama \
  --etiket-json $D/faturalar_etiketler.json \
  --girdi-json $D/faturalar_aciklamali.json \
  --output-json $D/faturalar_aciklamali_etiketler.json
```

Faz B'nin son adımı. Muhasebe kararı açıklama metni okunduktan sonra verilir,
bu yüzden Faz A'da değil burada üretilir.

`ese iliskisel etiket basildi` satırı **çıkmamalı**. Çıkarsa Faz A çiftin tek
üyesini etiketlemiş demektir.

## 6) Bölme

```bash
python -m faz_c_fis_gorsellestirme.veri_bol --girdi-json $D/faturalar_aciklamali.json \
  --etiket-json $D/faturalar_aciklamali_etiketler.json \
  --cikti-dizini $D/final_veriler --uygula
```

`--uygula` kısmını çıkarırsan rapor basar; bu sayede verileri bölmeden raporu
görebilirsin.

Bölme rastgele değil gruplu: mükerrer çiftler ve aynı metni taşıyan kayıtlar aynı
faza gider, yoksa test skoru ezberle şişer. `bolunen grup: 0` olmalı, olmazsa
script zaten dosya yazmaz.

## 7) Faz C: fiş görselleri

```bash
python -m faz_c_fis_gorsellestirme.fis_uret --bolme-dizini $D/final_veriler
```

Üç bölmeyi ayrı klasörlere render eder. Var olan PNG atlanır, yani kesilirse aynı
komutla devam eder. Fiş başına ~93 ms.

## 8) İngilizce alan adları ile (company,adress etc. )dışa aktarım

```bash
python -m faz_c_fis_gorsellestirme.alan_adlari --kaynak-dizin $D/final_veriler --hedef-dizin $D/final_veriler_en
```

Argümansız `python -m faz_c_fis_gorsellestirme.alan_adlari` gerçek NİHAİ seti (`data/final_veriler`
→ `data/final_veriler_en`) işler; deneme koşusunda YUKARIDAKİ gibi `$D` ver,
yoksa deneme çıktın yerine gerçek seti yeniden yazar. 7. adımda üretilen PNG'leri
de (varsa) opak `kayit_id` ile `{bolme}_gorsel/` altına kopyalar; bu yüzden 7'den
ÖNCE çalıştırma, görselsiz kopya çıkar.

`split_report.json` bu adımdan çıkmaz — ayrı bir rapor scripti
(`python -m faz_c_fis_gorsellestirme.final_veriler_en_rapor`, o da sabit `data/final_veriler_en`
okur).

## 9) Doğrulama

```bash
python -m faz_c_fis_gorsellestirme.mukerrer --girdi-json $D/faturalar_aciklamali.json \
  --etiket-json $D/faturalar_aciklamali_etiketler.json
```

`OKSUZ` sütunu iki türde de 0 olmalı. Eşi olmayan ilişkisel kayıt, çözülemeyen
bir anomali demektir.

---

## Sık yapılan hatalar

**`hiç üretilmiş açıklama yok`** — `batch_*_ciktilar.json` dosyaları `aciklama/`
içinde düz durmalı. Kaggle'dan inen zip bazen bir klasör içinde açılır:

```bash
mv $D/aciklama/ciktilar/*_ciktilar.json $D/aciklama/
```

**`--cikti-dizini` vermeyi unutmak** — varsayılanı `data/aciklama`, senin koşu
dizinin değil. `aciklama_birlestir` ve `onay_durumu_ata` için de geçerli.

**Batch havuzun dörtte birini geçmek** — manipulatif hedefin altında kalır.
Script uyarır, tabloya bak.

**final_veriler'i (adım 6/7'den SONRA) tek seferlik bir script'le düzeltmek** —
`fis_uret.py` var olan PNG'yi asla yeniden üretmez (`--yeniden` vermezsen). JSON'u
düzelttikten sonra etkilenen görselleri de `--yeniden` ile (ya da klasörü silip)
yeniden render etmezsen, fişler eski/yanlış veriyi göstermeye devam eder ve bunu
fark etmenin otomatik yolu yoktur (2026-08-20'de ~21.500 kayıttan ~21.200'ünde
yakalandı — birkaç şema düzeltmesinden önce üretilmiş görseller sessizce donmuş
kalmıştı).

---

## Kalite raporları (final_veriler her değiştiğinde)

Pipeline bittikten sonra (adım 8), NİHAİ set üzerinden HTML/JSON kalite raporları
üretilir. Repo kökünden:

```bash
python dataset_quality_reports/final_veriler_bolme_raporu_html_uret.py   # -> docs/raporlar/bolme_raporu.html
python dataset_quality_reports/final_veriler_kalite_raporu_html_uret.py  # -> docs/raporlar/kalite_raporu.html
python -m faz_c_fis_gorsellestirme.final_veriler_en_rapor                                    # -> data/final_veriler_en/split_report.json
```

- **Üçü de `data/final_veriler` / `data/final_veriler_en`'i SABİT okur — `$D`
  ALMAZ.** Deneme koşusunda (`$D=data/deneme`) çalıştırma; gerçek NİHAİ seti
  raporlarlar, deneme çıktını değil. Deneme çıktısını raporlamak istersen önce
  `$D/final_veriler`'i `data/final_veriler` konumuna taşı.
- `bolme_raporu` scripti `data/final_veriler/bolme_raporu.json`'a muhtaç — onu
  adım 6 (`veri_bol --uygula`) üretir.
- `dataset_quality_reports/` scriptleri repo kökünü `sys.path`'e ekler; `python -m`
  değil doğrudan yol ile çalıştırılır (`final_veriler_en_rapor` ise modül,
  `python -m faz_c_fis_gorsellestirme...`).
