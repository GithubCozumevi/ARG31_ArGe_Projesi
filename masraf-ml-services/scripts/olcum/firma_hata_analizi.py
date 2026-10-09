"""
Firma adinda yanlis cikan fislerin nedenini siniflandirir.

Girdi: data\\firma_olcum.csv  (once firma_olc.py calistirilmis olmali)
Siniflar:
  - kisa/uzun ad farki : biri otekinin icinde geciyor (ornek: "KOVA TAVUK" / "KOVA TAVUK GIDA SAN. LTD. STI.")
  - ortak kelime cok   : anahtar kelimelerin yarisindan fazlasi ortak (sube eki, yazim farki)
  - ortak kelime az    : bir iki ortak kelime var (marka / unvan farki olabilir)
  - tamamen farkli     : ortak kelime yok (gercek tanima hatasi ya da baska isim)
  - bos                : model firma adi vermemis

Kullanim (proje kokunde): python -m scripts.olcum.firma_hata_analizi
"""
import csv
import os
from collections import Counter

from app.services.vendors import firma_normalize
from scripts.ortak import anahtar_kelimeler, veri_yolu

YOL = veri_yolu("firma_olcum.csv")


def sinif(gold, tahmin):
    g, t = firma_normalize(gold), firma_normalize(tahmin)
    if not t:
        return "bos"
    if g in t or t in g:
        return "kisa/uzun ad farki"
    kg, kt = anahtar_kelimeler(gold), anahtar_kelimeler(tahmin)
    ortak = kg & kt
    if ortak:
        return "ortak kelime cok" if len(ortak) / max(len(kg | kt), 1) >= 0.5 else "ortak kelime az"
    return "tamamen farkli"


def yazdir(baslik, satirlar):
    yanlislar = [s for s in satirlar if s["dogru"] == "0"]
    print(f"\n=== {baslik}: {len(yanlislar)} yanlis / {len(satirlar)} fis ===")
    if not yanlislar:
        return
    sayac, ornek = Counter(), {}
    for s in yanlislar:
        k = sinif(s["gold_firma"], s["hibrit_firma"])
        sayac[k] += 1
        ornek.setdefault(k, []).append((s["fis"], s["gold_firma"], s["hibrit_firma"]))
    for k, c in sayac.most_common():
        print(f"  {c:>4}  (%{100 * c / len(yanlislar):.0f})  {k}")
        for fis, g, t in ornek[k][:4]:
            print(f"        {fis} | gold: {g} | tahmin: {t}")


def main():
    if not os.path.exists(YOL):
        print(f"{YOL} yok. Once firma_olc.py'yi calistirin.")
        return
    with open(YOL, encoding="utf-8-sig", newline="") as f:
        satirlar = list(csv.DictReader(f, delimiter=";"))

    yazdir("TUM FISLER (hibrit sonuc)", satirlar)
    yazdir("Sozlukten gelenler", [s for s in satirlar if s["kaynak"] == "sozluk"])
    yazdir("VLM'den gelenler (sozlukte olmayan satici)", [s for s in satirlar if s["kaynak"] == "vlm"])
    print("\nOkuma: 'kisa/uzun ad farki' ve 'ortak kelime' agirlikliysa sorun tanima degil, ad standardi (etiket kurali) sorunudur.")


if __name__ == "__main__":
    main()