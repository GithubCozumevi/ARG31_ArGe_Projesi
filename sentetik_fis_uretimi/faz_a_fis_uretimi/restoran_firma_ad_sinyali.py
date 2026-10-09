"""
Restoran firma adlarından (data/firma_adlari_osm.csv, is_kolu=restoran, 4.000
ad) NET olan dar alt tip sinyallerini regex ile çıkarır. Amaç TAM kapsama
DEĞİL -- "net olanı al, uç noktaları kovalama" (2026-08-17 kararı): yalnız
tek anlamlı kelime/marka içeren adlar eşleşir, geri kalan `genel`e (boş)
düşer ve kullanıcı elle bakar.

Desenler restoran_alt_tip_etiketle.py'deki (ürün tarafı) dar tiplerin AYNISI.
`corbaci` 2026-08-18'de EKLENDİ -- çorba ürünleri evrensel havuzda kalmaya
devam ediyor (herkes satabilir) ama gerçek adlarda net bir "çorbacı" sinyali
ölçüldüğü için firma kimliği olarak da tanınıyor artık; ikisi çelişmiyor
(bkz. docs/dökümantasyon/07-alt-tip-mekanizmasi.md). `cafe` de aynı tarihte
YENİ bir dar tip olarak eklendi, kendi ürün havuzuyla (kahve çeşitleri +
atıştırmalık sandviç + dilimlik pasta, `cafe_icecekleri`/`cafe_atistirmaliklari`/
`cafe_tatlilari` bölümleri) birlikte. `steakhouse` da aynı tarihte eklendi
(`steakhouse_etler` bölümü) -- BİLEREK DAR: yalnız "steakhouse" kelimesi ve
kesim adları (pirzola/tbone/ribeye/tomahawk) eşleşir, bare "et"/"mangal"/
"biftek" ÖLÇÜLÜP elendi (kullanıcının elle etiketlediği veride bunlar zaten
donerci/kebapci'ye gitmiş, ayrı bir sinyal değil).

SIRA ÖNEMLİ (MUTFAK_KISITLARI ile aynı sözleşme, field_generator.py):
ilk eşleşen kazanır. fırın deseni pide'DEN SONRA gelmeli ('Taş Fırın Pide'
bir pidecidir, 2026-08-13 dersi). tantuni/ciğerci kebap'TAN ÖNCE gelmeli
(genel "kebap" deseni onları da yakalardı).

Çıktı: data/companies/restoran_ad_sinyali_taslak.csv (isim, onerilen_alt_tip,
desen) -- firma_registry.csv'ye YAZILMAZ, bu bir TASLAK; registry alt_tip
kolonu ayrı bir adımda (registry yeniden üretimiyle birlikte) eklenecek.

2026-08-21: bu script'in 2026-08-18/21 çıktısı (regex + elle inceleme, bkz.
docs/dökümantasyon/07-alt-tip-mekanizmasi.md) zaten `data/companies/
firma_adlari_osm_alt_tip.csv`'nin restoran alt_tip kolonuna YAZILDI ve eski
taslak/revize CSV'leri silindi -- bu script yalnız firma_adlari_osm.csv
YENİDEN ÇEKİLİRSE (yeni firma seti) tekrar çalıştırılmalı.

Kullanım:
    python -m faz_a_fis_uretimi.restoran_firma_ad_sinyali
"""

import csv
import re
from pathlib import Path

OSM_CSV = Path("data/companies/firma_adlari_osm.csv")
CIKTI_CSV = Path("data/companies/restoran_ad_sinyali_taslak.csv")


def ascii_kucuk(metin: str) -> str:
    metin = metin.replace("İ", "i").replace("I", "ı").lower()
    return metin.translate(str.maketrans("ğüşıöç", "gusioc"))


