"""
market_urunleri_ozet.csv'nin `alt_tipler` kolonunu doldurur (Açık işler #1,
CLAUDE.md). Kapsam yalnız GIDA + SÜT KAHVALTILIK + İÇECEK (temiz üretimde dar
market tiplerinin (kasap/manav/kuruyemisci/bufe/sekerci/balikci/firin) izinli
kategorilerinde yalnız bunlar var); KOZMETİK/DETERJAN/KAĞIT/EV/BEBEK/SİGARA/PET
hiç etiket istemiyor.

KASAP/MANAV deterministik: csv_kategori == ET TAVUK / MEYVE SEBZE. Geri kalan
beş etiket ürün adı ANAHTAR KELİMEYLE bulunuyor -- ama bu proje tam olarak
"ürün adında anahtar kelime = yanlış sınıflama" tuzağından kaçınmak için
kuruldu (kuruyemişçi + aroma sıfatı örneği). O yüzden her pozitif desenin
yanında bir NEGATİF filtre var: ürün markalı/paketli olduğu için "fıstık"
kelimesi çoğu zaman çikolata/gofret/puding/dondurma AROMASI olarak geçiyor,
ham ürünün kendisi değil (ör. "MAGNUM BADEM", "ULKER GOLF ... ANTEP F." --
ikisi de dondurma serisi; "SENER RECEL INCIR" -- reçel, kuru incir değil).

Desenler yinelemeli ölçülerek kalibre edildi (bkz. konuşma geçmişi); ~yüzde
birkaçlık kalıntı gürültü (SUTAS TATLIMMM gibi tekil aykırı örnekler) kabul
edilebilir -- amaç kusursuzluk değil, hatanın ÜRÜN BAZLI VE SONLU olması
(regex'in firma adına uygulanmasındaki SİSTEMİK hatanın aksine).

Yeniden üretimde elle düzeltilmiş satırlar `market_ozet_olustur.py`'deki
`mevcut_alt_tipleri_koru` ile zaten korunuyor; bu script COKO CSV'yi baştan
yazar, o yüzden yalnız BOŞ `alt_tipler` hücrelerini doldurur, elle girilmiş
olanların üzerine yazmaz.

Kullanım:
    python -m faz_a_fis_uretimi.market_alt_tip_etiketle            # CSV'yi günceller
    python -m faz_a_fis_uretimi.market_alt_tip_etiketle --dry-run   # yalnız rapor, yazmaz
"""

import argparse
import csv
import re
from pathlib import Path

CSV_YOLU = Path("data/urun_verileri/market_urunleri.csv")

DETERMINISTIK_KATEGORI = {"ET TAVUK": "kasap", "MEYVE SEBZE": "manav"}
DESENLI_KATEGORILER = {"GIDA", "SÜT KAHVALTILIK", "İÇECEK"}


def ascii_kucuk(metin: str) -> str:
    """generators/field_generator._ascii_kucuk ile AYNI -- Türkçe İ/I'yı
    .lower() ÇAĞRILMADAN ÖNCE değiştirir (aksi halde 'İ'.lower() birleşen
    nokta üretir ve alt çizgisiz/boşluklu desen eşleşmesini sessizce bozar)."""
    metin = metin.replace("İ", "i").replace("I", "ı").lower()
    return metin.translate(str.maketrans("ğüşıöç", "gusioc"))


# (etiket, pozitif desen, negatif desen ya da None)
# SIRA ÖNEMLİ DEĞİL -- her desen bağımsız test edilir, bir ürün BİRDEN FAZLA
# etiket alabilir (';' ile birleştirilir), market_urunleri_ozet.csv'nin zaten
# desteklediği çoklu-üyelik.
DESENLER: list[tuple[str, str, str | None]] = [
    ("kuruyemisci",
     r"\bfistik\w*|\bbadem\w*|\bceviz\b|\bcevizi\b|\bfindik\w*|\bleblebi\w*"
     r"|\bkuru ?uzum\w*|\bkaju\w*|\bkavrulmus\w*|\bcerez\b|\bcerezler\b"
     r"|\bcekirdek\w*|\bkuru ?kayisi\w*|\bincir\w*",
     r"cikolat|biskuvi|gofret|\bkek\b|dra[gj]e|wafer|kakao|krem|pasta|tablet"
     r"|cips|chips|cheetos|hanimel|dolgu|ezme|helva|nescafe|cafe crown"
     r"|3 ?in ?1|\bcik\.|puding|pudding|magnum|\bgolf\b|carte ?d.?or"
     r"|viennetta|\byagi\w*|buse|dondurma|biscolat|\bsekeri\b|\bsekerli\b"
     r"|\brecel\w*"),
    ("sekerci",
     r"cikolat|lokum|sekerleme|gofret|\bhelva\w*|dra[gj]e|jelibon|marsmelov"
     r"|karamel|toffe?e|sakiz",
     None),
    ("bufe",
     r"\bcola\b|\bkola\b|gazoz|\bsoda\b|enerji ?icecegi|\bcips\b|\bchips\b"
     r"|cheetos|\bkraker\b|\bpopcorn\b|patlamis misir|ice tea|\bayran\b"
     r"|\bmaden ?suyu\b",
     None),
    ("balikci",
     r"\bton ?bali(gi|k)|\bhamsi\b|\buskumru\b|\bsardalya\b|\bsomon\b"
     r"|\blevrek\b|\bcupra\b|\bmidye\b|\bkarides\b|\bahtapot\b|\bbalik\b",
     r"kraker|makarna"),
    ("firin",
     r"\bekmek\w*|\bsimit\w*|\bpogaca\w*|\blavas\w*|\bacma\w*|\bgrissini\w*",
     None),
]


