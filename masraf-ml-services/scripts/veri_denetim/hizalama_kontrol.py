"""
Gold etiketi ile OCR metni AYNI fisi mi anlatiyor? (etiket/dosya eslesmesi kontrolu)

Mantik: bir fisin gold'undaki tarih ve tutar, o fisin OCR metninde de gecmeli.
Gecmiyorsa ya etiket baska fise atanmistir, ya da OCR o fisi okuyamamistir.

Kullanim (proje kok klasorunden, venv aktifken):
  python -m scripts.veri_denetim.hizalama_kontrol --gold "C:\\...\\json3" --ocr "C:\\...\\ocr" [--kayma 2 --kayma-baslangic 144]
"""
import argparse
import os
import re
from collections import Counter, defaultdict

from app.services.extraction import normalize_sayi
from app.services.vendors import vkn_adaylari
from scripts.ortak import (
    gold_dosyalari, kayma_argumanlari_ekle, kayma_cozucu, ocr_metni, sadece_rakam, tarih_adaylari,
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", required=True)
    ap.add_argument("--ocr", required=True)
    kayma_argumanlari_ekle(ap)
    a = ap.parse_args()
    ocr_adi = kayma_cozucu(a)

    gold = dict(gold_dosyalari(a.gold))

    vkn_sahipleri = defaultdict(list)  # gold VKN -> hangi fislerde
    for fis, g in gold.items():
        v = normalize_sayi(str(g.get("seller_tax_id") or ""))
        if re.fullmatch(r"\d{10}", v):
            vkn_sahipleri[v].append(fis)

    gruplar = Counter()
    tarihsiz, capraz = [], []
    olculen = 0

    for fis, g in gold.items():
        metin = ocr_metni(os.path.join(a.ocr, ocr_adi(fis) + ".json"))
        gvkn = normalize_sayi(str(g.get("seller_tax_id") or ""))
        if metin is None or not re.fullmatch(r"\d{10}", gvkn):
            continue
        olculen += 1
        rakamlar = sadece_rakam(metin)

        tarih_var = any(t in rakamlar for t in tarih_adaylari(g.get("date")))
        vkn_var = gvkn in rakamlar or gvkn in vkn_adaylari(metin)

        if tarih_var and vkn_var:
            gruplar["1) Tarih VE VKN metinde var (etiket tutarli)"] += 1
        elif tarih_var and not vkn_var:
            gruplar["2) Tarih var ama VKN metinde yok"] += 1
        elif not tarih_var and vkn_var:
            gruplar["3) VKN var ama tarih metinde yok"] += 1
        else:
            gruplar["4) Ne tarih ne VKN metinde var (etiket baska fise ait olabilir)"] += 1
            tarihsiz.append((fis, g.get("date"), gvkn))

        # VKN metinde yoksa: metindeki numaralar baska fislerin gold VKN'si mi?
        if not vkn_var:
            baskalari = {}
            for aday in vkn_adaylari(metin):
                if aday in vkn_sahipleri:
                    baskalari[aday] = [x for x in vkn_sahipleri[aday] if x != fis]
            if baskalari:
                capraz.append((fis, g.get("date"), tarih_var, baskalari))

    print(f"\nKontrol edilen fis: {olculen}\n")
    for k in sorted(gruplar):
        print(f"  {gruplar[k]:>4}  {k}")

    print(f"\nVKN'si metinde bulunmayan fislerde, metindeki numara BASKA bir fisin gold VKN'si olan: {len(capraz)}")
    print("  (fis | gold tarihi | tarih metinde var mi | metindeki numara -> hangi fislerin gold'u)")
    for fis, tarih, tv, b in capraz[:15]:
        print(f"   {fis} | {tarih} | {'EVET' if tv else 'hayir'} | {b}")

    if tarihsiz:
        print("\n'Ne tarih ne VKN' grubundan ornekler (bu fislerin gorselini acip etiketle karsilastirin):")
        for fis, tarih, vkn in tarihsiz[:10]:
            print(f"   {fis} | gold tarih {tarih} | gold VKN {vkn}")


if __name__ == "__main__":
    main()