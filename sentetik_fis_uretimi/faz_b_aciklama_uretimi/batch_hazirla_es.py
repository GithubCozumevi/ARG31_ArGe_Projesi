"""
Cift gerektiren anomalilerde OKSUZ kalan kayitlarin ESLERINI batch'e alir.

`mukerrer_fis_yukleme` ve `fatura_no_cakismasi` iliskiseldir: bir fisin mukerrer
oldugu ancak esinin varliginda anlasilir. `batch_hazirla` kota secimi tur
farkindali ama CIFT farkindali degil -- etiketli uyeyi bilincli aliyor (%78-81),
esi ise siradan bir TEMIZ fatura oldugu icin yalniz dolgu oranidan (%19,7)
seciliyor. Sonuc: 584 mukerrer kaydin 459'u, 366 cakisma kaydinin 294'u oksuz.

Bu modul havuzdan yalniz o eksik esleri ceker. Havuzda ciftlerin %100'u tam.

Anahtar `(satici_vkn, fatura_no)`: yalniz `fatura_no` dogal cakismalari da cift
sayardi (farkli saticilar ayni numarayi kullanabilir, CLAUDE.md §7).

Batch semasi mevcut batch'lerle BIREBIR ayni olmali; runner bu alanlari okur.

    python -m faz_b_aciklama_uretimi.batch_hazirla_es --cikti-dizini data/aciklama_es
"""

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from ortak.cift_grup import cift_grup_anahtari

VARSAYILAN_GIRDI_JSON = "data/faturalar_aciklamali.json"
ILISKISEL_ANOMALILER = ("mukerrer_fis_yukleme", "fatura_no_cakismasi")


def batch_kaydi(fatura: dict, etiket: dict) -> dict:
    return {
        "kayit_id": fatura["kayit_id"],
        "fatura_no": fatura["fatura_no"],
        "fatura_tarihi": fatura["fatura_tarihi"],
        "satici_unvan": fatura["satici_unvan"],
        "kalemler": fatura["kalemler"],
        "aciklama_kategorisi": etiket["aciklama_kategorisi"],
        "is_anomali": etiket["is_anomali"],
        "anomali_turleri": etiket["anomali_turleri"],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Oksuz iliskisel kayitlarin eslerini batch'e al")
    ap.add_argument("--faturalar", default="data/faturalar.json")
    ap.add_argument("--etiketler", default="data/faturalar_etiketler.json")
    ap.add_argument("--girdi-json", default=VARSAYILAN_GIRDI_JSON,
                    help="mevcut veri seti; esi burada olmayanlar cekilir")
    ap.add_argument("--cikti-dizini", required=True)
    ap.add_argument("--batch-size", type=int, default=1000)
    # Dosya adlari onceki kosunun devami olarak numaralanir: cikti dosyalari tek
    # dizinde birlestirilecegi icin batch_0001_ciktilar.json cakisir ve 1000
    # aciklamayi sessizce ezer.
    ap.add_argument("--ilk-batch-no", type=int, default=26,
                    help="ilk batch numarasi (onceki kosu 1-25 kullandi)")
    args = ap.parse_args()

    print(f"[+] havuz okunuyor ({args.faturalar})...")
    faturalar = json.loads(Path(args.faturalar).read_text(encoding="utf-8"))
    etiketler = json.loads(Path(args.etiketler).read_text(encoding="utf-8"))
    mevcut = {r["kayit_id"] for r in json.loads(Path(args.girdi_json).read_text(encoding="utf-8"))}

    et = {e["kayit_id"]: e for e in etiketler}
    gr = {f["kayit_id"]: f for f in faturalar}
    anahtar: dict[tuple, list[str]] = defaultdict(list)
    for f in faturalar:
        anahtar[cift_grup_anahtari(f)].append(f["kayit_id"])

    eksik: dict[str, str] = {}   # es kayit_id -> hangi anomali yuzunden cekildi
    sayac = Counter()
    for kid in mevcut:
        turler = set(et[kid]["anomali_turleri"])
        iliskisel = turler & set(ILISKISEL_ANOMALILER)
        if not iliskisel:
            continue
        f = gr[kid]
        for es in anahtar[cift_grup_anahtari(f)]:
            if es != kid and es not in mevcut and es not in eksik:
                eksik[es] = sorted(iliskisel)[0]
                sayac[sorted(iliskisel)[0]] += 1

    print(f"[+] mevcut veri seti: {len(mevcut)} kayit")
    for a in ILISKISEL_ANOMALILER:
        print(f"    {a:22s} eksik es: {sayac[a]}")
    print(f"[+] cekilecek es TOPLAM: {len(eksik)}")
    if not eksik:
        print("[+] eksik es yok, yapilacak bir sey yok.")
        return

    # Getirilen esler cogunlukla TEMIZ olmali: cift, ayni fisin ilk (mesru)
    # yuklemesidir. Anomalili cikanlar, kopyalanmadan once zaten anomali almis
    # faturalardir; iliskisel etiket TASIMAMALARI beklenir.
    dagilim = Counter("+".join(sorted(et[k]["anomali_turleri"])) or "(temiz)" for k in eksik)
    print("\n[+] getirilen eslerin etiket dagilimi:")
    for k, n in dagilim.most_common(6):
        print(f"      {n:4d}x  {k}")
    kirli = [k for k in eksik if set(et[k]["anomali_turleri"]) & set(ILISKISEL_ANOMALILER)]
    if kirli:
        print(f"[!] {len(kirli)} es KENDISI de iliskisel etiketli, beklenmiyordu (kontrol et).")

    dizin = Path(args.cikti_dizini)
    dizin.mkdir(parents=True, exist_ok=True)
    secilen = sorted(eksik)
    batchler = []
    for i in range(0, len(secilen), args.batch_size):
        dilim = secilen[i:i + args.batch_size]
        ad = f"batch_{args.ilk_batch_no + i // args.batch_size:04d}.json"
        (dizin / ad).write_text(
            json.dumps([batch_kaydi(gr[k], et[k]) for k in dilim], ensure_ascii=False, indent=2),
            encoding="utf-8")
        batchler.append({"dosya": ad, "cikti_dosyasi": ad.replace(".json", "_ciktilar.json"),
                         "adet": len(dilim), "tamam": False})

    (dizin / "durum.json").write_text(json.dumps({
        "config": {"amac": "oksuz iliskisel kayitlarin esleri",
                   "girdi_json": args.girdi_json, "batch_size": args.batch_size},
        "toplam_secilen": len(secilen),
        "batch_sayisi": len(batchler),
        "batchler": batchler,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n[+] {len(batchler)} batch yazildi -> {dizin}")
    for b in batchler:
        print(f"      {b['dosya']}  {b['adet']}")
    print(f"\nKaggle'a yuklenecek: {dizin} dizininin TAMAMI (batch_*.json + durum.json)")


if __name__ == "__main__":
    main()
