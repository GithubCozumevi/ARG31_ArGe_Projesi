# Faz B — Açıklama Üretimi

> Her faturaya, çalışanın yazmış olacağı kısa açıklama metnini üretir. Metnin
> kalitesi rastgele değildir: her faturaya Faz A'da bir **kalite kategorisi**
> atanmıştır ve LLM o kategoriye sadık bir metin yazmak zorundadır.

![Faz B boru hattı](../diagrams/faz-b.png)

*Sol koldan alt küme seçimi ve batch'leme, ortada üretim çekirdeği ve LLM
çağrısı, sağda derleme. Pilot ve analiz modülleri üretimin zorunlu parçası
değildir; prompt kalibrasyonu için çekirdeğe geri besleme yaparlar. Sağ üstteki
kol ground-truth etiketi, alttaki kol model girdisini üretir.*

## Açıklama kalite kategorileri

Kategori Faz A'da atanır (`generators/aciklama_uretici.py`, `main.py`den
çağrılır) ve `faturalar_etiketler.json` içinde durur.

| kategori | ne demek | hedef oran |
| --- | --- | --- |
| `yeterli` | Kurumsal amacı ve kalemi açıkça anlatır | %50 |
| `yetersiz` | Anlamlı bilgi taşımaz ("Masraf.") | %20 |
| `manipulatif` | Gerçeği gizler, kılıf uydurur, ısrar eder | %20 |
| `ai_uretimi` | Yapay zekâ tarafından yazılmış izlenimi verir | %10 |

`manipulatif` beş dala ayrılır: `gizleme`, `bariz`, `kurnaz`, `zorunluluk` ve
`magduriyet` (sonuncusu `zorunluluk`un %30'u olarak ayrılır — kalıbı benzer,
öznesi farklıdır: "iş uzadı" ile "ben mahsur kaldım").

**"Kalite" = kategori sadakati, yazım güzelliği değildir.** İyi bir `yetersiz`
örneği ("Masraf.") düşük yazım kalitesi ve yüksek kategori sadakatidir ve
doğrudur. Veri seti hem iyi hem kötü örneklere muhtaçtır.

## Boru hattı

```
faturalar.json + faturalar_etiketler.json   (120k)
        │
        ▼
  batch_hazirla.py            25k alt küme seçer, 25×1000 batch'e böler -> python -m faz_b_aciklama_uretimi.batch_analiz --cikti-dizini data/aciklama_25k 
        |                                                                   batchin üretimde verilen detay raporu.         
        │
   batch_NNNN.json + durum.json
        │
        ▼
  aciklama_toplu_uret.py  ◄──►  Ollama / vLLM (Qwen3-8B)
        │                        prompt kurulumu ve denetim
        │                        aciklama_uretim_core.py'den gelir
   batch_NNNN_ciktilar.json
        │
        ├──► aciklama_birlestir.py  + faturalar.json
        │         └──► faturalar_aciklamali.json            MODEL GİRDİSİ
        │
        └──► onay_durumu_ata.py     + faturalar_etiketler.json
                  └──► faturalar_aciklamali_etiketler.json  GROUND TRUTH
```

Yan dallar (üretim zorunlu değil, kalibrasyon için):

- `aciklama_llm_pilot.py` — küçük örneklemle deneme koşusu, kalite testi
- `aciklama_analiz.py` — üretilen metinlerin çeşitlilik ve ihlal ölçümü

Pilot ile toplu üretim **aynı çekirdeği** kullanır (`aciklama_uretim_core.py`);
prompt, çağrı ve denetim mantığı tek kaynaktadır ve asla ayrışmaz.

## Alt küme neden ve nasıl seçiliyor?

120 bin faturanın hepsine açıklama üretmek zaman ve GPU bütçesi açısından
mümkün değil. Seçim **rastgele olamaz**: bazı anomali türleri havuzda birkaç
yüz kayıtken bazıları binlercedir; rastgele örnekleme bu dengesizliği aynen
taşır ve nadir türler eğitimde kullanılamaz hâle gelir.

