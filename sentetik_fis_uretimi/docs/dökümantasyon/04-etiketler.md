Claude code kullanımında kolaylık sağlaması ve token tüketimi azaltması için bu dosya lazım bir guide dökümantasyonu değil.
# Etiketler — onay_durumu ve karar tabloları (ayrıntı)

> CLAUDE.md'den taşındı. Bölüm numaraları ESKİ hâlinden korundu (§13 tek anlamlıdır,
> CLAUDE.md'de karşılığı yok); bu dosyaya atıf `docs/etiketler.md §13` biçiminde.

## 13. Onay Durumu Etiketi (`onay_durumu`) — REVİZE EDİLDİ (2026-07-28)

Çalışanın submitlediği fişin admin/muhasebe tarafındaki sonucunu temsil eden
GROUND-TRUTH etiketi. `fatura_to_dict`'e DAHİL DEĞİL (leakage —
`is_anomali`/`aciklama_kategorisi` ile aynı ilke) ve **Ollama hiçbir aşamada görmez**
(üretim tarafı yalnız `aciklama_kategorisi` taşır).

**Değerler:** `onaylandi` / `gozden_gecirilecek` / `onaylanmadi`.

**Nerede (DEĞİŞTİ):** artık Faz A'da DEĞİL, **Faz B'nin SON adımında** atanır →
`onay_durumu_ata.py` (kök dizin, ayrı modül). `main.py` bu etiketi ne hesaplar ne
export eder; `schema.Fatura`'da alanı yoktur; `generators/aciklama_uretici.py`'den
KALDIRILDI (orada yalnız kategori ataması kalır).

Sebep: muhasebe kararı ancak açıklama METNİ üretilip okunduktan sonra verilebilir.
Faz A'da atamak "önce karar, sonra açıklama" gibi ters bir nedensellik kuruyordu.

**Karar tablosu — sürücü `(is_anomali × aciklama_kategorisi)`, rastgelelik YOK:**

| | anomalisiz | anomalili (A/B ayrımı YOK) |
| --- | --- | --- |
| `yeterli` | **onaylandi** | onaylanmadi |
| `yetersiz` | gozden_gecirilecek | onaylanmadi |
| `manipulatif` | gozden_gecirilecek | onaylanmadi |
| `ai_uretimi` | gozden_gecirilecek | onaylanmadi |

Yani: **anomali varsa doğrudan RED**; anomali yoksa yalnız `yeterli` onaylanır,
diğer üç kategori incelemeye düşer.

**Eski kural (A/B grubu × kategori, 12 hücre) BIRAKILDI.** B-grubu teknik hatalar
"muhasebe düzeltir, onaylar" varsayımıyla `gozden_gecirilecek`'e düşüyordu; yeni
kuralda anomalinin kaynağı (çalışan mı, sistem mi) onay kararını değiştirmez. O ayrım
zaten `anomali_turleri`'nde duruyor ve `schema.anomali_grubu` ile her an türetilebilir
(sabitler analiz/kalibrasyon ekseni olarak schema.py'de KALDI, artık onay kararında
kullanılmıyor).

> **Kabul edilen taviz:** bu kuralla anomalili tarafta `onay_durumu` fiilen
> `is_anomali`'nin kopyasıdır; ek bilgi yalnız ANOMALİSİZ tarafta (kategori ekseninde)
> vardır. Bilinçli karar — model çıktısı olarak "red" kuralı basit ve tartışmasız olsun.

> **Rastgele karışım neden YOK:** hiçbir gözlenebilir değişkene bağlı olmayan rastgelelik
> saf ETİKET GÜRÜLTÜSÜdür — yazı-turası öğrenilemez, yalnız ulaşılabilir doğruluk tavanını
> düşürür ve varyansı artırır.

**Çalıştırma / çıktı:**

```bash
python -m faz_b_aciklama_uretimi.onay_durumu_ata    # aciklama_birlestir.py'den SONRA
# data/aciklama/batch_*_ciktilar.json + data/faturalar_etiketler.json
#   → data/faturalar_aciklamali_etiketler.json
```

