"""
Cift gerektiren anomalilerde (`mukerrer_fis_yukleme`, `fatura_no_cakismasi`)
esi veri setinde OLAN ve OKSUZ kalan kayitlari sayar.

Bu iki etiket ILISKISELDIR: bir fisin mukerrer oldugu kendi alanlarindan degil,
baska bir kaydin varliginda anlasilir. Esi sette yoksa kayit, karsilastirmali
bir model icin cozulemez.

Es aramasi TUM veri setinde yapilir: ciftin diger uyesi genelde `(temiz)`
etiketlidir, yalniz anomalili kayitlar arasinda arayinca hicbir cift bulunamaz.
Anahtar `(satici_vkn, fatura_no)` -- yalniz `fatura_no` dogal cakismalari da
cift sayardi (farkli saticilar ayni numarayi kullanabilir).

    python -m faz_c_fis_gorsellestirme.mukerrer
"""

import argparse
import json
from ortak.cift_grup import cift_grup_anahtari, kayit_imzasi
import shutil
from collections import Counter, defaultdict

VARSAYILAN_GIRDI_JSON = "data/faturalar_aciklamali.json"
VARSAYILAN_ETIKET_JSON = "data/faturalar_aciklamali_etiketler.json"
HEDEF_ANOMALILER = ("mukerrer_fis_yukleme", "fatura_no_cakismasi")

imza = kayit_imzasi


def main() -> None:
    ap = argparse.ArgumentParser(description="Cift gerektiren anomalilerde es/oksuz sayimi")
    ap.add_argument("--girdi-json", default=VARSAYILAN_GIRDI_JSON)
    ap.add_argument("--etiket-json", default=VARSAYILAN_ETIKET_JSON)
    ap.add_argument("--ornek", type=int, default=3, help="Gosterilecek ornek cift sayisi")
    ap.add_argument("--oksuzleri-sil", action="store_true",
                    help="Esi olmayan iliskisel kayitlari HER IKI dosyadan da sil (yedek alinir)")
    args = ap.parse_args()

    with open(args.girdi_json, "r", encoding="utf-8") as f:
        girdiler = json.load(f)
    with open(args.etiket_json, "r", encoding="utf-8") as f:
        etiketler = json.load(f)

    gr = {r["kayit_id"]: r for r in girdiler}
    et = {r["kayit_id"]: r for r in etiketler}

    # Iki dosya ayni kumeyi tasimazsa es aramasi KeyError verir. Tipik sebep:
    # aciklama_birlestir kosuldu ama onay_durumu_ata kosulmadi.
    eksik = set(gr) - set(et)
    if eksik:
        print(f"HATA: {len(eksik)} kaydin etiketi yok (girdi {len(gr)}, etiket {len(et)}).\n"
              f"     `python -m faz_b_aciklama_uretimi.onay_durumu_ata --cikti-dizini <dizin>` kosulmali.")
        return

    anahtar_kume: dict[tuple, list[str]] = defaultdict(list)
    imza_kume: dict[str, list[str]] = defaultdict(list)
    for r in girdiler:
        anahtar_kume[cift_grup_anahtari(r)].append(r["kayit_id"])
        imza_kume[imza(r)].append(r["kayit_id"])

    print(f"[+] {len(girdiler)} kayit okundu ({args.girdi_json})\n")
    print(f"{'anomali':24s} {'toplam':>7s} {'esi sette':>10s} {'OKSUZ':>7s} {'birebir kopya':>14s}")

    ayrinti = {}
    for anomali in HEDEF_ANOMALILER:
        kayitlar = [k for k, v in et.items() if anomali in v["anomali_turleri"]]
        esli, kopya = [], []
        for kid in kayitlar:
            r = gr[kid]
            if len(anahtar_kume[cift_grup_anahtari(r)]) > 1:
                esli.append(kid)
            if len(imza_kume[imza(r)]) > 1:
                kopya.append(kid)
        ayrinti[anomali] = (kayitlar, esli, kopya)
        print(f"{anomali:24s} {len(kayitlar):7d} {len(esli):10d} "
              f"{len(kayitlar) - len(esli):7d} {len(kopya):14d}")

    print("\n--- esi sette olanlarda ESIN etiketi ---")
    for anomali, (_, esli, _) in ayrinti.items():
        sayac = Counter()
        for kid in esli:
            r = gr[kid]
            for es in anahtar_kume[cift_grup_anahtari(r)]:
                if es != kid:
                    sayac["+".join(sorted(et[es]["anomali_turleri"])) or "(temiz)"] += 1
        print(f"  {anomali}:")
        for k, n in sayac.most_common(5):
            print(f"      {n:4d}x  {k}")
        if not sayac:
            print("      (esi olan kayit yok)")

    if args.oksuzleri_sil:
        oksuzler: set[str] = set()
        for kayitlar, esli, _ in ayrinti.values():
            oksuzler |= set(kayitlar) - set(esli)
        if not oksuzler:
            print("\n[+] Oksuz kayit yok, silinecek bir sey yok.")
            return
        # Silinen kayit BASKA bir iliskisel kaydin esi olmamali; grup boyu 2'den
        # buyukse (uclu) bu mumkun olur ve silme yeni oksuz dogurur.
        tehlike = [k for k in oksuzler
                   if any(x != k and x not in oksuzler
                          and set(et[x]["anomali_turleri"]) & set(HEDEF_ANOMALILER)
                          for x in anahtar_kume[(gr[k]["satici_vkn"], gr[k]["fatura_no"])])]
        if tehlike:
            print(f"\n[!] DURDURULDU: {len(tehlike)} kayit baska bir iliskisel kaydin esi, "
                  f"silmek yeni oksuz dogurur.")
            return
        oran = len(oksuzler) / len(gr)
        if oran > 0.05:
            print(f"\n[!] DURDURULDU: veri setinin %{100*oran:.1f}'i silinecekti (esik %5). "
                  f"Dosyalar dogru mu, kontrol et.")
            return

        print(f"\n[+] {len(oksuzler)} oksuz kayit siliniyor (yedek aliniyor)...")
        for yol, kayitlar in ((args.girdi_json, girdiler), (args.etiket_json, etiketler)):
            shutil.copy2(yol, yol + ".yedek_oksuz")
            kalan = [r for r in kayitlar if r["kayit_id"] not in oksuzler]
            with open(yol, "w", encoding="utf-8") as f:
                json.dump(kalan, f, ensure_ascii=False, indent=2)
            print(f"      {yol}: {len(kayitlar)} -> {len(kalan)}")
        return

    if args.ornek:
        print(f"\n--- ornek ciftler ---")
        for anomali, (_, esli, _) in ayrinti.items():
            print(f"  {anomali}:")
            for kid in esli[:args.ornek]:
                r = gr[kid]
                grup = anahtar_kume[cift_grup_anahtari(r)]
                for k in grup:
                    e = et[k]
                    print(f"      {k}  no={gr[k]['fatura_no']}  tutar={gr[k]['genel_toplam']}  "
                          f"turler={e['anomali_turleri'] or '[]'}  onay={e['onay_durumu']}")
                print()


if __name__ == "__main__":
    main()
