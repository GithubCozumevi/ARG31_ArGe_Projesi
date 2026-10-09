"""
Nihai veri setini egitim / dogrulama / test olarak boler (varsayilan 70/15/15).

GRUPLU bolme, rastgele DEGIL: ayni kaydin iki bolmede gorunmesine yol acan iki
kanal var. (1) Mukerrer cift -- f2, f1'in `kayit_id` ve `aciklama_metni` disinda
birebir kopyasi. (2) Ayni metin -- yakin kopyalar kasitli elenmedi, ama ayni dize
iki bolmeye dagilirsa ezber test skorunu sisirir. Iki eksen cakisabildigi icin
birlesik bilesen olarak cozulur (union-find), grup asla bolunmez.

Katmanlama `(is_anomali, aciklama_kategorisi)` hucresi + `anomali_turleri`
acigidir; `onay_durumu` birinciden deterministik turedigi icin ayrica dengelenmez.

Cikti bolme basina IKI dosyadir (girdi + etiket AYRI): tek dosya etiketi
feature'in yanina koyar. `kayit_id` join icin durur, feature DEGILDIR.

Varsayilan RAPOR modu; `--uygula` dosyalari yazar.

    python -m faz_c_fis_gorsellestirme.veri_bol
    python -m faz_c_fis_gorsellestirme.veri_bol --uygula
"""

import argparse
import json
import random
from collections import Counter, defaultdict
from pathlib import Path

from faz_b_aciklama_uretimi.aciklama_uretim_core import _dedup_normalize
from ortak.cift_grup import cift_grup_anahtari

VARSAYILAN_GIRDI_JSON = "data/faturalar_aciklamali.json"
VARSAYILAN_ETIKET_JSON = "data/faturalar_aciklamali_etiketler.json"
VARSAYILAN_CIKTI_DIZINI = "data/final_veriler"

BOLMELER = ("egitim", "dogrulama", "test")

# Cift anahtari `(satici_vkn, fatura_no)`. Tum-alan imzasi YETMEZ: mukerrer cift
# birebir kopyadir ve yakalanir ama `fatura_no_cakismasi` "ayni numara, farkli
# icerik" demektir, imzasi tutmaz (olculdu: 367 ciftin 164'u faz arasi ayriliyordu).
# Yalniz `fatura_no` da olmaz, farkli saticilar ayni numarayi kullanabilir.


class Birlestirici:
    """Union-find. Iki gruplama ekseni cakistiginda tek bilesene indirger."""

    def __init__(self, kimlikler):
        self.ust = {k: k for k in kimlikler}

    def bul(self, x):
        while self.ust[x] != x:
            self.ust[x] = self.ust[self.ust[x]]
            x = self.ust[x]
        return x

    def birlestir(self, a, b):
        ra, rb = self.bul(a), self.bul(b)
        if ra != rb:
            self.ust[ra] = rb


def gruplari_kur(girdiler: list[dict]) -> dict[str, list[str]]:
    """kayit_id -> grup. Grup = ayni fatura (cift) ∪ ayni normalize metin bileseni."""
    bf = Birlestirici(r["kayit_id"] for r in girdiler)

    cift_kume: dict[tuple, list[str]] = defaultdict(list)
    metin_kume: dict[str, list[str]] = defaultdict(list)
    for r in girdiler:
        cift_kume[cift_grup_anahtari(r)].append(r["kayit_id"])
        metin_kume[_dedup_normalize(r["aciklama_metni"])].append(r["kayit_id"])

    for kume in (cift_kume, metin_kume):
        for uyeler in kume.values():
            for x in uyeler[1:]:
                bf.birlestir(uyeler[0], x)

    gruplar: dict[str, list[str]] = defaultdict(list)
    for r in girdiler:
        gruplar[bf.bul(r["kayit_id"])].append(r["kayit_id"])
    return gruplar


def hucre(etiket: dict) -> str:
    """Katmanlama hucresi: (is_anomali, aciklama_kategorisi)."""
    return f"{'anomali' if etiket['is_anomali'] else 'temiz'}|{etiket['aciklama_kategorisi']}"


