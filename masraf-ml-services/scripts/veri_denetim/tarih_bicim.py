"""
Etiketlerdeki (gold) tarih degerlerinin hangi bicimlerde yazildigini sayar.
Rakamlar 9, harfler a ile degistirilir: "29.06.2026" -> "99.99.9999", "29 Haz 2026" -> "99 Aaa 9999".
Her bicim icin sayi, ornek ve tarih_norm'un cozup cozemedigi gosterilir.

Kullanim (proje kokunde):
  python -m scripts.veri_denetim.tarih_bicim --gold "<json3>"
"""
import argparse
import re
from collections import Counter, defaultdict

from scripts.olcum.alan_olc import ALAN_ADAYLARI, deger_al, tarih_norm
from scripts.ortak import gold_dosyalari


def bicim(deger):
    s = re.sub(r"\d", "9", str(deger).strip())
    return re.sub(r"[^\W\d_]", "a", s)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", required=True)
    a = ap.parse_args()

    sayac, ornek, cozulen = Counter(), defaultdict(list), Counter()
    bos = 0
    for _fis, g in gold_dosyalari(a.gold):
        d = deger_al(g, ALAN_ADAYLARI["date"])
        if not d:
            bos += 1
            continue
        b = bicim(d)
        sayac[b] += 1
        if len(ornek[b]) < 3:
            ornek[b].append(str(d))
        cozulen[b] += tarih_norm(d) is not None

    toplam = sum(sayac.values())
    print(f"\nTarihi olan fis: {toplam}   (tarihi bos: {bos})\n")
    print(f"{'Bicim':<26}{'Sayi':>6}{'  tarih_norm cozdu':>20}   Ornekler")
    print("-" * 90)
    for b, c in sayac.most_common():
        print(f"{b:<26}{c:>6}{cozulen[b]:>12}/{c:<6}   {' | '.join(ornek[b])}")
    print("\nOkuma: 'cozdu' sayisi sayidan azsa o bicim tarih_norm'da desteklenmiyor demektir.")


if __name__ == "__main__":
    main()