# Desen negatifiyle YAKALANAMAYAN tekil yanlış pozitifler (2026-08-17'de elle
# gözden geçirilip onaylandı). Bunlar aslında market/süpermarket ürünü --
# etiket temizlenir ama satır SİLİNMEZ, ürün havuzda "genel" olarak kalır.
# Yeni bir desen degisikligi bunlari YENIDEN etiketlemesin diye ISTISNA listesi
# desenlerden ONCE kontrol edilir (bkz. urune_etiket_ata).
ELLE_ISTISNA: set[str] = {
    "SUTAS TATLIMMM 4LU FINDIKLI",              # sut tatlisi, cekirdek degil
    "NESTLE 40GR ANTEP FISTIKLI SUTLU CIKOLA",  # cikolata (adi kesik yazilmis)
    "MILKA 80GR ANTEP FISTIK CIKOLT",           # cikolata (adi kesik yazilmis)
    "SOLEN DIAMOND 440 GR FINDIKLI CIFT KAPLA.",  # cikolata kaplamali
    "ULKER ROYAL MARAS USULU KAYMAKLI A.FISTIKL",  # dondurma serisi
    "NESTLE MÜSLİ CAPPUCCINO CIKOLATA 260 GR",  # kahvaltilik musli
    "ULKER KELLOGG`S K FLAKES 250 GR CIKOLATA",  # kahvaltilik misir gevregi
}


def urune_etiket_ata(urun_adi: str) -> str:
    if urun_adi in ELLE_ISTISNA:
        return ""
    ad = ascii_kucuk(urun_adi)
    etiketler = []
    for etiket, pozitif, negatif in DESENLER:
        if re.search(pozitif, ad) and not (negatif and re.search(negatif, ad)):
            etiketler.append(etiket)
    return ";".join(etiketler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                         help="CSV'yi yazma, yalnız sayım raporu bas")
    args = parser.parse_args()

    satirlar = list(csv.DictReader(open(CSV_YOLU, encoding="utf-8")))

    degisen = 0
    kategori_sayim: dict[str, int] = {}
    for satir in satirlar:
        if (satir.get("alt_tipler") or "").strip():
            continue  # elle girilmis deger -- ustune yazma
        kategori = satir["csv_kategori"]
        if kategori in DETERMINISTIK_KATEGORI:
            satir["alt_tipler"] = DETERMINISTIK_KATEGORI[kategori]
            degisen += 1
        elif kategori in DESENLI_KATEGORILER:
            etiket = urune_etiket_ata(satir["urun_adi"])
            if etiket:
                satir["alt_tipler"] = etiket
                degisen += 1
        if satir.get("alt_tipler"):
            for e in satir["alt_tipler"].split(";"):
                kategori_sayim[e] = kategori_sayim.get(e, 0) + 1

    print(f"[+] {degisen} satıra alt_tipler atandı (toplam {len(satirlar)} satır).")
    for etiket, n in sorted(kategori_sayim.items(), key=lambda x: -x[1]):
        print(f"    {etiket:<15} {n}")

    if args.dry_run:
        print("[--dry-run] CSV yazılmadı.")
        return

    with open(CSV_YOLU, "w", newline="", encoding="utf-8") as f:
        yazici = csv.DictWriter(f, fieldnames=["urun_adi", "csv_kategori", "siklik", "alt_tipler"])
        yazici.writeheader()
        yazici.writerows(satirlar)
    print(f"[+] {CSV_YOLU} güncellendi.")


if __name__ == "__main__":
    main()