Bunun yerine **tür başına taban ve tavan kotası** uygulanır:

```bash
python -m faz_b_aciklama_uretimi.batch_hazirla --toplam 25000 --batch-size 1000 \
    --tur-taban 400 --tur-tavan 600 --cikti-dizini data/aciklama_25k
```

Çoklu etiketli bir fatura seçildiğinde sahip olduğu **tüm** türlerin
kotasından düşer. Kalan hedef temiz faturalarla doldurulur. Fonksiyon tür
bazlı dağılımı ve tabanın altında kalan türleri raporlar.

Doğrulanmış sonuç (25k koşusu): kategori dağılımı **50,0 / 20,0 / 20,0 / 10,0**
tam, anomali oranı **0,281**, 14 türün hepsi temsil ediliyor.
`fatura_no_cakismasi` havuzda 397 kayıt olduğu için 400 tabanının altında
kalır — yapısal, beklenen bir durum, rapor bunu işaretler.

**Kategori dağılımı etiket değiştirilerek değil, o kategoriye sahip faturalar
seçilerek tutturulur.** Kategoriyi yeniden atamak Faz A'da kalibre edilmiş
etiketi bozardı.

### Kota seçiminin ölçülmüş gerekçeleri

`batch_hazirla.anomali_turu_kotali_sec` içindeki kararların sayısal dayanağı
(kod tarafında yalnız tek satırlık özetleri duruyor):

- **Kategori ekseni neden eklendi.** Seçim yalnız anomali türüne bakarken
  kompozisyon havuzun doğal dağılımına düşüyordu: 20k koşusunda
  **%56,0 / 29,6 / 6,9 / 7,5** ölçüldü, yani manipulatif hedefinin üçte biri.
- **Hedef salt seçimle karşılanabilir mi (fizibilite).** Tür kotaları altında
  seçilebilen anomalili faturaların 2991'i manipulatif, havuzda ayrıca 1495
  temiz manipulatif var: ulaşılabilir tavan **4486 ≥ 4000** (20k'nın %20'si).
  Manipulatif en dar eksendir, bu yüzden tür döngüsünde önceliği o alır.
- **Kıtlık sırası neden temiz havuza bakıyor.** Temizde yeterli 44.355,
  manipulatif 1.495. Temizde bol olanı kıt anomalili slotta harcamak israf.
- **Konteyner tavanı neden gerekli.** `genel_toplam` ve `satir_toplami`
  havuzlarının %100'ü aynı zamanda `footer_kismi`dir (injector yan etkisi).
  Konteyner tavanı 600'de kalırsa 300+300 bağımlı taban footer'a hiç yer
  bırakmaz ve ikisi birbirini bloklar; `_konteyner_tavanlarini_hesapla`
  örtüşmeyi (≥%90) veriden bulup bağımlı başına `tur_taban` kadar ek bütçe verir.
- **Kırpmada nadirlik anahtarı geri alındı (2026-07-29).** "Nadir türe ait olanı
  tut" kuralı, havuzu büyük ama tek etiketli türleri (`dusuk_ondalik_kaymasi`,
  havuz 2287) sıralamanın sonuna atıyordu; kategori ekseni de onları geri
  ittiği için tür 561'den 22'ye çöktü. Kırpma tek eksende kalmalı.
- **Kategori override neden varsayılan kapalı.** Açıkken 20k'da 4107 fatura
  (%20,5) override ediliyordu, en sık "temiz + yeterli → manipulatif" (1241).
  Bu hem kalibre edilmiş anomali↔kategori korelasyonunu bozar hem de yeni
  kategori etiket dosyasına geri yazılmadığı için metinle etiketi çeliştirir.

## Prompt katmanı

Tasarım ilkesi şu: model "üretmek" yerine **verilen slotları doğal Türkçeye
çevirsin**. Kalite sinyalleri prompt'a havuzlardan verilir.

