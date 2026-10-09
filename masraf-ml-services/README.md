# Masraf ML Service

Masraf Denetim projesinin ML tarafı: fiş görselinden elde edilen **OCR metninden satıcı VKN/TCKN'sini bulan** hibrit bir akış,
bu akışı gerçek fişlerde **ölçen betikler**, fişlerde **alan konumunu (layout) araştıran betikler** ve MinIO / MLflow ile konuşan bir **FastAPI servisi**.

> Durum: araştırma ve pilot aşaması. Servis tek başına üretime hazır değildir (bkz. "Bilinen sınırlar").
> Son kontrol: 9 Ekim 2026 (86 birim testi geçti; tüm betikler sentetik veriyle hatasız çalıştı).

## İçindekiler

1. [Ne yapıyor?](#1-ne-yapıyor)
2. [Kurulum ve çalıştırma](#2-kurulum-ve-çalıştırma)
3. [Proje yapısı ve dosya rehberi](#3-proje-yapısı-ve-dosya-rehberi)
4. [Veri biçimleri ve numara kayması](#4-veri-biçimleri-ve-numara-kayması)
5. [Karar akışı](#5-karar-akışı)
6. [API](#6-api)
7. [Betikler nasıl kullanılır?](#7-betikler-nasıl-kullanılır)
8. [Son ölçüm sonuçları](#8-son-ölçüm-sonuçları)
9. [Bilinen sınırlar](#9-bilinen-sınırlar)
10. [Güvenlik ve veri](#10-güvenlik-ve-veri)
11. [Geliştirme notları ve yol haritası](#11-geliştirme-notları-ve-yol-haritası)
12. [Değişiklik geçmişi](#12-değişiklik-geçmişi)

---

## 1. Ne yapıyor?

Bir fişten şu alanlar çıkarılmak istenir: firma adı, satıcı VKN/TCKN, tarih, toplam tutar.
Bu repo üç iş yapar:

1. **VKN/TCKN çıkarımı** (`app/`): OCR metninden numarayı bulur, checksum ile doğrular, satıcı sözlüğüyle destekler,
   emin olamazsa fişi **insan kontrolüne** yönlendirir.
2. **Ölçüm ve veri denetimi** (`scripts/olcum`, `scripts/veri_denetim`): etiketli (gold) fişlerle çıkarımın ne kadar doğru olduğunu,
   hangi yöntemin işe yaradığını ve etiketlerin kendisinin ne kadar temiz olduğunu ölçer.
3. **Layout araştırması** (`scripts/layout`): alanların (firma, VKN, tarih, toplam) fişin neresinde durduğunu kutulu (bbox) OCR ile ölçer,
   sentetik fişlerden otomatik kutu etiketi üretilip üretilemeyeceğini dener.

Bu repo OCR veya VLM modelini **servis olarak çalıştırmaz**. OCR (PaddleOCR) ve VLM (Qwen2.5-VL) çıktıları dışarıda (Colab / Kaggle) üretilir,
buraya JSON olarak girdi verilir. (Layout betikleri istisna: gerekirse OCR'ı kendileri çalıştırır, bkz. bölüm 7.3.)

```
fiş görseli --(OCR: PaddleOCR)--> OCR metni --+
            --(VLM: Qwen2.5-VL)-> VLM JSON  --+--> [Bu repo: hibrit karar akışı] --> VKN + firma + "insan kontrolü gerekir mi?"
                                                          ^
                                          satıcı sözlüğü (VKN -> firma)
```

## 2. Kurulum ve çalıştırma

Gereksinim: Python 3.11 (geliştirmede 3.11 kullanıldı).

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
pip install -r requirements-dev.txt     # yalnız testler için (API testlerinin httpx'i)
copy .env.example .env
```

`.env` içindeki değerleri doldur (MinIO ve MLflow için altyapı: `masraf-denetim-infra` klasörü / reposu).
`.env` dosyası git'e **eklenmez**.

| Değişken | Açıklama |
| --- | --- |
| `MINIO_ENDPOINT`, `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY`, `MINIO_SECURE` | MinIO bağlantısı |
| `MINIO_BUCKET` | Fiş görsellerinin yüklendiği bucket (varsayılan `masraf-fisler`) |
| `MLFLOW_TRACKING_URI` | MLflow sunucusu (varsayılan `http://localhost:5000`) |
| `MLFLOW_LOG_AKTIF` | `false` ise `/extract/vkn` sonuçları MLflow'a yazılmaz (varsayılan `true`) |
| `MLFLOW_S3_ENDPOINT_URL`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | MLflow'un artifact'ları MinIO'ya yazması için (aynı key'ler) |
| `SATICI_SOZLUGU_YOLU` | İsteğe bağlı; varsayılan `data/satici_sozlugu.json` |

**Servisi başlat:**

```powershell
uvicorn app.main:app --reload
```

Etkileşimli doküman: `http://localhost:8000/docs`

**Testleri çalıştır:**

```powershell
python -m unittest discover -s tests -v
```

API testleri sahte MinIO/MLflow kullanır, gerçek servis gerekmez. `fastapi` / `httpx` kurulu değilse 15 API testi atlanır, kalanı çalışır.

> Betikler **proje kökünden** `python -m scripts.<grup>.<betik>` ile çalıştırılır. Çıktılar hangi klasörden çalıştırılırsa çalıştırılsın proje içindeki `data\` klasörüne yazılır.

## 3. Proje yapısı ve dosya rehberi

```
.
├── app/                          FastAPI servisi
│   ├── main.py                   Uygulama girişi, router'lar
│   ├── config.py                 Ortam değişkenleri (bucket, MLflow log açık/kapalı)
│   ├── routers/                  HTTP katmanı (extract, storage, tracking)
│   └── services/                 İş mantığı (extraction, pipeline, vendors, storage, tracking)
├── scripts/                      Ölçüm, veri denetimi ve layout betikleri
│   ├── ortak.py                  Ortak yardımcılar (dosya okuma, kayma, çıktı yolu)
│   ├── olcum/                    Doğruluk ölçümleri
│   ├── veri_denetim/             Etiket / dosya eşleşmesi denetimleri
│   ├── arac/                     Satıcı sözlüğü üretimi
│   └── layout/                   Bbox'lı OCR ve alan konumu araştırması
├── tests/                        Birim testleri (86 test)
├── data/                         Betik çıktıları ve sözlük (git'e girmez, gerçek VKN içerir)
├── requirements.txt              Servis bağımlılıkları
├── requirements-dev.txt          Test bağımlılıkları
└── .env.example
```

### 3.1 Servis kodu (`app/`)

| Dosya | Ne yapar |
| --- | --- |
| `app/main.py` | FastAPI uygulamasını kurar, üç router'ı bağlar, `/health` uç noktasını sunar |
| `app/config.py` | Servis ayarlarını (`MINIO_BUCKET`, `MLFLOW_LOG_AKTIF`) çağrı anında ortamdan okur |
| `app/routers/extract.py` | `/extract/*`: VKN çıkarımı ve satıcı sözlüğü uçları. MLflow logunu yanıttan sonra arka planda yazar |
| `app/routers/storage.py` | `/storage/*`: fiş görselini MinIO'ya yükle (dosya adı ve uzantı denetimiyle), alt klasörlü listele, geçici indirme linki üret |
| `app/routers/tracking.py` | `/ml/log-ocr-run`: OCR/VLM sonucunu MLflow'a loglar |
| `app/services/extraction.py` | OCR metninden 10/11 haneli adayları bulur, VKN ve TCKN checksum'unu doğrular (`kimlik_gecerli`), "VERGİ NO" etiketine bakar, OCR'da tek hane düzeltmesi dener |
| `app/services/pipeline.py` | Hibrit karar akışı `vkn_karar_ver`. Her basamak ayrı fonksiyon; `Kaynak` ve `Guven` sabitleri sonuç değerlerini tanımlar (bkz. bölüm 5) |
| `app/services/vendors.py` | Satıcı sözlüğü: kurma (VKN ve geçerli TCKN), kaydetme, yükleme, tek hane farkla arama, firma adı normalizasyonu. `SozlukDeposu` sözlüğü ilk kullanımda yükler, `yenile()` ile tekrar okur |
| `app/services/storage.py` | MinIO istemcisi (ilk kullanımda kurulur): yükle / indir / var mı / listele (özyinelemeli) / geçici link |
| `app/services/tracking.py` | MLflow loglama (`log_ocr_run`, `log_vkn_run`). MLflow ilk kullanımda içe aktarılır, servis MLflow olmadan da açılır |

### 3.2 Betikler (`scripts/`)

Çalıştırma: proje kökünden `python -m scripts.<grup>.<betik> ...`. Gold / OCR / VLM verisi repoda yoktur (bölüm 4).
"Durum" sütunu betiğin güvenilirliğini söyler.

**Ölçüm (`scripts/olcum`)**

| Betik | Ne yapar | Girdi | Çıktı | Durum |
| --- | --- | --- | --- | --- |
| `vkn_olc` | VKN çıkarımını 3 yöntemle ölçer: A) yalnız OCR, B) OCR+VLM, C) OCR+VLM+sözlük (leave-one-out). Sessiz hataları (otomatik kabul edilen ama yanlış olanlar) nedenleriyle listeler | gold, OCR, VLM (isteğe bağlı) | ekran özeti, `data\vkn_olcum.csv`, `--mlflow` ile MLflow | Güncel, ana ölçüm |
| `firma_olc` | Firma adı doğruluğu: yalnız VLM ile "VKN → sözlük → firma" hibritini karşılaştırır. Katı ve esnek (hukuki ekler atılır) ölçü verir | gold, OCR, VLM | ekran özeti, `data\firma_olcum.csv` | Güncel |
| `firma_hata_analizi` | `firma_olc`'un yanlışlarını sınıflar (kısa/uzun ad farkı, ortak kelime çok/az, tamamen farklı, boş) | `data\firma_olcum.csv` | ekran özeti | Güncel. Önce `firma_olc` çalışmış olmalı |
| `alan_olc` | VLM çıktısının alan bazında doğruluğu: firma, tarih, tutar, VKN | gold, VLM | ekran tablosu, `data\alan_olcum.csv` | Güncel. Başka betikler bu modüldeki fonksiyonları kullanır |
| `dogrulama_olc` | Çapraz doğrulama: VLM'in tarih/tutar/firma değeri OCR metninde de geçiyor mu? İki kaynak uyuşuyorsa otomatik kabul, uyuşmuyorsa insana | gold, OCR, VLM | ekran tablosu | Güncel |
| `konum_analiz` | Firma adı ve toplam tutar fişin neresinde (satır sırasına göre yüzde dilimi)? | gold, OCR | ekran dağılımı | Sınırlı: koordinat kullanmaz. Fotoğraf fişlerde tutarsız çıkar; koordinatlı sürüm için `layout/konum_gercek` |

**Veri denetimi (`scripts/veri_denetim`)**

| Betik | Ne yapar | Girdi | Çıktı | Durum |
| --- | --- | --- | --- | --- |
| `etiket_denetim` | Gold etiketlerdeki şüpheli VKN'leri listeler: boş, 11 haneli, checksum geçmeyen, garip uzunluk, bozuk JSON | gold | `data\etiket_denetim.csv` | Güncel |
| `tarih_bicim` | Gold'daki tarih biçimlerini sayar ve `tarih_norm`'un çözemediklerini gösterir | gold | ekran tablosu | Güncel |
| `kayma_bul` | OCR dosyaları ile gold arasındaki numara farkını (ofset) bulur: VKN + tarih eşleşmesinden | gold, OCR | ofset dağılımı | Güncel. Kayma şüphesinde önce bunu çalıştır |
| `hizalama_kontrol` | Gold etiketi ile OCR metni aynı fişe mi ait? Tarih ve VKN'nin OCR'da geçip geçmediğine bakar. `--kayma` desteklenir | gold, OCR | ekran özeti | Güncel |
| `gorsel_etiket_eslestir` | Bbox'lı OCR çıktılarından her görselin hangi etikete ait olduğunu içerikten (VKN +2, tarih +1, toplam +1 puan) bulur; güvenilir / zayıf / belirsiz diye işaretler | bbox'lı OCR, gold | `data\gorsel_etiket_eslesme.csv`, ofset özeti | Güncel. Görsel numaraları etiket numaralarıyla uyuşmadığında kullan |

**Araç (`scripts/arac`)**

| Betik | Ne yapar | Girdi | Çıktı |
| --- | --- | --- | --- |
| `satici_sozlugu_olustur` | Gold klasöründen satıcı sözlüğünü üretir (geçerli VKN ve TCKN'li satıcılar). Servisten önce çalıştırılmalı | gold | `data\satici_sozlugu.json` |

**Layout (`scripts/layout`)** — ek bağımlılık ister: `pip install rapidocr-onnxruntime pillow numpy` (ya da PaddleOCR için `paddlepaddle==2.6.2 paddleocr==2.10.0`)

| Betik | Ne yapar | Girdi | Çıktı |
| --- | --- | --- | --- |
| `ocr_bbox_kaydet` | Klasördeki gerçek fiş görsellerini OCR'dan geçirir ve **her satırın kutusunu (bbox)** kaydeder. Dosya adı görselin adıyla aynıdır (numara kayması olmaz); var olanı atlar, kesilirse devam eder | görsel klasörü | `{görsel}.json` (`image`, `boyut`, `ocr[text, bbox, confidence]`) |
| `konum_gercek` | Gerçek fişlerde firma, VKN, tarih ve toplam fişin neresinde (yukarıdan aşağıya yüzde)? Fiş numarası gruplarına göre dağılım verir (`--gruplar "1-51,52-143"`). Amaç: koordinat kuralı yeter mi, model mi gerekir? | gold, bbox'lı OCR | ekran dağılımı |
| `layout_pilot` | Sentetik fişlerde (`final_veriler_en`) bilinen alan değerlerinin OCR kutularına eşleşip eşleşmediğini ölçer; eşleşenleri "zayıf etiket" olarak kaydeder (LiLT / LayoutXLM eğitimi için). Kural: çoğu alanda eşleşme ≥ %80 ise otomatik kutu etiketi üretilebilir | sentetik veri klasörü | `zayif_etiket.jsonl`, `ocr/*.json`, ekran özeti |

**Yardımcı:** `scripts/ortak.py` — JSON okuma (`gold_dosyalari`, `ocr_metni`, `vlm_oku`), kayma çözümü (`kayma_cozucu`), çıktı yolu (`veri_yolu`), tarih adayları, firma adı anahtar kelimeleri. Kendi başına çalıştırılmaz.

> "Her zaman çalışmıyor" olmasının nedeni genelde betiğin kendisi değil **girdi**dir: üç klasörün (gold / OCR / VLM) dosya adları aynı düzende olmalı,
> numaralar kaymışsa `--kayma` verilmeli, `firma_hata_analizi` için önce `firma_olc` çalışmış olmalı.

## 4. Veri biçimleri ve numara kayması

Veri klasörleri repoda yoktur. Betikler şunları bekler:

**Gold (etiket)** — `{fis}.json`:

```json
{"company": "ÖRNEK MARKET A.Ş.", "seller_tax_id": "1234567890", "date": "14.05.2026", "total": "125,50"}
```

**OCR** — `{fis}.json`:

```json
{"ocr": [{"text": "ÖRNEK MARKET A.Ş."}, {"text": "VERGİ NO: 1234567890"}]}
```

Satırlar yukarıdan aşağıya sıralıdır. Layout betikleri ayrıca `boyut` (`[genişlik, yükseklik]`) ve her satır için `bbox` (4 köşe noktası) ister; `ocr_bbox_kaydet` bu biçimde üretir.

**VLM** — `{fis}_clean.json` ya da `{fis}_pred.json` (ya da `{fis}.json`), aynı alan adlarıyla (`company`, `seller_tax_id`, `date`, `total`).

### Numara kayması (`--kayma`, `--kayma-baslangic`)

Gold, OCR ve görsel dosya numaraları her zaman aynı fişi göstermeyebilir. Kaymayı betikler şöyle düzeltir:
gold `fisM`, `M >= --kayma-baslangic` ise OCR/VLM dosyası `fis(M + --kayma)` olarak aranır.

```powershell
python -m scripts.olcum.vkn_olc --gold "<json3>" --ocr "<ocr>" --vlm "<cleaned>" --kayma 2 --kayma-baslangic 144
```

Kaymanın olup olmadığını ve değerini içerikten (VKN + tarih) bulmak için `kayma_bul` (metin OCR'ı için) ya da `gorsel_etiket_eslestir` (görseller için) kullan.
Tek bir sabit ofset her bölge için geçerli olmayabilir; ofset dağılımı birden fazla değer gösteriyorsa kaymayı bölge bölge incele.

## 5. Karar akışı

`pipeline.vkn_karar_ver(ocr_text, vlm_vkn, sozluk, firma_ipucu)` basamakları sırayla dener, ilk güvenilir olanda durur:

| Sıra | `kaynak` | `guven` | `insan_kontrolu` | Ne zaman |
| --- | --- | --- | --- | --- |
| 1 | `ocr_regex` | yüksek | hayır | OCR'da checksum'i geçen tek numara var (ya da birden fazla varsa "VERGİ NO / VKN" etiketinin yanındaki tek numara) |
| 2 | `satici_sozlugu` | yüksek (`bilinen`) / orta (`kurtarildi`) | hayır | OCR'daki numara sözlükte aynen var ya da tek hane farkla sözlükteki tek bir VKN'ye uyuyor. VLM farklı ama geçerli bir VKN söylüyorsa **evet** |
| 3 | `ocr_vlm_uyumu` | yüksek | hayır | OCR'da birden fazla geçerli numara var, VLM bunlardan biriyle uyuşuyor |
| 4 | `vlm` | orta | hayır | OCR bulamadı, VLM'in numarası checksum'i geçiyor |
| 5 | `ocr_duzeltme` | düşük | **evet** | OCR'da tek hane düzeltmesiyle geçerli numara bulundu (yaklaşık %2 yanlış riski) |
| 6 | `yok` | yok | **evet** | Hiçbiri güvenilir değil |

Notlar:

- 10 haneli numara VKN, 11 haneli numara TCKN (şahıs satıcı) olarak doğrulanır; `kimlik_tipi` alanı bunu söyler.
- Tarih+saat damgası, `00` ile başlayan 10 haneli sayı (fiş/işlem no), `0` ile başlayan 11 haneli sayı (telefon) ve tek rakamın tekrarı VKN sayılmaz.
- "Aynı VKN, farklı firma adı" uyarısı (`firma_uyusmuyor`) yalnızca `detay.firma_uyarisi` olarak kaydedilir, fişi insana göndermez. Sebep: firma ipucu olarak kullanılan VLM firma adı yeterince güvenilir değil.
- Ölçüm ilkesi: **otomatik kabul doğruluğu** = insan kontrolüne gönderilmeden kabul edilen sonuçların doğruluğu. Asıl güvenilirlik ölçüsü budur.
- Sözlük, geçerli TCKN'li (şahıs) satıcıları da içerir. TCKN'de sözlükle "tek hane kurtarma" yapılmaz, yalnızca VKN bulunduktan sonra firma adı getirilir.

## 6. API

| Endpoint | Açıklama |
| --- | --- |
| `GET /health` | Servis ayakta mı |
| `POST /extract/vkn` | OCR metninden VKN/TCKN çıkarır, sözlükten firmayı bulur, sonucu arka planda MLflow'a loglar |
| `GET /extract/sozluk-durum` | Satıcı sözlüğü yüklü mü, kaç satıcı var |
| `POST /extract/sozluk-yenile` | Sözlüğü servisi yeniden başlatmadan tekrar yükler |
| `POST /storage/upload` | Fiş görselini MinIO'ya yükler. İzinli uzantılar: png, jpg, jpeg, pdf, webp. Dosya adındaki klasör parçaları atılır. Aynı ad varsa `409`, `?overwrite=true` ile üzerine yazılır |
| `GET /storage/list` | Bucket'taki dosyaları alt klasörler dahil listeler; `?prefix=gercek/` ile daraltılır |
| `GET /storage/url/{object_name}` | Geçici indirme linki (1 saat geçerli); `gercek/fis1.jpg` gibi alt klasörlü adlar desteklenir, dosya yoksa `404` |
| `POST /ml/log-ocr-run` | Bir OCR/VLM çıkarımını MLflow'a loglar (`model_name` isteğe bağlı) |

`POST /extract/vkn` gövdesi:

```json
{"fis_id": "fis1", "ocr_text": "ÖRNEK MARKET\nVERGİ NO: 1234567890", "vlm_vkn": null, "firma_ipucu": null}
```

Yanıt alanları: `vkn`, `kaynak`, `guven`, `insan_kontrolu`, `kimlik_tipi`, `satici` (`durum`: `vkn_yok` / `yeni_satici` / `bilinen_satici`; bilinen ise `firma`, `fis_sayisi`),
`detay` (adım adım ne olduğu), `fis_id`, `mlflow_log` (`kuyruga_alindi` ya da `kapali`).

Depo (MinIO) ya da MLflow hatalarında servis ayrıntıyı istemciye göndermez (`502` ve genel mesaj); ayrıntı sunucu log'una yazılır.

Satıcı sözlüğü `data\satici_sozlugu.json` dosyasındadır. Yoksa servis sözlüksüz çalışır.
Sözlüğü üretmek için: `python -m scripts.arac.satici_sozlugu_olustur "<gold klasörü>"`, sonra `POST /extract/sozluk-yenile`.

## 7. Betikler nasıl kullanılır?

### 7.1 Ölçüm sırası (gerçek etiketli veri geldiğinde)

```powershell
# 1) Etiketler temiz mi?
python -m scripts.veri_denetim.etiket_denetim --gold "<json3>"
python -m scripts.veri_denetim.tarih_bicim --gold "<json3>"

# 2) Dosya numaraları kaymış mı?
python -m scripts.veri_denetim.kayma_bul --gold "<json3>" --ocr "<ocr>"

# 3) Sözlüğü üret
python -m scripts.arac.satici_sozlugu_olustur "<json3>"

# 4) Ölçümler (kayma varsa --kayma ve --kayma-baslangic ekle)
python -m scripts.olcum.vkn_olc       --gold "<json3>" --ocr "<ocr>" --vlm "<cleaned>"
python -m scripts.olcum.firma_olc     --gold "<json3>" --ocr "<ocr>" --vlm "<cleaned>"
python -m scripts.olcum.firma_hata_analizi
python -m scripts.olcum.alan_olc      --gold "<json3>" --vlm "<cleaned>"
python -m scripts.olcum.dogrulama_olc --gold "<json3>" --ocr "<ocr>" --vlm "<cleaned>"
```

Sonuçları MLflow'a kaydetmek için `vkn_olc` komutuna `--mlflow` ekle (experiment adı: `vkn-olcum`).

### 7.2 Gerçek fişlerde konum analizi (bbox ile)

```powershell
pip install rapidocr-onnxruntime pillow numpy

# 1) Görselleri kutulu OCR'dan geçir (OneDrive'da "bu cihazda her zaman tut" seçili olmalı)
python -m scripts.layout.ocr_bbox_kaydet --gorsel "<görsel klasörü>" --cikti data\ocr_bbox

# 2) Hangi görsel hangi etikete ait? (numara kayması varsa burada görünür)
python -m scripts.veri_denetim.gorsel_etiket_eslestir --ocr data\ocr_bbox --gold "<json3>"

# 3) Alanlar fişin neresinde?
python -m scripts.layout.konum_gercek --gold "<json3>" --ocr data\ocr_bbox --gruplar "1-51,52-143"
```

`konum_gercek` etiket numarasıyla OCR dosya adını birebir eşler; görsel numaraları etiketlerle uyuşmuyorsa önce 2. adımın sonucuna göre düzelt.

### 7.3 Sentetik fişlerde layout pilotu

```powershell
python -m scripts.layout.layout_pilot --veri "<final_veriler_en>" --bolum egitim --n 300 --motor rapidocr --cikti data\layout_pilot_cikti
```

`--motor paddle` (varsayılan) PaddleOCR 2.10 kullanır (modeller internetten indirilir), `--motor rapidocr` modelleri pakete gömülü gelir.
`--ocr-atla` daha önce kaydedilmiş OCR dosyalarını yeniden kullanır.

### Kavramlar

- **Leave-one-out:** Sözlük ölçülen her fiş için o fişin kendisi çıkarılarak yeniden kurulur; sözlük cevabı önceden görmez.
- **Katı / esnek firma ölçüsü:** Katı ölçü adların benzerliğine (normalize edilmiş, eşik 0,85) bakar. Esnek ölçü A.Ş., LTD. ŞTİ. gibi hukuki ekleri atar; kısa ad, uzun adın en az 2 anahtar kelimesini ve en az yarısını içeriyorsa aynı sayar.
- **Çapraz doğrulama:** VLM değeri OCR metninde de geçiyorsa güvenilir sayılır. Kapsam (kaç fiş doğrulanabildi) ve doğrulananların doğruluğu birlikte okunur.
- **Zayıf etiket:** Elle işaretlenmemiş, bilinen değerin OCR kutusuna eşlenmesiyle otomatik üretilmiş kutu etiketi.

## 8. Son ölçüm sonuçları

Etiketli gerçek fişlerde Ekim 2026'da yapılan ölçümler (veri veya etiket değiştikçe yeniden çalıştırılmalıdır):

| Ölçüm | Sonuç |
| --- | --- |
| Firma adı, yalnız VLM (katı / esnek) | %51,3 / %67,7 |
| Firma adı, VKN → sözlük hibriti (katı / esnek) | %77,4 / %86,3 |
| Tarih, VLM değeri OCR ile doğrulananlar | %97,3 doğru, kapsam %83 |
| Toplam tutar, VLM değeri OCR ile doğrulananlar | %96,7 doğru, kapsam %94 |
| Firma adı, OCR satır benzerliğiyle "doğrulama" | ayırt edici değil (%92 "doğrulandı", gerçek doğruluk %50,2) |

Layout (bbox'lı OCR, RapidOCR, doğru eşleşen küçük örneklem):

| Grup | Sonuç |
| --- | --- |
| Temiz e-fişler (fis1-51, 18 fiş) | Firma %100 bulundu ve hep üst çeyrekte; VKN %78 (üst çeyrekte); tarih %100; toplam %94 (ortalarda). Koordinat kuralı yeter |
| Fotoğraf fişler (fis52-143, 31 fiş) | Firma %45 bulundu; alanlar fişin her yerinde çıkıyor. Kural yetmez; kırpma / döndürme ve model gerekir |

Firma adı hatalarının önemli bölümü okuma hatası değil **ad standardı** sorunudur: marka adı mı hukuki unvan mı yazılacağı belli değil
(ör. DOMİNO'S PİZZA, PAPA JOHNS, ISPARK) ve bazı etiketler aynı satıcı için tutarsız.
Sentetik fişler temiz gruba benzer; fotoğraf grubu için gerçek fişlerle ince ayar gerekir.

## 9. Bilinen sınırlar

**Servis**

- Kimlik doğrulaması yok; yerel geliştirme içindir.
- `/extract/vkn` her istek için MLflow'da ayrı bir run açar (arka planda). Yoğun kullanımda bu gereksiz yük olur; toplu loglama düşünülmeli.
- `/ml/log-ocr-run` gerçek OCR/VLM çıktısına bağlı değildir, elle gönderilen özet kaydeder.
- TCKN'li satıcılarda sözlükle tek hane kurtarma yapılmaz.

**Ölçüm ve veri**

- `konum_analiz` yalnız satır sırasına bakar; fotoğrafla çekilmiş fişlerde alanlar her yerde çıkabilir.
- Numara kayması tek bir sabit ofsetle düzeltilemeyebilir (bölgeye göre değişir); `--kayma` tek değer alır.
- Etiketlerde bilinen sorunlar var (boş / şüpheli VKN, tutarsız firma adları). `etiket_denetim` listesine bak.
- Ölçümler küçük örneklemlerde yapıldı; yüzdeler arası küçük farklar anlamlı değildir.

**Genel**

- Sentetik fişler temiz ve şablon tabanlıdır (domain farkı); modeller yalnız gerçek fişlerle test edilmelidir.
- Üretimde GPU kullanılması hedeflenmiyor. VLM çalıştırma (Colab / Kaggle) bu repoda yok.
- Layout betikleri (`rapidocr-onnxruntime`, `pillow`, `numpy`) `requirements.txt`'te değildir; yalnız araştırma için gerekir.

## 10. Güvenlik ve veri

- `.env` (gerçek şifreler / anahtarlar) repoya girmez. `.env.example` girer ve değerleri boştur ya da `degistir` yazar.
- `data/` gerçek VKN/TCKN ve firma adları içerir, `.gitignore`'dadır. Gerçek fiş görselleri, OCR çıktıları, VLM çıktıları ve gold etiketler (`json3/`, `ocr/`, `cleaned/`) repoya **yüklenmez**.
- Testlerdeki numaralar sentetiktir (gerçek bir firmaya / kişiye ait değildir).
- Dosya yüklemede ad ve uzantı denetlenir; servis iç hata ayrıntısını istemciye göndermez.

## 11. Geliştirme notları ve yol haritası

- Yeni betik yazarken `scripts/ortak.py`'deki yardımcıları kullan (`gold_dosyalari`, `ocr_metni`, `vlm_oku`, `kayma_argumanlari_ekle`, `kayma_cozucu`, `veri_yolu`); aynı okuma kodunu tekrar yazma.
- Çıkarım mantığını değiştirirsen `tests/` altına test ekle. Betiklerin çıktısını değiştiren bir düzenleme yaparsan, eski ve yeni çıktıyı aynı veriyle karşılaştır.
- Her betik çıktısını `data\` klasörüne yazar; çıktıları repoya ekleme.

Planlanan işler:

- Satıcı sözlüğüne CSV ile elle ekleme (`vendors.py`) ve "insan bir kez onaylar, sözlüğe girer" döngüsü.
- Fotoğraf fişleri için ön işleme (kırpma, döndürme, EXIF yönü) ve bbox tabanlı alan modeli (LiLT / LayoutXLM, Colab'da eğitim).
- Etiket temizliği ve firma adı standardı (marka / unvan kuralı).
- Kod kalitesi aracı (ruff) ve CI'da testlerin otomatik çalışması.

## 12. Değişiklik geçmişi

**9 Ekim 2026 — clean code düzenlemesi**

- Kök dizindeki betikler `scripts/` paketine taşındı (`olcum`, `veri_denetim`, `arac`, `layout`); `olcum_ortak.py` → `scripts/ortak.py`. Çalıştırma artık `python -m scripts.<grup>.<betik>`.
- Gold klasörü okuma, kayma çözümü ve çıktı yolu ortak yardımcılara taşındı; `vkn_olc` fonksiyonlara bölündü. Eski ve yeni betik çıktıları aynı sentetik veriyle birebir karşılaştırıldı (tek fark aşağıdaki TCKN değişikliği).
- Servis: `/storage/list` alt klasörleri listeler; `/storage/url/...` alt klasörlü adlarla çalışır; bucket `MINIO_BUCKET` ile ayarlanır; yükleme dosya adı / uzantı denetler, aynı ada `409` döner; iç hata ayrıntıları istemciye gitmez.
- Servis: MinIO istemcisi ve MLflow ilk kullanımda kurulur; satıcı sözlüğü `SozlukDeposu` ile ilk istekte yüklenir; `/extract/vkn` MLflow logunu arka planda yazar. **Yanıt değişikliği:** `mlflow_loglandi` / `mlflow_hata` alanları yerine `mlflow_log` (`kuyruga_alindi` / `kapali`).
- `pipeline.vkn_karar_ver` basamak fonksiyonlarına bölündü, `Kaynak` ve `Guven` sabitleri eklendi (sonuç değerleri aynı).
- **Davranış değişikliği:** Üretim satıcı sözlüğü artık geçerli TCKN'li (şahıs) satıcıları da içerir; ölçüm betikleri zaten böyle çalışıyordu, ikisi tutarlı hale geldi.
- `hizalama_kontrol` `--kayma` destekler; `tarih_bicim` artık gereksiz yere `vkn_olc`'u içe aktarmaz.
- `tests/`: 26 → 86 test (API uçları, ortak yardımcılar, firma ölçüsü, sözlük, layout fonksiyonları).
- Layout betikleri (`ocr_bbox_kaydet`, `konum_gercek`, `layout_pilot`, `gorsel_etiket_eslestir`) projeye eklendi.
