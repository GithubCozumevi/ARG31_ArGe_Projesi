"""
Gercek fislerde alan konumu (bbox ile): firma, VKN, tarih, toplam fisin neresinde (yukaridan asagi %)?

Girdi:
  --gold  : etiket klasoru (fisN.json; company, seller_tax_id, date, total)
  --ocr   : ocr_bbox_kaydet.py ciktisi (fisN.json; "boyut" ve "ocr" icinde "bbox")
Eslestirme mantigi layout_pilot.py ile aynidir (ayni klasorde olmali).

Kullanim:
  python -m scripts.layout.konum_gercek --gold "<json3>" --ocr "<ocr_bbox>" --gruplar "1-51,52-156,157-229,230-400"
"""
import argparse
import glob
import json
import os
import re
import statistics

from scripts.layout.layout_pilot import firma_eslesmesi, satir_eslesir, sayi_cevir, tutar_sec

ALANLAR = [("company", "firma"), ("seller_tax_id", "VKN"), ("date", "tarih"), ("total", "toplam")]


def ocr_kutulari(yol):
    with open(yol, encoding="utf-8") as f:
        d = json.load(f)
    satirlar = []
    for o in d["ocr"]:
        xs = [p[0] for p in o["bbox"]]
        ys = [p[1] for p in o["bbox"]]
        satirlar.append({"text": o["text"], "score": o.get("confidence", 1.0),
                         "box": [min(xs), min(ys), max(xs), max(ys)]})
    satirlar.sort(key=lambda s: (s["box"][1], s["box"][0]))
    return satirlar, d["boyut"]


def esle(alan, gold, satirlar):
    """Dondurur: eslesen satir indeksleri (bos liste = bulunamadi)."""
    if alan == "company":
        return firma_eslesmesi(gold, satirlar)
    if alan == "seller_tax_id":
        idx = [i for i, s in enumerate(satirlar) if satir_eslesir("seller_tax_id", gold, s["text"])]
        return idx[:1]
    if alan == "date":
        try:
            idx = [i for i, s in enumerate(satirlar) if satir_eslesir("date", gold, s["text"])]
        except ValueError:
            return []
        return idx[:1]
    if alan == "total":
        v = sayi_cevir(str(gold))
        if v is None:
            return []
        idx = [i for i, s in enumerate(satirlar) if satir_eslesir("total_amount", v, s["text"])]
        return tutar_sec("total_amount", idx, satirlar) if idx else []
    return []


def grup_ayir(arg):
    gruplar = []
    for parca in (arg or "0-99999").split(","):
        a, b = parca.split("-")
        gruplar.append((int(a), int(b)))
    return gruplar


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", required=True)
    ap.add_argument("--ocr", required=True)
    ap.add_argument("--gruplar", default="", help="fis numarasi araliklari, ornek: 1-51,52-156")
    a = ap.parse_args()
    gruplar = grup_ayir(a.gruplar)

    veri = {g: {al: [] for al, _ in ALANLAR} for g in gruplar}   # g -> alan -> [y_oran veya None]
    n_fis = {g: 0 for g in gruplar}

    for yol in sorted(glob.glob(os.path.join(a.gold, "*.json"))):
        ad = os.path.splitext(os.path.basename(yol))[0]
        m = re.fullmatch(r"fis(\d+)", ad)
        ocr_yol = os.path.join(a.ocr, ad + ".json")
        if not m or not os.path.exists(ocr_yol):
            continue
        numara = int(m.group(1))
        grup = next((g for g in gruplar if g[0] <= numara <= g[1]), None)
        if grup is None:
            continue
        try:
            with open(yol, encoding="utf-8") as f:
                gold = json.load(f)
            satirlar, boyut = ocr_kutulari(ocr_yol)
        except (OSError, ValueError, KeyError):
            continue
        n_fis[grup] += 1
        for alan, _ in ALANLAR:
            deger = str(gold.get(alan) or "").strip()
            if not deger:
                continue
            idx = esle(alan, deger, satirlar)
            if idx:
                # Fotograflarda fis gorselin tamami degil: konumu yazili alana (ilk satirin ustu - son satirin alti) gore olc
                ust = min(s["box"][1] for s in satirlar)
                alt = max(s["box"][3] for s in satirlar)
                yc = statistics.mean((satirlar[i]["box"][1] + satirlar[i]["box"][3]) / 2 for i in idx)
                veri[grup][alan].append((yc - ust) / max(alt - ust, 1.0))
            else:
                veri[grup][alan].append(None)

    for g in gruplar:
        if n_fis[g] == 0:
            continue
        print(f"\n=== fis{g[0]}-fis{g[1]}  ({n_fis[g]} fis) ===")
        print(f"{'Alan':<8}{'Etiketli':>9}{'Bulunan':>9}{'Oran':>7}   {'Medyan':>7}{'0-25%':>7}{'25-50%':>8}{'50-75%':>8}{'75-100%':>9}")
        for alan, ad in ALANLAR:
            v = veri[g][alan]
            bulunan = [y for y in v if y is not None]
            if not v:
                continue
            if bulunan:
                k = [sum(lo <= y < hi for y in bulunan) for lo, hi in ((0, .25), (.25, .5), (.5, .75), (.75, 1.01))]
                yuzde = [f"{100 * c / len(bulunan):.0f}%" for c in k]
                print(f"{ad:<8}{len(v):>9}{len(bulunan):>9}{100 * len(bulunan) / len(v):>6.0f}%   {100 * statistics.median(bulunan):>6.0f}%"
                      f"{yuzde[0]:>7}{yuzde[1]:>8}{yuzde[2]:>8}{yuzde[3]:>9}")
            else:
                print(f"{ad:<8}{len(v):>9}{0:>9}{0:>6}%")
    print("\nOkuma: bir alan hep ayni dilimdeyse (ornegin toplam %90 alt dilimde) koordinat kurali yeter; dagiliksa model gerekir.")


if __name__ == "__main__":
    main()
