"""
Firma adi dogrulugu: yalniz VLM'in firma adi  vs  VKN -> satici sozlugu ile firma adi (hibrit).

Hibrit mantik (her fis icin):
  1. VKN/TCKN pipeline ile bulunur (kural + VLM + sozluk).
  2. Bulunan VKN sozlukte varsa firma adi sozlukten alinir.
  3. Yoksa VLM'in firma adi kullanilir.
Sozluk fis basina leave-one-out kurulur: fisin kendi etiketi sozlukte bulunmaz (sizinti yok).

Kullanim (proje kokunde):
  python -m scripts.olcum.firma_olc --gold "<json3>" --ocr "<ocr>" --vlm "<cleaned>" --kayma 2 --kayma-baslangic 144
"""
import argparse
import csv
import os
from difflib import SequenceMatcher

from app.services.extraction import is_valid_tckn_checksum, is_valid_vkn_checksum, normalize_sayi
from app.services.pipeline import vkn_karar_ver
from app.services.vendors import firma_normalize, sozluk_kur
from scripts.ortak import (
    anahtar_kelimeler, gold_dosyalari, kayma_argumanlari_ekle, kayma_cozucu, ocr_metni, veri_yolu, vlm_oku,
)

ESIK = 0.85  # normalize edilmis firma adlari bu benzerlikten fazlaysa ayni sayilir


def ayni_mi(a, b):
    a, b = firma_normalize(a), firma_normalize(b)
    if not a or not b:
        return False
    return a == b or SequenceMatcher(None, a, b).ratio() >= ESIK


def esnek_ayni_mi(a, b):
    """Esnek olcu: hukuki ekler (A.S., LTD. STI. ...) atilir; ayni ya da biri digerinin ana kelimelerini
    iceriyorsa (en az 2 kelime ve uzun adin yarisi) ayni firma sayilir. Tek kelimelik kismi eslesme kabul edilmez."""
    if ayni_mi(a, b):
        return True
    ka, kb = anahtar_kelimeler(a), anahtar_kelimeler(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    kisa, uzun = (ka, kb) if len(ka) <= len(kb) else (kb, ka)
    return kisa <= uzun and len(kisa) >= 2 and len(kisa) / len(uzun) >= 0.5


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", required=True)
    ap.add_argument("--ocr", required=True)
    ap.add_argument("--vlm", required=True)
    kayma_argumanlari_ekle(ap)
    a = ap.parse_args()

    ocr_adi = kayma_cozucu(a)

    gold = []   # (fis, vkn, firma)
    for fis, g in gold_dosyalari(a.gold):
        firma = str(g.get("company") or "").strip()
        vkn = normalize_sayi(str(g.get("seller_tax_id") or ""))
        gecerli = (len(vkn) == 10 and is_valid_vkn_checksum(vkn)) or (len(vkn) == 11 and is_valid_tckn_checksum(vkn))
        gold.append((fis, vkn if gecerli else "", firma))

    tum_ciftler = [(v, f) for _, v, f in gold if v and f]

    n = vlm_dogru = hibrit_dogru = hibrit_esnek = vlm_esnek = 0
    sozluk_kullanilan = sozluk_dogru = vlm_ayni_yerde_dogru = 0
    satirlar, hatalar = [], []

    for fis, gvkn, gfirma in gold:
        if not gfirma:
            continue
        metin = ocr_metni(os.path.join(a.ocr, ocr_adi(fis) + ".json"))
        if metin is None:
            continue
        vlm_vkn, vlm_firma = vlm_oku(a.vlm, ocr_adi(fis))

        ciftler = list(tum_ciftler)
        if (gvkn, gfirma) in ciftler:
            ciftler.remove((gvkn, gfirma))
        sozluk = sozluk_kur(ciftler)

        c = vkn_karar_ver(metin, vlm_vkn, sozluk=sozluk, firma_ipucu=vlm_firma)
        vkn = c["vkn"]

        n += 1
        vlm_ok = ayni_mi(gfirma, vlm_firma)
        vlm_dogru += vlm_ok

        if vkn and vkn in sozluk:
            tahmin, kaynak = sozluk[vkn]["firma"], "sozluk"
            sozluk_kullanilan += 1
            ok = any(ayni_mi(gfirma, ad) for ad in sozluk[vkn]["takma_adlar"])
            esnek_ok = any(esnek_ayni_mi(gfirma, ad) for ad in sozluk[vkn]["takma_adlar"])
            sozluk_dogru += ok
            vlm_ayni_yerde_dogru += vlm_ok
            if not ok:
                hatalar.append((fis, gfirma, tahmin, vkn, gvkn))
        else:
            tahmin, kaynak = vlm_firma or "", "vlm"
            ok = vlm_ok
            esnek_ok = esnek_ayni_mi(gfirma, vlm_firma)
        hibrit_dogru += ok
        hibrit_esnek += esnek_ok
        vlm_esnek += esnek_ayni_mi(gfirma, vlm_firma)
        satirlar.append({"fis": fis, "gold_firma": gfirma, "vlm_firma": vlm_firma, "hibrit_firma": tahmin,
                         "kaynak": kaynak, "dogru": int(ok), "dogru_esnek": int(esnek_ok),
                         "vkn": vkn, "gold_vkn": gvkn})

    if n == 0:
        print("Olculecek fis bulunamadi.")
        return
    print(f"\nOlculen fis: {n}\n")
    print(f"  Yalniz VLM firma adi          : {vlm_dogru:>4} / {n}  = %{100 * vlm_dogru / n:.1f}")
    print(f"  Hibrit (VKN -> sozluk -> firma): {hibrit_dogru:>4} / {n}  = %{100 * hibrit_dogru / n:.1f}")
    print("\n  ESNEK OLCU (A.S./LTD. STI. gibi ekler atilir, kisa ad uzun adin icindeyse ayni sayilir):")
    print(f"  Yalniz VLM (esnek)            : {vlm_esnek:>4} / {n}  = %{100 * vlm_esnek / n:.1f}")
    print(f"  Hibrit (esnek)                : {hibrit_esnek:>4} / {n}  = %{100 * hibrit_esnek / n:.1f}")
    print(f"\n  Sozlukten firma adi alinan fis: {sozluk_kullanilan}  (%{100 * sozluk_kullanilan / n:.0f})")
    if sozluk_kullanilan:
        print(f"    Bunlarda sozluk dogrulugu   : %{100 * sozluk_dogru / sozluk_kullanilan:.1f}")
        print(f"    Ayni fislerde VLM dogrulugu : %{100 * vlm_ayni_yerde_dogru / sozluk_kullanilan:.1f}")
    print(f"  Sozlukte olmayan (VLM kullanilan): {n - sozluk_kullanilan}")

    if hatalar:
        print(f"\nSozlukten gelip yanlis olan {len(hatalar)} fis (fis | gold firma | sozluk firma | bulunan VKN | gold VKN):")
        for h in hatalar[:12]:
            print("  ", " | ".join(str(x) for x in h))

    with open(veri_yolu("firma_olcum.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(satirlar[0].keys()), delimiter=";")
        w.writeheader()
        w.writerows(satirlar)
    print("\nFis bazinda sonuclar: data\\firma_olcum.csv")


if __name__ == "__main__":
    main()