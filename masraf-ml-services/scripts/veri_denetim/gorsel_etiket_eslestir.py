"""
Gorsellerin hangi etikete (json) ait oldugunu ICERIGE bakarak bulur (numara kaymasina guvenmez).

Her gorselin OCR metninde, her etiketin VKN / tarih / toplam degerleri aranir; en cok uyan etiket secilir.
Esit puanda, onceki gorsellerdeki numara farkina (offset) en yakin olan secilir.

Girdi:
  --ocr   : ocr_bbox_kaydet.py ciktisi (fisN.json)
  --gold  : etiket klasoru (fisN.json; seller_tax_id, date, total, company)
Cikti: data\\gorsel_etiket_eslesme.csv  (gorsel; etiket; fark; puan; durum)

Puan: VKN bulundu +2, tarih bulundu +1, toplam bulundu +1 (en fazla 4).
Durum: "guvenilir" (puan>=3), "zayif" (puan=2), "belirsiz" (puan<2 veya birden cok etiket esit).

Kullanim (proje kokunde):
  python -m scripts.veri_denetim.gorsel_etiket_eslestir --ocr "<ocr_bbox>" --gold "<json3>"
"""
import argparse
import csv
import glob
import json
import os
import re
import statistics

from scripts.layout.layout_pilot import satir_eslesir, sayi_cevir
from scripts.ortak import veri_yolu


def numara(ad):
    m = re.search(r"(\d+)", ad)
    return int(m.group(1)) if m else -1


def etiketleri_oku(klasor):
    etiketler = {}
    for yol in glob.glob(os.path.join(klasor, "fis*.json")):
        try:
            with open(yol, encoding="utf-8") as f:
                g = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(g, dict):
            continue
        ad = os.path.splitext(os.path.basename(yol))[0]
        vkn = re.sub(r"\D", "", str(g.get("seller_tax_id") or ""))
        toplam = sayi_cevir(str(g.get("total") or "")) if g.get("total") else None
        etiketler[ad] = {"vkn": vkn if len(vkn) >= 10 else "", "date": str(g.get("date") or ""), "total": toplam}
    return etiketler


def puanla(metin_satirlari, e):
    """OCR satirlarinda etiketin degerlerini ara; (puan, ayrinti)."""
    puan, nedenler = 0, []
    if e["vkn"] and any(satir_eslesir("seller_tax_id", e["vkn"], s) for s in metin_satirlari):
        puan += 2
        nedenler.append("vkn")
    if e["date"]:
        try:
            if any(satir_eslesir("date", e["date"], s) for s in metin_satirlari):
                puan += 1
                nedenler.append("tarih")
        except ValueError:
            pass
    if e["total"] is not None and any(satir_eslesir("total_amount", e["total"], s) for s in metin_satirlari):
        puan += 1
        nedenler.append("toplam")
    return puan, "+".join(nedenler)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ocr", required=True)
    ap.add_argument("--gold", required=True)
    ap.add_argument("--cikti", default=None, help="varsayilan: data\\gorsel_etiket_eslesme.csv")
    a = ap.parse_args()
    a.cikti = a.cikti or veri_yolu("gorsel_etiket_eslesme.csv")

    etiketler = etiketleri_oku(a.gold)
    print(f"Etiket: {len(etiketler)}")
    gorseller = sorted(glob.glob(os.path.join(a.ocr, "fis*.json")), key=lambda p: numara(os.path.basename(p)))
    print(f"OCR'li gorsel: {len(gorseller)}")

    satirlar_cikti, son_farklar = [], []
    for yol in gorseller:
        ad = os.path.splitext(os.path.basename(yol))[0]
        with open(yol, encoding="utf-8") as f:
            metin = [o["text"] for o in json.load(f)["ocr"]]
        puanlar = []
        for e_ad, e in etiketler.items():
            p, neden = puanla(metin, e)
            if p >= 2:
                puanlar.append((p, e_ad, neden))
        if not puanlar:
            satirlar_cikti.append({"gorsel": ad, "etiket": "", "fark": "", "puan": 0, "durum": "belirsiz", "neden": ""})
            continue
        en_yuksek = max(p for p, _, _ in puanlar)
        adaylar = [(e_ad, neden) for p, e_ad, neden in puanlar if p == en_yuksek]
        ref = statistics.median(son_farklar[-5:]) if son_farklar else 0
        # esit puanda: numara farki onceki gorsellerdekine en yakin olan
        e_ad, neden = min(adaylar, key=lambda x: abs((numara(x[0]) - numara(ad)) - ref))
        fark = numara(e_ad) - numara(ad)
        durum = "guvenilir" if en_yuksek >= 3 else "zayif"
        if len(adaylar) > 1 and abs(fark - ref) > 3:
            durum = "belirsiz"
        if durum != "belirsiz":
            son_farklar.append(fark)
        satirlar_cikti.append({"gorsel": ad, "etiket": e_ad, "fark": fark, "puan": en_yuksek, "durum": durum, "neden": neden})

    os.makedirs(os.path.dirname(a.cikti) or ".", exist_ok=True)
    with open(a.cikti, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(satirlar_cikti[0].keys()), delimiter=";")
        w.writeheader()
        w.writerows(satirlar_cikti)

    from collections import Counter
    print("\nDurum:", dict(Counter(s["durum"] for s in satirlar_cikti)))
    print("\nNumara farki (etiket_no - gorsel_no) araliklara gore (yalniz 'guvenilir'):")
    araliklar = [(1, 50), (51, 143), (144, 229), (230, 300), (301, 400)]
    for lo, hi in araliklar:
        f = [s["fark"] for s in satirlar_cikti if s["durum"] == "guvenilir" and lo <= numara(s["gorsel"]) <= hi]
        if f:
            print(f"  fis{lo}-fis{hi}: {dict(Counter(f).most_common(4))}  ({len(f)} gorsel)")
    print(f"\nSonuc dosyasi: {a.cikti}")


if __name__ == "__main__":
    main()