def bolmelere_ata(gruplar: dict[str, list[str]], etiket_map: dict[str, dict],
                  oranlar: dict[str, float], seed: int) -> dict[str, str]:
    """kayit_id -> bolme adi. Grup, aciklarini en cok kapatan bolmeye gider.

    Buyukler ONCE: 149'luk bir grup sona kalirsa sapmayi kapatacak kucuk grup
    kalmaz. Acik MUTLAK sayidir, boylece bolme boyutu ayrica modellenmez.
    Tur terimi olmadan turler savruluyordu (mukerrer_fis_yukleme testte %10,1);
    temiz kayitlarda sifir oldugu icin ana dengeyi bozmaz."""
    rnd = random.Random(seed)
    toplam_hucre = Counter(hucre(etiket_map[kid]) for kid in etiket_map)
    toplam_tur = Counter(t for e in etiket_map.values() for t in e["anomali_turleri"])
    hedef = {b: {h: n * oranlar[b] for h, n in toplam_hucre.items()} for b in BOLMELER}
    hedef_tur = {b: {t: n * oranlar[b] for t, n in toplam_tur.items()} for b in BOLMELER}
    mevcut = {b: Counter() for b in BOLMELER}
    mevcut_tur = {b: Counter() for b in BOLMELER}

    # Sira RASTGELE olmali. Buyukten kucuge sirali gitmek cok uyeli gruplarin
    # TAMAMINI egitime yiginiyordu: kosunun basinda egitimin mutlak acigi
    # digerlerinin ~4,7 kati, ilk islenen her grup oraya dusuyor (olculdu: 584
    # mukerrer ciftin 584'u egitimde). Rastgele sirada grup boyutu fazla
    # korelasyon kurmaz, ciftler de 70/15/15'e yayilir.
    sirali = sorted(gruplar.values(), key=lambda uyeler: uyeler[0])
    rnd.shuffle(sirali)
    atama: dict[str, str] = {}
    for uyeler in sirali:
        gh = Counter(hucre(etiket_map[kid]) for kid in uyeler)
        gt = Counter(t for kid in uyeler for t in etiket_map[kid]["anomali_turleri"])
        puanlar = {
            b: sum(adet * (hedef[b][h] - mevcut[b][h]) for h, adet in gh.items())
               + sum(adet * (hedef_tur[b][t] - mevcut_tur[b][t]) for t, adet in gt.items())
            for b in BOLMELER
        }
        en_iyi = max(puanlar.values())
        secim = rnd.choice([b for b in BOLMELER if puanlar[b] == en_iyi])
        mevcut[secim].update(gh)
        mevcut_tur[secim].update(gt)
        for kid in uyeler:
            atama[kid] = secim
    return atama


def rapor_uret(atama: dict[str, str], etiket_map: dict[str, dict],
               gruplar: dict[str, list[str]], oranlar: dict[str, float]) -> dict:
    toplam = len(atama)
    rapor: dict = {"toplam_kayit": toplam, "grup_sayisi": len(gruplar),
                   "hedef_oranlar": oranlar, "bolmeler": {}}

    tur_toplam = Counter(t for e in etiket_map.values() for t in e["anomali_turleri"])
    for b in BOLMELER:
        idler = [k for k, v in atama.items() if v == b]
        onay = Counter(etiket_map[k]["onay_durumu"] for k in idler)
        kat = Counter(etiket_map[k]["aciklama_kategorisi"] for k in idler)
        tur = Counter(t for k in idler for t in etiket_map[k]["anomali_turleri"])
        rapor["bolmeler"][b] = {
            "adet": len(idler),
            "oran": round(len(idler) / toplam, 4),
            "onay_durumu": {k: {"adet": v, "oran": round(v / len(idler), 4)}
                            for k, v in onay.most_common()},
            "aciklama_kategorisi": {k: {"adet": v, "oran": round(v / len(idler), 4)}
                                    for k, v in kat.most_common()},
            # Oran, bolmeye degil TURUN toplamina gore: sapma ancak boyle gorunur.
            "anomali_turleri": {t: {"adet": tur[t], "turun_orani": round(tur[t] / n, 4)}
                                for t, n in tur_toplam.most_common()},
        }

    bolunen = [g for g, uyeler in gruplar.items() if len({atama[k] for k in uyeler}) > 1]
    rapor["dogrulama"] = {
        "bolunen_grup": len(bolunen),
        "atanan_kayit": toplam,
        "bolme_toplamlari_esit_mi": sum(r["adet"] for r in rapor["bolmeler"].values()) == toplam,
        "en_buyuk_grup": max(len(u) for u in gruplar.values()),
    }
    return rapor