**Sabit kalıp collapse üretir, havuz üretmez.** Ölçüldü: tek bir sabit ifade
`ai_uretimi` çıktılarının yarısını ele geçirmişti; 62 girdilik bir olay havuzu
ise çeşitliliği yükseltti.

Başlıca havuzlar:

- `ROL_DEPARTMAN`, `GRUP_OLAY`, `BIREYSEL_OLAY`, `DEPARTMAN_OLAY` — `yeterli`
  kategorisinin amaç çıpası. Prompt'a **%50 olasılıkla** girer.
- `MANIPULATIF_KILIF_HAVUZU` — `kurnaz` ve `bariz` dallarının çıpası.
- `MAGDURIYET_CERCEVELERI` — beşinci dalın kendi havuzu.
- `GRUP_YUKLEM` — cümlelerin %20'si "aldım" ile bitiyordu; kaleme uyan yüklem
  önerilir.
- `ayrilma_eki()` — `-dan/-den/-tan/-ten` Türkçede kurallıdır ama 8B model
  tutturamıyor. Doğrusu hesaplanıp prompt'a hazır verilir.

Sistem promptu **sabittir** ve bu önemlidir: sabit olduğu için sunucu tarafında
önbelleklenir. Persona, uzunluk gibi değişkenler kullanıcı prompt'una konur.
Bu sabitliği bozma.

### Ölçülmüş prompt kararları

Bunlar sezgiye aykırı çıktığı için yazılıdır:

- **Olay enjeksiyonu %50'de kasıtlı.** Her `yeterli` metin bağlam cümlesiyle
  başlarsa şablon imzası doğar ve aşağı akıştaki model kategoriyi içerikten
  değil kalıptan öğrenir. %100'e çıkarmak zaten hiçbir şeyi düzeltmedi
  (totoloji %10,2 → %11,2); talimata tek cümlelik bir kısıt eklemek %6,1'e
  indirdi. **Mekanizmayı büyütmeden önce kısıtı söyle.**

- **"Pembe fil" evrensel değil.** "Yasak ifadeyi anma" ilkesi genelde doğrudur,
  ama `yeterli` kategorisinde açık yasağı kaldırmak sızıntıyı %3,0'ten %10,2'ye
  çıkardı. Açık yasak yük taşıyabilir; kaldırmadan önce ölç.

- **Uzunluk bandı ile kategori gerçeği senkron olmalı.** `yeterli` "çok kısa"
  (8-45 karakter) bandını alamaz: amaç ve kalem 45 karaktere sığmıyor, atanma
  %20 iken fiilen yazılan %7'ydi ve ihlallerin %89'u oradan geliyordu.

- **Prompt'a verilen olay havuzları ürün adı içeremez.** Havuza "sunum için
  taşınabilir bellek" konduğunda model fişte olmayan o ürünü satın almış gibi
  yazdı. Olay bir **bağlam** anlatır ("sunum hazırlığı"), asla bir kalem.

- **Üretim havuzu ile doğrulama kuralı senkron olmalı.** 134 olay girdisinin
  34'ü kendi doğrulama kuralımızı geçemiyordu: model bizim verdiğimiz bağlamı
  yazıyor, kural "amaçsız" diyor, retry düzeltemiyor. Saf israf. Havuza yeni
  girdi eklerken kuralın onu tanıdığını test et.

- **Yanlış kural pahalıdır, kural pahalı değildir.** 25k'da her 10 puan retry
  yaklaşık 8 saat eder. Doğru kural ya ilk denemede uyum sağlatır ya ikincide
  düzeltir; yanlış kural iki çağrıyı da çöpe atar.

## Denetim ve retry

Üretilen metin kural tabanlı denetimden geçer (`ihlalleri_bul`). LLM-judge
kullanılmaz; denenmiş ve güvenilmez bulunmuştur.

