"""
OCR dosyasi fisN.json, gold'daki hangi fise ait? (kayma / ofset bulma)

Her OCR metni icin: hangi gold fislerinin VKN'si ve tarihi bu metinde geciyor?
  - VKN + tarih ikisi birden uyan gold fis  -> guclu kanit
  - Sadece VKN uyan (ayni satici cok fiste olabilir) -> zayif kanit
Sonra ofset = (eslesen gold fis no) - (OCR dosya no) dagilimi cikarilir.

Kullanim:
  python -m scripts.veri_denetim.kayma_bul --gold "<json3>" --ocr "<ocr>"
"""
import argparse
import glob
import os
import re
from collections import Counter

from app.services.extraction import normalize_sayi
from app.services.vendors import vkn_adaylari
from scripts.ortak import gold_dosyalari, ocr_metni, sadece_rakam, tarih_adaylari


def no(fis):
    m = re.search(r"(\d+)$", fis)
    return int(m.group(1)) if m else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", required=True)
    ap.add_argument("--ocr", required=True)
    a = ap.parse_args()

    gold = dict(gold_dosyalari(a.gold))

    guclu = {}   # ocr fis -> [gold fis] (VKN + tarih uyuyor)
    zayif = {}   # ocr fis -> [gold fis] (sadece VKN)
    for yol in sorted(glob.glob(os.path.join(a.ocr, "*.json"))):
        fis = os.path.splitext(os.path.basename(yol))[0]
        metin = ocr_metni(yol)
        if not metin:
            continue
        r = sadece_rakam(metin)
        adaylar = set(vkn_adaylari(metin)) | {x for x in re.findall(r"\d{10}", r)}
        g_list, z_list = [], []
        for gf, g in gold.items():
            v = normalize_sayi(str(g.get("seller_tax_id") or ""))
            if v not in adaylar:
                continue
            if any(t in r for t in tarih_adaylari(g.get("date"))):
                g_list.append(gf)
            else:
                z_list.append(gf)
        if g_list:
            guclu[fis] = g_list
        elif z_list:
            zayif[fis] = z_list

    # Guclu kanit: ofset dagilimi
    ofsetler = Counter()
    satir = []
    for fis, liste in guclu.items():
        n = no(fis)
        farklar = sorted({no(x) - n for x in liste if no(x) is not None})
        for d in farklar:
            ofsetler[d] += 1
        satir.append((n, fis, farklar))

    print(f"\nOCR dosyasi icinde hangi gold fisine ait oldugu GUCLU kanitla bulunan: {len(guclu)}")
    print("\nOfset dagilimi (gold no - ocr no):  (0 = dosya adi dogru)")
    for d, c in ofsetler.most_common(8):
        print(f"  ofset {d:+d}: {c} fis")

    print("\nFis numarasina gore ofset (guclu kanit):")
    satir.sort()
    for n, fis, farklar in satir:
        print(f"  {fis}: {farklar}")


if __name__ == "__main__":
    main()