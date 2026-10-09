"""
Market firma adlarindan (data/firma_adlari_osm.csv, is_kolu=market, 4.000 ad)
NET olan dar alt tip sinyallerini regex ile cikarir -- restoran_firma_ad_sinyali.py
ile AYNI yontem ve sozlesme (2026-08-21 karari, bkz. docs/dökümantasyon/
07-alt-tip-mekanizmasi.md).

NEDEN GEREKLI: market icin OSM'in kendi etiketi (firma_adlari_osm_alt_tip.csv)
%100 `genel`e dusuyor -- ham cekimde yalniz iki kaba shop= etiketi var
(convenience 2.390, supermarket 1.589), butcher/greengrocer/confectionery/
kiosk/alcohol/tobacco/seafood HIC gelmemis. Oysa firma ADLARINDA bu sinyal
acikca duruyor ("Uludag Kuruyemis", "Sultanbeyli Balikcisi", "Cilli Tekel
Sarkuteri") -- OSM'in gormedigi bilgi isimde yaziyor. Hedef alt tipler
market_urunleri_ozet.csv'nin `alt_tipler` kolonuyla BIREBIR ortusuyor
(sekerci/bufe/kasap/manav/kuruyemisci/firin/balikci) -- urun tarafi zaten
hazir, eksik olan firma tarafiydi.

SIRA ONEMLI (restoran ile ayni sozlesme): ilk eslesen kazanir. Gercek
verideki en sik coklu-eslesme "tekel + kuruyemis/sarkuteri" hibrit
isimleridir (37 ornek olculdu, orn. "Yagmur Tekel Kuruyemis", "Candas
Tekel Kuruyemis Sarkuteri") -- Turkiye'de tekel bayileri fiilen kuruyemis
ve sarkuteri de satar, bu YANLIS etiketleme degil gercek hibrit isletme.
Bu yuzden DAHA SPESIFIK urun sinyali (kuruyemis/kasap/manav/balikci/firin)
`bufe`den (tekel/buyfe -- en genis, en az bilgi tasiyan terim) ONCE
denenir. `sekerci` dusuk verim (yalniz 6/4000) ama sifir yanlis pozitif
olcduldu (baklava/tatlici/sekerci disinda hicbir seye carpismiyor).

`bufe` hem 'tekel' hem 'bufe/büfe' kelimesini kapsar -- firma_adlari_osm_cek.py:
ALT_TIP_ERITME'deki ayni karari tekrarlar (tutuncu/tekel -> bufe: alkol/sigara
yasakli kategori, urun tarafinda ayri bir havuzu yok, ikisi de icecek/
atistirmalik satar).

Cikti: data/companies/market_ad_sinyali_taslak.csv (isim, onerilen_alt_tip,
desen) -- firma_registry.csv'ye YAZILMAZ, bu bir TASLAK; registry alt_tip
kolonu ayri bir adimda (registry yeniden uretimiyle birlikte, CLAUDE.md Acik
Isler madde 3) eklenecek.

2026-08-21: bu script'in ciktisi zaten `data/companies/
firma_adlari_osm_alt_tip.csv`'nin market alt_tip kolonuna YAZILDI ve eski
taslak CSV'si silindi -- bu script yalnız firma_adlari_osm.csv YENİDEN
ÇEKİLİRSE (yeni firma seti) tekrar çalıştırılmalı.

Kullanim:
    python -m faz_a_fis_uretimi.market_firma_ad_sinyali
"""

import csv
import re
from pathlib import Path

OSM_CSV = Path("data/companies/firma_adlari_osm.csv")
CIKTI_CSV = Path("data/companies/market_ad_sinyali_taslak.csv")


def ascii_kucuk(metin: str) -> str:
    metin = metin.replace("İ", "i").replace("I", "ı").lower()
    return metin.translate(str.maketrans("ğüşıöç", "gusioc"))


# (alt_tip, desen) -- SIRA ONEMLI, ilk eslesen kazanir. Alt tip adlari
# market_urunleri_ozet.csv:alt_tipler kolonuyla AYNI yazilmali.
DESENLER: list[tuple[str, str]] = [
    ("manav", r"\bmanav\w*|\bsebze\w*|\bmeyve\w*"),
    ("balikci", r"\bbalik\b|balikcisi|deniz urun|deniz mahsul|midyeci"),
    ("kuruyemisci", r"kuruyemis"),
    # firin kasap'tan ONCE: "Ugur ekmek sarkuteri" gibi isimlerde ekmek/firin
    # sinyali sarkuteri'den daha spesifik/birincil (2026-08-21 olcumu).
    ("firin", r"\bfirin\w*|halk ekmek|unlu mamul|\bsimit\b|pogaca|\bekmek\b"),
    # sarkuteri (deli/sarap-sucuk tezgahi) kasap'in en yakin karsiligi --
    # market_urunleri_ozet.csv'de ayri bir 'deli' alt tipi yok.
    ("kasap", r"\bkasap\w*|kasabi|sarkuteri"),
    ("sekerci", r"sekerci|baklava\w*|helvaci|tatlici|tatli center"),
    # bufe EN SONDA: en genis/en az bilgi tasiyan terim (tekel/buyfe), daha
    # spesifik urun sinyali varsa ona kaybetmeli (bkz. modul docstring'i).
    ("bufe", r"\btekel\b|\bbufe\b|\bbüfe\b"),
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
            if satir.get("is_kolu") == "market":
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

    print(f"[+] {len(isimler)} market adı işlendi -> {CIKTI_CSV}\n")
    esleşen = sum(v for k, v in sayim.items() if k != "(esleşmedi)")
    print(f"Eşleşen: {esleşen} (%{100*esleşen/len(isimler):.1f})  "
          f"Eşleşmeyen: {sayim.get('(esleşmedi)', 0)}\n")
    for alt_tip, n in sorted(sayim.items(), key=lambda x: -x[1]):
        print(f"    {alt_tip:<22} {n}")


if __name__ == "__main__":
    main()
