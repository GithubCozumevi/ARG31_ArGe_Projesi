"""
Gold etiketlerinde supheli VKN'leri listeler (duzeltme listesi).

Kullanim:
  python -m scripts.veri_denetim.etiket_denetim --gold "<json3>"

Cikti: data\\etiket_denetim.csv  (fis; sorun; deger; firma)
Sorunlar: vkn_bos | vkn_11_hane (TCKN olabilir) | vkn_checksum_gecersiz | vkn_baska_uzunluk
"""
import argparse
import csv
import glob
import os
import re

from app.services.extraction import is_valid_tckn_checksum, is_valid_vkn_checksum, normalize_sayi
from scripts.ortak import json_oku, veri_yolu


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", required=True)
    a = ap.parse_args()

    satirlar, toplam = [], 0
    for yol in sorted(glob.glob(os.path.join(a.gold, "*.json"))):
        fis = os.path.splitext(os.path.basename(yol))[0]
        g = json_oku(yol)
        if not isinstance(g, dict):
            satirlar.append((fis, "bozuk_json", "", ""))
            continue
        toplam += 1
        ham = str(g.get("seller_tax_id") or "")
        vkn = normalize_sayi(ham)
        firma = str(g.get("company") or "")
        if not vkn:
            satirlar.append((fis, "vkn_bos", ham, firma))
        elif re.fullmatch(r"\d{11}", vkn):
            sorun = "vkn_11_hane_TCKN_gecerli" if is_valid_tckn_checksum(vkn) else "vkn_11_hane_TCKN_checksum_gecersiz"
            satirlar.append((fis, sorun, vkn, firma))
        elif re.fullmatch(r"\d{10}", vkn):
            if not is_valid_vkn_checksum(vkn):
                satirlar.append((fis, "vkn_checksum_gecersiz", vkn, firma))
        else:
            satirlar.append((fis, "vkn_baska_uzunluk", ham, firma))

    with open(veri_yolu("etiket_denetim.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["fis", "sorun", "deger", "firma"])
        w.writerows(satirlar)

    sayim = {}
    for _, s, _, _ in satirlar:
        sayim[s] = sayim.get(s, 0) + 1
    print(f"\nTaranan etiket: {toplam}   Supheli: {len(satirlar)}")
    for s, n in sorted(sayim.items(), key=lambda x: -x[1]):
        print(f"  {n:>4}  {s}")
    print("\nIlk 15 supheli:")
    for fis, s, deger, firma in satirlar[:15]:
        print(f"  {fis} | {s} | {deger} | {firma}")
    print("\nTum liste: data\\etiket_denetim.csv  (Excel'de acip gorselle karsilastirarak duzeltin)")


if __name__ == "__main__":
    main()