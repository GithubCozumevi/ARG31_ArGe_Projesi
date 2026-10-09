"""
`veri_bol.py`nin rapor mekanizmasının (gruplari_kur/hucre/rapor_uret) İngilizce
alan-adlı KOPYASI -- data/final_veriler_en için data/final_veriler/bolme_raporu.json
karşılığını üretir.

Neden kopya, import DEĞİL: final_veriler_en'in alan adları (record_id/
anomaly_types/note_quality/approval_status/seller_tax_id/receipt_no/user_note)
TR tarafından (kayit_id/anomali_turleri/aciklama_kategorisi/onay_durumu/
satici_vkn/fatura_no/aciklama_metni) farklı; veri_bol.py'nin fonksiyonları bu
alanlara sabit-adla erişiyor, doğrudan çağrılamaz.

`gruplari_kur`/`rapor_uret`'in mantığı BİREBİR aynı kalır (union-find grup
tespiti + katman bazlı rapor); yalnız alan adları çevrilir. `atama` (record_id
-> bölme) veri_bol.py'nin tekrar-bölme mantığını ÇALIŞTIRMAZ -- final_veriler_en
zaten alan_adlari.py'nin final_veriler'den TÜRETTİĞİ, sabit bir bölme; bu script
yalnız MEVCUT dosya üyeliğinden atamayı okur ve rapor üretir.

Kullanım:
    python -m faz_c_fis_gorsellestirme.final_veriler_en_rapor
"""

import json
from collections import Counter, defaultdict
from pathlib import Path

from faz_b_aciklama_uretimi.aciklama_uretim_core import _dedup_normalize

DIZIN = Path("data/final_veriler_en")
BOLMELER = ("egitim", "dogrulama", "test")
# data/final_veriler/bolme_raporu.json'daki hedef_oranlar ile AYNI (veri_bol.py
# varsayılanı) -- final_veriler_en o bölmeden türetildiği için değişmez.
ORANLAR = {"egitim": 0.7, "dogrulama": 0.15, "test": 0.15}


class Birlestirici:
    """Union-find. veri_bol.Birlestirici ile BİREBİR aynı."""

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


def cift_anahtari_en(rec: dict) -> tuple:
    """cift_grup.cift_grup_anahtari'nin İngilizce karşılığı: (seller_tax_id, receipt_no)."""
    return (rec["seller_tax_id"], rec["receipt_no"])


def gruplari_kur_en(girdiler: list[dict]) -> dict[str, list[str]]:
    """veri_bol.gruplari_kur'un portu: record_id -> grup (çift ∪ aynı-metin bileşeni)."""
    bf = Birlestirici(r["record_id"] for r in girdiler)

    cift_kume: dict[tuple, list[str]] = defaultdict(list)
    metin_kume: dict[str, list[str]] = defaultdict(list)
    for r in girdiler:
        cift_kume[cift_anahtari_en(r)].append(r["record_id"])
        metin_kume[_dedup_normalize(r["user_note"])].append(r["record_id"])

    for kume in (cift_kume, metin_kume):
        for uyeler in kume.values():
            for x in uyeler[1:]:
                bf.birlestir(uyeler[0], x)

    gruplar: dict[str, list[str]] = defaultdict(list)
    for r in girdiler:
        gruplar[bf.bul(r["record_id"])].append(r["record_id"])
    return gruplar


def hucre_en(etiket: dict) -> str:
    """veri_bol.hucre'nin portu: (is_anomaly, note_quality)."""
    return f"{'anomaly' if etiket['is_anomaly'] else 'clean'}|{etiket['note_quality']}"


def rapor_uret_en(atama: dict[str, str], etiket_map: dict[str, dict],
                   gruplar: dict[str, list[str]], oranlar: dict[str, float]) -> dict:
    """veri_bol.rapor_uret'in portu, alan adları İngilizce."""
    toplam = len(atama)
    rapor: dict = {"total_records": toplam, "group_count": len(gruplar),
                   "target_ratios": oranlar, "splits": {}}

    tur_toplam = Counter(t for e in etiket_map.values() for t in e["anomaly_types"])
    for b in BOLMELER:
        idler = [k for k, v in atama.items() if v == b]
        onay = Counter(etiket_map[k]["approval_status"] for k in idler)
        kat = Counter(etiket_map[k]["note_quality"] for k in idler)
        tur = Counter(t for k in idler for t in etiket_map[k]["anomaly_types"])
        rapor["splits"][b] = {
            "count": len(idler),
            "ratio": round(len(idler) / toplam, 4),
            "approval_status": {k: {"count": v, "ratio": round(v / len(idler), 4)}
                                 for k, v in onay.most_common()},
            "note_quality": {k: {"count": v, "ratio": round(v / len(idler), 4)}
                             for k, v in kat.most_common()},
            "anomaly_types": {t: {"count": tur[t], "type_ratio": round(tur[t] / n, 4)}
                              for t, n in tur_toplam.most_common()},
        }

    bolunen = [g for g, uyeler in gruplar.items() if len({atama[k] for k in uyeler}) > 1]
    rapor["validation"] = {
        "split_groups": len(bolunen),
        "assigned_records": toplam,
        "split_totals_match": sum(r["count"] for r in rapor["splits"].values()) == toplam,
        "largest_group": max(len(u) for u in gruplar.values()),
    }
    return rapor


def main() -> None:
    girdiler_all: list[dict] = []
    etiket_map: dict[str, dict] = {}
    atama: dict[str, str] = {}

    for b in BOLMELER:
        girdi_yol = DIZIN / f"{b}_girdi.json"
        etiket_yol = DIZIN / f"{b}_etiket.json"
        if not girdi_yol.exists():
            print(f"[!] {girdi_yol} yok, atlaniyor.")
            continue
        girdi = json.loads(girdi_yol.read_text(encoding="utf-8"))
        etiket = json.loads(etiket_yol.read_text(encoding="utf-8"))
        girdiler_all.extend(girdi)
        for e in etiket:
            etiket_map[e["record_id"]] = e
            atama[e["record_id"]] = b

    gruplar = gruplari_kur_en(girdiler_all)
    rapor = rapor_uret_en(atama, etiket_map, gruplar, ORANLAR)

    hedef = DIZIN / "split_report.json"
    hedef.write_text(json.dumps(rapor, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[+] {hedef}")
    print(f"Toplam {rapor['total_records']} kayit, {rapor['group_count']} grup "
          f"(en buyuk grup {rapor['validation']['largest_group']})")
    print(f"Bolunen grup: {rapor['validation']['split_groups']} "
          f"(0 olmali -- grup asla bolunmemeli)")
    for b in BOLMELER:
        s = rapor["splits"][b]
        print(f"  {b:10s} {s['count']:6d} (%{100*s['ratio']:.1f})")


if __name__ == "__main__":
    main()