# (alt_tip, desen) -- SIRA ÖNEMLİ, ilk eşleşen kazanır.
#
# 2026-08-18 GÜNCELLEMESİ: kullanıcının 4.000 restoran adını elle taradığı
# revize CSV'sinden (data/restoran_ad_sinyali_taslak_revize.csv) ölçülen
# örüntüler eklendi -- bkz. docs/dökümantasyon/07-alt-tip-mekanizmasi.md.
#   bufe -> tostcu (%89,5, n=66 etiketli), corba/iskembe -> corbaci (%63,6,
#   n=11), tavuk/chicken/bbq -> tavukcu (%86,4, n=22) -- hepsi kullanicinin
#   elle etiketlediği verideki ÇOĞUNLUK sinyaline dayanıyor.
#   'cafe'/'kafe' YİNE eklenmedi: aynı CSV'de 133 "cafe" adının etiketli
#   55'i borekci/pastane arasında dağınık VE borekci'ye giden örneklerin
#   çoğu zaten "börek"/"simit" kelimesi taşıdığı için o desenle eşleşiyor,
#   "cafe" kelimesinin kendisiyle ilgisi yok -- gerçek ayırt edici sinyal
#   yok, ölçülerek doğrulandı.
DESENLER: list[tuple[str, str]] = [
    ("cigkofteci", r"cig ?kofte"),
    ("kokorecci", r"kokorec"),
    ("tantunici", r"tantuni"),
    ("cigerci", r"cigerci\b"),
    ("corbaci", r"corba|corbaci|iskembe"),
    ("borekci", r"borek|boreg|borekci"),
    ("uzakdogu", r"sushi|susi|\bwok\b|japon|ramen|noodle|teriyaki|uzak ?dogu"
                 r"|asya mutfa|cin mutfa|cin lokanta"),
    ("pastane", r"pastane|patisserie|\bwaffle\b|dondurma|magnolia"),
    ("tavukcu", r"tavuk|chicken|\bbbq\b"),
    ("tatlici", r"tatlici|baklava|helvaci"),
    ("balikci", r"\bbalik\b|balikcisi|deniz urunleri|deniz mahsul|midyeci"),
    ("pizzaci", r"pizza|domino|little caesar|sbarro|papa john"),
    ("pideci_lahmacunci", r"\bpide\b|pideci|lahmacun|etli ekmek|gozleme"),
    ("borekci", r"\bfirin|halk ekmek|unlu mamul|\bsimit\b|pogaca"),  # pide'den SONRA
    ("burgerci", r"burger|mcdonald|burger king"),
    ("tostcu", r"\btost\w*|sandvic\w*|\bkumru\b|subway"),
    # 'durum'/'durumcu' BURADA -- kebapci'DEN cikarildi (2026-08-18): urun
    # verisinde "Durum Doner" donerci'ye etiketli, kullanicinin elle
    # etiketlemesiyle de tutarli (bkz. 07-alt-tip-mekanizmasi.md).
    ("donerci", r"\bdoner\b|donerci|\bdurum\b|durumcu"),
    # 'kofteci' BİLEREK YOK (2026-08-18): bölgesel köfte kimliği (Tekirdağ/
    # Sivas/İnegöl) var, tek bir paylaşımlı köfteci alt tipi yanlış eşleşme
    # riski taşır (altyapı yok); köfteci adları `genel`e düşsün, kullanıcı
    # elle karar versin. 'mangalbasi' EKLENDİ, kullanıcı elle kebapci yazmıştı;
    # AYRI bir ocakbaşı/mangalbaşı alt tipi açılmadı (2026-08-18 kararı) --
    # kebapci ile aynı ızgara-et havuzunu paylaşıyor sayılıyor.
    ("kebapci", r"kebap|kebab|ocakbasi|mangalbasi|iskender"),
    # 'steakhouse' YENİ (2026-08-18): kebapci'den AYRI -- kırmızı et kesimi
    # ağırlıklı batı tarzı menü (T-Bone/Tomahawk/Ribeye), kebapçının ızgara
    # kebap havuzuyla örtüşmüyor. Kendi ürün havuzu var (steakhouse_etler).
    # BİLEREK DAR: 'et'/'et mangal'/'biftek'/bare 'steak' ÖLÇÜLDÜ ve elendi --
    # kullanıcının elle etiketlediği CSV'de bunlar zaten çoğunlukla donerci/
    # kebapci/tantunici'ye gitmiş (net bir "steakhouse" çoğunluğu yok, n küçük
    # ve tutarsız). Yalnız işletmenin kendini AÇIKÇA "steakhouse" diye
    # adlandırdığı ya da kesim adı taşıyan durumlar kalıyor; pirzola/tbone/
    # ribeye/tomahawk gerçek adlarda şu an 0 kez geçiyor (kalemlerle birebir
    # örtüşen, yanlış eşleşme riski sıfır -- nadiren tetiklenmesi beklenir).
    ("steakhouse", r"steak ?house|pirzola|t.?bone|ribeye|tomahawk"),
    # 'cafe' EN SONDA (2026-08-18): daha spesifik türler (börek/pastane/
    # tavukçu vb.) önce denenmeli -- "Mir Börek Cafe" gibi adlar hâlâ
    # börekci'ye düşsün, cafe yalnız BAŞKA HİÇBİR şeyle eşleşmeyen adlarda
    # devreye girsin. Kendi ürün havuzu var artık (cafe_icecekleri/
    # cafe_atistirmaliklari/cafe_tatlilari), bkz. yukarıdaki not.
    ("cafe", r"\bcafe\b|\bcaffe\b|\bcafee\b|\bkafe\b"),
]


def alt_tip_bul(ad: str) -> tuple[str, str]:
    a = ascii_kucuk(ad)
    for alt_tip, desen in DESENLER:
        if re.search(desen, a):
            return alt_tip, desen
    return "", ""


def main():
    isimler = []
    with open(OSM_CSV, encoding="utf-8") as f:
        for satir in csv.DictReader(f):
            if satir.get("is_kolu") == "restoran":
                isimler.append(satir["isim"])

    sonuc = []
    sayim: dict[str, int] = {}
    for ad in isimler:
        alt_tip, desen = alt_tip_bul(ad)
        sonuc.append({"isim": ad, "onerilen_alt_tip": alt_tip, "desen": desen})
        anahtar = alt_tip or "(esleşmedi)"
        sayim[anahtar] = sayim.get(anahtar, 0) + 1

    with open(CIKTI_CSV, "w", newline="", encoding="utf-8") as f:
        yazici = csv.DictWriter(f, fieldnames=["isim", "onerilen_alt_tip", "desen"])
        yazici.writeheader()
        yazici.writerows(sonuc)

    print(f"[+] {len(isimler)} restoran adı işlendi -> {CIKTI_CSV}\n")
    esleşen = sum(v for k, v in sayim.items() if k != "(esleşmedi)")
    print(f"Eşleşen: {esleşen} (%{100*esleşen/len(isimler):.1f})  "
          f"Eşleşmeyen: {sayim.get('(esleşmedi)', 0)}\n")
    for alt_tip, n in sorted(sayim.items(), key=lambda x: -x[1]):
        print(f"    {alt_tip:<22} {n}")


if __name__ == "__main__":
    main()