def raporu_yazdir(rapor: dict) -> None:
    print(f"\n[+] Toplam {rapor['toplam_kayit']} kayit, {rapor['grup_sayisi']} grup "
          f"(en buyuk grup {rapor['dogrulama']['en_buyuk_grup']})")
    for b, r in rapor["bolmeler"].items():
        hedef = rapor["hedef_oranlar"][b]
        print(f"\n--- {b.upper()}: {r['adet']} kayit  (%{100 * r['oran']:.2f}, hedef %{100 * hedef:.0f})")
        for eksen in ("onay_durumu", "aciklama_kategorisi"):
            satir = "  ".join(f"{k}=%{100 * v['oran']:.1f}" for k, v in r[eksen].items())
            print(f"    {eksen:20s} {satir}")

    print("\n--- anomali turlerinin bolmeler arasi dagilimi (turun kendi toplamina gore) ---")
    turler = rapor["bolmeler"]["egitim"]["anomali_turleri"]
    print(f"    {'tur':34s} {'egitim':>8s} {'dogrulama':>10s} {'test':>8s}")
    for t in turler:
        satir = "".join(f"{100 * rapor['bolmeler'][b]['anomali_turleri'][t]['turun_orani']:9.1f}%"
                        .rjust(10 if b == "dogrulama" else 9) for b in BOLMELER)
        print(f"    {t:34s}{satir}")

    d = rapor["dogrulama"]
    durum = "TAMAM" if d["bolunen_grup"] == 0 and d["bolme_toplamlari_esit_mi"] else "HATA"
    print(f"\n[{'+' if durum == 'TAMAM' else '!'}] Dogrulama: {durum} "
          f"(bolunen grup: {d['bolunen_grup']}, toplam tutuyor: {d['bolme_toplamlari_esit_mi']})")


def main() -> None:
    ap = argparse.ArgumentParser(description="Nihai veri setini egitim/dogrulama/test olarak bol")
    ap.add_argument("--girdi-json", default=VARSAYILAN_GIRDI_JSON)
    ap.add_argument("--etiket-json", default=VARSAYILAN_ETIKET_JSON)
    ap.add_argument("--cikti-dizini", default=VARSAYILAN_CIKTI_DIZINI)
    ap.add_argument("--egitim-orani", type=float, default=0.70)
    ap.add_argument("--dogrulama-orani", type=float, default=0.15)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--uygula", action="store_true", help="Dosyalari yaz (varsayilan: rapor)")
    args = ap.parse_args()

    test_orani = round(1.0 - args.egitim_orani - args.dogrulama_orani, 6)
    if test_orani <= 0:
        print(f"HATA: egitim+dogrulama oranlari 1.0'i asiyor (test payi {test_orani}).")
        return
    oranlar = {"egitim": args.egitim_orani, "dogrulama": args.dogrulama_orani, "test": test_orani}

    with open(args.girdi_json, "r", encoding="utf-8") as f:
        girdiler = json.load(f)
    with open(args.etiket_json, "r", encoding="utf-8") as f:
        etiketler = json.load(f)

    etiket_map = {e["kayit_id"]: e for e in etiketler}
    girdi_idler = {r["kayit_id"] for r in girdiler}
    if girdi_idler != set(etiket_map):
        print(f"HATA: girdi ve etiket kayit_id kumeleri AYNI DEGIL "
              f"(girdi {len(girdi_idler)}, etiket {len(etiket_map)}, "
              f"kesisim {len(girdi_idler & set(etiket_map))}).\n"
              f"     aciklama_birlestir.py ve onay_durumu_ata.py ayni dizinle kosuldu mu?")
        return

    print(f"[+] {len(girdiler)} kayit okundu.")
    gruplar = gruplari_kur(girdiler)
    atama = bolmelere_ata(gruplar, etiket_map, oranlar, args.seed)
    rapor = rapor_uret(atama, etiket_map, gruplar, oranlar)
    raporu_yazdir(rapor)

    if rapor["dogrulama"]["bolunen_grup"]:
        print("[!] Bolunen grup var, dosya YAZILMADI.")
        return

    if not args.uygula:
        print("\n[i] RAPOR modu, dosya yazilmadi. Yazmak icin: --uygula")
        return

    dizin = Path(args.cikti_dizini)
    dizin.mkdir(parents=True, exist_ok=True)
    girdi_map = {r["kayit_id"]: r for r in girdiler}
    print()
    for b in BOLMELER:
        idler = sorted(k for k, v in atama.items() if v == b)
        for ek, kaynak in (("girdi", girdi_map), ("etiket", etiket_map)):
            yol = dizin / f"{b}_{ek}.json"
            with open(yol, "w", encoding="utf-8") as f:
                json.dump([kaynak[k] for k in idler], f, ensure_ascii=False, indent=2)
            print(f"[+] {yol}  ({len(idler)} kayit)")

    rapor_yolu = dizin / "bolme_raporu.json"
    with open(rapor_yolu, "w", encoding="utf-8") as f:
        json.dump(rapor, f, ensure_ascii=False, indent=2)
    print(f"[+] {rapor_yolu}")


if __name__ == "__main__":
    main()
