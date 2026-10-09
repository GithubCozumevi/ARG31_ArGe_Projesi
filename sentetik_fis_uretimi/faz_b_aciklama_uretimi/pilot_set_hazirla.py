"""
Pilot açıklama koşusu için KÜÇÜK ve DENGELİ bir alt küme çıkarır (varsayılan 32
fatura: kategori başına 8). Rastgele 30 fatura almak yerine dengeli seçmenin sebebi:
manipulatif popülasyonun yalnız ~%7'si, rastgele bir dilimde 1-2 tane çıkar ve
prompt yollarının çoğu HİÇ test edilmez.

Manipulatif tarafta DÖRT DALIN da temsil edilmesi ayrıca garanti edilir
(gizleme / zorunluluk / kurnaz-zorunlu / bariz|kurnaz) -- dal seçimi
anomali-farkındalı olduğu için (aciklama_uretim_core.prompt_olustur) her dalın
prompt'u ayrı bir kod yolu.

Kullanım:
    python -m faz_b_aciklama_uretimi.pilot_set_hazirla --per-kategori 8
"""

import argparse
import json
import random
from pathlib import Path


def manipulatif_dali(turler: list[str]) -> str:
    """prompt_olustur'daki dal seçimini yansıtır (kalem bilgisi olmadan, etiketten)."""
    if "yasakli_kategori" in turler or "is_kolu_kategori_uyumsuzlugu" in turler:
        return "gizleme"
    if "limit_asimi" in turler:
        return "zorunluluk"
    if "mukerrer_fis_yukleme" in turler:
        return "kurnaz"
    return "bariz|kurnaz"


def main():
    ap = argparse.ArgumentParser(description="Pilot için dengeli küçük alt küme çıkar")
    ap.add_argument("--input-json", default="data/faturalar.json")
    ap.add_argument("--etiket-json", default="data/faturalar_etiketler.json")
    ap.add_argument("--output-json", default="data/pilot_test.json")
    ap.add_argument("--output-etiket", default="data/pilot_test_etiketler.json")
    ap.add_argument("--per-kategori", type=int, default=8)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    rnd = random.Random(args.seed)
    faturalar = json.load(open(args.input_json, encoding="utf-8"))
    etiketler = json.load(open(args.etiket_json, encoding="utf-8"))
    et = {e["kayit_id"]: e for e in etiketler}

    havuz: dict[str, list[dict]] = {}
    for f in faturalar:
        e = et.get(f["kayit_id"])
        if e:
            havuz.setdefault(e["aciklama_kategorisi"], []).append(f)

    secilen: list[dict] = []
    for kategori, liste in havuz.items():
        rnd.shuffle(liste)
        if kategori == "manipulatif":
            # Dört dalı da temsil et: önce her daldan eşit pay, sonra kalanı doldur.
            dallar: dict[str, list[dict]] = {}
            for f in liste:
                dallar.setdefault(manipulatif_dali(et[f["kayit_id"]]["anomali_turleri"]), []).append(f)
            per_dal = max(1, args.per_kategori // max(len(dallar), 1))
            for dal_listesi in dallar.values():
                secilen += dal_listesi[:per_dal]
            kalan = args.per_kategori - sum(1 for f in secilen
                                            if et[f["kayit_id"]]["aciklama_kategorisi"] == "manipulatif")
            if kalan > 0:
                alinmis = {f["kayit_id"] for f in secilen}
                secilen += [f for f in liste if f["kayit_id"] not in alinmis][:kalan]
        else:
            secilen += liste[: args.per_kategori]

    rnd.shuffle(secilen)
    alinan = {f["kayit_id"] for f in secilen}
    secilen_etiket = [e for e in etiketler if e["kayit_id"] in alinan]

    Path(args.output_json).write_text(json.dumps(secilen, ensure_ascii=False, indent=2), encoding="utf-8")
    Path(args.output_etiket).write_text(json.dumps(secilen_etiket, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[+] {len(secilen)} fatura -> {args.output_json}")
    print(f"[+] {len(secilen_etiket)} etiket -> {args.output_etiket}")
    from collections import Counter
    kat = Counter(et[f["kayit_id"]]["aciklama_kategorisi"] for f in secilen)
    print("    kategori:", dict(kat))
    dal = Counter(manipulatif_dali(et[f["kayit_id"]]["anomali_turleri"])
                  for f in secilen if et[f["kayit_id"]]["aciklama_kategorisi"] == "manipulatif")
    print("    manipulatif dalları:", dict(dal))


if __name__ == "__main__":
    main()
