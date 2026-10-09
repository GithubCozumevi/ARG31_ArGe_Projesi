"""
Tum alanlar icin dogruluk olcumu: VLM ciktisi (company, date, total, seller_tax_id) ile gold etiket karsilastirmasi.
OCR-gold kaymasini --kayma / --kayma-baslangic ile duzeltir (vkn_olc.py ile ayni mantik).

Kullanim (proje kokunde, venv acikken):
  python -m scripts.olcum.alan_olc --gold "<json3>" --vlm "<cleaned>" --kayma 2 --kayma-baslangic 144

Cikti: alan basina Dogru / Yanlis / Bos tablosu + data\\alan_olcum.csv
"""
import argparse
import csv
import re
from difflib import SequenceMatcher

from app.services.extraction import normalize_sayi
from app.services.vendors import firma_normalize
from scripts.ortak import gold_dosyalari, kayma_argumanlari_ekle, kayma_cozucu, veri_yolu, vlm_json_oku

ALAN_ADAYLARI = {
    "company": ["company"],
    "date": ["date"],
    "total": ["total", "total_amount", "toplam", "grand_total"],
    "seller_tax_id": ["seller_tax_id"],
}


def deger_al(veri, anahtarlar):
    for k in anahtarlar:
        if k in veri and veri[k] not in (None, "", []):
            return veri[k]
    return None


def tarih_norm(s):
    s = str(s or "")
    m = re.search(r"(\d{1,2})[./\-](\d{1,2})[./\-](\d{2,4})", s)
    if m:
        g, a, y = m.groups()
    else:
        m = re.search(r"(\d{4})[./\-](\d{1,2})[./\-](\d{1,2})", s)
        if not m:
            return None
        y, a, g = m.groups()
    y = y if len(y) == 4 else "20" + y
    return f"{int(g):02d}.{int(a):02d}.{y}"


def tutar_norm(s):
    s = re.sub(r"[^\d.,]", "", str(s or ""))
    if not s:
        return None
    if "," in s and "." in s:
        ondalik = "," if s.rfind(",") > s.rfind(".") else "."
        s = s.replace("." if ondalik == "," else ",", "").replace(ondalik, ".")
    elif "," in s:
        s = s.replace(",", ".") if len(s.split(",")[-1]) <= 2 else s.replace(",", "")
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def karsilastir(alan, gold, tahmin):
    """Donen: 'dogru' | 'yanlis' | 'bos' | None (gold bos, olculmez)"""
    if gold is None:
        return None
    if tahmin is None:
        return "bos"
    if alan == "date":
        g, t = tarih_norm(gold), tarih_norm(tahmin)
        return "dogru" if g and g == t else "yanlis"
    if alan == "total":
        g, t = tutar_norm(gold), tutar_norm(tahmin)
        return "dogru" if g is not None and t is not None and abs(g - t) < 0.01 else "yanlis"
    if alan == "seller_tax_id":
        return "dogru" if normalize_sayi(str(gold)) == normalize_sayi(str(tahmin)) else "yanlis"
    if alan == "company":
        g, t = firma_normalize(gold), firma_normalize(tahmin)
        if g == t:
            return "dogru"
        return "dogru" if SequenceMatcher(None, g, t).ratio() >= 0.85 else "yanlis"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", required=True)
    ap.add_argument("--vlm", required=True)
    kayma_argumanlari_ekle(ap)
    a = ap.parse_args()

    vlm_adi = kayma_cozucu(a)

    sayac = {alan: {"dogru": 0, "yanlis": 0, "bos": 0} for alan in ALAN_ADAYLARI}
    satirlar = []
    bulunan_anahtar = {}
    vlm_yok = 0

    for fis, gold in gold_dosyalari(a.gold):
        vlm = vlm_json_oku(a.vlm, vlm_adi(fis))
        if vlm is None:
            vlm_yok += 1
            continue
        satir = {"fis": fis}
        for alan, anahtarlar in ALAN_ADAYLARI.items():
            g = deger_al(gold, anahtarlar)
            t = deger_al(vlm, anahtarlar)
            if g is not None:
                bulunan_anahtar.setdefault(alan, True)
            sonuc = karsilastir(alan, g, t)
            if sonuc:
                sayac[alan][sonuc] += 1
            satir[f"{alan}_gold"], satir[f"{alan}_vlm"], satir[f"{alan}_sonuc"] = g, t, sonuc
        satirlar.append(satir)

    print(f"\nOlculen fis: {len(satirlar)}   (VLM ciktisi olmayan gold: {vlm_yok})")
    print(f"{'Alan':<16}{'Olculen':>8}{'Dogru':>8}{'Yanlis':>8}{'Bos':>6}   Dogruluk")
    print("-" * 62)
    for alan, c in sayac.items():
        n = sum(c.values())
        if n == 0:
            print(f"{alan:<16}  gold'da bu alan bulunamadi (anahtar adlarini kontrol edin: {ALAN_ADAYLARI[alan]})")
            continue
        print(f"{alan:<16}{n:>8}{c['dogru']:>8}{c['yanlis']:>8}{c['bos']:>6}   %{100 * c['dogru'] / n:.1f}")
    print("\nNot: Bu olcum yalnizca VLM ciktisini olcer (kural/sozluk katmani yok). Firma adi %85 benzerlikle dogru sayilir.")

    if satirlar:
        with open(veri_yolu("alan_olcum.csv"), "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(satirlar[0].keys()), delimiter=";")
            w.writeheader()
            w.writerows(satirlar)
        print("Fis bazinda sonuclar: data\\alan_olcum.csv")


if __name__ == "__main__":
    main()