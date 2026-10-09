"""Iliskisel ciftlerin ESLERINI etiketler, `cift_grup_id` yazar, seti dengeler.

Uc is, tek gecis:
  1. Ciftin iki uyesi de ciftin iliskisel turunu alir (`is_anomali=True`).
     `aciklama_kategorisi`'ne DOKUNULMAZ.
  2. Her etikete `cift_grup_id` yazilir (None YOK: alanin varligi degil,
     CAKISMASI bilgi tasir). Model girdisine girmez.
  3. Anomali orani hedefe cekilir: iliskisel OLMAYAN anomali kayitlari, her
     anomali turunun sayisi birbirine esitlenecek sekilde elenir. Cift uyesi
     asla elenmez (oksuz kayit olusmaz).

    python -m faz_b_aciklama_uretimi.iliskisel_es_etiketle                    # RAPOR
    python -m faz_b_aciklama_uretimi.iliskisel_es_etiketle --uygula
"""

import argparse
import collections
import json
import shutil
from pathlib import Path

from ortak.cift_grup import cift_grup_id, ciftleri_bul

VARSAYILAN_GIRDI_JSON = "data/faturalar_aciklamali.json"
VARSAYILAN_ETIKET_JSON = "data/faturalar_aciklamali_etiketler.json"

ILISKISEL_TURLER = {"mukerrer_fis_yukleme", "fatura_no_cakismasi"}
# `genel_toplam`/`satir_toplami` bunun neredeyse tam alt kumesi (injector yan
# etkisi), sayisi dogal olarak yuksek -> dengelemeye katilmaz.
KONTEYNER_TURLER = {"footer_kismi"}


def esleri_etiketle(girdiler, etiketler) -> int:
    """Ciftin iliskisel turunu iki uyeye de basar. Degisen kayit sayisini doner."""
    degisen = 0
    for uyeler in ciftleri_bul(girdiler).values():
        ortak = {t for k in uyeler for t in etiketler[k]["anomali_turleri"]} & ILISKISEL_TURLER
        if not ortak:
            continue
        for kid in uyeler:
            et = etiketler[kid]
            yeni = set(et["anomali_turleri"]) | ortak
            if yeni != set(et["anomali_turleri"]):
                et["anomali_turleri"] = sorted(yeni)
                et["is_anomali"] = True
                degisen += 1
    return degisen


def elenecekleri_sec(etiketler, korunan: set[str], hedef_adet: int) -> list[str]:
    """Tur sayimlarini esitleyerek eleme adaylari secer.

    Her adimda o anki taban korunur: bir kaydi ancak TUM denge turleri tabanin
    ustunde kalacaksa siler. Tabani koruyan aday kalmayinca taban bir dusurulur.
    Boylece turler birlikte iner. Esitlik bozucu: kategori dagilimini havuzun
    kendi oranlarinda tutmak.
    """
    sayim = collections.Counter(t for x in etiketler.values() for t in x["anomali_turleri"])
    denge = {t for t in sayim if t not in KONTEYNER_TURLER | ILISKISEL_TURLER}
    aday = sorted(k for k, x in etiketler.items() if x["is_anomali"] and k not in korunan)
    if not aday or not denge:
        return []

    kat_havuz = collections.Counter(etiketler[k]["aciklama_kategorisi"] for k in aday)
    kalan, kalan_aday = dict(sayim), set(aday)
    silinen, kat_silinen = [], collections.Counter()
    taban = min(kalan[t] for t in denge)

    def turleri(k):
        return set(etiketler[k]["anomali_turleri"])

    def puan(k):
        kat = etiketler[k]["aciklama_kategorisi"]
        return (sum(kalan[t] for t in turleri(k) & denge),
                kat_havuz[kat] / len(aday) * hedef_adet - kat_silinen[kat], k)

    while len(silinen) < hedef_adet:
        uygun = [k for k in kalan_aday if all(kalan[t] - 1 >= taban for t in turleri(k) & denge)]
        if not uygun:
            taban -= 1
            if taban < 0:
                break
            continue
        k = max(uygun, key=puan)
        for t in turleri(k):
            kalan[t] -= 1
        kalan_aday.discard(k)
        silinen.append(k)
        kat_silinen[etiketler[k]["aciklama_kategorisi"]] += 1
    return silinen


def hedef_adet_hesapla(etiketler, hedef_oran: float) -> int:
    n = len(etiketler)
    anomali = sum(1 for x in etiketler.values() if x["is_anomali"])
    if hedef_oran <= 0 or anomali <= hedef_oran * n:
        return 0
    return round((anomali - hedef_oran * n) / (1 - hedef_oran))