İhlal bulunursa metin **bir kez** yeniden istenir, prompt'a düzeltme notu
eklenerek. İkinci denemede de ihlal kalırsa kayıt yine de veri setine girer ve
`kalan_ihlaller` alanında işaretlenir. Kalite ölçütü budur: retry oranı değil,
**iki denemede de düzelmeyen** kayıt oranı.

Bir kalite süzgeci (`elenmeli_mi`) yazılmıştır ama **varsayılan kapalıdır**
(`--eleme`). Açmadan önce çözülmesi gereken bir sorun var: eleme listesindeki
enum sızıntısı kuralı bir leakage değil üslup kuralıdır ve yanlış pozitif
üretiyor.

## Sağlayıcılar

`--saglayici ollama | groq | vllm`

| sağlayıcı | ne zaman |
| --- | --- |
| `ollama` | Yerel geliştirme ve pilot (varsayılan) |
| `vllm` | Kaggle/Colab'da kendi sunucun — anahtarsız, hız sınırlayıcısız |
| `groq` | Bulut API; anahtar `.env`den okunur, istemci tarafı kota sınırlayıcı devreye girer |

OpenAI uyumlu arayüzlerde `min_p` güvenlik ağı bulunmadığı için sıcaklık çıplak
kalır; `--sicaklik-tavani 0.9` ile kırpılır. Ölçüldü: 0.9 her eksende kazandı,
çeşitlilik dahil.

**Model kararı: Qwen3-8B.** Trendyol-LLM-8B ve Qwen2.5-32B-AWQ denendi. İlki
serbest üretimde daha akıcı ama verilen bağlama uymak yerine kendi amacını
icat ediyor; ikincisi bir önceki nesil ve çok yavaş. Plan katmanı geldikten
sonra hangi modelin iyi olduğu **değişti** — model seçimini prompt mimarisinden
bağımsız değerlendirme.

## Derleme

```bash
python -m faz_b_aciklama_uretimi.aciklama_birlestir --cikti-dizini data/aciklama_25k --sadece-uretilenler
python -m faz_b_aciklama_uretimi.onay_durumu_ata    --cikti-dizini data/aciklama_25k
```

`--cikti-dizini` varsayılanı `data/aciklama`'dır; 25k koşusunda **iki komuta da
açıkça ver**, yoksa yanlış dizini okur.

Birleştirme adımı 120 binlik `faturalar.json`'ı okur çünkü fatura alanlarının
tek kaynağı odur — batch dosyaları prompt için kırpılmış kopyalardır ve
satıcı VKN'si, iş kolu, alıcı bilgileri ve toplamları içermezler.
`--sadece-uretilenler` ile yalnız açıklaması olan kayıtlar yazılır.

Batch dosyalarında `is_anomali` ve `anomali_turleri` **bulunur** (üretim için
gerekli), ama birleştirme adımı çıktılardan yalnızca `aciklama_metni`'ni alır.
Model girdisi dosyasında hiçbir etiket alanı yoktur.

`onay_durumu` kararı için `04-etiketler.md`.

## Resume ve çok makineli koşu

Runner her fatura tamamlandığında çıktı dosyasına **anında** yazar ve yeniden
başlatıldığında üretilmiş `kayit_id`'leri atlar. Aynı komut kaldığı yerden
devam eder.

Çok makineli üretim için `--batch 1-10` gibi ayrık aralık verilir. Bu modda
paylaşılan `durum.json`'a **yazılmaz**, iki makine birbirinin durumunu ezmesin
diye; gerçek durum zaten çıktı dosyalarındadır.

Uzun koşularda `--ilerleme 1000` her bin kayıtta bir özet satırı basar. Bu
satır ayrı bir akışa (stderr) yazılır; fatura başına ayrıntı log dosyasına
giderken ilerleme canlı görünür.

## Ayrıntı

Prompt tasarımının tarihsel kayıtları ve ölçümleri: `arsiv/faz-b-prompt.md`.