- Yalnız **açıklaması ÜRETİLMİŞ** kayıtlara etiket üretir (metin yoksa karar da yok) →
  `faturalar_aciklamali.json` (model girdisi) ile aynı alt küme, `kayit_id` ile eşleşir.
- Kategori kaynağı **batch çıktısındaki** `aciklama_kategorisi`'dir (metin fiilen ona göre
  yazıldı); `--kategori-override` kullanılmışsa etiket dosyasındakinden farklı olabilir,
  modül farkı sayar/uyarır ve batch'i esas alır — böylece metin ile etiket çelişmez
  (§13.2'de anlatılan eski tutarsızlık burada kapanıyor).
- Girdi etiket dosyasında eski kuraldan kalma bir `onay_durumu` varsa yok sayılıp
  yeniden hesaplanır.

**Ölçülen dağılım (ESKİ kuralla, referans):** 100k `--anomali-orani 0.15` → onaylandi
%50.1 / gozden %43.1 / onaylanmadi %6.7; `0.25` ile onaylanmadi %10.4, 20k'lık batch alt
kümesinde %10.2. Yeni kuralda `onaylanmadi` doğrudan alt kümenin anomali oranına eşittir
(20k kota seçiminde ~%25-30) — yani red sınıfı azınlık olmaktan çıkar. **Azınlık sınıfını
kuralı bozarak değil, `--anomali-orani` ile ayarla.**

### 13.2 Karara bağlanan diğer maddeler (aynı turda uygulandı)

- **VKN "aynı ad / farklı VKN" YANLIŞ ALARMI kapatıldı.** `gecersiz_kimlik_no` VKN'yi
  bilerek bozuyor; aynı firmanın diğer faturaları gerçek VKN'siyle durduğu için sahte
  çelişki doğuyordu (100k'da 1208 ad → 6599 fatura, **4560'ı hiç anomalisi olmayan
  tertemiz fatura**). Etiketlere HİÇ sızmıyordu (`kural_ihlali_turlerini_tespit_et`
  VKN'ye bakmaz, `hatali_fatura_sayisi` yalnız ekrana basılır) ama raporu yanıltıyordu.
  Muafiyet iki yerde: `validators.KIMLIK_MUAF_ANOMALILER` (haritalara hiç girmez, O(1))
  ve `main.py:korunan_adlar`. Doğrulandı: uyarı 0, `Hatalı Fatura` ≈ anomali sayısı.
- **Ondalık kayması alt bandı gevşetildi.** `ONDALIK_KAYMASI_ALT_BANT_CARPANI = 10`
  (üst yön `×5` değişmedi). Fiyat dağılımı sağa çarpık olduğu için `min/5` doğal ucuz
  kalemlerin içine düşüyordu → union, pozitif sınıfın ~%24'ünü doğal ucuz kalemlerle
  dolduruyordu ("ucuz = anomali" diye YANLIŞ kavram öğretiyordu). Enjekte etiketler
  kaybolmaz (union additive). Asimetri ~42× → ~1×.
- **Fat-finger kayma büyüklüğü ağırlıklandı:** `_kayma_carpani_sec()` 10x/100x'i **70/30**
  seçer (eskiden 50/50). Gerçekte ondalık noktası çoğunlukla tek basamak kayar.
- **batch_hazirla kategori override'ı VARSAYILAN KAPALI** (`--kategori-override` ile
  açılır). Ölçüldü: 20k'da 4107 fatura (%20.5) override ediliyordu, en sık
  "temiz + yeterli → manipulatif" (1241). Bu, `ACIKLAMA_KATEGORI_ORANLARI`'nın kalibre
  edilmiş anomali↔kategori korelasyonunu bozuyor; üstelik override edilen kategori
  etiket dosyasına GERİ YAZILMADIĞI için (`aciklama_birlestir.py` bu alana dokunmaz)
  metin ile `aciklama_kategorisi`/`onay_durumu` çelişir hale geliyordu.
  Kapalıyken 20k kompozisyonu ~%56.7/29.7/7.3/6.3 olur (50/20/20/10 DEĞİL); manipulatif
  payını artırmanın doğru yolu `--anomali-orani`'nı yükseltmektir (havuz büyür), kategori
  yeniden atamak değil.

---