def rapor_uret(onceki_sayim, onceki_kat, etiketler, silinen, korunan) -> dict:
    kalanlar = [k for k in etiketler if k not in set(silinen)]
    sayim = collections.Counter(t for k in kalanlar for t in etiketler[k]["anomali_turleri"])
    denge = {t for t in sayim if t not in KONTEYNER_TURLER | ILISKISEL_TURLER}
    d = [sayim[t] for t in denge]
    kat = collections.Counter(etiketler[k]["aciklama_kategorisi"] for k in kalanlar)
    anomali = sum(1 for k in kalanlar if etiketler[k]["is_anomali"])
    return {
        "kayit": len(kalanlar),
        "silinen": len(silinen),
        "anomali_orani": round(anomali / len(kalanlar), 4),
        "turler": {t: {"once": onceki_sayim[t], "sonra": sayim[t]}
                   for t, _ in sayim.most_common()},
        "denge_yayilimi": {
            "once": max(onceki_sayim[t] for t in denge) - min(onceki_sayim[t] for t in denge),
            "sonra": max(d) - min(d),
        },
        "kategori": {k: {"once": round(onceki_kat[k] / sum(onceki_kat.values()), 4),
                         "sonra": round(v / len(kalanlar), 4)} for k, v in kat.most_common()},
        "denetim": {
            "silinen_cift_uyesi": len(set(silinen) & korunan),
            "cift_grup_id_eksik": sum(1 for k in kalanlar if not etiketler[k].get("cift_grup_id")),
        },
    }


def raporu_yazdir(rapor: dict) -> None:
    print(f"\n[+] {rapor['kayit']} kayit kaldi ({rapor['silinen']} elendi), "
          f"anomali orani {rapor['anomali_orani']}")

    print("\n--- anomali turleri (once -> sonra) ---")
    for t, v in rapor["turler"].items():
        et = "  konteyner" if t in KONTEYNER_TURLER else "  iliskisel" if t in ILISKISEL_TURLER else ""
        print(f"    {t:34s} {v['once']:5d} -> {v['sonra']:5d}{et}")
    y = rapor["denge_yayilimi"]
    print(f"    denge turleri yayilimi: {y['once']} -> {y['sonra']}")

    print("\n--- aciklama_kategorisi ---")
    for k, v in rapor["kategori"].items():
        print(f"    {k:14s} %{100 * v['once']:.1f} -> %{100 * v['sonra']:.1f}")

    print("\n--- denetim ---")
    tamam = True
    for ad, n in rapor["denetim"].items():
        tamam &= n == 0
        print(f"    {'OK ' if n == 0 else 'HATA'} {ad}: {n}")
    print(f"\n[{'+' if tamam else '!'}] Denetim: {'TAMAM' if tamam else 'HATA'}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Iliskisel esleri etiketle, cift_grup_id yaz, seti dengele")
    ap.add_argument("--girdi-json", default=VARSAYILAN_GIRDI_JSON)
    ap.add_argument("--etiket-json", default=VARSAYILAN_ETIKET_JSON)
    ap.add_argument("--hedef-oran", type=float, default=0.28)
    ap.add_argument("--eleme-yapma", action="store_true")
    ap.add_argument("--uygula", action="store_true", help="Yedek alip yazar (varsayilan: rapor)")
    args = ap.parse_args()

    with open(args.girdi_json, "r", encoding="utf-8") as f:
        girdiler = json.load(f)
    with open(args.etiket_json, "r", encoding="utf-8") as f:
        etiket_listesi = json.load(f)
    etiketler = {x["kayit_id"]: x for x in etiket_listesi}

    if {r["kayit_id"] for r in girdiler} != set(etiketler):
        print("HATA: girdi ve etiket kayit_id kumeleri AYNI DEGIL.")
        return

    onceki_sayim = collections.Counter(t for x in etiketler.values() for t in x["anomali_turleri"])
    onceki_kat = collections.Counter(x["aciklama_kategorisi"] for x in etiketler.values())
    print(f"[+] {len(girdiler)} kayit okundu.")

    degisen = esleri_etiketle(girdiler, etiketler)
    print(f"[+] {degisen} ese iliskisel etiket basildi.")

    girdi_map = {r["kayit_id"]: r for r in girdiler}
    for kid, et in etiketler.items():
        et["cift_grup_id"] = cift_grup_id(girdi_map[kid])

    korunan = {k for u in ciftleri_bul(girdiler).values() for k in u}
    hedef = 0 if args.eleme_yapma else hedef_adet_hesapla(etiketler, args.hedef_oran)
    silinen = elenecekleri_sec(etiketler, korunan, hedef)
    if hedef and len(silinen) < hedef:
        print(f"[!] Hedef {hedef} kayit, secilebilen {len(silinen)}.")

    rapor = rapor_uret(onceki_sayim, onceki_kat, etiketler, silinen, korunan)
    raporu_yazdir(rapor)

    if any(rapor["denetim"].values()):
        print("[!] Denetim basarisiz, dosya YAZILMADI.")
        return
    if not args.uygula:
        print("\n[i] RAPOR modu, dosya yazilmadi. Yazmak icin: --uygula")
        return

    elenen = set(silinen)
    print()
    for yol, veri in ((args.girdi_json, [r for r in girdiler if r["kayit_id"] not in elenen]),
                      (args.etiket_json, [etiketler[x["kayit_id"]] for x in etiket_listesi
                                          if x["kayit_id"] not in elenen])):
        yedek = Path(yol).with_suffix(".json.yedek")
        shutil.copy2(yol, yedek)
        with open(yol, "w", encoding="utf-8") as f:
            json.dump(veri, f, ensure_ascii=False, indent=2)
        print(f"[+] {yol} guncellendi ({len(veri)} kayit, yedek: {yedek.name})")


if __name__ == "__main__":
    main()
